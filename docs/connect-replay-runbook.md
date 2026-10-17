# Connect bridge replay runbook (CON-165)

Use this when CON-8 delivery alerts show queued/retrying/dead-lettered outbound bridge work after an outage.

## Safety invariants

- Replay is **dry-run by default**; add `--execute` only after inspecting the candidate list.
- Replay reuses the existing `BridgeOutboundEvent` row and idempotency key. It never creates a second outbound envelope and never replays `accepted`, `delivered`, `failed`, `forbidden`, `rate_limited`, or `conflict` rows.
- Every execute attempt appends `actor`, `task_key`, `reason`, timestamp, action, and prior status to `audit_metadata.replay_history`.
- Duplicate-storm guard suppresses repeated identical outbound payloads for the same provider/account/channel/conversation/recipient/body hash above `CONNECT_BRIDGE_DUPLICATE_STORM_THRESHOLD` within `CONNECT_BRIDGE_DUPLICATE_STORM_WINDOW_SECONDS`.

## Dry run

```bash
python manage.py replay_bridge_outbound \
  --provider whatsapp \
  --channel-key ops-alerts \
  --since 2026-07-05T00:00:00Z \
  --batch-size 50 \
  --actor kai@shizuha.com \
  --task-key CON-165 \
  --reason "incident recovery after provider outage"
```

## Execute bounded replay

```bash
python manage.py replay_bridge_outbound \
  --provider whatsapp \
  --channel-key ops-alerts \
  --since 2026-07-05T00:00:00Z \
  --batch-size 50 \
  --actor kai@shizuha.com \
  --task-key CON-165 \
  --reason "incident recovery after provider outage" \
  --execute
```

## Observe

- `connect_bridge_replay_total{provider,channel,result}`: dry-run, accepted, failed, skipped-terminal counts.
- `connect_duplicate_storm_total{provider,channel,action}`: duplicate storm suppression counts.
- `connect_outbound_queue_depth` / `connect_outbound_oldest_pending_age_seconds`: remaining backlog.
- `connect_bridge_outbound_dead_lettered` / `connect_bridge_outbound_retrying`: invariant gauges for terminal/retrying rows.
