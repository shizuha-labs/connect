"""CON-262: connect_get_my_bookmarks must format the SOURCE post (id/content/
author/timestamp), not the bookmark row — the pre-fix code passed Bookmark
objects to _fmt_post_list and rendered a ghost row (@Unknown, empty content)."""
import importlib.util
import sys
from pathlib import Path


def load_server(monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("SHIZUHA_CONNECT_URL", "http://connect.test")
    sys.path.insert(0, str(repo / "mcp-server"))
    spec = importlib.util.spec_from_file_location("connect_server_under_test", repo / "mcp-server" / "connect_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_get_my_bookmarks_formats_source_post_not_bookmark_row(monkeypatch):
    server = load_server(monkeypatch)
    bookmark_id = "6b334ff2-cc0d-4e18-9e57-c1253097cc02"
    post_id = "0caf1283-2f1f-4a3c-998b-b67bd5b1bf18"

    def fake_request(method, path, data=None, **kwargs):
        return {
            "results": [
                {
                    "id": bookmark_id,  # bookmark's own UUID
                    "created_at": "2026-07-21T18:20:11Z",  # bookmark timestamp
                    "note": "",
                    "post": {
                        "id": post_id,
                        "content": "hello from zen",
                        "author_username": "zen",
                        "created_at": "2026-07-20T10:00:00Z",
                        "reactions_count": 0,
                        "comments_count": 0,
                        "reposts_count": 0,
                    },
                }
            ]
        }

    monkeypatch.setattr(server.base, "api_request", fake_request)
    out = server.get_my_bookmarks(limit=20)

    # The source post identity/content/author must appear, not the bookmark id.
    assert "@zen" in out
    assert "hello from zen" in out
    assert post_id in out
    assert "2026-07-20T10:00:00Z" in out  # source post timestamp
    assert "Unknown" not in out
    assert bookmark_id not in out  # bookmark's own UUID must not be shown as the post id


def test_get_my_bookmarks_tolerates_flat_post_shape(monkeypatch):
    server = load_server(monkeypatch)
    post_id = "0caf1283-2f1f-4a3c-998b-b67bd5b1bf18"

    def fake_request(method, path, data=None, **kwargs):
        # Older/other endpoints may already return the post shape directly.
        return {"results": [{"id": post_id, "content": "flat", "author_username": "zen"}]}

    monkeypatch.setattr(server.base, "api_request", fake_request)
    out = server.get_my_bookmarks(limit=20)
    assert "@zen" in out
    assert "flat" in out
    assert post_id in out
