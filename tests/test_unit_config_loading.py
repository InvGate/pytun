"""Unit tests for pytun.py's connector.ini config-loading helpers.

Covers get_inspection_address(), get_smtp_alert_sender() and
get_post_alert_sender(): reading a real ConfigParser section built in-process
from a tmp_path ini file (never a real repo config), and returning
sender objects, or None when the optional section is absent.

These call pytun's functions directly and never spawn a subprocess or touch
network I/O -- SMTP/HTTP senders are only constructed here, never used to
actually send anything.
"""
import configparser
import logging

import pytest

import pytun
from alerts.email_alert import EmailAlertSender
from alerts.http_post_alert import HTTPPostAlertSender


@pytest.fixture
def logger():
    log = logging.getLogger("test-config-loading")
    log.addHandler(logging.NullHandler())
    return log


def _params_from_ini(tmp_path, body, section="pytun"):
    ini_path = tmp_path / "connector.ini"
    ini_path.write_text(body)
    config = configparser.ConfigParser()
    config.read(str(ini_path))
    return config[section]


class TestGetInspectionAddress:
    def test_defaults_to_localhost_only_and_port_9999(self, tmp_path):
        params = _params_from_ini(tmp_path, "[pytun]\ntunnel_manager_id=abc\n")
        assert pytun.get_inspection_address(params) == ("127.0.0.1", 9999)

    def test_can_open_to_all_interfaces_and_custom_port(self, tmp_path):
        params = _params_from_ini(
            tmp_path,
            "[pytun]\ntunnel_manager_id=abc\ninspection_localhost_only=False\ninspection_port=8080\n",
        )
        assert pytun.get_inspection_address(params) == ("0.0.0.0", 8080)


class TestGetSmtpAlertSender:
    def test_returns_none_when_smtp_hostname_absent(self, tmp_path, logger):
        params = _params_from_ini(tmp_path, "[pytun]\ntunnel_manager_id=abc\n")
        assert pytun.get_smtp_alert_sender(logger, "abc", params) is None

    def test_returns_email_sender_when_smtp_section_present(self, tmp_path, logger):
        params = _params_from_ini(
            tmp_path,
            "[pytun]\n"
            "tunnel_manager_id=abc\n"
            "smtp_hostname=smtp.example.com\n"
            "smtp_to=alerts@example.com\n"
            "smtp_login=alerts@example.com\n"
            "smtp_port=587\n"
            "smtp_security=tls\n",
        )
        sender = pytun.get_smtp_alert_sender(logger, "abc", params)
        assert isinstance(sender, EmailAlertSender)

    def test_exits_when_smtp_hostname_present_but_smtp_to_missing(self, tmp_path, logger):
        # smtp_to is a required positional arg of EmailAlertSender; params['smtp_to']
        # (mandatory dict access, not .get) raises KeyError -> sys.exit(-1).
        params = _params_from_ini(
            tmp_path,
            "[pytun]\ntunnel_manager_id=abc\nsmtp_hostname=smtp.example.com\n",
        )
        with pytest.raises(SystemExit):
            pytun.get_smtp_alert_sender(logger, "abc", params)

    OAUTH_INI = (
        "[pytun]\n"
        "tunnel_manager_id=abc\n"
        "smtp_hostname=smtp.office365.com\n"
        "smtp_port=587\n"
        "smtp_security=tls\n"
        "smtp_login=alerts@example.com\n"
        "smtp_to=ops@example.com\n"
        "smtp_oauth_token_url=https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token\n"
        "smtp_oauth_client_id=client-id\n"
        "smtp_oauth_client_secret=client-secret\n"
        "smtp_oauth_scope=https://outlook.office365.com/.default\n"
    )

    def test_builds_an_oauth_sender_from_the_smtp_oauth_keys(self, tmp_path, logger):
        sender = pytun.get_smtp_alert_sender(logger, "abc", _params_from_ini(tmp_path, self.OAUTH_INI))
        assert sender.oauth == {
            "token_url": "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "scope": "https://outlook.office365.com/.default",
        }

    def test_without_oauth_keys_the_sender_uses_no_oauth(self, tmp_path, logger):
        ini = "\n".join(line for line in self.OAUTH_INI.splitlines() if "oauth" not in line)
        assert pytun.get_smtp_alert_sender(logger, "abc", _params_from_ini(tmp_path, ini)).oauth is None

    def test_any_oauth_key_turns_oauth_on_so_a_missing_client_id_is_not_ignored(self, tmp_path, logger):
        ini = self.OAUTH_INI.replace("smtp_oauth_client_id=client-id\n", "") + "smtp_password=old-password\n"
        with pytest.raises(SystemExit):
            pytun.get_smtp_alert_sender(logger, "abc", _params_from_ini(tmp_path, ini))

    def test_an_empty_oauth_key_counts_as_missing(self, tmp_path, logger):
        ini = self.OAUTH_INI.replace("smtp_oauth_client_id=client-id", "smtp_oauth_client_id=")
        with pytest.raises(SystemExit):
            pytun.get_smtp_alert_sender(logger, "abc", _params_from_ini(tmp_path, ini))

    def test_exits_when_an_oauth_key_is_missing(self, tmp_path, logger):
        ini = self.OAUTH_INI.replace("smtp_oauth_scope=https://outlook.office365.com/.default\n", "")
        with pytest.raises(SystemExit):
            pytun.get_smtp_alert_sender(logger, "abc", _params_from_ini(tmp_path, ini))

    def test_tls_verification_is_on_unless_disabled(self, tmp_path, logger):
        assert pytun.get_smtp_alert_sender(logger, "abc", _params_from_ini(tmp_path, self.OAUTH_INI)).tls_verify
        ini = self.OAUTH_INI + "smtp_tls_verify=False\n"
        assert not pytun.get_smtp_alert_sender(logger, "abc", _params_from_ini(tmp_path, ini)).tls_verify

    def test_exits_when_smtp_tls_verify_is_not_a_boolean(self, tmp_path, logger):
        ini = self.OAUTH_INI + "smtp_tls_verify=maybe\n"
        with pytest.raises(SystemExit):
            pytun.get_smtp_alert_sender(logger, "abc", _params_from_ini(tmp_path, ini))

    def test_exits_when_oauth_is_configured_without_tls(self, tmp_path, logger):
        ini = self.OAUTH_INI.replace("smtp_security=tls", "smtp_security=none")
        with pytest.raises(SystemExit):
            pytun.get_smtp_alert_sender(logger, "abc", _params_from_ini(tmp_path, ini))


