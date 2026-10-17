from django.db import migrations
from django.db.models.expressions import F, OrderBy


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0036_alter_jobapplication_company'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='conversation',
            options={
                'ordering': [
                    OrderBy(F('last_message_at'), descending=True, nulls_last=True),
                ],
            },
        ),
    ]
