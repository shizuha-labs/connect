"""Capture full private source rows only after all legacy writers are frozen."""
import gzip
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from connect.services.jobs_cutover import TABLES, frozen_tables

COLLECTIONS = {
    'company_pages': 'connect_company_page',
    'job_listings': 'connect_job_listing',
    'job_applications': 'connect_job_application',
    'job_saves': 'connect_job_save',
    'company_admins': 'connect_company_admin',
    'company_follows': 'connect_company_follow',
    'dojo_waitlist_signups': 'connect_dojo_waitlist_signup',
    'dojo_validation_events': 'connect_dojo_validation_event',
    'profile_experiences': 'connect_experience',
}


class Command(BaseCommand):
    help = 'Export a consistent mode-0600 gzip snapshot after freeze_jobs_for_cutover freeze.'

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True, help='new private .json.gz archive path outside Git')

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('all six source tables must be frozen before exporting')
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [0x434F4E4E, 465])
            if set(frozen_tables()) != set(TABLES):
                raise CommandError('all six source tables must be frozen before exporting')
            payload = {'_metadata': {'schema': 1, 'captured_at': datetime.now(timezone.utc).isoformat(),
                                      'write_freeze': list(TABLES)}}
            for name, table in COLLECTIONS.items():
                # Fixed identifiers; SQL JSON preserves every physical column,
                # including the retired Dojo tables absent from runtime models.
                cursor.execute('SELECT to_jsonb(source_row) FROM '+connection.ops.quote_name(table)+
                               ' source_row ORDER BY id')
                payload[name] = [json.loads(row[0]) if isinstance(row[0], str) else row[0]
                                 for row in cursor.fetchall()]
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        compressed = gzip.compress(raw, mtime=0)
        path = Path(options['output'])
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as archive:
                archive.write(compressed)
                archive.flush()
                os.fsync(archive.fileno())
        except OSError as exc:
            raise CommandError('could not create the new private snapshot archive') from exc
        self.stdout.write(json.dumps({'archive': str(path), 'sha256': hashlib.sha256(compressed).hexdigest(),
                                     'payload_sha256': hashlib.sha256(raw).hexdigest(),
                                     'counts': {name: len(payload[name]) for name in COLLECTIONS}}, sort_keys=True))