class TestGetPostAlertSender:
    def test_returns_none_when_http_url_absent(self, tmp_path, logger):
        params = _params_from_ini(tmp_path, "[pytun]\ntunnel_manager_id=abc\n")
        assert pytun.get_post_alert_sender(logger, "abc", params) is None

    def test_returns_http_post_sender_when_http_section_present(self, tmp_path, logger):
        params = _params_from_ini(
            tmp_path,
            "[pytun]\n"
            "tunnel_manager_id=abc\n"
            "http_url=https://example.com/alert\n"
            "http_user=user\n"
            "http_password=pass\n",
        )
        sender = pytun.get_post_alert_sender(logger, "abc", params)
        assert isinstance(sender, HTTPPostAlertSender)

    def test_exits_when_http_url_present_but_http_user_missing(self, tmp_path, logger):
        params = _params_from_ini(
            tmp_path,
            "[pytun]\ntunnel_manager_id=abc\nhttp_url=https://example.com/alert\n",
        )
        with pytest.raises(SystemExit):
            pytun.get_post_alert_sender(logger, "abc", params)


class TestTunnelManagerIdRequiredness:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Real bug: CLAUDE.md documents tunnel_manager_id as REQUIRED, and "
            "pytun.py's main() has a `if tunnel_manager_id is None: sys.exit(1)` "
            "guard meant to enforce that. But main() reads it via "
            "`params.get('tunnel_manager_id', '')`, so a missing key resolves "
            "to '' (falsy but not None), and the guard never fires -- the "
            "connector proceeds with an empty tunnel_manager_id instead of "
            "exiting. This test documents the CORRECT (currently unmet) "
            "behaviour: a missing tunnel_manager_id should be treated as unset."
        ),
    )
    def test_missing_tunnel_manager_id_defaults_to_none_not_empty_string(self, tmp_path):
        params = _params_from_ini(tmp_path, "[pytun]\nlog_level=INFO\n")
        tunnel_manager_id = params.get("tunnel_manager_id", '')
        assert tunnel_manager_id is None
