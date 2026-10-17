"""
Custom pagination classes for Shizuha Connect.
"""

from rest_framework.pagination import CursorPagination, PageNumberPagination


class CreatedAtCursorPagination(CursorPagination):
    """
    Cursor pagination using 'created_at' field.

    CursorPagination defaults to 'created' but our models use 'created_at'.
    This class ensures consistent ordering across all endpoints.
    """
    ordering = '-created_at'
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class PostsPagination(CursorPagination):
    """Pagination for posts feed."""
    ordering = '-created_at'
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 50


class MessagesPagination(CursorPagination):
    """Pagination for messages (oldest first)."""
    ordering = 'created_at'
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 100


class CommentsPagination(CursorPagination):
    """Pagination for comments."""
    ordering = '-created_at'
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 50


class StandardResultsSetPagination(PageNumberPagination):
    """Standard page number pagination for list endpoints."""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100
