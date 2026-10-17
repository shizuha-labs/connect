"""CON-15 (CON-6 P2): notification policy engine + idempotent fanout tests."""
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from datetime import datetime, time, timedelta, timezone as dt_timezone

from connect.models import (
    Conversation, ConversationParticipant, DirectMessage, NotificationDispatch,
    NotificationPreference, BridgeDeliveryCooldown, PresenceSession,
)
from connect.services import notifications as N


def _allow_all(message):
    """Default privacy stub: every participant is delivery-allowed (no blocks)."""
    return [p.user_id for p in message.conversation.participants.all()]


class NotificationPolicyTests(TestCase):
    def setUp(self):
        self.conv = Conversation.objects.create(conversation_type='direct')
        self.sender = ConversationParticipant.objects.create(
            conversation=self.conv, user_id=1, has_left=False)
        self.recipient = ConversationParticipant.objects.create(
            conversation=self.conv, user_id=2, has_left=False,
            notifications_enabled=True, is_muted=False)
        self.msg = DirectMessage.objects.create(
            conversation=self.conv, sender_id=1, content='hi', event_id=100)

    def _should(self):
        with patch.object(N, 'should_notify', wraps=N.should_notify):
            return N.should_notify(self.recipient, self.msg)

    # --- suppression conditions (ordered) -----------------------------------
    def test_pass_when_nothing_suppresses(self):
        with patch('connect.services.privacy.allowed_delivery_user_ids', _allow_all):
            notify, reason = N.should_notify(self.recipient, self.msg)
        self.assertTrue(notify)
        self.assertIsNone(reason)

    def test_service_disabled(self):
        self.recipient.notifications_enabled = False
        notify, reason = N.should_notify(self.recipient, self.msg)
        self.assertFalse(notify)
        self.assertEqual(reason, 'service_disabled')

    def test_muted_permanent(self):
        self.recipient.is_muted = True
        self.recipient.muted_until = None
        notify, reason = N.should_notify(self.recipient, self.msg)
        self.assertFalse(notify)
        self.assertEqual(reason, 'muted')

    def test_muted_timed_still_active(self):
        self.recipient.is_muted = True
        self.recipient.muted_until = timezone.now() + timedelta(hours=1)
        self.assertEqual(N.should_notify(self.recipient, self.msg)[1], 'muted')

    def test_muted_window_expired_does_not_suppress(self):
        self.recipient.is_muted = True
        self.recipient.muted_until = timezone.now() - timedelta(hours=1)
        with patch('connect.services.privacy.allowed_delivery_user_ids', _allow_all):
            notify, _ = N.should_notify(self.recipient, self.msg)
        self.assertTrue(notify)  # expired mute no longer suppresses

    def test_blocked(self):
        # recipient not in the delivery-allowed set => blocked suppression.
        with patch('connect.services.privacy.allowed_delivery_user_ids', lambda m: [999]):
            notify, reason = N.should_notify(self.recipient, self.msg)
        self.assertFalse(notify)
        self.assertEqual(reason, 'blocked')

    def test_read_receipt_race(self):
        # recipient already read at/past this message's event_id (read elsewhere).
        self.recipient.last_read_event_id = 100  # == msg.event_id
        with patch('connect.services.privacy.allowed_delivery_user_ids', _allow_all):
            notify, reason = N.should_notify(self.recipient, self.msg)
        self.assertFalse(notify)
        self.assertEqual(reason, 'read_race')

    def test_read_cursor_behind_does_not_suppress(self):
        self.recipient.last_read_event_id = 99  # < msg.event_id => still unread
        with patch('connect.services.privacy.allowed_delivery_user_ids', _allow_all):
            notify, _ = N.should_notify(self.recipient, self.msg)
        self.assertTrue(notify)

    def test_order_service_disabled_beats_blocked(self):
        # first matching suppressor wins (deterministic reason).
        self.recipient.notifications_enabled = False
        with patch('connect.services.privacy.allowed_delivery_user_ids', lambda m: [999]):
            self.assertEqual(N.should_notify(self.recipient, self.msg)[1], 'service_disabled')

    # --- idempotency gate ---------------------------------------------------
    def test_mark_dispatched_is_idempotent(self):
        first = N.mark_dispatched(2, self.conv.id, 100, N.MOBILE)
        second = N.mark_dispatched(2, self.conv.id, 100, N.MOBILE)
        self.assertTrue(first)    # first time -> perform fanout
        self.assertFalse(second)  # dup -> skip
        self.assertEqual(NotificationDispatch.objects.filter(
            recipient_id=2, conversation=self.conv, event_id=100, channel=N.MOBILE).count(), 1)

    def test_mark_dispatched_independent_per_channel_event_recipient(self):
        self.assertTrue(N.mark_dispatched(2, self.conv.id, 100, N.MOBILE))
        self.assertTrue(N.mark_dispatched(2, self.conv.id, 100, N.DASHBOARD))  # diff channel
        self.assertTrue(N.mark_dispatched(2, self.conv.id, 101, N.MOBILE))     # diff event
        self.assertTrue(N.mark_dispatched(3, self.conv.id, 100, N.MOBILE))     # diff recipient
        self.assertEqual(NotificationDispatch.objects.count(), 4)

    def test_mark_dispatched_no_event_id_not_deduped(self):
        # a message without an event_id is never silently dropped.
        self.assertTrue(N.mark_dispatched(2, self.conv.id, None, N.MOBILE))
        self.assertTrue(N.mark_dispatched(2, self.conv.id, None, N.MOBILE))

    def test_eligible_recipients_excludes_sender_and_suppressed(self):
        muted = ConversationParticipant.objects.create(
            conversation=self.conv, user_id=3, has_left=False, is_muted=True)
        with patch('connect.services.privacy.allowed_delivery_user_ids', _allow_all):
            ids = {p.user_id for p in N.eligible_recipients(self.msg)}
        self.assertIn(2, ids)        # normal recipient
        self.assertNotIn(1, ids)     # sender excluded
        self.assertNotIn(3, ids)     # muted suppressed


