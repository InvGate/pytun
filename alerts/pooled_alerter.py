from concurrent.futures.process import ProcessPoolExecutor
from concurrent.futures.thread import  ThreadPoolExecutor
from multiprocessing import Process

from ratelimit import RateLimitException

from alerts.alert_sender import AlertSender


class PooledAlerter(AlertSender):

    def get_default_pool(self):
        raise NotImplementedError()

    def __init__(self, alerters, process_pool=None):
        self.alerters = alerters
        if process_pool is None:
            process_pool = ProcessPoolExecutor(1)
        self.pool = process_pool

    def add_alerter(self, alerter):
        self.alerters.append(alerter)

    def send_alert(self, tunnel_name, message=None, exception_on_failure=False):
        for each in self.alerters:
            try:
                future = self.pool.submit(each.send_alert, tunnel_name, message, exception_on_failure)
            except RateLimitException:
                pass
            else:
                if exception_on_failure:
                    error = future.exception()
                    if error:
                        raise error


class DifferentThreadAlert(PooledAlerter):
    def get_default_pool(self):
        return ThreadPoolExecutor(1)






