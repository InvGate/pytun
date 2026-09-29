"""Reusable in-process paramiko SSH server that supports reverse port forwarding.

Used to exercise tunnel_infra/Tunnel.py's reverse_forward_tunnel()/handler()/
validate_tunnel_up() against a real SSH transport (request_port_forward,
accept(), open_session()) instead of only source inspection or duck-typed
fakes. Kept in its own module so more than one test file can reuse it.
"""
import select
import socket
import threading
import time

import paramiko


class _AuthorizedKeyServerInterface(paramiko.ServerInterface):
    """Minimal server-side auth + reverse-forward acceptance."""

    def __init__(self, authorized_key):
        self.authorized_key = authorized_key
        self.forward_requests = []
        self.listener = None

    def get_allowed_auths(self, username):
        return "publickey"

    def check_auth_publickey(self, username, key):
        if key.get_base64() == self.authorized_key.get_base64():
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_port_forward_request(self, address, port):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind((address or "127.0.0.1", 0))
        listener.listen(5)
        listener.settimeout(5)
        self.listener = listener
        bound_port = listener.getsockname()[1]
        self.forward_requests.append((address or "127.0.0.1", port, bound_port))
        return bound_port

    def cancel_port_forward_request(self, address, port):
        if self.listener is not None:
            self.listener.close()
            self.listener = None


class ReverseForwardSSHServer:
    """A real, minimal paramiko SSH server on loopback that accepts one
    client connection, authenticates by public key, honours
    ``request_port_forward``, and relays any TCP connection made to the
    forwarded port through a ``forwarded-tcpip`` channel back to the client.
    """

    def __init__(self, authorized_key):
        self.host_key = paramiko.ECDSAKey.generate()
        self.authorized_key = authorized_key
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self._sock.settimeout(5)
        self.host, self.port = self._sock.getsockname()
        self.transport = None
        self.server_iface = None
        self._threads = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except (socket.timeout, OSError):
            return
        transport = paramiko.Transport(conn)
        self.transport = transport
        transport.add_server_key(self.host_key)
        self.server_iface = _AuthorizedKeyServerInterface(self.authorized_key)
        try:
            transport.start_server(server=self.server_iface)
        except (paramiko.SSHException, EOFError, OSError):
            return
        accept_thread = threading.Thread(target=self._accept_forwarded, daemon=True)
        accept_thread.start()
        self._threads.append(accept_thread)

    def wait_for_forward_request(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.server_iface is not None and self.server_iface.forward_requests:
                return self.server_iface.forward_requests[-1]
            time.sleep(0.05)
        raise TimeoutError("Server never received a port-forward request")

    def _accept_forwarded(self):
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.server_iface is not None and self.server_iface.listener is not None:
                break
            time.sleep(0.05)
        listener = self.server_iface.listener if self.server_iface else None
        if listener is None:
            return
        while True:
            try:
                conn, addr = listener.accept()
            except (socket.timeout, OSError):
                return
            t = threading.Thread(target=self._relay, args=(conn, addr), daemon=True)
            t.start()
            self._threads.append(t)

    def _relay(self, conn, addr):
        address, _, bound_port = self.server_iface.forward_requests[-1]
        try:
            chan = self.transport.open_channel(
                "forwarded-tcpip", (address, bound_port), addr, timeout=5
            )
        except paramiko.SSHException:
            conn.close()
            return
        if chan is None:
            conn.close()
            return
        conn.settimeout(5)
        try:
            while True:
                r, _, _ = select.select([conn, chan], [], [], 5)
                if conn in r:
                    data = conn.recv(1024)
                    if not data:
                        break
                    chan.sendall(data)
                if chan in r:
                    data = chan.recv(1024)
                    if not data:
                        break
                    conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()
            chan.close()

    def close(self):
        self._sock.close()
        if self.transport is not None:
            self.transport.close()
        self._thread.join(timeout=5)
        for t in self._threads:
            t.join(timeout=5)

    def server_key_line(self):
        return "[%s]:%s %s %s\n" % (
            self.host, self.port, self.host_key.get_name(), self.host_key.get_base64(),
        )
