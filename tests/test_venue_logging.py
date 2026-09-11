"""Venue-native execution reasons retained for trade research."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.venue_hl import HLVenue  # noqa: E402


def test_hyperliquid_ioc_cancel_preserves_native_reason():
    body = {
        "status": "ok",
        "response": {"data": {"statuses": [
            {"error": "could not immediately match"},
        ]}},
    }

    result = HLVenue._parse(body)

    assert result["status"] == "canceled"
    assert result["reason"] == "could not immediately match"
    assert result["err"] is None
