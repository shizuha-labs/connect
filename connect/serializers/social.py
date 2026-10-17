"""
Social graph serializers for Shizuha Connect.
"""

from rest_framework import serializers
from connect.models import Follow, Connection, Block, Mute


class FollowSerializer(serializers.ModelSerializer):
    class Meta:
        model = Follow
        fields = ['id', 'follower_id', 'following_id', 'notify_on_post', 'created_at']
        read_only_fields = ['id', 'created_at']


class ConnectionSerializer(serializers.ModelSerializer):
    requester_name = serializers.SerializerMethodField()
    requester_username = serializers.SerializerMethodField()

    class Meta:
        model = Connection
        fields = [
            'id', 'user_a_id', 'user_b_id', 'requester_id',
            'requester_name', 'requester_username', 'status', 'message', 'created_at', 'accepted_at'
        ]
        read_only_fields = ['id', 'created_at', 'accepted_at']

    def _requester_profile(self, obj):
        from connect.models import SocialProfile
        return SocialProfile.objects.filter(user_id=obj.requester_id).first()

    def get_requester_name(self, obj):
        try:
            profile = self._requester_profile(obj)
            if profile:
                return profile.display_name or profile.username or f"User {obj.requester_id}"
        except Exception:
            pass
        return f"User {obj.requester_id}"

    def get_requester_username(self, obj):
        """CON-261: pending requests must carry the sender's username so the
        recipient can identify the requester (the MCP renders ``Unknown (@)``
        when username is missing)."""
        try:
            profile = self._requester_profile(obj)
            if profile and profile.username:
                return profile.username
        except Exception:
            pass
        return ""


class BlockSerializer(serializers.ModelSerializer):
    class Meta:
        model = Block
        fields = ['id', 'blocker_id', 'blocked_id', 'created_at']
        read_only_fields = ['id', 'created_at']


class MuteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Mute
        fields = [
            'id', 'user_id', 'muted_user_id', 'muted_keyword',
            'mute_notifications', 'mute_from_feed', 'created_at', 'expires_at'
        ]
        read_only_fields = ['id', 'created_at']
