from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('connect', '0020_merge_presence_author_username'),
    ]

    operations = [
        migrations.AddField(
            model_name='socialprofile',
            name='skills_text',
            field=models.TextField(blank=True, default=''),
        ),
    ]
