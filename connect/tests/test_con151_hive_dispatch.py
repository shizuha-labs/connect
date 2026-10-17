"""CON-151: owner→Hive-agent DM must actually dispatch to the Hive runtime.

Regression for the invisible dead-drop: the agent participant is created with
``nickname = stable_id[:50]`` (the RAW stable_id — prod ids look like
``hive-claw-<hex>``, hyphenated, NO ``hive:`` prefix), but
``hive_agent_stable_id_from_participant`` used to require a ``hive:`` prefix and so
returned '' for every real agent → ``dispatch_hive_agent_message`` silently no-op'd
and no owner→claw DM was ever delivered (message persisted 201, zero assistant reply).

The old con118/con120 tests masked this by using an unrealistic ``hive:claw:<uuid>``
stable_id that happened to satisfy the buggy prefix check. These tests use the REAL
hyphenated prod form.
"""
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from django.test import TestCase, override_settings

from connect.models import Conversation, ConversationParticipant, DirectMessage
from connect.services.hive_agents import (
    hive_agent_stable_id_from_participant,
    hive_agent_user_id,
    dispatch_hive_agent_message,
)

# Realistic PROD stable_id: hyphenated, NO 'hive:' prefix (the CON-151 repro form).
REAL_STABLE_ID = (f'hive-claw-{uuid4().hex}')[:50]


