"""CON-342 regression: profile-posts username resolution.

- connect_search must expose the canonical username for people results.
- connect_get_profile_posts must return a clear typed 404 for an unknown user
  (not an ambiguous empty list or a raw HTML error page).

Run: `python mcp-server/test_con342_profile_resolution.py` (or under pytest).
"""
import os
from unittest import mock

os.environ.setdefault("SHIZUHA_CONNECT_URL", "http://localhost:8019/connect/api")

import connect_server  # noqa: E402
from base import ShizuhaMCPBase  # noqa: E402


def _ok(payload):
    return payload


def test_search_people_exposes_username():
    result = {
        "posts": [],
        "people": [
            {"id": "d71678fa", "display_name": "Nagi Shizuha", "username": "nagi.shizuha"},
        ],
        "jobs": [],
        "companies": [],
    }
    with mock.patch.object(connect_server.base, "api_request", return_value=result):
        out = connect_server.search_global("Shizuha Drive")
    assert "@nagi.shizuha" in out, out


def test_search_people_omits_username_when_absent():
    result = {
        "posts": [],
        "people": [{"id": "abc", "display_name": "No Handle"}],
        "jobs": [],
        "companies": [],
    }
    with mock.patch.object(connect_server.base, "api_request", return_value=result):
        out = connect_server.search_global("No Handle")
    assert "@" not in out, out


def test_get_profile_posts_unknown_user_is_typed_404():
    err = {"error": "404 Client Error", "detail": {"detail": "Not found."}, "status_code": 404}
    with mock.patch.object(connect_server.base, "api_request", return_value=err):
        out = connect_server.get_profile_posts("no.such.user")
    assert "not found" in out.lower(), out
    assert "HTTP 404" in out, out


def test_get_profile_posts_known_user_no_posts_is_empty_not_error():
    with mock.patch.object(connect_server.base, "api_request", return_value={"results": []}):
        out = connect_server.get_profile_posts("nagi")
    assert "no posts" in out.lower(), out


if __name__ == "__main__":
    test_search_people_exposes_username()
    test_search_people_omits_username_when_absent()
    test_get_profile_posts_unknown_user_is_typed_404()
    test_get_profile_posts_known_user_no_posts_is_empty_not_error()
    print("CON-342 MCP regression tests OK")
