"""
Add agent participant fields to ConversationParticipant and DirectMessage.

Enables AI agents to participate in group conversations.
"""

import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0007_messagestar'),
    ]

    operations = [
        # ConversationParticipant: participant_type, agent_id, agent_role
        migrations.AddField(
            model_name='conversationparticipant',
            name='participant_type',
            field=models.CharField(
                choices=[('human', 'Human'), ('agent', 'AI Agent')],
                db_index=True,
                default='human',
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='conversationparticipant',
            name='agent_id',
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='conversationparticipant',
            name='agent_role',
            field=models.CharField(blank=True, max_length=50),
        ),
        # DirectMessage: sender_is_agent, agent_id
        migrations.AddField(
            model_name='directmessage',
            name='sender_is_agent',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='directmessage',
            name='agent_id',
            field=models.UUIDField(blank=True, null=True),
        ),
    ]
