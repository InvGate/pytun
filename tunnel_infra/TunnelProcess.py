import configparser
import multiprocessing
import os
import signal
import sys

import paramiko

from .Tunnel import Tunnel
from configure_logger import LogManager
from os.path import isabs, dirname, realpath, join

DEFAULT_KEEP_ALIVE_TIME = 30

SSH_PORT = 22
DEFAULT_PORT = 4000


class TunnelProcess(multiprocessing.Process):

    def __init__(self, tunnel_name, server_host, server_port, server_key, user_to_login, key_file, remote_port_to_forward,
                 remote_host, remote_port, keep_alive_time, log_level, log_to_console, alert_senders=None,
                 log_filename=None, log_path=None):
        if log_filename is None:
            log_filename = os.path.splitext(os.path.basename(tunnel_name))[0] + ".log"
        self.log_filename = log_filename
        self.log_path = log_path
        self.tunnel_name = tunnel_name
        self.server_host = server_host
        self.server_port = server_port
        self.server_key = server_key
        self.user_to_login = user_to_login
        self.key_file = key_file
        self.remote_port_to_forward = remote_port_to_forward
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.logger = None
        self.keep_alive_time = keep_alive_time
        self.tunnel = None
        self.log_level = log_level
        self.log_to_console = log_to_console
        self.alert_senders = alert_senders
        super().__init__()

    def exit_gracefully(self, *args):
        self.logger.info("Exit gracefully called for %s", self.pid)
        if self.tunnel:
            self.tunnel.stop()
            self.tunnel = None
        sys.exit(0)

    def run(self):
        LogManager.path = self.log_path
        self.logger = LogManager.configure_logger(self.log_filename, self.log_level, self.log_to_console,
                                                  name="pytun-tunnel")
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)
        client = self.ssh_connect()

        self.logger.info(
            "Now forwarding remote port %d to %s:%d ..."
            % (self.remote_port_to_forward, self.remote_host, self.remote_port)
        )
        try:
            tunnel = Tunnel(self.tunnel_name, self.remote_port_to_forward, self.remote_host, self.remote_port, client.get_transport(),
                            self.logger, keep_alive_time=self.keep_alive_time, alert_senders=self.alert_senders)
            self.tunnel = tunnel
            tunnel.reverse_forward_tunnel()
            sys.exit(0)
        except KeyboardInterrupt:
            if tunnel.timer:
                tunnel.timer.cancel()
            self.logger.info("Port forwarding stopped.")
            sys.exit(0)
        except Exception as e:
            if tunnel.timer:
                tunnel.timer.cancel()
            self.logger.exception("Port forwarding stopped with error %s", e)
            sys.exit(1)

    def ssh_connect(self, exit_on_failure=True):
        client = paramiko.SSHClient()
        if self.server_key:
            client.load_system_host_keys(self.server_key)

        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self.logger.info("Connecting to ssh host %s:%d ..." % (self.server_host, self.server_port))
        try:
            client.connect(
                self.server_host,
                self.server_port,
                username=self.user_to_login,
                key_filename=self.key_file,
                look_for_keys=False,
                allow_agent=False,
                timeout=10
            )
        except Exception as e:
            self.logger.info("Failed to connect to %s:%d: %r" % (self.server_host, self.server_port, e))
            if exit_on_failure:
                sys.exit(1)
            else:
                raise e
        return client

    @staticmethod
    def from_config_file(ini_file, alert_senders=None):
        config = configparser.ConfigParser()
        config.read(ini_file)
        directory = dirname(realpath(ini_file))
        if 'connector' in config:
            defaults = config['connector']
        else:
            defaults = config['tunnel']
        log_level = defaults.get('log_level', 'DEBUG')
        log_to_console = defaults.get('log_to_console', False)
        log_path = defaults.get('log_path', './logs')
        server_host = defaults['server_host']
        server_port = int(defaults.get('server_port', SSH_PORT))
        remote_host = defaults['remote_host']
        remote_port = int(defaults.get('remote_port', SSH_PORT))
        remote_port_to_forward = int(defaults.get('port', DEFAULT_PORT))
        tunnel_name = defaults.get('connector_name', realpath(ini_file))
        log_filename = os.path.basename(ini_file)
        log_filename = os.path.splitext(log_filename)[0] + ".log"
        key_file = defaults.get('keyfile')
        if key_file is None:
            raise Exception("Missing keyfile argument")
        if not isabs(key_file):
            key_file = join(directory, key_file)
        user_to_login = defaults["username"]
        server_key = defaults.get("server_key", None)
        if server_key is not None and not isabs(server_key):
            server_key = join(directory, server_key)
        keep_alive_time = int(defaults.get("keep_alive_time", DEFAULT_KEEP_ALIVE_TIME))
        tunnel_process = TunnelProcess(tunnel_name, server_host, server_port, server_key, user_to_login, key_file,
                                       remote_port_to_forward, remote_host, remote_port, keep_alive_time, log_level,
                                       log_to_console, alert_senders=alert_senders, log_filename=log_filename,
                                       log_path=log_path)
        return tunnel_process
