"""Legacy funnel retirement must remove writers while preserving rollback data."""
import uuid
from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase
from django.urls import Resolver404, resolve


class DojoRoutesRetiredTests(SimpleTestCase):
    def test_old_routes_are_not_registered(self):
        for route in ("waitlist", "events", "summary"):
            with self.subTest(route=route), self.assertRaises(Resolver404):
                resolve(f"/api/dojo/{route}/")
        for model in ("DojoWaitlistSignup", "DojoValidationEvent"):
            with self.assertRaises(LookupError):
                apps.get_model("connect", model)


class DojoRetirementPreservesRowsTests(TransactionTestCase):
    def test_forward_and_reverse_leave_full_event_row_unchanged(self):
        old = [("connect", "0039_notification_taxonomy")]
        new = [("connect", "0040_retire_legacy_dojo_state")]
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        state = executor.loader.project_state(old)
        Event = state.apps.get_model("connect", "DojoValidationEvent")
        key = uuid.uuid4()
        Event.objects.create(id=key, event_type="cta_click", visitor_id="retained-test-visitor",
                             metadata={"purpose": "retirement regression"})
        before = list(Event.objects.filter(pk=key).values())
        try:
            MigrationExecutor(connection).migrate(new)
            self.assertEqual(list(Event.objects.filter(pk=key).values()), before)
            MigrationExecutor(connection).migrate(old)
            self.assertEqual(list(Event.objects.filter(pk=key).values()), before)
        finally:
            Event.objects.filter(pk=key).delete()
            MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
