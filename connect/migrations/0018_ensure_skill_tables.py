"""
CON-33: Ensure connect_skill and connect_user_skill tables exist.

These tables are defined in 0001_initial but may be missing from databases
initialized before the Skill model was added to that migration. This migration
uses SeparateDatabaseAndState so Django's migration state is unchanged (tables
remain owned by 0001) while CREATE TABLE IF NOT EXISTS runs idempotently on any
environment regardless of whether the tables already exist.

CON-17: vendor-guarded — the CREATE TABLE SQL uses gen_random_uuid() and named
CONSTRAINT syntax that are Postgres-only.  On sqlite (bridge security CI) the
skill tables are created correctly by Django's initial migration machinery, so
the remediation RunSQL is a no-op there.
"""
from django.db import migrations


_SQL_UP = [
    """
    CREATE TABLE IF NOT EXISTS connect_skill (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        name varchar(100) NOT NULL,
        slug varchar(100) NOT NULL,
        category varchar(50) NOT NULL DEFAULT '',
        CONSTRAINT connect_skill_name_key UNIQUE (name),
        CONSTRAINT connect_skill_slug_key UNIQUE (slug)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS connect_user_skill (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        profile_id uuid NOT NULL,
        skill_id uuid NOT NULL REFERENCES connect_skill(id) ON DELETE CASCADE,
        endorsement_count integer NOT NULL DEFAULT 0,
        display_order integer NOT NULL DEFAULT 0,
        CONSTRAINT connect_user_skill_profile_skill UNIQUE (profile_id, skill_id),
        CONSTRAINT connect_user_skill_profile_fk
            FOREIGN KEY (profile_id)
            REFERENCES connect_social_profile(id) ON DELETE CASCADE
    );
    """,
    "CREATE INDEX IF NOT EXISTS connect_user_skill_profile_idx ON connect_user_skill (profile_id);",
    "CREATE INDEX IF NOT EXISTS connect_user_skill_skill_idx ON connect_user_skill (skill_id);",
]


def _run_postgres_ddl(apps, schema_editor):
    """Run Postgres-specific CREATE TABLE remediation DDL.

    CON-17: guarded by vendor so sqlite CI (bridge security tests) can
    apply the full migration chain without failing on Postgres-only syntax
    (gen_random_uuid(), named CONSTRAINT clauses).  On sqlite Django's ORM
    already created these tables via 0001_initial, so the remediation is
    safely skipped.
    """
    if schema_editor.connection.vendor == 'postgresql':
        for stmt in _SQL_UP:
            stmt = stmt.strip()
            if stmt:
                schema_editor.execute(stmt)


class Migration(migrations.Migration):

    dependencies = [
        ("connect", "0017_socialprofile_onboarding_state"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[],
            database_operations=[
                migrations.RunPython(
                    code=_run_postgres_ddl,
                    reverse_code=migrations.RunPython.noop,
                ),
            ],
        ),
    ]
