#!/usr/bin/env python3
"""
Shizuha Connect MCP Server.

Social/professional networking tools for AI agents.
Covers profiles, posts, comments, feed, social graph, search,
conversations, stories, meetings, and sharing.

Tools (65):
- connect_get_my_profile: Get current user's social profile
- connect_update_my_profile: Update current user's profile
- connect_get_my_posts: List current user's posts
- connect_get_my_bookmarks: List bookmarked posts
- connect_get_profile: Get a user's profile by username
- connect_get_profile_posts: Get a user's posts
- connect_get_profile_followers: List a user's followers
- connect_get_profile_following: List who a user follows
- connect_get_profile_skills: Get a user's skills
- connect_get_profile_experience: Get a user's work experience
- connect_get_profile_education: Get a user's education
- connect_list_skills: List all available skills
- connect_list_experience: List current user's experience entries
- connect_create_experience: Add work experience
- connect_update_experience: Update a work experience entry
- connect_delete_experience: Delete a work experience entry
- connect_list_education: List current user's education entries
- connect_create_education: Add education entry
- connect_update_education: Update an education entry
- connect_delete_education: Delete an education entry
- connect_list_posts: List feed posts (home, following, trending)
- connect_create_post: Create a new post
- connect_get_post: Get a post with full details
- connect_update_post: Update an existing post
- connect_delete_post: Delete a post
- connect_react_to_post: React to a post
- connect_unreact_to_post: Remove reaction from a post
- connect_repost: Repost/share a post
- connect_quote_post: Quote a post with commentary
- connect_bookmark_post: Bookmark a post
- connect_unbookmark_post: Remove bookmark from a post
- connect_list_comments: List comments on a post
- connect_add_comment: Add a comment to a post
- connect_list_replies: List replies to a comment
- connect_add_reply: Reply to a comment
- connect_like_comment: Like a comment
- connect_unlike_comment: Remove like from a comment
- connect_follow_user: Follow a user
- connect_unfollow_user: Unfollow a user
- connect_list_connections: List connections
- connect_list_connection_requests: List pending connection requests
- connect_send_connection_request: Send a connection request
- connect_accept_connection: Accept a connection request
- connect_reject_connection: Reject a connection request
- connect_block_user: Block a user
- connect_unblock_user: Unblock a user
- connect_search: Global search across content types
- connect_search_posts: Search posts
- connect_search_people: Search users
- connect_trending_hashtags: Get trending hashtags
- connect_list_conversations: List messaging conversations
- connect_create_conversation: Start a new conversation
- connect_get_conversation: Get conversation details
- connect_list_messages: List messages in a conversation
- connect_send_message: Send a message in a conversation
- connect_mark_conversation_read: Mark a conversation as read
- connect_list_stories: List stories from connections
- connect_create_story: Create a story
- connect_get_story: Get story details
- connect_view_story: Mark a story as viewed
- connect_react_to_story: React to a story
- connect_get_my_stories: Get current user's stories
- connect_list_meetings: List scheduled meetings
- connect_create_meeting: Schedule a new meeting
- connect_get_meeting: Get meeting details
- connect_create_instant_meeting: Start an instant meeting
- connect_start_meeting: Start a scheduled meeting
- connect_end_meeting: End a meeting
- connect_join_meeting: Join a meeting
- connect_join_meeting_by_code: Join a meeting via code
- connect_list_meeting_participants: List meeting participants
- connect_invite_to_meeting: Invite users to a meeting
- connect_share_to_drive: Share a Drive file into the Connect feed
- connect_share_to_pulse: Share a Pulse task into the Connect feed
- connect_share_to_wiki: Share a Wiki page into the Connect feed
- connect_share_to_notes: Share a Note into the Connect feed
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional

from base import ShizuhaMCPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from base import ShizuhaMCPBase, run_mcp_server


def parse_args():
    """Parse command-line arguments for CLI argument compatibility."""
    parser = argparse.ArgumentParser(description="Shizuha Connect MCP Server")
    parser.add_argument(
        "--jwt-token",
        help="JWT token for authentication (overrides SHIZUHA_CONNECT_JWT_TOKEN env var)",
    )
    parser.add_argument(
        "--api-url",
        help="Connect API URL (overrides SHIZUHA_CONNECT_URL env var)",
    )
    args, _ = parser.parse_known_args()
    return args


# Parse CLI args (for callers that don't pass env vars to MCP subprocesses)
_args = parse_args()

# Configuration priority: CLI args > environment > defaults
CONNECT_API_URL = (
    _args.api_url
    or os.environ.get("SHIZUHA_CONNECT_URL")
    or os.environ.get("CONNECT_API_URL")
)
CONNECT_JWT_TOKEN = (
    _args.jwt_token
    or os.environ.get("SHIZUHA_CONNECT_JWT_TOKEN")
    or os.environ.get("CONNECT_JWT_TOKEN")
    or ""
)

# CON-59 (cf. PLAT-567): shizuha-connect runs HA (>1 replica) behind streamable-HTTP.
# Without stateless_http, FastMCP keeps per-session state in-process, so a follow-up
# request load-balanced to a different replica fails with "Session not found" — the
# 2-replica session flap. stateless_http=True makes each request self-contained.
mcp = ShizuhaMCPServer("shizuha-connect", stateless_http=True)

# Advertise OAuth 2.0 discovery metadata so MCP clients can discover the auth server.
from base import _register_oauth_discovery_routes as _reg_oauth_routes
_reg_oauth_routes(mcp, "connect")
base = ShizuhaMCPBase("connect", 8019, api_url=CONNECT_API_URL, jwt_token=CONNECT_JWT_TOKEN)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_profile(p: dict) -> str:
    """Format a profile dict into markdown."""
    output = f"# {p.get('display_name', p.get('username'))}\n\n"
    output += f"**Username**: @{p.get('username')}\n"
    if p.get('id'):
        output += f"**ID**: {p.get('id')}\n"
    if p.get('headline'):
        output += f"**Headline**: {p['headline']}\n"
    if p.get('bio'):
        output += f"**Bio**: {p['bio']}\n"
    if p.get('location'):
        output += f"**Location**: {p['location']}\n"
    if p.get('website'):
        output += f"**Website**: {p['website']}\n"
    output += f"\n**Followers**: {p.get('followers_count', 0)}\n"
    output += f"**Following**: {p.get('following_count', 0)}\n"
    output += f"**Posts**: {p.get('posts_count', 0)}\n"
    output += f"**Connections**: {p.get('connections_count', 0)}\n"
    return output


def _preview(content: str, limit: int = 280) -> str:
    """CON-91: a feed preview must never cut mid-word with no indicator. Trim to a
    word boundary within `limit` and append an explicit truncation marker so the
    reader knows the content was clipped and can open the post (its ID is shown on
    the same line) for the full text. Non-truncated content is returned as-is."""
    content = (content or '').strip()
    if len(content) <= limit:
        return content
    head = content[:limit].rstrip()
    sp = head.rfind(' ')
    if sp > limit * 0.6:  # prefer a word boundary, but not a too-short preview
        head = head[:sp].rstrip()
    remaining = len(content) - len(head)
    return f"{head}\u2026 [+{remaining} more chars \u2014 open the post for full content]"


def _fmt_post(post: dict, *, brief: bool = False) -> str:
    """Format a single post into markdown."""
    # CON-35: author_username is the canonical field (added to PostSerializer);
    # fall back through older field names then author_email before 'Unknown'.
    author = (post.get('author_username') or post.get('author_name')
              or post.get('author') or post.get('author_email') or 'Unknown')
    if brief:
        content_preview = _preview(post.get('content', ''))
        line = f"- **@{author}** ({post.get('created_at', '')}): {content_preview}\n"
        line += f"  Reactions: {post.get('reactions_count', 0)} | Comments: {post.get('comments_count', 0)} | Reposts: {post.get('reposts_count', 0)} | ID: {post.get('id')}\n"
        return line

    output = f"# Post by @{author}\n\n"
    output += f"**Created**: {post.get('created_at')}\n"
    output += f"**Visibility**: {post.get('visibility', 'public')}\n\n"
    output += f"---\n\n{post.get('content', '')}\n\n---\n\n"
    output += f"Reactions: {post.get('reactions_count', 0)} | Comments: {post.get('comments_count', 0)} | Reposts: {post.get('reposts_count', 0)}\n"
    if post.get('hashtags'):
        tags = " ".join([f"#{h}" for h in post['hashtags']])
        output += f"\n**Hashtags**: {tags}\n"
    output += f"\n**ID**: {post.get('id')}\n"
    return output


def _fmt_post_list(posts: list, title: str) -> str:
    """Format a list of posts."""
    if not posts:
        return "No posts found."
    output = f"# {title} ({len(posts)} posts)\n\n"
    for post in posts:
        output += f"---\n{_fmt_post(post, brief=True)}\n"
    return output








def _extract_results(result: dict) -> list:
    """Extract results list from paginated or raw API response."""
    return (result if isinstance(result, list) else result.get("results", []))


def _trending_count(t: dict) -> int:
    """CON-69: resolve a trending-topic item's post count across response shapes.

    The TrendingTopicSerializer exposes `post_count`; older/alternate shapes used
    `posts_count` or `count`. Reading only the latter two made every normal
    serializer response render `(0 posts)`. Try `post_count` first."""
    return t.get('post_count', t.get('posts_count', t.get('count', 0)))


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

@mcp.tool(name="connect_get_my_profile")
def get_my_profile() -> str:
    """Get the current user's social profile including bio, stats, and headline."""
    result = base.api_request("GET", "/me/")
    if "error" in result:
        return base.format_error(result)
    return _fmt_profile(result)


