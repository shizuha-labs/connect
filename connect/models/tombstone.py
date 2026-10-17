"""
CON-19: Tombstone schema for deleted accounts (CON-16 C10).

When an account is deleted, its blocks and mutes are tombstoned — not removed.
This prevents re-contact via recreated accounts and preserves safety state.
"""

import uuid
from django.db import models
from django.utils import timezone


class AccountTombstone(models.Model):
    """
    Records that a user account has been deleted (C10).

    A tombstone preserves the user's block policy so that their safety choices
    (blocks, mutes) survive account deletion and remain effective against
    harassment from re-registered accounts.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # user_id is the deleted account's numeric ID (not a FK — account is gone).
    user_id = models.IntegerField(unique=True, db_index=True)
    tombstoned_at = models.DateTimeField(default=timezone.now)
    # reason captures how the account was removed (self-deletion vs admin action).
    REASON_CHOICES = [
        ('self_deletion', 'User deleted their own account'),
        ('admin_action', 'Removed by admin/moderation'),
        ('gdpr_erasure', 'GDPR/privacy erasure request'),
    ]
    reason = models.CharField(max_length=32, choices=REASON_CHOICES, default='self_deletion')

    class Meta:
        db_table = 'connect_account_tombstone'
        indexes = [
            models.Index(fields=['user_id', 'tombstoned_at'], name='tombstone_user_at_idx'),
        ]

    def __str__(self) -> str:
        return f"AccountTombstone(user_id={self.user_id}, reason={self.reason})"


class TombstonedBlock(models.Model):
    """
    A block relationship belonging to a deleted (tombstoned) account (C10).

    When a user's account is deleted, their Block rows are migrated here so
    that: (a) the block is not silently erased, and (b) if either party
    re-registers, the block can be reinstated immediately.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tombstone = models.ForeignKey(
        AccountTombstone, on_delete=models.PROTECT, related_name='blocks',
        db_index=True,
    )
    blocker_id = models.IntegerField(db_index=True)
    blocked_id = models.IntegerField(db_index=True)
    original_block_created_at = models.DateTimeField()
    tombstoned_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'connect_tombstoned_block'
        unique_together = ['blocker_id', 'blocked_id']
        indexes = [
            models.Index(fields=['blocker_id'], name='tombstoned_block_blocker_idx'),
            models.Index(fields=['blocked_id'], name='tombstoned_block_blocked_idx'),
        ]

    def __str__(self) -> str:
        return f"TombstonedBlock({self.blocker_id} → {self.blocked_id})"
