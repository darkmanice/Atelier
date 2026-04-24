"""
AES-GCM authenticated encryption for API keys stored on disk.

- Master key in `MASTER_ENCRYPTION_KEY` (32 bytes hex = 64 chars). Without it,
  the provider endpoints fail with 503; the one-shot flow still works.
- Random 12-byte nonce per encryption (recommended by AES-GCM).
- JSON-serializable output: {"nonce": b64, "ciphertext": b64}.

If the master key is lost or rotated, existing ciphertexts become
unreadable. This is intentional: it is the property we want.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CryptoError(RuntimeError):
    pass


@dataclass(frozen=True)
class SealedSecret:
    nonce_b64: str
    ciphertext_b64: str

    def to_dict(self) -> dict:
        return {"nonce": self.nonce_b64, "ciphertext": self.ciphertext_b64}

    @classmethod
    def from_dict(cls, data: dict) -> "SealedSecret":
        return cls(nonce_b64=data["nonce"], ciphertext_b64=data["ciphertext"])


def _load_master_key() -> bytes | None:
    raw = os.environ.get("MASTER_ENCRYPTION_KEY", "").strip()
    if not raw:
        return None
    try:
        key = bytes.fromhex(raw)
    except ValueError as e:
        raise CryptoError("MASTER_ENCRYPTION_KEY is not valid hex") from e
    if len(key) != 32:
        raise CryptoError(
            f"MASTER_ENCRYPTION_KEY must decode to 32 bytes (got {len(key)})"
        )
    return key


def is_available() -> bool:
    try:
        return _load_master_key() is not None
    except CryptoError:
        return False


def seal(plaintext: str) -> SealedSecret:
    key = _load_master_key()
    if key is None:
        raise CryptoError("MASTER_ENCRYPTION_KEY not configured")
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    return SealedSecret(
        nonce_b64=base64.b64encode(nonce).decode("ascii"),
        ciphertext_b64=base64.b64encode(ct).decode("ascii"),
    )


def open_(sealed: SealedSecret) -> str:
    key = _load_master_key()
    if key is None:
        raise CryptoError("MASTER_ENCRYPTION_KEY not configured")
    try:
        nonce = base64.b64decode(sealed.nonce_b64)
        ct = base64.b64decode(sealed.ciphertext_b64)
        pt = AESGCM(key).decrypt(nonce, ct, associated_data=None)
    except Exception as e:
        # We do not discriminate between "wrong master key" and "corrupted ciphertext"
        # so we do not give an oracle on the reason of the failure.
        raise CryptoError("Failed to decrypt secret (wrong master key or corrupted data)") from e
    return pt.decode("utf-8")
