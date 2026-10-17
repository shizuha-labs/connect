# CLAUDE.md - Shizuha Connect

## Overview

**Shizuha Connect** is a hybrid social networking service combining LinkedIn's professional networking with Twitter/X's real-time social feeds.

| Setting | Value |
|---------|-------|
| Backend Port | 8019 |
| Frontend Port | 5183 |
| Database | shizuha_connect |
| Redis DB | 8 |
| URL Path | `/connect/` |

## Access

| URL | Description |
|-----|-------------|
| `/connect/` | Social feed UI |
| `/connect/api/` | REST API |
| `/connect/admin/` | Django Admin |
| `/connect/ws/` | WebSocket (real-time feed) |

## API Endpoints

### Profile
```
GET/PATCH  /connect/api/profile/              # Current user profile
GET        /connect/api/profile/{username}/   # User by username
GET        /connect/api/profile/{username}/posts/ # User's posts
GET        /connect/api/me/                   # Current-user profile shortcut
GET        /connect/api/me/posts/             # Current user's posts
GET        /connect/api/me/bookmarks/         # Current user's bookmarks
```

### Posts
```
GET/POST   /connect/api/posts/                # List/create posts
GET/PATCH  /connect/api/posts/{id}/           # Post details
POST       /connect/api/posts/{id}/react/     # Add reaction
POST       /connect/api/posts/{id}/repost/    # Repost
GET/POST   /connect/api/posts/{id}/comments/  # Comments
```

### Feed
```
GET /connect/api/feed/home/                   # Algorithmic home feed
GET /connect/api/feed/following/              # Chronological feed
GET /connect/api/feed/trending/               # Trending posts
```

### Social
```
POST /connect/api/follow/{user_id}/           # Follow user
POST /connect/api/unfollow/{user_id}/         # Unfollow user
GET  /connect/api/connections/                # Connections list
GET  /connect/api/connections/requests/       # Incoming connection requests
POST /connect/api/connections/request/{user}/ # Send connection request
POST /connect/api/connections/{id}/accept/    # Accept connection
POST /connect/api/connections/{id}/reject/    # Reject connection
POST /connect/api/block/{user}/               # Block user
POST /connect/api/unblock/{user}/             # Unblock user
POST /connect/api/mute/{user}/                # Mute user
POST /connect/api/unmute/{user}/              # Unmute user
```

### External employment service

Jobs, applications and employers live in Guild. See `docs/core-boundary.md`.

### Messaging
```
GET/POST   /connect/api/conversations/        # Direct messages
GET/POST   /connect/api/conversations/{id}/messages/
POST       /connect/api/messaging/dm/         # Idempotent user/agent DM send
GET        /connect/api/messaging/sent/       # Current caller's DM outbox
POST       /connect/api/upload/               # Media upload
GET        /connect/api/presence/             # Presence state
GET        /connect/api/hive/agents/          # Owner-visible Hive agents
POST       /connect/api/hive/agents/conversation/ # Open an agent conversation
```

### Search and discovery
```
GET /connect/api/search/                      # Universal search
GET /connect/api/search/people/               # People search
GET /connect/api/search/posts/                # Post search
GET /connect/api/hashtags/trending/           # Trending hashtags
GET /connect/api/suggestions/                 # People suggestions
```

### Onboarding and identity
```
GET  /connect/api/onboarding/status/          # Onboarding status
POST /connect/api/onboarding/steps/{step}/complete/ # Complete a step
GET  /connect/api/onboarding/suggestions/     # Suggested connections
GET  /connect/api/users/by-username/{username}/uuid/ # Resolve canonical ID
```

### External channels and integrations
```
POST /connect/api/bridges/whatsapp/{account}/webhook/
POST /connect/api/bridges/outbound/send/
POST /connect/api/bridges/{provider}/{account}/delivery-callback/
POST /connect/api/share/drive/
POST /connect/api/share/pulse/
POST /connect/api/share/wiki/
POST /connect/api/share/notes/
```

### Internal service API
```
POST /connect/api/internal/conversations/{id}/agent-message/
POST /connect/api/internal/conversations/{id}/agent-typing/
POST /connect/api/internal/system-message/
POST /connect/api/internal/group-message/
POST /connect/api/internal/agents/telemetry/
GET  /connect/api/internal/agents/config/
```

## WebSocket Endpoints

```
ws://localhost/connect/ws/connect/           # Base Connect stream
ws://localhost/connect/ws/connect/feed/      # Real-time feed updates
ws://localhost/connect/ws/connect/chat/{id}/ # Conversation messages
ws://localhost/connect/ws/connect/user/      # Unified user/agent DM stream
ws://localhost/connect/ws/connect/agent/     # Compatibility alias for agents
```

## Key Models

- **SocialProfile** - Extended user profile with skills, experience
- **Post** - Social posts with media, hashtags, mentions
- **Follow/Connection** - Social graph
- **Reaction/Comment** - Engagement
- **Conversation/DirectMessage** - Messaging
- **ConversationParticipant** - Per-user read, archive, and clear state

## Features

- Twitter/X-style posts with reactions and reposts
- LinkedIn-style profiles with skills and experience
- Real-time feed via Django Channels
- Trending topics algorithm
- RS256/JWKS authentication with fail-closed token-revocation checks
- Tenant-scoped human and agent direct messaging with idempotent sends
- Cursor-based missed-message replay and outbound delivery audit state
- Hive personal-agent discovery and delivery
- External-channel webhooks, retries, and delivery callbacks
- Multi-architecture CI/CD on Shizuha Origin

## Commands

All development commands run from the `compose/` directory using the master docker-compose.yaml:

```bash
# Start service with frontend
cd compose && docker compose up -d shizuha-connect shizuha-connect-frontend

# Run migrations
cd compose && docker compose exec shizuha-connect python manage.py migrate

# Run tests
cd compose && docker compose exec shizuha-connect python manage.py test

# View logs
cd compose && docker compose logs -f shizuha-connect

```

## Project Structure

```
shizuha-connect/
├── connectmanager/       # Django project settings
├── connect/              # Main app
│   ├── models/           # Profile, Post, Social, Messaging
│   ├── views/            # DRF ViewSets
│   ├── consumers.py      # WebSocket consumers
│   └── tasks.py          # Celery tasks
└── frontend/             # React UI
```

Also reference [../CLAUDE.md](../CLAUDE.md) for platform-wide rules.
