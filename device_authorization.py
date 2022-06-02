import base64
import json
import os

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

from utils import get_net_if_mac_addresses, get_bundle_path

_MAC_ADDRESS_CFG_KEY = "signature"
_MAC_ADDRESS_PUB_KEY_PATH = "mac_address_pub_key"


def is_device_authorized(params: dict) -> bool:
    """
    Validates that the device the connector is running on is "authorized".
    A device is considered authorized if it does not have the mac_address key in the params dict or
    if it has a network interface with a MAC address that matches the MAC address specified in the params dict
    with a valid signature.
    This is meant to prevent users from running the connector in devices where it is not supposed to. It is not meant to
    prevent "malicious" users from doing it as MAC cloning is possible, making this validation useless.

    :param params: Connector config object
    :return: Is device authorized
    """
    if not params.get(_MAC_ADDRESS_CFG_KEY):
        return True

    try:
        mac_addr_json = json.loads(base64.b64decode(params.get(_MAC_ADDRESS_CFG_KEY)))
        mac_addr = mac_addr_json["payload"]
        mac_addr_sig = base64.b64decode(mac_addr_json["sig"])

        # check that device has a network interface with a MAC address that matches the one from the config
        if all(mac_addr != net_if_mac[1] for net_if_mac in get_net_if_mac_addresses()):
            return False

        with open(os.path.join(get_bundle_path(), _MAC_ADDRESS_PUB_KEY_PATH), "rb") as pub_key_file:
            pubkey = serialization.load_pem_public_key(pub_key_file.read(), backend=default_backend())

        pubkey.verify(
            mac_addr_sig,
            mac_addr.encode("utf8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
    except Exception:
        return False

    return True
