class AlertSender(object):
    def send_alert(self, tunnel_name, message=None, exception_on_failure=False):
        raise NotImplementedError