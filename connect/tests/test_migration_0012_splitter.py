"""CON-132: migration 0012's SQL splitter must not break fresh-Postgres migrate.

The original ``FORWARD_SQL.split(';')`` produced comment-only fragments
(psycopg2: "can't execute an empty query") and cut the plpgsql function body at
its internal semicolons. These tests pin the $$-aware splitter's behavior
against the real migration SQL so a regression fails fast on any backend.
"""
from importlib import import_module

from django.test import SimpleTestCase

m0012 = import_module('connect.migrations.0012_event_log_cursor')


class Migration0012SplitterTests(SimpleTestCase):
    def test_no_empty_or_comment_only_fragments(self):
        for script in (m0012.FORWARD_SQL, m0012.REVERSE_SQL):
            for stmt in m0012._split_postgres_statements(script):
                self.assertTrue(stmt.strip())
                self.assertFalse(
                    all(not line.strip() or line.strip().startswith('--')
                        for line in stmt.splitlines()),
                    f'comment-only fragment survived: {stmt[:80]!r}',
                )

    def test_plpgsql_function_body_kept_intact(self):
        stmts = m0012._split_postgres_statements(m0012.FORWARD_SQL)
        funcs = [s for s in stmts if 'CREATE OR REPLACE FUNCTION' in s]
        self.assertEqual(len(funcs), 1)
        func = funcs[0]
        self.assertEqual(func.count('$$'), 2)
        self.assertIn('LANGUAGE plpgsql', func)
        self.assertIn('RETURN NEW', func)

    def test_no_sql_lost_relative_to_source(self):
        stmts = m0012._split_postgres_statements(m0012.FORWARD_SQL)
        joined = '\n'.join(stmts)
        for line in m0012.FORWARD_SQL.splitlines():
            line = line.strip()
            if line and not line.startswith('--'):
                self.assertIn(line.rstrip(';'), joined)

    def test_semicolon_in_line_comment_ignored(self):
        """CON-215 regression: semicolons inside -- line comments must not split."""
        script = """\
-- Starts at 1; nextval() gives globally monotonic IDs
SELECT 1;
"""
        stmts = m0012._split_postgres_statements(script)
        self.assertEqual(len(stmts), 1)  # CON-215: the ';' in the comment did NOT split
        # The single statement executes as SELECT 1 (postgres ignores the leading
        # -- comment); the splitter need not physically strip it.
        self.assertIn('SELECT 1', stmts[0])

    def test_statement_counts(self):
        self.assertEqual(len(m0012._split_postgres_statements(m0012.FORWARD_SQL)), 11)
        self.assertEqual(len(m0012._split_postgres_statements(m0012.REVERSE_SQL)), 6)
