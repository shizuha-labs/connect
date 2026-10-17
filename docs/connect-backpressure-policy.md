# Connect backpressure policy (CON-164)

Pulse: CON-164

Connect delivery paths must fail visibly and boundedly:

- **DB/persistence**: no acknowledged-but-lost messages. HTTP/WS send paths only report success after a durable `DirectMessage`/`BridgeOutboundEvent` row is committed. Persistence failures return an explicit error frame/HTTP failure and are retryable by client idempotency keys.
- **Provider outage/rate-limit**: outbound bridge sends enter `retrying` with a bounded `Retry-After`/jitter hint. After `CONNECT_OUTBOUND_MAX_ATTEMPTS`, the event becomes `dead_lettered` (terminal) with the last error in redacted audit metadata.
- **Provider/local queue overload**: before sending to a provider, Connect checks pending/retrying depth and oldest age for the provider/channel. If `CONNECT_OUTBOUND_MAX_PENDING` or `CONNECT_OUTBOUND_MAX_OLDEST_SECONDS` is exceeded, the event is accepted as `retrying` with queue diagnostics instead of growing an unbounded queue.
- **Websocket slow consumers**: per-client frame sends are bounded by `CONNECT_WS_SEND_TIMEOUT_SECONDS`. A slow socket is closed with code `1013`; reconnect replay is the recovery path, so the server does not accumulate an unbounded in-memory backlog.

Relevant terminal/visible states: `pending`, `retrying`, `accepted`, `delivered`, `failed`, `dead_lettered`, `rate_limited`, `forbidden`, `conflict`.