class B2ChannelPolicyTests(TestCase):
    """CON-15 B.2: the 3 new suppression data sources + the channel-aware decision
    (active-conversation-open, quiet hours, external-bridge cooldown, per-channel
    opt-out) — kai's contract + test requirements."""

    def setUp(self):
        self.conv = Conversation.objects.create(conversation_type='direct')
        self.recipient = ConversationParticipant.objects.create(
            conversation=self.conv, user_id=2, has_left=False,
            notifications_enabled=True, is_muted=False)
        self.msg = DirectMessage.objects.create(
            conversation=self.conv, sender_id=1, content='hi', event_id=100)
        # Deterministic reference instant (UTC).
        self.now = datetime(2026, 6, 15, 23, 30, tzinfo=dt_timezone.utc)

    def _allow(self):
        return patch('connect.services.privacy.allowed_delivery_user_ids', _allow_all)

    # --- active-conversation-open (global suppressor) -----------------------
    def _focus_session(self, *, conv_id, updated_delta_s, status='online'):
        return PresenceSession.objects.create(
            user_id=2, session_key=f'sess-{updated_delta_s}-{status}',
            status=status, last_heartbeat_at=self.now,
            active_conversation_id=conv_id,
            active_conversation_updated_at=self.now - timedelta(seconds=updated_delta_s),
        )

    def test_active_conversation_fresh_suppresses(self):
        self._focus_session(conv_id=self.conv.id, updated_delta_s=10)  # < 60s TTL
        with self._allow():
            notify, reason = N.should_notify(self.recipient, self.msg, now=self.now)
        self.assertFalse(notify)
        self.assertEqual(reason, 'active_conversation')

    def test_active_conversation_stale_does_not_suppress(self):
        # focus timestamp older than ACTIVE_CONVERSATION_TTL_SECONDS => expired.
        self._focus_session(conv_id=self.conv.id,
                            updated_delta_s=N.ACTIVE_CONVERSATION_TTL_SECONDS + 5)
        with self._allow():
            notify, _ = N.should_notify(self.recipient, self.msg, now=self.now)
        self.assertTrue(notify)

    def test_active_conversation_other_conversation_does_not_suppress(self):
        other = Conversation.objects.create(conversation_type='direct')
        self._focus_session(conv_id=other.id, updated_delta_s=5)
        with self._allow():
            notify, _ = N.should_notify(self.recipient, self.msg, now=self.now)
        self.assertTrue(notify)

    def test_active_conversation_requires_online_status(self):
        # an 'away' session foregrounding the conv does NOT suppress (not foreground).
        self._focus_session(conv_id=self.conv.id, updated_delta_s=5, status='away')
        with self._allow():
            notify, _ = N.should_notify(self.recipient, self.msg, now=self.now)
        self.assertTrue(notify)

    # --- quiet hours (channel-aware; interrupt channels only) ---------------
    def _quiet(self, start, end, tz='UTC'):
        NotificationPreference.objects.update_or_create(
            user_id=2, defaults=dict(
                quiet_hours_enabled=True, quiet_start=start, quiet_end=end, timezone=tz))

    def test_quiet_hours_suppresses_interrupt_channels_not_dashboard(self):
        # window 22:00-06:00 UTC, now 23:30 UTC => inside (crosses midnight).
        self._quiet(time(22, 0), time(6, 0))
        with self._allow():
            for ch in (N.MOBILE, N.EXTERNAL, N.AGENT):
                notify, reason = N.should_notify_channel(self.recipient, self.msg, ch, now=self.now)
                self.assertFalse(notify, ch)
                self.assertEqual(reason, 'quiet_hours', ch)
            # dashboard (in-app, non-interrupting) is NOT suppressed by quiet hours.
            notify, _ = N.should_notify_channel(self.recipient, self.msg, N.DASHBOARD, now=self.now)
            self.assertTrue(notify)

    def test_quiet_hours_outside_window_does_not_suppress(self):
        self._quiet(time(22, 0), time(6, 0))
        noon = datetime(2026, 6, 15, 12, 0, tzinfo=dt_timezone.utc)
        with self._allow():
            notify, _ = N.should_notify_channel(self.recipient, self.msg, N.MOBILE, now=noon)
        self.assertTrue(notify)

    def test_quiet_hours_non_crossing_window(self):
        # 09:00-17:00, now 12:00 => inside; 20:00 => outside.
        self._quiet(time(9, 0), time(17, 0))
        inside = datetime(2026, 6, 15, 12, 0, tzinfo=dt_timezone.utc)
        outside = datetime(2026, 6, 15, 20, 0, tzinfo=dt_timezone.utc)
        with self._allow():
            self.assertFalse(N.should_notify_channel(self.recipient, self.msg, N.MOBILE, now=inside)[0])
            self.assertTrue(N.should_notify_channel(self.recipient, self.msg, N.MOBILE, now=outside)[0])

    def test_quiet_hours_timezone_aware(self):
        # 22:00-06:00 America/New_York (EDT = UTC-4 in June).
        self._quiet(time(22, 0), time(6, 0), tz='America/New_York')
        # 02:00 UTC == 22:00 EDT (prev day) => inside the window.
        inside = datetime(2026, 6, 15, 2, 0, tzinfo=dt_timezone.utc)
        # 18:00 UTC == 14:00 EDT => outside.
        outside = datetime(2026, 6, 15, 18, 0, tzinfo=dt_timezone.utc)
        with self._allow():
            self.assertEqual(
                N.should_notify_channel(self.recipient, self.msg, N.MOBILE, now=inside)[1], 'quiet_hours')
            self.assertTrue(
                N.should_notify_channel(self.recipient, self.msg, N.MOBILE, now=outside)[0])

    # --- per-channel opt-out -------------------------------------------------
    def test_per_channel_opt_out(self):
        NotificationPreference.objects.create(user_id=2, mobile_enabled=False)
        with self._allow():
            notify, reason = N.should_notify_channel(self.recipient, self.msg, N.MOBILE, now=self.now)
            self.assertFalse(notify)
            self.assertEqual(reason, 'channel_disabled')
            # other channels remain enabled.
            self.assertTrue(N.should_notify_channel(self.recipient, self.msg, N.DASHBOARD, now=self.now)[0])

    # --- external-bridge cooldown -------------------------------------------
    def test_bridge_cooldown_suppresses_external_only(self):
        N.record_bridge_failure(2, 'tg:room', now=self.now)  # arms cooldown in the future
        with self._allow():
            notify, reason = N.should_notify_channel(self.recipient, self.msg, N.EXTERNAL, now=self.now)
            self.assertFalse(notify)
            self.assertEqual(reason, 'bridge_cooldown')
            # cooldown is external-only; mobile is unaffected.
            self.assertTrue(N.should_notify_channel(self.recipient, self.msg, N.MOBILE, now=self.now)[0])

    def test_bridge_cooldown_expires(self):
        N.record_bridge_failure(2, 'tg:room', now=self.now)
        later = self.now + timedelta(seconds=N.BRIDGE_COOLDOWN_MAX_SECONDS + 1)
        with self._allow():
            self.assertTrue(N.should_notify_channel(self.recipient, self.msg, N.EXTERNAL, now=later)[0])

    def test_bridge_cooldown_backoff_grows_and_resets(self):
        cd1 = N.record_bridge_failure(2, 'tg:room', now=self.now)
        span1 = (cd1.cooldown_until - self.now).total_seconds()
        cd2 = N.record_bridge_failure(2, 'tg:room', now=self.now)
        span2 = (cd2.cooldown_until - self.now).total_seconds()
        self.assertGreater(span2, span1)              # exponential backoff
        self.assertEqual(cd2.consecutive_failures, 2)
        N.record_bridge_success(2, 'tg:room', now=self.now)
        row = BridgeDeliveryCooldown.objects.get(recipient_id=2, bridge_channel='tg:room')
        self.assertEqual(row.consecutive_failures, 0)
        self.assertIsNone(row.cooldown_until)

    # --- idempotency across ALL channels ------------------------------------
    def test_idempotency_independent_across_all_channels(self):
        for ch in N.NOTIFICATION_CHANNELS:
            self.assertTrue(N.mark_dispatched(2, self.conv.id, 100, ch), ch)   # first
            self.assertFalse(N.mark_dispatched(2, self.conv.id, 100, ch), ch)  # dup
        self.assertEqual(NotificationDispatch.objects.count(), len(N.NOTIFICATION_CHANNELS))

    def test_channel_decision_inherits_global_suppression(self):
        # a global suppressor (muted) suppresses on every channel with its reason.
        self.recipient.is_muted = True
        self.recipient.muted_until = None
        for ch in N.NOTIFICATION_CHANNELS:
            notify, reason = N.should_notify_channel(self.recipient, self.msg, ch, now=self.now)
            self.assertFalse(notify, ch)
            self.assertEqual(reason, 'muted', ch)


