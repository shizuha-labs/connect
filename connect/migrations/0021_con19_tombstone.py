"""
Migration for CON-19: AccountTombstone and TombstonedBlock models (CON-16 C10).
"""

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0020_merge_presence_onboarding_post_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='AccountTombstone',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('user_id', models.IntegerField(db_index=True, unique=True)),
                ('tombstoned_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('reason', models.CharField(
                    choices=[
                        ('self_deletion', 'User deleted their own account'),
                        ('admin_action', 'Removed by admin/moderation'),
                        ('gdpr_erasure', 'GDPR/privacy erasure request'),
                    ],
                    default='self_deletion',
                    max_length=32,
                )),
            ],
            options={
                'db_table': 'connect_account_tombstone',
                'indexes': [
                    models.Index(fields=['user_id', 'tombstoned_at'], name='tombstone_user_at_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='TombstonedBlock',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('tombstone', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='blocks',
                    to='connect.accounttombstone',
                )),
                ('blocker_id', models.IntegerField(db_index=True)),
                ('blocked_id', models.IntegerField(db_index=True)),
                ('original_block_created_at', models.DateTimeField()),
                ('tombstoned_at', models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={
                'db_table': 'connect_tombstoned_block',
                'unique_together': {('blocker_id', 'blocked_id')},
                'indexes': [
                    models.Index(fields=['blocker_id'], name='tombstoned_block_blocker_idx'),
                    models.Index(fields=['blocked_id'], name='tombstoned_block_blocked_idx'),
                ],
            },
        ),
    ]
