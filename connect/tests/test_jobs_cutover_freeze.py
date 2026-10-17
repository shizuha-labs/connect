"""The write freeze covers every legacy table and leaves core data available."""
from io import StringIO
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase
from django.db.migrations.executor import MigrationExecutor
from connect.models import Skill
from connect.services.jobs_cutover import TABLES, frozen_tables


class JobsCutoverFreezeTests(TransactionTestCase):
    def setUp(self):
        old = [('connect', '0040_retire_legacy_dojo_state')]
        state = MigrationExecutor(connection).loader.project_state(old).apps
        for name in ('CompanyPage', 'CompanyAdmin', 'CompanyFollow', 'JobListing', 'JobApplication', 'JobSave'):
            setattr(self, name, state.get_model('connect', name))
        self.addCleanup(self.cleanup_legacy_rows)
        self.company = self.CompanyPage.objects.create(name='Cutover fixture', slug='cutover-fixture', owner_id=42)
        self.job = self.JobListing.objects.create(company=self.company, posted_by_id=42, title='Fixture position', status='active')
        self.CompanyAdmin.objects.create(company=self.company, user_id=42, role='super_admin')
        self.CompanyFollow.objects.create(company=self.company, user_id=42)
        self.JobApplication.objects.create(job=self.job, company=self.company, applicant_id=42,
                                      applicant_email='fixture@example.com', applicant_name='Fixture')
        self.JobSave.objects.create(job=self.job, user_id=42)

    def cleanup_legacy_rows(self):
        for name in ('JobApplication', 'JobSave', 'CompanyFollow', 'CompanyAdmin', 'JobListing', 'CompanyPage'):
            getattr(self, name).objects.all().delete()

    def freeze(self):
        call_command('freeze_jobs_for_cutover', 'freeze', stdout=StringIO())
        self.addCleanup(lambda: call_command('freeze_jobs_for_cutover', 'thaw', stdout=StringIO()))

    def test_all_table_writes_fail_closed_but_core_writes_and_reads_survive(self):
        self.freeze()
        self.assertEqual(set(frozen_tables()), set(TABLES))
        for table in TABLES:
            with self.subTest(table=table):
                with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
                    # BEFORE trigger must run even before a duplicated PK constraint.
                    cursor.execute(f'INSERT INTO {connection.ops.quote_name(table)} SELECT * FROM {connection.ops.quote_name(table)} LIMIT 1')
                with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(f'DELETE FROM {connection.ops.quote_name(table)}')
        with self.assertRaises(DatabaseError), transaction.atomic():
            self.JobListing.objects.filter(pk=self.job.pk).update(title='Bulk bypass')
        Skill.objects.create(name='Core remains writable', slug='core-writable')
        self.assertEqual(self.JobListing.objects.get(pk=self.job.pk).title, 'Fixture position')
        self.assertEqual(self.CompanyPage.objects.count(), 1)

    def test_freeze_and_thaw_are_idempotent_and_restore_writes(self):
        self.freeze()
        call_command('freeze_jobs_for_cutover', 'freeze', stdout=StringIO())
        call_command('freeze_jobs_for_cutover', 'thaw', stdout=StringIO())
        call_command('freeze_jobs_for_cutover', 'thaw', stdout=StringIO())
        self.assertEqual(frozen_tables(), [])
        self.assertEqual(self.JobListing.objects.filter(pk=self.job.pk).update(title='Writable again'), 1)


    def test_export_requires_freeze_and_retains_all_physical_source_fields(self):
        import gzip
        import json
        import tempfile
        from pathlib import Path
        from django.core.management.base import CommandError
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "private.json.gz"
            with self.assertRaises(CommandError):
                call_command('export_jobs_cutover_snapshot', output=str(target), stdout=StringIO())
            self.freeze()
            call_command('export_jobs_cutover_snapshot', output=str(target), stdout=StringIO())
            data = json.loads(gzip.decompress(target.read_bytes()))
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(data['job_applications']), 1)
            self.assertEqual(data['job_applications'][0]['applicant_email'], 'fixture@example.com')
            self.assertIn('dojo_validation_events', data)
            self.assertIn('profile_experiences', data)
            with self.assertRaises(CommandError):
                call_command('export_jobs_cutover_snapshot', output=str(target), stdout=StringIO())
