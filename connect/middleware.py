import logging
import os

from django.http import JsonResponse

from connect.authentication import ShizuhaJWTAuthentication

logger = logging.getLogger(__name__)

# PLAT-12776: env-guarded credential-layer diagnostics. Default OFF — zero
# behavior change when disabled; when enabled (SHIZUHA_SERVICE_ACCESS_DIAG=1),
# logs the token-validation outcome, the enabled_services claim, and the
# DENY/ALLOW verdict so the 403 origin (middleware vs DRF permission) is
# named from logs alone.
_DIAG = os.environ.get('SHIZUHA_SERVICE_ACCESS_DIAG') == '1'


# Match both ingress mounts: /api/internal/... and /shizuha-connect/api/internal/...
_INTERNAL_MARKER = '/api/internal/'


def request_via_ingress(request) -> bool:
    """True when the request was proxied by the public nginx ingress.

    nginx-ingress always appends X-Forwarded-For / X-Real-IP to proxied
    requests and an external client cannot make it omit them; direct
    in-cluster Service calls (the only legitimate callers of the internal
    service-to-service API) carry neither.
    """
    meta = request.META
    return bool(meta.get('HTTP_X_FORWARDED_FOR') or meta.get('HTTP_X_REAL_IP'))


class InternalApiIngressGuardMiddleware:
    """404 any ingress-borne request to the internal service-to-service API (CON-208).

    Connect's /api/internal/ endpoints were guarded only by the spoofable
    X-Internal-Service header while the ingress regex (api|admin|static)(/|$)(.*)
    also exposed them publicly — any external client could set
    `X-Internal-Service: shizuha-pulse` and inject system messages, etc.
    This mirrors shizuha-id's internal_api_guard.py (the correct mitigation for
    the same ID-52 class): block internal paths that arrived through the ingress.
    An in-cluster caller that spoofs XFF simply locks itself out — fail closed.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _INTERNAL_MARKER in request.path and request_via_ingress(request):
            # 404 (not 403) — don't advertise that the path exists publicly.
            return JsonResponse({'error': 'Not found'}, status=404)
        return self.get_response(request)


class ServiceAccessMiddleware:
    SERVICE_SLUG = 'connect'

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith('/api/'):
            return self.get_response(request)
        if '/internal/' in request.path:
            return self.get_response(request)

        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
            try:
                validated_token = ShizuhaJWTAuthentication().get_validated_token(token)
                if _DIAG:
                    logger.error(
                        'PLAT-12776 diag token validated: claims keys=%s enabled_services=%r staff=%r path=%s',
                        sorted(validated_token.keys()),
                        validated_token.get('enabled_services'),
                        validated_token.get('is_staff'),
                        request.path,
                    )
                if validated_token.get('is_staff') or validated_token.get('is_superuser'):
                    return self.get_response(request)
                enabled_services = validated_token.get('enabled_services')
                if enabled_services is not None and self.SERVICE_SLUG not in enabled_services:
                    if _DIAG:
                        logger.error(
                            'PLAT-12776 diag DENY by middleware: slug=%r not in %r path=%s',
                            self.SERVICE_SLUG, enabled_services, request.path,
                        )
                    return JsonResponse(
                        {'error': 'You do not have access to this service.',
                         'service': self.SERVICE_SLUG,
                         'code': 'service_access_denied'},
                        status=403
                    )
                if _DIAG:
                    logger.error('PLAT-12776 diag middleware ALLOW: path=%s', request.path)
            except Exception as exc:
                if _DIAG:
                    logger.error('PLAT-12776 diag token validation raised: %r path=%s', exc, request.path)
                pass  # Let DRF authentication handle missing/invalid/expired tokens

        return self.get_response(request)
