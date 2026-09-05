"""Small, isolated helpers for the official Arcus Ed25519 schemes.

Arcus has two deliberately different signing formats. Orders sign the typed,
integer-valued payload itself; the legacy operations prepend a decimal
nanosecond timestamp and action to canonical JSON. Keeping those byte
construction rules here prevents the engine from knowing anything about Arcus
authentication.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def canonical_json(value: Any) -> bytes:
    """Return Arcus compact, recursively key-sorted JSON bytes."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("value cannot be encoded as canonical JSON") from exc


def _ordersign_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("ordersign payload must be a mapping")
    normalized = dict(payload)
    if "ad" in normalized:
        if not isinstance(normalized["ad"], str):
            raise ValueError("ordersign address must be a string")
        normalized["ad"] = normalized["ad"].lower()
    for field in ("c", "id"):
        if normalized.get(field) in (None, ""):
            normalized.pop(field, None)
    return normalized


def _timestamp(timestamp_ns: int) -> int:
    if isinstance(timestamp_ns, bool) or not isinstance(timestamp_ns, int):
        raise ValueError("Arcus timestamp must be an integer nanosecond epoch")
    if timestamp_ns <= 0:
        raise ValueError("Arcus timestamp must be positive")
    return timestamp_ns


def _private_key(value: str) -> ed25519.Ed25519PrivateKey:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ARCUS_API_PRIVATE_KEY is required")
    raw = value.strip().replace("\\n", "\n")
    try:
        if raw.startswith("-----BEGIN"):
            key = serialization.load_pem_private_key(
                raw.encode("utf-8"), password=None)
            if not isinstance(key, ed25519.Ed25519PrivateKey):
                raise ValueError("PEM key is not Ed25519")
            return key
        hex_value = raw[2:] if raw.lower().startswith("0x") else raw
        if len(hex_value) != 64 or not _HEX_RE.fullmatch(hex_value):
            raise ValueError("key is not a 32-byte seed hex value")
        return ed25519.Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(hex_value))
    except ValueError as exc:
        # Never include the input value: this error is allowed to reach a
        # command-line caller without leaking a secret into logs.
        raise ValueError("ARCUS_API_PRIVATE_KEY is not a valid Ed25519 key") \
            from exc


def _public_key_hex(key: ed25519.Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw).hex()


class ArcusSigner:
    """Ed25519 signer for one registered Arcus API key."""

    __slots__ = ("api_key", "_private_key")

    def __init__(self, api_key: str,
                 private_key: ed25519.Ed25519PrivateKey) -> None:
        self.api_key = api_key
        self._private_key = private_key

    @classmethod
    def from_private_key(cls, api_key: str, private_key: str) -> "ArcusSigner":
        if not isinstance(api_key, str):
            raise ValueError("ARCUS_API_KEY is required")
        normalized_api_key = api_key.strip().lower()
        if (len(normalized_api_key) != 64
                or not _HEX_RE.fullmatch(normalized_api_key)):
            raise ValueError(
                "ARCUS_API_KEY must be a 32-byte Ed25519 public-key hex value")
        key = _private_key(private_key)
        if _public_key_hex(key) != normalized_api_key:
            raise ValueError("ARCUS_API_KEY does not match private key")
        return cls(normalized_api_key, key)

    def __repr__(self) -> str:
        return f"ArcusSigner(api_key={self.api_key!r})"

    def scheme1_bytes(self, payload: Mapping[str, Any]) -> bytes:
        """Build Scheme 1 ordersign bytes, including official normalization."""
        return canonical_json(_ordersign_payload(payload))

    def sign_scheme1(self, payload: Mapping[str, Any]) -> str:
        return self._private_key.sign(self.scheme1_bytes(payload)).hex()

    def scheme2_bytes(self, timestamp_ns: int, action: str,
                      body: Mapping[str, Any]) -> bytes:
        timestamp = _timestamp(timestamp_ns)
        if not isinstance(action, str) or not action:
            raise ValueError("Arcus signing action is required")
        return (str(timestamp).encode("ascii") + action.encode("utf-8")
                + canonical_json(body))

    def sign_scheme2(self, timestamp_ns: int, action: str,
                     body: Mapping[str, Any]) -> str:
        return self._private_key.sign(
            self.scheme2_bytes(timestamp_ns, action, body)).hex()

    def headers(self, timestamp_ns: int, signature: str) -> dict[str, str]:
        timestamp = _timestamp(timestamp_ns)
        if (not isinstance(signature, str) or len(signature) != 128
                or not _HEX_RE.fullmatch(signature)):
            raise ValueError("Arcus signature must be 128 hexadecimal characters")
        return {
            "X-API-Key": self.api_key,
            "X-Timestamp": str(timestamp),
            "X-Signature": signature.lower(),
        }

