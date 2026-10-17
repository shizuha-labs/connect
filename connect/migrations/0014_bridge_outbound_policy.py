# Generated for CON-13 outbound bridge authorization/rate-limit/audit core.

import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0013_bridge_envelope_idempotency'),
    ]

    operations = [
        migrations.CreateModel(
            name='BridgeChannelPolicy',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('provider', models.CharField(choices=[('whatsapp', 'WhatsApp')], db_index=True, max_length=32)),
                ('provider_account_id', models.CharField(db_index=True, max_length=128)),
                ('channel_key', models.CharField(db_index=True, max_length=200)),
                ('task_key', models.CharField(db_index=True, max_length=80)),
                ('agent_id', models.IntegerField(db_index=True)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('admin_approved_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('admin_approved_by', models.IntegerField(blank=True, null=True)),
                ('allowed_recipients', models.JSONField(blank=True, default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'connect_bridge_channel_policy'},
        ),
        migrations.CreateModel(
            name='BridgeOutboundEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('outbound_event_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('provider', models.CharField(choices=[('whatsapp', 'WhatsApp')], db_index=True, max_length=32)),
                ('provider_account_id', models.CharField(db_index=True, max_length=128)),
                ('channel_key', models.CharField(db_index=True, max_length=200)),
                ('task_key', models.CharField(db_index=True, max_length=80)),
                ('agent_id', models.IntegerField(db_index=True)),
                ('recipient_external_id', models.CharField(db_index=True, max_length=200)),
                ('idempotency_key', models.CharField(db_index=True, max_length=200)),
                ('request_hash', models.CharField(max_length=64)),
                ('body_hash', models.CharField(max_length=64)),
                ('provider_delivery_id', models.CharField(blank=True, db_index=True, max_length=200)),
                ('status', models.CharField(choices=[('pending', 'Pending provider acceptance'), ('accepted', 'Accepted by provider'), ('delivered', 'Delivered'), ('failed', 'Failed'), ('forbidden', 'Policy denied'), ('rate_limited', 'Rate limited'), ('conflict', 'Idempotency conflict')], db_index=True, default='pending', max_length=24)),
                ('error_code', models.CharField(blank=True, max_length=80)),
                ('error_message', models.CharField(blank=True, max_length=240)),
                ('audit_metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('conversation', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='bridge_outbound_events', to='connect.conversation')),
            ],
            options={'db_table': 'connect_bridge_outbound_event'},
        ),
        migrations.AddConstraint(
            model_name='bridgechannelpolicy',
            constraint=models.UniqueConstraint(fields=('provider', 'provider_account_id', 'channel_key', 'task_key', 'agent_id'), name='unique_bridge_channel_policy_scope'),
        ),
        migrations.AddIndex(
            model_name='bridgeoutboundevent',
            index=models.Index(fields=['agent_id', 'created_at'], name='connect_bri_agent__db5aa1_idx'),
        ),
        migrations.AddIndex(
            model_name='bridgeoutboundevent',
            index=models.Index(fields=['recipient_external_id', 'created_at'], name='connect_bri_recipi_1d2819_idx'),
        ),
        migrations.AddIndex(
            model_name='bridgeoutboundevent',
            index=models.Index(fields=['provider', 'provider_account_id', 'created_at'], name='connect_bri_provide_b619c2_idx'),
        ),
        migrations.AddConstraint(
            model_name='bridgeoutboundevent',
            constraint=models.UniqueConstraint(fields=('provider', 'provider_account_id', 'idempotency_key'), name='unique_bridge_outbound_idempotency'),
        ),
    ]
