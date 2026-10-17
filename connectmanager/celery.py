"""
Celery configuration for Shizuha Connect.
"""

import os
from datetime import timedelta

from celery import Celery
from celery.schedules import crontab

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'connectmanager.settings')

app = Celery('connectmanager')

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Load task modules from all registered Django apps.
app.autodiscover_tasks()

# Celery Beat Schedule
app.conf.beat_schedule = {
    # CON-217: recover committed security events after broker/web-process loss.
    'relay-security-event-outbox': {
        'task': 'connect.tasks.relay_security_event_outbox',
        'schedule': timedelta(seconds=15),
    },
    # Calculate trending topics every 5 minutes
    'calculate-trending-topics': {
        'task': 'connect.tasks.calculate_trending_topics',
        'schedule': crontab(minute='*/5'),
    },
    # Mark missed-heartbeat presence sessions stale/offline every minute.
    'cleanup-stale-presence-sessions': {
        'task': 'connect.tasks.cleanup_stale_presence_sessions',
        'schedule': crontab(minute='*'),
    },
    # Expire old stories every hour
    'expire-stories': {
        'task': 'connect.tasks.expire_stories',
        'schedule': crontab(minute=0),  # Every hour at :00
    },
    # Cleanup old feed items daily
    'cleanup-feed-items': {
        'task': 'connect.tasks.cleanup_old_feed_items',
        'schedule': crontab(hour=3, minute=0),  # 3 AM daily
    },
    # Update profile stats every hour
    'update-profile-stats': {
        'task': 'connect.tasks.batch_update_profile_stats',
        'schedule': crontab(minute=30),  # Every hour at :30
    },
    # PLAT-401: daily standup prompt to the Daily Standup group, weekdays at
    # 09:17 IST (CELERY_TIMEZONE = Asia/Kolkata). Posts as the CoS (shizuha).
    'post-daily-standup-prompt': {
        'task': 'connect.tasks.post_daily_standup_prompt',
        'schedule': crontab(hour=9, minute=17, day_of_week='mon-fri'),
    },

}


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
