import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True, scope="session")
def _keep_repo_ini_intact():
    """Undo pytun.py's legacy pytun.ini -> connector.ini migration.

    The CLI tests run pytun.py from the repo, so its application path is the
    repo itself and the migration would rename the tracked pytun.ini.
    """
    legacy = os.path.join(REPO, "pytun.ini")
    migrated = os.path.join(REPO, "connector.ini")
    had_legacy = os.path.isfile(legacy)
    had_migrated = os.path.isfile(migrated)
    yield
    if had_legacy and not os.path.isfile(legacy) and not had_migrated and os.path.isfile(migrated):
        os.rename(migrated, legacy)
