import { Link, useNavigate, useParams } from 'react-router-dom'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { messagingApi } from '../services/api'
import { useAuth } from '../contexts/AuthContext'
import { ArrowDown, ArrowLeft, MessageSquare, Send } from 'lucide-react'
import { sanitizeMessagePreview } from '../utils/messagePreview'

const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-primary-500 dark:focus-visible:ring-primary-400 focus-visible:ring-offset-white dark:focus-visible:ring-offset-gray-950'

const KICKER =
  'font-mono text-[10px] font-medium uppercase tracking-[0.16em] text-gray-600 dark:text-gray-400'

function unwrapList(payload, key) {
  if (Array.isArray(payload)) return payload
  if (Array.isArray(payload?.results)) return payload.results
  if (Array.isArray(payload?.[key])) return payload[key]
  return []
}

function conversationLabel(conv) {
  if (conv.name) return conv.name
  if (Array.isArray(conv.participant_names) && conv.participant_names.length) {
    return conv.participant_names.join(', ')
  }
  const participants = Array.isArray(conv.participants) ? conv.participants : []
  const fallback = participants
    .map((p) => p.user_name || p.user_email || p.username || p.email || (p.user_id ? `User ${p.user_id}` : ''))
    .filter(Boolean)
  if (fallback.length) return fallback.join(', ')
  return conv.conversation_type === 'direct' ? 'Direct Message' : 'Conversation'
}

function messageAuthor(message) {
  return message.sender_name || message.sender_username || message.sender_email || 'Unknown sender'
}

function isOutbound(message, user) {
  if (!user) return false
  const uid = user.id ?? user.user_id
  const sid = message.sender_id ?? message.sender_user_id ?? message.user_id
  if (uid != null && sid != null && String(uid) === String(sid)) return true
  const email = (user.email || '').toLowerCase()
  const senderEmail = (message.sender_email || '').toLowerCase()
  if (email && senderEmail && email === senderEmail) return true
  const uname = (user.username || '').toLowerCase()
  const sname = (message.sender_username || '').toLowerCase()
  if (uname && sname && uname === sname) return true
  return Boolean(message.is_mine || message.is_outbound || message.from_me)
}

function formatTime(iso) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return ''
    return d.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return ''
  }
}

function useIsMobile(breakpointPx = 1024) {
  const [mobile, setMobile] = useState(() =>
    typeof window !== 'undefined' ? window.matchMedia(`(max-width: ${breakpointPx - 1}px)`).matches : false
  )
  useEffect(() => {
    const mq = window.matchMedia(`(max-width: ${breakpointPx - 1}px)`)
    const onChange = () => setMobile(mq.matches)
    onChange()
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [breakpointPx])
  return mobile
}

function ListSkeleton() {
  return (
    <div className="divide-y divide-gray-100 dark:divide-gray-800" data-testid="messages-list-loading" aria-busy="true">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-3 h-[68px] animate-pulse">
          <div className="w-10 h-10 rounded-full bg-gray-200 dark:bg-gray-700 shrink-0" />
          <div className="flex-1 min-w-0 space-y-2">
            <div className="h-3 w-1/3 rounded bg-gray-200 dark:bg-gray-700" />
            <div className="h-3 w-2/3 rounded bg-gray-100 dark:bg-gray-800" />
          </div>
        </div>
      ))}
    </div>
  )
}

function ThreadSkeleton() {
  return (
    <div className="space-y-3 p-1" data-testid="messages-thread-loading" aria-busy="true">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className={`max-w-[75%] h-14 rounded-2xl animate-pulse ${
            i % 2 === 0 ? 'bg-gray-100 dark:bg-gray-800 mr-auto' : 'bg-primary-50 dark:bg-primary-950/40 ml-auto'
          }`}
        />
      ))}
    </div>
  )
}

