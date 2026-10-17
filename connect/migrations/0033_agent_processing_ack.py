from django.db import migrations, models


# Confirmed transport-delivered but never gateway-persisted during the
# 2026-08-02 Nagi runtime-roll incident. IDs only; no content is embedded.
RECOVERY_MESSAGE_IDS = [
    'afb6f407-6c9b-4f33-92a4-3ccbe1c7a0a0',
    '29a927e7-3604-4b27-bd9a-6aef6ee1f3ec',
    '95142878-fd13-4d7d-b3e6-e116da0a18f0',
    'ef54c738-4b5d-43bb-918d-52f688d5d541',
    '6140b976-43fa-4ef0-8e25-15929b24c55e',
    '1b738730-6558-4577-99d0-f620154bc3a5',
    '513d84f0-5297-429e-9520-e81240c5b5dd',
]


def mark_confirmed_unprocessed(apps, schema_editor):
    MessageDelivery = apps.get_model('connect', 'MessageDelivery')
    MessageDelivery.objects.filter(
        user_id=141,
        message_id__in=RECOVERY_MESSAGE_IDS,
        processed_at__isnull=True,
    ).update(processing_ack_required=True)


class Migration(migrations.Migration):
    dependencies = [('connect', '0032_verifiedalertdelivery')]

    operations = [
        migrations.AddField(model_name='messagedelivery', name='processing_ack_required', field=models.BooleanField(default=False)),
        migrations.AddField(model_name='messagedelivery', name='processed_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='messagedelivery', name='processing_attempts', field=models.PositiveSmallIntegerField(default=0)),
        migrations.AddField(model_name='messagedelivery', name='last_processing_attempt_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='messagedelivery', name='processing_dead_lettered_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddIndex(
            model_name='messagedelivery',
            index=models.Index(
                fields=['user_id', 'processing_ack_required', 'processed_at', 'processing_dead_lettered_at'],
                name='connect_delivery_ack_replay',
            ),
        ),
        migrations.RunPython(mark_confirmed_unprocessed, migrations.RunPython.noop),
    ]
