import configparser
import multiprocessing
import os
import signal
import sys
from functools import cached_property
from logging import Logger

import paramiko

from alerts.alert_sender import AlertSender
from configure_logger import LogManager
from os.path import isabs, dirname, realpath, join

from tunnel_infra.Tunnel import Tunnel

DEFAULT_KEEP_ALIVE_TIME = 30

SSH_PORT = 22
DEFAULT_PORT = 4000


class TunnelProcess(multiprocessing.Process):
    default_log_path = './logs'

    def __init__(
            self,
            tunnel_name: str,
            *,
            server_host: str,
            server_port: int,
            server_port_to_forward: int,
            server_key: str,
            user_to_login: str,
            key_file: str,
            recipient_host: str,
            recipient_port: int,
            keep_alive_time: int,
            log_level: str | int,
            log_to_console: bool,
            log_filename: str = None,
            log_path: str = None,
            alert_senders: list[AlertSender] = None
    ) -> None:
        """

        :param tunnel_name: Name of the tunnel
        :param server_host: Host of server that will forward the data
        :param server_port: Port of the server that will forward the data
        :param server_port_to_forward: Port of the server to forward data from. Whatever data the server receives in
            this port will be forwarded to the recipient_host:recipient_port
        :param server_key: Path to the public key of the external server to connect to,
            also called ssh server fingerprint or known host key
        :param user_to_login: Username used to connect to the server
        :param key_file: Path to the private key of the client, also called "host key"
        :param recipient_host: Host that's going to receive the data forwarded by the server
        :param recipient_port: Port that's going to receive the data forwarded by the server
        :param keep_alive_time: Time in seconds to check that the tunnel is working
        """
        self.tunnel_name = tunnel_name
        self.server_host = server_host
        self.server_port = server_port
        self.server_port_to_forward = server_port_to_forward
        self.server_key = server_key
        self.user_to_login = user_to_login
        self.key_file = key_file
        self.recipient_host = recipient_host
        self.recipient_port = recipient_port
        self.keep_alive_time = keep_alive_time
        self.alert_senders = alert_senders

        self.log_level = log_level
        self.log_to_console = log_to_console
        self.log_path = log_path or TunnelProcess.default_log_path
        self.log_filename = (
            log_filename
            if log_filename is not None
            else f"{os.path.splitext(os.path.basename(tunnel_name))[0]}.log"
        )

        self.tunnel: Tunnel | None = None

        super().__init__()

    @cached_property
    def logger(self) -> Logger:
        return LogManager.configure_logger(
            self.log_filename,
            self.log_level,
            self.log_to_console,
            name="connector",
            path=self.log_path
        )

    def exit_gracefully(self, *args):
        self.logger.info("Exit gracefully called for %s", self.pid)
        if self.tunnel:
            self.tunnel.stop()
            self.tunnel = None
        sys.exit(0)

    def run(self):
        self.logger.info("Starting TunnelProcess with the process id: %s", self.pid)
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)
        client = self.ssh_connect()
        self.logger.info(
            "Now forwarding remote port %d to %s:%d ..."
            % (self.server_port_to_forward, self.recipient_host, self.recipient_port)
        )
        try:
            tunnel = Tunnel(
                self.tunnel_name,
                port_to_forward=self.server_port_to_forward,
                recipient_host=self.recipient_host,
                recipient_port=self.recipient_port,
                client=client,
                logger=self.logger,
                keep_alive_time=self.keep_alive_time,
                alert_senders=self.alert_senders
            )
            self.tunnel = tunnel
            tunnel.reverse_forward_tunnel()
            sys.exit(0)
        except KeyboardInterrupt:
            self.logger.info("Port forwarding stopped.")
            sys.exit(0)
        except Exception as e:
            self.logger.exception("Port forwarding stopped with error %s", e)
            sys.exit(1)
        finally:
            if self.tunnel:
                self.tunnel.stop()
            if client:
                client.close()

    def ssh_connect(self, exit_on_failure=True):
        client = None
        try:
            client = paramiko.SSHClient()
            if self.server_key:
                # load the public key of the server, also called ssh server fingerprint or known host key
                client.load_system_host_keys(self.server_key)

            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            self.logger.info("Connecting to ssh host %s:%d ..." % (self.server_host, self.server_port))
            client.connect(
                self.server_host,
                self.server_port,
                username=self.user_to_login,
                key_filename=self.key_file,  # private key of the client, what is called "host key" in SSH
                look_for_keys=False,
                allow_agent=False,
                timeout=10
            )
        except Exception as e:
            self.logger.info("Failed to connect to %s:%d: %r" % (self.server_host, self.server_port, e))

            if client:
                client.close()

            if exit_on_failure:
                sys.exit(1)
            else:
                raise e
        return client

    @staticmethod
    def from_config_file(ini_file, alert_senders=None):
        config = configparser.ConfigParser()
        config.read(ini_file)
        defaults = config['connector'] if 'connector' in config else config['tunnel']

        directory = dirname(realpath(ini_file))
        key_file = defaults.get('keyfile')
        if key_file is None:
            raise Exception("Missing keyfile argument")
        if not isabs(key_file):
            key_file = join(directory, key_file)

        server_key = defaults.get("server_key", None)
        if server_key is not None and not isabs(server_key):
            server_key = join(directory, server_key)

        return TunnelProcess(
            tunnel_name=defaults.get('connector_name' if 'connector_name' in defaults else 'tunnel_name', realpath(ini_file)),
            server_host=defaults['server_host'],
            server_port=int(defaults.get('server_port', SSH_PORT)),
            server_port_to_forward=int(defaults.get('port', DEFAULT_PORT)),
            server_key=server_key,
            user_to_login=defaults["username"],
            key_file=key_file,
            recipient_host=defaults['remote_host'],
            recipient_port=int(defaults.get('remote_port', SSH_PORT)),
            keep_alive_time=int(defaults.get("keep_alive_time", DEFAULT_KEEP_ALIVE_TIME)),
            alert_senders=alert_senders,
            log_filename=f"{os.path.splitext(os.path.basename(ini_file))[0]}.log",
            log_level=defaults.get('log_level', 'DEBUG'),
            log_to_console=bool(defaults.get('log_to_console', False)),
            log_path=TunnelProcess.default_log_path
        )

