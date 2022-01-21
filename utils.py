import os
import sys


def get_application_path():
    # hack to get the application path
    if getattr(sys, 'frozen', False):   # check if the application is running as a bundle
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))