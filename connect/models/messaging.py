"""
Messaging models for Shizuha Connect.

Handles direct messages and group chats.
"""

import uuid
from django.db import models
from django.db.models import F
from django.utils import timezone


class Conversation(models.Model):
    """
    DM conversations (1:1 or group).
    """

    CONVERSATION_TYPES = [
        ('direct', 'Direct Message'),
        ('group', 'Group Chat'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation_type = models.CharField(max_length=10, choices=CONVERSATION_TYPES, default='direct')

    # For group chats
    name = models.CharField(max_length=100, blank=True)
    description = models.TextField(max_length=500, blank=True)
    avatar_url = models.URLField(blank=True)

    # Group settings
    created_by_id = models.IntegerField(null=True, blank=True)

    # Stats
    message_count = models.PositiveIntegerField(default=0)
    last_message_at = models.DateTimeField(null=True, blank=True)
    last_message_preview = models.CharField(max_length=100, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # WhatsApp-style 1:1 uniqueness key. For direct conversations this is
    # "min_uid:max_uid" (stable regardless of which side initiates). For
    # groups this is null. Paired with a partial unique constraint below so
    # concurrent message-sends can't produce two separate DMs between the
    # same pair (TOCTOU race previously visible as "2 chats with System").
    direct_key = models.CharField(max_length=50, null=True, blank=True, db_index=True)

    # CON-429 S2 (design §5/§10 Q2): per-channel digest flag. Three-state —
    # None/OFF until the §7.1 backfill is verified, then ON for system
    # channels. A per-user override (child 3) always wins; this is the
    # channel-level default only.
    digest_enabled = models.BooleanField(null=True, blank=True, default=None)

    class Meta:
        db_table = 'connect_conversation'
        # NULL last_message_at (never-messaged threads) must not sort first.
        ordering = [F('last_message_at').desc(nulls_last=True)]
        constraints = [
            models.UniqueConstraint(
                fields=['direct_key'],
                condition=models.Q(conversation_type='direct'),
                name='unique_direct_conversation_pair',
            ),
        ]

    @staticmethod
    def build_direct_key(user_a_id: int, user_b_id: int) -> str:
        """Canonical key for a 1:1 conversation — order-independent."""
        a, b = sorted([int(user_a_id), int(user_b_id)])
        return f"{a}:{b}"

    def __str__(self):
        if self.name:
            return self.name
        return f"Conversation {self.id}"


class ConversationParticipant(models.Model):
    """Participants in conversations"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='participants')
    user_id = models.IntegerField(db_index=True)
    user_email = models.EmailField(blank=True)
    user_name = models.CharField(max_length=200, blank=True)

    # Participant type (human, AI agent, or system pseudo-user)
    PARTICIPANT_TYPES = [
        ('human', 'Human'),
        ('agent', 'AI Agent'),
        ('system', 'System'),  # Bot/scheduler pseudo-user (always user_id=0)
    ]
    participant_type = models.CharField(
        max_length=10, choices=PARTICIPANT_TYPES, default='human', db_index=True
    )
    agent_id = models.UUIDField(null=True, blank=True, db_index=True)  # Agent UUID from shizuha-agent
    agent_role = models.CharField(max_length=50, blank=True)  # "Engineer", "Architect", etc.

    # For group chats
    is_admin = models.BooleanField(default=False)
    nickname = models.CharField(max_length=50, blank=True)

    # Read status
    last_read_at = models.DateTimeField(null=True, blank=True)
    # Event-log cursor for this participant. Supersedes `last_read_at` as the
    # delivery-correctness primitive: on reconnect, the server replays every
    # DirectMessage with event_id > last_read_event_id. Independent of clock
    # skew, decoupled from retention. `last_read_at` is retained as a UI hint
    # ("last seen 3h ago"). Null on pre-migration rows; handler treats null
    # as "no cursor yet" and falls back to the time-based cutoff.
    # See MESSAGING_KAFKA.md § Schema additions.
    last_read_event_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    unread_count = models.PositiveIntegerField(default=0)

    # Settings
    is_muted = models.BooleanField(default=False)
    muted_until = models.DateTimeField(null=True, blank=True)
    notifications_enabled = models.BooleanField(default=True)

    # Conversation management (per-user)
    is_pinned = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    cleared_at = models.DateTimeField(null=True, blank=True)

    # Status
    has_left = models.BooleanField(default=False)
    left_at = models.DateTimeField(null=True, blank=True)

    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_conversation_participant'
        unique_together = ['conversation', 'user_id']
        indexes = [
            models.Index(fields=['user_id', 'has_left']),
        ]

    def __str__(self):
        return f"User {self.user_id} in {self.conversation}"


class PresenceSession(models.Model):
    """Per-client/session presence heartbeat state.

    Presence is deliberately separate from chat message fanout. WebSocket,
    agent, and bridge surfaces record heartbeats here; aggregation/broadcast
    is derived from these rows rather than from raw message delivery events.
    """

    SESSION_STATUSES = [
        ('online', 'Online'),
        ('away', 'Away'),
        ('degraded', 'Degraded'),
        ('stale', 'Stale'),
        ('offline', 'Offline'),
    ]

    CLIENT_TYPES = [
        ('web', 'Web'),
        ('mobile', 'Mobile'),
        ('agent', 'Agent'),
        ('bridge', 'Bridge'),
        ('unknown', 'Unknown'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.IntegerField(db_index=True)
    session_key = models.CharField(max_length=200, unique=True, db_index=True)
    client_id = models.CharField(max_length=200, blank=True, db_index=True)
    client_type = models.CharField(max_length=20, choices=CLIENT_TYPES, default='unknown', db_index=True)
    client_label = models.CharField(max_length=200, blank=True)
    is_agent = models.BooleanField(default=False, db_index=True)
    status = models.CharField(max_length=20, choices=SESSION_STATUSES, default='online', db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    # Tenant captured from the live JWT at touch time. Persisted so the
    # context-free stale-cleanup worker can publish the offline transition to
    # the caller's actual org bridge channel instead of defaulting federated
    # users (no local organization_id) to org 1 (CON-14 federated-tenant fix).
    organization_id = models.IntegerField(null=True, blank=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    last_heartbeat_at = models.DateTimeField(db_index=True)
    stale_at = models.DateTimeField(null=True, blank=True)
    offline_at = models.DateTimeField(null=True, blank=True)
    channel_name = models.CharField(max_length=255, blank=True)
    # CON-15 B.2 active-conversation-open suppression: the conversation this
    # session is currently foregrounding, written by authenticated dashboard/mobile
    # WS focus/open events and cleared on blur/close. Suppresses a DM notification
    # only while the recipient is actively *viewing* that conversation.
    # `active_conversation_updated_at` gives a freshness TTL so a backgrounded/stale
    # tab that never sent a blur does not suppress forever
    # (notifications.ACTIVE_CONVERSATION_TTL_SECONDS).
    active_conversation_id = models.UUIDField(null=True, blank=True, db_index=True)
    active_conversation_updated_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_presence_session'
        indexes = [
            models.Index(fields=['user_id', 'status']),
            models.Index(fields=['status', 'last_heartbeat_at']),
            models.Index(fields=['client_type', 'status']),
            models.Index(fields=['user_id', 'active_conversation_id'],
                         name='connect_presence_aconv_idx'),
        ]

    def __str__(self):
        return f"PresenceSession user={self.user_id} session={self.session_key} status={self.status}"


class PresenceStatus(models.Model):
    """Deterministic aggregate presence for one user."""

    USER_STATUSES = [
        ('online', 'Online'),
        ('away', 'Away'),
        ('degraded', 'Degraded'),
        ('offline', 'Offline'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id = models.IntegerField(unique=True, db_index=True)
    status = models.CharField(max_length=20, choices=USER_STATUSES, default='offline', db_index=True)
    active_session_count = models.PositiveIntegerField(default=0)
    degraded_session_count = models.PositiveIntegerField(default=0)
    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)
    details = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_presence_status'
        indexes = [
            models.Index(fields=['status', 'updated_at']),
        ]

    def __str__(self):
        return f"PresenceStatus user={self.user_id} status={self.status}"


# CON-429 S2 (design §2): notification taxonomy vocabulary.
NOTIFICATION_CLASSES = [
    ('actionable', 'Actionable'),
    ('informational', 'Informational'),
    ('alert', 'Alert'),
]
NOTIFICATION_STATES = [
    ('unread', 'Unread'),
    ('read', 'Read'),
    ('needs_action', 'Needs Action'),
    ('addressed', 'Addressed'),
    ('dismissed', 'Dismissed'),
    # §7.3: grandfathered pre-taxonomy unread rows — classified for digest
    # correctness but excluded from needs-action counting.
    ('read_eligible', 'Read-Eligible (grandfathered)'),
]
# CON-429 §2.1: server-side allowlist. The ingress legs validate the
# payload-supplied class against this set and REJECT unknown values (never
# coerce), so a producer bug cannot mint an arbitrary class string.
VALID_NOTIFICATION_CLASSES = frozenset({'actionable', 'informational', 'alert'})


class DirectMessage(models.Model):
    """
    Messages in conversations.
    """

    MESSAGE_TYPES = [
        ('text', 'Text'),
        ('image', 'Image'),
        ('video', 'Video'),
        ('file', 'File'),
        ('voice', 'Voice Message'),
        ('system', 'System Message'),
        ('shared_post', 'Shared Post'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    # Event-log cursor — globally monotonic across all conversations, assigned
    # by Postgres on INSERT. Clients store the highest event_id they've
    # ingested; on WS reconnect they send `{"type":"sync", "since_event_id":N}`
    # and the server replays everything with event_id > N.
    #
    # Why BIGSERIAL via PositiveBigIntegerField + db_default: we want Postgres
    # to assign the value at the DB level (atomic, gap-free, no Python-side
    # race). Django 4+ supports db_default with server-side sequences.
    # On pre-migration rows the migration backfills in created_at order.
    # See MESSAGING_KAFKA.md § Schema additions.
    # Declared as nullable BigIntegerField so Django doesn't try to manage the
    # underlying sequence; the migration creates a dedicated Postgres sequence
    # and wires it as the column default (BIGSERIAL-equivalent). On every
    # INSERT, Postgres assigns the next value atomically — no Python-side
    # race. The unique constraint lives in the migration too.
    event_id = models.BigIntegerField(unique=True, null=True, blank=True, editable=False, db_index=True)
    # Per-conversation monotonic counter. Assigned atomically in save() via
    # COALESCE(MAX(conversation_seq_num),0)+1 under a row lock. Gives clients
    # a stable ordering key independent of clock skew. Populates the `seqNum`
    # payload field that all three clients already parse via the ordering
    # tri-key `(seqNum, createdAt, id)`.
    conversation_seq_num = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    sender_id = models.IntegerField(db_index=True)
    sender_email = models.EmailField(blank=True)
    sender_name = models.CharField(max_length=200, blank=True)
    # Canonical shizuha-id username for the sender. Populated at write time
    # from the authenticated user's token claims. Humans and agents both have
    # usernames — this is the primary identifier for inline message display
    # (e.g., "[kai] ..." prefix in ConnectClient) since it's short, stable,
    # and doesn't vary by locale or display-name updates.
    sender_username = models.CharField(max_length=150, blank=True, default='', db_index=True)

    message_type = models.CharField(max_length=20, choices=MESSAGE_TYPES, default='text')
    content = models.TextField()

    # Agent sender fields
    sender_is_agent = models.BooleanField(default=False, db_index=True)
    agent_id = models.UUIDField(null=True, blank=True)

    # For media messages
    media_url = models.URLField(blank=True)
    media_metadata = models.JSONField(default=dict, blank=True)

    # Client-generated idempotency key to prevent duplicate sends on retry
    client_message_id = models.UUIDField(null=True, blank=True, db_index=True)

    # For shared posts
    shared_post_id = models.UUIDField(null=True, blank=True)

    # Reply to
    reply_to = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='replies'
    )

    # Forwarded from
    forwarded_from = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='forwards'
    )

    # Reactions
    reactions = models.JSONField(default=dict, blank=True)  # {emoji: [user_ids]}

    # Status
    is_edited = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    # CON-429 S2 (design §2/§7): sender-side notification taxonomy. Connect
    # has no standalone Notification model — system posts are message rows
    # created via `_create_system_message` / `InternalSystemMessageView`, so
    # the taxonomy lands on the message row. Nullable: DMs are unclassified
    # and unaffected (§8); system-channel posts stamp at the ingress legs
    # (§2.1). `notification_class` is IMMUTABLE AT MINT (§2.2) — lifecycle
    # changes are modeled on the separate mutable `notification_state`
    # (`unread` → `read` / `needs_action` → `addressed` / `dismissed`), and
    # escalation (e.g. informational → alert) is a NEW alert post referencing
    # the original, never a class mutation. These columns are separate from
    # the lease-worthiness inputs (§3.1 seam).
    notification_class = models.CharField(
        max_length=20, choices=NOTIFICATION_CLASSES, null=True, blank=True, db_index=True,
    )
    notification_state = models.CharField(
        max_length=20, choices=NOTIFICATION_STATES, default='unread', db_index=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_direct_message'
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['conversation', 'created_at']),
            models.Index(fields=['sender_id', 'created_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['conversation', 'client_message_id'],
                name='unique_client_message_per_conversation',
                condition=models.Q(client_message_id__isnull=False),
            ),
        ]

    def __str__(self):
        return f"Message from {self.sender_email} in {self.conversation}"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # CON-429 §2.2: stash the mint-time class so save() can enforce
        # immutability without a DB round-trip.
        self._minted_notification_class = self.notification_class

    def save(self, *args, **kwargs):
        if self.pk is not None and self.notification_class != self._minted_notification_class:
            raise ValueError(
                'notification_class is immutable at mint (CON-429 design §2.2); '
                'express lifecycle changes on notification_state, and express '
                'escalation as a new alert post referencing the original'
            )
        return super().save(*args, **kwargs)


class DirectMessageIdempotency(models.Model):
    """Durable idempotency record for direct-message sends.

    A caller-generated ``client_message_id`` identifies one logical send for a
    sender.  The record stores the resolved recipient/conversation and a stable
    request hash so retries can return the original message while conflicting
    re-use of the same client id returns HTTP 409 instead of creating a second
    row or silently replaying the wrong body.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sender_id = models.IntegerField(db_index=True)
    recipient_id = models.IntegerField(db_index=True)
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='message_idempotency')
    client_message_id = models.UUIDField(db_index=True)
    request_hash = models.CharField(max_length=64)
    message = models.OneToOneField(
        DirectMessage,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='idempotency_record',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_direct_message_idempotency'
        constraints = [
            models.UniqueConstraint(
                fields=['sender_id', 'client_message_id'],
                name='unique_dm_idempotency_sender_client',
            ),
        ]
        indexes = [
            models.Index(fields=['sender_id', 'recipient_id', 'client_message_id']),
            models.Index(fields=['conversation', 'client_message_id']),
        ]

    def __str__(self):
        return f"DM idempotency sender={self.sender_id} client_message_id={self.client_message_id}"


class VerifiedAlertDelivery(models.Model):
    """Opaque, exact-org receipt for one deterministic alert-delivery phase."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id = models.PositiveBigIntegerField(db_index=True)
    requester_id = models.IntegerField(db_index=True)
    recipient_id = models.IntegerField(db_index=True)
    client_message_id = models.UUIDField()
    request_hash = models.CharField(max_length=64)
    message = models.OneToOneField(
        DirectMessage, on_delete=models.PROTECT, related_name='verified_alert_delivery'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_verified_alert_delivery'
        constraints = [
            models.UniqueConstraint(
                fields=['requester_id', 'client_message_id'],
                name='unique_verified_alert_request_phase',
            ),
        ]
        indexes = [
            models.Index(fields=['organization_id', 'recipient_id', 'created_at']),
        ]


class MessageRead(models.Model):
    """Track read receipts"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(DirectMessage, on_delete=models.CASCADE, related_name='read_receipts')
    user_id = models.IntegerField()
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_message_read'
        unique_together = ['message', 'user_id']

    def __str__(self):
        return f"User {self.user_id} read message {self.message_id}"


class MessageDelivery(models.Model):
    """Track transport delivery separately from agent-loop processing."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(DirectMessage, on_delete=models.CASCADE, related_name='delivery_receipts')
    user_id = models.IntegerField()
    delivered_at = models.DateTimeField(auto_now_add=True)
    processing_ack_required = models.BooleanField(default=False)
    processed_at = models.DateTimeField(null=True, blank=True)
    processing_attempts = models.PositiveSmallIntegerField(default=0)
    last_processing_attempt_at = models.DateTimeField(null=True, blank=True)
    processing_dead_lettered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'connect_message_delivery'
        unique_together = ['message', 'user_id']
        indexes = [
            models.Index(
                fields=[
                    'user_id',
                    'processing_ack_required',
                    'processed_at',
                    'processing_dead_lettered_at',
                ],
                name='connect_delivery_ack_replay',
            ),
        ]

    def __str__(self):
        return f"User {self.user_id} received message {self.message_id}"


class MessageDeletion(models.Model):
    """Per-user message deletion (delete for me)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.ForeignKey(DirectMessage, on_delete=models.CASCADE, related_name='deletions')
    user_id = models.IntegerField()

    class Meta:
        db_table = 'connect_message_deletion'
        unique_together = ['message', 'user_id']

    def __str__(self):
        return f"User {self.user_id} deleted message {self.message_id}"


class MessageStar(models.Model):
    """User-starred/bookmarked messages."""

    message = models.ForeignKey(DirectMessage, on_delete=models.CASCADE, related_name='stars')
    user_id = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_message_star'
        unique_together = ('message', 'user_id')

    def __str__(self):
        return f"User {self.user_id} starred message {self.message_id}"


class BridgeWebhookSecret(models.Model):
    """Encrypted provider webhook secret for an external bridge account."""

    class Provider(models.TextChoices):
        WHATSAPP = 'whatsapp', 'WhatsApp'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=32, choices=Provider.choices, db_index=True)
    provider_account_id = models.CharField(max_length=128, db_index=True)
    secret_ciphertext = models.TextField()
    key_id = models.CharField(max_length=80, default='default')
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_bridge_webhook_secret'
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'provider_account_id'],
                name='unique_bridge_webhook_secret_account',
            ),
        ]

    def __str__(self):
        return f"{self.provider}:{self.provider_account_id} secret"


