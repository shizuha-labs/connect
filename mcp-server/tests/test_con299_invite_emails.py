#!/usr/bin/env python3
"""CON-299 regression: connect_invite_to_meeting must forward `emails`.

The live `/meetings/<id>/invite/` endpoint requires a non-empty `emails`
list (MeetingInviteCreateSerializer). The MCP adapter used to send
`user_ids`, so every documented call hit HTTP 400 `{"emails": ["This field
is required."]}`. These tests lock the adapter to the backend contract:
accept and forward comma-separated email addresses as `emails`.
"""

import importlib.util
import os
from pathlib import Path

os.environ.setdefault("SHIZUHA_CONNECT_URL", "http://connect.test")

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("connect_server", ROOT / "connect_server.py")
connect_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connect_server)


def _capture_invite_call(emails, *, expected_payload):
    calls = []

    def fake_api_request(method, path, params=None, data=None):
        calls.append((method, path, params, data))
        return {"status": "invites_sent", "count": 1}

    old = connect_server.base.api_request
    connect_server.base.api_request = fake_api_request
    try:
        result = connect_server.invite_to_meeting(
            meeting_id="e1a27e24-5805-4838-89bc-d0c262b84d08",
            emails=emails,
        )
    finally:
        connect_server.base.api_request = old

    assert "Invited" in result
    assert calls == [
        (
            "POST",
            "/meetings/e1a27e24-5805-4838-89bc-d0c262b84d08/invite/",
            None,
            expected_payload,
        )
    ]


def test_invite_forwards_emails_to_connect_api():
    _capture_invite_call(
        "qa.participant@example.com",
        expected_payload={"emails": ["qa.participant@example.com"]},
    )


def test_invite_forwards_multiple_emails_and_strips_whitespace():
    _capture_invite_call(
        " one@example.com, two@example.com ",
        expected_payload={"emails": ["one@example.com", "two@example.com"]},
    )


def test_invite_skips_empty_entries():
    _capture_invite_call(
        "one@example.com,,two@example.com",
        expected_payload={"emails": ["one@example.com", "two@example.com"]},
    )


def test_invite_rejects_no_emails_without_calling_api():
    calls = []

    def fake_api_request(method, path, params=None, data=None):
        calls.append((method, path, params, data))
        return {}

    old = connect_server.base.api_request
    connect_server.base.api_request = fake_api_request
    try:
        result = connect_server.invite_to_meeting(
            meeting_id="m-1",
            emails="  ,  ",
        )
    finally:
        connect_server.base.api_request = old

    assert "at least one email" in result
    assert calls == []
