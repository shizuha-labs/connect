"""
LiveKit service for video conferencing token generation and room management.
"""

import logging
from datetime import timedelta
from django.conf import settings
from livekit.api import AccessToken, VideoGrants
from rest_framework.exceptions import APIException

logger = logging.getLogger(__name__)


class LiveKitUnavailable(APIException):
    status_code = 503
    default_detail = 'Video meetings are not available on this installation.'
    default_code = 'video_unavailable'


class LiveKitService:
    """
    Service for interacting with LiveKit server.
    Handles token generation and room management.
    """

    def __init__(self):
        self.api_key = getattr(settings, 'LIVEKIT_API_KEY', '')
        self.api_secret = getattr(settings, 'LIVEKIT_API_SECRET', '')
        self.url = getattr(settings, 'LIVEKIT_URL', 'http://shizuha-livekit:7880')
        self.external_url = getattr(settings, 'LIVEKIT_EXTERNAL_URL', 'ws://localhost:7880')

    def create_token(
        self,
        room_name: str,
        participant_identity: str,
        participant_name: str = '',
        is_host: bool = False,
        ttl_seconds: int = 86400,  # 24 hours
    ) -> str:
        """
        Create a LiveKit access token for a participant.

        Args:
            room_name: The room to join
            participant_identity: Unique identifier for the participant
            participant_name: Display name for the participant
            is_host: Whether this participant is the host (has admin privileges)
            ttl_seconds: Token validity duration

        Returns:
            JWT token string
        """
        if not self.api_key or not self.api_secret:
            raise LiveKitUnavailable()

        # Configure video grants based on role
        grant = VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
        )

        # Hosts get additional privileges
        if is_host:
            grant.room_admin = True
            grant.room_record = True

        # Create token with grants
        token = AccessToken(
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        token.identity = participant_identity
        token.name = participant_name or participant_identity
        token.ttl = timedelta(seconds=ttl_seconds)
        token.video_grants = grant

        return token.to_jwt()

    def get_websocket_url(self) -> str:
        """
        Get the LiveKit WebSocket URL for frontend connection.

        Returns:
            WebSocket URL string
        """
        return self.external_url

    def create_room_token(
        self,
        meeting,
        user_id: int,
        user_name: str,
        user_email: str,
        is_host: bool = False,
    ) -> dict:
        """
        Create a token for joining a meeting room.

        Args:
            meeting: Meeting model instance
            user_id: User's ID
            user_name: User's display name
            user_email: User's email
            is_host: Whether this user is the host

        Returns:
            Dict with token and connection info
        """
        # Create unique participant identity
        participant_identity = f"user-{user_id}"

        # Generate token
        token = self.create_token(
            room_name=meeting.livekit_room_name,
            participant_identity=participant_identity,
            participant_name=user_name or user_email,
            is_host=is_host,
        )

        return {
            'token': token,
            'url': self.get_websocket_url(),
            'room_name': meeting.livekit_room_name,
            'participant_identity': participant_identity,
        }


# Singleton instance
_livekit_service = None


def get_livekit_service() -> LiveKitService:
    """Get the singleton LiveKit service instance."""
    global _livekit_service
    if _livekit_service is None:
        _livekit_service = LiveKitService()
    return _livekit_service
