import unittest
from datetime import timedelta

from django.db import connection
from django.test import SimpleTestCase, TestCase
from rest_framework.pagination import PageNumberPagination
from django.utils import timezone

from connect.search.services import (
    build_post_search_query,
    ranked_post_search,
    tokenize_search_query,
)
from connect.views.search import PostSearchView


class PostSearchQueryParsingTests(SimpleTestCase):
    def test_tokenize_search_query_deduplicates_and_caps_tokens(self):
        tokens = tokenize_search_query(" AI assistant, AI-agent #Connect @Scout x ")
        self.assertEqual(tokens, ("ai", "assistant", "agent", "connect", "scout"))

    def test_build_post_search_query_uses_any_token_fallback_for_multi_word_queries(self):
        parsed = build_post_search_query("AI assistant")
        self.assertEqual(parsed.tokens, ("ai", "assistant"))
        self.assertIsNotNone(parsed.broad_query)

    def test_build_post_search_query_does_not_add_broad_fallback_for_single_token(self):
        parsed = build_post_search_query("AI")
        self.assertEqual(parsed.tokens, ("ai",))
        self.assertIsNone(parsed.broad_query)


class PostSearchEndpointPaginationTests(SimpleTestCase):
    def test_post_search_pagination_preserves_ranked_queryset_order(self):
        paginator = PostSearchView.pagination_class()

        self.assertIsInstance(paginator, PageNumberPagination)
        self.assertEqual(paginator.page_size_query_param, "limit")
        self.assertFalse(
            hasattr(paginator, "ordering"),
            "CursorPagination.ordering would override ranked_post_search() ordering",
        )


if connection.vendor == "postgresql":
    from connect.models import Post

    class RankedPostSearchPostgresTests(TestCase):
        def _post(self, content, *, days_old=0, likes=0, reposts=0, visibility="public"):
            post = Post.objects.create(
                author_id=1,
                author_email="kai@shizuha.com",
                author_username="kai",
                author_display_name="Kai",
                content=content,
                visibility=visibility,
                likes_count=likes,
                reposts_count=reposts,
            )
            if days_old:
                Post.objects.filter(pk=post.pk).update(created_at=timezone.now() - timedelta(days=days_old))
            return Post.objects.get(pk=post.pk)

        def test_multi_word_query_matches_any_token_but_ranks_both_terms_first(self):
            both = self._post("Building an AI assistant for Connect search", likes=1)
            one = self._post("AI agents need better discovery", likes=50)
            self._post("Gardening update with no relevant terms", likes=100)

            ids = list(ranked_post_search("AI assistant", limit=None).values_list("id", flat=True))

            self.assertIn(both.id, ids)
            self.assertIn(one.id, ids)
            self.assertLess(ids.index(both.id), ids.index(one.id))

        def test_author_and_hashtag_filters_compose_with_ranked_search(self):
            from connect.models import Hashtag, PostHashtag

            included = self._post("AI assistant hashtag post")
            excluded_author = self._post("AI assistant from another author")
            Post.objects.filter(pk=excluded_author.pk).update(author_id=2)
            tag = Hashtag.objects.create(name="ai")
            PostHashtag.objects.create(post=included, hashtag=tag)

            ids = list(
                ranked_post_search(
                    "AI assistant", limit=None, author=1, hashtag="ai"
                ).values_list("id", flat=True)
            )

            self.assertEqual(ids, [included.id])
else:
    class RankedPostSearchPostgresTests(SimpleTestCase):
        @unittest.skip("PostgreSQL FTS integration test")
        def test_postgresql_fts_integration_coverage_marker(self):
            pass
