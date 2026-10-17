"""
WebSocket consumers for Shizuha Connect.

Handles real-time updates for:
- Feed notifications
- Chat messages
- Typing indicators
- Online presence

Cross-service bridge:
- Presence events are published to Redis for shizuha-agent to forward to mobile clients
- Chat events are published to Redis for unified WebSocket delivery
"""

import hashlib
import hmac
import json
import logging
import asyncio
import time
import uuid
import redis
from datetime import datetime, timedelta
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.db import models
from django.utils import timezone

from connect.services.backpressure import WS_CLOSE_CODE_OVERLOAD, WS_SEND_TIMEOUT_SECONDS
from connect.metrics import (
    observe_dm_replay_per_reconnect,
    record_dm_live_delivery,
    record_dm_replay_capped,
    record_dm_replay_fanout,
    user_role_label,
)
from connect.services.presence import (
    mark_offline, refresh_presence, set_active_conversation, touch_presence,
)
from connect.services.privacy import (
    actor_can_reach_recipient,
    can_access_conversation,
    visible_conversation_ids_for_user,
)

logger = logging.getLogger(__name__)


def _sender_same_org(scope, sender_id) -> bool:
    """Return authoritative same-org provenance; unknown always fails open."""
    from connect.services.org_tenancy import get_user_org_ids, org_ids_from_token
    recipient_org_ids = org_ids_from_token((scope or {}).get('token'))
    sender_org_ids = get_user_org_ids(sender_id)
    return bool(recipient_org_ids and sender_org_ids
                and recipient_org_ids.intersection(sender_org_ids))


def _get_bridge_redis():
    """Get a Redis connection for cross-service bridge pub/sub."""
    try:
        return redis.from_url(
            settings.CHANNEL_REDIS_URL,
            socket_timeout=settings.CHANNEL_REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout=settings.CHANNEL_REDIS_CONNECT_TIMEOUT,
        )
    except Exception:
        return None


def _publish_bridge(channel: str, data: dict):
    """Publish an event to the cross-service Redis bridge (fire-and-forget)."""
    try:
        r = _get_bridge_redis()
        if r:
            r.publish(channel, json.dumps(data))
            r.close()
    except Exception:
        logger.debug("Failed to publish bridge event to %s", channel)


async def _publish_bridge_async(channel: str, data: dict):
    await database_sync_to_async(_publish_bridge, thread_sensitive=False)(channel, data)


def _publish_bridge_background(channel: str, data: dict):
    asyncio.create_task(_publish_bridge_async(channel, data))


AGENT_TELEMETRY_KEY = "shizuha:agent_telemetry:{username}"
AGENT_TELEMETRY_INDEX = "shizuha:agent_telemetry:index"
AGENT_TELEMETRY_TTL = 300  # seconds — a snapshot older than this means the agent stopped reporting
DEFAULT_DM_REPLAY_LIMIT = 200
DEFAULT_DM_REPLAY_WINDOW_DAYS = 7
DEFAULT_AGENT_DM_REPLAY_LIMIT = 25


def _replay_limits_for(is_agent: bool) -> tuple[int, datetime]:
    """Return replay row cap and oldest allowed timestamp for reconnect replay.

    PLAT-9268: agents keep a tight row CAP (every replayed DM becomes a new
    LLM turn, and CON-231's drain-across-reconnects bounds the per-reconnect
    spawn), but the oldest-allowed FLOOR is the human-scale day window for
    both classes. The previous 15-minute agent floor was the silent-loss
    mechanism: any WebSocket outage longer than the window permanently
    orphaned messages stored during it (observed 09-19: two DMs sent ~50
    minutes before the recipient's reconnect were never replayed and never
    delivered, while later messages were). An undelivered message is not
    stale — it was never seen; staleness filtering is the recipient runtime's
    coalescing job, and the cap bounds the turn cost.
    """
    if is_agent:
        raw_limit = getattr(settings, 'CONNECT_AGENT_DM_REPLAY_LIMIT', DEFAULT_AGENT_DM_REPLAY_LIMIT)
        raw_window = getattr(settings, 'CONNECT_DM_REPLAY_WINDOW_DAYS', DEFAULT_DM_REPLAY_WINDOW_DAYS)
        default_limit = DEFAULT_AGENT_DM_REPLAY_LIMIT
        default_window = DEFAULT_DM_REPLAY_WINDOW_DAYS
        window_unit = 'days'
    else:
        raw_limit = getattr(settings, 'CONNECT_DM_REPLAY_LIMIT', DEFAULT_DM_REPLAY_LIMIT)
        raw_window = getattr(settings, 'CONNECT_DM_REPLAY_WINDOW_DAYS', DEFAULT_DM_REPLAY_WINDOW_DAYS)
        default_limit = DEFAULT_DM_REPLAY_LIMIT
        default_window = DEFAULT_DM_REPLAY_WINDOW_DAYS
        window_unit = 'days'

    try:
        limit = max(0, int(raw_limit))
    except (TypeError, ValueError):
        limit = default_limit
    try:
        window = max(0, float(raw_window))
    except (TypeError, ValueError):
        window = default_window

    if window_unit == 'seconds':
        floor = timezone.now() - timedelta(seconds=window)
    else:
        floor = timezone.now() - timedelta(days=window)
    return limit, floor


def _store_agent_telemetry(username: str, payload: dict):
    """Persist the latest telemetry snapshot for an agent (TTL'd) + register it in
    an index set so the REST view can enumerate agents without a KEYS scan. The
    snapshot itself carries health.recent_errors, so proactive surfacing reads the
    snapshots rather than a separate (dupe-prone) error stream."""
    try:
        r = _get_bridge_redis()
        if not r:
            return
        rec = dict(payload or {})
        rec['agent_username'] = username
        rec['received_at'] = int(time.time())
        r.setex(AGENT_TELEMETRY_KEY.format(username=username), AGENT_TELEMETRY_TTL, json.dumps(rec))
        r.sadd(AGENT_TELEMETRY_INDEX, username)
        r.close()
    except Exception:
        logger.debug("Failed to store agent telemetry for %s", username)


def _read_all_agent_telemetry():
    """Return {username: snapshot} for every agent with a live (un-expired)
    telemetry snapshot. Used by the internal REST endpoint Hive reads."""
    out = {}
    try:
        r = _get_bridge_redis()
        if not r:
            return out
        members = r.smembers(AGENT_TELEMETRY_INDEX) or []
        for m in members:
            u = m.decode() if isinstance(m, (bytes, bytearray)) else str(m)
            raw = r.get(AGENT_TELEMETRY_KEY.format(username=u))
            if raw:
                try:
                    out[u] = json.loads(raw)
                except Exception:
                    pass
            else:
                # snapshot expired — drop from the index so it doesn't linger
                r.srem(AGENT_TELEMETRY_INDEX, u)
        r.close()
    except Exception:
        logger.debug("Failed to read agent telemetry index")
    return out


def _presence_event_from_snapshot(snapshot):
    event = snapshot.as_event()
    event['type'] = 'presence_update'
    return event


def _org_id_from_scope(scope, user) -> int:
    """Resolve the caller's organization id for the presence Redis bridge.

    Prefer the validated JWT's `organization_id` claim (stashed on the channels
    scope by JWTAuthMiddleware). Federated users have no `organization_id`
    attribute, so a `getattr(user, ...)` lookup wrongly defaults every federated
    socket to org 1 and cross-publishes their presence into the wrong tenant.
    Fall back to the user attribute (local users), then org 1.
    """
    token = scope.get('token') if scope else None
    if token is not None:
        try:
            org = token.get('organization_id')
        except Exception:
            org = None
        if org is not None:
            return org
    return getattr(user, 'organization_id', None) or 1


