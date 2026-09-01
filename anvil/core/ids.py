"""Typed, sortable, human-legible identifiers.

Every id is ``<prefix>_<26-char Crockford base32 ULID>``. The prefix makes a raw
id self-describing in a log line or a support ticket; the ULID body sorts
lexicographically by creation time, which keeps Postgres index locality good
and makes ``ORDER BY id`` a valid chronological ordering.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Final

_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32, no I L O U
_ENCODE_LEN: Final = 26


class IdPrefix:
    MERCHANT = "mch"
    CUSTOMER = "cus"
    SUBSCRIPTION = "sub"
    MANDATE = "mdt"
    AUTHORISATION = "aut"
    CASE = "cse"
    ACTION = "act"
    ATTEMPT = "atm"
    LEDGER_TXN = "ltx"
    LEDGER_ENTRY = "len"
    ACCOUNT = "acc"
    RESERVATION = "rsv"
    POLICY_BUNDLE = "pol"
    POLICY_RULE = "prl"
    APPROVAL = "apr"
    STEP_UP = "stp"
    MESSAGE = "msg"
    CONSENT = "cnt"
    ERASURE = "ers"
    EVENT = "evt"
    AUDIT = "adt"
    WEBHOOK = "whk"
    BATCH = "bat"
    EXPERIMENT = "exp"
    LLM_CALL = "llm"
    IDEMPOTENCY = "idm"


def _ulid(now_ms: int | None = None, randomness: bytes | None = None) -> str:
    """Encode a 48-bit timestamp plus 80 bits of randomness as base32."""
    ms = now_ms if now_ms is not None else int(time.time() * 1000)
    rand = randomness if randomness is not None else secrets.token_bytes(10)
    value = (ms << 80) | int.from_bytes(rand, "big")
    out = [""] * _ENCODE_LEN
    for i in range(_ENCODE_LEN - 1, -1, -1):
        out[i] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(out)


def new_id(prefix: str) -> str:
    """Mint a fresh prefixed id."""
    return f"{prefix}_{_ulid()}"


def deterministic_id(prefix: str, *parts: str) -> str:
    """A stable id derived from its inputs.

    Used where an id must be reproducible across runs -- seeded simulator
    entities and experiment assignment -- so a rerun with the same seed yields
    a byte-identical database.
    """
    import hashlib

    digest = hashlib.blake2b("\x1f".join(parts).encode(), digest_size=16).digest()
    value = int.from_bytes(digest, "big")
    out = [""] * _ENCODE_LEN
    for i in range(_ENCODE_LEN - 1, -1, -1):
        out[i] = _ALPHABET[value & 0x1F]
        value >>= 5
    return f"{prefix}_{''.join(out)}"


def prefix_of(identifier: str) -> str:
    return identifier.split("_", 1)[0]


def is_valid(identifier: str, expected_prefix: str | None = None) -> bool:
    parts = identifier.split("_", 1)
    if len(parts) != 2:
        return False
    prefix, body = parts
    if expected_prefix is not None and prefix != expected_prefix:
        return False
    return len(body) == _ENCODE_LEN and all(c in _ALPHABET for c in body)


def idempotency_key(*parts: str) -> str:
    """A caller-generated key that is stable across retries of one logical action.

    Invariant 5: the key must depend only on the *intent*, never on the attempt.
    Two retries of the same logical debit therefore produce the same key, and
    Razorpay collapses them.
    """
    import hashlib

    if not parts or any(not p for p in parts):
        raise ValueError("idempotency_key needs non-empty parts")
    digest = hashlib.blake2b("\x1f".join(parts).encode(), digest_size=16).hexdigest()
    return f"anvil_{digest}"


def request_id() -> str:
    return os.urandom(8).hex()
