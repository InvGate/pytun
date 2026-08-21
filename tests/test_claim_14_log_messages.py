"""Claim 14: what gets logged on tunnel failure, and where.

The document states the Connector "does not write to the Event Viewer: all
logging goes to the log files in the configured directory", and publishes a
table of literal messages a customer is expected to grep or ingest:

    Connector down!                              -> tunnel lost connectivity
    Connector down! Transport is not active      -> SSH transport stopped
    Failed to connect to <host>:<port>           -> could not reach our server
    Port forwarding stopped with error           -> forwarding interrupted
    Forwarding request to <host>:<port> failed    -> local service unreachable
    Connector <file> is down                     -> detected by supervision loop
    Going to restart connector from file <file>   -> automatic restart began

plus three authentication errors: BadHostKeyException, AuthenticationException,
PasswordRequiredException.

These strings are a contract: the document tells the customer to build
monitoring on them. A message that does not exist verbatim is an alert that
never fires, so each is asserted against the source that emits it.
"""
import inspect
import logging
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = ("tests", "build", "dist", "__pycache__")


def _is_project_source(path):
    """Only first-party source. A dependency mentioning win32evtlog is not us."""
    parts = path.parts
    if any(part.startswith(".venv") or part == "venv" or part == "site-packages"
           for part in parts):
        return False
    return not any(part in EXCLUDED_DIRS for part in parts)


SOURCE_FILES = sorted(p for p in REPO.rglob("*.py") if _is_project_source(p))


def all_source():
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in SOURCE_FILES)


# ------------------------------------------------- the published message table

PUBLISHED_MESSAGES = [
    ("Connector down!", "tunnel_infra/Tunnel.py"),
    ("Connector down! Transport is not active", "tunnel_infra/Tunnel.py"),
    ("Failed to connect to %s:%d", "tunnel_infra/TunnelProcess.py"),
    ("Port forwarding stopped with error", "tunnel_infra/TunnelProcess.py"),
    ("Forwarding request to %s:%d failed", "tunnel_infra/Tunnel.py"),
    ("Connector %s is down", "pytun.py"),
    ("Going to restart connector from file %s", "pytun.py"),
]


@pytest.mark.parametrize("message,expected_file", PUBLISHED_MESSAGES,
                         ids=[m for m, _ in PUBLISHED_MESSAGES])
def test_published_message_exists_verbatim(message, expected_file):
    """Each documented message must exist in the source, in the stated component."""
    source = (REPO / expected_file).read_text(encoding="utf-8")
    assert message in source, (
        "documented message %r not found in %s" % (message, expected_file)
    )


@pytest.mark.parametrize("exception_name", [
    "BadHostKeyException",
    "AuthenticationException",
    "PasswordRequiredException",
])
def test_documented_auth_exception_is_caught_and_logged(exception_name):
    """The three published authentication errors must be handled, not swallowed."""
    source = (REPO / "pytun.py").read_text(encoding="utf-8")
    assert "except %s as e:" % exception_name in source


# --------------------------------------------- "does not write to Event Viewer"

def test_nothing_writes_to_the_windows_event_log():
    """Claim 14's structural assertion: logging goes to files, never Event Viewer."""
    source = all_source()
    for forbidden in ("NTEventLogHandler", "win32evtlog", "win32evtlogutil",
                      "ReportEvent", "servicemanager.LogInfoMsg"):
        assert forbidden not in source, "found Event Log usage: %s" % forbidden


def test_logging_goes_to_a_rotating_file_handler():
    """The counterpart: the sink actually is a file in the configured directory."""
    from configure_logger import LogManager

    source = inspect.getsource(LogManager.configure_logger)
    assert "TimedRotatingFileHandler" in source
    assert "os.path.join(path, filename)" in source


# ------------------------------------- the messages actually fire when expected

def test_connector_down_messages_are_emitted_by_validate_tunnel_up():
    """'Connector down!' variants must come from the health check that detects it."""
    from tunnel_infra.Tunnel import Tunnel

    source = inspect.getsource(Tunnel.validate_tunnel_up)
    assert "Connector down! %s" in source
    assert "Connector down! Transport is not active" in source
    # The document says this message drives the automatic restart.
    assert "self.failed = True" in source


def test_supervision_loop_logs_down_then_restart_in_that_order():
    """Claim 14: 'Connector <file> is down' precedes the restart message."""
    import pytun

    check_source = inspect.getsource(pytun.check_tunnels)
    restart_source = inspect.getsource(pytun.restart_tunnels)

    assert "Connector %s is down" in check_source
    assert "to_restart.append(key)" in check_source
    assert "Going to restart connector from file %s" in restart_source


def test_forwarding_failure_message_reports_the_local_service_target(caplog):
    """'Forwarding request to <host>:<port> failed' must name the local service.

    Emitted for real by pointing the forwarder at a closed port.
    """
    from tunnel_infra.Tunnel import Tunnel

    logger = logging.getLogger("claim14-forward")
    tunnel = Tunnel.__new__(Tunnel)  # bypass __init__: only handler() is exercised
    tunnel.logger = logger
    tunnel.alert_senders = None
    tunnel.name = "t"

    with caplog.at_level(logging.ERROR, logger="claim14-forward"):
        # Port 1 on loopback is closed; connect() fails and the message fires.
        tunnel.handler(chan=None, host="127.0.0.1", port=1)

    assert any("Forwarding request to 127.0.0.1:1 failed" in r.getMessage()
               for r in caplog.records), caplog.text


# ------------------------------------------------------- accuracy of the table

@pytest.mark.xfail(
    strict=True,
    reason="known defect: auth handlers log recipient_host (local service) "
           "for failures against the SSH server; see claim 14 accuracy note",
)
def test_auth_failure_messages_name_the_ssh_server_not_the_local_service():
    """Accuracy check on the authentication messages.

    Claim 14 presents the auth errors as server-identity/key problems, and
    claim 6 says a mismatched server identity is what gets rejected. But the
    handlers in test_tunnels() format 'Failed to connect with service %s:%s'
    with recipient_host/recipient_port — the LOCAL service — even though what
    failed is the connection to the SSH server (server_host/server_port).

    A customer following the log would investigate the wrong endpoint. This
    test documents the discrepancy; it is expected to fail until the messages
    are corrected.
    """
    import pytun

    source = inspect.getsource(pytun.test_tunnels)
    handlers = ("BadHostKeyException", "AuthenticationException",
                "PasswordRequiredException")

    offenders = []
    for exc in handlers:
        start = source.index("except %s as e:" % exc)
        # The block runs to the next except at the same level, or to the end.
        rest = source[start + len("except %s as e:" % exc):]
        next_except = rest.find("\n        except ")
        block = rest if next_except == -1 else rest[:next_except]

        assert "logger.exception" in block, "sliced %s handler too short" % exc
        if "recipient_host" in block:
            offenders.append(exc)

    assert not offenders, (
        "these handlers report recipient_host/recipient_port (the LOCAL service) "
        "for a failure against the SSH server (server_host/server_port): %s"
        % ", ".join(offenders)
    )