class BridgeIdentityMapping(models.Model):
    """Server-owned mapping from a stable provider sender id to Shizuha user id."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=32, choices=BridgeWebhookSecret.Provider.choices, db_index=True)
    provider_account_id = models.CharField(max_length=128, db_index=True)
    external_sender_id = models.CharField(max_length=160, db_index=True)
    mapped_user_id = models.IntegerField(db_index=True)
    mapped_user_email = models.EmailField(blank=True)
    mapped_user_name = models.CharField(max_length=200, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_admin_capable = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_bridge_identity_mapping'
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'provider_account_id', 'external_sender_id'],
                name='unique_bridge_identity_mapping',
            ),
        ]

    def __str__(self):
        return f"{self.provider}:{self.provider_account_id}:{self.external_sender_id} -> {self.mapped_user_id}"


class BridgeInboundEvent(models.Model):
    """Canonical inbound provider event envelope and idempotency/audit record."""

    class Provider(models.TextChoices):
        WHATSAPP = 'whatsapp', 'WhatsApp'

    class Direction(models.TextChoices):
        INBOUND = 'inbound', 'Inbound'

    class SignatureStatus(models.TextChoices):
        VALID = 'valid', 'Valid'
        CONFLICT = 'conflict', 'Payload conflict'

    class Status(models.TextChoices):
        RECEIVED = 'received', 'Received'
        ROUTED = 'routed', 'Routed'
        REPLAYED = 'replayed', 'Idempotent replay'
        CONFLICT = 'conflict', 'Payload conflict'
        UNMAPPED = 'unmapped', 'No identity mapping'
        FORBIDDEN = 'forbidden', 'Identity freshness/admin denied'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bridge_event_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    provider = models.CharField(max_length=32, choices=Provider.choices, db_index=True)
    provider_account_id = models.CharField(max_length=128, db_index=True)
    provider_event_id = models.CharField(max_length=200, db_index=True)
    external_sender_id = models.CharField(max_length=160, db_index=True)
    mapped_user_id = models.IntegerField(null=True, blank=True, db_index=True)
    identity_verified_at = models.DateTimeField(null=True, blank=True)
    conversation_key = models.CharField(max_length=360, db_index=True)
    direction = models.CharField(max_length=16, choices=Direction.choices, default=Direction.INBOUND)
    content_text = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    payload_hash = models.CharField(max_length=64)
    signature_status = models.CharField(
        max_length=16,
        choices=SignatureStatus.choices,
        default=SignatureStatus.VALID,
        db_index=True,
    )
    processing_status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.RECEIVED,
        db_index=True,
    )
    message = models.ForeignKey(
        DirectMessage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='bridge_events',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_bridge_inbound_event'
        indexes = [
            models.Index(fields=['provider', 'provider_account_id', 'external_sender_id']),
            models.Index(fields=['mapped_user_id', 'created_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'provider_account_id', 'provider_event_id'],
                name='unique_bridge_inbound_provider_event',
            ),
        ]

    def __str__(self):
        return f"{self.provider}:{self.provider_account_id}:{self.provider_event_id}"


class BridgeChannelPolicy(models.Model):
    """Explicit task/channel grant required before outbound external sends."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=32, choices=BridgeWebhookSecret.Provider.choices, db_index=True)
    provider_account_id = models.CharField(max_length=128, db_index=True)
    channel_key = models.CharField(max_length=200, db_index=True)
    task_key = models.CharField(max_length=80, db_index=True)
    agent_id = models.IntegerField(db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    admin_approved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    admin_approved_by = models.IntegerField(null=True, blank=True)
    allowed_recipients = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_bridge_channel_policy'
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'provider_account_id', 'channel_key', 'task_key', 'agent_id'],
                name='unique_bridge_channel_policy_scope',
            ),
        ]

    def __str__(self):
        return f"{self.provider}:{self.provider_account_id}:{self.channel_key}:{self.task_key}:{self.agent_id}"