class ConnectConsumer(AsyncWebsocketConsumer):
    """
    Main WebSocket consumer for Connect notifications.
    Handles:
    - New post notifications
    - Reaction updates
    - Follow notifications
    - Online presence
    """

    async def connect(self):
        import asyncio
        self.user = self.scope.get('user', AnonymousUser())
        self._keepalive_task = None

        if self.user.is_authenticated:
            self.presence_session_key = f'connect:{self.channel_name}'
            # Join personal notification channel
            self.user_group = f'user_{self.user.id}'
            await self.channel_layer.group_add(
                self.user_group,
                self.channel_name
            )

            # Join global presence channel
            await self.channel_layer.group_add(
                'presence_global',
                self.channel_name
            )

            await self.accept()
            await self._update_last_active()
            snapshot = await self._touch_presence('online')

            # Broadcast online status
            await self.channel_layer.group_send(
                'presence_global',
                _presence_event_from_snapshot(snapshot)
            )

            # Publish to Redis bridge for shizuha-agent unified WS
            org_id = _org_id_from_scope(self.scope, self.user)
            _publish_bridge_background(
                f"shizuha:presence:{org_id}",
                snapshot.as_event()
            )
            # Passive notification sockets may never send JSON ping frames; keep
            # their presence fresh so the stale cleanup does not mark a live socket
            # degraded/offline.
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        else:
            await self.close()

    async def disconnect(self, close_code):
        if getattr(self, '_keepalive_task', None):
            self._keepalive_task.cancel()
        if hasattr(self, 'user_group'):
            await self.channel_layer.group_discard(
                self.user_group,
                self.channel_name
            )

        if self.user.is_authenticated:
            await self._update_last_active()
            snapshot = await self._mark_presence_offline()

            await self.channel_layer.group_discard(
                'presence_global',
                self.channel_name
            )

            # Broadcast offline status
            await self.channel_layer.group_send(
                'presence_global',
                _presence_event_from_snapshot(snapshot)
            )

            # Publish to Redis bridge for shizuha-agent unified WS
            org_id = _org_id_from_scope(self.scope, self.user)
            _publish_bridge_background(
                f"shizuha:presence:{org_id}",
                snapshot.as_event()
            )

    async def _keepalive_loop(self):
        import asyncio
        try:
            while True:
                await asyncio.sleep(30)
                try:
                    await self.send(text_data=json.dumps({'type': 'ping'}))
                    await self._refresh_presence()
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    @database_sync_to_async
    def _update_last_active(self):
        from connect.models import SocialProfile
        SocialProfile.objects.filter(user_id=self.user.id).update(last_active_at=timezone.now())

    @database_sync_to_async
    def _touch_presence(self, status='online'):
        return touch_presence(
            user_id=self.user.id,
            session_key=self.presence_session_key,
            client_id=self.channel_name,
            client_type='web',
            channel_name=self.channel_name,
            organization_id=_org_id_from_scope(self.scope, self.user),
            status=status,
        )

    @database_sync_to_async
    def _refresh_presence(self):
        return refresh_presence(
            user_id=self.user.id,
            session_key=self.presence_session_key,
            client_id=self.channel_name,
            client_type='web',
            channel_name=self.channel_name,
        )

    @database_sync_to_async
    def _mark_presence_offline(self):
        return mark_offline(
            user_id=self.user.id,
            session_key=getattr(self, 'presence_session_key', f'connect:{self.channel_name}'),
        )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            message_type = data.get('type')

            handlers = {
                'ping': self.handle_ping,
                'subscribe_post': self.handle_subscribe_post,
                'unsubscribe_post': self.handle_unsubscribe_post,
                # CON-15 B.2: active-conversation focus signal for notification
                # suppression (recipient is foreground-viewing a conversation).
                'focus_conversation': self.handle_focus_conversation,
                'blur_conversation': self.handle_blur_conversation,
            }

            handler = handlers.get(message_type)
            if handler:
                await handler(data)

        except json.JSONDecodeError:
            logger.error("Invalid JSON received")

    async def handle_ping(self, data):
        status_value = data.get('status') if data.get('status') in {'online', 'away', 'degraded'} else 'online'
        snapshot = await self._touch_presence(status_value)
        await self.channel_layer.group_send(
            'presence_global',
            _presence_event_from_snapshot(snapshot)
        )
        org_id = _org_id_from_scope(self.scope, self.user)
        _publish_bridge_background(
            f"shizuha:presence:{org_id}",
            snapshot.as_event()
        )
        await self.send(text_data=json.dumps({'type': 'pong'}))

    async def handle_focus_conversation(self, data):
        """CON-15 B.2: the client foregrounded a conversation — record it so DM
        notifications for that conversation are suppressed while the user is
        actively viewing it. Refresh presence so the focus row's freshness TTL is
        anchored to a live session."""
        conversation_id = self._parse_uuid(data.get('conversation_id'))
        if conversation_id is None:
            return
        await self._touch_presence('online')
        await self._set_active_conversation(conversation_id)

    async def handle_blur_conversation(self, data):
        """CON-15 B.2: the client backgrounded/closed the conversation — clear the
        focus so notifications resume."""
        await self._set_active_conversation(None)

    @staticmethod
    def _parse_uuid(value):
        if not value:
            return None
        try:
            return uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return None

    @database_sync_to_async
    def _set_active_conversation(self, conversation_id):
        return set_active_conversation(
            user_id=self.user.id,
            session_key=self.presence_session_key,
            conversation_id=conversation_id,
        )

    async def handle_subscribe_post(self, data):
        post_id = data.get('post_id')
        if post_id:
            await self.channel_layer.group_add(
                f'post_{post_id}',
                self.channel_name
            )

    async def handle_unsubscribe_post(self, data):
        post_id = data.get('post_id')
        if post_id:
            await self.channel_layer.group_discard(
                f'post_{post_id}',
                self.channel_name
            )

    # Channel layer message handlers
    async def new_post_notification(self, event):
        await self.send(text_data=json.dumps({
            'type': 'new_post',
            'post': event['post_data']
        }))

    async def reaction_update(self, event):
        await self.send(text_data=json.dumps({
            'type': 'reaction_update',
            'post_id': event['post_id'],
            'reactions': event['reactions']
        }))

    async def new_follower(self, event):
        await self.send(text_data=json.dumps({
            'type': 'new_follower',
            'follower': event['follower_data']
        }))

    async def new_comment(self, event):
        await self.send(text_data=json.dumps({
            'type': 'new_comment',
            'comment': event['comment_data']
        }))

    async def agent_config_update(self, event):
        # HIVE-195: relay a Hive-pushed config update down to the agent's bridge WS.
        await self.send(text_data=json.dumps({
            'type': 'agent_config_update',
            'config': event.get('config', {}),
        }))

    async def presence_update(self, event):
        await self.send(text_data=json.dumps({
            'type': 'presence',
            'user_id': event['user_id'],
            'status': event['status'],
            'active_session_count': event.get('active_session_count', 0),
            'degraded_session_count': event.get('degraded_session_count', 0),
            'last_seen_at': event.get('last_seen_at'),
        }))


class FeedConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time feed updates.
    """

    async def connect(self):
        self.user = self.scope.get('user', AnonymousUser())

        if self.user.is_authenticated:
            self.feed_group = f'feed_{self.user.id}'
            await self.channel_layer.group_add(
                self.feed_group,
                self.channel_name
            )
            await self.accept()
        else:
            await self.close()

    async def disconnect(self, close_code):
        if hasattr(self, 'feed_group'):
            await self.channel_layer.group_discard(
                self.feed_group,
                self.channel_name
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            message_type = data.get('type')

            if message_type == 'ping':
                await self.send(text_data=json.dumps({'type': 'pong'}))

        except json.JSONDecodeError:
            pass

    async def new_feed_item(self, event):
        await self.send(text_data=json.dumps({
            'type': 'new_feed_item',
            'post': event['post_data'],
            'reason': event.get('reason', 'followed_user')
        }))


class ChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time chat.
    """

    async def connect(self):
        self.user = self.scope.get('user', AnonymousUser())
        self.conversation_id = self.scope['url_route']['kwargs']['conversation_id']

        if self.user.is_authenticated:
            # Verify user is participant
            is_participant = await self.check_participant()
            if not is_participant:
                await self.close()
                return

            self.room_group = f'chat_{self.conversation_id}'
            await self.channel_layer.group_add(
                self.room_group,
                self.channel_name
            )
            await self.accept()

            # Notify others user joined
            await self.channel_layer.group_send(
                self.room_group,
                {
                    'type': 'user_joined',
                    'user_id': self.user.id
                }
            )

            # Push messages created while this user was offline
            await self._push_missed_messages()

            # Start bridge command listener for commands from agent unified WS
            import asyncio
            self._bridge_listener_task = asyncio.create_task(
                self._listen_bridge_commands()
            )
        else:
            await self.close()

    async def disconnect(self, close_code):
        # Cancel bridge listener
        import asyncio
        bridge_task = getattr(self, '_bridge_listener_task', None)
        if bridge_task and not bridge_task.done():
            bridge_task.cancel()

        if hasattr(self, 'room_group'):
            # Clear typing state so others don't see stale indicator
            try:
                await self.channel_layer.group_send(
                    self.room_group,
                    {
                        'type': 'typing_indicator',
                        'user_id': self.user.id,
                        'is_typing': False,
                    }
                )
            except Exception:
                pass

            # Notify others user left
            await self.channel_layer.group_send(
                self.room_group,
                {
                    'type': 'user_left',
                    'user_id': self.user.id
                }
            )

            await self.channel_layer.group_discard(
                self.room_group,
                self.channel_name
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            message_type = data.get('type')

            handlers = {
                'ping': self.handle_ping,
                'typing_start': self.handle_typing_start,
                'typing_stop': self.handle_typing_stop,
                'message_read': self.handle_message_read,
            }

            handler = handlers.get(message_type)
            if handler:
                await handler(data)

        except json.JSONDecodeError:
            pass

    async def handle_ping(self, data):
        await self.send(text_data=json.dumps({'type': 'pong'}))

    async def handle_typing_start(self, data):
        await self.channel_layer.group_send(
            self.room_group,
            {
                'type': 'typing_indicator',
                'user_id': self.user.id,
                'is_typing': True
            }
        )

    async def handle_typing_stop(self, data):
        await self.channel_layer.group_send(
            self.room_group,
            {
                'type': 'typing_indicator',
                'user_id': self.user.id,
                'is_typing': False
            }
        )

    async def handle_message_read(self, data):
        message_id = data.get('message_id')
        if message_id and await self._can_access_conversation(self.conversation_id, self.user.id):
            # Persist the read receipt to DB so it survives disconnects
            await self._persist_read_receipt(message_id)
            await self.channel_layer.group_send(
                self.room_group,
                {
                    'type': 'message_read_receipt',
                    'user_id': self.user.id,
                    'message_id': message_id
                }
            )

    async def _listen_bridge_commands(self):
        """Listen for commands from agent WS bridge (typing, read receipts)."""
        import redis.asyncio as aioredis

        try:
            r = aioredis.from_url(
                settings.CHANNEL_REDIS_URL,
                socket_timeout=settings.CHANNEL_REDIS_SOCKET_TIMEOUT,
                socket_connect_timeout=settings.CHANNEL_REDIS_CONNECT_TIMEOUT,
            )
            pubsub = r.pubsub()
            await pubsub.subscribe(f"shizuha:ws_bridge:chat_{self.conversation_id}")

            async for message in pubsub.listen():
                if message['type'] != 'message':
                    continue
                try:
                    data = json.loads(message['data'])
                    event_type = data.get('event_type')
                    user_id = data.get('user_id')

                    if event_type == 'typing_indicator':
                        if await self._can_access_conversation(self.conversation_id, user_id):
                            await self.channel_layer.group_send(
                                self.room_group,
                                {
                                    'type': 'typing_indicator',
                                    'user_id': user_id,
                                    'is_typing': data.get('is_typing', False),
                                }
                            )
                    elif event_type == 'message_read':
                        msg_id = data.get('message_id')
                        if msg_id:
                            persisted = await self._persist_read_receipt_for_user(msg_id, user_id)
                            if persisted:
                                await self.channel_layer.group_send(
                                    self.room_group,
                                    {
                                        'type': 'message_read_receipt',
                                        'user_id': user_id,
                                        'message_id': msg_id,
                                    }
                                )
                except Exception:
                    logger.debug("Failed to process bridge command", exc_info=True)

            await pubsub.unsubscribe()
            await r.aclose()
        except Exception:
            logger.debug("Bridge command listener ended", exc_info=True)

    @database_sync_to_async
    def _persist_read_receipt_for_user(self, message_id, user_id):
        """Persist read receipt on behalf of a bridged user."""
        from connect.models import MessageRead, DirectMessage, ConversationParticipant
        try:
            if not can_access_conversation(self.conversation_id, user_id):
                return
            msg = DirectMessage.objects.filter(
                id=message_id, conversation_id=self.conversation_id
            ).first()
            if msg and actor_can_reach_recipient(msg.sender_id, user_id):
                MessageRead.objects.get_or_create(message=msg, user_id=user_id)
                ConversationParticipant.objects.filter(
                    conversation_id=self.conversation_id,
                    user_id=user_id,
                ).update(last_read_at=timezone.now(), unread_count=0)
                return True
        except Exception:
            logger.exception("Failed to persist bridged read receipt for message %s", message_id)
        return False

    @database_sync_to_async
    def _persist_read_receipt(self, message_id):
        """
        Mark `message_id` as read by this user AND cascade to every
        prior message in the same conversation that was sent by others
        and hasn't been read yet. Returns the list of message ids that
        were newly marked read (excluding already-read ones), so the
        caller can broadcast them as a batch.

        This matches WhatsApp's semantics: opening a chat marks every
        unread message from the other side as read up to the read marker,
        not just the one message explicitly tapped.
        """
        from connect.models import MessageRead, DirectMessage, ConversationParticipant
        newly_read = []
        try:
            if not can_access_conversation(self.conversation_id, self.user.id):
                return newly_read
            msg = DirectMessage.objects.filter(
                id=message_id, conversation_id=self.conversation_id
            ).first()
            if not msg:
                return newly_read

            # Find every unread message in this conv from other senders
            # with created_at <= target. Filter out the ones we've already
            # marked as read to keep the broadcast tight.
            already_read_ids = set(MessageRead.objects.filter(
                message__conversation_id=self.conversation_id,
                user_id=self.user.id,
            ).values_list('message_id', flat=True))

            candidates = DirectMessage.objects.filter(
                conversation_id=self.conversation_id,
                created_at__lte=msg.created_at,
            ).exclude(sender_id=self.user.id).values_list('id', flat=True)

            to_mark = [mid for mid in candidates if mid not in already_read_ids]
            if to_mark:
                MessageRead.objects.bulk_create(
                    [MessageRead(message_id=mid, user_id=self.user.id) for mid in to_mark],
                    ignore_conflicts=True,
                )
                newly_read = [str(mid) for mid in to_mark]

            # Update participant's last_read_at and reset unread count
            ConversationParticipant.objects.filter(
                conversation_id=self.conversation_id,
                user_id=self.user.id,
            ).update(last_read_at=timezone.now(), unread_count=0)
        except Exception:
            logger.exception("Failed to persist read receipt for message %s", message_id)
        return newly_read

    async def _push_missed_messages(self):
        """Push messages the user may have missed while disconnected.

        Queries for recent messages in this conversation that the user
        hasn't read yet, and sends them over the WebSocket so the client
        doesn't need to manually fetch history.
        """
        from connect.models import DirectMessage, ConversationParticipant
        from connect.serializers.messaging import DirectMessageSerializer
        from datetime import timedelta

        participant = await database_sync_to_async(
            lambda: ConversationParticipant.objects.filter(
                conversation_id=self.conversation_id,
                user_id=self.user.id,
                has_left=False,
            ).first()
        )()

        if not participant:
            return

        # PLAT-9268: the replay window is bounded by an age floor and the
        # participant's cleared_at — NOT by last_read_at. A read marker is a
        # READ boundary, not a DELIVERY boundary: it advances whenever any
        # newer message is read, which permanently orphaned undelivered
        # messages sent during a WS outage. The CON-110 delivery-receipt
        # exclusion below already prevents re-sending delivered messages, so
        # the read marker is redundant for dedupe and harmful for completeness.
        cutoff = participant.cleared_at or (
            timezone.now() - timedelta(days=DEFAULT_DM_REPLAY_WINDOW_DAYS)
        )

        missed_data = await database_sync_to_async(
            lambda: DirectMessageSerializer(
                DirectMessage.objects.filter(
                    conversation_id=self.conversation_id,
                    created_at__gt=cutoff,
                    is_deleted=False,
                ).exclude(
                    sender_id=self.user.id,
                ).exclude(
                    # CON-110: suppress cross-reconnect duplicates. This consumer
                    # already writes MessageDelivery receipts for everything it
                    # delivers (_record_bulk_delivery below) but never consulted
                    # them on read, so a time-cutoff replay re-sent already-
                    # delivered messages on every reconnect. Excluding delivered
                    # rows mirrors the all-conversations path (UserChatConsumer).
                    delivery_receipts__user_id=self.user.id,
                ).select_related('reply_to').prefetch_related(
                    'read_receipts'
                ).order_by('created_at')[:500],
                many=True,
            ).data
        )()

        delivered_ids = []
        for msg_data in missed_data:
            msg_data['sender_same_org'] = await database_sync_to_async(
                _sender_same_org, thread_sensitive=False,
            )(self.scope, msg_data.get('sender_id'))
            try:
                await self.send(text_data=json.dumps({
                    'type': 'missed_message',
                    'message': msg_data,
                }))
                delivered_ids.append(msg_data['id'])
            except Exception:
                break  # WS broken — stop

        # Bulk-record delivery for all missed messages received
        if delivered_ids:
            await self._record_bulk_delivery(delivered_ids)

    @database_sync_to_async
    def _record_bulk_delivery(self, message_ids):
        """Bulk-record delivery receipts for missed messages."""
        from connect.models import MessageDelivery, DirectMessage
        try:
            messages = DirectMessage.objects.filter(
                id__in=message_ids
            ).exclude(sender_id=self.user.id)
            existing = set(MessageDelivery.objects.filter(
                message_id__in=[m.id for m in messages],
                user_id=self.user.id,
            ).values_list('message_id', flat=True))
            to_create = [
                MessageDelivery(message=m, user_id=self.user.id)
                for m in messages if m.id not in existing
            ]
            if to_create:
                MessageDelivery.objects.bulk_create(to_create, ignore_conflicts=True)
        except Exception:
            logger.exception("Failed to bulk-record delivery receipts")

    @database_sync_to_async
    def _can_access_conversation(self, conversation_id, user_id):
        return can_access_conversation(conversation_id, user_id)

    @database_sync_to_async
    def check_participant(self):
        from connect.models import ConversationParticipant
        return can_access_conversation(self.conversation_id, self.user.id)

    @database_sync_to_async
    def _record_delivery(self, message_id):
        """Record that this user's device received a message."""
        from connect.models import MessageDelivery, DirectMessage
        try:
            msg = DirectMessage.objects.filter(id=message_id).first()
            if msg and msg.sender_id != self.user.id and actor_can_reach_recipient(msg.sender_id, self.user.id):
                MessageDelivery.objects.get_or_create(
                    message=msg, user_id=self.user.id
                )
                return True
        except Exception:
            pass
        return False

    # Channel layer message handlers
    async def new_message(self, event):
        if event.get('skip_agent_notify') and getattr(self, 'is_agent', False):
            return
        msg_data = dict(event['message_data'])
        msg_data['sender_same_org'] = await database_sync_to_async(
            _sender_same_org, thread_sensitive=False,
        )(self.scope, msg_data.get('sender_id'))
        await self.send(text_data=json.dumps({
            'type': 'new_message',
            'message': msg_data
        }))
        # Record delivery and notify sender
        msg_id = msg_data.get('id')
        if msg_id and str(msg_data.get('sender_id')) != str(self.user.id):
            delivered = await self._record_delivery(msg_id)
            if delivered:
                await self.channel_layer.group_send(
                    self.room_group,
                    {
                        'type': 'message_delivered_receipt',
                        'user_id': self.user.id,
                        'message_id': str(msg_id),
                    }
                )

    async def missed_message(self, event):
        """Forward missed message to WebSocket and also bridge."""
        await self.send(text_data=json.dumps({
            'type': 'missed_message',
            'message': event.get('message_data', event.get('message', {})),
        }))

    async def typing_indicator(self, event):
        if event['user_id'] != self.user.id and await self._can_access_conversation(self.conversation_id, self.user.id):
            await self.send(text_data=json.dumps({
                'type': 'typing',
                'user_id': event['user_id'],
                'is_typing': event['is_typing']
            }))

    async def message_read_receipt(self, event):
        await self.send(text_data=json.dumps({
            'type': 'message_read',
            'user_id': event['user_id'],
            'message_id': event['message_id']
        }))

    async def message_delivered_receipt(self, event):
        await self.send(text_data=json.dumps({
            'type': 'message_delivered',
            'user_id': event['user_id'],
            'message_id': event['message_id']
        }))

    async def user_joined(self, event):
        await self.send(text_data=json.dumps({
            'type': 'user_joined',
            'user_id': event['user_id']
        }))

    async def user_left(self, event):
        await self.send(text_data=json.dumps({
            'type': 'user_left',
            'user_id': event['user_id']
        }))

    async def message_edited(self, event):
        await self.send(text_data=json.dumps({
            'type': 'message_edited',
            'message': event['message_data']
        }))

    async def message_deleted(self, event):
        await self.send(text_data=json.dumps({
            'type': 'message_deleted',
            'message_id': event['message_id']
        }))

    async def participant_added(self, event):
        await self.send(text_data=json.dumps({
            'type': 'participant_added',
            'participant': event['participant']
        }))

    async def participant_removed(self, event):
        await self.send(text_data=json.dumps({
            'type': 'participant_removed',
            'user_id': event['user_id']
        }))
        # Self-disconnect if we were the one removed
        if event['user_id'] == self.user.id:
            await self.close(code=4001)

    async def group_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'group_updated',
            'name': event.get('name', ''),
            'description': event.get('description', '')
        }))

    async def agent_stream_event(self, event):
        """Forward agent streaming events to human clients watching this conversation."""
        # Don't send back to the agent that originated the event
        if event.get('origin_channel') == self.channel_name:
            return
        payload = {
            'type': 'agent_stream',
            'conversation_id': event.get('conversation_id', ''),
            'event_type': event.get('event_type', ''),
            'agent_id': event.get('agent_id', ''),
            'data': event.get('data', {}),
        }
        await self.send(text_data=json.dumps(payload))

        # Also publish to Redis bridge for cross-service delivery (Kotlin app via agent WS)
        _publish_bridge(f"shizuha:chat_events:{self.user.id}", {
            'conv_id': event.get('conversation_id', ''),
            'event_type': 'agent_stream',
            'data': payload,
        })


class MissedMessageBatch(list):
    """Serialized missed messages plus replay-query telemetry metadata."""

    def __init__(self, messages, *, capped=False, limit=0, oldest_at=None,
                 newest_at=None, conversation_count=0):
        super().__init__(messages)
        self.capped = bool(capped)
        # CON-231: cap-hit evidence captured where the rows still exist. The
        # zero-delivered exits below return before any message is serialized,
        # so without carrying this here an operator sees a bare counter tick
        # with no way to identify the cohort or judge backlog age.
        self.limit = int(limit or 0)
        self.oldest_at = oldest_at
        self.newest_at = newest_at
        self.conversation_count = int(conversation_count or 0)


