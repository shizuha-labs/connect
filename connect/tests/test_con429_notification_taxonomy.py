"""CON-429 S2: notification taxonomy + digest invariants.

Discharges the design's test-asserted invariants (ichi advisory anchors):

- Anchor 1: the digest queryset's class filter excludes `alert` at the
  QUERY level — a batching bug cannot batch an andon.
- Anchor 3: the digest queryset excludes its own posts — no self-feeding.
- Wake-relevant producers never fold (§2.1 ichi ruling).
- `notification_class` is immutable at mint (§2.2); lifecycle changes go
  through `notification_state`.
- Both ingress legs stamp: `InternalSystemMessageView` validates the
  payload class against the server-side allowlist (reject, never coerce)
  and `_create_system_message` stamps the fallback default.
- Digest posts once per window; channels without the flag are skipped.
- Backfill is idempotent, defaults ambiguous rows to informational +
  logged, and grandfathers pre-taxonomy unread rows (§7.2/§7.3).
"""
import hashlib
import hmac
import time
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient

from connect.models import (
    Conversation, DirectMessage, MessageRead, VALID_NOTIFICATION_CLASSES,
)
from connect.services.notification_digest import (
    DIGEST_SERVICE_TAG,
    build_digest_post,
    digest_queryset,
    digest_window,
)
from connect.management.commands.backfill_notification_classes import (
    Command as BackfillCommand,
)

SYSTEM_USER_ID = 999
RECIPIENT_ID = 42
ORIGIN_SECRET = 'test-origin-secret'
ORIGIN_ALLOWLIST = {'shizuha-pulse'}


def _origin_token(sender_service, recipient_user_id, content):
    """Compute a valid X-System-Message-Token (mirrors the view's contract)."""
    ts = int(time.time())
    body_hash = hashlib.sha256((content or '').encode('utf-8')).hexdigest()
    payload = f'{sender_service}|{int(recipient_user_id)}|{body_hash}|{ts}'
    supplied = hmac.new(
        ORIGIN_SECRET.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256,
    ).hexdigest()
    return f'{ts}:{supplied}'


def _mint_system_dm():
    """Mint the dedicated System→recipient DM the internal view posts into."""
    direct_key = Conversation.build_direct_key(SYSTEM_USER_ID, RECIPIENT_ID)
    conv, _ = Conversation.objects.get_or_create(
        conversation_type='direct', direct_key=direct_key,
    )
    return conv


class DigestQueryInvariantTests(TestCase):
    """ichi anchors 1 + 3 + the §2.1 wake-relevant ruling."""

    def setUp(self):
        self.conv = Conversation.objects.create(conversation_type='group', name='System')

    def _window(self):
        """Half-open [start, end) captured AFTER minting — the digest window
        ends at 'now', so rows minted before the capture must fall inside."""
        now = timezone.now()
        return now - timedelta(hours=1), now

    def _mint(self, cls, **kwargs):
        return DirectMessage.objects.create(
            conversation=self.conv,
            sender_id=0, sender_email='system',
            message_type='system', content=kwargs.pop('content', 'event'),
            notification_class=cls,
            **kwargs,
        )

    def test_digest_queryset_excludes_alert_class(self):
        """Anchor 1: an alert row in the window can never match the digest query."""
        self._mint('informational', content='routing hold')
        self._mint('alert', content='andon pull')
        rows = list(digest_queryset(self.conv, *self._window()))
        self.assertEqual([r.content for r in rows], ['routing hold'])

    def test_digest_queryset_excludes_its_own_posts(self):
        """Anchor 3: digest posts (informational, connect-digest origin) never re-enter."""
        self._mint('informational', content='routing hold')
        self._mint(
            'informational',
            content='Digest (daily) for ...',
            media_metadata={'sender_service': DIGEST_SERVICE_TAG},
        )
        rows = list(digest_queryset(self.conv, *self._window()))
        self.assertEqual([r.content for r in rows], ['routing hold'])

    def test_digest_queryset_excludes_wake_relevant_producers(self):
        """§2.1: wake-relevant informational producers never fold into digests."""
        self._mint('informational', content='routing hold')
        self._mint(
            'informational',
            content='cycle fired',
            sender_name='Pulse Scheduler',
            media_metadata={'sender_service': 'pulse'},
        )
        rows = list(digest_queryset(self.conv, *self._window()))
        self.assertEqual([r.content for r in rows], ['routing hold'])

    def test_digest_window_frequencies(self):
        now = timezone.now()
        start, end = digest_window(now=now, frequency='daily')
        self.assertEqual(end - start, timedelta(days=1))
        start, end = digest_window(now=now, frequency='weekly')
        self.assertEqual(end - start, timedelta(days=7))
        with self.assertRaises(ValueError):
            digest_window(now=now, frequency='hourly')


