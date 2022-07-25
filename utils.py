import os
import shutil
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
                yield interface, snic.address.lower().replace('-', ':')


def clean_runtime_tempdir():
    # hack to clean the pyinstaller runtime tempdir as the _MEIXXX folders are not being deleted due to a bug
    # https://github.com/pyinstaller/pyinstaller/issues/2379

    # this folder is specified when pyinstaller is run using the --runtime-tmpdir argument. Here pyinstaller creates
    # _MEIXXX folders with everything needed to run the application
    temps_dir = os.path.abspath(os.path.join(get_application_path(), 'tmp'))
    print(f"{temps_dir=}")

    # current _MEIXXX folder
    current_mei_folder_path = os.path.abspath(get_bundle_path())
    print(f"{current_mei_folder_path=}")

    for mei_folder_path in [f.name for f in os.scandir(temps_dir) if f.is_dir()]:
        print(f"{mei_folder_path=}")

        if mei_folder_path == current_mei_folder_path:
            continue
        try:
            shutil.rmtree(mei_folder_path)
        except Exception as err:
            print(f"Couldn't delete {mei_folder_path}", err)





