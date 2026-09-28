"""Unit tests for tunnel_infra/pathtype.py.

Ground truth: every validation branch inside PathType.__call__() (existence
checks, file/dir/symlink type checks, dash handling) is commented out in the
source -- the method unconditionally returns the input string unchanged. This
is exercised directly with Windows-style path strings (no real filesystem
dependency), so the assertions hold identically on Linux and Windows CI.
"""
import inspect

from tunnel_infra.pathtype import PathType


class TestPathTypeIsCurrentlyANoop:
    def test_call_returns_the_input_string_unchanged(self):
        path_type = PathType()
        raw = r"C:\Users\svc\some\config.ini"
        assert path_type(raw) == raw

    def test_nonexistent_path_is_not_rejected(self):
        # All validation logic is commented out, so exists=True (the
        # default) does not actually enforce existence.
        path_type = PathType(exists=True, type="file")
        assert path_type(r"C:\does\not\exist.ini") == r"C:\does\not\exist.ini"

    def test_dash_argument_is_returned_as_is(self):
        path_type = PathType(dash_ok=True)
        assert path_type("-") == "-"

    def test_windows_extended_length_prefix_is_left_untouched(self):
        # PathType does no stripping; callers in pytun.py handle the prefix.
        path_type = PathType()
        raw = "\\\\?\\C:\\Users\\svc\\logs"
        assert path_type(raw) == raw

    def test_constructor_accepts_documented_type_values(self):
        for type_value in ("file", "dir", "symlink", None):
            PathType(type=type_value)

    def test_constructor_accepts_a_callable_type(self):
        PathType(type=lambda s: True)

    def test_constructor_rejects_an_invalid_exists_value(self):
        import pytest
        with pytest.raises(AssertionError):
            PathType(exists="maybe")

    def test_constructor_rejects_an_invalid_type_value(self):
        import pytest
        with pytest.raises(AssertionError):
            PathType(type="not-a-valid-type")

    def test_body_of_call_is_entirely_commented_out(self):
        """Documents *why* the behaviour above holds: guards against someone
        uncommenting only part of the validation logic without updating these
        tests.
        """
        source = inspect.getsource(PathType.__call__)
        active_lines = [
            line for line in source.splitlines()[1:]  # skip "def __call__..."
            if line.strip() and not line.strip().startswith("#")
        ]
        assert active_lines == ["        return string"]
