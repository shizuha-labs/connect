"""
Post serializers for Shizuha Connect.
"""

from rest_framework import serializers
from connect.models import (
    Post, PostMedia, Hashtag, PostHashtag, Mention, Reaction, Comment, Bookmark, BookmarkCollection
)

class PostMediaSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostMedia
        fields = [
            'id', 'media_type', 'file_url', 'thumbnail_url',
            'width', 'height', 'duration', 'alt_text', 'position'
        ]
        read_only_fields = ['id']


class HashtagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Hashtag
        fields = ['id', 'name', 'post_count', 'weekly_post_count']
        read_only_fields = ['id', 'post_count', 'weekly_post_count']


class MentionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Mention
        fields = [
            'id', 'mentioned_user_id', 'mentioned_email',
            'mentioned_username', 'start_index', 'end_index'
        ]
        read_only_fields = ['id']


class ReactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Reaction
        fields = ['id', 'user_id', 'user_email', 'reaction_type', 'created_at']
        read_only_fields = ['id', 'created_at']


class CommentSerializer(serializers.ModelSerializer):
    replies = serializers.SerializerMethodField()
    replies_count = serializers.SerializerMethodField()

    class Meta:
        model = Comment
        fields = [
            'id', 'post', 'author_id', 'author_email', 'author_name',
            'content', 'parent', 'depth', 'likes_count', 'is_edited',
            'created_at', 'updated_at', 'replies', 'replies_count'
        ]
        read_only_fields = [
            'id', 'author_id', 'author_email', 'author_name',
            'likes_count', 'created_at', 'updated_at'
        ]

    def get_replies(self, obj):
        if obj.depth >= 2:  # Limit nesting depth
            return []
        replies = Comment.objects.filter(parent=obj).order_by('created_at')[:5]
        return CommentSerializer(replies, many=True).data

    def get_replies_count(self, obj):
        # CON-347: the parent comment row must report its reply count so
        # list_comments renders Replies: N consistently with list_replies.
        return Comment.objects.filter(parent=obj).count()


class CommentCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Comment
        fields = ['content', 'parent']


class PostSerializer(serializers.ModelSerializer):
    media = PostMediaSerializer(many=True, read_only=True)
    mentions = MentionSerializer(many=True, read_only=True)
    original_post_data = serializers.SerializerMethodField()
    user_reaction = serializers.SerializerMethodField()
    reactions_count = serializers.SerializerMethodField()
    is_bookmarked = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = [
            'id', 'author_id', 'author_email', 'author_username', 'author_display_name',
            'post_type', 'content',
            'content_html', 'original_post', 'reply_to', 'thread_root',
            'thread_depth', 'visibility', 'shared_content_type',
            'shared_content_id', 'shared_content_data', 'likes_count',
            'comments_count', 'reposts_count', 'quotes_count', 'views_count',
            'language', 'is_sensitive', 'is_pinned', 'is_edited',
            'created_at', 'updated_at', 'scheduled_at',
            'media', 'mentions', 'original_post_data', 'user_reaction',
            'reactions_count', 'is_bookmarked'
        ]
        read_only_fields = [
            'id', 'author_id', 'author_email', 'author_username', 'author_display_name',
            'likes_count', 'comments_count',
            'reposts_count', 'quotes_count', 'views_count', 'created_at',
            'updated_at'
        ]

    def get_original_post_data(self, obj):
        if obj.original_post:
            return {
                'id': str(obj.original_post.id),
                'author_id': obj.original_post.author_id,
                'author_email': obj.original_post.author_email,
                'author_username': obj.original_post.author_username,
                'author_display_name': obj.original_post.author_display_name,
                'content': obj.original_post.content[:280],
                'created_at': obj.original_post.created_at.isoformat(),
            }
        return None

    def get_user_reaction(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            reaction = Reaction.objects.filter(
                post=obj,
                user_id=request.user.id
            ).first()
            if reaction:
                return reaction.reaction_type
        return None

    def get_reactions_count(self, obj):
        """CON-263: expose the live reaction count so read surfaces (MCP
        connect_get_post / connect_get_my_posts) render it instead of a
        hardcoded 0."""
        return obj.reactions.count()

    def get_is_bookmarked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return Bookmark.objects.filter(
                post=obj,
                user_id=request.user.id
            ).exists()
        return False


class PostCreateSerializer(serializers.ModelSerializer):
    media = PostMediaSerializer(many=True, required=False)
    hashtags = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
        write_only=True,
    )

    class Meta:
        model = Post
        fields = [
            'id', 'content', 'post_type', 'visibility', 'reply_to',
            'is_sensitive', 'scheduled_at', 'media', 'hashtags'
        ]
        read_only_fields = ['id']

    def create(self, validated_data):
        media_data = validated_data.pop('media', [])
        hashtag_names = validated_data.pop('hashtags', [])
        post = Post.objects.create(**validated_data)

        for position, media_item in enumerate(media_data):
            media_item['position'] = position
            PostMedia.objects.create(post=post, **media_item)

        for raw_name in hashtag_names:
            name = str(raw_name).strip().lstrip('#')[:100]
            if not name:
                continue
            hashtag, _ = Hashtag.objects.get_or_create(name=name)
            PostHashtag.objects.get_or_create(post=post, hashtag=hashtag)

        return post


class PostUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Post
        fields = ['content', 'visibility', 'is_sensitive']


class BookmarkCollectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = BookmarkCollection
        fields = ['id', 'name', 'description', 'is_private', 'created_at']
        read_only_fields = ['id', 'created_at']


class BookmarkSerializer(serializers.ModelSerializer):
    post = PostSerializer(read_only=True)
    collection = BookmarkCollectionSerializer(read_only=True)

    class Meta:
        model = Bookmark
        fields = ['id', 'post', 'collection', 'note', 'created_at']
        read_only_fields = ['id', 'created_at']
