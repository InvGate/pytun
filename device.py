import base64
import json
import os
from typing import Optional

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

from utils import get_net_if_mac_addresses, get_bundle_path

_MAC_ADDRESS_PUB_KEY_PATH = "mac_address_pub_key"


class Device:
    def __init__(self, mac_address_signature: str):
        self._authorized_mac_address: Optional[str] = self._get_authorized_mac_address()
        self._mac_address_signature = mac_address_signature

    @property
    def mac_address(self):
        return self._authorized_mac_address

    def _get_authorized_mac_address(self) -> Optional[str]:
        try:
            mac_addr_json = json.loads(base64.b64decode(self._mac_address_signature))
            mac_addr = mac_addr_json["payload"]
            mac_addr_sig = base64.b64decode(mac_addr_json["sig"])

            # check that device has a network interface with a MAC address that matches the one from the config
            if all(mac_addr != net_if_mac[1] for net_if_mac in get_net_if_mac_addresses()):
                return None

            if not is_mac_address_signature_valid(mac_address_signature=mac_addr_sig, mac_address=mac_addr):
                return None
        except Exception:
            return None

        return mac_addr

    def is_authorized(self):
        """
        Validates that the device the connector is running on is "authorized".

        A device is considered authorized if it has a network interface with a MAC address that matches
        the MAC address specified in the params dict with a valid signature.
        For now, to not break running connectors without the config key used to validate the MAC address, a device is
        also considered authorized if it does not have the MAC address key in the params dict

        This is meant to prevent users from running the connector in devices where it is not supposed to. It is not
        meant to prevent "malicious" users from doing it as MAC cloning is possible, making this validation useless.

        :return: Is device authorized
        """

        # TO BE REMOVED:
        # Authorize connectors without the MAC address config key to not break running connectors without it
        if not self._mac_address_signature:
            return True

        return self._get_authorized_mac_address() is not None


def is_mac_address_signature_valid(mac_address_signature: bytes, mac_address: str):
    """
    Check if a signature is valid for a given MAC address

    :param mac_address_signature: Signature of the MAC address
    :param mac_address: MAC address that the signature should match
    :return: Is MAC address signature valid
    """
    try:
        with open(os.path.join(get_bundle_path(), _MAC_ADDRESS_PUB_KEY_PATH), "rb") as pub_key_file:
            pubkey = serialization.load_pem_public_key(pub_key_file.read(), backend=default_backend())

        pubkey.verify(
            mac_address_signature,
            mac_address.encode("utf8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
    except Exception:
        return False

    return True
