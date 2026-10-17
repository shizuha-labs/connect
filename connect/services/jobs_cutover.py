"""Scoped PostgreSQL write freeze for an interruption-safe jobs cutover."""
from django.db import connection

TABLES = ('connect_company_page', 'connect_job_listing', 'connect_job_application',
          'connect_job_save', 'connect_company_admin', 'connect_company_follow')
TRIGGER = 'connect_jobs_cutover_freeze'
FUNCTION = 'connect_jobs_cutover_reject_write'


def frozen_tables():
    if connection.vendor != 'postgresql':
        return []
    with connection.cursor() as cursor:
        cursor.execute('''SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                          JOIN pg_namespace n ON n.oid=c.relnamespace
                          WHERE t.tgname=%s AND NOT t.tgisinternal AND t.tgenabled IN ('O', 'A') AND n.nspname=current_schema()
                          AND c.relname = ANY(%s) ORDER BY c.relname''', [TRIGGER, list(TABLES)])
        return [row[0] for row in cursor.fetchall()]
