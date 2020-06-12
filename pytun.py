import argparse
import configparser
import signal
import sys
import time
from concurrent.futures.process import ProcessPoolExecutor
from concurrent.futures.thread import ThreadPoolExecutor
from multiprocessing import freeze_support
from multiprocessing.pool import ThreadPool
from multiprocessing import Queue
from os import listdir
from os.path import isabs, dirname, realpath
from os.path import isfile, join
from socket import socket

from paramiko import BadHostKeyException, PasswordRequiredException, AuthenticationException, SSHException

from alerts.email_alert import EmailAlertSender
from alerts.pooled_alerter import DifferentThreadAlert
from configure_logger import configure_logger
from tunnel_infra.TunnelProcess import TunnelProcess
from tunnel_infra.pathtype import PathType

freeze_support()


def main():
    parser = argparse.ArgumentParser(description='Tunnel')
    parser.add_argument("--config_ini", dest="config_ini", help="Confiuration file to use", default="pytun.ini",
                        type=PathType(dash_ok=False))
    parser.add_argument("--test_smtp", dest="test_mail", help="Send a test email to validate the smtp config and exits",
                        action='store_true', default=False)
    parser.add_argument("--test_connections", dest="test_connections",
                        help="Test to connect to the exposed services for each tunnel", action='store_true',
                        default=False)
    parser.add_argument("--test_tunnels", dest="test_tunnels",
                        help="Test to establish each one of the tunnels", action='store_true',
                        default=False)

    args = parser.parse_args()
    config = configparser.ConfigParser()
    if not isabs(args.config_ini):
        ini_path = join(dirname(realpath(__file__)), args.config_ini)
    else:
        ini_path = args.config_ini

    config.read(ini_path)
    params = config['pytun']
    logger = configure_logger(params.get("log_level", "INFO"), params.get("log_to_console", False) or args.test_mail)
    smtp_sender = get_smtp_alert_sender(logger, params)

    if args.test_mail:
        test_mail_and_exit(logger, smtp_sender)

    tunnel_path = params.get("tunnel_dirs", "configs")
    if not isabs(args.config_ini):
        tunnel_path = join(dirname(realpath(__file__)), tunnel_path)

    files = [join(tunnel_path, f) for f in listdir(tunnel_path) if isfile(join(tunnel_path, f)) and f[-4:] == '.ini']
    processes = {}

    if args.test_connections:
        test_connections_and_exit(files, logger, processes)

    if args.test_tunnels:
        test_tunnels_and_exit(files, logger, processes)


    if smtp_sender:
        senders = [smtp_sender]
    else:
        senders = []

    pool = ThreadPoolExecutor(1)
    main_sender = DifferentThreadAlert(senders, pool)

    start_tunnels(files, logger, processes, senders)

    if len(processes) == 0:
        logger.exception("No config files found")
        sys.exit(1)


    register_signal_handlers(processes, pool)

    while True:
        items = list(processes.items())
        to_restart = []
        check_tunnels(files, items, logger, processes, to_restart, main_sender)
        restart_tunnels(files, logger, processes, to_restart, senders)
        time.sleep(30)


def test_tunnels_and_exit(files, logger, processes):
    failed = False
    for each in range(len(files)):
        try:
            config_file = files[each]
            logger.info("Going to start tunnel from file %s", config_file)
            try:
                tunnel_process = TunnelProcess.from_config_file(config_file, [])

            except Exception as e:
                logger.exception("Failed to create tunnel from file %s: %s", config_file, e)
                failed = True
                continue
            tunnel_process.logger = logger
            client = tunnel_process.ssh_connect(exit_on_failure=False)
            transport = client.get_transport()
            try:
                transport.request_port_forward("", tunnel_process.remote_port_to_forward)
                transport.close()
            except SSHException as e:
                message = """Failed to connect with service %s:%s. Port binding rejected
                                        Please check server_host, server_port and port in your config
                                        Error %r"""
                logger.exception(message % (tunnel_process.remote_host, tunnel_process.remote_port, e))
                failed = True
                continue
            client.close()
        except BadHostKeyException as e:
            message = """Failed to connect with service %s:%s. The host key given by the SSH server did not match what 
            we were expecting.
            The hostname was %s, 
            the expected key was %s, 
            the key that we got was %s
            Please check server_key in your config
            Error %r"""
            logger.exception(message % (tunnel_process.remote_host, tunnel_process.remote_port, e.hostname,
                                        e.expected_key.get_base64(), e.key.get_base64(), e))
            failed = True
        except AuthenticationException as e:
            message = """Failed to connect with service %s:%s. The private key file was rejected. 
                                    Please check keyfile in your config
                                    Error %r"""
            logger.exception(message % (tunnel_process.remote_host, tunnel_process.remote_port, e))
            failed = True
        except PasswordRequiredException as e:
            message = """Failed to connect with service %s:%s. The private key file is encrypted. 
                        Please check keyfile and username in your config
                        Error %r"""
            logger.exception(message % (tunnel_process.remote_host, tunnel_process.remote_port, e))
            failed = True

        except Exception as e:
            failed = True
            logger.exception("Failed to establish tunnel %s with error %r" %
                             (tunnel_process.tunnel_name, e))
    if failed:
        logger.error("Some tunnels failed!")
        sys.exit(4)
    else:
        logger.info("All the tunnels worked!")
        sys.exit(0)


