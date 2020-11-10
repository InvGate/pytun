import socket

from alerts.alert_sender import AlertSender


class ConnectionCheck:
    def __init__(self, logger, alert_sender:AlertSender=None):
        self.logger = logger
        self.alert_sender = alert_sender

    def test_connection(self, tunnel_name, remote_host, remote_port):
        with socket.socket() as sock:
            try:
                sock.settimeout(5)
                sock.connect((remote_host, remote_port))
                self.logger.debug("Connection to service %s:%s succesfully established", remote_host, remote_port)
                return True
            except Exception as e:
                msg = "Failed to connect with service %s:%s. Please check that the service is up and listening for connections, that you have network access, that there is not a firewall blocking the connection or that remote_host and remote_port in your config are correct. Error %r" %(remote_host, remote_port, e)
                self.logger.exception(msg)
                if self.alert_sender:
                    self.alert_sender.send_alert(tunnel_name, msg)
                return False

