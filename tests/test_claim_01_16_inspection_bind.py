"""Claims 1 and 16: the introspection port binds to localhost only by default.

Claim 1 states the /status endpoint can be integrated with observability tools
"as long as it is queried from 127.0.0.1, because the introspection port is
meant for local access".

Claim 16 states the connector "is installed configured to accept connections
only from the machine itself (inspection_localhost_only=True), which makes the
port unreachable from the network".

Both rest on get_inspection_address() translating the config flag into the
bind address.
"""
import configparser
import socket

import pytest

from pytun import get_inspection_address


def params(**values):
    config = configparser.ConfigParser()
    config["pytun"] = {str(k): str(v) for k, v in values.items()}
    return config["pytun"]


def test_default_bind_is_localhost_when_flag_absent():
    """The claim says True is the shipped default, so an absent flag must bind local."""
    host, port = get_inspection_address(params(tunnel_manager_id="x"))
    assert host == "127.0.0.1"
    assert port == 9999


@pytest.mark.parametrize("value", ["True", "true", "1", "yes", "on"])
def test_truthy_flag_binds_localhost(value):
    host, _ = get_inspection_address(params(inspection_localhost_only=value))
    assert host == "127.0.0.1"


@pytest.mark.parametrize("value", ["False", "false", "0", "no", "off"])
def test_falsy_flag_binds_all_interfaces(value):
    """The documented danger case: disabling the flag exposes it on every interface."""
    host, _ = get_inspection_address(params(inspection_localhost_only=value))
    assert host == "0.0.0.0"


def test_custom_inspection_port_is_honoured():
    _, port = get_inspection_address(params(inspection_port="18080"))
    assert port == 18080


def test_localhost_bind_is_actually_unreachable_from_a_routable_address():
    """Claim 16 says the port is 'unreachable from the network'.

    Bind a real socket the way the connector does and prove a connection to a
    non-loopback local address is refused.
    """
    host, _ = get_inspection_address(params(inspection_localhost_only="True"))

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, 0))
    server.listen(1)
    bound_port = server.getsockname()[1]
    try:
        # Loopback reaches it.
        with socket.create_connection(("127.0.0.1", bound_port), timeout=2):
            pass

        routable_ip = _primary_non_loopback_ip()
        if routable_ip is None:
            pytest.skip("no non-loopback IPv4 address available on this host")

        with pytest.raises((ConnectionRefusedError, socket.timeout, OSError)):
            with socket.create_connection((routable_ip, bound_port), timeout=2):
                pass
    finally:
        server.close()


def _primary_non_loopback_ip():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, no packets are sent
        ip = probe.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None
    finally:
        probe.close()
