"""
Meeting serializers for Shizuha Connect.
"""

from rest_framework import serializers
from connect.models import Meeting, MeetingParticipant, MeetingInvite


class MeetingParticipantSerializer(serializers.ModelSerializer):
    """Serializer for meeting participants."""

    display_name = serializers.ReadOnlyField()

    class Meta:
        model = MeetingParticipant
        fields = [
            'id',
            'user_id',
            'user_email',
            'user_name',
            'is_guest',
            'guest_name',
            'display_name',
            'role',
            'status',
            'joined_at',
            'left_at',
            'is_muted',
            'is_video_off',
            'is_screen_sharing',
            'is_hand_raised',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at', 'display_name']


class MeetingInviteSerializer(serializers.ModelSerializer):
    """Serializer for meeting invites."""

    class Meta:
        model = MeetingInvite
        fields = [
            'id',
            'meeting',
            'invited_user_id',
            'invited_email',
            'invited_name',
            'inviter_id',
            'inviter_name',
            'accepted',
            'declined',
            'responded_at',
            'message',
            'created_at',
        ]
        read_only_fields = ['id', 'meeting', 'inviter_id', 'inviter_name', 'created_at']


class MeetingSerializer(serializers.ModelSerializer):
    """Full serializer for meetings with participants."""

    participants = MeetingParticipantSerializer(many=True, read_only=True)
    is_active = serializers.ReadOnlyField()
    can_join = serializers.ReadOnlyField()
    shareable_link = serializers.SerializerMethodField()

    class Meta:
        model = Meeting
        fields = [
            'id',
            'title',
            'description',
            'meeting_code',
            'host_id',
            'host_email',
            'host_name',
            'scheduled_start',
            'scheduled_end',
            'started_at',
            'ended_at',
            'livekit_room_name',
            'waiting_room_enabled',
            'recording_enabled',
            'chat_enabled',
            'screen_share_enabled',
            'max_participants',
            'status',
            'participant_count',
            'peak_participants',
            'participants',
            'is_active',
            'can_join',
            'shareable_link',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'meeting_code',
            'host_id',
            'host_email',
            'host_name',
            'livekit_room_name',
            'started_at',
            'ended_at',
            'status',
            'participant_count',
            'peak_participants',
            'created_at',
            'updated_at',
        ]

    def get_shareable_link(self, obj):
        request = self.context.get('request')
        if request:
            base_url = f"{request.scheme}://{request.get_host()}"
            return obj.get_shareable_link(base_url)
        return obj.get_shareable_link()


class MeetingListSerializer(serializers.ModelSerializer):
    """Lighter serializer for listing meetings."""

    is_active = serializers.ReadOnlyField()
    can_join = serializers.ReadOnlyField()

    class Meta:
        model = Meeting
        fields = [
            'id',
            'title',
            'meeting_code',
            'host_id',
            'host_name',
            'scheduled_start',
            'scheduled_end',
            'status',
            'participant_count',
            'is_active',
            'can_join',
            'created_at',
        ]


class MeetingCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating meetings."""

    invite_emails = serializers.ListField(
        child=serializers.EmailField(),
        required=False,
        write_only=True,
        default=list
    )

    class Meta:
        model = Meeting
        fields = [
            'title',
            'description',
            'scheduled_start',
            'scheduled_end',
            'waiting_room_enabled',
            'recording_enabled',
            'chat_enabled',
            'screen_share_enabled',
            'max_participants',
            'invite_emails',
        ]

    def create(self, validated_data):
        # Remove invite_emails before creating meeting
        invite_emails = validated_data.pop('invite_emails', [])

        # Create meeting
        meeting = Meeting.objects.create(**validated_data)

        # Create invites
        request = self.context.get('request')
        if request and invite_emails:
            for email in invite_emails:
                MeetingInvite.objects.create(
                    meeting=meeting,
                    invited_email=email,
                    inviter_id=request.user.id,
                    inviter_name=request.user.get_full_name(),
                )

        return meeting


class MeetingJoinSerializer(serializers.Serializer):
    """Serializer for joining a meeting by code."""

    meeting_code = serializers.CharField(max_length=12)
    display_name = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_meeting_code(self, value):
        # Normalize code (lowercase, keep hyphens)
        value = value.lower().strip()

        # Validate format
        if len(value) != 12 or value[3] != '-' or value[8] != '-':
            raise serializers.ValidationError('Invalid meeting code format. Expected: xxx-xxxx-xxx')

        # Check meeting exists and can be joined
        try:
            meeting = Meeting.objects.get(meeting_code=value)
        except Meeting.DoesNotExist:
            raise serializers.ValidationError('Meeting not found')

        if not meeting.can_join:
            if meeting.status == 'ended':
                raise serializers.ValidationError('This meeting has ended')
            elif meeting.status == 'cancelled':
                raise serializers.ValidationError('This meeting has been cancelled')

        return value


class LiveKitTokenSerializer(serializers.Serializer):
    """Serializer for LiveKit token response."""

    token = serializers.CharField()
    url = serializers.CharField()
    room_name = serializers.CharField()
    participant_identity = serializers.CharField()
    meeting = MeetingSerializer(read_only=True)


class MeetingInviteCreateSerializer(serializers.Serializer):
    """Serializer for sending meeting invites."""

    emails = serializers.ListField(
        child=serializers.EmailField(),
        min_length=1,
        max_length=50
    )
    message = serializers.CharField(required=False, allow_blank=True, default='')


class AdmitParticipantSerializer(serializers.Serializer):
    """Serializer for admitting a participant from waiting room."""

    participant_id = serializers.UUIDField()
