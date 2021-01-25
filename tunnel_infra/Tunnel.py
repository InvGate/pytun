import select
import socket
import threading

from paramiko import SSHException


class Tunnel(object):

    def __init__(self, name, server_port, remote_host, remote_port, transport, logger, keep_alive_time=30,
                 alert_senders=None):
        self.name = name
        self.timer = None
        self.server_port = server_port
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.transport = transport
        self.logger = logger
        self.keep_alive_time = keep_alive_time
        self.alert_senders = alert_senders
        self.failed = False

    def handler(self, chan, host, port):
        with socket.socket() as sock:
            try:
                sock.settimeout(2)
                sock.connect((host, port))
            except Exception as e:
                self.logger.exception("Forwarding request to %s:%d failed: %r" % (host, port, e))
                if self.alert_senders:
                    message = "Failed to Establish connection to %s:%d with error: %r" % (host, port, e)
                    for each in self.alert_senders:
                        try:
                            each.send_alert(self.name, message=message)
                        except Exception as e:
                            self.logger.exception("Failed to send alert: %r", e)
                return

            self.logger.debug(
                "Connected!  Connector open %r -> %r -> %r"
                , chan.origin_addr, chan.getpeername(), (host, port)
            )
            try:
                while True:
                    r, w, x = select.select([sock, chan], [], [])
                    if sock in r:
                        data = sock.recv(1024)
                        if len(data) == 0:
                            break
                        chan.send(data)
                    if chan in r:
                        data = chan.recv(1024)
                        if len(data) == 0:
                            break
                        sock.send(data)
                chan.close()
                self.logger.debug("Connector closed from %r", chan.origin_addr)
            except ConnectionResetError as e:
                self.logger.debug(e)
            except Exception as e:
                self.logger.exception(e)

    def validate_tunnel_up(self):
        self.logger.debug("Going to check if connector is up")
        try:
            self.transport.send_ignore()
        except Exception as e:
            self.logger.exception("Connector down! %s", e)
            self.failed=True
            return
        if not self.transport.is_active():
            self.logger.exception("Connector down! Transport is not active")
            self.failed = True
            return
        try:
            chn = self.transport.open_session(timeout=30)
            chn.close()
        except Exception as e:
            self.logger.exception("Connector down! Failed to start a check session %s with timeout 30 seconds", e)
            self.failed = True
            return
        self.timer = threading.Timer(self.keep_alive_time, self.validate_tunnel_up)
        self.timer.start()

    def reverse_forward_tunnel(self):
        self.transport.request_port_forward("", self.server_port)
        self.timer = threading.Timer(30, self.validate_tunnel_up)
        self.timer.start()
        while True:
            chan = self.transport.accept(10)
            if self.failed:
                return
            if chan is None:
                continue
            thr = threading.Thread(
                target=self.handler, args=(chan, self.remote_host, self.remote_port)
            )
            thr.setDaemon(True)
            thr.start()

    def stop(self):
        if self.timer:
            self.timer.cancel()
