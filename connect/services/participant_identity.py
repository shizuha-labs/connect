"""Repair incomplete agent delivery projections from authoritative Identity rows."""

import logging
from concurrent.futures import ThreadPoolExecutor

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from connect.models import ConversationParticipant
from connect.services.display_names import resolve_identity_users
from connect.services.hive_agents import is_hive_agent_user_id

logger = logging.getLogger(__name__)


def _identity_row(row, user_id):
    if not isinstance(row, dict):
        return None
    raw_id = row.get('id', row.get('user_id'))
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
        return None
    try:
        if int(raw_id) != user_id:
            return None
    except ValueError:
        return None
    account_type = row.get('account_type')
    email = row.get('email')
    if account_type not in ('human', 'agent') or not isinstance(email, str):
        return None
    email = email.strip()
    if not email or len(email) > 254:
        return None
    try:
        validate_email(email)
    except ValidationError:
        return None
    return {'id': user_id, 'account_type': account_type, 'email': email}


def _lookup_chunk(user_ids):
    try:
        rows = resolve_identity_users(user_ids)
        return {
            user_id: row for user_id in user_ids
            if (row := _identity_row(rows.get(user_id), user_id)) is not None
        }
    except Exception:
        logger.warning('Participant Identity lookup unavailable', exc_info=True)
        return {}


def project_agent_delivery_identity(participants):
    candidates = [
        participant for participant in participants
        if type(participant.user_id) is int and participant.user_id > 0
        and participant.participant_type != 'system'
        and not is_hive_agent_user_id(participant.user_id)
        and not participant.has_left
        and (participant.participant_type != 'agent' or not participant.user_email.strip())
    ]
    user_ids = list(dict.fromkeys(participant.user_id for participant in candidates))
    if not user_ids:
        return
    keys = {user_id: f'connect:participant-identity:v1:{user_id}' for user_id in user_ids}
    try:
        cached = cache.get_many(keys.values())
    except Exception:
        logger.warning('Participant Identity cache read unavailable', exc_info=True)
        cached = {}
    identities = {
        user_id: row for user_id, key in keys.items()
        if (row := _identity_row(cached.get(key), user_id)) is not None
    }
    missing = [user_id for user_id in user_ids if user_id not in identities]
    chunks = [missing[offset:offset + 8] for offset in range(0, len(missing), 8)]
    resolved = {}
    if len(chunks) == 1:
        resolved.update(_lookup_chunk(chunks[0]))
    elif chunks:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='participant-identity') as executor:
            for rows in executor.map(_lookup_chunk, chunks):
                resolved.update(rows)
    if resolved:
        identities.update(resolved)
        try:
            cache.set_many({keys[user_id]: row for user_id, row in resolved.items()}, timeout=300)
        except Exception:
            logger.warning('Participant Identity cache write unavailable', exc_info=True)
    for participant in candidates:
        row = identities.get(participant.user_id)
        if row is None or row['account_type'] != 'agent':
            continue
        updated = ConversationParticipant.objects.filter(
            pk=participant.pk, user_id=participant.user_id, has_left=False,
            participant_type=participant.participant_type, user_email=participant.user_email,
        ).update(participant_type='agent', user_email=row['email'])
        if updated:
            participant.participant_type = 'agent'
            participant.user_email = row['email']
