# Generated for CON-14: persist the JWT-derived tenant on presence sessions so
# the context-free stale-cleanup worker bridges the offline transition to the
# caller's actual org (federated users would otherwise default to org 1).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0016_direct_message_idempotency'),
    ]

    operations = [
        migrations.AddField(
            model_name='presencesession',
            name='organization_id',
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