class DigestPostTests(TestCase):
    def setUp(self):
        self.conv = Conversation.objects.create(conversation_type='group', name='System')
        self.now = timezone.now()

    def test_skips_channels_without_the_flag(self):
        """§10 Q2: default OFF — no flag, no digest post."""
        DirectMessage.objects.create(
            conversation=self.conv, sender_id=0, sender_email='system',
            message_type='system', content='routing hold',
            notification_class='informational',
        )
        created, post, count = build_digest_post(self.conv, now=self.now)
        self.assertFalse(created)
        self.assertIsNone(post)
        self.assertEqual(count, 0)

    def test_posts_once_per_window(self):
        """A repeat run for the same window mints nothing (once-per-window)."""
        self.conv.digest_enabled = True
        self.conv.save()
        DirectMessage.objects.create(
            conversation=self.conv, sender_id=0, sender_email='system',
            message_type='system', content='routing hold',
            notification_class='informational',
        )
        # Window captured AFTER minting — the half-open [start, end) window
        # ends at 'now', so the event row must predate the capture.
        created, post, count = build_digest_post(self.conv, now=timezone.now())
        self.assertTrue(created)
        self.assertEqual(count, 1)
        self.assertEqual(post.notification_class, 'informational')
        self.assertEqual(
            (post.media_metadata or {}).get('sender_service'), DIGEST_SERVICE_TAG,
        )
        # Repeat run for the same window: 200-with-nothing shape. The latest
        # digest post (minted between the calls) now satisfies
        # latest.created_at >= start, so the repeat mints nothing.
        created2, post2, count2 = build_digest_post(self.conv, now=timezone.now())
        self.assertFalse(created2)
        self.assertIsNone(post2)
        self.assertEqual(count2, 0)

    def test_empty_window_mints_nothing(self):
        self.conv.digest_enabled = True
        self.conv.save()
        created, post, count = build_digest_post(self.conv, now=self.now)
        self.assertFalse(created)
        self.assertEqual(count, 0)


class TaxonomySemanticsTests(TestCase):
    """§2.2: immutable-at-mint class + mutable state."""

    def setUp(self):
        self.conv = Conversation.objects.create(conversation_type='group', name='System')

    def test_notification_class_immutable_at_mint(self):
        msg = DirectMessage.objects.create(
            conversation=self.conv, sender_id=0, sender_email='system',
            message_type='system', content='routing hold',
            notification_class='informational',
        )
        msg.notification_class = 'alert'
        with self.assertRaises(ValueError):
            msg.save()

    def test_notification_state_is_mutable(self):
        msg = DirectMessage.objects.create(
            conversation=self.conv, sender_id=0, sender_email='system',
            message_type='system', content='task assigned',
            notification_class='actionable',
        )
        msg.notification_state = 'needs_action'
        msg.save()
        msg.refresh_from_db()
        self.assertEqual(msg.notification_state, 'needs_action')

    def test_allowlist_vocabulary(self):
        self.assertEqual(
            VALID_NOTIFICATION_CLASSES, {'actionable', 'informational', 'alert'},
        )


@override_settings(
    CONNECT_SYSTEM_MESSAGE_ORIGIN_SECRET=ORIGIN_SECRET,
    CONNECT_SYSTEM_MESSAGE_ALLOWLIST=ORIGIN_ALLOWLIST,
)
class InternalIngressStampingTests(APITestCase):
    """§2.1: the internal leg stamps the payload class (reject, never coerce)."""

    def setUp(self):
        # `_get_system_user_id` makes a one-shot HTTP call to shizuha-id —
        # mock it to the fixture System user for the whole class.
        mock.patch(
            'connect.views.internal_api._get_system_user_id',
            return_value=SYSTEM_USER_ID,
        ).start()
        self.client = APIClient()
        self.url = '/api/internal/system-message/'
        self.conv = _mint_system_dm()

    def _post(self, content='task SM-1 due', notification_class='__absent__'):
        payload = {
            'recipient_user_id': RECIPIENT_ID,
            'content': content,
            'sender_name': 'Pulse Scheduler',
            'sender_service': 'shizuha-pulse',
        }
        if notification_class != '__absent__':
            payload['notification_class'] = notification_class
        return self.client.post(
            self.url, payload, format='json',
            HTTP_X_INTERNAL_SERVICE='shizuha-pulse',
            HTTP_X_SYSTEM_MESSAGE_TOKEN=_origin_token(
                'shizuha-pulse', RECIPIENT_ID, content,
            ),
        )

    def test_stamps_payload_class(self):
        resp = self._post(notification_class='actionable')
        # The internal view's CREATE path returns 201 (Created); the
        # idempotent-replay path returns 200. First post here → 201.
        self.assertEqual(resp.status_code, 201)
        msg = DirectMessage.objects.get(conversation=self.conv)
        self.assertEqual(msg.notification_class, 'actionable')
        self.assertEqual(msg.notification_state, 'unread')

    def test_defaults_to_informational_when_absent(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 201)
        msg = DirectMessage.objects.get(conversation=self.conv)
        self.assertEqual(msg.notification_class, 'informational')

    def test_rejects_unknown_class_with_400(self):
        resp = self._post(notification_class='noise')
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            DirectMessage.objects.filter(conversation=self.conv).exists(),
        )


