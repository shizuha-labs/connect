"""
Django admin configuration for Shizuha Connect.
"""

from django.contrib import admin
from .models import (
    SocialProfile, Skill, UserSkill, Endorsement, Experience, Education,
    Follow, Connection, Block, Mute,
    Post, PostMedia, Hashtag, PostHashtag, Mention,
    Reaction, Comment, CommentLike, Bookmark, BookmarkCollection,
    Conversation, ConversationParticipant, DirectMessage, MessageRead,
    Story, StoryView, StoryReaction,
    FeedItem, TrendingTopic,
)


# Profile Admin
@admin.register(SocialProfile)
class SocialProfileAdmin(admin.ModelAdmin):
    list_display = ['username', 'display_name', 'user_email', 'followers_count', 'is_verified', 'created_at']
    list_filter = ['is_verified', 'profile_visibility', 'industry']
    search_fields = ['username', 'display_name', 'user_email']
    readonly_fields = ['followers_count', 'following_count', 'posts_count', 'connections_count']


@admin.register(Skill)
class SkillAdmin(admin.ModelAdmin):
    list_display = ['name', 'category', 'slug']
    list_filter = ['category']
    search_fields = ['name']
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Experience)
class ExperienceAdmin(admin.ModelAdmin):
    list_display = ['profile', 'title', 'company_name', 'is_current', 'start_date']
    list_filter = ['is_current', 'employment_type']
    search_fields = ['title', 'company_name']


# Social Graph Admin
@admin.register(Follow)
class FollowAdmin(admin.ModelAdmin):
    list_display = ['follower_id', 'following_id', 'notify_on_post', 'created_at']
    list_filter = ['notify_on_post']


@admin.register(Connection)
class ConnectionAdmin(admin.ModelAdmin):
    list_display = ['user_a_id', 'user_b_id', 'status', 'created_at', 'accepted_at']
    list_filter = ['status']


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = ['blocker_id', 'blocked_id', 'created_at']


# Content Admin
@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ['id', 'author_email', 'post_type', 'visibility', 'likes_count', 'created_at']
    list_filter = ['post_type', 'visibility', 'is_pinned', 'is_sensitive']
    search_fields = ['content', 'author_email']
    readonly_fields = ['likes_count', 'comments_count', 'reposts_count', 'views_count']


@admin.register(PostMedia)
class PostMediaAdmin(admin.ModelAdmin):
    list_display = ['id', 'post', 'media_type', 'is_processed', 'position']
    list_filter = ['media_type', 'is_processed']


@admin.register(Hashtag)
class HashtagAdmin(admin.ModelAdmin):
    list_display = ['name', 'post_count', 'weekly_post_count', 'last_used_at']
    search_fields = ['name']
    readonly_fields = ['post_count', 'weekly_post_count']


# Engagement Admin
@admin.register(Reaction)
class ReactionAdmin(admin.ModelAdmin):
    list_display = ['post', 'user_id', 'reaction_type', 'created_at']
    list_filter = ['reaction_type']


@admin.register(Comment)
class CommentAdmin(admin.ModelAdmin):
    list_display = ['post', 'author_email', 'depth', 'likes_count', 'created_at']
    list_filter = ['is_edited']
    search_fields = ['content', 'author_email']


# Company Admin




# Jobs Admin




# Messaging Admin
@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ['id', 'conversation_type', 'name', 'message_count', 'last_message_at']
    list_filter = ['conversation_type']


@admin.register(DirectMessage)
class DirectMessageAdmin(admin.ModelAdmin):
    list_display = ['id', 'conversation', 'sender_email', 'message_type', 'is_deleted', 'created_at']
    list_filter = ['message_type', 'is_deleted']


# Stories Admin
@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    list_display = ['id', 'author_email', 'story_type', 'views_count', 'created_at', 'expires_at']
    list_filter = ['story_type']


# Feed Admin
@admin.register(TrendingTopic)
class TrendingTopicAdmin(admin.ModelAdmin):
    list_display = ['name', 'rank', 'post_count', 'location', 'window_start', 'window_end']
    list_filter = ['location']
    ordering = ['rank']
