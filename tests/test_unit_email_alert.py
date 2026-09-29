"""Unit tests for alerts/email_alert.py: EmailAlertSender.

Uses a minimal stdlib-only threaded SMTP server on loopback rather than
aiosmtpd, since no new test-only dependency is needed to speak just enough of
RFC 5321 (EHLO/MAIL FROM/RCPT TO/DATA) for smtplib.SMTP to complete a plain
(security=none) send. The "none" security path exercises the real protocol
end to end; the tls/ssl branches only change which smtplib class is used and
whether starttls() is called, so those are covered by mocking smtplib
directly, per the task instructions.
"""
import gc
import smtplib
import socket
import threading

import pytest
from ratelimit.exception import RateLimitException

from alerts.email_alert import EmailAlertSender, SecurityValues


def _cell_for(func, freevar_name):
    if not func.__closure__:
        return None
    for name, cell in zip(func.__code__.co_freevars, func.__closure__):
        if name == freevar_name:
            return cell
    return None


@pytest.fixture(autouse=True)
def _reset_email_alert_rate_limiter():
    """@ratelimit_by_args wraps EmailAlertSender.send_alert exactly once, at
    class-definition (import) time -- so the `rate_limits` dict it closes
    over, and every RateLimitDecorator (and its live sqlite3 connection to
    `file:ratelimit?mode=memory&cache=shared`) it ever creates, live for the
    entire test process, not just for one test.

    That shared, `cache=shared` in-memory sqlite database is only reset once
    the *last* connection referencing it closes. tests/test_unit_ratelimit.py
    relies on exactly that reset happening between its own test functions
    (several of which reuse the same table name for "do_call" + arg "a").
    Any test here that actually calls send_alert() -- not just constructs an
    EmailAlertSender -- opens one of those connections and, without this
    fixture, would keep it open for the rest of the session, corrupting
    test_unit_ratelimit.py's independent-window assumption purely due to
    file/test ordering. This fixture closes and drops every connection this
    file's tests created so the shared in-memory database can be dropped
    again after this module finishes, restoring the isolation other test
    modules (and PooledAlerter/etc.) expect. It touches no production code.
    """
    yield
    wrapper = EmailAlertSender.send_alert
    rate_limits_cell = _cell_for(wrapper, "rate_limits")
    if rate_limits_cell is None:
        return
    rate_limits = rate_limits_cell.cell_contents
    for decorated in rate_limits.values():
        self_cell = _cell_for(decorated, "self")
        if self_cell is not None:
            self_cell.cell_contents.database.close()
    rate_limits.clear()
    gc.collect()


class _FakeSMTPServer:
    """Just enough SMTP on loopback for smtplib.SMTP to complete a send.

    Records every MAIL FROM / RCPT TO / DATA payload it receives so tests can
    assert on message content, sender, recipient and login credentials.
    """

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(5)
        self.host, self.port = self._sock.getsockname()
        self.received = []
        self.login_attempted = None
        self._thread = threading.Thread(target=self._serve_one, daemon=True)
        self._thread.start()

    def _serve_one(self):
        try:
            conn, _ = self._sock.accept()
        except socket.timeout:
            return
        conn.settimeout(5)
        with conn:
            f = conn.makefile("rwb")
            f.write(b"220 fake-smtp ready\r\n")
            f.flush()
            mail_from = None
            rcpt_to = None
            while True:
                line = f.readline()
                if not line:
                    return
                cmd = line.decode("ascii", "replace").strip()
                upper = cmd.upper()
                if upper.startswith("EHLO") or upper.startswith("HELO"):
                    f.write(b"250-fake-smtp\r\n250 AUTH LOGIN PLAIN\r\n")
                elif upper.startswith("AUTH LOGIN"):
                    f.write(b"334 VXNlcm5hbWU6\r\n")
                    f.flush()
                    user_line = f.readline()
                    f.write(b"334 UGFzc3dvcmQ6\r\n")
                    f.flush()
                    pass_line = f.readline()
                    self.login_attempted = (user_line.strip(), pass_line.strip())
                    f.write(b"235 Authentication successful\r\n")
                elif upper.startswith("MAIL FROM"):
                    mail_from = cmd
                    f.write(b"250 OK\r\n")
                elif upper.startswith("RCPT TO"):
                    rcpt_to = cmd
                    f.write(b"250 OK\r\n")
                elif upper.startswith("DATA"):
                    f.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                    f.flush()  # client waits for 354 before sending the body
                    body_lines = []
                    while True:
                        data_line = f.readline()
                        if data_line in (b".\r\n", b".\n"):
                            break
                        if not data_line:
                            break
                        body_lines.append(data_line)
                    self.received.append({
                        "mail_from": mail_from,
                        "rcpt_to": rcpt_to,
                        "body": b"".join(body_lines).decode("utf-8", "replace"),
                    })
                    f.write(b"250 OK: queued\r\n")
                elif upper.startswith("QUIT"):
                    f.write(b"221 Bye\r\n")
                    f.flush()
                    return
                else:
                    f.write(b"500 unrecognized command\r\n")
                f.flush()

    def close(self):
        self._sock.close()
        self._thread.join(timeout=5)


