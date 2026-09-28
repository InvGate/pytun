"""Tests for TunnelProcess.run() and exit_gracefully(), which
test_unit_tunnel_process.py leaves untested (it only covers ssh_connect() and
from_config_file()).

run() is invoked directly in-process (never via .start()/multiprocessing) so
we can patch ssh_connect()/Tunnel and observe sys.exit codes and cleanup
without needing a real subprocess or a real SSH server.
"""
import logging

import pytest

from tunnel_infra.TunnelProcess import TunnelProcess


def _make_process(tmp_path, **overrides):
    kwargs = dict(
        tunnel_name="test-tunnel",
        server_host="127.0.0.1",
        server_port=2222,
        server_port_to_forward=0,
        server_key=None,
        user_to_login="user",
        key_file="unused.key",
        recipient_host="127.0.0.1",
        recipient_port=0,
        keep_alive_time=30,
        log_level="DEBUG",
        log_to_console=False,
        log_path=str(tmp_path),
    )
    kwargs.update(overrides)
    return TunnelProcess(**kwargs)


class _FakeClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeTunnel:
    def __init__(self, *args, **kwargs):
        self.stopped = False
        self.forwarded = False
        self.raise_on_forward = kwargs.get("_raise_on_forward")
        self.raise_keyboard_interrupt = kwargs.get("_raise_keyboard_interrupt")

    def reverse_forward_tunnel(self):
        self.forwarded = True
        if self.raise_keyboard_interrupt:
            raise KeyboardInterrupt()
        if self.raise_on_forward:
            raise self.raise_on_forward

    def stop(self):
        self.stopped = True


class TestExitGracefully:
    def test_stops_tunnel_and_exits_zero(self, tmp_path):
        process = _make_process(tmp_path)
        fake_tunnel = _FakeTunnel()
        process.tunnel = fake_tunnel

        with pytest.raises(SystemExit) as exc_info:
            process.exit_gracefully()

        assert exc_info.value.code == 0
        assert fake_tunnel.stopped is True
        assert process.tunnel is None

    def test_exits_zero_even_without_an_active_tunnel(self, tmp_path):
        process = _make_process(tmp_path)
        process.tunnel = None

        with pytest.raises(SystemExit) as exc_info:
            process.exit_gracefully()

        assert exc_info.value.code == 0


class TestRun:
    def test_successful_forwarding_returns_exit_code_zero_and_cleans_up(self, tmp_path, monkeypatch):
        process = _make_process(tmp_path)
        fake_client = _FakeClient()
        monkeypatch.setattr(process, "ssh_connect", lambda: fake_client)

        created = {}

        def _fake_tunnel_ctor(*args, **kwargs):
            tunnel = _FakeTunnel(*args, **kwargs)
            created["tunnel"] = tunnel
            return tunnel

        import tunnel_infra.TunnelProcess as tp_module
        monkeypatch.setattr(tp_module, "Tunnel", _fake_tunnel_ctor)

        with pytest.raises(SystemExit) as exc_info:
            process.run()

        assert exc_info.value.code == 0
        assert created["tunnel"].forwarded is True
        assert created["tunnel"].stopped is True
        assert fake_client.closed is True

    def test_keyboard_interrupt_returns_exit_code_zero(self, tmp_path, monkeypatch):
        process = _make_process(tmp_path)
        fake_client = _FakeClient()
        monkeypatch.setattr(process, "ssh_connect", lambda: fake_client)

        import tunnel_infra.TunnelProcess as tp_module
        monkeypatch.setattr(
            tp_module, "Tunnel",
            lambda *a, **k: _FakeTunnel(*a, **k, _raise_keyboard_interrupt=True),
        )

        with pytest.raises(SystemExit) as exc_info:
            process.run()

        assert exc_info.value.code == 0
        assert fake_client.closed is True

    def test_forwarding_exception_returns_exit_code_one_and_still_cleans_up(self, tmp_path, monkeypatch):
        process = _make_process(tmp_path)
        fake_client = _FakeClient()
        monkeypatch.setattr(process, "ssh_connect", lambda: fake_client)

        import tunnel_infra.TunnelProcess as tp_module
        monkeypatch.setattr(
            tp_module, "Tunnel",
            lambda *a, **k: _FakeTunnel(*a, **k, _raise_on_forward=RuntimeError("boom")),
        )

        with pytest.raises(SystemExit) as exc_info:
            process.run()

        assert exc_info.value.code == 1
        assert fake_client.closed is True

    def test_run_registers_signal_handlers_for_graceful_shutdown(self, tmp_path, monkeypatch):
        import signal

        process = _make_process(tmp_path)
        fake_client = _FakeClient()
        monkeypatch.setattr(process, "ssh_connect", lambda: fake_client)

        import tunnel_infra.TunnelProcess as tp_module
        monkeypatch.setattr(tp_module, "Tunnel", lambda *a, **k: _FakeTunnel(*a, **k))

        registered = {}
        real_signal = signal.signal

        def _recording_signal(sig, handler):
            registered[sig] = handler
            return real_signal(sig, handler)

        monkeypatch.setattr(tp_module.signal, "signal", _recording_signal)

        with pytest.raises(SystemExit):
            process.run()

        assert registered.get(signal.SIGINT) == process.exit_gracefully
        assert registered.get(signal.SIGTERM) == process.exit_gracefully