@mcp.tool(name="connect_update_my_profile")
def update_my_profile(
    headline: Optional[str] = None,
    bio: Optional[str] = None,
    location: Optional[str] = None,
    website: Optional[str] = None,
) -> str:
    """
    Update the current user's profile.

    Args:
        headline: Professional headline
        bio: Profile biography
        location: Location string
        website: Personal website URL
    """
    data = {}
    if headline is not None:
        data["headline"] = headline
    if bio is not None:
        data["bio"] = bio
    if location is not None:
        data["location"] = location
    if website is not None:
        data["website"] = website
    if not data:
        return "Error: No fields provided to update."

    result = base.api_request("PATCH", "/me/", data=data)
    if "error" in result:
        return base.format_error(result)
    return "Profile updated.\n\n" + _fmt_profile(result)


@mcp.tool(name="connect_get_my_posts")
def get_my_posts(limit: int = 20) -> str:
    """
    List the current user's posts.

    Args:
        limit: Maximum number of posts to return
    """
    result = base.api_request("GET", "/me/posts/", params={"page_size": limit})
    if "error" in result:
        return base.format_error(result)
    return _fmt_post_list(_extract_results(result), "My Posts")


@mcp.tool(name="connect_get_my_bookmarks")
def get_my_bookmarks(limit: int = 20) -> str:
    """
    List bookmarked posts.

    Args:
        limit: Maximum number of bookmarks to return
    """
    result = base.api_request("GET", "/me/bookmarks/", params={"page_size": limit})
    if "error" in result:
        return base.format_error(result)
    # CON-262: /me/bookmarks/ returns Bookmark rows whose source post is nested
    # under ``post`` (BookmarkSerializer). Format the SOURCE post — its id,
    # content, author and timestamp — not the bookmark's own id/created_at,
    # which previously rendered as a ghost row (@Unknown, empty content).
    items = _extract_results(result)
    posts = [
        (item.get('post') or item) if isinstance(item, dict) else item
        for item in items
    ]
    return _fmt_post_list(posts, "Bookmarked Posts")


@mcp.tool(name="connect_get_profile")
def get_profile(username: str) -> str:
    """
    Get a user's social profile by username.

    Args:
        username: Username (without @)
    """
    result = base.api_request("GET", f"/profiles/{username}/")
    if "error" in result:
        return base.format_error(result)
    return _fmt_profile(result)


@mcp.tool(name="connect_get_profile_posts")
def get_profile_posts(username: str, limit: int = 20) -> str:
    """
    Get a user's posts.

    Args:
        username: Username
        limit: Maximum number of posts
    """
    result = base.api_request("GET", f"/profiles/{username}/posts/", params={"page_size": limit})
    if "error" in result:
        # CON-342: unknown user must be a clear typed 404, not an ambiguous
        # empty list or a raw HTML error page.
        if result.get("status_code") == 404:
            return f"User '@{username}' not found (HTTP 404)."
        return base.format_error(result)
    return _fmt_post_list(_extract_results(result), f"Posts by @{username}")


@mcp.tool(name="connect_get_profile_followers")
def get_profile_followers(username: str, limit: int = 50) -> str:
    """
    List a user's followers.

    Args:
        username: Username
        limit: Maximum results
    """
    result = base.api_request("GET", f"/profiles/{username}/followers/", params={"page_size": limit})
    if "error" in result:
        return base.format_error(result)
    users = _extract_results(result)
    if not users:
        return f"@{username} has no followers."
    output = f"# Followers of @{username} ({len(users)})\n\n"
    for u in users:
        output += f"- **{u.get('display_name', u.get('username'))}** (@{u.get('username')})"
        if u.get('headline'):
            output += f" -- {u['headline']}"
        output += "\n"
    return output


@mcp.tool(name="connect_get_profile_following")
def get_profile_following(username: str, limit: int = 50) -> str:
    """
    List who a user follows.

    Args:
        username: Username
        limit: Maximum results
    """
    result = base.api_request("GET", f"/profiles/{username}/following/", params={"page_size": limit})
    if "error" in result:
        return base.format_error(result)
    users = _extract_results(result)
    if not users:
        return f"@{username} is not following anyone."
    output = f"# @{username} is following ({len(users)})\n\n"
    for u in users:
        output += f"- **{u.get('display_name', u.get('username'))}** (@{u.get('username')})"
        if u.get('headline'):
            output += f" -- {u['headline']}"
        output += "\n"
    return output


@mcp.tool(name="connect_get_profile_skills")
def get_profile_skills(username: str) -> str:
    """
    Get a user's skills.

    Args:
        username: Username
    """
    result = base.api_request("GET", f"/profiles/{username}/skills/")
    if "error" in result:
        return base.format_error(result)
    skills = _extract_results(result)
    if not skills:
        return f"@{username} has no listed skills."
    output = f"# Skills -- @{username}\n\n"
    for s in skills:
        name = s.get('name', s.get('skill', ''))
        endorsements = s.get('endorsements_count', 0)
        output += f"- **{name}** ({endorsements} endorsements)\n"
    return output


@mcp.tool(name="connect_get_profile_experience")
def get_profile_experience(username: str) -> str:
    """
    Get a user's work experience.

    Args:
        username: Username
    """
    result = base.api_request("GET", f"/profiles/{username}/experience/")
    if "error" in result:
        return base.format_error(result)
    entries = _extract_results(result)
    if not entries:
        return f"@{username} has no listed experience."
    output = f"# Experience -- @{username}\n\n"
    for e in entries:
        output += f"## {e.get('title')} at {e.get('company', 'N/A')}\n"
        output += f"{e.get('start_date', '?')} - {e.get('end_date', 'Present')}\n"
        if e.get('description'):
            output += f"{e['description']}\n"
        output += "\n"
    return output


@mcp.tool(name="connect_get_profile_education")
def get_profile_education(username: str) -> str:
    """
    Get a user's education history.

    Args:
        username: Username
    """
    result = base.api_request("GET", f"/profiles/{username}/education/")
    if "error" in result:
        return base.format_error(result)
    entries = _extract_results(result)
    if not entries:
        return f"@{username} has no listed education."
    output = f"# Education -- @{username}\n\n"
    for e in entries:
        output += f"## {e.get('degree', '')} -- {e.get('school', e.get('institution', 'N/A'))}\n"
        output += f"{e.get('start_date', '?')} - {e.get('end_date', 'Present')}\n"
        if e.get('field_of_study'):
            output += f"**Field**: {e['field_of_study']}\n"
        if e.get('description'):
            output += f"{e['description']}\n"
        output += "\n"
    return output


# ---------------------------------------------------------------------------
# Skills (global)
# ---------------------------------------------------------------------------

@mcp.tool(name="connect_list_skills")
def list_skills(search: Optional[str] = None) -> str:
    """
    List all available skills on the platform.

    Args:
        search: Optional search filter
    """
    params = {}
    if search:
        params["search"] = search
    result = base.api_request("GET", "/skills/", params=params)
    if "error" in result:
        return base.format_error(result)
    skills = _extract_results(result)
    if not skills:
        return "No skills found."
    output = "# Available Skills\n\n"
    for s in skills:
        output += f"- {s.get('name', s.get('id', ''))}\n"
    return output


# ---------------------------------------------------------------------------
# Experience (own)
# ---------------------------------------------------------------------------

@mcp.tool(name="connect_list_experience")
def list_experience() -> str:
    """List the current user's work experience entries."""
    result = base.api_request("GET", "/experience/")
    if "error" in result:
        return base.format_error(result)
    entries = _extract_results(result)
    if not entries:
        return "No experience entries."
    output = "# My Experience\n\n"
    for e in entries:
        output += f"## {e.get('title')} at {e.get('company', 'N/A')}\n"
        output += f"{e.get('start_date', '?')} - {e.get('end_date', 'Present')}\n"
        if e.get('description'):
            output += f"{e['description']}\n"
        output += f"ID: {e.get('id')}\n\n"
    return output


