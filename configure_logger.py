import os
import logging
from logging.handlers import TimedRotatingFileHandler


class LogManager:

    path = "./"

    @staticmethod
    def configure_logger(filename, level=None, log_to_console=False, name="pytun"):
        level = level or logging.INFO
        logger = logging.getLogger(name)
        loggers = [logger]
        if name != "pytun":
            paramiko_log = logging.getLogger("paramiko")
            loggers.append(paramiko_log)
        log_handler = TimedRotatingFileHandler(filename=os.path.join(LogManager.path, filename), when="midnight")
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
