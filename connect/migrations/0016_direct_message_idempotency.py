# Generated for CON-9 direct-message idempotency core.

import uuid
import hashlib
import json
from django.db import migrations, models
import django.db.models.deletion


def _request_hash(sender_id, recipient_id, content, reply_to_message_id):
    payload = {
        'sender_id': int(sender_id),
        'recipient_id': int(recipient_id),
        'content': content,
        'reply_to_message_id': str(reply_to_message_id or ''),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def backfill_direct_message_idempotency(apps, schema_editor):
    DirectMessage = apps.get_model('connect', 'DirectMessage')
    ConversationParticipant = apps.get_model('connect', 'ConversationParticipant')
    DirectMessageIdempotency = apps.get_model('connect', 'DirectMessageIdempotency')

    messages = DirectMessage.objects.exclude(
        client_message_id__isnull=True
    ).filter(
        conversation__conversation_type='direct'
    ).select_related('conversation')
    for message in messages.iterator():
        recipient = (
            ConversationParticipant.objects
            .filter(conversation_id=message.conversation_id)
            .exclude(user_id=message.sender_id)
            .order_by('id')
            .first()
        )
        if not recipient:
            continue
        DirectMessageIdempotency.objects.get_or_create(
            sender_id=message.sender_id,
            client_message_id=message.client_message_id,
            defaults={
                'recipient_id': recipient.user_id,
                'conversation_id': message.conversation_id,
                'message_id': message.id,
                'request_hash': _request_hash(
                    message.sender_id,
                    recipient.user_id,
                    message.content,
                    message.reply_to_id,
                ),
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0015_presence_state'),
    ]

    operations = [
        migrations.CreateModel(
            name='DirectMessageIdempotency',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('sender_id', models.IntegerField(db_index=True)),
                ('recipient_id', models.IntegerField(db_index=True)),
                ('client_message_id', models.UUIDField(db_index=True)),
                ('request_hash', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('conversation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='message_idempotency', to='connect.conversation')),
                ('message', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='idempotency_record', to='connect.directmessage')),
            ],
            options={
                'db_table': 'connect_direct_message_idempotency',
            },
        ),
        migrations.RunPython(backfill_direct_message_idempotency, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='directmessageidempotency',
            constraint=models.UniqueConstraint(fields=('sender_id', 'client_message_id'), name='unique_dm_idempotency_sender_client'),
        ),
        migrations.AddIndex(
            model_name='directmessageidempotency',
            index=models.Index(fields=['sender_id', 'recipient_id', 'client_message_id'], name='connect_dir_sender__4d3a4d_idx'),
        ),
        migrations.AddIndex(
            model_name='directmessageidempotency',
            index=models.Index(fields=['conversation', 'client_message_id'], name='connect_dir_convers_d2db51_idx'),
        ),
    ]
