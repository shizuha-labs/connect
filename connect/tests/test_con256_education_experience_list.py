"""CON-256: GET /api/education/ and /api/experience/ must not 500.

Root cause: DEFAULT_PAGINATION_CLASS is CreatedAtCursorPagination
(ordering=-created_at). Education and Experience models have no created_at
field, so list() raised FieldError → HTTP 500. Profile-scoped reads
(/api/profiles/<user>/education|experience/) bypassed the list ViewSet and
worked. Fix: StandardResultsSetPagination + model-native ordering (same
pattern as SkillViewSet).
"""
from datetime import date

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from connect.models import Education, Experience, SocialProfile

User = get_user_model()


def _mk_user(username, email):
    return User.objects.create_user(username=username, email=email)


class EducationExperienceListTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = _mk_user("con256_owner", "con256@shizuha.com")
        self.other = _mk_user("con256_other", "con256_other@shizuha.com")
        self.profile = SocialProfile.objects.create(
            user_id=self.user.id,
            user_email=self.user.email,
            username="con256_owner",
            display_name="CON256 Owner",
        )
        SocialProfile.objects.create(
            user_id=self.other.id,
            user_email=self.other.email,
            username="con256_other",
            display_name="CON256 Other",
        )
        self.edu = Education.objects.create(
            profile=self.profile,
            school_name="Test University",
            degree="BS",
            field_of_study="CS",
            start_date=date(2018, 9, 1),
            end_date=date(2022, 6, 1),
        )
        self.exp = Experience.objects.create(
            profile=self.profile,
            title="Engineer",
            company_name="Shizuha",
            start_date=date(2022, 7, 1),
            is_current=True,
        )
        # Other user's rows must not leak into the owner list.
        other_profile = SocialProfile.objects.get(user_id=self.other.id)
        Education.objects.create(
            profile=other_profile,
            school_name="Other U",
            start_date=date(2010, 1, 1),
        )
        Experience.objects.create(
            profile=other_profile,
            title="Other Role",
            company_name="Elsewhere",
            start_date=date(2011, 1, 1),
        )

    def test_list_education_returns_200_not_500(self):
        self.client.force_authenticate(self.user)
        resp = self.client.get("/api/education/")
        self.assertEqual(
            resp.status_code,
            status.HTTP_200_OK,
            msg=f"expected 200, got {resp.status_code}: {getattr(resp, 'data', resp.content)}",
        )
        # PageNumber pagination wraps results.
        results = resp.data.get("results", resp.data)
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["school_name"], "Test University")
        self.assertEqual(str(results[0]["id"]), str(self.edu.id))

    def test_list_experience_returns_200_not_500(self):
        self.client.force_authenticate(self.user)
        resp = self.client.get("/api/experience/")
        self.assertEqual(
            resp.status_code,
            status.HTTP_200_OK,
            msg=f"expected 200, got {resp.status_code}: {getattr(resp, 'data', resp.content)}",
        )
        results = resp.data.get("results", resp.data)
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["company_name"], "Shizuha")
        self.assertEqual(str(results[0]["id"]), str(self.exp.id))

    def test_list_education_empty_for_user_without_rows(self):
        lonely = _mk_user("con256_lonely", "lonely@shizuha.com")
        SocialProfile.objects.create(
            user_id=lonely.id,
            user_email=lonely.email,
            username="con256_lonely",
            display_name="Lonely",
        )
        self.client.force_authenticate(lonely)
        resp = self.client.get("/api/education/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        results = resp.data.get("results", resp.data)
        self.assertEqual(results, [])

    def test_list_unauthenticated_rejected(self):
        resp = self.client.get("/api/education/")
        self.assertIn(
            resp.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
