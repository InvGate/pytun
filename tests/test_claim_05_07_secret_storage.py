"""Claims 5 and 7: how secrets are stored, and what the connector does not support.

Claim 5 states the keyfile is "a file on disk, inside the Connector's
configuration directory. The path is defined by the keyfile parameter of each
tunnel's configuration... It is not encrypted: its protection depends on NTFS
permissions."

Claim 7 states: "In the current version, no. SMTP and HTTP passwords are read
from the configuration file in plain text, and the Connector does not support
environment variables, nor the Credentials Manager."

Both are NEGATIVE claims — assertions that a capability is absent. Proving an
absence needs two halves: the alternative mechanisms are nowhere in the code,
AND the one path that does exist is the plain-file path. Both halves are
asserted here.
"""
import configparser
import inspect
import os
import pathlib
import stat

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
REAL_CONFIGS = pathlib.Path("/home/alejandro-cantero/VMShared/configuracion_connector/configs")

EXCLUDED_DIRS = ("tests", "build", "dist", "__pycache__")


def _is_project_source(path):
    """Only first-party source. Virtualenvs and build output are not the product."""
    parts = path.parts
    if any(part.startswith(".venv") or part == "venv" or part == "site-packages"
           for part in parts):
        return False
    return not any(part in EXCLUDED_DIRS for part in parts)


SOURCE_FILES = sorted(p for p in REPO.rglob("*.py") if _is_project_source(p))

requires_real_configs = pytest.mark.skipif(
    not REAL_CONFIGS.is_dir(),
    reason="real customer configs not available at %s" % REAL_CONFIGS,
)


def all_source():
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in SOURCE_FILES)


# ------------------------------------------------------------------- claim 7:
# no environment variables, no Credentials Manager

@pytest.mark.parametrize("mechanism", [
    "os.environ",
    "os.getenv",
    "environ.get",
    "keyring",
    "win32cred",
    "CredRead",
    "CryptUnprotectData",
    "DPAPI",
])
def test_unsupported_secret_mechanism_is_absent_from_the_code(mechanism):
    """Half one of the negative claim: no alternative source exists."""
    assert mechanism not in all_source(), (
        "claim 7 says %s is not supported, but it appears in the source" % mechanism
    )


def test_credentials_are_read_from_the_config_parser_only():
    """Half two: the one path that exists is the plain config file.

    get_post_alert_sender and get_smtp_alert_sender take their secrets straight
    out of the ConfigParser section, with no indirection or decryption.
    """
    import pytun

    for factory in (pytun.get_post_alert_sender, pytun.get_smtp_alert_sender):
        source = inspect.getsource(factory)
        assert "params[" in source or "params.get(" in source
        for forbidden in ("environ", "getenv", "keyring", "decrypt", "unprotect"):
            assert forbidden not in source.lower(), (factory.__name__, forbidden)


def test_a_password_in_the_ini_reaches_the_sender_verbatim():
    """Behavioural proof that the password is used exactly as written on disk.

    No decryption, no unwrapping, no env-var expansion: the literal string in
    the file is the credential.
    """
    import pytun

    secret = "PlainTextPassword123!"
    config = configparser.ConfigParser()
    config["pytun"] = {
        "tunnel_manager_id": "1",
        "http_url": "http://example.invalid/hook",
        "http_user": "someuser",
        "http_password": secret,
    }

    sender = pytun.get_post_alert_sender(
        __import__("logging").getLogger("claim7"), "1", config["pytun"]
    )
    assert sender is not None

    # The credential is held as the literal file contents somewhere on the sender.
    held = [v for v in vars(sender).values() if v == secret]
    assert held, "the plain-text password from the ini did not reach the sender"


def test_percent_style_environment_syntax_is_rejected_outright():
    """A customer might try %VAR% as a workaround. It is worse than unsupported.

    ConfigParser's default BasicInterpolation treats % as its own syntax and
    raises ValueError, so a %VAR% password does not silently pass through as a
    literal — it breaks config loading. Worth telling the customer, since the
    answer only says the mechanism is unsupported.
    """
    config = configparser.ConfigParser()
    with pytest.raises(ValueError, match="interpolation syntax"):
        config["pytun"] = {
            "tunnel_manager_id": "1",
            "http_password": "%PYTUN_CLAIM7_SECRET%",
        }


