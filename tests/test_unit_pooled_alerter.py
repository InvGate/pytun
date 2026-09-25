"""Unit tests for alerts/pooled_alerter.py and alerts/alert_sender.py.

Ground truth on the "race condition" claimed at alerts/CLAUDE.md line 26 and
pytun's CLAUDE.md item 5 ("future.exception() called before future
completes"):

    future = self.pool.submit(each.send_alert, ...)
    error = future.exception()

`concurrent.futures.Future.exception(timeout=None)` (cpython
Lib/concurrent/futures/_base.py) blocks on `self._condition` until the future
reaches a finished state (FINISHED/CANCELLED*) *unless* a `timeout` is passed
-- in which case it either returns once finished or raises TimeoutError after
that many seconds. `pooled_alerter.py` calls `future.exception()` with no
`timeout` argument at all, so this call always blocks until `send_alert` on
the pooled alerter has actually returned (or raised), and `error` is only
ever read once that has happened. There is no race: `send_alert()` becomes
fully synchronous the moment it calls `future.exception()`, regardless of
pool size or how slow the underlying alerter is.

The single real consequence (not a race) is that PooledAlerter provides no
concurrency benefit at all when its pool has more than one worker, since the
loop over `self.alerters` blocks on each future before submitting the next
alerter's job -- but that is a design/performance property, not the race
described in CLAUDE.md. The test below proves the blocking-until-complete
behavior directly with a deliberately slow alerter and asserts the elapsed
time is compatible with having waited for it, which would be impossible if
`future.exception()` returned prematurely.
"""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from ratelimit.exception import RateLimitException

from alerts.alert_sender import AlertSender
from alerts.pooled_alerter import DifferentThreadAlert, PooledAlerter


class _SlowAlerter(AlertSender):
    """Sleeps before returning, to make a premature exception() read visible."""

    def __init__(self, delay, raise_error=None):
        self.delay = delay
        self.raise_error = raise_error
        self.calls = []

    def send_alert(self, tunnel_name, message=None, exception_on_failure=False):
        time.sleep(self.delay)
        self.calls.append((tunnel_name, message, exception_on_failure))
        if self.raise_error is not None:
            raise self.raise_error


class _RecordingLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, *args, **kwargs):
        self.warnings.append((args, kwargs))


class TestAlertSenderBase:
    def test_send_alert_is_not_implemented(self):
        with pytest.raises(NotImplementedError):
            AlertSender().send_alert("tunnel-a")


class TestPooledAlerterExceptionIsNotPremature:
    def test_future_exception_blocks_until_the_slow_alerter_actually_finished(self):
        """If future.exception() returned before completion, `slow.calls`
        would be empty right after send_alert() returns. It is not: the call
        that reads `error` only returns after `send_alert` really ran.
        """
        slow = _SlowAlerter(delay=0.3)
        pool = ThreadPoolExecutor(1)
        alerter = DifferentThreadAlert([slow], _RecordingLogger(), process_pool=pool)

        start = time.monotonic()
        alerter.send_alert("tunnel-a")
        elapsed = time.monotonic() - start

        assert slow.calls == [("tunnel-a", None, False)]
        assert elapsed >= 0.3

    def test_rate_limit_exception_from_the_pooled_call_is_logged_as_a_warning(self):
        alerter_stub = _SlowAlerter(delay=0, raise_error=RateLimitException("limited", 60))
        logger = _RecordingLogger()
        pool = ThreadPoolExecutor(1)
        alerter = DifferentThreadAlert([alerter_stub], logger, process_pool=pool)

        alerter.send_alert("tunnel-a")  # must not raise: rate limit is only logged

        assert len(logger.warnings) == 1
        assert "rate limit exceeded" in logger.warnings[0][0][0]

    def test_other_exceptions_are_swallowed_by_default(self):
        alerter_stub = _SlowAlerter(delay=0, raise_error=ValueError("boom"))
        logger = _RecordingLogger()
        pool = ThreadPoolExecutor(1)
        alerter = DifferentThreadAlert([alerter_stub], logger, process_pool=pool)

        alerter.send_alert("tunnel-a")  # must not raise

        assert logger.warnings == []

    def test_other_exceptions_reraise_when_exception_on_failure(self):
        alerter_stub = _SlowAlerter(delay=0, raise_error=ValueError("boom"))
        pool = ThreadPoolExecutor(1)
        alerter = DifferentThreadAlert([alerter_stub], _RecordingLogger(), process_pool=pool)

        with pytest.raises(ValueError, match="boom"):
            alerter.send_alert("tunnel-a", exception_on_failure=True)

    def test_dispatches_to_every_registered_alerter(self):
        first = _SlowAlerter(delay=0)
        second = _SlowAlerter(delay=0)
        pool = ThreadPoolExecutor(1)
        alerter = DifferentThreadAlert([first, second], _RecordingLogger(), process_pool=pool)

        alerter.send_alert("tunnel-a", message="down")

        assert first.calls == [("tunnel-a", "down", False)]
        assert second.calls == [("tunnel-a", "down", False)]

    def test_add_alerter_appends_to_the_alerter_list(self):
        first = _SlowAlerter(delay=0)
        pool = ThreadPoolExecutor(1)
        alerter = DifferentThreadAlert([first], _RecordingLogger(), process_pool=pool)
        second = _SlowAlerter(delay=0)

        alerter.add_alerter(second)
        alerter.send_alert("tunnel-a")

        assert second.calls == [("tunnel-a", None, False)]

    def test_default_process_pool_is_used_when_none_is_given(self):
        # get_default_pool() itself is abstract on PooledAlerter and unused by
        # __init__ (a ProcessPoolExecutor(1) is always the fallback), but
        # DifferentThreadAlert overrides it; confirm construction still works
        # without an explicit process_pool.
        alerter = PooledAlerter([], _RecordingLogger())
        assert alerter.pool is not None
        alerter.pool.shutdown(wait=True)
