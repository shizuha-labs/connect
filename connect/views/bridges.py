"""External provider bridge webhook endpoints."""

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from connect.services.bridge import (
    BridgeOutboundEnvelope,
    BridgeConflict,
    BridgeError,
    InvalidSignature,
    MissingWebhookSecret,
    process_inbound_webhook,
    record_outbound_delivery_status,
    send_outbound_bridge_message,
    verify_request_or_raise,
)


class WhatsAppWebhookView(APIView):
    """WhatsApp inbound webhook with verify-before-store signature enforcement."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, provider_account_id: str):
        raw_body = request.body or b""
        try:
            result = process_inbound_webhook("whatsapp", provider_account_id, raw_body, request.META)
        except MissingWebhookSecret as exc:
            return Response({"error": str(exc)}, status=exc.status_code)
        except InvalidSignature as exc:
            return Response({"error": str(exc)}, status=exc.status_code)
        except BridgeConflict as exc:
            return Response({"error": str(exc)}, status=exc.status_code)
        except BridgeError as exc:
            return Response({"error": str(exc)}, status=exc.status_code)
        if result.conflict:
            return Response(
                {
                    "error": "provider event id replayed with different payload",
                    "bridge_event_id": str(result.event.bridge_event_id),
                    "processing_status": result.event.processing_status,
                },
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            {
                "bridge_event_id": str(result.event.bridge_event_id),
                "provider_event_id": result.event.provider_event_id,
                "mapped_user_id": result.event.mapped_user_id,
                "message_id": str(result.event.message_id) if result.event.message_id else None,
                "processing_status": result.event.processing_status,
                "idempotent_replay": result.idempotent_replay,
            },
            status=status.HTTP_200_OK if result.idempotent_replay else status.HTTP_201_CREATED,
        )


class OutboundBridgeSendView(APIView):
    """Authenticated outbound bridge send with policy, idempotency, and audit gates."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            envelope = BridgeOutboundEnvelope(
                provider=(request.data.get('provider') or 'whatsapp').strip(),
                provider_account_id=(request.data.get('provider_account_id') or '').strip(),
                channel_key=(request.data.get('channel_key') or '').strip(),
                task_key=(request.data.get('task_key') or '').strip(),
                agent_id=getattr(request.user, 'id', 0),
                recipient_external_id=(request.data.get('recipient_external_id') or '').strip(),
                body=request.data.get('body') or '',
                idempotency_key=(request.data.get('idempotency_key') or '').strip(),
                conversation_id=request.data.get('conversation_id') or None,
            )
            result = send_outbound_bridge_message(envelope)
        except BridgeError as exc:
            return Response({'error': str(exc)}, status=exc.status_code)

        http_status = status.HTTP_200_OK if result.idempotent_replay else status.HTTP_201_CREATED
        if result.event.status == 'forbidden':
            http_status = status.HTTP_403_FORBIDDEN
        elif result.event.status == 'rate_limited':
            http_status = status.HTTP_429_TOO_MANY_REQUESTS
        elif result.event.status == 'retrying':
            http_status = status.HTTP_202_ACCEPTED
        elif result.event.status == 'dead_lettered':
            http_status = status.HTTP_503_SERVICE_UNAVAILABLE
        elif result.conflict:
            http_status = status.HTTP_409_CONFLICT
        response = Response(
            {
                'outbound_event_id': str(result.event.outbound_event_id),
                'provider_delivery_id': result.event.provider_delivery_id or None,
                'status': result.event.status,
                'idempotent_replay': result.idempotent_replay,
                'sent_to_provider': result.sent_to_provider,
                'error_code': result.event.error_code,
                'retry_after_seconds': (result.event.audit_metadata or {}).get('retry_after_seconds'),
                'delivery_state': (result.event.audit_metadata or {}).get('delivery_state') or result.event.status,
            },
            status=http_status,
        )
        retry_after = (result.event.audit_metadata or {}).get('retry_after_seconds')
        if retry_after:
            response['Retry-After'] = str(retry_after)
        return response


class OutboundBridgeDeliveryCallbackView(APIView):
    """Provider delivery/status callback. Stores hashes/provider ids, never bodies."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, provider: str, provider_account_id: str):
        raw_body = request.body or b""
        try:
            verify_request_or_raise(provider, provider_account_id, raw_body, request.META)
        except MissingWebhookSecret as exc:
            return Response({"error": str(exc)}, status=exc.status_code)
        except InvalidSignature as exc:
            return Response({"error": str(exc)}, status=exc.status_code)
        provider_delivery_id = (request.data.get('provider_delivery_id') or '').strip()
        status_value = (request.data.get('status') or '').strip()
        if not provider_delivery_id or not status_value:
            return Response(
                {'error': 'provider_delivery_id and status are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            event = record_outbound_delivery_status(
                provider=provider,
                provider_account_id=provider_account_id,
                provider_delivery_id=provider_delivery_id,
                status_value=status_value,
                payload=request.data,
            )
        except Exception:
            return Response({'error': 'delivery record not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            'outbound_event_id': str(event.outbound_event_id),
            'provider_delivery_id': event.provider_delivery_id,
            'status': event.status,
        })
