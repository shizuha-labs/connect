"""CON-429 S2: sender-side notification taxonomy + per-channel digest flag.

Design §2/§7: `notification_class` (nullable, indexed — DMs stay
unclassified) + `notification_state` (default `unread`, indexed) land on
the message row; `digest_enabled` (three-state nullable bool, None/OFF
until the §7.1 backfill is verified) lands on the conversation.

NOTE (deliberate omission, per the CON-425 S2 pin): design §7's
recipient-led composite index `(recipient_id, read_at, notification_class)`
is NOT created here — it references columns that do not exist on
DirectMessage (recipients are per-participant rows; read receipts live on
MessageRead). The mismatch is flagged on the S2 PR for architect ruling;
do not guess the seam.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0038_external_account'),
    ]

    operations = [
        migrations.AddField(
            model_name='directmessage',
            name='notification_class',
            field=models.CharField(
                blank=True,
                choices=[
                    ('actionable', 'Actionable'),
                    ('informational', 'Informational'),
                    ('alert', 'Alert'),
                ],
                db_index=True,
                max_length=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='directmessage',
            name='notification_state',
            field=models.CharField(
                choices=[
                    ('unread', 'Unread'),
                    ('read', 'Read'),
                    ('needs_action', 'Needs Action'),
                    ('addressed', 'Addressed'),
                    ('dismissed', 'Dismissed'),
                    ('read_eligible', 'Read-Eligible (grandfathered)'),
                ],
                db_index=True,
                default='unread',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='conversation',
            name='digest_enabled',
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
    ]
