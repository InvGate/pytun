"""Unit tests for device.py: RSA-PSS+SHA256 MAC address signature validation.

Every test generates its own throwaway RSA keypair and signs a MAC address
exactly the way production configs are signed (base64(json({"payload": mac,
"sig": base64(pss_sha256_signature)}))), so nothing here depends on the real
bundled mac_address_pub_key. The public key file path is redirected via
monkeypatching utils.get_bundle_path() (device.py resolves the key as
join(get_bundle_path(), "mac_address_pub_key")).
"""
import base64
import json
import logging
import types

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import device


def _generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _write_pub_key(tmp_path, public_key, filename="mac_address_pub_key"):
    pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_path = tmp_path / filename
    key_path.write_bytes(pem)
    return key_path


def _sign_mac(private_key, mac_address):
    return private_key.sign(
        mac_address.encode("utf8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )


def _build_signature_config(mac_address, signature_bytes):
    payload = {"payload": mac_address, "sig": base64.b64encode(signature_bytes).decode("ascii")}
    return base64.b64encode(json.dumps(payload).encode("utf8")).decode("ascii")


@pytest.fixture
def logger():
    log = logging.getLogger("test-device")
    log.addHandler(logging.NullHandler())
    return log


@pytest.fixture(autouse=True)
def _redirect_bundle_path(tmp_path, monkeypatch):
    """Point device.py's key lookup at a throwaway directory instead of the repo cwd."""
    monkeypatch.setattr(device, "get_bundle_path", lambda: str(tmp_path))
    return tmp_path


class TestIsMacAddressSignatureValid:
    def test_valid_signature_returns_true(self, tmp_path):
        private_key, public_key = _generate_keypair()
        _write_pub_key(tmp_path, public_key)
        mac = "aa:bb:cc:dd:ee:ff"
        signature = _sign_mac(private_key, mac)

        assert device.is_mac_address_signature_valid(signature, mac) is True

    def test_signature_for_different_mac_is_invalid(self, tmp_path):
        private_key, public_key = _generate_keypair()
        _write_pub_key(tmp_path, public_key)
        signature = _sign_mac(private_key, "aa:bb:cc:dd:ee:ff")

        assert device.is_mac_address_signature_valid(signature, "11:22:33:44:55:66") is False

    def test_signature_from_wrong_private_key_is_invalid(self, tmp_path):
        signing_key, _ = _generate_keypair()
        _, verifying_pub_key = _generate_keypair()  # unrelated keypair's public half
        _write_pub_key(tmp_path, verifying_pub_key)
        mac = "aa:bb:cc:dd:ee:ff"
        signature = _sign_mac(signing_key, mac)

        assert device.is_mac_address_signature_valid(signature, mac) is False

    def test_garbage_signature_bytes_are_invalid(self, tmp_path):
        _, public_key = _generate_keypair()
        _write_pub_key(tmp_path, public_key)

        assert device.is_mac_address_signature_valid(b"not-a-real-signature", "aa:bb:cc:dd:ee:ff") is False

    def test_missing_pub_key_file_is_invalid(self, tmp_path):
        # No mac_address_pub_key written at all in tmp_path.
        assert device.is_mac_address_signature_valid(b"whatever", "aa:bb:cc:dd:ee:ff") is False


class TestDeviceIsAuthorized:
    def test_authorized_when_local_mac_matches_valid_signature(self, tmp_path, logger, monkeypatch):
        private_key, public_key = _generate_keypair()
        _write_pub_key(tmp_path, public_key)
        mac = "aa:bb:cc:dd:ee:ff"
        signature = _sign_mac(private_key, mac)
        config_value = _build_signature_config(mac, signature)

        monkeypatch.setattr(device, "get_net_if_mac_addresses", lambda: iter([("eth0", mac)]))

        dev = device.Device(mac_address_signature=config_value, logger=logger)

        assert dev.is_authorized() is True
        assert dev.mac_address == mac

    def test_unauthorized_when_no_local_interface_matches(self, tmp_path, logger, monkeypatch):
        private_key, public_key = _generate_keypair()
        _write_pub_key(tmp_path, public_key)
        mac = "aa:bb:cc:dd:ee:ff"
        signature = _sign_mac(private_key, mac)
        config_value = _build_signature_config(mac, signature)

        monkeypatch.setattr(device, "get_net_if_mac_addresses", lambda: iter([("eth0", "11:22:33:44:55:66")]))

        dev = device.Device(mac_address_signature=config_value, logger=logger)

        assert dev.is_authorized() is False
        assert dev.mac_address is None

    def test_unauthorized_when_signature_is_invalid(self, tmp_path, logger, monkeypatch):
        signing_key, _ = _generate_keypair()
        _, unrelated_pub_key = _generate_keypair()
        _write_pub_key(tmp_path, unrelated_pub_key)
        mac = "aa:bb:cc:dd:ee:ff"
        signature = _sign_mac(signing_key, mac)
        config_value = _build_signature_config(mac, signature)

        monkeypatch.setattr(device, "get_net_if_mac_addresses", lambda: iter([("eth0", mac)]))

        dev = device.Device(mac_address_signature=config_value, logger=logger)

        assert dev.is_authorized() is False

    def test_missing_signature_is_authorized_by_backward_compat(self, logger):
        """Documents CURRENT (documented as tech debt) behaviour: a config with
        no mac_address_signature key at all is authorized, to avoid breaking
        connectors deployed before this validation existed. See
        CLAUDE.md "Known Issues" #7 and device.py:67-70 (`is_authorized`).
        This is intentionally NOT a bug fix target here.
        """
        dev = device.Device(mac_address_signature=None, logger=logger)

        assert dev.is_authorized() is True
        assert dev.mac_address is None

    def test_empty_string_signature_is_also_authorized_by_backward_compat(self, logger):
        dev = device.Device(mac_address_signature="", logger=logger)

        assert dev.is_authorized() is True

    def test_malformed_signature_payload_is_unauthorized(self, logger):
        dev = device.Device(mac_address_signature=base64.b64encode(b"not json at all").decode("ascii"),
                             logger=logger)

        assert dev.is_authorized() is False
        assert dev.mac_address is None
