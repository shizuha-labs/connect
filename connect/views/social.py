"""
Social graph views for Shizuha Connect.

Handles follows, connections, blocking, and muting.
"""

import uuid as _uuid

from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import models
from django.shortcuts import get_object_or_404
from django.utils import timezone

from connect.services.privacy import users_share_org
from connect.models import Follow, Connection, Block, Mute, SocialProfile
from connect.serializers.social import (
    FollowSerializer, ConnectionSerializer, BlockSerializer, MuteSerializer
)
from connect.serializers.profile import SocialProfileSerializer


def _resolve_user_id(user_identifier):
    """Resolve int/UUID/username to a shizuha-id integer user_id, or None."""
    try:
        return int(user_identifier)
    except (ValueError, TypeError):
        pass
    # Validate UUID format before UUIDField filter — a bare username passed to
    # a UUIDField raises ValueError inside get_db_prep_value (CON-33/CON-40).
    try:
        _uuid.UUID(user_identifier)
        profile = SocialProfile.objects.filter(id=user_identifier).first()
        if profile:
            return profile.user_id
    except ValueError:
        pass
    profile = SocialProfile.objects.filter(username=user_identifier).first()
    return profile.user_id if profile else None


class FollowView(APIView):
    """Follow a user."""
    permission_classes = [IsAuthenticated]

    def post(self, request, user_identifier):
        user_id = _resolve_user_id(user_identifier)
        if user_id is None:
            return Response(
                {'error': f'User not found: {user_identifier}'},
                status=status.HTTP_404_NOT_FOUND,
            )
        if user_id == request.user.id:
            return Response(
                {'error': 'Cannot follow yourself'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Check if blocked
        if Block.objects.filter(
            blocker_id=user_id,
            blocked_id=request.user.id
        ).exists():
            return Response(
                {'error': 'Cannot follow this user'},
                status=status.HTTP_403_FORBIDDEN
            )

        follow, created = Follow.objects.get_or_create(
            follower_id=request.user.id,
            following_id=user_id
        )

        return Response({
            'status': 'followed' if created else 'already_following',
            'follow': FollowSerializer(follow).data
        })


class UnfollowView(APIView):
    """Unfollow a user."""
    permission_classes = [IsAuthenticated]

    def post(self, request, user_identifier):
        user_id = _resolve_user_id(user_identifier)
        if user_id is None:
            return Response(
                {'error': f'User not found: {user_identifier}'},
                status=status.HTTP_404_NOT_FOUND,
            )
        deleted, _ = Follow.objects.filter(
            follower_id=request.user.id,
            following_id=user_id
        ).delete()

        return Response({
            'status': 'unfollowed' if deleted else 'not_following'
        })


class ConnectionListView(generics.ListAPIView):
    """List user's connections."""
    permission_classes = [IsAuthenticated]
    serializer_class = SocialProfileSerializer

    def get_queryset(self):
        user_id = self.request.user.id

        # Get all accepted connections
        connections = Connection.objects.filter(
            status='accepted'
        ).filter(
            models.Q(user_a_id=user_id) | models.Q(user_b_id=user_id)
        )

        # Get the other user's ID for each connection
        connected_ids = []
        for conn in connections:
            if conn.user_a_id == user_id:
                connected_ids.append(conn.user_b_id)
            else:
                connected_ids.append(conn.user_a_id)

        return SocialProfile.objects.filter(user_id__in=connected_ids)


class ConnectionRequestsView(generics.ListAPIView):
    """List pending connection requests."""
    permission_classes = [IsAuthenticated]
    serializer_class = ConnectionSerializer

    def get_queryset(self):
        user_id = self.request.user.id

        # Get pending requests where user is the recipient
        return Connection.objects.filter(
            status='pending'
        ).filter(
            models.Q(user_a_id=user_id) | models.Q(user_b_id=user_id)
        ).exclude(
            requester_id=user_id
        )


class SendConnectionRequestView(APIView):
    """Send a connection request.

    Accepts `user_identifier` as:
      - An integer string (shizuha-id user_id)
      - A UUID string (SocialProfile.id)
      - A username string (SocialProfile.username)
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, user_identifier):
        target_user_id = _resolve_user_id(user_identifier)
        if target_user_id is None:
            return Response(
                {'error': f'User not found: {user_identifier}'},
                status=status.HTTP_404_NOT_FOUND,
            )

        user_id = target_user_id
        if user_id == request.user.id:
            return Response(
                {'error': 'Cannot connect with yourself'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not users_share_org(request.user.id, user_id):
            return Response(
                {'error': 'Cannot connect with this user'},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Check if already connected or pending
        user_a = min(request.user.id, user_id)
        user_b = max(request.user.id, user_id)

        existing = Connection.objects.filter(
            user_a_id=user_a,
            user_b_id=user_b
        ).first()

        if existing:
            if existing.status == 'accepted':
                return Response(
                    {'error': 'Already connected'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if existing.status == 'pending':
                return Response(
                    {'error': 'Connection request already pending'},
                    status=status.HTTP_400_BAD_REQUEST
                )

        message = request.data.get('message', '')

        connection = Connection.objects.create(
            user_a_id=user_a,
            user_b_id=user_b,
            requester_id=request.user.id,
            message=message,
            status='pending'
        )

        return Response({
            'status': 'request_sent',
            'connection': ConnectionSerializer(connection).data
        }, status=status.HTTP_201_CREATED)


class AcceptConnectionView(APIView):
    """Accept a connection request."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        connection = get_object_or_404(Connection, pk=pk)

        # Verify user is the recipient
        if connection.requester_id == request.user.id:
            return Response(
                {'error': 'Cannot accept your own request'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if request.user.id not in [connection.user_a_id, connection.user_b_id]:
            return Response(
                {'error': 'Not authorized'},
                status=status.HTTP_403_FORBIDDEN
            )

        if connection.status != 'pending':
            return Response(
                {'error': f'Connection is {connection.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        connection.status = 'accepted'
        connection.accepted_at = timezone.now()
        connection.save()

        # Also create mutual follows
        Follow.objects.get_or_create(
            follower_id=connection.user_a_id,
            following_id=connection.user_b_id
        )
        Follow.objects.get_or_create(
            follower_id=connection.user_b_id,
            following_id=connection.user_a_id
        )

        return Response({
            'status': 'accepted',
            'connection': ConnectionSerializer(connection).data
        })


class RejectConnectionView(APIView):
    """Reject a connection request."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        connection = get_object_or_404(Connection, pk=pk)

        if request.user.id not in [connection.user_a_id, connection.user_b_id]:
            return Response(
                {'error': 'Not authorized'},
                status=status.HTTP_403_FORBIDDEN
            )

        if connection.status != 'pending':
            return Response(
                {'error': f'Connection is {connection.status}'},
                status=status.HTTP_400_BAD_REQUEST
            )

        connection.status = 'rejected'
        connection.save()

        return Response({'status': 'rejected'})


class BlockView(APIView):
    """Block a user."""
    permission_classes = [IsAuthenticated]

    def post(self, request, user_identifier):
        user_id = _resolve_user_id(user_identifier)
        if user_id is None:
            return Response(
                {'error': f'User not found: {user_identifier}'},
                status=status.HTTP_404_NOT_FOUND,
            )
        if user_id == request.user.id:
            return Response(
                {'error': 'Cannot block yourself'},
                status=status.HTTP_400_BAD_REQUEST
            )

        block, created = Block.objects.get_or_create(
            blocker_id=request.user.id,
            blocked_id=user_id
        )

        # Remove any follows
        Follow.objects.filter(
            follower_id=request.user.id,
            following_id=user_id
        ).delete()
        Follow.objects.filter(
            follower_id=user_id,
            following_id=request.user.id
        ).delete()

        return Response({
            'status': 'blocked' if created else 'already_blocked'
        })


class UnblockView(APIView):
    """Unblock a user."""
    permission_classes = [IsAuthenticated]

    def post(self, request, user_identifier):
        user_id = _resolve_user_id(user_identifier)
        if user_id is None:
            return Response(
                {'error': f'User not found: {user_identifier}'},
                status=status.HTTP_404_NOT_FOUND,
            )
        deleted, _ = Block.objects.filter(
            blocker_id=request.user.id,
            blocked_id=user_id
        ).delete()

        return Response({
            'status': 'unblocked' if deleted else 'not_blocked'
        })


class MuteView(APIView):
    """Mute a user."""
    permission_classes = [IsAuthenticated]

    def post(self, request, user_identifier):
        user_id = _resolve_user_id(user_identifier)
        if user_id is None:
            return Response(
                {'error': f'User not found: {user_identifier}'},
                status=status.HTTP_404_NOT_FOUND,
            )
        duration = request.data.get('duration')  # in hours, None = permanent
        mute_notifications = request.data.get('mute_notifications', True)
        mute_from_feed = request.data.get('mute_from_feed', True)

        expires_at = None
        if duration:
            expires_at = timezone.now() + timezone.timedelta(hours=int(duration))

        mute, created = Mute.objects.update_or_create(
            user_id=request.user.id,
            muted_user_id=user_id,
            defaults={
                'expires_at': expires_at,
                'mute_notifications': mute_notifications,
                'mute_from_feed': mute_from_feed,
            }
        )

        return Response({
            'status': 'muted' if created else 'updated',
            'mute': MuteSerializer(mute).data
        })


class UnmuteView(APIView):
    """Unmute a user."""
    permission_classes = [IsAuthenticated]

    def post(self, request, user_identifier):
        user_id = _resolve_user_id(user_identifier)
        if user_id is None:
            return Response(
                {'error': f'User not found: {user_identifier}'},
                status=status.HTTP_404_NOT_FOUND,
            )
        deleted, _ = Mute.objects.filter(
            user_id=request.user.id,
            muted_user_id=user_id
        ).delete()

        return Response({
            'status': 'unmuted' if deleted else 'not_muted'
        })
