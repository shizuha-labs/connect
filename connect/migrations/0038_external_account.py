from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0037_conversation_last_message_nulls_last'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExternalAccount',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('user_id', models.IntegerField(db_index=True)),
                ('provider', models.CharField(choices=[('linkedin', 'LinkedIn')], max_length=32)),
                ('status', models.CharField(choices=[('connected', 'Connected'), ('disconnected', 'Disconnected'), ('error', 'Error')], default='connected', max_length=20)),
                ('email', models.EmailField(blank=True, max_length=254)),
                ('profile_url', models.URLField(blank=True)),
                ('display_name', models.CharField(blank=True, max_length=200)),
                ('secret_ref', models.CharField(blank=True, help_text='Where the password lives, e.g. k8s:connect-linkedin-agent', max_length=200)),
                ('capabilities', models.JSONField(blank=True, default=list)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('connected_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'connect_external_account',
                'unique_together': {('user_id', 'provider')},
            },
        ),
    ]
