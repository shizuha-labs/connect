#!/usr/bin/env python3
"""CON-300 regression: connect_get_meeting must render scheduled start/end.

The live meeting detail serializer exposes `scheduled_start` / `scheduled_end`
(not `start_time` / `end_time`). The MCP adapter read the wrong keys, so every
detail response rendered `Start: N/A` / `End: N/A` even for scheduled meetings.
These tests lock the adapter to the serializer contract.
"""

import importlib.util
import os
from pathlib import Path

os.environ.setdefault("SHIZUHA_CONNECT_URL", "http://connect.test")

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("connect_server", ROOT / "connect_server.py")
connect_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connect_server)


def _get_meeting_with(meeting_payload):
    calls = []

    def fake_api_request(method, path, params=None, data=None):
        calls.append((method, path, params, data))
        return meeting_payload

    old = connect_server.base.api_request
    connect_server.base.api_request = fake_api_request
    try:
        result = connect_server.get_meeting(meeting_id="e1a27e24-5805-4838-89bc-d0c262b84d08")
    finally:
        connect_server.base.api_request = old

    assert calls == [("GET", "/meetings/e1a27e24-5805-4838-89bc-d0c262b84d08/", None, None)]
    return result


def test_renders_scheduled_start_and_end():
    out = _get_meeting_with(
        {
            "title": "CON-298 QA lifecycle",
            "scheduled_start": "2026-07-29T03:03:00Z",
            "scheduled_end": "2026-07-29T03:18:00Z",
            "status": "active",
            "id": "e1a27e24-5805-4838-89bc-d0c262b84d08",
        }
    )
    assert "**Start**: 2026-07-29T03:03:00Z" in out
    assert "**End**: 2026-07-29T03:18:00Z" in out
    assert "**Status**: active" in out


def test_renders_legacy_start_time_field_as_fallback():
    # Defensive: if a payload only carries start_time/end_time, still render it.
    out = _get_meeting_with(
        {
            "title": "legacy",
            "start_time": "2026-07-29T03:03:00Z",
            "end_time": "2026-07-29T03:18:00Z",
            "status": "active",
            "id": "m-1",
        }
    )
    assert "**Start**: 2026-07-29T03:03:00Z" in out
    assert "**End**: 2026-07-29T03:18:00Z" in out


def test_renders_na_only_when_no_timestamps_present():
    out = _get_meeting_with({"title": "instant", "status": "active", "id": "m-2"})
    assert "**Start**: N/A" in out
    assert "**End**: N/A" in out
