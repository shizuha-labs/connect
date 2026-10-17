from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0012_event_log_cursor'),
    ]

    operations = [
        migrations.CreateModel(
            name='BridgeWebhookSecret',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('provider', models.CharField(choices=[('whatsapp', 'WhatsApp')], db_index=True, max_length=32)),
                ('provider_account_id', models.CharField(db_index=True, max_length=128)),
                ('secret_ciphertext', models.TextField()),
                ('key_id', models.CharField(default='default', max_length=80)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'connect_bridge_webhook_secret'},
        ),
        migrations.CreateModel(
            name='BridgeIdentityMapping',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('provider', models.CharField(choices=[('whatsapp', 'WhatsApp')], db_index=True, max_length=32)),
                ('provider_account_id', models.CharField(db_index=True, max_length=128)),
                ('external_sender_id', models.CharField(db_index=True, max_length=160)),
                ('mapped_user_id', models.IntegerField(db_index=True)),
                ('mapped_user_email', models.EmailField(blank=True, max_length=254)),
                ('mapped_user_name', models.CharField(blank=True, max_length=200)),
                ('verified_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('is_admin_capable', models.BooleanField(default=False)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'connect_bridge_identity_mapping'},
        ),
        migrations.CreateModel(
            name='BridgeInboundEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('bridge_event_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('provider', models.CharField(choices=[('whatsapp', 'WhatsApp')], db_index=True, max_length=32)),
                ('provider_account_id', models.CharField(db_index=True, max_length=128)),
                ('provider_event_id', models.CharField(db_index=True, max_length=200)),
                ('external_sender_id', models.CharField(db_index=True, max_length=160)),
                ('mapped_user_id', models.IntegerField(blank=True, db_index=True, null=True)),
                ('identity_verified_at', models.DateTimeField(blank=True, null=True)),
                ('conversation_key', models.CharField(db_index=True, max_length=360)),
                ('direction', models.CharField(choices=[('inbound', 'Inbound')], default='inbound', max_length=16)),
                ('content_text', models.TextField(blank=True)),
                ('payload', models.JSONField(blank=True, default=dict)),
                ('payload_hash', models.CharField(max_length=64)),
                ('signature_status', models.CharField(choices=[('valid', 'Valid'), ('conflict', 'Payload conflict')], db_index=True, default='valid', max_length=16)),
                ('processing_status', models.CharField(choices=[('received', 'Received'), ('routed', 'Routed'), ('replayed', 'Idempotent replay'), ('conflict', 'Payload conflict'), ('unmapped', 'No identity mapping'), ('forbidden', 'Identity freshness/admin denied')], db_index=True, default='received', max_length=24)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('message', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='bridge_events', to='connect.directmessage')),
            ],
            options={'db_table': 'connect_bridge_inbound_event'},
        ),
        migrations.AddConstraint(
            model_name='bridgewebhooksecret',
            constraint=models.UniqueConstraint(fields=('provider', 'provider_account_id'), name='unique_bridge_webhook_secret_account'),
        ),
        migrations.AddConstraint(
            model_name='bridgeidentitymapping',
            constraint=models.UniqueConstraint(fields=('provider', 'provider_account_id', 'external_sender_id'), name='unique_bridge_identity_mapping'),
        ),
        migrations.AddIndex(
            model_name='bridgeinboundevent',
            index=models.Index(fields=['provider', 'provider_account_id', 'external_sender_id'], name='connect_bri_provide_47764c_idx'),
        ),
        migrations.AddIndex(
            model_name='bridgeinboundevent',
            index=models.Index(fields=['mapped_user_id', 'created_at'], name='connect_bri_mapped__c127b8_idx'),
        ),
        migrations.AddConstraint(
            model_name='bridgeinboundevent',
            constraint=models.UniqueConstraint(fields=('provider', 'provider_account_id', 'provider_event_id'), name='unique_bridge_inbound_provider_event'),
        ),
    ]
