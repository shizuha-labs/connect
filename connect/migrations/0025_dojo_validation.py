# Generated for DOJO-7 central waitlist / validation events.

import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('connect', '0024_notification_prefs_bridge_cooldown_active_conv'),
    ]

    operations = [
        migrations.CreateModel(
            name='DojoWaitlistSignup',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=160)),
                ('email', models.EmailField(db_index=True, max_length=254, unique=True)),
                ('role', models.CharField(choices=[('software-engineer', 'Software engineer'), ('frontend', 'Frontend engineer'), ('backend', 'Backend engineer'), ('data-ml', 'Data / ML engineer'), ('product-case', 'Product / case interview')], max_length=40)),
                ('stage', models.CharField(choices=[('student', 'Student / fresher'), ('job-switch', 'Job switcher'), ('final-round', 'Final round soon'), ('campus-placement', 'Campus placement')], max_length=40)),
                ('goal', models.TextField(blank=True)),
                ('source', models.CharField(db_index=True, default='connect-dojo-waitlist', max_length=80)),
                ('visitor_id', models.CharField(blank=True, db_index=True, max_length=80)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'connect_dojo_waitlist_signup',
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['role', 'stage'], name='connect_doj_role_cde5a1_idx'), models.Index(fields=['source', '-created_at'], name='connect_doj_source_934209_idx')],
            },
        ),
        migrations.CreateModel(
            name='DojoValidationEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('event_type', models.CharField(choices=[('visit', 'Visit'), ('signup', 'Signup'), ('qualified', 'Qualified signup'), ('reply', 'Qualitative reply'), ('cta_click', 'CTA click')], db_index=True, max_length=40)),
                ('email', models.EmailField(blank=True, db_index=True, max_length=254)),
                ('source', models.CharField(db_index=True, default='connect-dojo-waitlist', max_length=80)),
                ('visitor_id', models.CharField(blank=True, db_index=True, max_length=80)),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('occurred_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('signup', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='validation_events', to='connect.dojowaitlistsignup')),
            ],
            options={
                'db_table': 'connect_dojo_validation_event',
                'ordering': ['-occurred_at'],
                'indexes': [models.Index(fields=['event_type', '-occurred_at'], name='connect_doj_event_t_fcbdde_idx'), models.Index(fields=['source', '-occurred_at'], name='connect_doj_source_a9b6ee_idx'), models.Index(fields=['visitor_id', 'event_type'], name='connect_doj_visitor_cc0873_idx')],
            },
        ),
    ]