class UserChatConsumer(AsyncWebsocketConsumer):
    """
    Multiplexed WebSocket for all participants — humans AND agents.

    One persistent WS per participant, handling ALL their conversations.
    Replaces per-conversation ChatConsumer connections, the old
    AgentChatConsumer, and the shizuha-agent bridge relay.

    Agents are just users with an `is_agent: true` JWT claim. This claim
    unlocks two capabilities that humans don't need:

      1. Inbound `stream_event` frames (token-level deltas, tool events)
         broadcast as `agent_stream` on the `chat_<conversation_id>` group.
      2. Sent messages get stamped with `sender_is_agent=True, agent_id`
         on persistence so clients can render them differently.

    Everything else — conversation groups, inbox, typing, read receipts —
    is identical regardless of participant type. No duplication.

    Endpoint: ws://host/connect/ws/connect/user/?token=<jwt>
              ws://host/connect/ws/connect/agent/?token=<jwt>  (alias, same consumer)

    Outbound:
      - new_message: someone sent a message in any conversation
      - agent_stream: agent streaming events (content, tool_start, etc.)
      - typing: typing indicator
      - message_read: read receipt
      - conversation_created: participant added to new conversation
      - missed_message: messages received while disconnected

    Inbound:
      - send_message: send a message to a conversation (humans + agents)
      - stream_event: streaming response chunks (agents only)
      - typing_start / typing_stop: typing indicator
      - message_read: mark message as read
      - ping: keepalive
    """

    async def connect(self):
        import asyncio
        self.user = self.scope.get('user', AnonymousUser())
        self.conversation_groups = set()
        self._keepalive_task = None
        self._last_presence_status = None
        self._delivered_message_ids_this_connection = set()
        self._announced_conversation_ids_this_connection = set()
        # The handshake flag is available before connect-time replay. A
        # capability frame received after accept is necessarily too late for
        # rows emitted by `_push_missed_messages_all_conversations()` below.
        query_string = self.scope.get('query_string', b'')
        if isinstance(query_string, bytes):
            query_string = query_string.decode('ascii', errors='ignore')
        self.processing_ack_capable = any(
            item == 'processing_ack=1'
            for item in str(query_string).split('&')
        )

        # Agent-identity claim from the validated JWT (stored on scope by
        # JWTAuthMiddleware). Used for persist-time `sender_is_agent`
        # stamping on new_message broadcasts originating from this WS.
        # We deliberately do NOT read an `agent_id` UUID claim — sender
        # identity is fully captured by user_id + is_agent.
        token = self.scope.get('token')
        self.is_agent = bool(token.get('is_agent', False)) if token else False
        self.presence_session_key = f'userchat:{self.channel_name}'

        if not self.user.is_authenticated:
            # Close after accept so reverse proxies and clients observe a real
            # WebSocket close code instead of an upstream reset/502 during the
            # HTTP upgrade. This keeps expired-token retries diagnosable and
            # lets runtimes trigger auth refresh paths reliably.
            await self.accept()
            await self.close(code=4401)
            return

        await self.accept()
        await self._join_all_groups()

        # Replay messages received while this user was disconnected. Without
        # this, DMs sent to an offline agent's user_inbox_<id> group are
        # discarded by the channel layer and never reach the recipient on
        # reconnect — silently breaks the autonomous-agent contract on every
        # container restart. See PLAT-6.
        legacy_processing_quarantine = await self._legacy_agent_processing_quarantined()
        if legacy_processing_quarantine:
            # A pending processing-ack row proves this legacy agent has already
            # received transport bytes that it cannot durably acknowledge. Do
            # not add more in-memory work while it drains toward a runtime
            # upgrade. The skipped messages remain undelivered in Postgres and
            # the ack-capable successor replays them on reconnect.
            logger.warning(
                "Quarantining inbound delivery to legacy agent user=%s until processing-ack upgrade",
                self.user.id,
            )
        else:
            try:
                await self._push_missed_messages_all_conversations()
            except Exception:
                logger.exception(
                    "Failed to push missed messages on connect for user %s",
                    self.user.id,
                )

        # The opening query advertises this capability before accept, so replay
        # old transport-delivered rows immediately. Do not depend on the
        # post-open capability frame: that best-effort send can itself be lost
        # while leaving the socket open indefinitely.
        try:
            await self._push_unprocessed_agent_messages()
        except Exception:
            logger.exception(
                "Failed to push unprocessed agent messages on connect for user %s",
                self.user.id,
            )

        logger.info(
            "%s %s (%s) connected to UserChatConsumer — joined %d conversations",
            "Agent" if self.is_agent else "User",
            self.user.username, self.user.id, len(self.conversation_groups)
        )

        # Server-initiated keepalive: ping every 30s + re-subscribe groups every 5 min
        # This prevents silent group membership expiry in Redis
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _join_all_groups(self, publish_presence=True):
        """(Re-)join all conversation groups. Called on connect and periodically."""
        conversations = await self._get_user_conversations()
        for conv_id in conversations:
            group = f'chat_{conv_id}'
            await self.channel_layer.group_add(group, self.channel_name)
            self.conversation_groups.add(group)

        self.user_inbox_group = f'user_inbox_{self.user.id}'
        await self.channel_layer.group_add(self.user_inbox_group, self.channel_name)

        # Join the global presence ring so clients on UserChatConsumer also
        # receive online/offline events — keeps ori-expo (and any other
        # client that uses only this WS) in sync without a second socket.
        self.presence_group = 'presence_global'
        await self.channel_layer.group_add(self.presence_group, self.channel_name)

        if publish_presence:
            # Initial connect: broadcast our own presence so peers flip us online
            # immediately. `presence_update` is the channel-layer event type (see
            # ConnectConsumer.presence_update). This consumer re-emits it to
            # clients as a `presence` frame below.
            snapshot = await self._touch_presence('online')
            await self._broadcast_presence_snapshot(snapshot)
        else:
            # Periodic group rejoin is only a membership refresh; do not reset an
            # explicit away/degraded status back to online.
            await self._refresh_presence()

    async def _keepalive_loop(self):
        """Server-side keepalive: ping client + periodically refresh group memberships."""
        import asyncio
        tick = 0
        try:
            while True:
                await asyncio.sleep(30)
                tick += 1
                # Send server ping every 30s
                try:
                    await self.send(text_data=json.dumps({'type': 'ping'}))
                    snapshot = await self._refresh_presence()
                    # Only broadcast when the aggregate status transitions — broadcasting
                    # on every 30s tick causes fleet-wide continuous fanout to
                    # presence_global + Redis even when nothing changed.
                    if snapshot and snapshot.status != 'offline':
                        if snapshot.status != self._last_presence_status:
                            self._last_presence_status = snapshot.status
                            await self._broadcast_presence_snapshot(snapshot)
                except Exception:
                    break  # Connection dead

                # Re-subscribe to all groups every 5 minutes (10 ticks)
                # This prevents Redis from expiring group memberships
                if tick % 10 == 0:
                    try:
                        await self._join_all_groups(publish_presence=False)
                    except Exception:
                        logger.debug("Failed to refresh group memberships", exc_info=True)
        except asyncio.CancelledError:
            pass

    async def force_auth_reject(self, event):
        """Internal test hook: close this socket with an auth-class code.

        Delivered only by the internal API to this consumer's concrete channel
        name. It lets live verification exercise runtime stale-token recovery
        without revoking credentials or disrupting the whole fleet.
        """
        try:
            code = int(event.get('code') or 4401)
        except (TypeError, ValueError):
            code = 4401
        if code not in {4401, 4403, 1008}:
            code = 4401
        logger.warning(
            'Closing UserChatConsumer with auth-reject test code %s for user %s session %s',
            code, getattr(self.user, 'id', None), event.get('target_session_id'),
        )
        await self.close(code=code)

    def _log_dm_replay_capped(self, batch, result, delivered):
        """CON-231: privacy-safe, attributable cap-hit evidence.

        The Prometheus counter carries only bounded labels, so this log is the
        only place an operator can identify WHICH cohort hit the cap and judge
        whether the backlog is draining. The user is identified by an HMAC of
        the id keyed on SECRET_KEY, truncated for readability: stable enough to
        correlate one user's reconnects, and -- unlike the unkeyed digest this
        replaced -- not brute-forceable from the log alone over the small,
        sequential id space (@reika, CON-231 P3). Raw user/conversation ids stay
        out of both the metric and this line.
        """
        user_id = getattr(getattr(self, 'user', None), 'id', None)
        user_hash = hmac.new(
            settings.SECRET_KEY.encode(),
            f'connect-dm-replay:{user_id}'.encode(),
            hashlib.sha256,
        ).hexdigest()[:12]
        now = timezone.now()
        oldest_age = (now - batch.oldest_at).total_seconds() if batch.oldest_at else None
        newest_age = (now - batch.newest_at).total_seconds() if batch.newest_at else None
        logger.warning(
            'connect dm replay hit the candidate cap',
            extra={
                'event': 'dm_replay_capped',
                'user_hash': user_hash,
                'user_role': user_role_label(getattr(self, 'is_agent', False)),
                'result': result,
                'candidate_limit': batch.limit,
                'returned': len(batch),
                'delivered': delivered,
                'conversation_count': batch.conversation_count,
                'oldest_candidate_age_s': oldest_age,
                'newest_candidate_age_s': newest_age,
            },
        )

    async def _push_missed_messages_all_conversations(self):
        """Replay DMs that arrived while this user was disconnected.

        Channel-layer group_send to `user_inbox_<id>` is fire-and-forget:
        if the recipient's UserChatConsumer was not connected, the message
        is dropped. On reconnect we query the DB for messages newer than
        each conversation's `last_read_at` (or a 7-day cap, whichever is
        more recent) that this user did not author and have not been
        delivered yet, and emit them as `missed_message` frames so the
        client can process them as if they had arrived live.

        Per-user, capped at 200 messages total to keep reconnect cheap;
        anything older the client must page in via the normal history API.
        """
        missed = await self._fetch_missed_messages()
        capped = getattr(missed, 'capped', False)
        # CON-231: the dedupe filter below rebinds `missed` to a plain list,
        # which drops the cap-hit metadata. Keep the original batch for the
        # later exits.
        batch = missed
        if not missed:
            observe_dm_replay_per_reconnect(0)
            if capped:
                self._log_dm_replay_capped(batch, 'no_candidates', 0)
                record_dm_replay_capped(
                    user_role_label(getattr(self, 'is_agent', False)), 'no_candidates')
            return

        # A live `new_message` can be queued to this socket while connect-time
        # replay is running (we join groups before replay so newly-created
        # conversations can reach us). Dedupe at the connection level in both
        # directions: if live delivery won the race, skip the replay row; if
        # replay wins, `new_message()` will skip the queued live copy below.
        missed = [
            msg_data for msg_data in missed
            if str(msg_data.get('id')) not in self._delivered_message_ids_this_connection
        ]
        if not missed:
            observe_dm_replay_per_reconnect(0)
            if capped:
                self._log_dm_replay_capped(batch, 'all_deduped', 0)
                record_dm_replay_capped(
                    user_role_label(getattr(self, 'is_agent', False)), 'all_deduped')
            return

        # If the conversation itself was created while the user was offline,
        # the best-effort `conversation_created` broadcast to user_inbox_<id>
        # was also dropped. Replay an idempotent conversation announcement
        # before the first missed_message for that conversation so clients can
        # create/open the thread before applying the message.
        conversation_ids = {str(msg.get('conversation_id')) for msg in missed if msg.get('conversation_id')}
        conversation_payloads = await self._fetch_conversation_replay_payloads(conversation_ids)
        for conv_id in sorted(conversation_ids):
            group = f'chat_{conv_id}'
            try:
                await self.channel_layer.group_add(group, self.channel_name)
                self.conversation_groups.add(group)
            except Exception:
                return  # Channel layer broken — stop replay before announcing
            if conv_id in self._announced_conversation_ids_this_connection:
                continue
            try:
                await self.send(text_data=json.dumps({
                    'type': 'conversation_created',
                    'conversation_id': conv_id,
                    'conversation': conversation_payloads.get(conv_id, {'id': conv_id}),
                    'replayed': True,
                }))
                self._announced_conversation_ids_this_connection.add(conv_id)
            except Exception:
                return  # WS broken — stop replay

        delivered_ids = []

        for msg_data in missed:
            msg_data['sender_same_org'] = await database_sync_to_async(
                _sender_same_org, thread_sensitive=False,
            )(self.scope, msg_data.get('sender_id'))
            msg_id = str(msg_data.get('id'))
            if msg_id in self._delivered_message_ids_this_connection:
                continue
            try:
                await self.send(text_data=json.dumps({
                    'type': 'missed_message',
                    'message': msg_data,
                }))
                self._delivered_message_ids_this_connection.add(msg_id)
                delivered_ids.append(msg_data['id'])
            except Exception:
                break  # WS broken — stop replay

        if delivered_ids:
            record_dm_replay_fanout(
                user_role_label(getattr(self, 'is_agent', False)),
                len(delivered_ids),
            )
        if delivered_ids and self.is_agent:
            await self._record_bulk_delivery(delivered_ids)

        observe_dm_replay_per_reconnect(len(delivered_ids))
        if capped:
            self._log_dm_replay_capped(batch, 'delivered', len(delivered_ids))
            record_dm_replay_capped(
                user_role_label(getattr(self, 'is_agent', False)), 'delivered')

    async def _push_unprocessed_agent_messages(self):
        """Replay transport-sent rows that lack a durable agent processing ack."""
        if not self.is_agent or not self.processing_ack_capable:
            return
        pending = await self._fetch_unprocessed_agent_messages()
        for msg_data in pending:
            msg_id = str(msg_data.get('id'))
            if msg_id in self._delivered_message_ids_this_connection:
                continue
            msg_data['sender_same_org'] = await database_sync_to_async(
                _sender_same_org, thread_sensitive=False,
            )(self.scope, msg_data.get('sender_id'))
            sent = await self._send_bounded({
                'type': 'missed_message',
                'message': msg_data,
                'processing_replay': True,
            })
            if not sent:
                break
            self._delivered_message_ids_this_connection.add(msg_id)
            await self._record_processing_attempt(msg_id)

    @database_sync_to_async
    def _legacy_agent_processing_quarantined(self):
        """Whether this legacy agent must stop accepting new inbound turns.

        Processing-ack rows are a durable recovery fence. A client that did
        not advertise the capability cannot complete those rows, so continuing
        live/missed delivery would let its volatile queue replenish forever and
        prevent a safe runtime upgrade. Ack-capable agents and human sockets are
        never quarantined.
        """
        if not self.is_agent or self.processing_ack_capable:
            return False
        from connect.models import MessageDelivery
        return MessageDelivery.objects.filter(
            user_id=self.user.id,
            processing_ack_required=True,
            processed_at__isnull=True,
            processing_dead_lettered_at__isnull=True,
        ).exists()

    @database_sync_to_async
    def _fetch_unprocessed_agent_messages(self):
        from connect.models import DirectMessage
        from connect.serializers.messaging import DirectMessageSerializer

        rows = list(DirectMessage.objects.filter(
            delivery_receipts__user_id=self.user.id,
            delivery_receipts__processing_ack_required=True,
            delivery_receipts__processed_at__isnull=True,
            delivery_receipts__processing_dead_lettered_at__isnull=True,
            is_deleted=False,
        ).exclude(
            sender_id=self.user.id,
        ).exclude(
            deletions__user_id=self.user.id,
        ).order_by('created_at')[:200])
        return DirectMessageSerializer(rows, many=True).data

    @database_sync_to_async
    def _record_processing_attempt(self, message_id):
        from connect.models import MessageDelivery
        from django.utils import timezone

        receipt = MessageDelivery.objects.filter(
            message_id=message_id,
            user_id=self.user.id,
            processing_ack_required=True,
            processed_at__isnull=True,
        ).first()
        if not receipt:
            return
        receipt.processing_attempts += 1
        receipt.last_processing_attempt_at = timezone.now()
        if receipt.processing_attempts >= 5:
            receipt.processing_dead_lettered_at = timezone.now()
            logger.error(
                "Agent message processing replay dead-lettered after %d attempts: user=%s message=%s",
                receipt.processing_attempts,
                self.user.id,
                message_id,
            )
        receipt.save(update_fields=[
            'processing_attempts',
            'last_processing_attempt_at',
            'processing_dead_lettered_at',
        ])

    @database_sync_to_async
    def _record_message_processed(self, message_id):
        from connect.models import DirectMessage, MessageDelivery
        from django.utils import timezone

        processed_at = timezone.now()
        updated = MessageDelivery.objects.filter(
            message_id=message_id,
            user_id=self.user.id,
            processing_ack_required=True,
            processed_at__isnull=True,
        ).update(processed_at=processed_at, processing_dead_lettered_at=None)
        if updated:
            return updated

        # The delivery receipt is intentionally written only after a WS send
        # succeeds. A fast agent can finish and ack before that post-send write
        # commits. Preserve the ack by creating the recipient-scoped receipt,
        # but only for a real message in one of this user's conversations.
        message = DirectMessage.objects.filter(
            id=message_id,
            conversation__participants__user_id=self.user.id,
            conversation__participants__has_left=False,
            is_deleted=False,
        ).exclude(sender_id=self.user.id).distinct().first()
        if not message:
            return 0
        receipt, _ = MessageDelivery.objects.update_or_create(
            message=message,
            user_id=self.user.id,
            defaults={
                'processing_ack_required': True,
                'processed_at': processed_at,
                'processing_dead_lettered_at': None,
            },
        )
        return 1 if receipt.processed_at else 0

    @database_sync_to_async
    def _fetch_missed_messages(self):
        """Query undelivered DMs across all of this user's conversations."""
        from connect.models import ConversationParticipant
        from connect.serializers.messaging import DirectMessageSerializer

        participants = ConversationParticipant.objects.filter(
            user_id=self.user.id,
            has_left=False,
        ).only('conversation_id', 'last_read_at', 'cleared_at')

        # Hard floor: never replay older rows than the reconnect window. Human
        # clients keep a broad safety net; agents are deliberately much tighter
        # because every replayed DM becomes a new LLM turn.
        replay_limit, floor = _replay_limits_for(getattr(self, 'is_agent', False))
        if replay_limit <= 0:
            return MissedMessageBatch([], capped=False)

        # CON-231: the eligibility predicate lives in connect.services.dm_replay
        # so the operator backlog probe cannot drift from what replay actually
        # considers a candidate. It still excludes already-delivered rows BEFORE
        # the cap, so a reconnect whose newest candidates were delivered live
        # cannot permanently starve older undelivered rows inside the window.
        from connect.services.dm_replay import eligible_candidate_queryset

        candidates = eligible_candidate_queryset(self.user.id, participants, floor)
        missed_plus_one = list(candidates[:replay_limit + 1])
        capped = len(missed_plus_one) > replay_limit
        missed = missed_plus_one[:replay_limit]

        # Replay in chronological order so the client renders sensibly
        missed.sort(key=lambda m: m.created_at)

        return MissedMessageBatch(
            DirectMessageSerializer(missed, many=True).data,
            capped=capped,
            limit=replay_limit,
            # CON-231: `missed` is re-sorted ASCENDING two lines above
            # (`missed.sort(key=lambda m: m.created_at)`, for client render
            # order), so missed[0] is the OLDEST and missed[-1] the NEWEST.
            # Do NOT "fix" this against the queryset's `-created_at` ordering:
            # that ordering is real but is discarded by the sort, and inverting
            # these makes `oldest_candidate_age_s` report the newest row's age
            # -- failing in the reassuring direction. Pinned by
            # Con231CapLogOrderingTests.
            oldest_at=missed[0].created_at if missed else None,
            newest_at=missed[-1].created_at if missed else None,
            conversation_count=len({m.conversation_id for m in missed}),
        )

    @database_sync_to_async
    def _fetch_conversation_replay_payloads(self, conversation_ids):
        """Serialize conversations whose creation broadcasts may have been missed."""
        from connect.models import Conversation
        from connect.serializers.messaging import ConversationSerializer

        if not conversation_ids:
            return {}

        conversations = Conversation.objects.filter(
            id__in=conversation_ids,
            participants__user_id=self.user.id,
            participants__has_left=False,
        ).prefetch_related('participants').distinct()
        request = type('ReplayRequest', (), {'user': self.user})()
        return {
            str(item['id']): item
            for item in ConversationSerializer(
                conversations,
                many=True,
                context={'request': request},
            ).data
        }

    @database_sync_to_async
    def _touch_presence(self, status='online'):
        return touch_presence(
            user_id=self.user.id,
            session_key=self.presence_session_key,
            client_id=self.channel_name,
            client_type='agent' if getattr(self, 'is_agent', False) else 'web',
            client_label='agent-ws' if getattr(self, 'is_agent', False) else 'user-ws',
            is_agent=getattr(self, 'is_agent', False),
            status=status,
            channel_name=self.channel_name,
            organization_id=_org_id_from_scope(self.scope, self.user),
        )

    @database_sync_to_async
    def _refresh_presence(self):
        return refresh_presence(
            user_id=self.user.id,
            session_key=self.presence_session_key,
            client_id=self.channel_name,
            client_type='agent' if getattr(self, 'is_agent', False) else 'web',
            client_label='agent-ws' if getattr(self, 'is_agent', False) else 'user-ws',
            is_agent=getattr(self, 'is_agent', False),
            channel_name=self.channel_name,
        )

    async def _broadcast_presence_snapshot(self, snapshot):
        await self.channel_layer.group_send(
            getattr(self, 'presence_group', 'presence_global'),
            _presence_event_from_snapshot(snapshot),
        )
        org_id = _org_id_from_scope(self.scope, self.user)
        _publish_bridge_background(
            f"shizuha:presence:{org_id}",
            snapshot.as_event()
        )

    @database_sync_to_async
    def _mark_presence_offline(self):
        return mark_offline(
            user_id=self.user.id,
            session_key=getattr(self, 'presence_session_key', f'userchat:{self.channel_name}'),
        )

    async def disconnect(self, close_code):
        # Cancel keepalive and drain: await completion so any in-flight
        # database_sync_to_async dispatch in _refresh_presence() finishes
        # before _mark_presence_offline() runs below.  Without this drain,
        # the in-flight thread may commit "online" after we write "offline".
        if hasattr(self, '_keepalive_task') and self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except asyncio.CancelledError:
                pass

        for group in self.conversation_groups:
            try:
                await self.channel_layer.group_discard(group, self.channel_name)
            except Exception:
                pass
        if hasattr(self, 'user_inbox_group'):
            try:
                await self.channel_layer.group_discard(self.user_inbox_group, self.channel_name)
            except Exception:
                pass
        if hasattr(self, 'presence_group') and self.user.is_authenticated:
            try:
                snapshot = await self._mark_presence_offline()
                await self._broadcast_presence_snapshot(snapshot)
                await self.channel_layer.group_discard(self.presence_group, self.channel_name)
            except Exception:
                pass
        logger.info(
            "%s %s disconnected from UserChatConsumer",
            "Agent" if getattr(self, 'is_agent', False) else "User",
            getattr(self.user, 'username', '?'),
        )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            msg_type = data.get('type')

            if msg_type == 'ping':
                status_value = data.get('status') if data.get('status') in {'online', 'away', 'degraded'} else 'online'
                snapshot = await self._touch_presence(status_value)
                await self._broadcast_presence_snapshot(snapshot)
                await self.send(text_data=json.dumps({'type': 'pong'}))
            elif msg_type == 'send_message':
                await self._handle_send_message(data)
            elif msg_type == 'stream_event':
                # Talk seats stream token/sentence deltas so Home can speak
                # live. Fleet work agents still persist via message_user only;
                # they simply do not emit these frames.
                if not getattr(self, 'is_agent', False):
                    return
                conv_id = data.get('conversation_id')
                if conv_id and await self._can_access_conversation(conv_id, self.user.id):
                    await self.channel_layer.group_send(f'chat_{conv_id}', {
                        'type': 'agent_stream_event',
                        'conversation_id': str(conv_id),
                        'event_type': data.get('event_type') or '',
                        'agent_id': str(getattr(self.user, 'id', '') or ''),
                        'data': data.get('data') or {},
                        'origin_channel': self.channel_name,
                    })
            elif msg_type == 'agent.telemetry':
                # Agent runtime telemetry (model / context% / tok-per-sec / health
                # / recent errors) reported by the bridge over its existing socket.
                # Stored per-agent for Hive + the proactive health stream.
                await self._handle_agent_telemetry(data)
            elif msg_type == 'agent.processing_ack_capability':
                if self.is_agent and data.get('version') == 1:
                    self.processing_ack_capable = True
                    await self._push_unprocessed_agent_messages()
            elif msg_type == 'agent.message_processed':
                message_id = data.get('message_id')
                try:
                    message_id = uuid.UUID(str(message_id)) if message_id else None
                except (ValueError, TypeError, AttributeError):
                    message_id = None
                if self.is_agent and message_id is not None:
                    await self._record_message_processed(message_id)
            elif msg_type == 'typing_start':
                conv_id = data.get('conversation_id')
                if conv_id:
                    if await self._can_access_conversation(conv_id, self.user.id):
                        await self.channel_layer.group_send(f'chat_{conv_id}', {
                            'type': 'typing_indicator',
                            'conversation_id': conv_id,
                            'user_id': self.user.id,
                            'is_typing': True,
                        })
            elif msg_type == 'typing_stop':
                conv_id = data.get('conversation_id')
                if conv_id:
                    if await self._can_access_conversation(conv_id, self.user.id):
                        await self.channel_layer.group_send(f'chat_{conv_id}', {
                            'type': 'typing_indicator',
                            'conversation_id': conv_id,
                            'user_id': self.user.id,
                            'is_typing': False,
                        })
            elif msg_type == 'message_read':
                conv_id = data.get('conversation_id')
                message_id = data.get('message_id')
                if conv_id and message_id and await self._can_access_conversation(conv_id, self.user.id):
                    # `_persist_read_receipt` returns every message newly
                    # marked read (cascades backwards from the target). We
                    # broadcast one event per id so the sender's UI can
                    # upgrade every sent-but-unread bubble to blue ✓✓ at
                    # once — same pattern as WhatsApp's "opened the chat".
                    newly_read = await self._persist_read_receipt(conv_id, message_id) or [message_id]
                    for mid in newly_read:
                        await self.channel_layer.group_send(f'chat_{conv_id}', {
                            'type': 'message_read_receipt',
                            'conversation_id': conv_id,
                            'user_id': self.user.id,
                            'message_id': str(mid),
                        })
            elif msg_type == 'sync':
                # Client-driven cursor replay (K4). Clients persist the
                # highest event_id they've ingested and send it back on every
                # WS open. Server replays everything with event_id > N across
                # all the user's conversations and terminates with a
                # `sync_complete` frame so the UI can clear its "catching up"
                # indicator.
                since_event_id = data.get('since_event_id')
                try:
                    cursor = int(since_event_id) if since_event_id is not None else None
                except (TypeError, ValueError):
                    cursor = None
                if cursor is None:
                    await self._push_missed_messages_all_conversations()
                else:
                    await self._push_missed_messages(since_event_id=cursor, record_reconnect_metric=True)
                await self.send(text_data=json.dumps({
                    'type': 'sync_complete',
                    'since_event_id': cursor,
                }))
            elif msg_type == 'history':
                # K5: WS-based scroll-back pagination. Client asks for the N
                # events immediately before a cursor; server replies with
                # `history_message` frames (distinct from `missed_message` so
                # clients can render "loaded older" differently if desired).
                conv_id = data.get('conversation_id')
                before_event_id = data.get('before_event_id')
                limit = data.get('limit', 50)
                try:
                    before_cursor = int(before_event_id) if before_event_id is not None else None
                    limit_n = max(1, min(int(limit), 200))
                except (TypeError, ValueError):
                    before_cursor = None
                    limit_n = 50
                if conv_id:
                    await self._push_history(conv_id, before_cursor, limit_n)
        except json.JSONDecodeError:
            pass
        except Exception:
            logger.exception("UserChatConsumer receive error")

    # ── Backpressure helpers ────────────────────────────────────────────────

    async def _send_bounded(self, payload, *, timeout=None):
        """Send one WS frame with a per-client lag bound.

        Channels already bounds cross-process group membership, but an individual
        slow socket can still make this consumer spend unbounded time awaiting a
        frame. CON-164 policy: delayed delivery is recovered by reconnect replay;
        a client that cannot accept a frame inside the bound is closed with 1013
        instead of accumulating an in-memory backlog.
        """
        timeout = timeout or WS_SEND_TIMEOUT_SECONDS
        try:
            await asyncio.wait_for(self.send(text_data=json.dumps(payload)), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            self.backpressure_closed = True
            logger.warning(
                "CON-164 websocket slow-consumer close user=%s channel=%s timeout=%ss",
                getattr(getattr(self, 'user', None), 'id', '?'),
                getattr(self, 'channel_name', '?'),
                timeout,
            )
            try:
                await self.close(code=WS_CLOSE_CODE_OVERLOAD)
            except Exception:
                pass
            return False

    # ── Send message ──

    async def _handle_agent_telemetry(self, data):
        """Store a telemetry snapshot reported by an agent's bridge. The key is the
        authenticated agent identity (not the payload's claimed username)."""
        if not getattr(self, 'is_agent', False):
            return
        username = getattr(getattr(self, 'user', None), 'username', None) or data.get('agent_username')
        if not username:
            return
        payload = {k: v for k, v in data.items() if k != 'type'}
        await database_sync_to_async(_store_agent_telemetry, thread_sensitive=False)(username, payload)

    async def _handle_send_message(self, data):
        """Create and broadcast a message from the user."""
        conv_id = data.get('conversation_id')
        content = data.get('content', '').strip()
        client_message_id = data.get('client_message_id')
        if not conv_id or not content:
            await self._send_bounded({
                'type': 'send_error',
                'conversation_id': conv_id or '',
                'client_message_id': client_message_id,
                'error': 'conversation_id and content are required',
                'delivery_state': 'failed_terminal',
            })
            return

        msg_data = await self._persist_message(conv_id, content, client_message_id)
        if msg_data:
            await self.channel_layer.group_send(f'chat_{conv_id}', {
                'type': 'new_message',
                'message_data': msg_data,
                'origin_channel': self.channel_name,  # Track sender for dedup
            })
        else:
            # Persist failed — conversation missing, not a participant, or DB error.
            # Tell the client so it can invalidate its cached conversation id and
            # stop sending into a void.
            await self._send_bounded({
                'type': 'send_error',
                'conversation_id': conv_id,
                'client_message_id': client_message_id,
                'error': 'conversation_not_found_or_not_participant',
                'delivery_state': 'failed_terminal',
            })

    # ── Channel layer handlers (receive broadcasts) ──

    async def new_message(self, event):
        if event.get('skip_agent_notify') and getattr(self, 'is_agent', False):
            return
        msg_data = dict(event.get('message_data', {}))
        msg_data['sender_same_org'] = await database_sync_to_async(
            _sender_same_org, thread_sensitive=False,
        )(self.scope, msg_data.get('sender_id'))
        conv_id = msg_data.get('conversation_id', '')
        msg_id = msg_data.get('id')
        if msg_id and str(msg_id) in self._delivered_message_ids_this_connection:
            return
        # Only skip if THIS specific connection sent the message (multi-device support)
        if event.get('origin_channel') == self.channel_name:
            return
        # Suppress self-echo: messages sent via HTTP (MessageUserView → broadcast)
        # don't have an origin_channel set, so the sender's own WS socket receives
        # the broadcast. Filter by sender_id to prevent the sender from processing
        # their own outbound message — applies to both agents and human users.
        #
        # PLAT-965: gate this on the ABSENCE of origin_channel. A WS-posted message
        # carries origin_channel, and the originating device is already suppressed by
        # the origin_channel == self.channel_name check above. Applying the sender_id
        # filter to WS-posted messages would ALSO suppress the sender's OTHER devices
        # (same user.id, different channel), breaking human multi-device live sync.
        # HTTP-posted messages (agents) have no origin_channel, so they still hit this
        # filter — preserving the CON-106 self-echo-loop fix.
        sender_id = msg_data.get('sender_id')
        try:
            if (not event.get('origin_channel')
                    and sender_id is not None and int(sender_id) == int(self.user.id)):
                return
        except (TypeError, ValueError):
            pass
        if await self._legacy_agent_processing_quarantined():
            # Preserve this message as undelivered for the ack-capable
            # successor instead of admitting it to an unacknowledged volatile
            # queue that can indefinitely starve the safe rollout boundary.
            return
        sent = await self._send_bounded({
            'type': 'new_message',
            'conversation_id': conv_id,
            'message': msg_data,
            'delivery_state': 'delivered',
        })
        if not sent:
            return
        # Per-connection dedupe is safe for every socket and prevents a live
        # frame queued behind connect-time replay from duplicating on this
        # specific connection. Persisted MessageDelivery remains agent-only:
        # PLAT-6 agents are single-process bridges, so user-level delivery is
        # the desired restart catch-up contract; humans can have multiple
        # devices, so a live frame on one socket must not suppress replay for
        # another offline socket owned by the same user.
        sender_id = msg_data.get('sender_id')
        try:
            if msg_id and sender_id is not None and int(sender_id) != int(self.user.id):
                self._delivered_message_ids_this_connection.add(str(msg_id))
                if self.is_agent:
                    created = await self._record_bulk_delivery([msg_id])
                    if self.processing_ack_capable:
                        await self._record_processing_attempt(msg_id)
                    if created:
                        record_dm_live_delivery(
                            user_role_label(getattr(self, 'is_agent', False)),
                            created,
                        )
        except (TypeError, ValueError):
            pass

    async def agent_stream_event(self, event):
        """Forward talk-seat token/sentence deltas to human clients only.

        Other agents must not see these frames — that was the agent↔agent
        feedback loop that caused the previous removal.
        """
        if event.get('origin_channel') == self.channel_name:
            return
        if getattr(self, 'is_agent', False):
            return
        payload = {
            'type': 'agent_stream',
            'conversation_id': event.get('conversation_id', ''),
            'event_type': event.get('event_type', ''),
            'agent_id': event.get('agent_id', ''),
            'data': event.get('data', {}),
        }
        await self.send(text_data=json.dumps(payload))

    async def typing_indicator(self, event):
        if event['user_id'] != self.user.id:
            await self.send(text_data=json.dumps({
                'type': 'typing',
                'conversation_id': event.get('conversation_id', ''),
                'user_id': event['user_id'],
                'is_typing': event['is_typing'],
            }))

    async def message_read_receipt(self, event):
        if event['user_id'] != self.user.id:
            await self.send(text_data=json.dumps({
                'type': 'message_read',
                'conversation_id': event.get('conversation_id', ''),
                'user_id': event['user_id'],
                'message_id': event['message_id'],
            }))

    async def message_delivered_receipt(self, event):
        await self.send(text_data=json.dumps({
            'type': 'message_delivered',
            'user_id': event['user_id'],
            'message_id': event['message_id'],
        }))

    async def conversation_created(self, event):
        """User added to a new conversation — auto-join the group."""
        conv_id = event.get('conversation_id')
        if conv_id:
            group = f'chat_{conv_id}'
            await self.channel_layer.group_add(group, self.channel_name)
            self.conversation_groups.add(group)
            conv_id_str = str(conv_id)
            if conv_id_str in self._announced_conversation_ids_this_connection:
                return
            self._announced_conversation_ids_this_connection.add(conv_id_str)
            await self.send(text_data=json.dumps({
                'type': 'conversation_created',
                'conversation_id': conv_id,
                'conversation': event.get('conversation', {}),
            }))

    async def dm_notification(self, event):
        """CON-15 B.2: forward a gated/deduped in-app DM notification (dashboard or
        agent-runtime inbox) to the client. The notification policy engine already
        decided this recipient+channel should be notified exactly once; the consumer
        just relays the event frame."""
        await self.send(text_data=json.dumps({
            'type': 'dm_notification',
            'conversation_id': event.get('conversation_id'),
            'message_id': event.get('message_id'),
            'sender_id': event.get('sender_id'),
            'sender_name': event.get('sender_name'),
            'title': event.get('title'),
            'preview': event.get('preview'),
        }))

    async def presence_update(self, event):
        """Forward presence_global broadcasts as `presence` frames.

        Skip self-echo so a client doesn't flip itself online/offline in its
        own UI. Shape matches the `presence` event previously emitted by
        shizuha-agent's unified WS (entity_type/entity_id/status) so the
        Kotlin app and ori-expo handle it with no client-side change.
        """
        uid = event.get('user_id')
        if uid is None:
            return
        try:
            if int(uid) == int(self.user.id):
                return
        except (TypeError, ValueError):
            pass
        await self.send(text_data=json.dumps({
            'type': 'presence',
            'data': {
                'entity_type': 'user',
                'entity_id': str(uid),
                'status': event.get('status', 'offline'),
                'active_session_count': event.get('active_session_count', 0),
                'degraded_session_count': event.get('degraded_session_count', 0),
                'last_seen_at': event.get('last_seen_at'),
            },
        }))

    async def user_joined(self, event):
        pass  # Not needed for multiplexed consumer

    async def user_left(self, event):
        pass

    async def message_edited(self, event):
        await self.send(text_data=json.dumps({
            'type': 'message_edited',
            'message': event.get('message_data', {}),
        }))

    async def message_deleted(self, event):
        await self.send(text_data=json.dumps({
            'type': 'message_deleted',
            'message_id': event['message_id'],
        }))

    async def participant_added(self, event):
        await self.send(text_data=json.dumps({
            'type': 'participant_added',
            'participant': event['participant'],
        }))

    async def participant_removed(self, event):
        await self.send(text_data=json.dumps({
            'type': 'participant_removed',
            'user_id': event['user_id'],
        }))
        if event['user_id'] == self.user.id:
            # Remove from this conversation's group
            conv_id = event.get('conversation_id')
            if conv_id:
                group = f'chat_{conv_id}'
                await self.channel_layer.group_discard(group, self.channel_name)
                self.conversation_groups.discard(group)

    async def group_updated(self, event):
        await self.send(text_data=json.dumps({
            'type': 'group_updated',
            'name': event.get('name', ''),
            'description': event.get('description', ''),
        }))

    async def agent_config_update(self, event):
        """Forward a live config push from InternalAgentConfigView to the bridge WS.

        HIVE-215: InternalAgentConfigView broadcasts to user_inbox_<id>;
        this handler receives it and forwards the payload over the WS so the
        connected agent bridge (claude/codex/openclaw) can apply the config live.
        Only agent connections receive this (human clients silently drop it).
        """
        if not self.is_agent:
            return
        await self.send(text_data=json.dumps({
            'type': 'agent_config_update',
            'config': event.get('config', {}),
        }))

    # ── Database helpers ──

    @database_sync_to_async
    def _get_user_conversations(self):
        from connect.models import ConversationParticipant
        return list(visible_conversation_ids_for_user(self.user.id, include_archived=True))

    @database_sync_to_async
    def _can_access_conversation(self, conversation_id, user_id):
        return can_access_conversation(conversation_id, user_id)

    async def _push_missed_messages(self, since_event_id=None, record_reconnect_metric=False):
        """
        Replay messages the user missed while offline across all their
        conversations. Uses a three-tier cursor strategy (first match wins):

        1. Client-supplied `since_event_id` — most precise, used for `sync`.
        2. Participant's `last_read_event_id` — server-tracked cursor,
           advances on every `message_read` receipt.
        3. Time-based fallback (`last_read_at` or now-30min) — used only
           when neither cursor exists (pre-migration rows).

        The event-log cursor paths sort by `event_id ASC` — strictly monotonic
        across the whole table, globally ordered regardless of clock skew.

        Emits each row as a separate `missed_message` frame so the client can
        stream them through the same upsert helper as live `new_message`.
        """
        from connect.models import DirectMessage, ConversationParticipant
        from connect.serializers.messaging import DirectMessageSerializer
        def _query():
            if since_event_id is not None:
                # Client drove the cursor — use it verbatim, scope to the
                # user's conversations + filter out self-sent messages the
                # client's own session already emitted locally.
                conv_ids = visible_conversation_ids_for_user(self.user.id, include_archived=True)
                return list(DirectMessage.objects.filter(
                    conversation_id__in=conv_ids,
                    event_id__gt=since_event_id,
                    is_deleted=False,
                ).exclude(
                    sender_id=self.user.id,
                ).select_related('reply_to').prefetch_related(
                    'read_receipts', 'delivery_receipts',
                ).order_by('event_id')[:500])

            # No client cursor — fall back to per-participant state.
            visible_ids = set(visible_conversation_ids_for_user(self.user.id, include_archived=True))
            participants = list(ConversationParticipant.objects.filter(
                user_id=self.user.id, has_left=False, conversation_id__in=visible_ids,
            ).values('conversation_id', 'last_read_event_id', 'last_read_at'))
            if not participants:
                return []
            default_time_cutoff = timezone.now() - timedelta(minutes=30)
            replay_limit, replay_floor = _replay_limits_for(getattr(self, 'is_agent', False))
            if replay_limit <= 0:
                return []
            out = []
            for p in participants:
                qs = DirectMessage.objects.filter(
                    conversation_id=p['conversation_id'], is_deleted=False,
                ).exclude(
                    sender_id=self.user.id,
                ).exclude(
                    deletions__user_id=self.user.id,
                ).exclude(
                    delivery_receipts__user_id=self.user.id,
                )
                if p['last_read_event_id'] is not None:
                    qs = qs.filter(event_id__gt=p['last_read_event_id']).order_by('event_id')
                else:
                    time_cutoff = p['last_read_at'] or default_time_cutoff
                    cutoff = max(time_cutoff, replay_floor)
                    qs = qs.filter(created_at__gt=cutoff).order_by('created_at')
                out.extend(list(qs.select_related('reply_to').prefetch_related(
                    'read_receipts', 'delivery_receipts',
                )[:replay_limit]))
            # Combined ordering: prefer event_id when present, else created_at.
            out.sort(key=lambda m: (m.event_id is None, m.event_id or 0, m.created_at))
            return out[:replay_limit]

        try:
            rows = await database_sync_to_async(_query)()
        except Exception:
            logger.exception("Failed to fetch missed messages for user %s", self.user.id)
            return

        if not rows:
            if record_reconnect_metric:
                observe_dm_replay_per_reconnect(0)
            return

        missed_data = await database_sync_to_async(
            lambda: DirectMessageSerializer(rows, many=True).data
        )()

        delivered_ids = []
        for msg_data in missed_data:
            try:
                await self.send(text_data=json.dumps({
                    'type': 'missed_message',
                    'conversation_id': msg_data.get('conversation_id', ''),
                    'message': msg_data,
                }))
                delivered_ids.append(msg_data['id'])
            except Exception:
                break  # WS dead

        if delivered_ids:
            record_dm_replay_fanout(
                user_role_label(getattr(self, 'is_agent', False)),
                len(delivered_ids),
            )
        if record_reconnect_metric:
            observe_dm_replay_per_reconnect(len(delivered_ids))

        if delivered_ids:
            await self._record_bulk_delivery(delivered_ids)

    async def _push_history(self, conversation_id, before_event_id, limit):
        """
        Scroll-back pagination via WS (K5). Returns the N rows immediately
        before `before_event_id` in the given conversation, newest-first on
        the wire (client reverses as needed). Falls back to `created_at DESC`
        pagination when cursor is null (first scroll-back before any
        event_id is known — e.g. from a cache that predates the migration).
        """
        from connect.models import DirectMessage, ConversationParticipant
        from connect.serializers.messaging import DirectMessageSerializer

        def _query():
            # Verify participation (don't leak other users' conversations).
            if not can_access_conversation(conversation_id, self.user.id):
                return None
            qs = DirectMessage.objects.filter(
                conversation_id=conversation_id, is_deleted=False,
            ).select_related('reply_to').prefetch_related(
                'read_receipts', 'delivery_receipts',
            )
            if before_event_id is not None:
                qs = qs.filter(event_id__lt=before_event_id).order_by('-event_id')
            else:
                qs = qs.order_by('-created_at', '-id')
            return list(qs[:limit])

        try:
            rows = await database_sync_to_async(_query)()
        except Exception:
            logger.exception("Failed history fetch for conv %s", conversation_id)
            return

        if rows is None:
            # Not a participant — silently ignore, don't leak existence.
            return

        data = await database_sync_to_async(
            lambda: DirectMessageSerializer(rows, many=True).data
        )()

        await self.send(text_data=json.dumps({
            'type': 'history',
            'conversation_id': str(conversation_id),
            'before_event_id': before_event_id,
            'messages': data,
        }))

    @database_sync_to_async
    def _record_bulk_delivery(self, message_ids):
        """Bulk-record delivery receipts for messages this user just received.

        Returns the number of newly-created delivery rows so live-delivery
        metrics only count durable MessageDelivery writes.
        """
        from connect.models import MessageDelivery, DirectMessage
        try:
            messages = list(DirectMessage.objects.filter(
                id__in=message_ids,
            ).exclude(sender_id=self.user.id))
            created = 0
            for message in messages:
                receipt, was_created = MessageDelivery.objects.get_or_create(
                    message=message,
                    user_id=self.user.id,
                    defaults={
                        'processing_ack_required': self.processing_ack_capable,
                    },
                )
                if was_created:
                    created += 1
                elif self.processing_ack_capable and not receipt.processing_ack_required:
                    receipt.processing_ack_required = True
                    receipt.save(update_fields=['processing_ack_required'])
            return created
        except Exception:
            logger.exception("Failed to bulk-record delivery receipts")
            return 0

    @database_sync_to_async
    def _persist_message(self, conversation_id, content, client_message_id=None):
        from connect.models import DirectMessage, Conversation, ConversationParticipant
        from connect.serializers.messaging import DirectMessageSerializer
        try:
            # Strip outer whitespace. Claude and other LLMs often prefix
            # responses with a stray \n\n which looks ugly in chat bubbles.
            # Harmless for humans (clients trim on send anyway).
            content = (content or '').strip()
            conv = Conversation.objects.filter(id=conversation_id).first()
            if not conv:
                return None
            # Verify participation and block policy.
            if not can_access_conversation(conversation_id, self.user.id):
                return None
            msg = DirectMessage.objects.create(
                conversation=conv,
                sender_id=self.user.id,
                sender_email=getattr(self.user, 'email', ''),
                sender_name=f"{getattr(self.user, 'first_name', '')} {getattr(self.user, 'last_name', '')}".strip() or self.user.username,
                sender_username=getattr(self.user, 'username', '') or '',
                content=content,
                client_message_id=client_message_id,
                # Agent-identity stamping — `is_agent` comes from the JWT
                # claim set by shizuha-id (based on profile.account_type).
                # `agent_id` is deliberately None: sender_id + sender_is_agent
                # are sufficient. Legacy column slated for removal.
                sender_is_agent=self.is_agent,
                agent_id=None,
            )
            # Write an idempotency record so REST retries with the same
            # client_message_id replay cleanly instead of hitting the unique
            # constraint and returning a 500 (CON-9 cycle-6 P2).
            if client_message_id and conv.conversation_type == 'direct':
                other = ConversationParticipant.objects.filter(
                    conversation=conv, has_left=False,
                ).exclude(user_id=self.user.id).first()
                if other:
                    from connect.models import DirectMessageIdempotency
                    from connect.views.messaging import _dm_request_hash
                    DirectMessageIdempotency.objects.get_or_create(
                        sender_id=self.user.id,
                        client_message_id=client_message_id,
                        defaults={
                            'recipient_id': other.user_id,
                            'conversation': conv,
                            'message': msg,
                            'request_hash': _dm_request_hash(
                                sender_id=self.user.id,
                                recipient_id=other.user_id,
                                content=content,
                                reply_to_message_id=None,
                            ),
                        },
                    )
            conv.last_message_at = msg.created_at
            conv.save(update_fields=['last_message_at'])
            # Increment unread only for recipients allowed by block policy.
            allowed_ids = [
                uid for uid in ConversationParticipant.objects.filter(
                    conversation_id=conversation_id, has_left=False
                ).values_list('user_id', flat=True)
                if uid != self.user.id and actor_can_reach_recipient(self.user.id, uid)
            ]
            if allowed_ids:
                ConversationParticipant.objects.filter(
                    conversation_id=conversation_id,
                    has_left=False,
                    user_id__in=allowed_ids,
                ).update(unread_count=models.F('unread_count') + 1)
            return DirectMessageSerializer(msg).data
        except Exception:
            logger.exception("Failed to persist message from user_id=%s", self.user.id)
            return None

    @database_sync_to_async
    def _persist_read_receipt(self, conversation_id, message_id):
        """
        Mark `message_id` as read + cascade to every prior unread message
        from other senders in the conversation. Returns the list of all
        newly-read message ids so the caller can broadcast them as a
        batch (matches WhatsApp: opening a chat marks all unread as read).

        Advances both `last_read_at` (time cutoff, retained for UI hints)
        and `last_read_event_id` (cursor into the event-log space used for
        reconnect replay — see MESSAGING_KAFKA.md).
        """
        from connect.models import MessageRead, DirectMessage, ConversationParticipant
        newly_read = []
        try:
            if not can_access_conversation(conversation_id, self.user.id):
                return newly_read
            msg = DirectMessage.objects.filter(
                id=message_id, conversation_id=conversation_id
            ).first()
            if not msg:
                return newly_read

            already_read_ids = set(MessageRead.objects.filter(
                message__conversation_id=conversation_id,
                user_id=self.user.id,
            ).values_list('message_id', flat=True))

            candidates = DirectMessage.objects.filter(
                conversation_id=conversation_id,
                created_at__lte=msg.created_at,
            ).exclude(sender_id=self.user.id).values_list('id', flat=True)

            to_mark = [mid for mid in candidates if mid not in already_read_ids]
            if to_mark:
                MessageRead.objects.bulk_create(
                    [MessageRead(message_id=mid, user_id=self.user.id) for mid in to_mark],
                    ignore_conflicts=True,
                )
                newly_read = [str(mid) for mid in to_mark]

            # Advance cursors. `event_id` may be NULL on pre-migration rows;
            # in that case we keep the existing last_read_event_id (don't
            # regress it to NULL). Read-receipt-driven advancement is
            # monotonic — never moves backwards even if an older message is
            # explicitly marked read after a newer one.
            update_fields = {'last_read_at': timezone.now(), 'unread_count': 0}
            target_event_id = getattr(msg, 'event_id', None)
            if target_event_id is not None:
                # COALESCE + GREATEST would be cleaner but requires raw SQL;
                # read the current value first.
                current = ConversationParticipant.objects.filter(
                    conversation_id=conversation_id,
                    user_id=self.user.id,
                ).values_list('last_read_event_id', flat=True).first()
                if current is None or target_event_id > current:
                    update_fields['last_read_event_id'] = target_event_id
            ConversationParticipant.objects.filter(
                conversation_id=conversation_id,
                user_id=self.user.id,
            ).update(**update_fields)
        except Exception:
            logger.exception("Failed to persist read receipt")
        return newly_read