def test_dollar_style_environment_syntax_is_not_expanded():
    """The other workaround shape: ${VAR} stays literal, never read from the env."""
    import pytun

    os.environ["PYTUN_CLAIM7_SECRET"] = "value-from-environment"
    try:
        config = configparser.ConfigParser()
        config["pytun"] = {
            "tunnel_manager_id": "1",
            "http_url": "http://example.invalid/hook",
            "http_user": "u",
            "http_password": "${PYTUN_CLAIM7_SECRET}",
        }
        sender = pytun.get_post_alert_sender(
            __import__("logging").getLogger("claim7b"), "1", config["pytun"]
        )
        values = list(vars(sender).values())
        assert "${PYTUN_CLAIM7_SECRET}" in values
        assert "value-from-environment" not in values
    finally:
        del os.environ["PYTUN_CLAIM7_SECRET"]


# ------------------------------------------------------------------- claim 5:
# the keyfile is a plain file, resolved from config, not encrypted

@requires_real_configs
def test_keyfile_path_is_resolved_relative_to_the_config_directory():
    """Claim 5: 'a file on disk, inside the Connector's configuration directory'.

    A relative keyfile in the ini resolves against the config file's own
    directory, which is what makes the documented layout work.
    """
    from tunnel_infra.TunnelProcess import TunnelProcess

    for ini in sorted(REAL_CONFIGS.glob("*.ini")):
        cfg = configparser.ConfigParser()
        cfg.read(ini)
        declared = cfg["tunnel"]["keyfile"]
        assert not os.path.isabs(declared), "expected the documented relative form"

        proc = TunnelProcess.from_config_file(str(ini))
        assert proc.key_file == str(ini.parent / declared)
        assert pathlib.Path(proc.key_file).is_file()


@requires_real_configs
def test_private_key_on_disk_is_not_encrypted():
    """Claim 5: 'It is not encrypted.'

    An encrypted PEM carries a Proc-Type/DEK-Info header or uses the PKCS#8
    ENCRYPTED PRIVATE KEY label. Neither is present, and the key loads with no
    password at all.
    """
    from cryptography.hazmat.primitives import serialization
    from tunnel_infra.TunnelProcess import TunnelProcess

    for ini in sorted(REAL_CONFIGS.glob("*.ini")):
        proc = TunnelProcess.from_config_file(str(ini))
        raw = pathlib.Path(proc.key_file).read_bytes()

        assert b"ENCRYPTED" not in raw, proc.key_file
        assert b"Proc-Type" not in raw, proc.key_file
        assert b"DEK-Info" not in raw, proc.key_file

        # Loads with no password: nothing is protecting it but the filesystem.
        serialization.load_pem_private_key(raw, password=None)


def test_the_connector_never_sets_or_hardens_file_permissions():
    """Claim 5: 'its protection depends on NTFS permissions.'

    The corollary the answer implies and customers must act on: the connector
    does not harden anything itself. If the ACL is loose, it stays loose.
    """
    source = all_source()
    for mechanism in ("os.chmod", "icacls", "SetFileSecurity", "SetNamedSecurityInfo",
                      "os.umask"):
        assert mechanism not in source, (
            "found %s: the connector does manage permissions after all" % mechanism
        )


@requires_real_configs
def test_keyfile_is_readable_by_the_process_with_no_extra_credential():
    """The practical meaning of 'protection depends on filesystem permissions'.

    Whoever can read the file has the key. Nothing else gates it.
    """
    from tunnel_infra.TunnelProcess import TunnelProcess

    ini = sorted(REAL_CONFIGS.glob("*.ini"))[0]
    proc = TunnelProcess.from_config_file(str(ini))
    key_path = pathlib.Path(proc.key_file)

    assert os.access(key_path, os.R_OK)
    content = key_path.read_text()
    assert "PRIVATE KEY" in content

    mode = stat.S_IMODE(key_path.stat().st_mode)
    # Reported, not asserted: POSIX modes say nothing about the Windows ACLs
    # the claim actually refers to. Recorded so a reviewer sees the real state.
    print("keyfile %s POSIX mode: %o" % (key_path.name, mode))


def test_ini_files_hold_credentials_in_plain_text_when_configured():
    """Claim 7's admission, stated as a test.

    Written against a synthetic ini so no real customer secret is read.
    """
    config = configparser.ConfigParser()
    config.read_string(
        "[pytun]\n"
        "tunnel_manager_id=1\n"
        "smtp_login=alerts@example.invalid\n"
        "smtp_password=SuperSecret\n"
    )
    assert config["pytun"]["smtp_password"] == "SuperSecret"
    # No hashing, no encoding, no envelope: the file is the vault.
