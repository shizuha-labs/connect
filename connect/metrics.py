"""Prometheus metrics for Connect delivery paths."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import timedelta
from functools import lru_cache

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, REGISTRY, generate_latest
except ImportError:  # pragma: no cover - dependency is installed in production images
    CONTENT_TYPE_LATEST = 'text/plain; version=0.0.4; charset=utf-8'

    class _NoopMetric:
        def labels(self, **_kwargs):
            return self

        def inc(self, _amount=1):
            return None

        def observe(self, _amount):
            return None

    def Counter(*_args, **_kwargs):
        return _NoopMetric()

    def Histogram(*_args, **_kwargs):
        return _NoopMetric()

    REGISTRY = None

    def generate_latest(_registry=None):
        return b''


_SAFE_LABEL = re.compile(r'[^a-z0-9_:-]+')
logger = logging.getLogger(__name__)

_SECURITY_PIPELINE_REDIS_KEY = 'connect:metrics:security_pipeline'
_SECURITY_PIPELINE_DEFAULTS = (
    ('queued', 'platform_boundary_denial'),
    ('evaluated', 'anomaly'),
    ('evaluated', 'no_anomaly'),
    ('evaluated', 'repeat_suppressed'),
    ('sent', 'success'),
    ('failure', 'no_recipient'),
    ('failure', 'send_exception'),
    ('failure', 'enqueue_exception'),
    ('failure', 'dedupe_exception'),
    ('failure', 'ingest_auth_rejected'),
    ('failure', 'ingest_invalid_event'),
)


def _bounded_label(value, *, default='unknown', max_len=48):
    """Normalize dynamic inputs before they become Prometheus labels."""
    text = str(value or default).strip().lower().replace('-', '_')
    text = _SAFE_LABEL.sub('_', text).strip('_')
    return (text or default)[:max_len]


def provider_label(provider):
    provider = _bounded_label(provider)
    return provider if provider in {'api', 'connect', 'hive', 'whatsapp'} else 'other'


def channel_label(channel):
    raw = _bounded_label(channel)
    known = {
        'api', 'direct', 'websocket', 'chat', 'user_inbox', 'presence',
        'dashboard', 'agent', 'external', 'mobile', 'whatsapp', 'hive',
    }
    if raw in known:
        return raw
    if raw.startswith('chat_'):
        return 'chat'
    if raw.startswith('user_inbox_'):
        return 'user_inbox'
    if 'whatsapp' in raw:
        return 'whatsapp'
    # Bridge policy channel keys are operator/task configured and may be high
    # cardinality. Collapse unknown channel keys to one stable bucket.
    return 'bridge_policy' if raw != 'unknown' else 'unknown'


def error_class_label(error):
    if isinstance(error, type):
        value = error.__name__
    else:
        value = getattr(error, '__class__', type(error)).__name__ if not isinstance(error, str) else error
    value = re.sub(r'(?<!^)(?=[A-Z])', '_', str(value)).lower()
    return _bounded_label(value, default='error', max_len=40)


def _fanout_path_label(group):
    group = _bounded_label(group)
    if group.startswith('chat_'):
        return 'chat'
    if group.startswith('user_inbox_'):
        return 'user_inbox'
    if group == 'presence_global' or group.startswith('presence'):
        return 'presence'
    return channel_label(group)


def _prom_label(value):
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


@lru_cache(maxsize=4)
def _security_metrics_client(url):
    import redis
    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
    )


def _security_metrics_redis_url():
    from django.conf import settings
    return (
        getattr(settings, 'CONNECT_SECURITY_METRICS_REDIS_URL', '')
        or getattr(settings, 'CELERY_BROKER_URL', '')
    )


def _security_pipeline_field(stage, result):
    stage = _bounded_label(stage, default='unknown', max_len=24)
    result = _bounded_label(result, default='unknown', max_len=48)
    return f'{stage}|{result}'


def _record_durable_security_stage(stage, result, count=1):
    """Persist a worker/web security-pipeline stage in shared Redis.

    Celery workers and web pods do not share a Prometheus process registry. The
    Redis hash is therefore the cross-process source scraped by the backend;
    process-local counters below remain for backward-compatible diagnostics.
    """
    if not count:
        return True
    url = _security_metrics_redis_url()
    if not url:
        error = RuntimeError('CONNECT_SECURITY_METRICS_REDIS_URL/CELERY_BROKER_URL missing')
        logger.error('PLAT-4138 durable security metrics Redis URL is not configured')
        _record_security_metrics_write_failure(error)
        return False
    try:
        _security_metrics_client(url).hincrby(
            _SECURITY_PIPELINE_REDIS_KEY,
            _security_pipeline_field(stage, result),
            int(count),
        )
        return True
    except Exception as exc:
        logger.exception(
            'PLAT-4138 failed to persist Connect security pipeline stage=%s result=%s',
            stage, result,
        )
        _record_security_metrics_write_failure(exc)
        return False


def _record_security_metrics_write_failure(error):
    """Record Redis writer failure through independent Postgres storage."""
    try:
        from django.db.models import F
        from django.utils import timezone

        from connect.models import SecurityPipelineMetricHealth

        row, _ = SecurityPipelineMetricHealth.objects.get_or_create(channel='redis')
        SecurityPipelineMetricHealth.objects.filter(pk=row.pk).update(
            failure_count=F('failure_count') + 1,
            last_failure_at=timezone.now(),
            last_error_class=error_class_label(error),
        )
        return True
    except Exception:
        # The independent channel itself failed. Do not break message delivery,
        # but leave a CRITICAL log for the platform log/DB health alerts.
        logger.critical(
            'PLAT-4138 failed to record Redis metric-write failure in Postgres',
            exc_info=True,
        )
        return False


def _security_metrics_write_health():
    """Return (total failures, recent failure gauge, independent probe error)."""
    try:
        from django.conf import settings
        from django.utils import timezone

        from connect.models import SecurityPipelineMetricHealth

        row = SecurityPipelineMetricHealth.objects.filter(channel='redis').first()
        if row is None:
            return 0, 0, 0
        window_seconds = max(
            60,
            int(getattr(
                settings,
                'CONNECT_SECURITY_METRICS_WRITE_FAILURE_RECENT_SECONDS',
                600,
            )),
        )
        cutoff = timezone.now() - timedelta(seconds=window_seconds)
        recent = int(bool(row.last_failure_at and row.last_failure_at >= cutoff))
        return max(0, int(row.failure_count)), recent, 0
    except Exception:
        logger.exception('PLAT-4138 failed to collect independent worker-write health')
        return None, None, 1


def render_security_pipeline_metrics():
    """Render Redis-backed SIEM-lite stages from workers through web `/metrics`."""
    values = None
    probe_errors = 0
    try:
        url = _security_metrics_redis_url()
        if not url:
            raise RuntimeError('CONNECT_SECURITY_METRICS_REDIS_URL/CELERY_BROKER_URL missing')
        raw = _security_metrics_client(url).hgetall(_SECURITY_PIPELINE_REDIS_KEY)
        parsed_values = {
            _security_pipeline_field(stage, result): 0
            for stage, result in _SECURITY_PIPELINE_DEFAULTS
        }
        read_valid = True
        # Hard bound protects the scrape even if an older build wrote unexpected
        # fields. Recorders normalize labels; renderer validates again.
        for field, count in list((raw or {}).items())[:256]:
            if '|' not in str(field):
                continue
            stage, result = str(field).split('|', 1)
            normalized = _security_pipeline_field(stage, result)
            try:
                parsed_values[normalized] = max(0, int(count or 0))
            except (TypeError, ValueError):
                probe_errors = 1
                read_valid = False
        if read_valid:
            values = parsed_values
    except Exception:
        probe_errors = 1
        logger.exception('PLAT-4138 failed to collect durable Connect security pipeline metrics')

    write_failures, write_failure_recent, write_health_probe_error = (
        _security_metrics_write_health()
    )
    probe_errors = max(
        probe_errors,
        write_failure_recent or 0,
        write_health_probe_error,
    )

    lines = []
    if values is not None:
        lines.extend([
            '# HELP connect_security_pipeline_stage_total Durable cross-process SIEM-lite worker/web pipeline stages.',
            '# TYPE connect_security_pipeline_stage_total counter',
        ])
        for field, count in sorted(values.items()):
            stage, result = field.split('|', 1)
            lines.append(
                'connect_security_pipeline_stage_total'
                f'{{stage="{_prom_label(stage)}",result="{_prom_label(result)}"}} {count}'
            )
    if write_failures is not None:
        lines.extend([
            '# HELP connect_security_pipeline_worker_write_failures_total Worker Redis metric writes that failed and were recorded independently in Postgres.',
            '# TYPE connect_security_pipeline_worker_write_failures_total counter',
            f'connect_security_pipeline_worker_write_failures_total{{service="connect"}} {write_failures}',
            '# HELP connect_security_pipeline_worker_write_failure_recent A worker Redis metric write failed inside the configured alert window.',
            '# TYPE connect_security_pipeline_worker_write_failure_recent gauge',
            f'connect_security_pipeline_worker_write_failure_recent{{service="connect"}} {write_failure_recent}',
        ])
    lines.extend([
        '# HELP connect_security_pipeline_probe_errors Durable SIEM-lite metric backend collection failures.',
        '# TYPE connect_security_pipeline_probe_errors gauge',
        f'connect_security_pipeline_probe_errors{{service="connect"}} {probe_errors}',
    ])
    return '\n'.join(lines) + '\n'


def render_security_outbox_metrics():
    """Render CON-217 accepted→terminal truth from Postgres.

    On a database probe failure, truth-bearing families are omitted rather than
    exported as misleading zeroes; only the probe-error metric is emitted.
    """
    try:
        from django.db.models import Count, Sum
        from django.utils import timezone

        from connect.models import SecurityEventOutbox, SecurityEventRelayHealth

        now = timezone.now()
        rows = SecurityEventOutbox.objects.all()
        pending = rows.filter(terminal_at__isnull=True)
        oldest = pending.order_by('accepted_at').values_list('accepted_at', flat=True).first()
        attempts = rows.aggregate(
            publish=Sum('publish_attempts'),
            delivery=Sum('delivery_attempts'),
            conflicts=Sum('conflict_count'),
        )
        terminal = {
            status: count
            for status, count in rows.filter(terminal_at__isnull=False)
            .values_list('status')
            .annotate(count=Count('id'))
        }
        health = SecurityEventRelayHealth.objects.filter(channel='security_outbox').first()

        lines = [
            '# HELP connect_security_outbox_accepted_total Security events durably accepted in Postgres.',
            '# TYPE connect_security_outbox_accepted_total gauge',
            f'connect_security_outbox_accepted_total{{service="connect"}} {rows.count()}',
            '# HELP connect_security_outbox_pending Durable security events without a terminal outcome.',
            '# TYPE connect_security_outbox_pending gauge',
            f'connect_security_outbox_pending{{service="connect"}} {pending.count()}',
            '# HELP connect_security_outbox_oldest_pending_age_seconds Age of the oldest non-terminal event.',
            '# TYPE connect_security_outbox_oldest_pending_age_seconds gauge',
            'connect_security_outbox_oldest_pending_age_seconds{service="connect"} '
            f'{max(0.0, (now - oldest).total_seconds()) if oldest else 0}',
            '# HELP connect_security_outbox_attempts_total Postgres-recorded publish and delivery attempts.',
            '# TYPE connect_security_outbox_attempts_total gauge',
            f'connect_security_outbox_attempts_total{{stage="publish"}} {int(attempts["publish"] or 0)}',
            f'connect_security_outbox_attempts_total{{stage="delivery"}} {int(attempts["delivery"] or 0)}',
            '# HELP connect_security_outbox_conflicts_total Producer identities reused with a different canonical payload.',
            '# TYPE connect_security_outbox_conflicts_total gauge',
            f'connect_security_outbox_conflicts_total{{service="connect"}} {int(attempts["conflicts"] or 0)}',
            '# HELP connect_security_outbox_terminal_total Durable terminal outcomes by bounded status.',
            '# TYPE connect_security_outbox_terminal_total gauge',
        ]
        for status, _label in SecurityEventOutbox.Status.choices:
            if status in SecurityEventOutbox.TERMINAL_STATUSES:
                lines.append(
                    'connect_security_outbox_terminal_total'
                    f'{{status="{_prom_label(status)}"}} {int(terminal.get(status, 0))}'
                )
        lines.extend([
            '# HELP connect_security_outbox_relay_last_run_timestamp_seconds Last recovery relay execution observed in Postgres.',
            '# TYPE connect_security_outbox_relay_last_run_timestamp_seconds gauge',
            'connect_security_outbox_relay_last_run_timestamp_seconds{service="connect"} '
            f'{health.last_run_at.timestamp() if health and health.last_run_at else 0}',
            '# HELP connect_security_outbox_relay_failures_total Recovery relay failures recorded in Postgres.',
            '# TYPE connect_security_outbox_relay_failures_total gauge',
            'connect_security_outbox_relay_failures_total{service="connect"} '
            f'{int(health.failure_count) if health else 0}',
            '# HELP connect_security_outbox_probe_errors Postgres outbox metric collection failures.',
            '# TYPE connect_security_outbox_probe_errors gauge',
            'connect_security_outbox_probe_errors{service="connect"} 0',
        ])
        return '\n'.join(lines) + '\n'
    except Exception:
        logger.exception('CON-217 failed to collect Postgres security outbox metrics')
        return '\n'.join([
            '# HELP connect_security_outbox_probe_errors Postgres outbox metric collection failures.',
            '# TYPE connect_security_outbox_probe_errors gauge',
            'connect_security_outbox_probe_errors{service="connect"} 1',
            '',
        ])


DM_REPLAY_FANOUT_TOTAL = Counter(
    'connect_dm_replay_fanout_total',
    'Messages replayed by UserChatConsumer reconnect missed-DM fanout.',
    ['user_role'],
)

DM_REPLAY_PER_RECONNECT_COUNT = Histogram(
    'connect_dm_replay_per_reconnect_count',
    'Messages replayed by one UserChatConsumer reconnect or explicit cursor-sync flush.',
    buckets=(0, 1, 2, 5, 10, 25, 50, 100, 200, 500),
)

DM_REPLAY_CAPPED_TOTAL = Counter(
    'connect_dm_replay_capped_total',
    'Reconnect replay queries that hit the candidate cap.',
    # CON-231: bounded labels only. `user_role` is human|agent; `result` is one
    # of the three cap exits PLUS the 'other' fold below, so cardinality is
    # fixed at 2 x 4 = 8 series (@reika P3 -- the fold is a fourth value, not a
    # discarded one). User and conversation attribution belongs in the
    # structured log, never here.
    ['user_role', 'result'],
)

DM_LIVE_DELIVERY_TOTAL = Counter(
    'connect_dm_live_delivery_total',
    'Live UserChatConsumer MessageDelivery rows written after new_message fanout.',
    ['user_role'],
)

CONNECT_INBOUND_MESSAGES_TOTAL = Counter(
    'connect_inbound_messages_total',
    'Inbound provider/web/API messages observed by Connect, partitioned by bounded provider/channel/result labels.',
    ['provider', 'channel', 'result'],
)

CONNECT_MESSAGE_PERSISTENCE_FAILURES_TOTAL = Counter(
    'connect_message_persistence_failures_total',
    'DirectMessage/bridge message persistence failures by bounded provider/channel/error class.',
    ['provider', 'channel', 'error_class'],
)

CONNECT_WS_FANOUT_TOTAL = Counter(
    'connect_ws_fanout_total',
    'WebSocket/channel-layer fanout attempts by bounded path and result.',
    ['path', 'result'],
)

CONNECT_WS_FANOUT_LAG_SECONDS = Histogram(
    'connect_ws_fanout_lag_seconds',
    'Synchronous channel-layer fanout call duration before connected clients can receive the frame.',
    ['path', 'result'],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)

CONNECT_PROVIDER_EVENTS_TOTAL = Counter(
    'connect_provider_events_total',
    'Provider/channel bridge lifecycle events (auth failures, send failures, reconnect/disconnect/status callbacks).',
    ['provider', 'channel', 'event', 'result'],
)

CONNECT_SECURITY_EVENTS_QUEUED_TOTAL = Counter(
    'connect_security_events_queued_total',
    'Connect runtime security events queued for SIEM-lite worker evaluation.',
    ['event_type'],
)

CONNECT_SECURITY_EVENTS_EVALUATED_TOTAL = Counter(
    'connect_security_events_evaluated_total',
    'Connect runtime security events evaluated by the SIEM-lite worker.',
    ['outcome'],
)

CONNECT_SECURITY_ALERTS_SENT_TOTAL = Counter(
    'connect_security_alerts_sent_total',
    'Connect runtime security anomaly alerts sent to the configured security lead.',
)

CONNECT_SECURITY_ALERT_FAILURES_TOTAL = Counter(
    'connect_security_alert_failures_total',
    'Connect runtime security anomaly alert failures or misconfiguration skips.',
    ['reason'],
)

CONNECT_BRIDGE_REPLAY_TOTAL = Counter(
    'connect_bridge_replay_total',
    'Bounded CON-165 outbound bridge replay attempts by provider/channel/result.',
    ['provider', 'channel', 'result'],
)

CONNECT_DUPLICATE_STORM_TOTAL = Counter(
    'connect_duplicate_storm_total',
    'CON-165 duplicate-storm guard observations/suppressions by provider/channel/action.',
    ['provider', 'channel', 'action'],
)


def _initialize_metric_series():
    """Create zero-valued series for absent() liveness alerts before traffic arrives."""
    for labels in (
        {'provider': 'api', 'channel': 'direct', 'result': 'persisted'},
        {'provider': 'api', 'channel': 'hive', 'result': 'persisted'},
        {'provider': 'whatsapp', 'channel': 'whatsapp', 'result': 'routed'},
    ):
        CONNECT_INBOUND_MESSAGES_TOTAL.labels(**labels)
    for labels in (
        {'provider': 'api', 'channel': 'direct', 'error_class': 'integrity_error'},
        {'provider': 'whatsapp', 'channel': 'whatsapp', 'error_class': 'database_error'},
    ):
        CONNECT_MESSAGE_PERSISTENCE_FAILURES_TOTAL.labels(**labels)
    for labels in (
        {'path': 'chat', 'result': 'success'},
        {'path': 'chat', 'result': 'failure'},
        {'path': 'user_inbox', 'result': 'success'},
    ):
        CONNECT_WS_FANOUT_TOTAL.labels(**labels)
        CONNECT_WS_FANOUT_LAG_SECONDS.labels(**labels)
    for labels in (
        {'provider': 'whatsapp', 'channel': 'whatsapp', 'event': 'auth_failure', 'result': 'failure'},
        {'provider': 'whatsapp', 'channel': 'bridge_policy', 'event': 'provider_send', 'result': 'accepted'},
        {'provider': 'whatsapp', 'channel': 'bridge_policy', 'event': 'provider_send', 'result': 'failure'},
    ):
        CONNECT_PROVIDER_EVENTS_TOTAL.labels(**labels)
    for labels in (
        {'provider': 'whatsapp', 'channel': 'bridge_policy', 'result': 'dry_run'},
        {'provider': 'whatsapp', 'channel': 'bridge_policy', 'result': 'accepted'},
        {'provider': 'whatsapp', 'channel': 'bridge_policy', 'result': 'skipped_terminal'},
    ):
        CONNECT_BRIDGE_REPLAY_TOTAL.labels(**labels)
    for labels in (
        {'provider': 'whatsapp', 'channel': 'bridge_policy', 'action': 'suppressed'},
    ):
        CONNECT_DUPLICATE_STORM_TOTAL.labels(**labels)


_initialize_metric_series()

def user_role_label(is_agent):
    """Normalize the user role label used by Connect DM delivery metrics."""
    return 'agent' if is_agent else 'human'


def record_dm_replay_fanout(user_role, count=1):
    if count:
        DM_REPLAY_FANOUT_TOTAL.labels(user_role=user_role).inc(count)


def observe_dm_replay_per_reconnect(count):
    DM_REPLAY_PER_RECONNECT_COUNT.observe(count)


DM_REPLAY_CAP_RESULTS = ('no_candidates', 'all_deduped', 'delivered')


def record_dm_replay_capped(user_role, result, count=1):
    """CON-231: a cap hit is only actionable with the role and which exit it took.

    `result` must come from DM_REPLAY_CAP_RESULTS; an unknown value is folded to
    'other' rather than minting an unbounded series.
    """
    if not count:
        return
    safe_result = result if result in DM_REPLAY_CAP_RESULTS else 'other'
    DM_REPLAY_CAPPED_TOTAL.labels(user_role=user_role, result=safe_result).inc(count)


def record_dm_live_delivery(user_role, count=1):
    if count:
        DM_LIVE_DELIVERY_TOTAL.labels(user_role=user_role).inc(count)


def record_inbound_message(provider, channel, result, count=1):
    if count:
        CONNECT_INBOUND_MESSAGES_TOTAL.labels(
            provider=provider_label(provider),
            channel=channel_label(channel),
            result=_bounded_label(result),
        ).inc(count)


def record_message_persistence_failure(provider, channel, error, count=1):
    if count:
        CONNECT_MESSAGE_PERSISTENCE_FAILURES_TOTAL.labels(
            provider=provider_label(provider),
            channel=channel_label(channel),
            error_class=error_class_label(error),
        ).inc(count)


def record_ws_fanout(group, result, duration_seconds=None, count=1):
    if not count:
        return
    path = _fanout_path_label(group)
    result = _bounded_label(result)
    CONNECT_WS_FANOUT_TOTAL.labels(path=path, result=result).inc(count)
    if duration_seconds is not None:
        CONNECT_WS_FANOUT_LAG_SECONDS.labels(path=path, result=result).observe(max(0.0, float(duration_seconds)))


def record_provider_event(provider, channel, event, result='observed', count=1):
    if count:
        CONNECT_PROVIDER_EVENTS_TOTAL.labels(
            provider=provider_label(provider),
            channel=channel_label(channel),
            event=_bounded_label(event, max_len=40),
            result=_bounded_label(result, max_len=40),
        ).inc(count)


def record_connect_security_event_queued(event_type, count=1):
    if count:
        _record_durable_security_stage('queued', event_type or 'unknown', count)
        CONNECT_SECURITY_EVENTS_QUEUED_TOTAL.labels(event_type=event_type or 'unknown').inc(count)


def record_connect_security_event_evaluated(outcome, count=1):
    if count:
        _record_durable_security_stage('evaluated', outcome or 'unknown', count)
        CONNECT_SECURITY_EVENTS_EVALUATED_TOTAL.labels(outcome=outcome or 'unknown').inc(count)


def record_connect_security_alert_sent(count=1):
    if count:
        _record_durable_security_stage('sent', 'success', count)
        CONNECT_SECURITY_ALERTS_SENT_TOTAL.inc(count)


def record_connect_security_alert_failure(reason, count=1):
    if count:
        _record_durable_security_stage('failure', reason or 'unknown', count)
        CONNECT_SECURITY_ALERT_FAILURES_TOTAL.labels(reason=reason or 'unknown').inc(count)


def record_bridge_replay(provider, channel, result, count=1):
    if count:
        CONNECT_BRIDGE_REPLAY_TOTAL.labels(
            provider=provider_label(provider),
            channel=channel_label(channel),
            result=_bounded_label(result, max_len=40),
        ).inc(count)


def record_duplicate_storm(provider, channel, action, count=1):
    if count:
        CONNECT_DUPLICATE_STORM_TOTAL.labels(
            provider=provider_label(provider),
            channel=channel_label(channel),
            action=_bounded_label(action, max_len=40),
        ).inc(count)


def render_outbound_queue_metrics(now=None):
    """Render bounded provider/channel outbound backlog gauges at scrape time."""
    try:
        from django.utils import timezone
        from connect.models.messaging import BridgeOutboundEvent

        now = now or timezone.now()
        pending = BridgeOutboundEvent.objects.filter(status=BridgeOutboundEvent.Status.PENDING)
        by_key = defaultdict(lambda: {'depth': 0, 'oldest': 0})
        for provider, channel, created_at in pending.values_list('provider', 'channel_key', 'created_at'):
            key = (provider_label(provider), channel_label(channel))
            age = int((now - created_at).total_seconds()) if created_at else 0
            by_key[key]['depth'] += 1
            by_key[key]['oldest'] = max(by_key[key]['oldest'], age)
        lines = [
            '# HELP connect_outbound_queue_depth Pending outbound provider sends grouped by bounded provider/channel labels.',
            '# TYPE connect_outbound_queue_depth gauge',
            '# HELP connect_outbound_oldest_pending_age_seconds Oldest pending outbound provider send age by bounded provider/channel labels.',
            '# TYPE connect_outbound_oldest_pending_age_seconds gauge',
        ]
        if not by_key:
            lines.extend([
                'connect_outbound_queue_depth{provider="none",channel="none"} 0',
                'connect_outbound_oldest_pending_age_seconds{provider="none",channel="none"} 0',
            ])
        else:
            for (provider, channel), values in sorted(by_key.items()):
                labels = f'provider="{_prom_label(provider)}",channel="{_prom_label(channel)}"'
                lines.append(f'connect_outbound_queue_depth{{{labels}}} {values["depth"]}')
                lines.append(f'connect_outbound_oldest_pending_age_seconds{{{labels}}} {values["oldest"]}')
        return '\n'.join(lines) + '\n'
    except Exception:
        return (
            '# HELP connect_outbound_queue_probe_errors Outbound queue metric collection failures (fail-loud).\n'
            '# TYPE connect_outbound_queue_probe_errors gauge\n'
            'connect_outbound_queue_probe_errors 1\n'
        )


def render_prometheus_metrics():
    """Render the default Prometheus registry plus invariant self-checks."""
    body = generate_latest(REGISTRY)
    body += render_security_pipeline_metrics().encode()
    body += render_security_outbox_metrics().encode()
    body += render_outbound_queue_metrics().encode()
    # PLAT-1278: invariant gauges are computed at scrape time so every scrape
    # reflects live state; a collection failure must not take down the
    # delivery-path metrics above (it is surfaced via the probe-errors gauge
    # inside render_invariant_metrics, and by the absent() alert rules if this
    # block itself cannot render).
    try:
        from connect.invariants import collect_connect_invariant_metrics, render_invariant_metrics

        body += render_invariant_metrics(collect_connect_invariant_metrics()).encode()
    except Exception:
        body += b'# HELP connect_invariant_probe_errors Invariant collection sections that failed this scrape (fail-loud).\n' \
                b'# TYPE connect_invariant_probe_errors gauge\nconnect_invariant_probe_errors 99\n'
    return body
