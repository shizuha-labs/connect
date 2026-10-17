"""Ranked search services for Connect.

CON-57: centralize post search so universal and typed post endpoints share
PostgreSQL FTS retrieval and deterministic relevance ranking.
CON-58: centralize people/profile search so the universal and typed people
endpoints share exact/prefix-aware retrieval and ranking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import (
    Case, ExpressionWrapper, F, FloatField, Q, QuerySet, Value, When,
)
from django.db.models.functions import Cast, Least, Length, Ln
from django.utils import timezone

from connect.models import Post, SocialProfile

TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
MAX_QUERY_TOKENS = 8


@dataclass(frozen=True)
class PeopleSearchQuery:
    """Parsed query and PostgreSQL FTS query objects for people search."""

    raw_query: str
    username_query: str
    tokens: tuple[str, ...]
    strict_query: SearchQuery
    broad_query: SearchQuery | None


@dataclass(frozen=True)
class PostSearchQuery:
    """Parsed query and PostgreSQL FTS query objects for post search."""

    raw_query: str
    tokens: tuple[str, ...]
    strict_query: SearchQuery
    broad_query: SearchQuery | None


def tokenize_search_query(query: str, *, max_tokens: int = MAX_QUERY_TOKENS) -> tuple[str, ...]:
    """Return normalized FTS tokens used by the broad any-token fallback."""

    seen: set[str] = set()
    tokens: list[str] = []
    for match in TOKEN_RE.finditer((query or "").lower()):
        token = match.group(0).strip("'@#-")
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= max_tokens:
            break
    return tuple(tokens)


def normalize_people_query(query: str) -> str:
    """Normalize a people-search query for username exact/prefix matching."""

    return (query or "").strip().lower().lstrip("@")


def build_people_search_query(query: str) -> PeopleSearchQuery:
    """Build the strict FTS query plus any-token fallback metadata."""

    raw_query = (query or "").strip()
    tokens = tokenize_search_query(raw_query)
    strict_query = SearchQuery(raw_query, search_type="websearch")
    broad_query = None
    if len(tokens) > 1:
        broad_query = SearchQuery(" | ".join(tokens), search_type="raw")
    return PeopleSearchQuery(
        raw_query=raw_query,
        username_query=normalize_people_query(raw_query),
        tokens=tokens,
        strict_query=strict_query,
        broad_query=broad_query,
    )


def build_post_search_query(query: str) -> PostSearchQuery:
    """Build the strict websearch query and controlled any-token fallback."""

    raw_query = (query or "").strip()
    tokens = tokenize_search_query(raw_query)
    strict_query = SearchQuery(raw_query, search_type="websearch")
    broad_query = None
    if len(tokens) > 1:
        broad_query = SearchQuery(" | ".join(tokens), search_type="raw")
    return PostSearchQuery(
        raw_query=raw_query,
        tokens=tokens,
        strict_query=strict_query,
        broad_query=broad_query,
    )


def ranked_people_search(
    query: str,
    *,
    limit: int | None = 20,
    location: str | None = None,
    industry: str | None = None,
) -> QuerySet[SocialProfile]:
    """Return profiles ordered by exact/prefix, FTS, social proof, and activity.

    Prefix matches are included in the candidate set so short queries such as
    `sco` can surface `@scout` even before FTS would match the full username.
    """

    parsed = build_people_search_query(query)
    if not parsed.raw_query:
        return SocialProfile.objects.none()

    candidate_filter = (
        Q(search_vector=parsed.strict_query)
        | Q(username__iexact=parsed.username_query)
        | Q(username__istartswith=parsed.username_query)
        | Q(display_name__istartswith=parsed.raw_query)
    )
    if parsed.broad_query is not None:
        candidate_filter |= Q(search_vector=parsed.broad_query)

    queryset = SocialProfile.objects.filter(candidate_filter)

    # Privacy hook: preserve current endpoint behaviour until the CON-18
    # block/privacy matrix lands; wire profile_visibility filtering there.
    if location:
        queryset = queryset.filter(location__icontains=location)
    if industry:
        queryset = queryset.filter(industry=industry)

    now = timezone.now()
    broad_rank = (
        SearchRank(F("search_vector"), parsed.broad_query, normalization=32)
        if parsed.broad_query is not None
        else Value(0.0)
    )
    social_raw = ExpressionWrapper(
        Value(1.0)
        + Cast(F("followers_count"), FloatField())
        + Cast(F("connections_count"), FloatField()),
        output_field=FloatField(),
    )

    queryset = queryset.annotate(
        exact_username=Case(
            When(username__iexact=parsed.username_query, then=Value(1.0)),
            default=Value(0.0),
            output_field=FloatField(),
        ),
        prefix_username=Case(
            When(username__istartswith=parsed.username_query, then=Value(1.0)),
            default=Value(0.0),
            output_field=FloatField(),
        ),
        # Prefix TIGHTNESS: how much of the candidate username the query covers
        # (len(query)/len(username), 1.0 == exact). Typeahead intent is to reach
        # the closest handle, so `sco` should surface `scout` (3/5) above the
        # looser `scoria` (3/6) even when the looser match has more followers —
        # only a hugely-popular looser match can still outweigh a tighter one.
        prefix_tightness=Case(
            When(
                username__istartswith=parsed.username_query,
                then=ExpressionWrapper(
                    Value(float(len(parsed.username_query)))
                    / Cast(Least(Length('username'), Value(64)), FloatField()),
                    output_field=FloatField(),
                ),
            ),
            default=Value(0.0),
            output_field=FloatField(),
        ),
        prefix_display_name=Case(
            When(display_name__istartswith=parsed.raw_query, then=Value(1.0)),
            default=Value(0.0),
            output_field=FloatField(),
        ),
        strict_rank=SearchRank(F("search_vector"), parsed.strict_query, normalization=32),
        broad_rank=broad_rank,
        social_score=ExpressionWrapper(Ln(social_raw) / Value(8.0), output_field=FloatField()),
        active_recency=Case(
            When(last_active_at__gte=now - timedelta(days=7), then=Value(1.0)),
            When(last_active_at__gte=now - timedelta(days=30), then=Value(0.5)),
            default=Value(0.0),
            output_field=FloatField(),
        ),
    ).annotate(
        people_score=ExpressionWrapper(
            F("exact_username") * Value(2.50)
            + F("prefix_username") * Value(1.75)
            + F("prefix_tightness") * Value(1.75)
            + F("prefix_display_name") * Value(1.25)
            + F("strict_rank") * Value(1.0)
            + F("broad_rank") * Value(0.35)
            + F("social_score") * Value(0.15)
            + F("active_recency") * Value(0.10),
            output_field=FloatField(),
        )
    ).order_by("-people_score", "-exact_username", "username", "id").distinct()

    if limit is not None:
        return queryset[:limit]
    return queryset


def ranked_post_search(
    query: str,
    *,
    limit: int | None = 20,
    author: str | int | None = None,
    hashtag: str | None = None,
) -> QuerySet[Post]:
    """Return public posts ordered by lexical relevance plus recency/engagement.

    Retrieval is strict websearch OR broad any-token fallback for natural
    multi-word queries. Lexical rank dominates; recency and engagement only
    break ties among similarly relevant posts.
    """

    parsed = build_post_search_query(query)
    if not parsed.raw_query:
        return Post.objects.none()

    candidate_filter = Q(search_vector=parsed.strict_query)
    if parsed.broad_query is not None:
        candidate_filter |= Q(search_vector=parsed.broad_query)

    queryset = Post.objects.filter(visibility="public").filter(candidate_filter)

    if author:
        queryset = queryset.filter(author_id=author)

    if hashtag:
        queryset = queryset.filter(hashtag_links__hashtag__name__iexact=hashtag.strip().lstrip("#"))

    now = timezone.now()
    broad_rank = (
        SearchRank(F("search_vector"), parsed.broad_query, normalization=32)
        if parsed.broad_query is not None
        else Value(0.0)
    )
    engagement_raw = ExpressionWrapper(
        Value(1.0)
        + Cast(F("likes_count"), FloatField())
        + Cast(F("reposts_count"), FloatField()) * Value(2.0)
        + Cast(F("comments_count"), FloatField()) * Value(0.5)
        + Cast(F("quotes_count"), FloatField())
        + Cast(F("views_count"), FloatField()) * Value(0.05),
        output_field=FloatField(),
    )

    queryset = queryset.annotate(
        strict_rank=SearchRank(F("search_vector"), parsed.strict_query, normalization=32),
        broad_rank=broad_rank,
        recency_score=Case(
            When(created_at__gte=now - timedelta(hours=24), then=Value(1.0)),
            When(created_at__gte=now - timedelta(days=7), then=Value(0.7)),
            When(created_at__gte=now - timedelta(days=30), then=Value(0.35)),
            default=Value(0.1),
            output_field=FloatField(),
        ),
        engagement_score=Least(
            Value(1.0),
            ExpressionWrapper(Ln(engagement_raw) / Value(5.0), output_field=FloatField()),
        ),
    ).annotate(
        post_score=ExpressionWrapper(
            F("strict_rank") * Value(1.0)
            + F("broad_rank") * Value(0.35)
            + F("recency_score") * Value(0.20)
            + F("engagement_score") * Value(0.15),
            output_field=FloatField(),
        )
    ).order_by("-post_score", "-strict_rank", "-created_at", "id").distinct()

    if limit is not None:
        return queryset[:limit]
    return queryset
