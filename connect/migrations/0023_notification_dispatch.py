from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    # CON-15 (CON-6 P2): idempotency ledger for DM notification fanout. Based on the
    # single leaf 0022_merge so the graph stays linear.
    dependencies = [
        ('connect', '0022_merge_con19_tombstone_skills_text'),
    ]

    operations = [
        migrations.CreateModel(
            name='NotificationDispatch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('recipient_id', models.IntegerField(db_index=True)),
                ('event_id', models.BigIntegerField(db_index=True)),
                ('channel', models.CharField(max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('conversation', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notification_dispatches', to='connect.conversation')),
            ],
            options={
                'db_table': 'connect_notification_dispatch',
            },
        ),
        migrations.AddIndex(
            model_name='notificationdispatch',
            index=models.Index(fields=['recipient_id', 'created_at'], name='connect_notif_recip_idx'),
        ),
        migrations.AddConstraint(
            model_name='notificationdispatch',
            constraint=models.UniqueConstraint(fields=('recipient_id', 'conversation', 'event_id', 'channel'), name='unique_notification_dispatch'),
        ),
    ]