class FallbackLegStampingTests(TestCase):
    """§2.1: `_create_system_message` stamps the fallback so direct callers
    cannot mint unclassified rows."""

    def setUp(self):
        from connect.views.messaging import _create_system_message
        self._create_system_message = _create_system_message
        self.conv = Conversation.objects.create(conversation_type='group', name='System')

    def test_stamps_fallback_informational(self):
        msg = self._create_system_message(self.conv, 'group created')
        self.assertEqual(msg.notification_class, 'informational')

    def test_caller_can_pass_a_class(self):
        msg = self._create_system_message(self.conv, 'task assigned', notification_class='actionable')
        self.assertEqual(msg.notification_class, 'actionable')

    def test_invalid_class_is_rejected_never_coerced(self):
        with self.assertRaises(ValueError):
            self._create_system_message(self.conv, 'x', notification_class='noise')
        self.assertFalse(DirectMessage.objects.filter(conversation=self.conv).exists())


class BackfillTests(TestCase):
    """§7.1–§7.3: idempotent, ambiguity default, grandfathering."""

    def setUp(self):
        self.conv = Conversation.objects.create(conversation_type='group', name='System')
        self.cmd = BackfillCommand()

    def test_classifies_maps_and_grandfathers(self):
        DirectMessage.objects.create(  # confident map
            conversation=self.conv, sender_id=0, sender_email='system',
            message_type='text', content='andon',
            sender_name='Shizuha Alert Router',
        )
        DirectMessage.objects.create(  # `_create_system_message` leg
            conversation=self.conv, sender_id=0, sender_email='system',
            message_type='system', content='group created',
        )
        DirectMessage.objects.create(  # ambiguous → informational + logged
            conversation=self.conv, sender_id=7, sender_email='x@shizuha.com',
            message_type='text', content='mystery sender',
            sender_name='Mystery Bot',
        )
        out = _capture_command(self.cmd, [])
        self.assertIn('3 classified', out)
        self.assertIn('1 ambiguous', out)
        rows = {r.sender_name: r for r in DirectMessage.objects.all()}
        self.assertEqual(rows['Shizuha Alert Router'].notification_class, 'alert')
        self.assertEqual(rows[''].notification_class, 'informational')  # system leg
        self.assertEqual(rows['Mystery Bot'].notification_class, 'informational')
        # §7.3: pre-taxonomy unread rows grandfather as read_eligible.
        for r in rows.values():
            self.assertEqual(r.notification_state, 'read_eligible')

    def test_pre_taxonomy_alert_router_row_classifies_alert(self):
        """rui 09-23 review 5315: pre-taxonomy alert-router rows mint with the
        SYSTEM shape (message_type='system', sender_id=0) and class NULL — the
        generic early return fired before the SENDER_CLASS_MAP lookup, so the
        map's 'Shizuha Alert Router': 'alert' branch was unreachable for
        exactly the rows it exists for. Map-first ordering reaches it: the row
        classifies alert AND stays out of the ambiguous list (it folded as
        "confident"-informational before, silently losing the class)."""
        DirectMessage.objects.create(
            conversation=self.conv, sender_id=0, sender_email='system',
            message_type='system', content='stall detected',
            sender_name='Shizuha Alert Router',
        )
        out = _capture_command(self.cmd, [])
        self.assertIn('1 classified', out)
        self.assertIn('0 ambiguous', out)  # not folded into the logged list
        row = DirectMessage.objects.get()
        self.assertEqual(row.notification_class, 'alert')
        self.assertEqual(row.notification_state, 'read_eligible')  # §7.3

    def test_rerun_is_idempotent(self):
        msg = DirectMessage.objects.create(
            conversation=self.conv, sender_id=0, sender_email='system',
            message_type='system', content='group created',
        )
        _capture_command(self.cmd, [])
        first_cls = DirectMessage.objects.get(pk=msg.pk).notification_class
        _capture_command(self.cmd, [])
        again = DirectMessage.objects.get(pk=msg.pk)
        self.assertEqual(again.notification_class, first_cls)
        self.assertEqual(again.notification_state, 'read_eligible')

    def test_already_read_rows_mint_read_state(self):
        msg = DirectMessage.objects.create(
            conversation=self.conv, sender_id=0, sender_email='system',
            message_type='system', content='group created',
        )
        MessageRead.objects.create(message=msg, user_id=RECIPIENT_ID)
        _capture_command(self.cmd, [])
        self.assertEqual(DirectMessage.objects.get(pk=msg.pk).notification_state, 'read')


def _capture_command(cmd, argv):
    """Run a management command capturing stdout (no call_command DB reload)."""
    from io import StringIO
    from django.core.management import call_command
    buf = StringIO()
    call_command(cmd, *argv, stdout=buf)
    return buf.getvalue()
