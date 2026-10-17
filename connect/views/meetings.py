"""
Meeting views for video conferencing in Shizuha Connect.
"""

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Q

from connect.models import Meeting, MeetingParticipant, MeetingInvite
from connect.serializers.meetings import (
    MeetingSerializer,
    MeetingListSerializer,
    MeetingCreateSerializer,
    MeetingParticipantSerializer,
    MeetingJoinSerializer,
    LiveKitTokenSerializer,
    MeetingInviteSerializer,
    MeetingInviteCreateSerializer,
    AdmitParticipantSerializer,
)
from connect.services.livekit_service import get_livekit_service
from connect.services.privacy import users_share_org


def _meeting_join_granted(meeting, user):
    """CON-150: meeting_code possession alone is not authorization.

    A code holder may join only when they are the host, are already a participant,
    hold an explicit invite, or share an org with the host. Everyone else is sent
    to the waiting room without receiving a LiveKit token.
    """
    if meeting.host_id == user.id:
        return True
    if MeetingParticipant.objects.filter(
        meeting=meeting,
        user_id=user.id,
        status__in=['invited', 'joined'],
    ).exists():
        return True
    if MeetingInvite.objects.filter(
        meeting=meeting
    ).filter(Q(invited_user_id=user.id) | Q(invited_email=user.email)).exists():
        return True
    return users_share_org(user.id, meeting.host_id)


