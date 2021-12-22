from concurrent.futures.process import ProcessPoolExecutor
from concurrent.futures.thread import  ThreadPoolExecutor
from multiprocessing import Process

from ratelimit import RateLimitException

from alerts.alert_sender import AlertSender


class PooledAlerter(AlertSender):

    def get_default_pool(self):
        raise NotImplementedError()

    def __init__(self, alerters, logger, process_pool=None):
        self.alerters = alerters
        self.logger = logger
        if process_pool is None:
            process_pool = ProcessPoolExecutor(1)
        self.pool = process_pool

    def add_alerter(self, alerter):
        self.alerters.append(alerter)

    def send_alert(self, tunnel_name, message=None, exception_on_failure=False):
        for each in self.alerters:
            future = self.pool.submit(each.send_alert, tunnel_name, message, exception_on_failure)

            error = future.exception()
            if isinstance(error, RateLimitException):
                self.logger.warning(f"{each.__class__.__name__} rate limit exceeded "
                                    f"while sending alert for tunnel {tunnel_name}. "
                                    f"The alert will not be sent until the rate limiter allows it")

            if exception_on_failure and error:
                raise error


class DifferentThreadAlert(PooledAlerter):
    def get_default_pool(self):
        return ThreadPoolExecutor(1)