function Messages() {
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { user } = useAuth()
  const [draft, setDraft] = useState('')
  const [sendError, setSendError] = useState('')
  const isMobile = useIsMobile()
  const composerRef = useRef(null)
  const threadEndRef = useRef(null)
  // CON-418: jump-to-latest affordance state. The scroller ref measures the
  // distance from the live edge; the pill renders once the user is ≥200px
  // above it and hides at-bottom (standard chat UX, per the CON-237
  // checklist's own expectation).
  const scrollerRef = useRef(null)
  const [atBottom, setAtBottom] = useState(true)

  const measureAtBottom = () => {
    const el = scrollerRef.current
    if (!el) return
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight
    setAtBottom(distance < 200)
  }

  const jumpToLatest = () => {
    const el = scrollerRef.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }

  // CON-408 (P3): draft isolation per thread — switching conversations must
  // not carry the previous thread's draft (or its error state) over.
  useEffect(() => {
    setDraft('')
    setSendError('')
    if (composerRef.current) {
      composerRef.current.style.height = 'auto'
    }
  }, [conversationId])

  const {
    data: conversationsResponse,
    isLoading,
    isError: conversationsError,
    isFetching: conversationsFetching,
    refetch: refetchConversations,
    dataUpdatedAt: conversationsUpdatedAt,
  } = useQuery({
    queryKey: ['conversations'],
    queryFn: () => messagingApi.getConversations(),
  })

  const conversations = useMemo(
    () => unwrapList(conversationsResponse?.data, 'conversations').filter((conv) => {
      // Same recency rule as the API: never-messaged threads must not occupy
      // the inbox for any counterpart (hive, fleet, or human).
      const count = Number(conv.message_count || 0)
      return Boolean(conv.last_message_at) || count > 0
    }),
    [conversationsResponse]
  )
  // Preserve last good list through a failed refresh (fail-soft)
  const [cachedConversations, setCachedConversations] = useState([])
  useEffect(() => {
    if (conversations.length > 0 || conversationsUpdatedAt) {
      setCachedConversations(conversations)
    }
  }, [conversations, conversationsUpdatedAt])
  const displayConversations =
    conversationsError && cachedConversations.length > 0 ? cachedConversations : conversations
  const listDegraded = Boolean(conversationsError && cachedConversations.length > 0)

  const selectedConversation = displayConversations.find((conv) => String(conv.id) === String(conversationId))

  // Instant open from list row state (game-engine: never wait to show known data)
  const localThreadSeed = useMemo(() => {
    if (!selectedConversation?.last_message_preview && !selectedConversation?.last_message) return []
    const preview = selectedConversation.last_message_preview || selectedConversation.last_message?.content
    if (!preview) return []
    return [
      {
        id: `local-seed-${selectedConversation.id}`,
        content: preview,
        sender_name: conversationLabel(selectedConversation),
        created_at: selectedConversation.updated_at || selectedConversation.last_message_at,
        _localSeed: true,
      },
    ]
  }, [selectedConversation])

  const {
    data: messagesResponse,
    isLoading: messagesLoading,
    isError: messagesError,
    isFetching: messagesFetching,
    refetch: refetchMessages,
    dataUpdatedAt: messagesUpdatedAt,
  } = useQuery({
    queryKey: ['conversation-messages', conversationId],
    queryFn: () => messagingApi.getMessages(conversationId),
    enabled: Boolean(conversationId),
  })
  const messages = useMemo(
    () => unwrapList(messagesResponse?.data, 'messages'),
    [messagesResponse]
  )
  const [threadCache, setThreadCache] = useState(() => new Map())
  useEffect(() => {
    if (!conversationId) return
    if (messages.length > 0 || (messagesUpdatedAt && !messagesError)) {
      setThreadCache((prev) => {
        const next = new Map(prev)
        next.set(String(conversationId), messages)
        return next
      })
    }
  }, [conversationId, messages, messagesUpdatedAt, messagesError])

  const cachedForThread = conversationId ? threadCache.get(String(conversationId)) || [] : []
  const displayMessages =
    messages.length > 0
      ? messages
      : messagesError && cachedForThread.length > 0
        ? cachedForThread
        : localThreadSeed
  const threadDegraded = Boolean(messagesError && (cachedForThread.length > 0 || localThreadSeed.length > 0))
  const threadNeverSeen = Boolean(
    conversationId && messagesLoading && displayMessages.length === 0 && cachedForThread.length === 0
  )

  // CON-418: land at the latest when a thread opens; while the user has
  // scrolled up to re-read, new messages must not yank them back down —
  // the jump-to-latest pill is the way back to the live edge.
  useEffect(() => {
    setAtBottom(true)
    threadEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [conversationId])

  useEffect(() => {
    if (!atBottom) return
    threadEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [displayMessages.length, atBottom])

  const sendMessage = useMutation({
    mutationFn: ({ conversationId: id, content }) =>
      messagingApi.sendMessage(id, { content, message_type: 'text' }),
    onSuccess: () => {
      setSendError('')
      setDraft('')
      if (composerRef.current) {
        composerRef.current.style.height = 'auto'
      }
      queryClient.invalidateQueries({ queryKey: ['conversation-messages', conversationId] })
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
    },
    // CON-408: the error path must never be silent — surface a visible,
    // screen-reader-announced failure and keep the draft so nothing is lost.
    onError: () => {
      setSendError('Message not sent — check your connection and try again.')
    },
  })

  const submitMessage = (event) => {
    event?.preventDefault?.()
    const content = draft.trim()
    if (!content || !conversationId || sendMessage.isPending) return
    setSendError('')
    sendMessage.mutate({ conversationId, content })
  }

  const onComposerKeyDown = (event) => {
    // CON-516: Enter must dispatch the REST send. Clearing the draft only
    // happens in onSuccess, so a dropped send keeps the text. Shift+Enter
    // inserts a newline. Ctrl/Cmd+Enter remains a send alias.
    if (event.key !== 'Enter' || event.isComposing) return
    if (event.shiftKey) return
    event.preventDefault()
    submitMessage(event)
  }

  const onComposerInput = (event) => {
    setDraft(event.target.value)
    const el = event.target
    el.style.height = 'auto'
    const next = Math.min(Math.max(el.scrollHeight, 44), 160)
    el.style.height = `${next}px`
  }

  const showList = !isMobile || !conversationId
  const showThread = !isMobile || Boolean(conversationId)

  const onConversationKeyDown = (event, id) => {
    if (event.key === ' ' || event.key === 'Enter') {
      event.preventDefault()
      navigate(`/messages/${id}`)
    }
  }

  const conversationList = (
    <div
      className={`${isMobile ? 'w-full min-w-0' : 'w-[min(360px,34%)] shrink-0 border-r border-gray-200/80 dark:border-gray-700/80'} flex flex-col min-w-0 h-full bg-white dark:bg-gray-900`}
      data-testid="messages-list-pane"
    >
      <div className="px-4 py-3 border-b border-gray-200/80 dark:border-gray-700/80 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className={KICKER}>Messages · Inbox</p>
          <h2 className="text-base font-semibold tracking-tight text-gray-900 dark:text-gray-100">Conversations</h2>
        </div>
        {listDegraded && (
          <span className="text-xs text-amber-700 dark:text-amber-300 shrink-0" role="status">
            Stale ·{' '}
            <button
              type="button"
              onClick={() => refetchConversations()}
              className={`underline text-primary-600 dark:text-primary-400 ${FOCUS_RING} rounded`}
            >
              Retry
            </button>
          </span>
        )}
      </div>

      {isLoading && displayConversations.length === 0 ? (
        <ListSkeleton />
      ) : conversationsError && displayConversations.length === 0 ? (
        <div
          className="p-6 text-center text-sm text-gray-600 dark:text-gray-300"
          data-testid="messages-list-error"
          role="alert"
        >
          <p className="font-medium text-gray-900 dark:text-gray-100">Could not load conversations</p>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">Check your connection and try again.</p>
          <button
            type="button"
            onClick={() => refetchConversations()}
            aria-label="Retry loading conversations"
            className={`mt-3 inline-flex items-center rounded-lg bg-primary-600 dark:bg-primary-500 px-3 py-2 text-sm font-medium text-white dark:text-gray-950 hover:bg-primary-500 ${FOCUS_RING}`}
          >
            Retry
          </button>
        </div>
      ) : displayConversations.length === 0 ? (
        <div className="p-6 text-center text-gray-500 dark:text-gray-400" data-testid="messages-list-empty">
          <MessageSquare className="w-10 h-10 mx-auto mb-3 text-primary-300 dark:text-primary-700" aria-hidden="true" />
          <p className="font-medium text-gray-900 dark:text-gray-100">No conversations yet</p>
          <p className="mt-1 text-xs">Message an agent or teammate to get started.</p>
        </div>
      ) : (
        <div
          className="overflow-y-auto flex-1 min-h-0 divide-y divide-gray-100 dark:divide-gray-800/80"
          role="listbox"
          aria-label="Conversations"
        >
          {displayConversations.map((conv) => {
            const selected = String(conversationId) === String(conv.id)
            const unread = Number(conv.unread_count || 0) > 0
            const preview = sanitizeMessagePreview(conv.last_message_preview || '')
            return (
              <Link
                key={conv.id}
                to={`/messages/${conv.id}`}
                role="option"
                aria-selected={selected}
                aria-current={selected ? 'true' : undefined}
                aria-labelledby={`conversation-${conv.id}-name`}
                aria-describedby={unread ? `conversation-${conv.id}-unread` : undefined}
                tabIndex={0}
                onKeyDown={(e) => onConversationKeyDown(e, conv.id)}
                data-testid="conversation-row"
                className={`flex items-center gap-3 px-4 min-h-[68px] py-2.5 cursor-pointer min-w-0 transition-colors motion-reduce:transition-none ${FOCUS_RING} ${
                  selected
                    ? 'bg-primary-50 dark:bg-primary-950/40 shadow-[inset_3px_0_0_0] shadow-primary-600 dark:shadow-primary-400'
                    : 'hover:bg-gray-50 dark:hover:bg-gray-800/60'
                }`}
              >
                <div className="relative shrink-0">
                  <div
                    className="w-10 h-10 rounded-full bg-gradient-to-br from-primary-100 to-primary-200 dark:from-primary-900 dark:to-primary-800 ring-1 ring-black/5 dark:ring-white/10"
                    aria-hidden="true"
                  />
                  {unread && (
                    <span
                      className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full bg-primary-600 dark:bg-primary-400 ring-2 ring-white dark:ring-gray-900"
                      aria-hidden="true"
                    />
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 min-w-0">
                    <p
                      id={`conversation-${conv.id}-name`}
                      className={`truncate text-sm tracking-tight text-gray-900 dark:text-gray-100 ${
                        unread ? 'font-semibold' : 'font-medium'
                      }`}
                    >
                      {conversationLabel(conv)}
                    </p>
                    {preview.chip && (
                      <span className="shrink-0 rounded-full bg-gray-100 dark:bg-gray-800 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-gray-600 dark:text-gray-300">
                        {preview.chip}
                      </span>
                    )}
                  </div>
                  <p
                    className="mt-0.5 text-xs text-gray-500 dark:text-gray-400 line-clamp-2 break-words"
                    data-testid="conversation-preview"
                    aria-hidden="true"
                  >
                    {preview.text || 'No messages yet'}
                  </p>
                </div>
                {unread && (
                  <span
                    id={`conversation-${conv.id}-unread`}
                    data-testid="conversation-unread"
                    className="shrink-0 rounded-full bg-primary-600 dark:bg-primary-500 px-2 py-0.5 text-[11px] font-semibold tabular-nums text-white dark:text-gray-950"
                  >
                    {Number(conv.unread_count) > 99 ? '99+' : conv.unread_count}
                    <span className="sr-only"> unread</span>
                  </span>
                )}
              </Link>
            )
          })}
        </div>
      )}
      {conversationsFetching && !isLoading && (
        <div className="sr-only" role="status">
          Refreshing conversations
        </div>
      )}
    </div>
  )

  const threadPane = (
    <div
      className="relative flex-1 flex flex-col min-w-0 h-full bg-[var(--color-bg-canvas,#f9fafb)] dark:bg-gray-950"
      data-testid="messages-thread-pane"
    >
      {conversationId ? (
        <>
          <div className="sticky top-0 z-10 px-4 py-3 border-b border-gray-200/80 dark:border-gray-800 bg-white/90 dark:bg-gray-900/90 backdrop-blur-sm flex items-center gap-2 min-w-0 shadow-sm">
            <Link
              to="/messages"
              aria-label="Back to conversations"
              data-testid="messages-back"
              className={`inline-flex items-center justify-center min-h-11 min-w-11 rounded-lg text-gray-700 dark:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 shrink-0 lg:hidden ${FOCUS_RING}`}
            >
              <ArrowLeft className="w-5 h-5" aria-hidden="true" />
              <span className="sr-only">Inbox</span>
            </Link>
            <div className="min-w-0 flex-1">
              <p className={KICKER}>Thread</p>
              <h3 className="font-semibold tracking-tight text-gray-900 dark:text-gray-100 truncate">
                {selectedConversation ? conversationLabel(selectedConversation) : 'Conversation'}
              </h3>
            </div>
            {threadDegraded && (
              <span className="text-xs text-amber-700 dark:text-amber-300 shrink-0" role="status">
                Syncing…
              </span>
            )}
          </div>

          <div
            ref={scrollerRef}
            onScroll={measureAtBottom}
            className="flex-1 px-4 py-4 overflow-y-auto space-y-3 min-h-0 min-w-0"
          >
            {threadNeverSeen ? (
              <ThreadSkeleton />
            ) : messagesError && displayMessages.length === 0 ? (
              <div
                className="text-center text-sm text-gray-600 dark:text-gray-300 py-8"
                data-testid="messages-thread-error"
                role="alert"
              >
                <p className="font-medium text-gray-900 dark:text-gray-100">Could not load this thread</p>
                <button
                  type="button"
                  onClick={() => refetchMessages()}
                  aria-label="Retry loading message thread"
                  className={`mt-3 inline-flex rounded-lg bg-primary-600 dark:bg-primary-500 px-3 py-2 text-sm font-medium text-white dark:text-gray-950 ${FOCUS_RING}`}
                >
                  Retry
                </button>
              </div>
            ) : displayMessages.length === 0 ? (
              <div className="text-center py-10" data-testid="messages-thread-empty">
                <p className="text-sm font-medium text-gray-900 dark:text-gray-100">No messages yet</p>
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  Send the first message to start this conversation.
                </p>
              </div>
            ) : (
              <>
                {threadDegraded && (
                  <div
                    className="rounded-lg border border-amber-200/80 dark:border-amber-900/50 bg-amber-50/80 dark:bg-amber-950/30 px-3 py-2 text-xs text-amber-900 dark:text-amber-100 flex items-center justify-between gap-2"
                    role="status"
                    data-testid="messages-thread-degraded"
                  >
                    <span>Showing known messages — refresh failed.</span>
                    <button
                      type="button"
                      onClick={() => refetchMessages()}
                      className={`underline text-primary-700 dark:text-primary-300 ${FOCUS_RING} rounded`}
                    >
                      Retry
                    </button>
                  </div>
                )}
                {displayMessages.map((message) => {
                  const outbound = isOutbound(message, user)
                  const system =
                    message.message_type === 'system' ||
                    message.is_system ||
                    /^(system|workflow)/i.test(messageAuthor(message))
                  if (system) {
                    return (
                      <div
                        key={message.id}
                        className="mx-auto max-w-md text-center px-3 py-1.5 text-xs text-gray-500 dark:text-gray-400"
                        data-testid="message-bubble-system"
                      >
                        <span className={`${KICKER} block mb-0.5`}>System</span>
                        <span className="whitespace-pre-wrap break-words">
                          {message.is_deleted ? 'This message was deleted.' : message.content}
                        </span>
                      </div>
                    )
                  }
                  return (
                    <div
                      key={message.id}
                      className={`flex min-w-0 ${outbound ? 'justify-end' : 'justify-start'}`}
                      data-testid={outbound ? 'message-bubble-out' : 'message-bubble-in'}
                    >
                      <div
                        className={`max-w-[min(85%,28rem)] min-w-0 rounded-2xl px-3.5 py-2 shadow-sm ring-1 ring-black/5 dark:ring-white/5 ${
                          outbound
                            ? 'rounded-br-sm bg-primary-50 dark:bg-primary-950/50 text-gray-900 dark:text-gray-100'
                            : 'rounded-bl-sm bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100'
                        }`}
                      >
                        {!outbound && (
                          <div className="text-[11px] font-medium text-gray-500 dark:text-gray-400 mb-0.5">
                            {messageAuthor(message)}
                          </div>
                        )}
                        <div className="text-sm whitespace-pre-wrap break-words [overflow-wrap:anywhere]">
                          {message.is_deleted ? 'This message was deleted.' : message.content}
                        </div>
                        {message.created_at && (
                          <div
                            className={`mt-1 text-[10px] tabular-nums ${
                              outbound ? 'text-primary-700/70 dark:text-primary-300/70 text-right' : 'text-gray-400'
                            }`}
                          >
                            {formatTime(message.created_at)}
                            {message._localSeed ? ' · syncing' : ''}
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })}
                <div ref={threadEndRef} />
              </>
            )}
            {messagesFetching && !messagesLoading && (
              <div className="sr-only" role="status">
                Refreshing messages
              </div>
            )}
          </div>

          {/* CON-418: floating jump-to-latest pill — renders when scrolled
              ≥200px above the live edge, hides at-bottom. The wrapper is
              pointer-events-none so it never intercepts message clicks; only
              the pill itself is interactive. */}
          {!atBottom && (
            <div className="pointer-events-none absolute bottom-4 right-4 z-20">
              <button
                type="button"
                onClick={jumpToLatest}
                aria-label="Jump to latest message"
                data-testid="messages-jump-latest"
                className={`pointer-events-auto inline-flex items-center gap-1.5 rounded-full bg-white dark:bg-gray-900 px-3.5 py-2 text-xs font-medium text-gray-700 dark:text-gray-200 shadow-lg ring-1 ring-black/10 dark:ring-white/10 hover:bg-gray-50 dark:hover:bg-gray-800 ${FOCUS_RING}`}
              >
                <ArrowDown className="w-3.5 h-3.5" aria-hidden="true" />
                Latest
              </button>
            </div>
          )}

          <form
            onSubmit={submitMessage}
            className="p-3 sm:p-4 border-t border-gray-200/80 dark:border-gray-800 bg-white dark:bg-gray-900 min-w-0"
            data-testid="message-composer-form"
          >
            <label className="sr-only" htmlFor="message-composer">
              Message. Press Enter to send. Shift+Enter inserts a newline.
            </label>
            <div className="flex items-end gap-2 min-w-0 rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50/80 dark:bg-gray-950/40 p-2 shadow-sm focus-within:ring-2 focus-within:ring-primary-500/40">
              <textarea
                ref={composerRef}
                id="message-composer"
                rows={1}
                value={draft}
                onChange={onComposerInput}
                onKeyDown={onComposerKeyDown}
                placeholder="Write a message…"
                disabled={sendMessage.isPending}
                aria-describedby="composer-hint"
                className="flex-1 min-w-0 min-h-[44px] max-h-[160px] resize-none bg-transparent px-2 py-2 text-sm text-gray-900 dark:text-gray-100 placeholder:text-gray-400 focus:outline-none disabled:opacity-60"
              />
              <button
                type="submit"
                disabled={!draft.trim() || sendMessage.isPending}
                aria-label="Send message"
                className={`inline-flex items-center gap-1.5 shrink-0 rounded-lg bg-primary-600 dark:bg-primary-500 px-3 py-2.5 text-sm font-medium text-white dark:text-gray-950 hover:bg-primary-500 disabled:opacity-40 disabled:cursor-not-allowed ${FOCUS_RING}`}
              >
                <Send className="w-4 h-4" aria-hidden="true" />
                <span className="hidden sm:inline">Send</span>
              </button>
            </div>
            {sendError && (
              <p
                role="alert"
                data-testid="message-send-error"
                className="mt-1.5 text-[11px] font-medium text-red-600 dark:text-red-400"
              >
                {sendError}
              </p>
            )}
            <p id="composer-hint" className="mt-1.5 text-[11px] text-gray-400 dark:text-gray-500">
              Enter to send · Shift+Enter for a new line
            </p>
          </form>
        </>
      ) : (
        <div
          className="flex-1 flex items-center justify-center text-gray-500 dark:text-gray-400 p-8"
          data-testid="messages-no-selection"
        >
          <div className="text-center max-w-sm">
            <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-primary-50 dark:bg-primary-950/40 ring-1 ring-primary-100 dark:ring-primary-900">
              <MessageSquare className="w-8 h-8 text-primary-500 dark:text-primary-400" aria-hidden="true" />
            </div>
            <p className={`${KICKER} mb-2`}>Messages</p>
            <p className="text-base font-semibold tracking-tight text-gray-900 dark:text-gray-100">
              Pick a conversation
            </p>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              Threads are listed by most recent message. Every counterpart is ordered the same way.
            </p>
          </div>
        </div>
      )}
    </div>
  )

  return (
    <div className="max-w-5xl mx-auto min-w-0 px-0 sm:px-0" data-testid="messages-page">
      <div className="mb-3 px-1">
        <p className={KICKER}>Connect · Messages</p>
        <h1 className="text-xl font-semibold tracking-tight text-gray-900 dark:text-gray-100">Inbox</h1>
      </div>
      <div className="bg-white dark:bg-gray-900 rounded-xl shadow-[0_1px_2px_rgb(0_0_0/0.04),0_0_0_1px_rgb(0_0_0/0.04)] dark:shadow-[0_0_0_1px_rgb(255_255_255/0.06)] overflow-hidden min-w-0">
        <div className="flex flex-col lg:flex-row h-[min(640px,calc(100dvh-9rem))] min-w-0">
          <div
            className={`${
              conversationId ? 'hidden lg:contents' : 'flex flex-col min-w-0 h-full min-h-0'
            }`}
          >
            {conversationList}
          </div>
          <div
            className={`${
              conversationId
                ? 'flex flex-col min-w-0 h-full min-h-0 flex-1'
                : 'hidden lg:flex lg:flex-col lg:min-w-0 lg:h-full lg:min-h-0 lg:flex-1'
            }`}
          >
            {threadPane}
          </div>
        </div>
      </div>
    </div>
  )
}

export default Messages
