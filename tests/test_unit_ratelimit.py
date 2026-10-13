"""Unit tests for lib/ratelimit.py: ratelimit_by_args.

The underlying `ratelimit` package (deckar01-ratelimit) tracks call history in
a real sqlite3 in-memory table keyed by wall-clock time, so it can't be driven
by a monkeypatched `time` module. To stay fast and deterministic without
sleeping, tests use a small `calls` budget and a long `period` (so the window
never actually expires during the test) and assert on immediate
RateLimitException behaviour instead of waiting for the window to reset.

The package keeps that table in a shared in-memory database named after the
function and its arguments, and it can outlive a test, so each test uses its
own function name to avoid inheriting another test's call count.
"""
import pytest
from ratelimit.exception import RateLimitException

from lib.ratelimit import ratelimit_by_args


class TestRatelimitByArgs:
    def test_allows_calls_up_to_the_limit(self):
        calls = []

        @ratelimit_by_args(calls=2, period=3600)
        def call_allows_calls_up_to_the_limit(x):
            calls.append(x)
            return x

        assert call_allows_calls_up_to_the_limit("a") == "a"
        assert call_allows_calls_up_to_the_limit("a") == "a"
        assert calls == ["a", "a"]

    def test_raises_once_limit_exceeded_for_same_args(self):
        @ratelimit_by_args(calls=2, period=3600)
        def call_raises_once_limit_exceeded_for_same_args(x):
            return x

        call_raises_once_limit_exceeded_for_same_args("a")
        call_raises_once_limit_exceeded_for_same_args("a")
        with pytest.raises(RateLimitException):
            call_raises_once_limit_exceeded_for_same_args("a")

    def test_different_positional_args_get_independent_limits(self):
        @ratelimit_by_args(calls=1, period=3600)
        def call_different_positional_args_get_independent_limits(x):
            return x

        call_different_positional_args_get_independent_limits("a")
        # "b" hasn't been called before, so it gets its own fresh bucket.
        call_different_positional_args_get_independent_limits("b")
        with pytest.raises(RateLimitException):
            call_different_positional_args_get_independent_limits("a")
        with pytest.raises(RateLimitException):
            call_different_positional_args_get_independent_limits("b")

    def test_different_keyword_args_get_independent_limits(self):
        @ratelimit_by_args(calls=1, period=3600)
        def call_different_keyword_args_get_independent_limits(**kwargs):
            return kwargs

        call_different_keyword_args_get_independent_limits(name="tunnel1")
        call_different_keyword_args_get_independent_limits(name="tunnel2")
        with pytest.raises(RateLimitException):
            call_different_keyword_args_get_independent_limits(name="tunnel1")

    def test_wraps_preserves_function_metadata(self):
        @ratelimit_by_args(calls=5, period=3600)
        def documented_call(x):
            """docstring"""
            return x

        assert documented_call.__name__ == "documented_call"
        assert documented_call.__doc__ == "docstring"
