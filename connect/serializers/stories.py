"""
Stories serializers for Shizuha Connect.
"""

from rest_framework import serializers
from connect.models import Story, StoryView, StoryReaction


class StoryViewSerializer(serializers.ModelSerializer):
    class Meta:
        model = StoryView
        fields = ['id', 'viewer_id', 'viewer_email', 'viewed_at']
        read_only_fields = ['id', 'viewed_at']


class StoryReactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = StoryReaction
        fields = ['id', 'user_id', 'user_email', 'reaction', 'message', 'created_at']
        read_only_fields = ['id', 'created_at']


class StorySerializer(serializers.ModelSerializer):
    is_viewed = serializers.SerializerMethodField()
    time_remaining_seconds = serializers.SerializerMethodField()
    # CON-306: user-facing aliases for the MCP/API contract. The MCP client
    # reads content/caption/author_name; keep text_content/author_username for
    # backward compat with existing consumers.
    content = serializers.SerializerMethodField()
    caption = serializers.SerializerMethodField()
    author_name = serializers.SerializerMethodField()

    class Meta:
        model = Story
        fields = [
            'id', 'author_id', 'author_email', 'author_username', 'author_name',
            'story_type', 'visibility', 'media_url', 'text_content', 'content', 'caption',
            'background_color', 'font_style', 'duration', 'stickers', 'link_url', 'link_title',
            'location', 'music_track', 'views_count', 'created_at',
            'expires_at', 'is_viewed', 'time_remaining_seconds'
        ]
        read_only_fields = [
            'id', 'author_id', 'author_email', 'author_username',
            'views_count', 'created_at', 'expires_at'
        ]

    def get_content(self, obj):
        return obj.text_content

    def get_caption(self, obj):
        return obj.text_content

    def get_author_name(self, obj):
        return obj.author_username or obj.author_email or 'Unknown'

    def get_is_viewed(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return StoryView.objects.filter(
                story=obj,
                viewer_id=request.user.id
            ).exists()
        return False

    def get_time_remaining_seconds(self, obj):
        remaining = obj.time_remaining
        return int(remaining.total_seconds())


class StoryCreateSerializer(serializers.ModelSerializer):
    # CON-306: accept the MCP contract field names content/caption and map them
    # to the model's text_content. story_type defaults to 'text' when content is
    # provided, and id is included in the create output so the client gets the
    # canonical story ID (previously returned ID: None).
    content = serializers.CharField(required=False, allow_blank=True)
    caption = serializers.CharField(required=False, allow_blank=True)
    # The model field has no default, so DRF marks it required at field level;
    # make it optional here so validate() can default it to 'text' when content
    # is provided (CON-306).
    story_type = serializers.ChoiceField(
        choices=Story.STORY_TYPES, required=False, allow_blank=True
    )

    class Meta:
        model = Story
        fields = [
            'id', 'story_type', 'visibility', 'media_url', 'text_content',
            'content', 'caption', 'background_color', 'font_style', 'duration',
            'stickers', 'link_url', 'link_title', 'location', 'music_track'
        ]
        read_only_fields = ['id']

    def validate(self, data):
        content = data.pop('content', None)
        caption = data.pop('caption', None)
        if not data.get('text_content'):
            data['text_content'] = content or caption or ''
        if not data.get('story_type') and (data.get('text_content') or data.get('media_url')):
            data['story_type'] = 'text'

        story_type = data.get('story_type')

        if story_type in ['image', 'video'] and not data.get('media_url'):
            raise serializers.ValidationError(
                f"{story_type} stories require a media_url"
            )

        if story_type == 'text' and not data.get('text_content'):
            raise serializers.ValidationError(
                "Text stories require text_content"
            )

        return data

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['content'] = instance.text_content
        data['caption'] = instance.text_content
        return data
