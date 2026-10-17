import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('connect', '0031_security_event_outbox')]

    operations = [
        migrations.CreateModel(
            name='VerifiedAlertDelivery',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('organization_id', models.PositiveBigIntegerField(db_index=True)),
                ('requester_id', models.IntegerField(db_index=True)),
                ('recipient_id', models.IntegerField(db_index=True)),
                ('client_message_id', models.UUIDField()),
                ('request_hash', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('message', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='verified_alert_delivery', to='connect.directmessage')),
            ],
            options={'db_table': 'connect_verified_alert_delivery'},
        ),
        migrations.AddConstraint(
            model_name='verifiedalertdelivery',
            constraint=models.UniqueConstraint(fields=('requester_id', 'client_message_id'), name='unique_verified_alert_request_phase'),
        ),
        migrations.AddIndex(
            model_name='verifiedalertdelivery',
            index=models.Index(fields=['organization_id', 'recipient_id', 'created_at'], name='connect_ver_organiz_febfc2_idx'),
        ),
    ]
