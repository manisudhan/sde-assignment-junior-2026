"""PII protection at rest.

Two different primitives, because they answer two different questions:

* **Blind index** - a keyed HMAC-SHA256 of the canonical phone number. It is
  deterministic, so it can be a UNIQUE key and a join key: events still find
  their lead by phone without the phone ever being stored in plaintext.
  Unkeyed hashing would be useless here - the 10-digit Indian mobile space is
  ~4e9 wide, so a plain SHA-256 is brute-forceable in seconds.
* **AES-256-GCM** - randomised authenticated encryption for the values we must
  be able to read back (the phone we would dial, the name the voice agent
  greets). Randomised means two leads with the same name get different
  ciphertext, so the column leaks nothing; it also means you cannot search it,
  which is exactly why the blind index exists.

Both keys are derived from one master key with HKDF, so a single
``LEDGER_PII_KEY`` rotates everything and the two uses never share key material.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ENV_KEY = "LEDGER_PII_KEY"

#: Used when ``LEDGER_PII_KEY`` is unset so that a clean clone runs out of the
#: box. The blind index must be stable across runs or re-ingest would create
#: duplicate leads, so we cannot generate a random key per run.
DEV_FALLBACK_KEY = b"assignment-a-development-key-not-for-production"

_HKDF_SALT = b"agentic-universe/ledger/v1"
_INFO_BLIND_INDEX = b"phone-blind-index"
_INFO_FIELD_ENC = b"field-encryption"

_NONCE_LEN = 12  # GCM standard nonce length
_warned = False


def load_master_key() -> bytes:
    """Read the master key from the environment, or fall back to the dev key."""
    global _warned
    raw = os.environ.get(ENV_KEY)
    if raw:
        return raw.encode("utf-8")
    if not _warned:
        print(
            f"[warn] {ENV_KEY} is not set; using the committed development key. "
            "Set a real key before using this on anything but synthetic data.",
            file=sys.stderr,
        )
        _warned = True
    return DEV_FALLBACK_KEY


def _derive(master: bytes, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=_HKDF_SALT, info=info
    ).derive(master)


class PiiCipher:
    """Blind-index + authenticated-encryption helper for PII fields."""

    def __init__(self, master_key: bytes | None = None) -> None:
        master = master_key if master_key is not None else load_master_key()
        self._index_key = _derive(master, _INFO_BLIND_INDEX)
        self._aead = AESGCM(_derive(master, _INFO_FIELD_ENC))
        # Identifies the key without revealing it, so the database can refuse to
        # be opened with the wrong one (see db.assert_key_matches).
        self.fingerprint = hashlib.sha256(_HKDF_SALT + master).hexdigest()[:16]

    def blind_index(self, value: str) -> str:
        """Deterministic, keyed, searchable token for ``value``."""
        return hmac.new(self._index_key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def encrypt(self, plaintext: str | None) -> bytes | None:
        if plaintext is None:
            return None
        nonce = os.urandom(_NONCE_LEN)
        return nonce + self._aead.encrypt(nonce, plaintext.encode("utf-8"), None)

    def decrypt(self, blob: bytes | None) -> str | None:
        if blob is None:
            return None
        return self._aead.decrypt(blob[:_NONCE_LEN], blob[_NONCE_LEN:], None).decode("utf-8")