@pytest.fixture
def fake_smtp_server():
    server = _FakeSMTPServer()
    yield server
    server.close()


@pytest.fixture
def logger():
    import logging
    return logging.getLogger("test-email-alert")


class TestEmailAlertSenderPlainSend:
    def test_sends_default_down_message_with_expected_headers(self, fake_smtp_server, logger):
        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host=fake_smtp_server.host,
            login=None,
            password=None,
            from_address="alerts@example.com",
            to_address="ops@example.com",
            logger=logger,
            security="none",
            port=fake_smtp_server.port,
        )

        sender.send_alert("tunnel-a-1")

        fake_smtp_server._thread.join(timeout=5)
        assert len(fake_smtp_server.received) == 1
        message = fake_smtp_server.received[0]
        assert "ops@example.com" in message["rcpt_to"]
        assert "tunnel-a-1 is down" in message["body"]
        assert "mgr-1" in message["body"]
        assert "Subject: Connector tunnel-a-1 notification" in message["body"]

    def test_sends_custom_message_text_instead_of_default(self, fake_smtp_server, logger):
        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host=fake_smtp_server.host,
            login=None,
            password=None,
            from_address="alerts@example.com",
            to_address="ops@example.com",
            logger=logger,
            security="none",
            port=fake_smtp_server.port,
        )

        sender.send_alert("tunnel-a-2", message="custom failure text")

        fake_smtp_server._thread.join(timeout=5)
        assert "custom failure text" in fake_smtp_server.received[0]["body"]

    def test_logs_in_when_credentials_provided(self, fake_smtp_server, logger):
        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host=fake_smtp_server.host,
            login="smtpuser@example.com",
            password="smtppass",
            to_address="ops@example.com",
            logger=logger,
            security="none",
            port=fake_smtp_server.port,
        )

        sender.send_alert("tunnel-a-3")

        fake_smtp_server._thread.join(timeout=5)
        assert fake_smtp_server.login_attempted is not None

    def test_from_address_defaults_to_login(self, fake_smtp_server, logger):
        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host=fake_smtp_server.host,
            login="sender@example.com",
            password="pw",
            to_address="ops@example.com",
            logger=logger,
            security="none",
            port=fake_smtp_server.port,
        )

        sender.send_alert("tunnel-a-4")

        fake_smtp_server._thread.join(timeout=5)
        assert "sender@example.com" in fake_smtp_server.received[0]["mail_from"]


class TestEmailAlertSenderSecurityBranches:
    """tls/ssl only change which smtplib class is used and whether starttls()
    runs; real TLS negotiation is out of scope, so smtplib is mocked here.
    """

    def test_ssl_security_uses_smtp_ssl_class(self, logger, monkeypatch):
        calls = {}

        class FakeSMTPSSL:
            def __init__(self, host, port, timeout=None):
                calls["init"] = (host, port, timeout)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self):
                calls["starttls"] = True

            def login(self, user, password):
                calls["login"] = (user, password)

            def sendmail(self, *args):
                calls["sendmail"] = args
                return {}

        monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTPSSL)
        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host="smtp.example.com",
            login="user@example.com",
            password="p",
            to_address="ops@example.com",
            logger=logger,
            security="ssl",
            port=465,
        )

        sender.send_alert("tunnel-a-5")

        assert calls["init"] == ("smtp.example.com", 465, 10)
        assert "starttls" not in calls  # ssl branch does not call starttls()
        assert calls["login"] == ("user@example.com", "p")

    def test_tls_security_calls_starttls_on_plain_smtp(self, logger, monkeypatch):
        calls = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                calls["init"] = (host, port, timeout)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self):
                calls["starttls"] = True

            def login(self, user, password):
                calls["login"] = (user, password)

            def sendmail(self, *args):
                return {}

        monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host="smtp.example.com",
            login="user@example.com",
            password="p",
            to_address="ops@example.com",
            logger=logger,
            security="tls",
            port=587,
        )

        sender.send_alert("tunnel-a-6")

        assert calls.get("starttls") is True

    def test_none_security_does_not_call_starttls(self, logger, monkeypatch):
        calls = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def starttls(self):
                calls["starttls"] = True

            def login(self, user, password):
                calls["login"] = (user, password)

            def sendmail(self, *args):
                return {}

        monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host="smtp.example.com",
            login=None,
            password=None,
            from_address="alerts@example.com",
            to_address="ops@example.com",
            logger=logger,
            security=None,
            port=25,
        )

        sender.send_alert("tunnel-a-7")

        assert "starttls" not in calls
        assert "login" not in calls  # no login when self.login is falsy

    def test_invalid_security_value_raises_value_error(self, logger):
        with pytest.raises(ValueError):
            EmailAlertSender(
                tunnel_manager_id="mgr-1",
                host="smtp.example.com",
                login=None,
                password=None,
                from_address="alerts@example.com",
                to_address="ops@example.com",
                logger=logger,
                security="wat",
            )


