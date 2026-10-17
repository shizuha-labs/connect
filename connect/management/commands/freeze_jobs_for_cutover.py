"""Freeze all jobs/company writers atomically; messaging remains writable."""
import json
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from connect.services.jobs_cutover import FUNCTION, TABLES, TRIGGER, frozen_tables


class Command(BaseCommand):
    help = 'CON-465: freeze/status/thaw the six legacy jobs tables before a consistent private export.'

    def add_arguments(self, parser):
        parser.add_argument('action', choices=('freeze', 'status', 'thaw'))

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('jobs cutover write freeze requires PostgreSQL')
        action = options['action']
        if action != 'status':
            quote = connection.ops.quote_name
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '10s'")
                cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [0x434F4E4E, 465])
                # Wait for in-flight writes, then freeze every table together.
                cursor.execute('LOCK TABLE '+', '.join(map(quote, TABLES))+' IN SHARE ROW EXCLUSIVE MODE')
                if action == 'freeze':
                    cursor.execute(f'''CREATE OR REPLACE FUNCTION {quote(FUNCTION)}() RETURNS trigger
                        LANGUAGE plpgsql AS $$ BEGIN
                        RAISE EXCEPTION 'Jobs data is read-only for cutover' USING ERRCODE='55000';
                        END; $$''')
                for table in TABLES:
                    cursor.execute(f'DROP TRIGGER IF EXISTS {quote(TRIGGER)} ON {quote(table)}')
                    if action == 'freeze':
                        cursor.execute(f'''CREATE TRIGGER {quote(TRIGGER)} BEFORE INSERT OR UPDATE OR DELETE
                                           ON {quote(table)} FOR EACH ROW EXECUTE FUNCTION {quote(FUNCTION)}()''')
                if action == 'thaw':
                    cursor.execute(f'DROP FUNCTION IF EXISTS {quote(FUNCTION)}()')
        tables = frozen_tables()
        if action == 'freeze' and set(tables) != set(TABLES):
            raise CommandError('incomplete jobs freeze; stop cutover')
        if action == 'thaw' and tables:
            raise CommandError('jobs thaw was incomplete')
        self.stdout.write(json.dumps({'action': action, 'frozen_tables': tables, 'complete': len(tables) == len(TABLES)}))
