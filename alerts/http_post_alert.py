import json

from alerts.alert_sender import AlertSender

import requests

class HTTPPostAlertSender(AlertSender):
    def __init__(self, post_url, user, password, logger):
        self.post_url = post_url
        self.user = user
        self.password = password
        self.logger = logger

    def send_alert(self, tunnel_name, message=None, exception_on_failure=False):
        try:
            message = message or "Tunnel Down!"
            data = {'tunnel_name': tunnel_name, 'message':message}
            requests.post(self.post_url, auth=(self.user, self.password), data=json.dumps(data))
        except Exception as e:
            self.logger.exception("Failed to post alert %v", e)
            if exception_on_failure:
                raise e