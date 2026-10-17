"""
K1 — Event-log cursor schema.

Adds the three columns needed for cursor-based replay on reconnect
(see MESSAGING_KAFKA.md § Schema additions):

1. `DirectMessage.event_id` — globally-monotonic BIGINT, assigned by a
   Postgres sequence on INSERT. This is the cursor clients send back as
   `since_event_id` on WS reconnect to replay only what they missed.

2. `DirectMessage.conversation_seq_num` — strictly monotonic PER-conversation,
   assigned by a `BEFORE INSERT` trigger under a conversation-scoped advisory
   lock. Gives clients a stable `(seqNum, createdAt, id)` ordering key that
   can't flip under NTP jitter.

3. `ConversationParticipant.last_read_event_id` — cursor into the event_id
   space, advanced on every `message_read` WS event. Supersedes
   `last_read_at` as the replay-window primitive.

Backfill:
- event_id: assign in chronological order (oldest → lowest id) so that pre-
  migration messages get stable cursors.
- conversation_seq_num: same, but partitioned by conversation.
- last_read_event_id: leave NULL; consumers treat NULL as "no cursor yet"
  and fall back to the time-based cutoff.

All three columns stay NULL-able at the schema level. The sequence + trigger
guarantee they're populated on every new INSERT; the backfill guarantees
they're populated on every pre-existing row. Applying the NOT NULL constraint
is a follow-up migration we can land once we're sure no write path bypasses
the trigger (e.g. bulk_create with explicit ignore_conflicts).
"""
from django.db import migrations, models


# Forward DDL + backfill. Postgres-specific (uses SEQUENCE, triggers,
# advisory locks). If you ever port to SQLite for tests, the save() override
# in the Python model needs to mirror the trigger logic — but the main app
# runs Postgres end-to-end.
FORWARD_SQL = r"""
-- 1. Sequence for event_id. Starts at 1; nextval() gives globally monotonic
--    BIGINT across all conversations. Matches WhatsApp's per-user offset
--    concept when combined with the participant's last_read_event_id.
CREATE SEQUENCE IF NOT EXISTS connect_direct_message_event_id_seq AS BIGINT;

-- 2. Wire the sequence as the column default. New INSERTs without an
--    explicit event_id get the next value atomically.
ALTER TABLE connect_direct_message
  ALTER COLUMN event_id SET DEFAULT nextval('connect_direct_message_event_id_seq');
ALTER SEQUENCE connect_direct_message_event_id_seq
  OWNED BY connect_direct_message.event_id;

-- 3. Supporting composite indexes — replay queries filter by conversation_id
--    + event_id, and history queries paginate by (conversation_id, event_id DESC).
--    Create these BEFORE any DML so Postgres isn't holding "pending trigger
--    events" on the table when CREATE INDEX runs (which it refuses to do in
--    a txn that already has uncommitted trigger fires).
CREATE INDEX IF NOT EXISTS idx_dm_conversation_event
  ON connect_direct_message (conversation_id, event_id);
CREATE INDEX IF NOT EXISTS idx_dm_conversation_seq_num
  ON connect_direct_message (conversation_id, conversation_seq_num);

-- 4. BEFORE INSERT trigger assigning both cursors.
--    event_id: Django sends explicit NULL for nullable columns on INSERT
--    (doesn't let the column DEFAULT fire). Trigger assigns nextval()
--    directly when NEW.event_id IS NULL — belt-and-suspenders with the
--    column DEFAULT so both pathways produce the same result.
--    conversation_seq_num: uses a conversation-scoped advisory lock so two
--    concurrent INSERTs into the same conversation serialize; different
--    conversations proceed in parallel.
CREATE OR REPLACE FUNCTION connect_direct_message_assign_conv_seq()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.event_id IS NULL THEN
    NEW.event_id := nextval('connect_direct_message_event_id_seq');
  END IF;
  IF NEW.conversation_seq_num IS NULL THEN
    PERFORM pg_advisory_xact_lock(
      hashtextextended(NEW.conversation_id::text, 0)
    );
    SELECT COALESCE(MAX(conversation_seq_num), 0) + 1
      INTO NEW.conversation_seq_num
      FROM connect_direct_message
     WHERE conversation_id = NEW.conversation_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_connect_direct_message_assign_conv_seq
  ON connect_direct_message;
CREATE TRIGGER trg_connect_direct_message_assign_conv_seq
  BEFORE INSERT ON connect_direct_message
  FOR EACH ROW EXECUTE FUNCTION connect_direct_message_assign_conv_seq();

-- 5. Backfill event_id for pre-existing rows in strict chronological order.
--    row_number() over created_at guarantees no collisions with the sequence
--    (we advance the sequence to the max afterwards).
WITH ordered AS (
  SELECT id, row_number() OVER (ORDER BY created_at, id) AS rn
    FROM connect_direct_message
   WHERE event_id IS NULL
)
UPDATE connect_direct_message dm
   SET event_id = ordered.rn
  FROM ordered
 WHERE dm.id = ordered.id;

-- Advance the sequence past the backfilled values so subsequent INSERTs
-- don't collide. `is_called=true` means next nextval() returns last_value+1.
SELECT setval(
  'connect_direct_message_event_id_seq',
  COALESCE((SELECT MAX(event_id) FROM connect_direct_message), 1),
  true
);

-- 6. Backfill conversation_seq_num per conversation in chronological order.
UPDATE connect_direct_message dm
   SET conversation_seq_num = sub.seq
  FROM (
    SELECT id,
           row_number() OVER (
             PARTITION BY conversation_id
             ORDER BY created_at, id
           ) AS seq
      FROM connect_direct_message
     WHERE conversation_seq_num IS NULL
  ) sub
 WHERE dm.id = sub.id;
"""

