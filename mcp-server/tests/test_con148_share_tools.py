#!/usr/bin/env python3
"""CON-148 regression checks for Connect cross-service share MCP tools.

The Connect `/share/*/` API creates a feed post for an existing object from
another Shizuha service.  The MCP adapter used to send `content_type` and
`content_id` only, so every tool hit the backend's raw `<target>_id is required`
400.  These tests lock the adapter to the backend contract: accept and forward
the concrete target id, and fail locally with a helpful message when legacy
source-content args are supplied without one.
"""

import importlib.util
import os
from pathlib import Path

os.environ.setdefault("SHIZUHA_CONNECT_URL", "http://connect.test")

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("connect_server", ROOT / "connect_server.py")
connect_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connect_server)


def _capture_share_call(func, *, expected_path, expected_payload, **kwargs):
    calls = []

    def fake_api_request(method, path, params=None, data=None):
        calls.append((method, path, params, data))
        return {"id": "post-123"}

    old = connect_server.base.api_request
    connect_server.base.api_request = fake_api_request
    try:
        result = func(**kwargs)
    finally:
        connect_server.base.api_request = old

    assert "Shared" in result
    assert "post-123" in result
    assert calls == [("POST", expected_path, None, expected_payload)]


def test_share_drive_forwards_file_id_to_connect_api():
    _capture_share_call(
        connect_server.share_to_drive,
        file_id="file-1",
        message="see file",
        expected_path="/share/drive/",
        expected_payload={"file_id": "file-1", "message": "see file"},
    )


def test_share_pulse_forwards_task_id_to_connect_api():
    _capture_share_call(
        connect_server.share_to_pulse,
        task_id="CON-148",
        message="see task",
        expected_path="/share/pulse/",
        expected_payload={"task_id": "CON-148", "message": "see task"},
    )


def test_share_wiki_forwards_page_id_to_connect_api():
    _capture_share_call(
        connect_server.share_to_wiki,
        page_id="page-1",
        expected_path="/share/wiki/",
        expected_payload={"page_id": "page-1"},
    )


def test_share_notes_forwards_note_id_to_connect_api():
    _capture_share_call(
        connect_server.share_to_notes,
        note_id="note-1",
        expected_path="/share/notes/",
        expected_payload={"note_id": "note-1"},
    )


def test_legacy_content_args_fail_locally_without_backend_400():
    calls = []

    def fake_api_request(method, path, params=None, data=None):
        calls.append((method, path, params, data))
        raise AssertionError("should not call backend without file_id")

    old = connect_server.base.api_request
    connect_server.base.api_request = fake_api_request
    try:
        result = connect_server.share_to_drive(
            content_type="post",
            content_id="00000000-0000-0000-0000-000000000000",
        )
    finally:
        connect_server.base.api_request = old

    assert "`file_id` is required" in result
    assert "content_type`/`content_id" in result
    assert calls == []


if __name__ == "__main__":
    test_share_drive_forwards_file_id_to_connect_api()
    test_share_pulse_forwards_task_id_to_connect_api()
    test_share_wiki_forwards_page_id_to_connect_api()
    test_share_notes_forwards_note_id_to_connect_api()
    test_legacy_content_args_fail_locally_without_backend_400()
    print("CON-148 share MCP adapter tests passed")
