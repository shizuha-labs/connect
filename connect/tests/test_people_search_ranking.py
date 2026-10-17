import unittest

from django.db import connection
from django.test import SimpleTestCase, TestCase

from connect.search.services import (
    build_people_search_query,
    normalize_people_query,
    ranked_people_search,
    tokenize_search_query,
)


class PeopleSearchQueryParsingTests(SimpleTestCase):
    def test_normalize_people_query_strips_at_for_username_matching(self):
        self.assertEqual(normalize_people_query(" @Scout "), "scout")

    def test_tokenize_search_query_splits_punctuation_for_raw_tsquery_safety(self):
        self.assertEqual(tokenize_search_query("Scout AI-agent"), ("scout", "ai", "agent"))

    def test_build_people_search_query_uses_any_token_fallback_for_multi_word_queries(self):
        parsed = build_people_search_query("Scout documentation")
        self.assertEqual(parsed.username_query, "scout documentation")
        self.assertEqual(parsed.tokens, ("scout", "documentation"))
        self.assertIsNotNone(parsed.broad_query)


if connection.vendor == "postgresql":
    from connect.models import Skill, SocialProfile, UserSkill

    class RankedPeopleSearchPostgresTests(TestCase):
        def _profile(self, username, display_name, **kwargs):
            defaults = {
                "user_id": len(username) * 100,
                "user_email": f"{username}@agents.shizuha.io",
                "bio": "",
                "headline": "",
                "current_position": "",
                "current_company": "",
                "industry": "",
                "followers_count": 0,
                "connections_count": 0,
            }
            defaults.update(kwargs)
            return SocialProfile.objects.create(
                username=username,
                display_name=display_name,
                **defaults,
            )

        def test_exact_and_prefix_username_queries_surface_scout_first(self):
            scout = self._profile("scout", "Scout", headline="Agent research scout")
            self._profile("scoria", "Scoria", followers_count=1000)

            exact_ids = list(ranked_people_search("@scout", limit=None).values_list("id", flat=True))
            prefix_ids = list(ranked_people_search("sco", limit=None).values_list("id", flat=True))

            self.assertEqual(exact_ids[0], scout.id)
            self.assertEqual(prefix_ids[0], scout.id)

        def test_skill_and_professional_text_match_people_results(self):
            scout = self._profile("scout", "Scout", current_position="Documentation agent")
            skill = Skill.objects.create(name="Documentation", slug="documentation")
            UserSkill.objects.create(profile=scout, skill=skill)

            ids = list(ranked_people_search("documentation", limit=None).values_list("id", flat=True))

            self.assertIn(scout.id, ids)
else:
    class RankedPeopleSearchPostgresTests(SimpleTestCase):
        @unittest.skip("PostgreSQL FTS integration test")
        def test_postgresql_fts_integration_coverage_marker(self):
            pass
