"""Add client_message_id idempotency key to DirectMessage."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0003_conversationparticipant_cleared_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='directmessage',
            name='client_message_id',
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
    ]
