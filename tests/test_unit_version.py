"""Unit test for version.py's get_version()."""
import version


def test_get_version_returns_the_module_level_version_string():
    assert version.get_version() == version.__version__
    assert isinstance(version.get_version(), str)
