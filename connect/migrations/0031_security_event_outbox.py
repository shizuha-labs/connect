import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('connect', '0030_securitypipelinemetrichealth'),
    ]

    operations = [
        migrations.CreateModel(
            name='SecurityEventOutbox',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('producer_service', models.CharField(max_length=64)),
                ('producer_event_id', models.CharField(max_length=128)),
                ('schema_version', models.CharField(default='1.0', max_length=16)),
                ('event_type', models.CharField(max_length=128)),
                ('payload', models.JSONField()),
                ('request_hash', models.CharField(max_length=64)),
                ('semantic_fingerprint', models.CharField(blank=True, db_index=True, max_length=64)),
                ('status', models.CharField(choices=[
                    ('accepted', 'Accepted'),
                    ('retrying', 'Retrying'),
                    ('processing', 'Processing'),
                    ('sent', 'Sent'),
                    ('suppressed_no_anomaly', 'Suppressed: no anomaly'),
                    ('suppressed_repeat', 'Suppressed: semantic repeat'),
                    ('disabled', 'Disabled'),
                    ('no_recipient', 'No recipient'),
                    ('dead_lettered', 'Dead lettered'),
                ], default='accepted', max_length=32)),
                ('publish_attempts', models.PositiveIntegerField(default=0)),
                ('delivery_attempts', models.PositiveIntegerField(default=0)),
                ('conflict_count', models.PositiveIntegerField(default=0)),
                ('next_dispatch_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('lease_owner', models.CharField(blank=True, max_length=64)),
                ('lease_expires_at', models.DateTimeField(blank=True, null=True)),
                ('accepted_at', models.DateTimeField(auto_now_add=True)),
                ('first_published_at', models.DateTimeField(blank=True, null=True)),
                ('last_published_at', models.DateTimeField(blank=True, null=True)),
                ('terminal_at', models.DateTimeField(blank=True, null=True)),
                ('last_error_class', models.CharField(blank=True, max_length=64)),
                ('terminal_reason', models.CharField(blank=True, max_length=128)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('terminal_message', models.OneToOneField(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='security_event_outbox',
                    to='connect.directmessage',
                )),
            ],
            options={
                'db_table': 'connect_security_event_outbox',
                'indexes': [
                    models.Index(fields=['status', 'next_dispatch_at'], name='connect_sec_out_due_idx'),
                    models.Index(fields=['status', 'accepted_at'], name='connect_sec_out_age_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('producer_service', 'producer_event_id'),
                        name='unique_security_event_producer_identity',
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name='SecurityEventRelayHealth',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('channel', models.CharField(default='security_outbox', max_length=32, unique=True)),
                ('last_run_at', models.DateTimeField(blank=True, null=True)),
                ('last_success_at', models.DateTimeField(blank=True, null=True)),
                ('failure_count', models.PositiveBigIntegerField(default=0)),
                ('last_error_class', models.CharField(blank=True, max_length=64)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={'db_table': 'connect_security_event_relay_health'},
        ),
        migrations.CreateModel(
            name='SecurityEventRepeatClaim',
            fields=[
                ('fingerprint', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner_outbox', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='repeat_claims',
                    to='connect.securityeventoutbox',
                )),
            ],
            options={'db_table': 'connect_security_event_repeat_claim'},
        ),
    ]
