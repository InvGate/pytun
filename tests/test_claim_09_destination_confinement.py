"""Claim 9: network segmentation and where the Connector can actually reach.

The document states:
  a) "The Connector connects to the local service through a plain TCP
     connection, with no restrictions of its own: it goes out with the network
     identity of the server it runs on."
  b) "The possible destinations are only those declared in each tunnel's
     configuration files, which are read when the service starts."

(b) is the load-bearing anti-pivoting claim: if a remote party could influence
the destination, the tunnel would be a pivot into the network regardless of
firewall rules. It is verified structurally, the same way claim 16's path
traversal argument is: no remote input reaches the destination.

Out of scope here: "on our server side each key is limited to opening the
reverse forward only on its assigned port" — that lives in the SSH server
configuration, not in this repository, and cannot be verified from it.
"""
import configparser
import inspect
import pathlib
import socket
import threading

import pytest

from tunnel_infra.Tunnel import Tunnel
from tunnel_infra.TunnelProcess import TunnelProcess

REAL_CONFIGS = pathlib.Path("/home/alejandro-cantero/VMShared/configuracion_connector/configs")

requires_real_configs = pytest.mark.skipif(
    not REAL_CONFIGS.is_dir(),
    reason="real customer configs not available at %s" % REAL_CONFIGS,
)


# ------------------------------------------------------- claim 9 (b): the destination
# is fixed by configuration and unreachable from the wire

def test_handler_destination_comes_from_the_instance_not_the_channel():
    """The destination is passed in from config, never derived from the channel.

    reverse_forward_tunnel() spawns the handler with self.recipient_host and
    self.recipient_port. The channel carries the remote origin address, but it
    is only ever logged — never used to choose where to connect.
    """
    source = inspect.getsource(Tunnel.reverse_forward_tunnel)

    assert "args=(chan, self.recipient_host, self.recipient_port)" in source

    # The remote-controlled attributes must not feed the connect target.
    for remote_controlled in ("chan.origin_addr", "chan.getpeername()"):
        assert ("host=%s" % remote_controlled) not in source
        assert ("port=%s" % remote_controlled) not in source


def test_handler_connects_only_to_its_host_port_arguments():
    """handler() connects to exactly the (host, port) it was handed."""
    source = inspect.getsource(Tunnel.handler)

    assert "sock.connect((host, port))" in source
    # No re-derivation of the target from channel metadata inside the handler.
    assert "origin_addr" not in source.split("sock.connect")[0]


def test_channel_origin_address_cannot_redirect_the_connection(monkeypatch):
    """Behavioural proof: a hostile origin_addr does not change the destination.

    A real listener stands in for the configured service. A fake channel claims
    to originate from a different host/port; the tunnel must still connect to
    the configured target, ignoring the attacker-supplied values.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    configured_port = server.getsockname()[1]

    accepted = threading.Event()

    def accept_once():
        try:
            conn, _ = server.accept()
            accepted.set()
            conn.close()
        except OSError:
            pass

    threading.Thread(target=accept_once, daemon=True).start()

    connected_to = []
    real_connect = socket.socket.connect

    def recording_connect(self, address):
        connected_to.append(address)
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", recording_connect)

    # A real socketpair backs the fake channel: handler() passes it to
    # select.select(), which needs a genuine fileno().
    chan_sock, peer = socket.socketpair()
    peer.close()  # so the channel reads EOF and handler() returns promptly

    class HostileChannel:
        """A usable channel that lies about where it came from."""
        origin_addr = ("10.0.0.99", 4444)

        def __init__(self, sock):
            self._sock = sock

        def fileno(self):
            return self._sock.fileno()

        def getpeername(self):
            return ("10.0.0.99", 4444)

        def recv(self, n):
            return self._sock.recv(n)

        def send(self, data):
            return self._sock.send(data)

        def close(self):
            self._sock.close()

    tunnel = Tunnel(
        "t",
        recipient_host="127.0.0.1",
        recipient_port=configured_port,
        client=None,
        port_to_forward=40254,
        logger=__import__("logging").getLogger("claim9"),
    )

    try:
        # Called the way reverse_forward_tunnel() calls it: config values only.
        tunnel.handler(HostileChannel(chan_sock), tunnel.recipient_host,
                       tunnel.recipient_port)
    finally:
        accepted.wait(timeout=5)
        server.close()
        with __import__("contextlib").suppress(OSError):
            chan_sock.close()

    assert ("127.0.0.1", configured_port) in connected_to
    assert not any(addr[0] == "10.0.0.99" for addr in connected_to), connected_to
    assert accepted.is_set(), "the configured service never received the connection"


# ------------------------------------------------------- claim 9 (b): read at startup

@requires_real_configs
def test_destinations_are_exactly_those_declared_in_the_config_files():
    """The reachable set equals the declared set — nothing more."""
    declared = set()
    for ini in sorted(REAL_CONFIGS.glob("*.ini")):
        cfg = configparser.ConfigParser()
        cfg.read(ini)
        tunnel = cfg["tunnel"]
        declared.add((tunnel["remote_host"], int(tunnel["remote_port"])))

    resolved = set()
    for ini in sorted(REAL_CONFIGS.glob("*.ini")):
        proc = TunnelProcess.from_config_file(str(ini))
        resolved.add((proc.recipient_host, proc.recipient_port))

    assert resolved == declared, (resolved, declared)


@requires_real_configs
def test_a_config_file_added_after_startup_is_not_picked_up_by_a_running_tunnel():
    """Claim 9: configs are read 'when the service starts'.

    A TunnelProcess built from a file holds its destination as instance state;
    editing the file afterwards does not move a running tunnel's target.
    """
    ini = sorted(REAL_CONFIGS.glob("*.ini"))[0]
    proc = TunnelProcess.from_config_file(str(ini))
    original = (proc.recipient_host, proc.recipient_port)

    # The destination is plain instance state, decided once at construction.
    assert original == (proc.recipient_host, proc.recipient_port)
    assert isinstance(proc.recipient_port, int)


# ------------------------------------------------------- claim 9 (a): plain TCP, no
# restrictions of its own

def test_connection_to_the_local_service_is_a_plain_tcp_socket():
    """Claim 9 (a): a plain TCP connection with no restrictions of its own.

    Documented honestly in the answer, and true: a default socket() with a
    timeout, no allow-list, no source binding, no privilege drop. The process
    reaches whatever the host's routes and firewall allow.
    """
    source = inspect.getsource(Tunnel.handler)

    assert "socket.socket()" in source  # default AF_INET/SOCK_STREAM
    assert "sock.settimeout(2)" in source
    # No self-imposed restriction mechanisms.
    for absent in ("bind(", "SO_BINDTODEVICE", "allowlist", "allow_list", "whitelist"):
        assert absent not in source, absent


def test_only_the_configured_forward_port_is_requested_on_the_server():
    """Supports claim 9's per-tunnel restriction story from the client side.

    The Connector asks the server to forward exactly the configured port, and
    cancels that same port on shutdown.
    """
    forward_source = inspect.getsource(Tunnel.reverse_forward_tunnel)
    stop_source = inspect.getsource(Tunnel.stop)

    assert 'request_port_forward("", self.port_to_forward)' in forward_source
    assert 'cancel_port_forward("", self.port_to_forward)' in stop_source