class Hive602WakeOnDmTests(TestCase):
    """HIVE-602: the DM fanout nudges Hive to wake a stopped agent recipient."""

    def _participant(self, ptype='agent', uid=42, email='ichi@shizuha.com'):
        from unittest import mock as _m
        p = _m.Mock()
        p.participant_type = ptype
        p.user_id = uid
        p.user_email = email
        return p

    def test_wake_posts_to_hive_for_agent_recipient(self):
        import os
        from unittest import mock
        from connect import tasks as t
        t._WAKE_DEDUP.clear()
        msg = mock.Mock(conversation_id='c-1')
        with mock.patch.dict(os.environ, {
            'HIVE_CONNECT_WAKE_URL': 'http://hive/wake',
            'HIVE_CONNECT_WAKE_TOKEN': 'tok',
        }), mock.patch('requests.post') as post:
            post.return_value = mock.Mock(status_code=200, text='{"status":"started"}')
            t._wake_agent_runtime(self._participant(), msg)
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'http://hive/wake')
        self.assertEqual(kwargs['json']['email'], 'ichi@shizuha.com')
        self.assertEqual(kwargs['headers']['X-Hive-Wake-Token'], 'tok')

    def test_wake_dedups_within_window_and_skips_unconfigured(self):
        import os
        from unittest import mock
        from connect import tasks as t
        t._WAKE_DEDUP.clear()
        msg = mock.Mock(conversation_id='c-1')
        with mock.patch.dict(os.environ, {
            'HIVE_CONNECT_WAKE_URL': 'http://hive/wake',
            'HIVE_CONNECT_WAKE_TOKEN': 'tok',
        }), mock.patch('requests.post') as post:
            post.return_value = mock.Mock(status_code=200, text='')
            t._wake_agent_runtime(self._participant(), msg)
            t._wake_agent_runtime(self._participant(), msg)
        self.assertEqual(post.call_count, 1)
        # unconfigured → no call, no error
        t._WAKE_DEDUP.clear()
        with mock.patch.dict(os.environ, {'HIVE_CONNECT_WAKE_URL': '', 'HIVE_CONNECT_WAKE_TOKEN': ''}), \
             mock.patch('requests.post') as post2:
            t._wake_agent_runtime(self._participant(uid=43), msg)
        post2.assert_not_called()
