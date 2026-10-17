# Generated migration for MessageStar model

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0006_add_message_delivery'),
    ]

    operations = [
        migrations.CreateModel(
            name='MessageStar',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('user_id', models.IntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('message', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='stars', to='connect.directmessage')),
            ],
            options={
                'db_table': 'connect_message_star',
                'unique_together': {('message', 'user_id')},
            },
        ),
    ]
