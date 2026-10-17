"""
WebSocket URL routing for Shizuha Connect.
"""

from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/connect/$', consumers.ConnectConsumer.as_asgi()),
    re_path(r'ws/connect/feed/$', consumers.FeedConsumer.as_asgi()),
    re_path(r'ws/connect/chat/(?P<conversation_id>[0-9a-f-]+)/$', consumers.ChatConsumer.as_asgi()),
    # Unified multiplexed consumer for all participants. Agents identify
    # themselves via the `is_agent: true` JWT claim — same code path as humans.
    # `ws/connect/agent/` is kept as an alias for already-deployed agents; it
    # will be removed once all agents migrate to `ws/connect/user/`.
    re_path(r'ws/connect/user/$', consumers.UserChatConsumer.as_asgi()),
    re_path(r'ws/connect/agent/$', consumers.UserChatConsumer.as_asgi()),
]
