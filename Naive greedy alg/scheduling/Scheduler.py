import logging
from dataclasses import astuple, dataclass
from math import ceil
from operator import attrgetter, methodcaller
from random import random, sample, uniform
from statistics import mean, pstdev

from .Job import Job
from .Server import Server


@dataclass
class SchedulerConfig:
    shutdown_prob: float = 0.01
    shutdown_weight: float = 0.1
    reconfig_prob: int = 0.1
    reconfig_weight: float = 0.5
    shutdown_time: int = 800

    alpha_min_server_lower_range: float = 0.4
    alpha_min_server_mid_range: float = 0.6
    alpha_min_server_upper_range: float = 1

    alpha_lower: float = 0.65
    alpha_mid: float = 0.75

    @classmethod
    def random(cls):
        c = SchedulerConfig()
        c.shutdown_prob = uniform(0.001, 1.0)
        c.shutdown_weight = uniform(0.01, 1.0)
        c.reconfig_prob = uniform(0.001, 1.0)
        c.reconfig_weight = uniform(0.01, 1.0)
        c.shutdown_time = uniform(370, 1200)
        c.alpha_min_server_lower_range = uniform(0.01, 0.4)
        c.alpha_min_server_mid_range = uniform(
            c.alpha_min_server_lower_range, c.alpha_min_server_lower_range * 2
        )
        c.alpha_min_server_upper_range = uniform(c.alpha_min_server_mid_range, 1)
        c.alpha_lower = uniform(0.5, 0.7)
        c.alpha_mid = uniform(c.alpha_lower, 0.9)
        return c

    def to_dict(self):
        dict_obj = self.__dict__
        return dict_obj

    def to_list(self):
        return list(astuple(self))


@dataclass
class SchedulerStats:
    complete_jobs: dict
    start_time: int
    end_time: int
    work_duration: int
    reconfig_count: int
    power_off_count: int
    min_stretch_time: int
    max_stretch_time: int
    mean_stretch_time: float
    stdev_stretch_time: float
    average_power_norm: float
    cost: float

    def to_dict(self):
        dict_obj = self.__dict__
        dict_obj.pop("complete_jobs")
        return dict_obj


