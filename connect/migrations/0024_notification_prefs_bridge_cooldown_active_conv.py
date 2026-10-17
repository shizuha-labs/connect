from django.db import migrations, models


class Migration(migrations.Migration):
    # CON-15 B.2 (CON-6 P2): the three remaining HLD suppression data sources —
    # active-conversation-open (PresenceSession focus fields), quiet hours
    # (NotificationPreference), and external-bridge cooldown (BridgeDeliveryCooldown).
    # Single B.2 migration per kai's ruling. Based on the single leaf 0023.
    dependencies = [
        ('connect', '0023_notification_dispatch'),
    ]

    operations = [
        # --- active-conversation-open: PresenceSession focus fields ---
        migrations.AddField(
            model_name='presencesession',
            name='active_conversation_id',
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='presencesession',
            name='active_conversation_updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='presencesession',
            index=models.Index(fields=['user_id', 'active_conversation_id'],
                               name='connect_presence_aconv_idx'),
        ),
        # --- quiet hours + per-channel enablement: NotificationPreference ---
        migrations.CreateModel(
            name='NotificationPreference',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('user_id', models.IntegerField(db_index=True, unique=True)),
                ('timezone', models.CharField(default='UTC', max_length=64)),
                ('quiet_hours_enabled', models.BooleanField(default=False)),
                ('quiet_start', models.TimeField(blank=True, null=True)),
                ('quiet_end', models.TimeField(blank=True, null=True)),
                ('dashboard_enabled', models.BooleanField(default=True)),
                ('mobile_enabled', models.BooleanField(default=True)),
                ('external_enabled', models.BooleanField(default=True)),
                ('agent_enabled', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'connect_notification_preference',
            },
        ),
        # --- external-bridge cooldown: BridgeDeliveryCooldown ---
        migrations.CreateModel(
            name='BridgeDeliveryCooldown',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('recipient_id', models.IntegerField(db_index=True)),
                ('bridge_channel', models.CharField(max_length=128)),
                ('consecutive_failures', models.IntegerField(default=0)),
                ('cooldown_until', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('last_failure_at', models.DateTimeField(blank=True, null=True)),
                ('last_success_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'connect_bridge_delivery_cooldown',
            },
        ),
        migrations.AddIndex(
            model_name='bridgedeliverycooldown',
            index=models.Index(fields=['recipient_id', 'cooldown_until'],
                               name='connect_bridge_cd_recip_idx'),
        ),
        migrations.AddConstraint(
            model_name='bridgedeliverycooldown',
            constraint=models.UniqueConstraint(fields=('recipient_id', 'bridge_channel'),
                                               name='unique_bridge_cooldown'),
        ),
    ]
