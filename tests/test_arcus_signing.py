"""Deterministic Arcus signing vectors from the official auth contract."""
import os
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.arcus_signing import ArcusSigner, canonical_json  # noqa: E402


# RFC 8032 test seed; this is a public deterministic vector, not a credential.
PRIVATE_KEY = (
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
API_KEY = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
ADDRESS = "0xAbCdEfAbCdEfAbCdEfAbCdEfAbCdEfAbCdEfAbCd"
TIMESTAMP_NS = 1700000000000000000


def signer():
    return ArcusSigner.from_private_key(API_KEY, PRIVATE_KEY)


def test_scheme1_canonical_bytes_and_exact_signature():
    payload = {
        "ad": ADDRESS,
        "ai": 2,
        "c": "Client_X-7",
        "ct": TIMESTAMP_NS,
        "g": 1700000000000000000000000,
        "m": 33,
        "op": 1,
        "p": 176358,
        "q": 100000,
        "r": 0,
        "s": 0,
        "t": 2,
        "v": 1,
    }
    expected = (
        '{"ad":"0xabcdefabcdefabcdefabcdefabcdefabcdefabcd","ai":2,'
        '"c":"Client_X-7","ct":1700000000000000000,'
        '"g":1700000000000000000000000,"m":33,"op":1,"p":176358,'
        '"q":100000,"r":0,"s":0,"t":2,"v":1}'
    )
    assert signer().scheme1_bytes(payload).decode() == expected
    assert signer().sign_scheme1(payload) == (
        "c9758184b9690f23c4c32e0136799b33015af9ecbf5838435e0501a2b9ab4359"
        "f86e629b459823a01394a61acc1331d94c1c17cf2d59bf3f61ef3d837bcc5304"
    )


def test_scheme1_omits_empty_client_id_but_preserves_other_string_case():
    payload = {
        "ad": ADDRESS,
        "ai": 0,
        "c": "",
        "ct": TIMESTAMP_NS,
        "m": 33,
        "id": "Order-Mixed-Case",
        "op": 2,
        "v": 1,
    }
    raw = signer().scheme1_bytes(payload).decode()
    assert '"c"' not in raw
    assert '"id":"Order-Mixed-Case"' in raw
    assert '"ad":"0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"' in raw


def test_scheme2_canonical_bytes_and_exact_signature():
    body = {
        "accountIndex": 2,
        "address": ADDRESS.lower(),
        "channel": "orders",
    }
    expected = (
        '1700000000000000000authenticate{"accountIndex":2,'
        '"address":"0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",'
        '"channel":"orders"}'
    )
    assert signer().scheme2_bytes(TIMESTAMP_NS, "authenticate", body).decode() \
        == expected
    assert signer().sign_scheme2(TIMESTAMP_NS, "authenticate", body) == (
        "a7d5077012338c56b80960f347995af1849ea1544747ce1a0410a2eaedb780e3"
        "cabca57d1d060a245c1d3730f6132deb3bca683b44b19a9ad12c384b0f832a0f"
    )


def test_headers_are_exact_and_use_decimal_nanoseconds():
    signature = signer().sign_scheme2(TIMESTAMP_NS, "authenticate", {})
    assert signer().headers(TIMESTAMP_NS, signature) == {
        "X-API-Key": API_KEY,
        "X-Timestamp": str(TIMESTAMP_NS),
        "X-Signature": signature,
    }


def test_canonical_json_is_compact_sorted_and_rejects_nan():
    assert canonical_json({"z": 1, "a": {"d": 2, "c": 3}}) == (
        b'{"a":{"c":3,"d":2},"z":1}'
    )
    with pytest.raises(ValueError):
        canonical_json({"bad": float("nan")})


def test_private_key_mismatch_and_invalid_key_do_not_echo_secret():
    with pytest.raises(ValueError, match="does not match") as mismatch:
        ArcusSigner.from_private_key("00" * 32, PRIVATE_KEY)
    assert PRIVATE_KEY not in str(mismatch.value)
    with pytest.raises(ValueError) as invalid:
        ArcusSigner.from_private_key(API_KEY, "not-a-private-key")
    assert "not-a-private-key" not in str(invalid.value)


def test_signer_repr_does_not_contain_private_key():
    assert PRIVATE_KEY not in repr(signer())


def test_pem_private_key_is_supported_without_echoing_material():
    key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(PRIVATE_KEY))
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    loaded = ArcusSigner.from_private_key(API_KEY, pem)
    assert loaded.sign_scheme1({"ad": ADDRESS, "op": 1})
    assert PRIVATE_KEY not in repr(loaded)
