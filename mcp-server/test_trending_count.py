"""CON-69 regression: connect_trending_hashtags must read the serializer's
`post_count`, not only `posts_count`/`count` (which made every trending item
render `(0 posts)`).

Run: `python mcp-server/test_trending_count.py` (or under pytest).
"""
import os

# connect_server's base client requires a URL at import time.
os.environ.setdefault("SHIZUHA_CONNECT_URL", "http://localhost:8019/connect/api")

from connect_server import _trending_count  # noqa: E402


def test_reads_post_count_serializer_field():
    # TrendingTopicSerializer shape (the prod response that was rendering 0).
    assert _trending_count({"name": "con37dogfood", "post_count": 3}) == 3


def test_post_count_wins_over_legacy_keys():
    assert _trending_count({"post_count": 5, "posts_count": 99, "count": 99}) == 5


def test_legacy_posts_count_fallback():
    assert _trending_count({"posts_count": 7}) == 7


def test_legacy_count_fallback():
    assert _trending_count({"count": 2}) == 2


def test_defaults_to_zero_when_truly_absent():
    assert _trending_count({"name": "x"}) == 0


if __name__ == "__main__":
    test_reads_post_count_serializer_field()
    test_post_count_wins_over_legacy_keys()
    test_legacy_posts_count_fallback()
    test_legacy_count_fallback()
    test_defaults_to_zero_when_truly_absent()
    print("ALL CON-69 _trending_count assertions passed")