class Scheduler(object):
    def __init__(self, server_count, conf):
        self.servers = [Server(i) for i in range(server_count)]
        self.conf = conf
        self.req_queue = []
        self.req_by_id = {}
        self.active_jobs = []
        self.complete_jobs = {}

        self.logger = logging.getLogger(__name__)

    def is_working(self):
        return self.req_queue or (
            self.active_jobs and not all(job.is_power_off() for job in self.active_jobs)
        )

    def stop(self, time):
        for job in self.active_jobs:
            job.end_time = time
        self._remove_job(*self.active_jobs)

    def schedule(self, job_request):
        self.req_queue.append(job_request)
        self.req_queue.sort(key=attrgetter("sub_time"), reverse=True)
        self.req_by_id[job_request.id] = job_request

    # Add shrink logic
    # Start shrinking if len(req_queue) > conf_value_1
    # compute minimum number of servers required to start jobs
    # freeable_srv_count = max_freeable_srvs * conf_value_3
    # filter + order active jobs by data if data > conf_value_2
    # compute maximum freeable servers

    # try reduce to free enough servers (minimum required)
    def update_schedule(self, time):
        self._remove_job(*[job for job in self.active_jobs if job.is_complete(time)])

        av_servers = [server for server in self.servers if not server.is_busy(time)]
        self.logger.debug(f"{time}: av_servers: {av_servers}")
        while self.req_queue and av_servers:
            job_req = self.req_queue[-1]
            job_servers = self._allocate_servers(av_servers, job_req)
            if not job_servers:
                break
            self.logger.debug(f"{time} req {job_req}")
            job = Job.from_request(job_req, job_servers, start_time=time)
            self._start_job(job)
            self.req_queue.pop()
            av_servers = [server for server in av_servers if server not in job_servers]

        # Add config value to only consider jobs with data < conf_value
        jobs_by_mass = sorted(
            self.active_jobs, key=methodcaller("remaining_mass", time)
        )
        while jobs_by_mass and av_servers:
            job = jobs_by_mass[0]
            if self._is_job_reconfigurable(job, av_servers, time):
                av_servers = self._reconfigure_job(job, av_servers, time)
            jobs_by_mass.pop(0)

        for server in list(av_servers):
            if not self._shutdown_server(av_servers):
                break
                # if (
                #     random()
                #     < ((len(av_servers) / len(self.servers)) ** self.conf.shutdown_weight)
                #     * self.conf.shutdown_prob
                # ):
            power_off = Job.make_power_off(
                [server], start_time=time, duration=self.conf.shutdown_time
            )
            self._start_job(power_off)
            av_servers.remove(server)

    def _start_job(self, *jobs):
        for job in jobs:
            self.active_jobs.append(job)
            self.logger.debug(
                f"new {job} on {len(job.servers)} servers, active -> {self.active_jobs}"
            )
            for server in job.servers:
                server.add_job(job)

    def _remove_job(self, *jobs):
        for job in jobs:
            self.active_jobs.remove(job)
            self.logger.debug(f"remove {job}, active: {self.active_jobs}")
            for server in job.servers:
                server.remove_job(job)

            completed_jobs = self.complete_jobs.get(job.id, [])
            completed_jobs.append(job)
            self.complete_jobs[job.id] = completed_jobs

    def _reconfigure_job(self, job, av_servers, time):
        job.interupt(time)
        extra_srv_count = min(job.max_server_count - job.server_count, len(av_servers))
        extra_srvs = sample(av_servers, extra_srv_count)
        job_servers = job.servers + extra_srvs
        av_servers = [server for server in av_servers if server not in extra_srvs]

        self.logger.debug(f"{time} reconfigure {job} with {len(job_servers)} servers")
        reconfig_job, job_rest = job.reconfigure(job_servers, time)
        self._remove_job(job)
        self._start_job(reconfig_job, job_rest)

        return av_servers

    def _is_job_reconfigurable(self, job, av_servers, time):
        if not job.is_reconfigurable():
            return False

        extra_srv_count = min(job.max_server_count - job.server_count, len(av_servers))
        return extra_srv_count > 0
        # return (
        #     random()
        #     < (
        #         ((len(job.servers) + extra_srv_count) / job.max_server_count)
        #         ** self.conf.reconfig_weight
        #     )
        #     * self.conf.reconfig_prob
        # )

    def _shutdown_server(self, av_servers):
        if not self.req_queue:
            return True

        required_servers = sum(req.min_num_servers for req in self.req_queue)
        return len(av_servers) > required_servers

    def _allocate_servers(self, available_servers, job_req):
        limits = [
            self.conf.alpha_min_server_lower_range,
            self.conf.alpha_min_server_mid_range,
            self.conf.alpha_min_server_upper_range,
        ]
        alpha = next((limit for limit in limits if job_req.alpha < limit), 1)
        min_servers = ceil(alpha * len(self.servers))
        min_servers = min(min_servers, job_req.max_num_servers, len(available_servers))
        # self.logger.debug(f"Try allocating {job_req} with {min_servers} ")
        if min_servers < job_req.min_num_servers:
            return []
        return sample(available_servers, k=min_servers)

    #############################################

    def stats(self, stretch_time_weight, energy_weight):
        stretch_times = self._stretch_times()
        start_time, end_time = self._work_span()
        return SchedulerStats(
            complete_jobs=self.complete_jobs,
            start_time=start_time,
            end_time=end_time,
            work_duration=self._work_duration(),
            reconfig_count=self._num_reconfig_job(),
            power_off_count=self._num_power_off(),
            min_stretch_time=min(stretch_times),
            max_stretch_time=max(stretch_times),
            mean_stretch_time=mean(stretch_times),
            stdev_stretch_time=pstdev(stretch_times),
            average_power_norm=self._normalized_average_power(),
            cost=self._cost_function(stretch_times, stretch_time_weight, energy_weight),
        )

    def _work_span(self):
        jobs = [job for jobs_by_id in self.complete_jobs.values() for job in jobs_by_id]
        first = min(jobs, key=attrgetter("start_time"))
        last = max(jobs, key=attrgetter("end_time"))
        return first.start_time, last.end_time

    def _work_duration(self):
        jobs = [job for jobs_by_id in self.complete_jobs.values() for job in jobs_by_id]
        first = min(jobs, key=attrgetter("start_time"))
        last = max(jobs, key=attrgetter("end_time"))
        return last.end_time - first.start_time

    def _stretch_times(self):
        def stretch_time(job_req):
            last = self.complete_jobs[job_req.id][-1]
            return (last.end_time - job_req.sub_time) / job_req.mass

        return [stretch_time(j) for j in self.req_by_id.values()]

    def _normalized_average_power(self):
        def average_power(work_duration):
            jobs = [
                job for jobs_by_id in self.complete_jobs.values() for job in jobs_by_id
            ]
            total_energy = 0
            area = 0
            for job in jobs:
                srv_count = len(job.servers)
                if job.is_power_off():
                    total_energy += Server.Consumption.reboot(job.duration) * srv_count
                else:
                    total_energy += Server.Consumption.active(job.duration) * srv_count
                area += job.duration * srv_count
            # adding idle time
            energy_idle = Server.Consumption.idle(
                work_duration * len(self.servers) - area
            )
            return total_energy + energy_idle

        work_duration = self._work_duration()
        idle_power = Server.Consumption.idle(work_duration) * len(self.servers)
        return average_power(work_duration) / idle_power

    def _num_reconfig_job(self):
        return sum(
            sum(job.is_reconfiguration() for job in jobs)
            for jobs in self.complete_jobs.values()
        )

    def _num_power_off(self):
        return len(self.complete_jobs.get(Job.POWER_OFF_ID, []))

    def _cost_function(self, stretch_times, stretch_time_weight, energy_weight):
        return (
            stretch_time_weight * mean(stretch_times)
            + energy_weight * self._normalized_average_power()
        )
