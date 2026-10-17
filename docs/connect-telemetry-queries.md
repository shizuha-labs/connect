# Connect telemetry query set (CON-163)

Pulse: CON-163

Use these with the `/metrics` endpoint and the `connect-dm-delivery` Prometheus rules to see bridge health, backlog, provider errors, and websocket fanout lag in one place.

## Golden signals

```promql
sum by (provider, channel, result) (rate(connect_inbound_messages_total[5m]))
sum by (provider, channel, error_class) (rate(connect_message_persistence_failures_total[5m]))
histogram_quantile(0.95, sum by (le, path) (rate(connect_ws_fanout_lag_seconds_bucket[5m])))
sum by (provider, channel) (connect_outbound_queue_depth)
max by (provider, channel) (connect_outbound_oldest_pending_age_seconds)
sum by (provider, channel, event, result) (rate(connect_provider_events_total[5m]))
```

## Fail-loud liveness checks

```promql
absent(connect_inbound_messages_total)
absent(connect_ws_fanout_lag_seconds_bucket)
absent(connect_outbound_queue_depth)
absent(connect_provider_events_total)
connect_outbound_queue_probe_errors > 0
connect_invariant_probe_errors > 0
```

Labels are deliberately bounded: dynamic conversation ids collapse to `chat`, inbox ids to `user_inbox`, unknown bridge channel keys to `bridge_policy`, and unsupported providers to `other`.