@mcp.tool(name="connect_create_experience")
def create_experience(
    title: str,
    company: str,
    start_date: str,
    end_date: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    is_current: bool = False,
) -> str:
    """
    Add a work experience entry.

    Args:
        title: Job title
        company: Company name
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD), omit if current
        description: Description of responsibilities
        location: Work location
        is_current: Whether this is the current position
    """
    data = {"title": title, "company_name": company, "start_date": start_date, "is_current": is_current}
    if end_date:
        data["end_date"] = end_date
    if description:
        data["description"] = description
    if location:
        data["location"] = location

    result = base.api_request("POST", "/experience/", data=data)
    if "error" in result:
        return base.format_error(result)
    return f"Experience added.\n\n**ID**: {result.get('id')}\n**Title**: {result.get('title')} at {result.get('company_name')}\n"


@mcp.tool(name="connect_update_experience")
def update_experience(
    experience_id: str,
    title: Optional[str] = None,
    company: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
    is_current: Optional[bool] = None,
) -> str:
    """
    Update a work experience entry.

    Args:
        experience_id: Experience entry ID
        title: Job title
        company: Company name
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        description: Description
        location: Location
        is_current: Whether current position
    """
    data = {}
    if title is not None:
        data["title"] = title
    if company is not None:
        data["company_name"] = company
    if start_date is not None:
        data["start_date"] = start_date
    if end_date is not None:
        data["end_date"] = end_date
    if description is not None:
        data["description"] = description
    if location is not None:
        data["location"] = location
    if is_current is not None:
        data["is_current"] = is_current
    if not data:
        return "Error: No fields provided to update."

    result = base.api_request("PATCH", f"/experience/{experience_id}/", data=data)
    if "error" in result:
        return base.format_error(result)
    return f"Experience updated.\n\n**ID**: {result.get('id')}\n**Title**: {result.get('title')} at {result.get('company_name')}\n"


@mcp.tool(name="connect_delete_experience")
def delete_experience(experience_id: str) -> str:
    """
    Delete a work experience entry.

    Args:
        experience_id: Experience entry ID
    """
    result = base.api_request("DELETE", f"/experience/{experience_id}/")
    if "error" in result:
        return base.format_error(result)
    return f"Experience entry {experience_id} deleted."


# ---------------------------------------------------------------------------
# Education (own)
# ---------------------------------------------------------------------------

@mcp.tool(name="connect_list_education")
def list_education() -> str:
    """List the current user's education entries."""
    result = base.api_request("GET", "/education/")
    if "error" in result:
        return base.format_error(result)
    entries = _extract_results(result)
    if not entries:
        return "No education entries."
    output = "# My Education\n\n"
    for e in entries:
        output += f"## {e.get('degree', '')} -- {e.get('school', e.get('institution', 'N/A'))}\n"
        output += f"{e.get('start_date', '?')} - {e.get('end_date', 'Present')}\n"
        if e.get('field_of_study'):
            output += f"**Field**: {e['field_of_study']}\n"
        output += f"ID: {e.get('id')}\n\n"
    return output


