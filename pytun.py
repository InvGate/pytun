import argparse
import configparser
import signal
import sys
import time
from multiprocessing import freeze_support
from multiprocessing.pool import ThreadPool
from os import listdir
from os.path import isabs, dirname, realpath
from os.path import isfile, join

from alerts.email_alert import EmailAlertSender
from configure_logger import configure_logger
from tunnel_infra.TunnelProcess import TunnelProcess
from tunnel_infra.pathtype import PathType

freeze_support()


def main():
    parser = argparse.ArgumentParser(description='Tunnel')
    parser.add_argument("--config_ini", dest="config_ini", help="Confiuration file to use", default="pytun.ini",
                        type=PathType(dash_ok=False))

    args = parser.parse_args()
    config = configparser.ConfigParser()
    if not isabs(args.config_ini):
        ini_path = join(dirname(realpath(__file__)), args.config_ini)
    else:
        ini_path = args.config_ini

    config.read(ini_path)
    params = config['pytun']
    tunnel_path = params.get("tunnel_dirs", "configs")
    if not isabs(args.config_ini):
        tunnel_path = join(dirname(realpath(__file__)), tunnel_path)

    files = [join(tunnel_path, f) for f in listdir(tunnel_path) if isfile(join(tunnel_path, f)) and f[-4:] == '.ini']
    processes = {}
    logger = configure_logger(params.get("log_level", "INFO"), params.get("log_to_console", False))
    smtp_sender = get_smtp_alert_sender(logger, params)
    if smtp_sender:
        senders = [smtp_sender]
        pool = ThreadPool(1)

    else:
        senders = []
        pool = None

    start_tunnels(files, logger, processes)

    if len(processes) == 0:
        logger.exception("No config files found")
        sys.exit(1)

    register_signal_handlers(processes, pool)

    while True:
        items = list(processes.items())
        to_restart = []
        check_tunnels(files, items, logger, processes, senders, to_restart, pool)
        restart_tunnels(files, logger, processes, to_restart)
        time.sleep(30)


def check_tunnels(files, items, logger, processes, senders, to_restart, pool):

    for key, proc in items:
        if (not proc.is_alive()) and proc.exitcode is not None:
            proc.terminate()
            del processes[key]
            to_restart.append(key)
            logger.info("Tunnel %s is down", files[key])
            for each in senders:
                pool.apply(each.send_alert,  args=(proc.tunnel_name,))
        else:
            logger.debug("Tunnel %s is up", files[key])


def restart_tunnels(files, logger, processes, to_restart):
    for each in to_restart:
        logger.info("Going to restart tunnel from file %s", files[each])
        tunnel_process = TunnelProcess.from_config_file(files[each], logger)
        processes[each] = tunnel_process
        tunnel_process.start()
        logger.info("Tunnel from file %s has pid %s", files[each], tunnel_process.pid)


def register_signal_handlers(processes, pool):
    def exit_gracefully(*args, **kwargs):
        for each in processes.values():
            each.terminate()
        for each in processes.values():
            each.join()
        if pool:
            pool.terminate()
        sys.exit(0)

    signal.signal(signal.SIGINT, exit_gracefully)
    signal.signal(signal.SIGTERM, exit_gracefully)


def start_tunnels(files, logger, processes):
    for each in range(len(files)):
        logger.info("Going to start tunnel from file %s", files[each])
        try:
            tunnel_process = TunnelProcess.from_config_file(files[each], logger)
        except Exception as e:
            logger.exception("Failed to create tunnel from file %s: %s", files[each], e)
            for each in processes.values():
                each.terminate()
            sys.exit(1)
        processes[each] = tunnel_process
        tunnel_process.start()
        logger.info("Tunnel from file %s has pid %s", files[each], tunnel_process.pid)


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
