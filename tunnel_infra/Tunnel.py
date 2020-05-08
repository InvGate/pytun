import select
import socket
import threading

from paramiko import SSHException


class Tunnel(object):

    def __init__(self, server_port, remote_host, remote_port, transport, logger, keep_alive_time=30):
        self.timer = None
        self.server_port = server_port
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.transport = transport
        self.logger = logger
        self.keep_alive_time = keep_alive_time
        self.exit = False
        self.event = threading.Event()

    def handler(self, chan, remote, local):
        host, port = self.remote_host, self.remote_port
        with socket.socket() as sock:
            try:
                sock.connect((host, port))
            except Exception as e:
                self.logger.exception("Forwarding request to %s:%d failed: %r" % (host, port, e))
                return

            self.logger.debug(
                "Connected!  Tunnel open %r -> %r -> %r"
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
                self.logger.debug("Tunnel closed from %r", chan.origin_addr)
            except ConnectionResetError as e:
                pass
            except Exception as e:
                self.logger.exception(e)

    def validate_tunnel_up(self):
        self.logger.debug("Going to check if tunnel is up")
        try:
            self.transport.send_ignore()
        except Exception as e:
            self.logger.exception("Tunnel down! %s", e)
            self.event.set()
            return
        if not self.transport.is_active():
            self.logger.exception("Tunnel down! Transport is not active")
            self.event.set()
            return
        try:
            chn = self.transport.open_session(timeout=30)
            chn.close()
        except SSHException as e:
            self.logger.exception("Tunnel down! Failed to start a check session %s with timeout 30 seconds", e)
            self.event.set()
            return
        self.timer = threading.Timer(self.keep_alive_time, self.validate_tunnel_up)
        self.timer.start()

    def reverse_forward_tunnel(self):
        self.transport.request_port_forward("", self.server_port, self.handler)
        self.timer = threading.Timer(self.keep_alive_time, self.validate_tunnel_up)
        self.timer.start()
        self.event.wait()

    def stop(self):
        self.exit = 1
        if self.timer:
            self.timer.cancel()
        self.event.set()
