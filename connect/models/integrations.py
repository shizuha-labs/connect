"""External account integrations for Connect (LinkedIn, …).

Secrets stay in k8s / host credential files. This table only stores the
binding from a Connect user to a provider plus non-secret metadata.
"""
import uuid
from django.db import models


class ExternalAccount(models.Model):
    PROVIDERS = [
        ('linkedin', 'LinkedIn'),
    ]
    STATUSES = [
        ('connected', 'Connected'),
        ('disconnected', 'Disconnected'),
        ('error', 'Error'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.IntegerField(db_index=True)
    provider = models.CharField(max_length=32, choices=PROVIDERS)
    status = models.CharField(max_length=20, choices=STATUSES, default='connected')
    email = models.EmailField(blank=True)
    profile_url = models.URLField(blank=True)
    display_name = models.CharField(max_length=200, blank=True)
    secret_ref = models.CharField(
        max_length=200, blank=True,
        help_text='Where the password lives, e.g. k8s:connect-linkedin-agent',
    )
    capabilities = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_external_account'
        unique_together = [('user_id', 'provider')]

    def __str__(self):
        return f'{self.provider}:{self.user_id}'