class BridgeOutboundEvent(models.Model):
    """Outbound provider send envelope, idempotency, status, and privacy-safe audit."""

    class Provider(models.TextChoices):
        WHATSAPP = 'whatsapp', 'WhatsApp'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending provider acceptance'
        RETRYING = 'retrying', 'Retrying after provider/backpressure delay'
        ACCEPTED = 'accepted', 'Accepted by provider'
        DELIVERED = 'delivered', 'Delivered'
        FAILED = 'failed', 'Failed'
        DEAD_LETTERED = 'dead_lettered', 'Terminal delivery failure'
        FORBIDDEN = 'forbidden', 'Policy denied'
        RATE_LIMITED = 'rate_limited', 'Rate limited'
        CONFLICT = 'conflict', 'Idempotency conflict'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    outbound_event_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    provider = models.CharField(max_length=32, choices=Provider.choices, db_index=True)
    provider_account_id = models.CharField(max_length=128, db_index=True)
    channel_key = models.CharField(max_length=200, db_index=True)
    task_key = models.CharField(max_length=80, db_index=True)
    agent_id = models.IntegerField(db_index=True)
    recipient_external_id = models.CharField(max_length=200, db_index=True)
    conversation = models.ForeignKey(
        Conversation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='bridge_outbound_events',
    )
    idempotency_key = models.CharField(max_length=200, db_index=True)
    request_hash = models.CharField(max_length=64)
    body_hash = models.CharField(max_length=64)
    provider_delivery_id = models.CharField(max_length=200, blank=True, db_index=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING, db_index=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.CharField(max_length=240, blank=True)
    audit_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_bridge_outbound_event'
        indexes = [
            models.Index(fields=['agent_id', 'created_at']),
            models.Index(fields=['recipient_external_id', 'created_at']),
            models.Index(fields=['provider', 'provider_account_id', 'created_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'provider_account_id', 'idempotency_key'],
                name='unique_bridge_outbound_idempotency',
            ),
        ]

    def __str__(self):
        return f"{self.provider}:{self.provider_account_id}:{self.idempotency_key}"


class NotificationDispatch(models.Model):
    """CON-15 (CON-6 P2): at-least-once dedup ledger for DM notification fanout.

    One row per (recipient, conversation, message-event, channel) that has been
    dispatched. The UNIQUE constraint makes `get_or_create` the idempotency gate —
    a retried/replayed/double-broadcast fanout creates nothing and is skipped, so a
    recipient is notified at most once per message per channel. See
    `connect.services.notifications`.
    """
    recipient_id = models.IntegerField(db_index=True)
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='notification_dispatches')
    # The DirectMessage.event_id (globally-monotonic per-message ordering id).
    event_id = models.BigIntegerField(db_index=True)
    channel = models.CharField(max_length=20)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'connect_notification_dispatch'
        constraints = [
            models.UniqueConstraint(
                fields=['recipient_id', 'conversation', 'event_id', 'channel'],
                name='unique_notification_dispatch',
            ),
        ]
        indexes = [
            # Explicit name MUST match migration 0023 (kai P2): without it Django
            # auto-names the Meta index, drifting from the migration state and
            # tripping a spurious RenameIndex on `makemigrations --check`.
            models.Index(fields=['recipient_id', 'created_at'], name='connect_notif_recip_idx'),
        ]

    def __str__(self):
        return f"dispatch r={self.recipient_id} ev={self.event_id} ch={self.channel}"


class NotificationPreference(models.Model):
    """CON-15 B.2 (CON-6 P2): per-user notification preferences — quiet hours +
    per-channel enablement.

    One row per user. `quiet_*` define a timezone-aware daily window during which
    the *interrupt* channels (mobile / external bridge / agent inbox) are
    suppressed; the in-app dashboard stream is NOT suppressed by quiet hours (it
    is non-interrupting and still records idempotent dispatch state). The window
    is evaluated in `timezone` (an IANA name) and may cross midnight
    (quiet_start > quiet_end wraps). Per-channel booleans are an always-on opt-out
    independent of quiet hours. See `connect.services.notifications`.
    """
    user_id = models.IntegerField(unique=True, db_index=True)
    timezone = models.CharField(max_length=64, default='UTC')

    quiet_hours_enabled = models.BooleanField(default=False)
    quiet_start = models.TimeField(null=True, blank=True)
    quiet_end = models.TimeField(null=True, blank=True)

    # Per-channel opt-out (default: all channels enabled).
    dashboard_enabled = models.BooleanField(default=True)
    mobile_enabled = models.BooleanField(default=True)
    external_enabled = models.BooleanField(default=True)
    agent_enabled = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_notification_preference'

    def channel_enabled(self, channel):
        """Per-channel opt-out check; unknown channels default to enabled."""
        return {
            'dashboard': self.dashboard_enabled,
            'mobile': self.mobile_enabled,
            'external': self.external_enabled,
            'agent': self.agent_enabled,
        }.get(channel, True)

    def __str__(self):
        return f"NotificationPreference user={self.user_id} quiet={self.quiet_hours_enabled}"


class BridgeDeliveryCooldown(models.Model):
    """CON-15 B.2 (CON-6 P2): per-recipient/per-bridge-channel delivery cooldown.

    External-bridge fanout (telegram/discord/whatsapp relay) records consecutive
    failures here and sets `cooldown_until` with exponential backoff, so a flapping
    or down bridge is not hammered once per message. The `external` channel
    suppressor skips a recipient whose matching bridge cooldown is still active.
    A successful delivery resets the row. Keyed UNIQUE on (recipient, bridge_channel).
    """
    recipient_id = models.IntegerField(db_index=True)
    # Bridge channel identifier the cooldown applies to (e.g. the bridge platform
    # / org channel key). Matched against the message's target external channel.
    bridge_channel = models.CharField(max_length=128)

    consecutive_failures = models.IntegerField(default=0)
    cooldown_until = models.DateTimeField(null=True, blank=True, db_index=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_bridge_delivery_cooldown'
        constraints = [
            models.UniqueConstraint(
                fields=['recipient_id', 'bridge_channel'],
                name='unique_bridge_cooldown',
            ),
        ]
        indexes = [
            models.Index(fields=['recipient_id', 'cooldown_until'],
                         name='connect_bridge_cd_recip_idx'),
        ]

    def __str__(self):
        return (f"BridgeCooldown r={self.recipient_id} ch={self.bridge_channel} "
                f"until={self.cooldown_until} fails={self.consecutive_failures}")


class SecurityPipelineMetricHealth(models.Model):
    """Independent liveness record for worker-side SIEM metric write failures.

    Worker stage counters normally live in Redis so web pods can expose them.
    A Redis HINCRBY failure cannot report itself through that same failed path,
    so this tiny Postgres record is the independent fail-loud channel.
    """

    channel = models.CharField(max_length=32, unique=True, default='redis')
    failure_count = models.PositiveBigIntegerField(default=0)
    last_failure_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error_class = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_security_pipeline_metric_health'

    def __str__(self):
        return f"SecurityMetricHealth {self.channel} failures={self.failure_count}"


class SecurityEventOutbox(models.Model):
    """Durable acceptance record for signed internal security events.

    A 202 response is emitted only after this row commits.  Celery is an
    at-least-once wake-up mechanism; this Postgres row owns identity, retry and
    terminal-delivery truth.
    """

    class Status(models.TextChoices):
        ACCEPTED = 'accepted', 'Accepted'
        RETRYING = 'retrying', 'Retrying'
        PROCESSING = 'processing', 'Processing'
        SENT = 'sent', 'Sent'
        SUPPRESSED_NO_ANOMALY = 'suppressed_no_anomaly', 'Suppressed: no anomaly'
        SUPPRESSED_REPEAT = 'suppressed_repeat', 'Suppressed: semantic repeat'
        DISABLED = 'disabled', 'Disabled'
        NO_RECIPIENT = 'no_recipient', 'No recipient'
        DEAD_LETTERED = 'dead_lettered', 'Dead lettered'

    TERMINAL_STATUSES = frozenset({
        Status.SENT,
        Status.SUPPRESSED_NO_ANOMALY,
        Status.SUPPRESSED_REPEAT,
        Status.DISABLED,
        Status.NO_RECIPIENT,
        Status.DEAD_LETTERED,
    })

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    producer_service = models.CharField(max_length=64)
    producer_event_id = models.CharField(max_length=128)
    schema_version = models.CharField(max_length=16, default='1.0')
    event_type = models.CharField(max_length=128)
    payload = models.JSONField()
    request_hash = models.CharField(max_length=64)
    semantic_fingerprint = models.CharField(max_length=64, blank=True, db_index=True)

    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.ACCEPTED,
    )
    publish_attempts = models.PositiveIntegerField(default=0)
    delivery_attempts = models.PositiveIntegerField(default=0)
    conflict_count = models.PositiveIntegerField(default=0)
    next_dispatch_at = models.DateTimeField(default=timezone.now)
    lease_owner = models.CharField(max_length=64, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    accepted_at = models.DateTimeField(auto_now_add=True)
    first_published_at = models.DateTimeField(null=True, blank=True)
    last_published_at = models.DateTimeField(null=True, blank=True)
    terminal_at = models.DateTimeField(null=True, blank=True)
    last_error_class = models.CharField(max_length=64, blank=True)
    terminal_reason = models.CharField(max_length=128, blank=True)
    terminal_message = models.OneToOneField(
        DirectMessage,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='security_event_outbox',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_security_event_outbox'
        constraints = [
            models.UniqueConstraint(
                fields=['producer_service', 'producer_event_id'],
                name='unique_security_event_producer_identity',
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'next_dispatch_at'], name='connect_sec_out_due_idx'),
            models.Index(fields=['status', 'accepted_at'], name='connect_sec_out_age_idx'),
        ]

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL_STATUSES


class SecurityEventRepeatClaim(models.Model):
    """Postgres-owned semantic repeat window, distinct from event identity."""

    fingerprint = models.CharField(max_length=64, primary_key=True)
    owner_outbox = models.ForeignKey(
        SecurityEventOutbox,
        on_delete=models.CASCADE,
        related_name='repeat_claims',
    )
    expires_at = models.DateTimeField(db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_security_event_repeat_claim'


class SecurityEventRelayHealth(models.Model):
    """Independent Postgres liveness signal for the recovery relay."""

    channel = models.CharField(max_length=32, unique=True, default='security_outbox')
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    failure_count = models.PositiveBigIntegerField(default=0)
    last_error_class = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'connect_security_event_relay_health'
