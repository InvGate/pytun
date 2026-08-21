"""Claim 12b: which test modes require stopping the service.

The document publishes this table:

    --test_connections  -> No
    --test_smtp         -> No
    --test_http         -> No
    --test_tunnels      -> Yes, recommended
    --test_all          -> No

The interesting cell is --test_all, because test_everything() runs the tunnel
check internally. These tests establish what each mode actually does with the
reverse port forward -- the only operation that can collide with a running
service -- and whether each mode checks the service state first.
"""
import inspect

import pytest

import pytun


# ------------------------------------------------- the operation that collides

def test_only_the_reverse_forward_can_collide_with_a_running_service():
    """Establish the premise: request_port_forward is the conflicting operation.

    A running service already holds the reverse forward for the configured
    port; asking for it again is what the server rejects. Everything else in
    the test paths is an outbound connection, which does not conflict.
    """
    source = inspect.getsource(pytun.test_tunnels)

    assert "transport.request_port_forward" in source
    # And the code knows the failure mode by name.
    assert "Port binding rejected" in source


def test_reverse_forward_is_guarded_by_the_test_reverse_forward_flag():
    """The collision is opt-out via a parameter, not unconditional."""
    signature = inspect.signature(pytun.test_tunnels)
    assert "test_reverse_forward" in signature.parameters
    assert signature.parameters["test_reverse_forward"].default is True

    source = inspect.getsource(pytun.test_tunnels)
    assert "if test_reverse_forward:" in source


# ----------------------------------------------------- --test_tunnels: "Yes"

def test_test_tunnels_always_requests_the_reverse_forward():
    """Table says 'Yes, recommended' -- and the code justifies it.

    test_tunnels_and_exit() calls test_tunnels() without overriding the flag,
    so the default True applies: the reverse forward is always attempted, with
    no check of whether the service is running.
    """
    source = inspect.getsource(pytun.test_tunnels_and_exit)

    assert "test_tunnels(files, logger)" in source
    assert "test_reverse_forward" not in source, (
        "test_tunnels_and_exit unexpectedly overrides the flag"
    )
    # It does not consult the service state at all.
    assert "test_service_is_running" not in source


# --------------------------------------------------------- --test_all: "No"

def test_test_all_disables_the_reverse_forward_when_the_service_is_up():
    """Table says 'No' for --test_all, and this is why it is correct.

    test_everything() checks the service first and passes
    test_reverse_forward=not service_up, so the colliding operation is skipped
    precisely when the service holds the port.
    """
    source = inspect.getsource(pytun.test_everything)

    assert "test_service_is_running(logger)" in source
    assert "test_tunnels(files, logger, test_reverse_forward=not service_up)" in source


def test_test_all_tells_the_user_the_tunnel_check_was_partial():
    """--test_all degrades explicitly rather than silently.

    This is what makes the 'No' honest: the mode still runs, but says so and
    points at stopping the service for a full check.
    """
    source = inspect.getsource(pytun.test_everything)

    assert "partially test the tunnels" in source
    assert "please stop the service" in source


@pytest.mark.parametrize("service_up,expected_reverse_forward", [
    (True, False),   # service running -> skip the colliding operation
    (False, True),   # service stopped -> full check
])
def test_test_all_flag_derivation_is_the_inverse_of_service_state(
    service_up, expected_reverse_forward, monkeypatch
):
    """Behavioural proof of the guard: run test_everything and record the flag.

    test_tunnels and test_connections are stubbed so nothing touches the
    network; only the decision under test is exercised.
    """
    recorded = {}

    monkeypatch.setattr(pytun, "test_service_is_running",
                        lambda logger, service_name='InvGateTunnel': service_up)
    monkeypatch.setattr(pytun, "test_connections",
                        lambda files, logger, processes: False)

    def fake_test_tunnels(files, logger, test_reverse_forward=True):
        recorded["test_reverse_forward"] = test_reverse_forward
        return False

    monkeypatch.setattr(pytun, "test_tunnels", fake_test_tunnels)

    import logging
    pytun.test_everything([], logging.getLogger("claim12b"), {})

    assert recorded["test_reverse_forward"] is expected_reverse_forward


# -------------------------------------- --test_connections / smtp / http: "No"

def test_test_connections_never_requests_a_port_forward():
    """Table says 'No': it only opens outbound sockets to the local services."""
    source = inspect.getsource(pytun.test_connections)

    assert "request_port_forward" not in source
    assert "ssh_connect" not in source
    assert "sock.connect" in source


@pytest.mark.parametrize("factory_name", ["test_mail_and_exit", "test_http_and_exit"])
def test_notification_tests_do_not_touch_the_tunnels(factory_name):
    """Table says 'No' for --test_smtp and --test_http.

    They exercise the alert senders only: no SSH, no port forward, no tunnel
    processes, so a running service is irrelevant.
    """
    source = inspect.getsource(getattr(pytun, factory_name))

    for tunnel_operation in ("request_port_forward", "ssh_connect",
                             "TunnelProcess", "test_tunnels"):
        assert tunnel_operation not in source, (factory_name, tunnel_operation)


# ------------------------------------------------------- accuracy of the table

def test_test_connections_does_not_check_or_require_the_service_state():
    """Reinforces the 'No' rows: these modes are indifferent to the service.

    They neither query the service nor change behaviour based on it.
    """
    for mode in (pytun.test_connections, pytun.test_connections_and_exit):
        source = inspect.getsource(mode)
        assert "test_service_is_running" not in source


@pytest.mark.xfail(
    strict=True,
    reason="known gap: --test_tunnels does not warn that the service should be "
           "stopped, unlike --test_all which detects it and degrades gracefully",
)
def test_test_tunnels_warns_the_user_when_the_service_is_running():
    """The documentation says 'Yes, recommended' -- the tool does not say so.

    --test_all detects a running service, explains the check is partial and
    tells the user to stop the service. --test_tunnels does neither: it
    attempts the reverse forward regardless and, if the service holds the
    port, reports 'Port binding rejected' with advice to check server_host,
    server_port and port in the config -- pointing at configuration when the
    real cause is the running service.

    Documents the gap; expected to fail until --test_tunnels either warns or
    degrades the way --test_all does.
    """
    source = (inspect.getsource(pytun.test_tunnels_and_exit)
              + inspect.getsource(pytun.test_tunnels))

    assert "test_service_is_running" in source or "stop the service" in source
