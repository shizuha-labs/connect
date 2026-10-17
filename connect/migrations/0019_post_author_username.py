# Migration for CON-41: add author_username + author_display_name to Post
# and backfill both from SocialProfile.

from django.db import migrations, models


def backfill_post_author_fields(apps, schema_editor):
    Post = apps.get_model('connect', 'Post')
    SocialProfile = apps.get_model('connect', 'SocialProfile')

    profile_map = {
        row[0]: (row[1], row[2])
        for row in SocialProfile.objects.values_list('user_id', 'username', 'display_name')
    }

    batch = []
    for post in Post.objects.filter(author_username='').iterator(chunk_size=500):
        entry = profile_map.get(post.author_id)
        if entry:
            post.author_username, post.author_display_name = entry
            batch.append(post)
        if len(batch) >= 500:
            Post.objects.bulk_update(batch, ['author_username', 'author_display_name'])
            batch = []

    if batch:
        Post.objects.bulk_update(batch, ['author_username', 'author_display_name'])


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0018_ensure_skill_tables'),
    ]

    operations = [
        migrations.AddField(
            model_name='post',
            name='author_username',
            field=models.CharField(blank=True, default='', max_length=150),
        ),
        migrations.AddField(
            model_name='post',
            name='author_display_name',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.RunPython(backfill_post_author_fields, migrations.RunPython.noop),
    ]
