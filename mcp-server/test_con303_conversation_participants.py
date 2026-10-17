"""CON-303 regression: connect_get_conversation must render participant
identities. ConversationParticipantSerializer exposes user_name (and
user_email), not display_name/username — the detail tool previously rendered
every participant blank.

Run: `python mcp-server/test_con303_conversation_participants.py`.
"""
import os
from unittest import mock

os.environ.setdefault("SHIZUHA_CONNECT_URL", "http://localhost:8019/connect/api")

import connect_server  # noqa: E402


def test_get_conversation_renders_participant_user_names():
    payload = {
        "id": "c7a99b4e",
        "name": "Zen",
        "created_at": "2026-04-20T11:39:26",
        "unread_count": 0,
        "participants": [
            {"user_id": 1, "user_name": "Mika", "user_email": "mika@example.com"},
            {"user_id": 2, "user_name": "Zen", "user_email": "zen@example.com"},
        ],
    }
    with mock.patch.object(connect_server.base, "api_request", return_value=payload):
        out = connect_server.get_conversation("c7a99b4e")
    assert "**Participants**: Mika, Zen" in out, out


def test_get_conversation_falls_back_to_email_when_name_blank():
    payload = {
        "id": "x",
        "created_at": "2026-01-01T00:00:00",
        "participants": [{"user_id": 9, "user_name": "", "user_email": "anon@example.com"}],
    }
    with mock.patch.object(connect_server.base, "api_request", return_value=payload):
        out = connect_server.get_conversation("x")
    assert "anon@example.com" in out, out


def test_get_conversation_no_participants_is_empty_not_error():
    payload = {"id": "y", "created_at": "2026-01-01T00:00:00", "participants": []}
    with mock.patch.object(connect_server.base, "api_request", return_value=payload):
        out = connect_server.get_conversation("y")
    assert "**Participants**: " in out, out


if __name__ == "__main__":
    test_get_conversation_renders_participant_user_names()
    test_get_conversation_falls_back_to_email_when_name_blank()
    test_get_conversation_no_participants_is_empty_not_error()
    print("CON-303 MCP regression tests OK")