class TestEmailAlertSenderFailurePaths:
    def test_connection_refused_is_swallowed_by_default(self, logger):
        # Nothing is listening on this loopback port.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]

        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host="127.0.0.1",
            login=None,
            password=None,
            from_address="alerts@example.com",
            to_address="ops@example.com",
            logger=logger,
            security="none",
            port=free_port,
        )

        sender.send_alert("tunnel-a-8")  # must not raise

    def test_connection_refused_reraises_when_exception_on_failure(self, logger):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]

        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host="127.0.0.1",
            login=None,
            password=None,
            from_address="alerts@example.com",
            to_address="ops@example.com",
            logger=logger,
            security="none",
            port=free_port,
        )

        with pytest.raises(OSError):
            sender.send_alert("tunnel-a-9", exception_on_failure=True)

    def test_login_failure_is_reraised_when_exception_on_failure(self, logger, monkeypatch):
        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def login(self, user, password):
                raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

            def sendmail(self, *args):
                return {}

        monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host="smtp.example.com",
            login="user@example.com",
            password="wrong",
            to_address="ops@example.com",
            logger=logger,
            security="none",
        )

        with pytest.raises(smtplib.SMTPAuthenticationError):
            sender.send_alert("tunnel-a-10", exception_on_failure=True)


class TestEmailAlertSenderRateLimit:
    def test_second_alert_for_same_tunnel_within_the_window_is_rate_limited(self, fake_smtp_server, logger):
        """@ratelimit_by_args(calls=1, period=600) keys its window on every
        positional/keyword argument, including `self`, so this must reuse one
        sender instance and the same tunnel_name across both calls to observe
        the limit (alerts/CLAUDE.md: one independent 10-minute window per
        unique combination of args).
        """
        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host=fake_smtp_server.host,
            login=None,
            password=None,
            from_address="alerts@example.com",
            to_address="ops@example.com",
            logger=logger,
            security="none",
            port=fake_smtp_server.port,
        )

        sender.send_alert("rate-limited-tunnel")
        with pytest.raises(RateLimitException):
            sender.send_alert("rate-limited-tunnel")


class TestBuildMessage:
    def test_raises_without_tunnel_name_or_message(self, logger):
        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host="smtp.example.com",
            login=None,
            password=None,
            from_address="alerts@example.com",
            to_address="ops@example.com",
            logger=logger,
        )
        with pytest.raises(ValueError):
            sender._build_message(None, None)

    def test_subject_without_tunnel_name(self, logger):
        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host="smtp.example.com",
            login=None,
            password=None,
            from_address="alerts@example.com",
            to_address="ops@example.com",
            logger=logger,
        )
        message = sender._build_message(None, "just a message")
        assert message["Subject"] == "Connector notification"


class TestSendmailPartialFailureIsWarned:
    """smtplib.sendmail() returns a non-empty dict of {refused_recipient: reason}
    for recipients it could not deliver to, even when the overall call
    succeeds; that must be surfaced as a warning rather than silently dropped.
    """

    def test_non_empty_sendmail_result_logs_a_warning(self, logger, monkeypatch):
        warnings = []

        class _WarningCapturingLogger:
            def info(self, *a, **k):
                pass

            def warning(self, *a, **k):
                warnings.append((a, k))

            def exception(self, *a, **k):
                pytest.fail("should not log an exception on a merely partial failure")

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def sendmail(self, *args):
                return {"unreachable@example.com": (550, b"mailbox unavailable")}

        monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
        sender = EmailAlertSender(
            tunnel_manager_id="mgr-1",
            host="smtp.example.com",
            login=None,
            password=None,
            from_address="alerts@example.com",
            to_address="ops@example.com",
            logger=_WarningCapturingLogger(),
        )

        sender.send_alert("partial-failure-tunnel")

        assert len(warnings) == 1
        assert "unreachable@example.com" in str(warnings[0])