class MeetingViewSet(viewsets.ModelViewSet):
    """
    ViewSet for managing video meetings.
    """
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        """Get meetings where user is host or participant."""
        user_id = self.request.user.id

        # Meetings where user is host
        host_meetings = Meeting.objects.filter(host_id=user_id)

        # Meetings where user is participant
        participant_meeting_ids = MeetingParticipant.objects.filter(
            user_id=user_id
        ).values_list('meeting_id', flat=True)

        # Meetings where user is invited
        invited_meeting_ids = MeetingInvite.objects.filter(
            Q(invited_user_id=user_id) | Q(invited_email=self.request.user.email)
        ).values_list('meeting_id', flat=True)

        return Meeting.objects.filter(
            Q(id__in=host_meetings) |
            Q(id__in=participant_meeting_ids) |
            Q(id__in=invited_meeting_ids)
        ).distinct().order_by('-created_at')

    def get_serializer_class(self):
        if self.action == 'list':
            return MeetingListSerializer
        if self.action == 'create':
            return MeetingCreateSerializer
        return MeetingSerializer

    def create(self, request, *args, **kwargs):
        """Create a scheduled meeting."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        meeting = serializer.save(
            host_id=user.id,
            host_email=user.email,
            host_name=user.get_full_name(),
        )

        # Add host as participant
        MeetingParticipant.objects.create(
            meeting=meeting,
            user_id=user.id,
            user_email=user.email,
            user_name=user.get_full_name(),
            role='host',
            status='invited',
        )

        # Return full meeting data
        response_serializer = MeetingSerializer(meeting, context={'request': request})
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'])
    def instant(self, request):
        """Create and start an instant meeting."""
        user = request.user

        # Create meeting
        meeting = Meeting.objects.create(
            title=request.data.get('title', 'Instant Meeting'),
            host_id=user.id,
            host_email=user.email,
            host_name=user.get_full_name(),
            status='active',
            started_at=timezone.now(),
        )

        # Add host as participant
        participant = MeetingParticipant.objects.create(
            meeting=meeting,
            user_id=user.id,
            user_email=user.email,
            user_name=user.get_full_name(),
            role='host',
            status='joined',
            joined_at=timezone.now(),
        )

        # Update participant count
        meeting.participant_count = 1
        meeting.peak_participants = 1
        meeting.save(update_fields=['participant_count', 'peak_participants'])

        # Generate LiveKit token
        livekit = get_livekit_service()
        token_data = livekit.create_room_token(
            meeting=meeting,
            user_id=user.id,
            user_name=user.get_full_name(),
            user_email=user.email,
            is_host=True,
        )

        response_data = {
            **token_data,
            'meeting': MeetingSerializer(meeting, context={'request': request}).data,
        }

        return Response(response_data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def start(self, request, pk=None):
        """Start a scheduled meeting (host only)."""
        meeting = self.get_object()

        # Verify user is host
        if meeting.host_id != request.user.id:
            return Response(
                {'error': 'Only the host can start the meeting'},
                status=status.HTTP_403_FORBIDDEN
            )

        if meeting.status != 'scheduled':
            return Response(
                {'error': f'Meeting cannot be started (status: {meeting.status})'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Start meeting
        meeting.start()

        # Update host participant status
        MeetingParticipant.objects.filter(
            meeting=meeting,
            user_id=request.user.id
        ).update(
            status='joined',
            joined_at=timezone.now()
        )

        # Generate token for host
        livekit = get_livekit_service()
        token_data = livekit.create_room_token(
            meeting=meeting,
            user_id=request.user.id,
            user_name=request.user.get_full_name(),
            user_email=request.user.email,
            is_host=True,
        )

        response_data = {
            **token_data,
            'meeting': MeetingSerializer(meeting, context={'request': request}).data,
        }

        return Response(response_data)

    @action(detail=True, methods=['post'])
    def end(self, request, pk=None):
        """End a meeting (host only)."""
        meeting = self.get_object()

        # Verify user is host
        if meeting.host_id != request.user.id:
            return Response(
                {'error': 'Only the host can end the meeting'},
                status=status.HTTP_403_FORBIDDEN
            )

        if meeting.status not in ['scheduled', 'active']:
            return Response(
                {'error': f'Meeting cannot be ended (status: {meeting.status})'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # End meeting
        meeting.end()

        # Mark all participants as left
        MeetingParticipant.objects.filter(
            meeting=meeting,
            status='joined'
        ).update(
            status='left',
            left_at=timezone.now()
        )

        return Response(MeetingSerializer(meeting, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def join(self, request, pk=None):
        """Join a meeting and get LiveKit token."""
        meeting = self.get_object()
        user = request.user

        if not meeting.can_join:
            return Response(
                {'error': f'Cannot join meeting (status: {meeting.status})'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check max participants
        if meeting.participant_count >= meeting.max_participants:
            return Response(
                {'error': 'Meeting is full'},
                status=status.HTTP_400_BAD_REQUEST
            )

        is_host = meeting.host_id == user.id
        # Evaluate authorization before get_or_create: a freshly-created
        # code-holder participant starts as ``invited`` by model default, and
        # must not become its own invite grant.
        join_granted = _meeting_join_granted(meeting, user)

        # Get or create participant
        participant, created = MeetingParticipant.objects.get_or_create(
            meeting=meeting,
            user_id=user.id,
            defaults={
                'user_email': user.email,
                'user_name': user.get_full_name(),
                'role': 'host' if meeting.host_id == user.id else 'participant',
            }
        )

        # Handle waiting room
        if (not join_granted or meeting.waiting_room_enabled) and not is_host and participant.status != 'joined':
            participant.status = 'waiting'
            participant.save(update_fields=['status', 'updated_at'])
            return Response({
                'status': 'waiting',
                'message': 'You are in the waiting room. The host will admit you shortly.',
                'meeting': MeetingSerializer(meeting, context={'request': request}).data,
            })

        # Join meeting
        if participant.status != 'joined':
            participant.join()

        # Generate LiveKit token
        livekit = get_livekit_service()
        token_data = livekit.create_room_token(
            meeting=meeting,
            user_id=user.id,
            user_name=user.get_full_name(),
            user_email=user.email,
            is_host=is_host,
        )

        response_data = {
            **token_data,
            'meeting': MeetingSerializer(meeting, context={'request': request}).data,
        }

        return Response(response_data)

    @action(detail=False, methods=['post'], url_path='join-by-code')
    def join_by_code(self, request):
        """Join a meeting using meeting code."""
        serializer = MeetingJoinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        meeting_code = serializer.validated_data['meeting_code']
        meeting = get_object_or_404(Meeting, meeting_code=meeting_code)

        # Directly handle join logic instead of delegating
        user = request.user

        if not meeting.can_join:
            return Response(
                {'error': f'Cannot join meeting (status: {meeting.status})'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check max participants
        if meeting.participant_count >= meeting.max_participants:
            return Response(
                {'error': 'Meeting is full'},
                status=status.HTTP_400_BAD_REQUEST
            )

        is_host = meeting.host_id == user.id
        # Evaluate authorization before get_or_create: a freshly-created
        # code-holder participant starts as ``invited`` by model default, and
        # must not become its own invite grant.
        join_granted = _meeting_join_granted(meeting, user)

        # Get or create participant
        participant, created = MeetingParticipant.objects.get_or_create(
            meeting=meeting,
            user_id=user.id,
            defaults={
                'user_email': user.email,
                'user_name': user.get_full_name(),
                'role': 'host' if meeting.host_id == user.id else 'participant',
            }
        )

        # Handle waiting room
        if (not join_granted or meeting.waiting_room_enabled) and not is_host and participant.status != 'joined':
            participant.status = 'waiting'
            participant.save(update_fields=['status', 'updated_at'])
            return Response({
                'status': 'waiting',
                'message': 'You are in the waiting room. The host will admit you shortly.',
                'meeting': MeetingSerializer(meeting, context={'request': request}).data,
            })

        # Join meeting
        if participant.status != 'joined':
            participant.join()

        # Generate LiveKit token
        livekit = get_livekit_service()
        token_data = livekit.create_room_token(
            meeting=meeting,
            user_id=user.id,
            user_name=user.get_full_name(),
            user_email=user.email,
            is_host=is_host,
        )

        response_data = {
            **token_data,
            'meeting': MeetingSerializer(meeting, context={'request': request}).data,
        }

        return Response(response_data)

    @action(detail=True, methods=['post'])
    def leave(self, request, pk=None):
        """Leave a meeting."""
        meeting = self.get_object()
        user = request.user

        participant = MeetingParticipant.objects.filter(
            meeting=meeting,
            user_id=user.id,
            status='joined'
        ).first()

        if participant:
            participant.leave()

        return Response({'status': 'left'})

    @action(detail=True, methods=['get'])
    def participants(self, request, pk=None):
        """Get meeting participants."""
        meeting = self.get_object()
        participants = meeting.participants.filter(
            status__in=['waiting', 'joined']
        ).order_by('-role', 'joined_at')

        serializer = MeetingParticipantSerializer(participants, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def invite(self, request, pk=None):
        """Send meeting invitations."""
        meeting = self.get_object()
        user = request.user

        # Verify user is host
        if meeting.host_id != user.id:
            return Response(
                {'error': 'Only the host can send invitations'},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = MeetingInviteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        emails = serializer.validated_data['emails']
        message = serializer.validated_data.get('message', '')

        created_invites = []
        for email in emails:
            invite, created = MeetingInvite.objects.get_or_create(
                meeting=meeting,
                invited_email=email,
                defaults={
                    'inviter_id': user.id,
                    'inviter_name': user.get_full_name(),
                    'message': message,
                }
            )
            if created:
                created_invites.append(invite)

        return Response({
            'status': 'invites_sent',
            'count': len(created_invites),
            'invites': MeetingInviteSerializer(created_invites, many=True).data,
        })

    @action(detail=True, methods=['post'])
    def admit(self, request, pk=None):
        """Admit a participant from the waiting room (host only)."""
        meeting = self.get_object()
        user = request.user

        # Verify user is host
        if meeting.host_id != user.id:
            return Response(
                {'error': 'Only the host can admit participants'},
                status=status.HTTP_403_FORBIDDEN
            )

        serializer = AdmitParticipantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        participant_id = serializer.validated_data['participant_id']
        participant = get_object_or_404(
            MeetingParticipant,
            id=participant_id,
            meeting=meeting,
            status='waiting'
        )

        # Admit participant
        participant.join()

        # Generate token for admitted participant
        livekit = get_livekit_service()
        token_data = livekit.create_room_token(
            meeting=meeting,
            user_id=participant.user_id,
            user_name=participant.user_name,
            user_email=participant.user_email,
            is_host=False,
        )

        return Response({
            'status': 'admitted',
            'participant': MeetingParticipantSerializer(participant).data,
            'token_data': token_data,
        })

    @action(detail=True, methods=['post'])
    def remove(self, request, pk=None):
        """Remove a participant from the meeting (host only)."""
        meeting = self.get_object()
        user = request.user

        # Verify user is host
        if meeting.host_id != user.id:
            return Response(
                {'error': 'Only the host can remove participants'},
                status=status.HTTP_403_FORBIDDEN
            )

        participant_id = request.data.get('participant_id')
        participant = get_object_or_404(
            MeetingParticipant,
            id=participant_id,
            meeting=meeting
        )

        # Cannot remove self
        if participant.user_id == user.id:
            return Response(
                {'error': 'Cannot remove yourself. Use "leave" instead.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        participant.status = 'removed'
        participant.left_at = timezone.now()
        participant.save(update_fields=['status', 'left_at', 'updated_at'])

        # Update meeting participant count
        meeting.participant_count = meeting.participants.filter(status='joined').count()
        meeting.save(update_fields=['participant_count', 'updated_at'])

        return Response({'status': 'removed'})

    @action(detail=True, methods=['get'])
    def waiting_room(self, request, pk=None):
        """Get participants in waiting room (host only)."""
        meeting = self.get_object()

        # Verify user is host
        if meeting.host_id != request.user.id:
            return Response(
                {'error': 'Only the host can view the waiting room'},
                status=status.HTTP_403_FORBIDDEN
            )

        waiting = meeting.participants.filter(status='waiting')
        serializer = MeetingParticipantSerializer(waiting, many=True)
        return Response(serializer.data)