REVERSE_SQL = r"""
DROP INDEX IF EXISTS idx_dm_conversation_seq_num;
DROP INDEX IF EXISTS idx_dm_conversation_event;
DROP TRIGGER IF EXISTS trg_connect_direct_message_assign_conv_seq
  ON connect_direct_message;
DROP FUNCTION IF EXISTS connect_direct_message_assign_conv_seq();
ALTER TABLE connect_direct_message
  ALTER COLUMN event_id DROP DEFAULT;
DROP SEQUENCE IF EXISTS connect_direct_message_event_id_seq;
"""


def _split_postgres_statements(script):
    """Split a SQL script on ';' without destroying $$-quoted bodies (CON-132)
    or semicolons inside -- line comments (CON-215).

    The previous naive ``script.split(';')`` produced comment-only fragments
    (psycopg2 rejects them as empty queries), cut the plpgsql function body
    at its internal semicolons, and split on semicolons inside SQL line
    comments. This uses a simple state machine that tracks:
    - $$ dollar-quoted bodies (rejoin until closing $$)
    - -- line comments (ignore semicolons until newline)
    - ' single-quoted strings (ignore semicolons until closing quote)
    - /* block comments (ignore semicolons until */)
    Fragments containing only SQL comments/blank lines are dropped.
    """
    statements = []
    buf = ''
    in_dollar = False
    in_line_comment = False
    in_single_quote = False
    in_block_comment = False
    i = 0
    while i < len(script):
        ch = script[i]
        next_ch = script[i + 1] if i + 1 < len(script) else ''

        # Track state transitions
        if not in_dollar and not in_single_quote and not in_block_comment:
            if ch == '-' and next_ch == '-':
                in_line_comment = True
                i += 2
                buf += '--'
                continue
            if ch == '/' and next_ch == '*':
                in_block_comment = True
                i += 2
                buf += '/*'
                continue

        if in_line_comment:
            if ch == '\n':
                in_line_comment = False
            elif ch == '\r':
                in_line_comment = False
                if next_ch == '\n':
                    i += 1
                    buf += '\r\n'
                    continue
            buf += ch
            i += 1
            continue

        if in_block_comment:
            if ch == '*' and next_ch == '/':
                in_block_comment = False
                i += 2
                buf += '*/'
                continue
            buf += ch
            i += 1
            continue

        if ch == "'" and not in_dollar:
            in_single_quote = not in_single_quote
            buf += ch
            i += 1
            continue

        if ch == '$' and next_ch == '$':
            in_dollar = not in_dollar
            buf += '$$'
            i += 2
            continue

        if ch == ';' and not in_dollar and not in_single_quote:
            stmt = buf.strip()
            buf = ''
            if stmt and not all(
                not line.strip() or line.strip().startswith('--')
                for line in stmt.splitlines()
            ):
                statements.append(stmt)
            i += 1
            continue

        buf += ch
        i += 1

    # Handle trailing statement
    stmt = buf.strip()
    if stmt and not all(
        not line.strip() or line.strip().startswith('--')
        for line in stmt.splitlines()
    ):
        statements.append(stmt)

    return statements


def _run_postgres_ddl(apps, schema_editor):
    """Run Postgres-specific sequence/trigger/backfill DDL.

    CON-17: guarded by vendor so sqlite CI (bridge security tests) can
    run the migration without failing on Postgres-only syntax.  The
    AddField operations above already added the columns as nullable, so
    sqlite tests get the correct schema; the Python-level model logic
    assigns event_id / conversation_seq_num on save() for sqlite.
    """
    if schema_editor.connection.vendor == 'postgresql':
        # Execute statement-by-statement to tolerate non-atomic=False mode.
        for stmt in _split_postgres_statements(FORWARD_SQL):
            schema_editor.execute(stmt)


def _reverse_postgres_ddl(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        for stmt in _split_postgres_statements(REVERSE_SQL):
            schema_editor.execute(stmt)


class Migration(migrations.Migration):

    # Non-atomic: the sequence+trigger+backfill block mixes DDL and DML on the
    # same table. Postgres refuses to `CREATE INDEX` on a table that has
    # pending trigger-fire events from DML earlier in the same txn. Running
    # each operation in its own transaction sidesteps that constraint and
    # keeps the backfill idempotent — a mid-migration crash can be retried
    # because the column adds use IF NOT EXISTS semantics (nullable columns)
    # and the backfill updates are WHERE `<col> IS NULL`.
    atomic = False

    dependencies = [
        ('connect', '0011_conversation_direct_key_unique'),
    ]

    operations = [
        # Step 1: add the columns as nullable so pre-existing rows survive.
        migrations.AddField(
            model_name='directmessage',
            name='event_id',
            field=models.BigIntegerField(
                blank=True, db_index=True, editable=False, null=True, unique=True,
            ),
        ),
        migrations.AddField(
            model_name='directmessage',
            name='conversation_seq_num',
            field=models.PositiveIntegerField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='conversationparticipant',
            name='last_read_event_id',
            field=models.BigIntegerField(blank=True, db_index=True, null=True),
        ),
        # Step 2 (CON-17): vendor-guarded — only runs on Postgres.
        # sqlite: columns stay nullable with no sequence/trigger; the
        # application-level save() assigns event_id/conversation_seq_num so
        # bridge security tests run without Postgres infrastructure.
        migrations.RunPython(
            code=_run_postgres_ddl,
            reverse_code=_reverse_postgres_ddl,
        ),
    ]

