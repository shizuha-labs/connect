"""
Feed serializers for Shizuha Connect.
"""

from rest_framework import serializers
from connect.models import FeedItem, TrendingTopic


class FeedItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = FeedItem
        fields = [
            'id', 'user_id', 'feed_type', 'post', 'score',
            'reason', 'reason_user_id', 'created_at', 'post_created_at'
        ]
        read_only_fields = ['id', 'created_at']


class TrendingTopicSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrendingTopic
        fields = [
            'id', 'name', 'hashtag', 'post_count', 'velocity',
            'rank', 'score', 'location', 'category',
            'window_start', 'window_end', 'sample_post_id'
        ]
        read_only_fields = ['id']
