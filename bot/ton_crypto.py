"""TON wallet cryptography — key derivation, address generation, stateInit.

Uses tonsdk for correct v4R2 wallet derivation.
"""

import base64

from tonsdk.contract.wallet import WalletV4ContractR2
from tonsdk.crypto import mnemonic_to_wallet_key


def derive_keypair(mnemonic_words: list[str]) -> tuple[bytes, bytes]:
    """Derive Ed25519 keypair from 24-word mnemonic.

    Returns (private_key, public_key).
    """
    pub_key, priv_key = mnemonic_to_wallet_key(mnemonic_words)
    return priv_key, pub_key


def get_wallet_address(public_key: bytes, private_key: bytes = b"") -> str:
    """Generate v4R2 wallet address from keys (raw format 0:hex)."""
    if not private_key:
        private_key = b"\x00" * 32
    wallet = WalletV4ContractR2(public_key=public_key, private_key=private_key)
    return f"0:{wallet.address.hash_part.hex()}"


def create_state_init(public_key: bytes, private_key: bytes = b"") -> str:
    """Create base64-encoded stateInit BOC for TON Connect."""
    if not private_key:
        private_key = b"\x00" * 32
    wallet = WalletV4ContractR2(public_key=public_key, private_key=private_key)
    state_init = wallet.create_state_init()
    si_cell = state_init["state_init"]
    si_boc = bytes(si_cell.to_boc())
    return base64.b64encode(si_boc).decode()
