import os
import shutil
import sys
import time
from logging import Logger

import psutil


def is_app_running_as_pyinstaller_bundle():
    return getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')


def get_application_path():
    # hack to get the application path
    if is_app_running_as_pyinstaller_bundle():
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


def clean_runtime_tempdir(logger: Logger, time_threshold: int = 15*60) -> None:
    """
    Windows hack to clean the pyinstaller runtime tempdir as the _MEIXXX folders are not being deleted due to a bug
    https://github.com/pyinstaller/pyinstaller/issues/2379

    :param logger: App logger
    :param time_threshold: Delete _MEI folders that were created after this threshold (in seconds). Default is 15min
    """

    # This hack deletes all _MEI folders created by the connector except the one that is being used by the current
    # running connector.
    # As multiple connectors could be running at the same time (1 running as a service and there could also be many
    # running tests) it is not possible to know which _MEI folders are being used. To avoid deleting folders that
    # are in use only folders created after time_threshold are deleted.
    # Also, this hack relies on how Windows handles permissions. When the connector is run as a service its _MEI folder
    # is created with SYSTEM permissions but when the connector's tests are run the _MEI folders are created with the
    # current admin user permissions, so when the connector is run as a service it won't be able to delete the tests
    # _MEI folders and when the connector's tests are run it won't be able to delete the service _MEI folder.
    # Errors when deleting a folder are ignored.

    # if app is not running as a pyinstaller bundle or OS is not windows
    if not is_app_running_as_pyinstaller_bundle() or os.name != "nt":
        return

    # current _MEIXXX folder
    current_mei_folder_path = os.path.abspath(get_bundle_path())

    # directory that contains the _MEI folders
    temps_dir = os.path.abspath(os.path.join(current_mei_folder_path, '..'))

    now = time.time()
    for dir_entry in os.scandir(temps_dir):
        if dir_entry.is_dir():
            mei_folder_path = dir_entry.path
            if (
                    mei_folder_path == current_mei_folder_path or
                    (now-os.path.getctime(mei_folder_path)) <= time_threshold
            ):
                continue

            try:
                shutil.rmtree(mei_folder_path)
            except Exception:
                logger.warning(f"Couldn't delete {mei_folder_path}", exc_info=True)





