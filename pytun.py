import argparse
import configparser
import signal
import sys
import time
from multiprocessing import freeze_support
from os import listdir
from os.path import isabs, dirname, realpath
from os.path import isfile, join

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

    if len(processes) == 0:
        logger.exception("No config files found")
        sys.exit(1)

    def exit_gracefully(*args, **kwargs):
        for each in processes.values():
            each.terminate()
        for each in processes.values():
            each.join()
        sys.exit(0)

    signal.signal(signal.SIGINT, exit_gracefully)
    signal.signal(signal.SIGTERM, exit_gracefully)

    while True:
        items = list(processes.items())
        to_restart = []
        for key, proc in items:
            if (not proc.is_alive()) and proc.exitcode is not None:
                proc.terminate()
                del processes[key]
                to_restart.append(key)
                logger.info("Tunnel %s is down", files[key])
            else:
                logger.debug("Tunnel %s is up", files[key])
        for each in to_restart:
            logger.info("Going to restart tunnel from file %s", files[each])
            tunnel_process = TunnelProcess.from_config_file(files[each], logger)
            processes[each] = tunnel_process
            tunnel_process.start()
            logger.info("Tunnel from file %s has pid %s", files[each], tunnel_process.pid)
        time.sleep(30)


if __name__ == '__main__':
    main()
