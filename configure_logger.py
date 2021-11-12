import os
import logging
from logging.handlers import TimedRotatingFileHandler
from os.path import join, dirname, realpath


class LogManager:
    _fallback_path = "./logs"
    path = "./logs"

    @staticmethod
    def configure_logger(filename, level=None, log_to_console=False, name="pytun", path=None):
        path = path if path is not None else LogManager.path
        try:
            path = join(dirname(realpath(__file__)), path)
            os.makedirs(path)
        except FileExistsError:
            pass
        except OSError:
            path = join(dirname(realpath(__file__)), LogManager._fallback_path)
            os.makedirs(path)

        level = level or logging.INFO
        logger = logging.getLogger(name)
        loggers = [logger]
        if name != "pytun":
            paramiko_log = logging.getLogger("paramiko")
            loggers.append(paramiko_log)
        log_handler = TimedRotatingFileHandler(filename=os.path.join(path, filename), when="midnight", backupCount=30)
        log_formatter = logging.Formatter('%(asctime)s %(process)d %(name)-12s %(levelname)-8s %(message)s')
        log_handler.setFormatter(log_formatter)
        log_handler.setLevel(level)
        console_handler = None
        if log_to_console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(level)
            console_handler.setFormatter(log_formatter)
        for each in loggers:
            each.addHandler(log_handler)
            each.setLevel(level)
            if console_handler:
                each.addHandler(console_handler)
        return logger
