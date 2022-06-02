import os
import sys

import psutil


def get_application_path():
    # hack to get the application path
    if getattr(sys, 'frozen', False):   # check if the application is running as a bundle
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))


def get_bundle_path():
    return getattr(sys, '_MEIPASS', os.path.abspath("."))


def get_net_if_mac_addresses():
    """
    :return: All the network interfaces MAC addresses
    """
    for interface, snics in psutil.net_if_addrs().items():
        for snic in snics:
            if snic.family == psutil.AF_LINK:
                yield interface, snic.address.lower()
