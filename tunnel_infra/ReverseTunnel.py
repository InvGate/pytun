import contextlib
import logging
import select
import socket
import threading

import paramiko
from paramiko.client import SSHClient

from alerts.alert_sender import AlertSender


class ReverseTunnel:
    def __init__(
        self,
        name: str,
        *,
        recipient_host: str,
        recipient_port: int,
        client: SSHClient,
        port_to_forward: int,
        logger: logging.Logger,
        keep_alive_time: int = 30,
        alert_senders: list[AlertSender] | None = None
    ):
        """
        Create a SSH reverse tunnel by asking the ``client`` to forward the data that it receives in the
        ``port_to_forward`` port to the recipient

        :param name: Name of the tunnel
        :param recipient_host: Host that's going to receive the data forwarded by the client
        :param recipient_port: Port that's going to receive the data forwarded by the client
        :param client: Client to forward data from
        :param port_to_forward: Port from where the client forwards the data
        """
        self.name = name
        self.recipient_host = recipient_host
        self.recipient_port = recipient_port
        self.client = client
        self.port_to_forward = port_to_forward
        self.logger = logger
        self.keep_alive_time = keep_alive_time
        self.alert_senders = alert_senders

        self.transport: paramiko.transport.Transport | None = None
        self.timer: threading.Timer | None = None
        self.failed = False

    def handler(self, chan, host, port):
        """
        Forward data received through the channel to host:port using a socket and forward data received through the
        socket to the channel.
        """
        with socket.socket() as sock:
            try:
                sock.settimeout(2)
                sock.connect((host, port))
            except Exception as e:
                self.logger.exception(
                    "Forwarding request to %s:%d failed: %r" % (host, port, e)
                )
                if self.alert_senders:
                    message = (
                        "Failed to Establish connection to %s:%d with error: %r"
                        % (host, port, e)
                    )
                    for each in self.alert_senders:
                        try:
                            each.send_alert(self.name, message=message)
                        except Exception as e:
                            self.logger.exception("Failed to send alert: %r", e)
                return

            self.logger.debug(
                "Connected!  Connector open %r -> %r -> %r",
                chan.origin_addr,
                chan.getpeername(),
                (host, port),
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
        # we use multiple methods as some methods for checking if the connection is active return false positives
        self.logger.debug("Going to check if connector is up")
        try:
            self.transport.send_ignore()
        except Exception as e:
            self.logger.exception("Connector down! %s", e)
            self.failed = True
            return

        if not self.transport.is_active():
            self.logger.exception("Connector down! Transport is not active")
            self.failed = True
            return
        try:
            chn = self.transport.open_session(timeout=30)
            chn.close()
        except Exception as e:
            self.logger.exception(
                "Connector down! Failed to start a check session %s with timeout 30 seconds",
                e,
            )
            self.failed = True
            return
        self.timer = threading.Timer(self.keep_alive_time, self.validate_tunnel_up)
        self.timer.start()

    def reverse_forward_tunnel(self):
        try:
            # get a connection to the client
            self.transport = self.client.get_transport()

            # ask client to forward the data that it receives in the `remote_port_to_forward` through this SSH session
            self.transport.request_port_forward("", self.port_to_forward)

            self.timer = threading.Timer(30, self.validate_tunnel_up)
            self.timer.start()
            while True:
                chan = self.transport.accept(10)
                if self.failed:
                    return
                if chan is None:
                    continue
                thr = threading.Thread(
                    target=self.handler,
                    args=(chan, self.recipient_host, self.recipient_port),
                    daemon=True,
                )
                thr.start()
        except Exception as e:
            self.logger.exception("Failed to forward")

    def stop(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None

        if self.transport:
            with contextlib.suppress(Exception):
                self.transport.cancel_port_forward("", self.port_to_forward)
                self.transport.close()
                self.transport = None

    def __del__(self):
        self.stop()
