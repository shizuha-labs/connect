# Authoritative agent delivery identity

An ordinary conversation can contain a participant with only a display name.
The stored default `participant_type=human` and blank email are not sufficient
to classify a fleet-agent recipient. Previously that participant received human
notifications while the native Hive wake path was skipped.

Before notification channel classification, Connect now reconciles incomplete
agent delivery projections against Shizuha Identity. Only a matching canonical
user ID with `account_type=agent` and a valid email can update the existing
participant's type and email. The update is fenced by participant primary key,
user ID, active membership, and the observed projection values; unrelated fields
are not overwritten. Existing human/unknown rows are not promoted by display
names, client metadata, or email guessing. Hive personal-agent pseudo IDs and
complete agent projections retain their existing routing.

Confirmed, sanitized Identity rows are cached for five minutes. Unknown results,
errors and mismatched IDs are never negative-cached. The current Identity batch
endpoint may require per-ID fallback: each resolver receives at most eight IDs,
and at most four resolver calls run concurrently. Cache failures fall through to
Identity, while Identity failures preserve the existing delivery projection.
This does not change membership, notification policy, message acknowledgement,
agent lifecycle policy, or Hive's authenticated wake endpoint.

`connect.tests.test_participant_identity` exercises the real
`send_dm_notification` caller through agent-channel selection and native wake
HTTP, plus stale conversations, retry after outage, invalid authority rows,
cache failure, concurrent projection edits, pseudo IDs and bounded group lookup.
No manual participant repair or agent wake is required.

## Sender identity is also producer-owned

The ordinary conversation message and forwarding endpoints must persist
`sender_username` from the authenticated `request.user.username`. An email
local-part or display label is not necessarily the canonical Shizuha username.
Omitting this field caused the CLI's legacy fallback to address a reply to the
wrong username even after the recipient woke successfully. These endpoints now
write the authenticated username alongside the existing sender ID/email/name;
client-supplied sender fields cannot replace it. A forwarded message records
the current forwarder's identity, not the original sender's username.

Actual API regressions use differing username/email-local-part values and
forged client sender fields, then assert both response and persisted identity.
This preserves the native explicit `message_user` delivery contract; ordinary
LLM final text remains private and is not automatically relayed to Connect.