@mcp.tool(name="connect_create_education")
def create_education(
    school: str,
    degree: str,
    start_date: str,
    end_date: Optional[str] = None,
    field_of_study: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """
    Add an education entry.

    Args:
        school: School or institution name
        degree: Degree obtained or pursuing
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD), omit if current
        field_of_study: Field or major
        description: Additional details
    """
    data = {"school_name": school, "degree": degree, "start_date": start_date}
    if end_date:
        data["end_date"] = end_date
    if field_of_study:
        data["field_of_study"] = field_of_study
    if description:
        data["description"] = description

    result = base.api_request("POST", "/education/", data=data)
    if "error" in result:
        return base.format_error(result)
    return f"Education added.\n\n**ID**: {result.get('id')}\n**Degree**: {result.get('degree')} at {result.get('school_name')}\n"


@mcp.tool(name="connect_update_education")
def update_education(
    education_id: str,
    school: Optional[str] = None,
    degree: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    field_of_study: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """
    Update an education entry.

    Args:
        education_id: Education entry ID
        school: School or institution
        degree: Degree
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        field_of_study: Field or major
        description: Additional details
    """
    data = {}
    if school is not None:
        data["school_name"] = school
    if degree is not None:
        data["degree"] = degree
    if start_date is not None:
        data["start_date"] = start_date
    if end_date is not None:
        data["end_date"] = end_date
    if field_of_study is not None:
        data["field_of_study"] = field_of_study
    if description is not None:
        data["description"] = description
    if not data:
        return "Error: No fields provided to update."

    result = base.api_request("PATCH", f"/education/{education_id}/", data=data)
    if "error" in result:
        return base.format_error(result)
    return f"Education updated.\n\n**ID**: {result.get('id')}\n**Degree**: {result.get('degree')} at {result.get('school_name')}\n"


@mcp.tool(name="connect_delete_education")
def delete_education(education_id: str) -> str:
    """
    Delete an education entry.

    Args:
        education_id: Education entry ID
    """
    result = base.api_request("DELETE", f"/education/{education_id}/")
    if "error" in result:
        return base.format_error(result)
    return f"Education entry {education_id} deleted."


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------

@mcp.tool(name="connect_list_posts")
def list_posts(
    feed_type: str = "home",
    limit: int = 20,
) -> str:
    """
    List feed posts.

    Args:
        feed_type: Feed type -- home, following, or trending
        limit: Maximum posts to return
    """
    result = base.api_request("GET", f"/feed/{feed_type}/", params={"page_size": limit})
    if "error" in result:
        return base.format_error(result)
    posts = _extract_results(result)
    return _fmt_post_list(posts, f"{feed_type.title()} Feed")


@mcp.tool(name="connect_create_post")
def create_post(
    content: str,
    visibility: str = "public",
    hashtags: Optional[str] = None,
) -> str:
    """
    Create a new post.

    Args:
        content: Post content (text body)
        visibility: Visibility level -- public, followers, connections, or mentioned
        hashtags: Comma-separated hashtags (without # prefix)
    """
    data = {"content": content, "visibility": visibility}
    if hashtags:
        data["hashtags"] = [h.strip().lstrip('#') for h in hashtags.split(",")]

    result = base.api_request("POST", "/posts/", data=data)
    if "error" in result:
        return base.format_error(result)
    return f"Post created!\n\n**ID**: {result.get('id')}\n**Content**: {_preview(result.get('content') or '', 200)}\n"


@mcp.tool(name="connect_get_post")
def get_post(post_id: str) -> str:
    """
    Get a post with full details.

    Args:
        post_id: Post ID
    """
    result = base.api_request("GET", f"/posts/{post_id}/")
    if "error" in result:
        return base.format_error(result)
    return _fmt_post(result)


@mcp.tool(name="connect_update_post")
def update_post(
    post_id: str,
    content: Optional[str] = None,
    visibility: Optional[str] = None,
) -> str:
    """
    Update an existing post.

    Args:
        post_id: Post ID
        content: New content
        visibility: New visibility (public, followers, connections, or mentioned)
    """
    data = {}
    if content is not None:
        data["content"] = content
    if visibility is not None:
        data["visibility"] = visibility
    if not data:
        return "Error: No fields provided to update."

    result = base.api_request("PATCH", f"/posts/{post_id}/", data=data)
    if "error" in result:
        return base.format_error(result)
    return f"Post updated.\n\n**ID**: {result.get('id')}\n"


@mcp.tool(name="connect_delete_post")
def delete_post(post_id: str) -> str:
    """
    Delete a post.

    Args:
        post_id: Post ID
    """
    result = base.api_request("DELETE", f"/posts/{post_id}/")
    if "error" in result:
        return base.format_error(result)
    return f"Post {post_id} deleted."


@mcp.tool(name="connect_react_to_post")
def react_to_post(post_id: str, reaction_type: str = "like") -> str:
    """
    React to a post.

    Args:
        post_id: Post ID
        reaction_type: Reaction type -- like, love, celebrate, support, insightful
    """
    result = base.api_request("POST", f"/posts/{post_id}/react/", data={"reaction_type": reaction_type})
    if "error" in result:
        return base.format_error(result)
    return f"Reacted to post {post_id} with '{reaction_type}'."


@mcp.tool(name="connect_unreact_to_post")
def unreact_to_post(post_id: str) -> str:
    """
    Remove reaction from a post.

    Args:
        post_id: Post ID
    """
    result = base.api_request("POST", f"/posts/{post_id}/unreact/")
    if "error" in result:
        return base.format_error(result)
    return f"Removed reaction from post {post_id}."


@mcp.tool(name="connect_repost")
def repost(post_id: str) -> str:
    """
    Repost/share a post to your feed.

    Args:
        post_id: Post ID to repost
    """
    result = base.api_request("POST", f"/posts/{post_id}/repost/")
    if "error" in result:
        return base.format_error(result)
    return f"Reposted post {post_id}.\n\n**New Post ID**: {result.get('id', 'N/A')}\n"


@mcp.tool(name="connect_quote_post")
def quote_post(post_id: str, content: str) -> str:
    """
    Quote a post with your own commentary.

    Args:
        post_id: Post ID to quote
        content: Your commentary text
    """
    result = base.api_request("POST", f"/posts/{post_id}/quote/", data={"content": content})
    if "error" in result:
        return base.format_error(result)
    return f"Quoted post {post_id}.\n\n**New Post ID**: {result.get('id', 'N/A')}\n"


@mcp.tool(name="connect_bookmark_post")
def bookmark_post(post_id: str) -> str:
    """
    Bookmark a post for later reading.

    Args:
        post_id: Post ID
    """
    result = base.api_request("POST", f"/posts/{post_id}/bookmark/")
    if "error" in result:
        return base.format_error(result)
    return f"Bookmarked post {post_id}."


@mcp.tool(name="connect_unbookmark_post")
def unbookmark_post(post_id: str) -> str:
    """
    Remove a bookmark from a post.

    Args:
        post_id: Post ID
    """
    result = base.api_request("POST", f"/posts/{post_id}/unbookmark/")
    if "error" in result:
        return base.format_error(result)
    return f"Removed bookmark from post {post_id}."


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

@mcp.tool(name="connect_list_comments")
def list_comments(post_id: str, limit: int = 50) -> str:
    """
    List comments on a post.

    Args:
        post_id: Post ID
        limit: Maximum number of comments
    """
    result = base.api_request("GET", f"/posts/{post_id}/comments/", params={"page_size": limit})
    if "error" in result:
        return base.format_error(result)
    comments = _extract_results(result)
    if not comments:
        return "No comments on this post."
    output = f"# Comments on Post {post_id} ({len(comments)})\n\n"
    for c in comments:
        author = c.get('author_name', c.get('author', 'Unknown'))
        output += f"- **@{author}** ({c.get('created_at', '')}): {c.get('content', '')}\n"
        output += f"  Likes: {c.get('likes_count', 0)} | Replies: {c.get('replies_count', 0)} | ID: {c.get('id')}\n"
    return output


@mcp.tool(name="connect_add_comment")
def add_comment(post_id: str, content: str) -> str:
    """
    Add a comment to a post.

    Args:
        post_id: Post ID
        content: Comment text
    """
    result = base.api_request("POST", f"/posts/{post_id}/comments/", data={"content": content})
    if "error" in result:
        return base.format_error(result)
    return f"Comment added!\n\n**ID**: {result.get('id')}\n"


@mcp.tool(name="connect_list_replies")
def list_replies(comment_id: str, limit: int = 50) -> str:
    """
    List replies to a comment.

    Args:
        comment_id: Parent comment ID
        limit: Maximum number of replies
    """
    result = base.api_request("GET", f"/comments/{comment_id}/replies/", params={"page_size": limit})
    if "error" in result:
        return base.format_error(result)
    replies = _extract_results(result)
    if not replies:
        return "No replies to this comment."
    output = f"# Replies to Comment {comment_id} ({len(replies)})\n\n"
    for r in replies:
        author = r.get('author_name', r.get('author', 'Unknown'))
        output += f"- **@{author}** ({r.get('created_at', '')}): {r.get('content', '')}\n"
        output += f"  Likes: {r.get('likes_count', 0)} | ID: {r.get('id')}\n"
    return output


@mcp.tool(name="connect_add_reply")
def add_reply(comment_id: str, content: str) -> str:
    """
    Reply to a comment.

    Args:
        comment_id: Comment ID to reply to
        content: Reply text
    """
    result = base.api_request("POST", f"/comments/{comment_id}/replies/", data={"content": content})
    if "error" in result:
        return base.format_error(result)
    return f"Reply added!\n\n**ID**: {result.get('id')}\n"


@mcp.tool(name="connect_like_comment")
def like_comment(comment_id: str) -> str:
    """
    Like a comment.

    Args:
        comment_id: Comment ID
    """
    result = base.api_request("POST", f"/comments/{comment_id}/like/")
    if "error" in result:
        return base.format_error(result)
    return f"Liked comment {comment_id}."


@mcp.tool(name="connect_unlike_comment")
def unlike_comment(comment_id: str) -> str:
    """
    Remove like from a comment.

    Args:
        comment_id: Comment ID
    """
    result = base.api_request("POST", f"/comments/{comment_id}/unlike/")
    if "error" in result:
        return base.format_error(result)
    return f"Removed like from comment {comment_id}."


# ---------------------------------------------------------------------------
# Social Graph
# ---------------------------------------------------------------------------

@mcp.tool(name="connect_follow_user")
def follow_user(user_id: str) -> str:
    """
    Follow a user.

    Args:
        user_id: User ID to follow
    """
    result = base.api_request("POST", f"/follow/{user_id}/")
    if "error" in result:
        return base.format_error(result)
    return f"Now following user {user_id}."


@mcp.tool(name="connect_unfollow_user")
def unfollow_user(user_id: str) -> str:
    """
    Unfollow a user.

    Args:
        user_id: User ID to unfollow
    """
    result = base.api_request("POST", f"/unfollow/{user_id}/")
    if "error" in result:
        return base.format_error(result)
    return f"Unfollowed user {user_id}."


@mcp.tool(name="connect_list_connections")
def list_connections(limit: int = 50) -> str:
    """
    List accepted connections.

    Args:
        limit: Maximum results
    """
    result = base.api_request("GET", "/connections/", params={"page_size": limit})
    if "error" in result:
        return base.format_error(result)
    conns = _extract_results(result)
    if not conns:
        return "No connections."
    output = f"# My Connections ({len(conns)})\n\n"
    for c in conns:
        user = c.get('user', c)
        output += f"- **{user.get('display_name', user.get('username', 'Unknown'))}** (@{user.get('username', '')})\n"
    return output


@mcp.tool(name="connect_list_connection_requests")
def list_connection_requests(limit: int = 50) -> str:
    """
    List pending connection requests (received).

    Args:
        limit: Maximum results
    """
    result = base.api_request("GET", "/connections/requests/", params={"page_size": limit})
    if "error" in result:
        return base.format_error(result)
    reqs = _extract_results(result)
    if not reqs:
        return "No pending connection requests."
    output = f"# Pending Connection Requests ({len(reqs)})\n\n"
    for r in reqs:
        sender = r.get('sender', r.get('from_user', {}))
        if isinstance(sender, dict) and sender:
            name = sender.get('display_name', sender.get('username', 'Unknown'))
            username = sender.get('username', '')
        else:
            # CON-261: the API serializes the requester as flat fields
            # (requester_name / requester_username), not a nested sender object.
            name = r.get('requester_name') or r.get('sender_name') or 'Unknown'
            username = r.get('requester_username') or r.get('sender_username') or ''
        output += f"- From **{name}** (@{username})\n"
        if r.get('message'):
            output += f"  Message: {r['message']}\n"
        output += f"  ID: {r.get('id')}\n"
    return output


@mcp.tool(name="connect_send_connection_request")
def send_connection_request(user_id: str, message: Optional[str] = None) -> str:
    """
    Send a connection request to a user.

    Args:
        user_id: User ID to connect with
        message: Optional personalized message
    """
    data = {}
    if message:
        data["message"] = message
    result = base.api_request("POST", f"/connections/request/{user_id}/", data=data if data else None)
    if "error" in result:
        return base.format_error(result)
    return f"Connection request sent to user {user_id}."


@mcp.tool(name="connect_accept_connection")
def accept_connection(request_id: str) -> str:
    """
    Accept a connection request.

    Args:
        request_id: Connection request ID
    """
    result = base.api_request("POST", f"/connections/{request_id}/accept/")
    if "error" in result:
        return base.format_error(result)
    return f"Connection request {request_id} accepted."


@mcp.tool(name="connect_reject_connection")
def reject_connection(request_id: str) -> str:
    """
    Reject a connection request.

    Args:
        request_id: Connection request ID
    """
    result = base.api_request("POST", f"/connections/{request_id}/reject/")
    if "error" in result:
        return base.format_error(result)
    return f"Connection request {request_id} rejected."


@mcp.tool(name="connect_block_user")
def block_user(user_id: str) -> str:
    """
    Block a user.

    Args:
        user_id: User ID to block
    """
    result = base.api_request("POST", f"/block/{user_id}/")
    if "error" in result:
        return base.format_error(result)
    return f"Blocked user {user_id}."


@mcp.tool(name="connect_unblock_user")
def unblock_user(user_id: str) -> str:
    """
    Unblock a user.

    Args:
        user_id: User ID to unblock
    """
    result = base.api_request("POST", f"/unblock/{user_id}/")
    if "error" in result:
        return base.format_error(result)
    return f"Unblocked user {user_id}."


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@mcp.tool(name="connect_search")
def search_global(query: str, limit: int = 20) -> str:
    """
    Search core social content (posts and people).

    Args:
        query: Search query string
        limit: Maximum results per type
    """
    result = base.api_request("GET", "/search/", params={"q": query, "page_size": limit})
    if "error" in result:
        return base.format_error(result)

    output = f"# Search Results: '{query}'\n\n"
    for section in ("posts", "people"):
        items = result.get(section, [])
        if items:
            output += f"## {section.title()} ({len(items)})\n\n"
            for item in items:
                output += f"- {item.get('title', item.get('display_name', _preview(item.get('content', ''), 100)))}\n"
                output += f"  ID: {item.get('id')}\n"
                # CON-342: people results must expose the canonical username so
                # an agent can resolve a person to their profile posts.
                if section == "people" and item.get("username"):
                    output += f"  Username: @{item['username']}\n"
            output += "\n"

    if output.strip() == f"# Search Results: '{query}'":
        return f"No results found for '{query}'."
    return output


@mcp.tool(name="connect_search_posts")
def search_posts(query: str, limit: int = 20) -> str:
    """
    Search posts.

    Args:
        query: Search query
        limit: Maximum results
    """
    result = base.api_request("GET", "/search/posts/", params={"q": query, "page_size": limit})
    if "error" in result:
        return base.format_error(result)
    posts = _extract_results(result)
    if not posts:
        return f"No posts found matching '{query}'."
    output = f"# Post Search: '{query}' ({len(posts)})\n\n"
    for post in posts:
        output += _fmt_post(post, brief=True)
    return output


@mcp.tool(name="connect_search_people")
def search_people(query: str, limit: int = 20) -> str:
    """
    Search for users/people.

    Args:
        query: Search query (name, headline, skills)
        limit: Maximum results
    """
    result = base.api_request("GET", "/search/people/", params={"q": query, "page_size": limit})
    if "error" in result:
        return base.format_error(result)
    users = _extract_results(result)
    if not users:
        return f"No users found matching '{query}'."
    output = f"# People: '{query}' ({len(users)})\n\n"
    for user in users:
        output += f"- **{user.get('display_name', user.get('username'))}** (@{user.get('username')})\n"
        output += f"  ID: {user.get('id')}\n"
        if user.get('headline'):
            output += f"  {user['headline']}\n"
    return output






@mcp.tool(name="connect_trending_hashtags")
def trending_hashtags(limit: int = 20) -> str:
    """
    Get trending hashtags on the platform.

    Args:
        limit: Maximum number of hashtags to return
    """
    result = base.api_request("GET", "/hashtags/trending/", params={"page_size": limit})
    if "error" in result:
        return base.format_error(result)
    tags = _extract_results(result)
    if not tags:
        return "No trending hashtags."
    output = "# Trending Hashtags\n\n"
    for i, t in enumerate(tags, 1):
        name = t.get('name', t.get('hashtag', ''))
        count = _trending_count(t)  # CON-69: read post_count (serializer field) first
        output += f"{i}. **#{name}** ({count} posts)\n"
    return output


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------















# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------















# ---------------------------------------------------------------------------
# Applications (own)
# ---------------------------------------------------------------------------









# ---------------------------------------------------------------------------
# Conversations / Messaging
# ---------------------------------------------------------------------------

@mcp.tool(name="connect_list_conversations")
def list_conversations(limit: int = 20) -> str:
    """
    List messaging conversations.

    Args:
        limit: Maximum conversations to return
    """
    result = base.api_request("GET", "/conversations/", params={"page_size": limit})
    if "error" in result:
        return base.format_error(result)
    convos = _extract_results(result)
    if not convos:
        return "No conversations."
    output = f"# Conversations ({len(convos)})\n\n"
    for c in convos:
        # CON-52: prefer the server-computed participant_names, which already
        # falls back to the SocialProfile display_name/username for agents (whose
        # raw participant user_name is blank). Only re-derive from raw participant
        # fields if the server didn't provide names.
        names_list = c.get('participant_names') or []
        if names_list:
            names = ", ".join(str(n) for n in names_list[:4] if n)
        else:
            participants = c.get('participants', [])
            names = ", ".join(
                p.get('user_name') or p.get('nickname') or p.get('user_email') or f"User {p.get('user_id', '?')}"
                for p in participants[:4]
            )
        # `title` is blank for direct conversations; `or names` (not the dict.get
        # default) ensures we fall back to participant names even when the key is
        # present-but-empty.
        output += f"- **{c.get('title') or names}**\n"
        if c.get('last_message'):
            msg = c['last_message']
            output += f"  Last: {_preview(msg.get('content', ''), 80)} ({msg.get('created_at', '')})\n"
        output += f"  Unread: {c.get('unread_count', 0)} | ID: {c.get('id')}\n"
    return output


@mcp.tool(name="connect_create_conversation")
def create_conversation(
    participant_ids: str,
    title: Optional[str] = None,
    initial_message: Optional[str] = None,
    conversation_type: Optional[str] = None,
) -> str:
    """
    Start a new messaging conversation (direct OR group).

    Args:
        participant_ids: Comma-separated user IDs to include (do NOT include
            yourself — you're added automatically; admin for groups)
        title: Conversation name. REQUIRED for groups (the group's display name).
        initial_message: Optional first message to send
        conversation_type: 'direct' or 'group'. If omitted, defaults to 'group'
            when more than one participant is given, else 'direct' — the server
            rejects a multi-participant 'direct' conversation.
    """
    ids = []
    for pid in participant_ids.split(","):
        pid = pid.strip()
        if not pid:
            continue
        try:
            ids.append(int(pid))
        except ValueError:
            return f"Error: participant_ids must be integers; got {pid!r}."
    # Dedupe (order-preserving) — duplicate ids would double-add participants /
    # trip the (conversation, user_id) uniqueness on ConversationParticipant.
    ids = list(dict.fromkeys(ids))
    if not ids:
        return "Error: at least one participant_id is required."

    ctype = conversation_type or ("group" if len(ids) > 1 else "direct")
    data = {"conversation_type": ctype, "participant_ids": ids}
    # The serializer field is `name` (not `title`); groups require it.
    if ctype == "group":
        data["name"] = (title or "").strip() or "Group conversation"
    elif title:
        data["name"] = title

    result = base.api_request("POST", "/conversations/", data=data)
    if "error" in result:
        return base.format_error(result)

    conv_id = result.get("id")
    # `initial_message` is not a create-endpoint field — post it to the new
    # conversation's messages endpoint after creation.
    msg_note = ""
    if initial_message and conv_id:
        msg = base.api_request(
            "POST", f"/conversations/{conv_id}/messages/",
            data={"content": initial_message},
        )
        if "error" in msg:
            msg_note = f"\n(warning: conversation created but initial message failed: {base.format_error(msg)})"
    return f"Conversation created!\n\n**Type**: {ctype}\n**ID**: {conv_id}{msg_note}\n"


@mcp.tool(name="connect_create_group_conversation")
def create_group_conversation(
    title: str,
    participant_ids: str,
    initial_message: Optional[str] = None,
) -> str:
    """
    Create a GROUP conversation (you + 2 or more others) in Connect.

    Use this for standups, team syncs, and any multi-person chat.
    `connect_create_conversation` is 1:1 ONLY — it hits the direct path and the
    server rejects multiple participants ("Direct messages must have exactly one
    other participant"). This sends `conversation_type='group'` + a name, which
    is what the server requires for a group.

    You are added automatically as the group admin — do NOT include your own
    user id in participant_ids. Resolve usernames to ids with the Shizuha ID
    records if needed (CON-2).

    Args:
        title: group name (REQUIRED for groups)
        participant_ids: comma-separated Shizuha user IDs, e.g. "13,14,17"
        initial_message: optional first message posted to the new group
    """
    if not title or not title.strip():
        return "Error: title is required for a group conversation."

    ids = []
    for pid in participant_ids.split(","):
        pid = pid.strip()
        if not pid:
            continue
        try:
            ids.append(int(pid))
        except ValueError:
            return f"Error: participant_ids must be integers; got {pid!r}."
    # Dedupe (order-preserving) — duplicate ids would double-add participants /
    # trip the (conversation, user_id) uniqueness on ConversationParticipant.
    ids = list(dict.fromkeys(ids))
    if not ids:
        return "Error: at least one participant_id is required."

    data = {
        "conversation_type": "group",
        "name": title.strip(),
        "participant_ids": ids,
    }
    result = base.api_request("POST", "/conversations/", data=data)
    if "error" in result:
        return base.format_error(result)

    conv_id = result.get("id")
    msg_note = ""
    if initial_message and conv_id:
        msg = base.api_request(
            "POST", f"/conversations/{conv_id}/messages/",
            data={"content": initial_message},
        )
        msg_note = ("\n(initial message sent)" if "error" not in msg
                    else f"\n(warning: group created but initial message failed: {base.format_error(msg)})")

    return (
        f"Group conversation created!\n\n**Name**: {title.strip()}\n"
        f"**ID**: {conv_id}\n**Participants**: {len(ids)} (+ you as admin){msg_note}\n"
    )


@mcp.tool(name="connect_get_conversation")
def get_conversation(conversation_id: str) -> str:
    """
    Get conversation details.

    Args:
        conversation_id: Conversation ID
    """
    result = base.api_request("GET", f"/conversations/{conversation_id}/")
    if "error" in result:
        return base.format_error(result)
    c = result
    participants = c.get('participants', [])
    # CON-303: ConversationParticipantSerializer exposes user_name (and
    # user_email), not display_name/username — reading the wrong keys rendered
    # every participant blank.
    names = ", ".join(
        (p.get('user_name') or p.get('display_name') or p.get('username') or p.get('user_email') or '')
        for p in participants
    )
    output = f"# Conversation: {c.get('title', c.get('name', 'Direct Message'))}\n\n"
    output += f"**Participants**: {names}\n"
    output += f"**Created**: {c.get('created_at', 'N/A')}\n"
    output += f"**Unread**: {c.get('unread_count', 0)}\n"
    output += f"**ID**: {c.get('id')}\n"
    return output


@mcp.tool(name="connect_list_messages")
def list_messages(conversation_id: str, limit: int = 50) -> str:
    """
    List messages in a conversation.

    Args:
        conversation_id: Conversation ID
        limit: Maximum messages to return
    """
    # CON-38: messages endpoint uses {"messages": [...]} not {"results": [...]}
    # Also view reads ?limit=, not ?page_size=
    result = base.api_request("GET", f"/conversations/{conversation_id}/messages/", params={"limit": limit})
    if "error" in result:
        return base.format_error(result)
    messages = result.get("messages", [])
    if not messages:
        return "No messages in this conversation."
    output = f"# Messages ({len(messages)})\n\n"
    for m in messages:
        sender = m.get('sender_name', m.get('sender', 'Unknown'))
        output += f"**@{sender}** ({m.get('created_at', '')}):\n{m.get('content', '')}\n\n"
    return output


@mcp.tool(name="connect_send_message")
def send_message(conversation_id: str, content: str) -> str:
    """
    Send a message in a conversation.

    Args:
        conversation_id: Conversation ID
        content: Message text
    """
    result = base.api_request("POST", f"/conversations/{conversation_id}/messages/", data={"content": content})
    if "error" in result:
        return base.format_error(result)
    return f"Message sent!\n\n**ID**: {result.get('id')}\n"


@mcp.tool(name="connect_list_sent_messages")
def list_sent_messages(
    limit: int = 20,
    recipient_username: Optional[str] = None,
    since: Optional[str] = None,
) -> str:
    """
    List your OWN recently sent direct messages (outbox), newest-first. Use this
    to check whether you already sent an equivalent message before sending again
    — avoids duplicate sends across heartbeats or session restarts (e.g. replying
    to the same standup ping multiple times).

    Args:
        limit: Max messages to return (default 20, max 100)
        recipient_username: Only messages you sent to this username
        since: ISO-8601 timestamp; only messages sent at/after this time
    """
    params: Dict[str, Any] = {"limit": limit}
    if recipient_username:
        params["recipient_username"] = recipient_username
    if since:
        params["since"] = since
    result = base.api_request("GET", "/messaging/sent/", params=params)
    if "error" in result:
        return base.format_error(result)
    msgs = result.get("messages", [])
    if not msgs:
        return "No sent messages found."
    output = f"# Your sent messages ({len(msgs)}, newest first)\n\n"
    for m in msgs:
        who = (
            m.get("recipient_username")
            or m.get("recipient_name")
            or f"user {m.get('recipient_user_id')}"
        )
        output += (
            f"- **to {who}** at {m.get('sent_at')}\n"
            f"  {m.get('content_preview', '')}\n"
            f"  (conversation {m.get('conversation_id')}, message {m.get('message_id')})\n"
        )
    return output


@mcp.tool(name="connect_mark_conversation_read")
def mark_conversation_read(conversation_id: str) -> str:
    """
    Mark all messages in a conversation as read.

    Args:
        conversation_id: Conversation ID
    """
    result = base.api_request("POST", f"/conversations/{conversation_id}/read/")
    if "error" in result:
        return base.format_error(result)
    return f"Conversation {conversation_id} marked as read."


@mcp.tool(name="message_user")
def message_user(
    content: str,
    recipient_username: Optional[str] = None,
    recipient_email: Optional[str] = None,
    client_message_id: Optional[str] = None,
) -> str:
    """
    Send a direct message to any user on the platform. THE primary
    mechanism for replying and reaching out — the only way to deliver a
    message to any user, human or agent.

    On Shizuha Connect, natural text output from your LLM turn is your
    private reasoning and is NEVER delivered to anyone. If you want the
    sender of a message (or any other user) to see something, you MUST
    call this tool. There is no auto-relay of turn output. The skill
    `connect-messaging` explains why.

    Addressing: pass EITHER `recipient_username` (the simple case —
    looked up in shizuha-id) OR `recipient_email` (for email addresses
    you already have). Usernames are the recommended form — they're
    shorter, always valid, and work identically for humans and agents.

    Args:
        content: The message text. Supports markdown. Be concise and
            self-contained — assume the recipient has no context about
            why you're messaging them.
        recipient_username: The recipient's Shizuha ID username (e.g.
            "hritik", "kai", "akira"). Looked up via the `id` service.
            Preferred when the username is what you have — which is the
            default, since inbound messages arrive prefixed with the
            sender's username: `[kai] can you review AT-91?`.
        recipient_email: Alternative to `recipient_username`. Use when
            you already have the email (e.g. "user@example.com",
            "kai@agents.shizuha.io"). Exactly one of username or email
            must be provided.
        client_message_id: Optional UUID idempotency key. If the same
            key is posted twice (e.g. retry after a network hiccup), the
            second call returns the original message without creating
            a duplicate. Omit and the tool generates one for you.

    Examples:
        # Reply to the inbound username with the actual answer.
        # Do not send a one-word "pong" (docs token, not a live ping).
        message_user(
            recipient_username="<inbound-username>",
            content="<the actual reply they should see>",
        )

        # Delegate to another agent by username
        message_user(
            recipient_username="akira",
            content="Please run a security scan on shizuha-id.",
        )

        # Status update to a human with a specific email you were given
        message_user(
            recipient_email="user@example.com",
            content="AT-62 verification complete. Summary attached.",
        )
    """
    if not content or not content.strip():
        return "Error: content is required."
    try:
        from connect.views.messaging import is_ack_only_message
        if is_ack_only_message(content):
            return (
                "Error: content must be the words the recipient should see. "
                "Do not send pong / Test message / stuck-in-a-loop liveness spam."
            )
    except Exception:
        pass
    username = (recipient_username or "").strip()
    email = (recipient_email or "").strip()
    if not username and not email:
        return (
            "Error: provide either `recipient_username` (e.g. 'hritik') or "
            "`recipient_email` (e.g. 'kai@agents.shizuha.io'). Usernames are "
            "the preferred form."
        )

    payload: Dict[str, Any] = {
        "content": content.strip(),
    }
    if username:
        payload["recipient_username"] = username
    if email:
        payload["recipient_email"] = email
    if client_message_id:
        payload["client_message_id"] = client_message_id
    else:
        # Always provide idempotency so accidental retries of the same tool
        # call (e.g. rate-limit retry, network blip) don't produce duplicate
        # messages in the recipient's inbox.
        import uuid as _uuid
        payload["client_message_id"] = str(_uuid.uuid4())

    result = base.api_request("POST", "/messaging/dm/", data=payload)
    if "error" in result:
        return base.format_error(result)

    recipient = result.get("recipient") or {}
    conv_id = result.get("conversation_id", "?")
    msg_id = result.get("message_id", "?")
    idempotent = result.get("idempotent_replay", False)
    created_new = result.get("created_new_conversation", False)

    label = recipient.get("name") or recipient.get("email") or username or email
    parts = [
        f"Message sent to {label}.",
        f"  conversation_id: {conv_id}",
        f"  message_id:      {msg_id}",
    ]
    if created_new:
        parts.append("  (started a new conversation)")
    if idempotent:
        parts.append("  (idempotent replay — no duplicate created)")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Stories
# ---------------------------------------------------------------------------

@mcp.tool(name="connect_list_stories")
def list_stories() -> str:
    """List stories from your connections and followed users."""
    result = base.api_request("GET", "/stories/")
    if "error" in result:
        return base.format_error(result)
    stories = _extract_results(result)
    if not stories:
        return "No stories available."
    output = f"# Stories ({len(stories)})\n\n"
    for s in stories:
        author = s.get('author_name', s.get('author', 'Unknown'))
        output += f"- **@{author}**: {_preview(s.get('content', s.get('caption', '')), 100)}\n"
        output += f"  Views: {s.get('views_count', 0)} | Reactions: {s.get('reactions_count', 0)} | ID: {s.get('id')}\n"
    return output


@mcp.tool(name="connect_create_story")
def create_story(
    content: Optional[str] = None,
    caption: Optional[str] = None,
    background_color: Optional[str] = None,
) -> str:
    """
    Create a story (visible for 24 hours).

    Args:
        content: Story text content
        caption: Caption for the story
        background_color: Background color hex code (e.g., #FF5733)
    """
    text_content = content or caption
    data = {"story_type": "text"}
    if text_content:
        data["text_content"] = text_content
    if background_color:
        data["background_color"] = background_color

    result = base.api_request("POST", "/stories/", data=data)
    if "error" in result:
        return base.format_error(result)
    return f"Story created!\n\n**ID**: {result.get('id')}\n"


@mcp.tool(name="connect_get_story")
def get_story(story_id: str) -> str:
    """
    Get story details.

    Args:
        story_id: Story ID
    """
    result = base.api_request("GET", f"/stories/{story_id}/")
    if "error" in result:
        return base.format_error(result)
    s = result
    author = s.get('author_name', s.get('author', 'Unknown'))
    output = f"# Story by @{author}\n\n"
    output += f"**Content**: {s.get('content', s.get('caption', ''))}\n"
    output += f"**Created**: {s.get('created_at', 'N/A')}\n"
    output += f"**Expires**: {s.get('expires_at', 'N/A')}\n"
    output += f"**Views**: {s.get('views_count', 0)}\n"
    output += f"**Reactions**: {s.get('reactions_count', 0)}\n"
    output += f"**ID**: {s.get('id')}\n"
    return output


@mcp.tool(name="connect_view_story")
def view_story(story_id: str) -> str:
    """
    Mark a story as viewed.

    Args:
        story_id: Story ID
    """
    result = base.api_request("POST", f"/stories/{story_id}/view/")
    if "error" in result:
        return base.format_error(result)
    return f"Story {story_id} viewed."


@mcp.tool(name="connect_react_to_story")
def react_to_story(story_id: str, reaction_type: str = "like") -> str:
    """
    React to a story.

    Args:
        story_id: Story ID
        reaction_type: Reaction type (like, love, wow, haha)
    """
    result = base.api_request("POST", f"/stories/{story_id}/react/", data={"reaction_type": reaction_type})
    if "error" in result:
        return base.format_error(result)
    return f"Reacted to story {story_id} with '{reaction_type}'."


@mcp.tool(name="connect_get_my_stories")
def get_my_stories() -> str:
    """Get the current user's active stories."""
    result = base.api_request("GET", "/stories/my_stories/")
    if "error" in result:
        return base.format_error(result)
    stories = _extract_results(result)
    if not stories:
        return "You have no active stories."
    output = f"# My Stories ({len(stories)})\n\n"
    for s in stories:
        output += f"- {_preview(s.get('content', s.get('caption', '')), 100)}\n"
        output += f"  Views: {s.get('views_count', 0)} | Reactions: {s.get('reactions_count', 0)} | ID: {s.get('id')}\n"
    return output


# ---------------------------------------------------------------------------
# Meetings
# ---------------------------------------------------------------------------

@mcp.tool(name="connect_list_meetings")
def list_meetings(limit: int = 20) -> str:
    """
    List scheduled meetings.

    Args:
        limit: Maximum meetings to return
    """
    result = base.api_request("GET", "/meetings/", params={"page_size": limit})
    if "error" in result:
        return base.format_error(result)
    meetings = _extract_results(result)
    if not meetings:
        return "No meetings scheduled."
    output = f"# Meetings ({len(meetings)})\n\n"
    for m in meetings:
        output += f"## {m.get('title', 'Untitled Meeting')}\n"
        output += f"**When**: {m.get('start_time', 'N/A')} - {m.get('end_time', 'N/A')}\n"
        output += f"**Status**: {m.get('status', 'N/A')}\n"
        if m.get('join_code'):
            output += f"**Code**: {m['join_code']}\n"
        output += f"**ID**: {m.get('id')}\n\n"
    return output


@mcp.tool(name="connect_create_meeting")
def create_meeting(
    title: str,
    start_time: str,
    end_time: str,
    description: Optional[str] = None,
    participant_ids: Optional[str] = None,
) -> str:
    """
    Schedule a new meeting.

    Args:
        title: Meeting title
        start_time: Start time (ISO 8601, e.g., 2026-03-15T10:00:00Z)
        end_time: End time (ISO 8601)
        description: Meeting description/agenda
        participant_ids: Comma-separated user IDs to invite
    """
    data = {"title": title, "start_time": start_time, "end_time": end_time}
    if description:
        data["description"] = description
    if participant_ids:
        data["participant_ids"] = [pid.strip() for pid in participant_ids.split(",")]

    result = base.api_request("POST", "/meetings/", data=data)
    if "error" in result:
        return base.format_error(result)
    output = f"Meeting scheduled!\n\n"
    output += f"**ID**: {result.get('id')}\n"
    output += f"**Title**: {result.get('title')}\n"
    if result.get('join_code'):
        output += f"**Join Code**: {result.get('join_code')}\n"
    return output


@mcp.tool(name="connect_get_meeting")
def get_meeting(meeting_id: str) -> str:
    """
    Get meeting details.

    Args:
        meeting_id: Meeting ID
    """
    result = base.api_request("GET", f"/meetings/{meeting_id}/")
    if "error" in result:
        return base.format_error(result)
    m = result
    output = f"# {m.get('title', 'Meeting')}\n\n"
    # The API serializer exposes scheduled_start/scheduled_end (not
    # start_time/end_time) — reading the wrong keys rendered Start/End as N/A.
    output += f"**Start**: {m.get('scheduled_start') or m.get('start_time', 'N/A')}\n"
    output += f"**End**: {m.get('scheduled_end') or m.get('end_time', 'N/A')}\n"
    output += f"**Status**: {m.get('status', 'N/A')}\n"
    if m.get('description'):
        output += f"\n{m['description']}\n"
    if m.get('join_code'):
        output += f"\n**Join Code**: {m['join_code']}\n"
    if m.get('join_url'):
        output += f"**Join URL**: {m['join_url']}\n"
    output += f"\n**ID**: {m.get('id')}\n"
    return output


@mcp.tool(name="connect_create_instant_meeting")
def create_instant_meeting(title: Optional[str] = None) -> str:
    """
    Start an instant meeting (no scheduling needed).

    Args:
        title: Optional meeting title
    """
    data = {}
    if title:
        data["title"] = title

    result = base.api_request("POST", "/meetings/instant/", data=data if data else None)
    if "error" in result:
        return base.format_error(result)
    output = f"Instant meeting started!\n\n"
    output += f"**ID**: {result.get('id')}\n"
    if result.get('join_code'):
        output += f"**Join Code**: {result.get('join_code')}\n"
    if result.get('join_url'):
        output += f"**Join URL**: {result.get('join_url')}\n"
    return output


@mcp.tool(name="connect_start_meeting")
def start_meeting(meeting_id: str) -> str:
    """
    Start a scheduled meeting.

    Args:
        meeting_id: Meeting ID
    """
    result = base.api_request("POST", f"/meetings/{meeting_id}/start/")
    if "error" in result:
        return base.format_error(result)
    return f"Meeting {meeting_id} started."


@mcp.tool(name="connect_end_meeting")
def end_meeting(meeting_id: str) -> str:
    """
    End an active meeting.

    Args:
        meeting_id: Meeting ID
    """
    result = base.api_request("POST", f"/meetings/{meeting_id}/end/")
    if "error" in result:
        return base.format_error(result)
    return f"Meeting {meeting_id} ended."


@mcp.tool(name="connect_join_meeting")
def join_meeting(meeting_id: str) -> str:
    """
    Join a meeting by its ID.

    Args:
        meeting_id: Meeting ID
    """
    result = base.api_request("POST", f"/meetings/{meeting_id}/join/")
    if "error" in result:
        return base.format_error(result)
    output = f"Joined meeting {meeting_id}.\n"
    if result.get('join_url'):
        output += f"**Join URL**: {result.get('join_url')}\n"
    return output


@mcp.tool(name="connect_join_meeting_by_code")
def join_meeting_by_code(code: str) -> str:
    """
    Join a meeting using a join code.

    Args:
        code: Meeting join code
    """
    result = base.api_request("POST", "/meetings/join-by-code/", data={"code": code})
    if "error" in result:
        return base.format_error(result)
    output = f"Joined meeting via code '{code}'.\n"
    output += f"**Meeting ID**: {result.get('id', 'N/A')}\n"
    if result.get('join_url'):
        output += f"**Join URL**: {result.get('join_url')}\n"
    return output


@mcp.tool(name="connect_list_meeting_participants")
def list_meeting_participants(meeting_id: str) -> str:
    """
    List participants of a meeting.

    Args:
        meeting_id: Meeting ID
    """
    result = base.api_request("GET", f"/meetings/{meeting_id}/participants/")
    if "error" in result:
        return base.format_error(result)
    participants = _extract_results(result)
    if not participants:
        return "No participants."
    output = f"# Meeting Participants ({len(participants)})\n\n"
    for p in participants:
        user = p.get('user', p)
        status = p.get('status', 'invited')
        output += f"- **{user.get('display_name', user.get('username', 'Unknown'))}** -- {status}\n"
    return output


@mcp.tool(name="connect_invite_to_meeting")
def invite_to_meeting(meeting_id: str, emails: str) -> str:
    """
    Invite users to a meeting.

    Args:
        meeting_id: Meeting ID
        emails: Comma-separated email addresses to invite
    """
    data = {"emails": [email.strip() for email in emails.split(",") if email.strip()]}
    if not data["emails"]:
        return "Error: at least one email is required."
    result = base.api_request("POST", f"/meetings/{meeting_id}/invite/", data=data)
    if "error" in result:
        return base.format_error(result)
    return f"Invited {len(data['emails'])} user(s) to meeting {meeting_id}."


# ---------------------------------------------------------------------------
# Cross-service Sharing
# ---------------------------------------------------------------------------


def _missing_share_target_error(
    *,
    target_arg: str,
    service_label: str,
    legacy_content_type: Optional[str] = None,
    legacy_content_id: Optional[str] = None,
) -> str:
    """Return a clear tool-side error when the required share target is absent.

    The Connect API's `/share/*/` endpoints create a Connect feed post for an
    existing object from another Shizuha app.  The previous MCP adapter exposed
    `content_type`/`content_id` instead, so every call hit the backend's raw
    `<target>_id is required` 400.  Keep those legacy args accepted for a
    helpful compatibility error, but do not forward an impossible payload.
    """
    legacy_hint = ""
    if legacy_content_type or legacy_content_id:
        legacy_hint = (
            "\n\n`content_type`/`content_id` identify Connect content and are "
            "not accepted by this Connect feed-share endpoint. Create or pick "
            f"the {service_label} object first, then pass `{target_arg}`."
        )
    return (
        "# Error (invalid input)\n\n"
        f"**Message**: `{target_arg}` is required to share an existing "
        f"{service_label} object into the Connect feed.{legacy_hint}\n"
    )


def _share_success(
    result: dict,
    *,
    service_label: str,
    target_label: str,
    target_id: str,
) -> str:
    post_id = result.get("id", "N/A")
    return (
        f"Shared {service_label} to the Connect feed.\n\n"
        f"**Post ID**: {post_id}\n"
        f"**{target_label}**: {target_id}\n"
    )


@mcp.tool(name="connect_share_to_drive")
def share_to_drive(
    file_id: Optional[str] = None,
    content_type: Optional[str] = None,
    content_id: Optional[str] = None,
    folder_id: Optional[str] = None,
    message: Optional[str] = None,
) -> str:
    """
    Share an existing Shizuha Drive file into the Connect feed.

    Args:
        file_id: Drive file ID to share into Connect
        content_type: Legacy arg; not used by this endpoint
        content_id: Legacy arg; not used by this endpoint
        folder_id: Deprecated legacy arg; not used by this endpoint
        message: Optional annotation
    """
    if not file_id:
        return _missing_share_target_error(
            target_arg="file_id",
            service_label="Drive file",
            legacy_content_type=content_type,
            legacy_content_id=content_id,
        )

    data = {"file_id": file_id}
    if message:
        data["message"] = message

    result = base.api_request("POST", "/share/drive/", data=data)
    if "error" in result:
        return base.format_error(result)
    return _share_success(
        result,
        service_label="Drive file",
        target_label="File ID",
        target_id=file_id,
    )


@mcp.tool(name="connect_share_to_pulse")
def share_to_pulse(
    task_id: Optional[str] = None,
    content_type: Optional[str] = None,
    content_id: Optional[str] = None,
    project_id: Optional[str] = None,
    task_title: Optional[str] = None,
    message: Optional[str] = None,
) -> str:
    """
    Share an existing Shizuha Pulse task into the Connect feed.

    Args:
        task_id: Pulse task ID/key to share into Connect
        content_type: Legacy arg; not used by this endpoint
        content_id: Legacy arg; not used by this endpoint
        project_id: Deprecated legacy arg; not used by this endpoint
        task_title: Deprecated legacy arg; not used by this endpoint
        message: Optional context
    """
    if not task_id:
        return _missing_share_target_error(
            target_arg="task_id",
            service_label="Pulse task",
            legacy_content_type=content_type,
            legacy_content_id=content_id,
        )

    data = {"task_id": task_id}
    if message:
        data["message"] = message

    result = base.api_request("POST", "/share/pulse/", data=data)
    if "error" in result:
        return base.format_error(result)
    return _share_success(
        result,
        service_label="Pulse task",
        target_label="Task ID",
        target_id=task_id,
    )


@mcp.tool(name="connect_share_to_wiki")
def share_to_wiki(
    page_id: Optional[str] = None,
    content_type: Optional[str] = None,
    content_id: Optional[str] = None,
    space_id: Optional[str] = None,
    page_title: Optional[str] = None,
    message: Optional[str] = None,
) -> str:
    """
    Share an existing Shizuha Wiki page into the Connect feed.

    Args:
        page_id: Wiki page ID to share into Connect
        content_type: Legacy arg; not used by this endpoint
        content_id: Legacy arg; not used by this endpoint
        space_id: Deprecated legacy arg; not used by this endpoint
        page_title: Deprecated legacy arg; not used by this endpoint
        message: Optional annotation
    """
    if not page_id:
        return _missing_share_target_error(
            target_arg="page_id",
            service_label="Wiki page",
            legacy_content_type=content_type,
            legacy_content_id=content_id,
        )

    data = {"page_id": page_id}
    if message:
        data["message"] = message

    result = base.api_request("POST", "/share/wiki/", data=data)
    if "error" in result:
        return base.format_error(result)
    return _share_success(
        result,
        service_label="Wiki page",
        target_label="Page ID",
        target_id=page_id,
    )


@mcp.tool(name="connect_share_to_notes")
def share_to_notes(
    note_id: Optional[str] = None,
    content_type: Optional[str] = None,
    content_id: Optional[str] = None,
    notebook_id: Optional[str] = None,
    note_title: Optional[str] = None,
    message: Optional[str] = None,
) -> str:
    """
    Share an existing Shizuha Note into the Connect feed.

    Args:
        note_id: Notes note ID to share into Connect
        content_type: Legacy arg; not used by this endpoint
        content_id: Legacy arg; not used by this endpoint
        notebook_id: Deprecated legacy arg; not used by this endpoint
        note_title: Deprecated legacy arg; not used by this endpoint
        message: Optional annotation
    """
    if not note_id:
        return _missing_share_target_error(
            target_arg="note_id",
            service_label="Notes note",
            legacy_content_type=content_type,
            legacy_content_id=content_id,
        )

    data = {"note_id": note_id}
    if message:
        data["message"] = message

    result = base.api_request("POST", "/share/notes/", data=data)
    if "error" in result:
        return base.format_error(result)
    return _share_success(
        result,
        service_label="Notes note",
        target_label="Note ID",
        target_id=note_id,
    )


# ---------------------------------------------------------------------------
# Server Entry Point
# ---------------------------------------------------------------------------

def main():
    """Run the MCP server."""
    run_mcp_server(mcp, base)


if __name__ == "__main__":
    main()