def test_connections_and_exit(files, logger, processes):
    create_tunnels_from_config([], files, logger, processes)
    failed = False
    for key, tunnel_proc in processes.items():
        with socket() as sock:
            try:
                sock.settimeout(2)
                sock.connect((tunnel_proc.remote_host, tunnel_proc.remote_port))
            except Exception as e:
                logger.exception("Failed to connect with service %s:%s. Please check remote_host and remote_port in your cofig. Error %r" %
                                 (tunnel_proc.remote_host, tunnel_proc.remote_port, e))
                failed = True
    if failed:
        logger.error("Some connections failed!")
        sys.exit(3)
    else:
        logger.info("All the connections worked!")
        sys.exit(0)


def test_mail_and_exit(logger, smtp_sender):
    if smtp_sender is None:
        logger.error("No SMTP config found!")
        sys.exit(2)
    try:
        smtp_sender.send_alert("Testing email", message="Testing email", exception_on_failure=True)
    except Exception as e:
        logger.exception("Failed to send email %r", e)
        sys.exit(1)
    logger.info("Mail test success!")
    sys.exit(0)


def check_tunnels(files, items, logger, processes, to_restart, pooled_sender):

    for key, proc in items:
        if (not proc.is_alive()) and proc.exitcode is not None:
            proc.terminate()
            del processes[key]
            to_restart.append(key)
            logger.info("Tunnel %s is down", files[key])
            pooled_sender.send_alert(proc.tunnel_name)
        else:
            logger.debug("Tunnel %s is up", files[key])


def restart_tunnels(files, logger, processes, to_restart, alert_senders):
    for each in to_restart:
        logger.info("Going to restart tunnel from file %s", files[each])
        tunnel_process = TunnelProcess.from_config_file(files[each], alert_senders)
        processes[each] = tunnel_process
        tunnel_process.start()
        logger.info("Tunnel %s has pid %s", tunnel_process.tunnel_name, tunnel_process.pid)


def register_signal_handlers(processes,pool):
    def exit_gracefully(*args, **kwargs):
        if pool:
            pool.shutdown()
        for each in processes.values():
            each.terminate()
        for each in processes.values():
            each.join()

        sys.exit(0)

    signal.signal(signal.SIGINT, exit_gracefully)
    signal.signal(signal.SIGTERM, exit_gracefully)


def start_tunnels(files, logger, processes, alert_senders):
    create_tunnels_from_config(alert_senders, files, logger, processes)
    for key, tunnel_process in processes.items():
        tunnel_process.start()
        logger.info("Tunnel %s has pid %s", tunnel_process.tunnel_name, tunnel_process.pid)


def create_tunnels_from_config(alert_senders, files, logger, processes):
    for each in range(len(files)):
        config_file = files[each]
        logger.info("Going to start tunnel from file %s", config_file)
        try:
            tunnel_process = TunnelProcess.from_config_file(config_file, alert_senders)
        except Exception as e:
            logger.exception("Failed to create tunnel from file %s: %s", config_file, e)
            for pr in processes.values():
                pr.terminate()
            sys.exit(1)
        processes[each] = tunnel_process


def get_smtp_alert_sender(logger, params):
    if params.get("smtp_hostname"):
        try:
            smtp_sender = EmailAlertSender(params['smtp_hostname'], params['smtp_login'], params['smtp_password'],
                                           params['smtp_to'], logger,
                                           port=params.get('smtp_port', 25), from_address=params.get('smtp_from'),
                                           security=params.get("smtp_security"))
        except KeyError as e:
            logger.exception("Missing smtp param %s" % e)
            sys.exit(-1)
    else:
        smtp_sender = None
    return smtp_sender


if __name__ == '__main__':
    main()