@override_settings(HIVE_CONNECT_HMAC_SECRET='test-secret')
class Con151HiveDispatchTests(TestCase):
    def setUp(self):
        self.context_patcher = mock.patch(
            'connect.services.hive_agents._authoritative_delivery_context',
            return_value={
                'owner_id': '1',
                'organization_id': 1,
                'agent_participant_id': '999',
            },
        )
        self.context_patcher.start()
        self.addCleanup(self.context_patcher.stop)

    def _agent_participant(self, nickname):
        return SimpleNamespace(participant_type='agent', nickname=nickname, user_id=999)

    def test_stable_id_extracted_from_realistic_agent_nickname(self):
        # The bug: prod ids don't start with 'hive:', so the old check returned ''.
        p = self._agent_participant(REAL_STABLE_ID)
        assert hive_agent_stable_id_from_participant(p) == REAL_STABLE_ID

    def test_non_agent_participant_yields_no_stable_id(self):
        human = SimpleNamespace(participant_type='human', nickname='display-nick', user_id=1)
        assert hive_agent_stable_id_from_participant(human) == ''

    @mock.patch('connect.services.hive_agents.requests.post')
    def test_dispatch_delivers_for_realistic_agent(self, post):
        post.return_value = SimpleNamespace(status_code=200, text='')
        conv = SimpleNamespace(id=uuid4())
        msg = SimpleNamespace(id=uuid4(), sender_id=1, content='hello', client_message_id=None)
        ok = dispatch_hive_agent_message(conv, msg, self._agent_participant(REAL_STABLE_ID))
        assert ok is True
        assert post.called                      # NOT a silent no-op
        import json
        payload = json.loads(post.call_args.kwargs['data'])
        assert payload['stable_id'] == REAL_STABLE_ID
        assert payload['owner_id'] == '1'
        assert payload['organization_id'] == 1

    @override_settings(HIVE_CONNECT_HMAC_SECRET='wire-secret')
    @mock.patch('connect.services.hive_agents.time.time', return_value=1700000000)
    @mock.patch('connect.services.hive_agents.requests.post')
    def test_dispatch_sends_exact_signed_canonical_wire_bytes(self, post, _time):
        post.return_value = SimpleNamespace(status_code=200, text='', json=lambda: {})
        conv = SimpleNamespace(id=uuid4())
        msg = SimpleNamespace(id=uuid4(), sender_id=1, content='hello', client_message_id=None)

        assert dispatch_hive_agent_message(
            conv, msg, self._agent_participant(REAL_STABLE_ID),
        ) is True

        body = post.call_args.kwargs['data']
        assert body == __import__('json').dumps(
            __import__('json').loads(body), separators=(',', ':'), sort_keys=True,
        )
        expected = __import__('hmac').new(
            b'wire-secret', f'1700000000||{body}'.encode(), __import__('hashlib').sha256,
        ).hexdigest()
        assert post.call_args.kwargs['headers']['X-Hive-Timestamp'] == '1700000000'
        assert post.call_args.kwargs['headers']['X-Hive-Signature-256'] == expected

    @override_settings(HIVE_CONNECT_HMAC_SECRET='')
    @mock.patch('connect.services.hive_agents.requests.post')
    def test_missing_secret_fails_closed_with_durable_visible_error(self, post):
        conv = Conversation.objects.create(conversation_type='direct', created_by_id=1)
        msg = DirectMessage.objects.create(
            conversation=conv,
            sender_id=1,
            sender_email='owner@example.com',
            sender_name='Owner',
            content='hello',
        )
        assert dispatch_hive_agent_message(
            conv, msg, self._agent_participant(REAL_STABLE_ID),
        ) is False
        post.assert_not_called()
        system = DirectMessage.objects.get(conversation=conv, message_type='system')
        assert system.reply_to_id == msg.id
        assert 'Delivery failed' in system.content

    @mock.patch(
        'connect.services.hive_agents._authoritative_delivery_context',
        return_value=None,
    )
    @mock.patch('connect.services.hive_agents.requests.post')
    def test_missing_authority_fails_closed_with_durable_visible_error(
        self, post, _context,
    ):
        conv = Conversation.objects.create(conversation_type='direct', created_by_id=1)
        msg = DirectMessage.objects.create(
            conversation=conv,
            sender_id=1,
            sender_email='owner@example.com',
            sender_name='Owner',
            content='hello',
        )
        assert dispatch_hive_agent_message(
            conv, msg, self._agent_participant(REAL_STABLE_ID),
        ) is False
        post.assert_not_called()
        system = DirectMessage.objects.get(conversation=conv, message_type='system')
        assert system.reply_to_id == msg.id
        assert 'Delivery failed' in system.content

    @mock.patch('connect.services.hive_agents.requests.post')
    def test_dispatch_fails_loud_when_stable_id_missing(self, post):
        # A genuinely blank nickname must NOT silently drop — it must log loudly.
        p = SimpleNamespace(participant_type='agent', nickname='', user_id=2)
        conv = Conversation.objects.create(conversation_type='direct', created_by_id=1)
        msg = DirectMessage.objects.create(
            conversation=conv,
            sender_id=1,
            sender_email='owner@example.com',
            sender_name='Owner',
            content='hello',
        )
        with self.assertLogs('connect.services.hive_agents', level='ERROR') as cm:
            ok = dispatch_hive_agent_message(conv, msg, p)
        assert ok is False
        assert not post.called
        assert any('missing_stable_id' in line for line in cm.output)
        system = DirectMessage.objects.get(conversation=conv, message_type='system')
        assert system.reply_to_id == msg.id

    @mock.patch('connect.services.hive_agents.requests.post')
    def test_dispatch_failure_persists_user_visible_system_error(self, post):
        post.return_value = SimpleNamespace(
            status_code=409,
            text='runtime stopped',
            json=lambda: {'error': 'runtime stopped'},
        )
        conv = Conversation.objects.create(conversation_type='direct', created_by_id=1)
        msg = DirectMessage.objects.create(
            conversation=conv,
            sender_id=1,
            sender_email='owner@example.com',
            sender_name='Owner',
            content='hello stopped runtime',
        )
        participant = self._agent_participant(REAL_STABLE_ID)

        ok = dispatch_hive_agent_message(conv, msg, participant)

        assert ok is False
        system = DirectMessage.objects.get(conversation=conv, message_type='system')
        assert system.sender_id == 0
        assert system.reply_to_id == msg.id
        assert 'currently stopped' in system.content
        assert 'Start it in Hive' in system.content

        # Re-running the same failed dispatch must not create duplicate visible errors.
        again = dispatch_hive_agent_message(conv, msg, participant)
        assert again is False
        assert DirectMessage.objects.filter(conversation=conv, message_type='system').count() == 1

    @mock.patch('connect.services.hive_agents.requests.post')
    def test_dispatch_202_agent_not_running_persists_system_error(self, post):
        post.return_value = SimpleNamespace(
            status_code=202,
            text='{"status":"agent_not_running","claw_status":"stopped"}',
            json=lambda: {'status': 'agent_not_running', 'claw_status': 'stopped'},
        )
        conv = Conversation.objects.create(conversation_type='direct', created_by_id=1)
        msg = DirectMessage.objects.create(
            conversation=conv,
            sender_id=1,
            sender_email='owner@example.com',
            sender_name='Owner',
            content='hello stopped runtime',
        )

        ok = dispatch_hive_agent_message(conv, msg, self._agent_participant(REAL_STABLE_ID))

        assert ok is False
        system = DirectMessage.objects.get(conversation=conv, message_type='system')
        assert 'currently stopped' in system.content
        assert system.reply_to_id == msg.id

    @mock.patch('connect.services.hive_agents.requests.post')
    def test_accepted_retry_after_rate_exhaustion_adds_no_terminal(self, post):
        """Production order: accept, fill, reject new work, retry accepted tuple."""
        accepted = SimpleNamespace(
            status_code=202,
            text='{"status":"accepted"}',
            json=lambda: {'status': 'accepted'},
        )
        rate_limited = SimpleNamespace(
            status_code=429,
            text='{"status":"rate_limited"}',
            json=lambda: {'status': 'rate_limited'},
        )
        duplicate = SimpleNamespace(
            status_code=200,
            text='{"status":"duplicate"}',
            json=lambda: {'status': 'duplicate'},
        )
        post.side_effect = [accepted, accepted, accepted, rate_limited, duplicate]

        conversation = Conversation.objects.create(
            conversation_type='direct', created_by_id=1,
        )
        original = DirectMessage.objects.create(
            conversation=conversation,
            sender_id=1,
            sender_email='owner@example.com',
            sender_name='Owner',
            content='original',
        )
        fillers = [
            DirectMessage.objects.create(
                conversation=conversation,
                sender_id=1,
                sender_email='owner@example.com',
                sender_name='Owner',
                content=f'filler-{index}',
            )
            for index in range(3)
        ]
        participant = self._agent_participant(REAL_STABLE_ID)

        assert dispatch_hive_agent_message(
            conversation, original, participant,
        ) is True
        assert dispatch_hive_agent_message(
            conversation, fillers[0], participant,
        ) is True
        assert dispatch_hive_agent_message(
            conversation, fillers[1], participant,
        ) is True
        assert dispatch_hive_agent_message(
            conversation, fillers[2], participant,
        ) is False
        assert dispatch_hive_agent_message(
            conversation, original, participant,
        ) is True

        self.assertFalse(DirectMessage.objects.filter(
            conversation=conversation,
            message_type='system',
            reply_to=original,
        ).exists())
        self.assertEqual(DirectMessage.objects.filter(
            conversation=conversation,
            message_type='system',
            reply_to=fillers[2],
        ).count(), 1)


