"""Core extraction removes runtime access while retaining complete rollback rows."""
from django.apps import apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TransactionTestCase
from django.urls import Resolver404, resolve


class JobsRoutesRetiredTests(SimpleTestCase):
    def test_private_product_routes_and_runtime_models_are_gone(self):
        for route in ('jobs', 'companies', 'applications', 'search/jobs', 'search/companies'):
            with self.subTest(route=route), self.assertRaises(Resolver404):
                resolve('/api/' + route + '/')
        for model in ('CompanyPage', 'CompanyAdmin', 'CompanyFollow', 'JobListing', 'JobApplication', 'JobSave'):
            with self.subTest(model=model), self.assertRaises(LookupError):
                apps.get_model('connect', model)


class JobsRetirementPreservesRowsTests(TransactionTestCase):
    def test_forward_reverse_preserve_six_complete_source_rows(self):
        old = [('connect', '0040_retire_legacy_dojo_state')]
        new = [('connect', '0041_retire_jobs_state')]
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        state = executor.loader.project_state(old).apps
        Company = state.get_model('connect', 'CompanyPage')
        Job = state.get_model('connect', 'JobListing')
        company = Company.objects.create(name='Archived employer', slug='retained-employer', owner_id=42, industry='software')
        job = Job.objects.create(company=company, posted_by_id=42, title='Archived role', description='Complete retained text', employment_type='full_time', workplace_type='remote', location='remote')
        rows = [company, job,
                state.get_model('connect', 'CompanyAdmin').objects.create(company=company, user_id=42, user_email='fixture@example.test', role='recruiting_admin'),
                state.get_model('connect', 'CompanyFollow').objects.create(company=company, user_id=42),
                state.get_model('connect', 'JobSave').objects.create(job=job, user_id=42),
                state.get_model('connect', 'JobApplication').objects.create(job=job, applicant_id=42, applicant_email='fixture@example.test', recruiter_notes='private retained note', answers={'proof': ['retained']})]
        before = [(type(row), row.pk, type(row).objects.filter(pk=row.pk).values().get()) for row in rows]
        try:
            for target in (new, old):
                MigrationExecutor(connection).migrate(target)
                for model, key, value in before:
                    self.assertEqual(model.objects.filter(pk=key).values().get(), value)
        finally:
            for row in reversed(rows):
                type(row).objects.filter(pk=row.pk).delete()
            MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())


class RetiredSchedulerTests(TransactionTestCase):
    def test_upgrade_disables_only_retired_dispatch_and_preserves_configuration(self):
        from django_celery_beat.models import IntervalSchedule, PeriodicTask
        old = [('connect', '0041_retire_jobs_state')]
        new = [('connect', '0042_disable_retired_jobs_schedule')]
        MigrationExecutor(connection).migrate(old)
        interval = IntervalSchedule.objects.create(every=30, period='seconds')
        retired = PeriodicTask.objects.create(name='fixture-retired-writer', interval=interval,
            task='connect.tasks.ingest_public_swe_jobs_daily', kwargs='{"fixture": true}', enabled=True)
        core = PeriodicTask.objects.create(name='fixture-core-writer', interval=interval,
            task='connect.tasks.calculate_trending_topics', enabled=True)
        before = PeriodicTask.objects.filter(pk=retired.pk).values().get()
        try:
            MigrationExecutor(connection).migrate(new)
            after = PeriodicTask.objects.filter(pk=retired.pk).values().get()
            self.assertEqual(after, {**before, 'enabled': False})
            core.refresh_from_db()
            self.assertTrue(core.enabled)
            MigrationExecutor(connection).migrate(old)
            self.assertEqual(PeriodicTask.objects.filter(pk=retired.pk).values().get(), after)
        finally:
            retired.delete()
            core.delete()
            interval.delete()
            MigrationExecutor(connection).migrate(new)
