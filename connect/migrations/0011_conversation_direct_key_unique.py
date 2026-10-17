"""
Conversation.direct_key + partial unique constraint.

Prevents the TOCTOU race where two concurrent message-sends between the same
pair of users can both find no existing conversation and each create one,
producing duplicate 1:1 chats (visible as "2 chats with System User" in the
Kotlin app).

Strategy:
  1. Add the nullable `direct_key` column (data migration runs next).
  2. Backfill it for every existing `direct` conversation by sorting the
     participant user_ids and joining with ":".
  3. If any duplicate pairs exist (shouldn't — we merge them separately —
     but be defensive), keep the oldest conversation with the key and null
     out the others' keys so the partial unique index still applies.
  4. Add the partial UniqueConstraint.
"""
from collections import defaultdict

from django.db import migrations, models


def backfill_direct_key(apps, schema_editor):
    Conversation = apps.get_model('connect', 'Conversation')

    # Build pair → [conv_ids...] map
    pair_to_convs = defaultdict(list)
    for conv in Conversation.objects.filter(conversation_type='direct').order_by('created_at'):
        participant_ids = sorted(conv.participants.values_list('user_id', flat=True).distinct())
        if len(participant_ids) != 2:
            # Malformed direct convo (0, 1, or 3+ participants) — skip. The
            # partial unique constraint won't apply anyway because the NULL
            # key is excluded from uniqueness.
            continue
        a, b = participant_ids
        key = f"{a}:{b}"
        pair_to_convs[key].append(conv)

    for key, convs in pair_to_convs.items():
        # Keep the oldest conversation as the canonical one for this pair.
        canonical = convs[0]
        canonical.direct_key = key
        canonical.save(update_fields=['direct_key'])
        # For any duplicates left behind (should have been merged manually
        # but just in case), leave direct_key null so the partial unique
        # constraint permits them to coexist. This preserves data safety
        # even if the operator hasn't merged duplicates yet.


def reverse_noop(apps, schema_editor):
    # Reversing just drops the column via the schema migration; no data to undo.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0010_alter_conversationparticipant_participant_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversation',
            name='direct_key',
            field=models.CharField(blank=True, db_index=True, max_length=50, null=True),
        ),
        migrations.RunPython(backfill_direct_key, reverse_noop),
        migrations.AddConstraint(
            model_name='conversation',
            constraint=models.UniqueConstraint(
                condition=models.Q(conversation_type='direct'),
                fields=('direct_key',),
                name='unique_direct_conversation_pair',
            ),
        ),
    ]
