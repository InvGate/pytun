"""Unit tests for lib/ratelimit.py: ratelimit_by_args.

The underlying `ratelimit` package (deckar01-ratelimit) tracks call history in
a real sqlite3 in-memory table keyed by wall-clock time, so it can't be driven
by a monkeypatched `time` module. To stay fast and deterministic without
sleeping, tests use a small `calls` budget and a long `period` (so the window
never actually expires during the test) and assert on immediate
RateLimitException behaviour instead of waiting for the window to reset.
"""
import pytest
from ratelimit.exception import RateLimitException

from lib.ratelimit import ratelimit_by_args


class TestRatelimitByArgs:
    def test_allows_calls_up_to_the_limit(self):
        calls = []

        @ratelimit_by_args(calls=2, period=3600)
        def do_call(x):
            calls.append(x)
            return x

        assert do_call("a") == "a"
        assert do_call("a") == "a"
        assert calls == ["a", "a"]

    def test_raises_once_limit_exceeded_for_same_args(self):
        @ratelimit_by_args(calls=2, period=3600)
        def do_call(x):
            return x

        do_call("a")
        do_call("a")
        with pytest.raises(RateLimitException):
            do_call("a")

    def test_different_positional_args_get_independent_limits(self):
        @ratelimit_by_args(calls=1, period=3600)
        def do_call(x):
            return x

        do_call("a")
        # "b" hasn't been called before, so it gets its own fresh bucket.
        do_call("b")
        with pytest.raises(RateLimitException):
            do_call("a")
        with pytest.raises(RateLimitException):
            do_call("b")

    def test_different_keyword_args_get_independent_limits(self):
        @ratelimit_by_args(calls=1, period=3600)
        def do_call(**kwargs):
            return kwargs

        do_call(name="tunnel1")
        do_call(name="tunnel2")
        with pytest.raises(RateLimitException):
            do_call(name="tunnel1")

    def test_wraps_preserves_function_metadata(self):
        @ratelimit_by_args(calls=5, period=3600)
        def documented_call(x):
            """docstring"""
            return x

        assert documented_call.__name__ == "documented_call"
        assert documented_call.__doc__ == "docstring"
