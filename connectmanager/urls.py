"""
URL configuration for Shizuha Connect.
"""

import os

from django.contrib import admin
from django.http import HttpResponse, JsonResponse
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView


def health_check(request):
    """Simple liveness check — process is up and serving HTTP."""
    return JsonResponse({'status': 'healthy'})


def _recycle_channel_layer_cache():
    """CON-107: drop the cached channel-layer backend so the next
    get_channel_layer() rebuilds fresh redis connection pools.

    A transient redis blip can leave channels_redis holding stale pooled
    connections; the cached RedisChannelLayer singleton then keeps failing the
    ws-health round-trip even after redis recovers -> /api/ws-health/ wedges at
    503 until a manual pod restart (prod incident 2026-06-28). Evicting the
    Channels layer-manager cache forces a brand-new backend (fresh pools) on the
    next probe, so readiness recovers on its own within a probe cycle. Safe /
    non-destructive: it builds a NEW layer talking to the same redis keys; it does
    NOT flush channel data. New backend instances share group/message state via
    redis, so no consistency loss."""
    try:
        from channels.layers import channel_layers
        backends = getattr(channel_layers, 'backends', None)
        if isinstance(backends, dict):
            backends.clear()
    except Exception:
        pass


async def ws_health_check(request):
    """CON-97: WS-aware READINESS check.

    The plain /api/health/ above stayed green for 7h while one daphne pod was
    wedged (hung Channels WS app-instances saturated it → WS handshakes 502'd),
    because it never exercises the ASGI/WS substrate. This runs IN the daphne
    event loop and does a real channel-layer (Redis) round-trip under a short
    timeout — the same substrate the WS consumers depend on. A wedged/saturated
    process or a dead channel layer fails the round-trip → 503 → k8s restarts the
    pod instead of black-holing WS traffic silently. Point the k8s readiness
    probe at THIS endpoint (not /api/health/)."""
    import asyncio
    import time
    from channels.layers import get_channel_layer

    timeout = float(os.environ.get('WS_HEALTH_TIMEOUT_SECONDS', '3'))
    layer = get_channel_layer()
    if layer is None:
        return JsonResponse({'status': 'unhealthy', 'reason': 'no_channel_layer'}, status=503)

    async def _roundtrip():
        # Use the channel layer's allocator so the channel name is valid for the
        # backend in use. channels_redis reserves process-specific names with
        # ``!`` for names that match its client prefix; a hand-rolled
        # ``healthz!<uuid>`` trips an AssertionError and makes every healthy pod
        # fail readiness.
        # Keep the prefix short: channels_redis appends its own process-specific
        # suffix (`.<client_prefix>!<uuid>`), and channel names must stay <100 chars.
        probe_channel = layer.new_channel('healthz')
        if asyncio.iscoroutine(probe_channel) or hasattr(probe_channel, '__await__'):
            probe_channel = await probe_channel
        # send-to-channel then receive-from-it: verifies the Redis channel layer
        # is reachable AND the event loop can complete an await round-trip.
        await layer.send(probe_channel, {'type': 'healthz.ping'})
        return await layer.receive(probe_channel)

    def _channel_failure_response(payload):
        # PLAT-923: a channel-layer timeout/error means the WS substrate is slow or
        # redis pools need recycling, but it should not by itself pull every Connect
        # pod out of Service endpoints and create a 502 storm. Keep the probe
        # event-loop-aware: if Daphne/the event loop is truly wedged, Kubernetes
        # still sees the HTTP probe timeout. For channel-layer failures that DO
        # return from the event loop, measure + recycle the pool and (by default)
        # soft-pass readiness as degraded. Set WS_HEALTH_CHANNEL_SOFT_FAIL=0 to
        # restore fail-closed 503 behaviour during controlled debugging.
        soft = os.environ.get('WS_HEALTH_CHANNEL_SOFT_FAIL', '1').lower() not in {'0', 'false', 'no'}
        body = {'status': 'degraded' if soft else 'unhealthy', **payload, 'readiness_soft_fail': soft}
        return JsonResponse(body, status=200 if soft else 503)

    try:
        t0 = time.monotonic()
        await asyncio.wait_for(_roundtrip(), timeout=timeout)
        return JsonResponse({
            'status': 'healthy',
            'channel_layer_ms': round((time.monotonic() - t0) * 1000),
        })
    except asyncio.TimeoutError:
        # CON-107: a timeout here is the stale-pool wedge symptom — recycle so the
        # next probe reconnects instead of failing forever.
        _recycle_channel_layer_cache()
        return _channel_failure_response(
            {'reason': 'channel_layer_roundtrip_timeout', 'timeout_s': timeout},
        )
    except Exception as exc:  # noqa: BLE001 — channel-layer errors are soft by default (PLAT-923)
        # CON-107: drop the (possibly stale) cached layer so the next probe rebuilds
        # fresh redis connections and self-heals without a pod restart.
        _recycle_channel_layer_cache()
        return _channel_failure_response(
            {'reason': 'channel_layer_error', 'error': exc.__class__.__name__},
        )


def metrics(request):
    """Prometheus scrape endpoint."""
    from connect.metrics import CONTENT_TYPE_LATEST, render_prometheus_metrics

    return HttpResponse(
        render_prometheus_metrics(),
        content_type=CONTENT_TYPE_LATEST,
    )


urlpatterns = [
    # Health check endpoint (for container healthchecks)
    path('api/health/', health_check, name='health-check'),
    # CON-97: WS-aware readiness — k8s readiness probe should target this.
    path('api/ws-health/', ws_health_check, name='ws-health-check'),
    path('metrics', metrics, name='prometheus-metrics'),

    # Admin
    path('admin/', admin.site.urls),

    # API Documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),

    # Connect API
    path('api/', include('connect.urls')),
]
