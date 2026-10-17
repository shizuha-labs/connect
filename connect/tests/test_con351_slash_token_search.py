"""CON-351: slash/hyphen-joined product tokens must be individually searchable.

``Tally/Zoho/CSV import`` was not discoverable via ``Tally`` / ``Zoho`` /
``CSV`` because the full-text search vector tokenized the joined string as one
token. The signal now normalizes separators to spaces before building the
vector, so each product token is indexed.
"""
from django.test import TestCase

from connect.models import Post
from connect.signals import _normalize_search_text, _postgres_search_available


class NormalizeSearchTextTests(TestCase):
    def test_splits_slash_joined_tokens(self):
        self.assertEqual(
            _normalize_search_text('Tally/Zoho/CSV import'),
            'Tally Zoho CSV import',
        )

    def test_splits_hyphen_and_underscore(self):
        self.assertEqual(
            _normalize_search_text('GST-TDS_TCS compliance'),
            'GST TDS TCS compliance',
        )

    def test_preserves_plain_text(self):
        self.assertEqual(_normalize_search_text('Books compliance cockpit'), 'Books compliance cockpit')

    def test_empty_and_none(self):
        self.assertEqual(_normalize_search_text(''), '')
        self.assertEqual(_normalize_search_text(None), '')


class PostSearchVectorSlashTokensTests(TestCase):
    def test_slash_joined_product_tokens_are_searchable(self):
        if not _postgres_search_available():
            self.skipTest('PostgreSQL full-text search not available')
        from django.contrib.postgres.search import SearchQuery

        post = Post.objects.create(
            author_id=1,
            author_email='fumi@x.com',
            content='Books Compliance Cockpit supports Tally/Zoho/CSV import and GST/TDS/TCS.',
        )
        # The post_save signal populates search_vector from normalized content.
        post.refresh_from_db()
        for token in ('Tally', 'Zoho', 'CSV', 'GST', 'TDS', 'TCS'):
            found = Post.objects.filter(
                pk=post.pk, search_vector=SearchQuery(token)
            ).exists()
            self.assertTrue(found, f'token {token!r} should be searchable')
