"""
CON-102: trending hashtags use the last-7-days window + rank by author diversity.

- 7-day window (was 6h) so a sparse/new platform surfaces real recent trends
  instead of a dead panel; posts older than the window are excluded.
- distinct-author ranking so a tag many people use outranks one spammed by a
  single user (a 1-author tag is not a real "trend").
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from connect.models import Post, Hashtag, PostHashtag, TrendingTopic
from connect.tasks import calculate_trending_topics


def _post(author_id, content='hi'):
    return Post.objects.create(
        author_id=author_id, author_email=f'{author_id}@x.com', content=content)


def _tag(name):
    return Hashtag.objects.create(name=name)


def _link(post, tag, days_ago=0):
    ph = PostHashtag.objects.create(post=post, hashtag=tag)
    if days_ago:
        # created_at is auto_now_add; override via update() to age the link.
        PostHashtag.objects.filter(pk=ph.pk).update(
            created_at=timezone.now() - timedelta(days=days_ago))
    return ph


class TrendingCon102Tests(TestCase):
    def test_ranks_by_distinct_authors_over_raw_count(self):
        # 'diverse' = 2 distinct authors (2 links); 'solo' = 1 author spamming 3 links.
        diverse, solo = _tag('diverse'), _tag('solo')
        _link(_post(1), diverse)
        _link(_post(2), diverse)
        for _ in range(3):
            _link(_post(3, content='spam'), solo)  # 3 posts, all author_id=3

        calculate_trending_topics()

        ordered = list(TrendingTopic.objects.order_by('rank').values_list('name', flat=True))
        self.assertIn('diverse', ordered)
        self.assertIn('solo', ordered)
        # diversity (2 authors) outranks raw count (3 posts, 1 author)
        self.assertLess(ordered.index('diverse'), ordered.index('solo'))

    def test_seven_day_window_excludes_older_posts(self):
        recent, old = _tag('recent'), _tag('old')
        _link(_post(1), recent, days_ago=5)   # inside 7d
        _link(_post(2), recent, days_ago=5)
        _link(_post(3), old, days_ago=8)      # outside 7d
        _link(_post(4), old, days_ago=8)

        calculate_trending_topics()

        names = set(TrendingTopic.objects.values_list('name', flat=True))
        self.assertIn('recent', names)
        self.assertNotIn('old', names)