class HiveDeliveryBindingTests(TestCase):
    def setUp(self):
        self.owner_id = 101
        self.agent_user_id = hive_agent_user_id(REAL_STABLE_ID)
        self.conversation = Conversation.objects.create(
            conversation_type='direct', created_by_id=self.owner_id,
            direct_key=Conversation.build_direct_key(
                self.owner_id, self.agent_user_id,
            ),
        )
        ConversationParticipant.objects.create(
            conversation=self.conversation, user_id=self.owner_id,
            participant_type='human', user_email='owner@example.com',
        )
        self.agent_participant = ConversationParticipant.objects.create(
            conversation=self.conversation, user_id=self.agent_user_id,
            participant_type='agent', nickname=REAL_STABLE_ID,
        )
        self.message = DirectMessage.objects.create(
            conversation=self.conversation, sender_id=self.owner_id,
            sender_email='owner@example.com', sender_name='Owner', content='hello',
        )

    @mock.patch('connect.services.org_tenancy.get_user_personal_org_id', return_value=7)
    def test_binding_is_derived_from_persisted_participants_and_memberships(self, _orgs):
        from connect.services.hive_agents import _authoritative_delivery_context
        self.assertEqual(
            _authoritative_delivery_context(
                self.conversation, self.message, self.agent_participant,
            ),
            {
                'owner_id': str(self.owner_id),
                'organization_id': 7,
                'agent_participant_id': str(self.agent_user_id),
            },
        )

    @mock.patch('connect.services.org_tenancy.get_user_personal_org_id', return_value=7)
    def test_forged_nonparticipant_sender_is_rejected(self, _orgs):
        from connect.services.hive_agents import _authoritative_delivery_context
        self.message.sender_id = 999
        self.assertIsNone(_authoritative_delivery_context(
            self.conversation, self.message, self.agent_participant,
        ))

    @mock.patch('connect.services.org_tenancy.get_user_personal_org_id', return_value=7)
    def test_cross_owner_direct_key_is_rejected(self, _orgs):
        from connect.services.hive_agents import _authoritative_delivery_context
        self.conversation.direct_key = Conversation.build_direct_key(
            202, self.agent_user_id,
        )
        self.assertIsNone(_authoritative_delivery_context(
            self.conversation, self.message, self.agent_participant,
        ))

    @mock.patch('connect.services.org_tenancy.get_user_personal_org_id', return_value=7)
    def test_forged_agent_participant_id_is_rejected(self, _orgs):
        from connect.services.hive_agents import _authoritative_delivery_context
        self.agent_participant.user_id += 1
        self.assertIsNone(_authoritative_delivery_context(
            self.conversation, self.message, self.agent_participant,
        ))
