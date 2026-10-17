import { useMemo, useState } from 'react'
import { useQuery, keepPreviousData } from '@tanstack/react-query'
import { feedApi } from '../services/api'
import PostComposer from '../components/posts/PostComposer'
import PostCard from '../components/posts/PostCard'
import TrendingSidebar from '../components/feed/TrendingSidebar'
import EmptyFeedPrompts from '../components/feed/EmptyFeedPrompts'
import FeedSkeleton from '../components/feed/FeedSkeleton'
import OnboardingWizard from '../components/onboarding/OnboardingWizard'
import SuggestedConnections from '../components/onboarding/SuggestedConnections'
import GetStartedCard from '../components/onboarding/GetStartedCard'
import SectionKicker from '../components/common/SectionKicker'

const TABS = [
  { key: 'home', label: 'For You' },
  { key: 'following', label: 'Following' },
  { key: 'trending', label: 'Trending' },
]

const FOCUS_RING =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-950'

function normalizePosts(data) {
  if (!data) return []
  const body = data?.data ?? data
  if (Array.isArray(body?.results)) return body.results
  if (Array.isArray(body)) return body
  return []
}

function Home() {
  const [feedType, setFeedType] = useState('home')

  const { data, isLoading, isFetching, isError, error, refetch, dataUpdatedAt } = useQuery({
    queryKey: ['feed', feedType],
    queryFn: () => {
      if (feedType === 'home') return feedApi.home()
      if (feedType === 'following') return feedApi.following()
      return feedApi.trending()
    },
    // Keep prior tab data visible while a tab switch deepens (game-engine rule).
    placeholderData: keepPreviousData,
  })

  const posts = useMemo(() => normalizePosts(data), [data])
  const hasPosts = posts.length > 0
  // First-ever paint for this tab: no cached/placeholder rows yet.
  const showSkeleton = isLoading && !hasPosts
  const showErrorPanel = isError && !hasPosts
  const staleHint = isError && hasPosts
  const deepening = isFetching && hasPosts && !isLoading

  return (
    <div className="grid min-w-0 grid-cols-1 gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      {/* Main Feed */}
      <div className="min-w-0 space-y-4">
        <header className="space-y-1">
          <SectionKicker>Connect · Feed</SectionKicker>
          <h1 className="text-xl font-semibold tracking-tight text-gray-900 dark:text-gray-100">
            Home
          </h1>
        </header>

        {/* Slim non-blocking onboarding (never multi-field form above feed) */}
        <OnboardingWizard />

        <PostComposer />

        {/* Network empty nudge — slim banner */}
        <GetStartedCard />

        {/* Feed Type Tabs */}
        <div
          className="rounded-xl bg-white dark:bg-gray-900 shadow-[0_1px_2px_rgb(0_0_0/0.04),0_0_0_1px_rgb(0_0_0/0.04)] dark:shadow-[0_1px_2px_rgb(0_0_0/0.2),0_0_0_1px_rgb(255_255_255/0.06)]"
          role="tablist"
          aria-label="Feed type"
        >
          <div className="flex border-b border-gray-200 dark:border-gray-700">
            {TABS.map((tab) => {
              const active = feedType === tab.key
              return (
                <button
                  key={tab.key}
                  type="button"
                  role="tab"
                  aria-selected={active}
                  onClick={() => setFeedType(tab.key)}
                  className={`min-h-11 flex-1 py-3 text-sm font-medium transition-colors ${FOCUS_RING} ${
                    active
                      ? 'border-b-2 border-primary-600 text-primary-600 dark:border-primary-400 dark:text-primary-400'
                      : 'text-gray-500 hover:text-gray-800 dark:text-gray-400 dark:hover:text-gray-200'
                  }`}
                >
                  {tab.label}
                </button>
              )
            })}
          </div>
        </div>

        {(deepening || staleHint) && (
          <p
            className="text-xs text-gray-500 dark:text-gray-400"
            role="status"
            aria-live="polite"
          >
            {staleHint
              ? 'Showing cached posts — sync paused. Retrying in the background…'
              : 'Syncing latest posts…'}
          </p>
        )}

        {/* Posts / states */}
        {showSkeleton ? (
          <FeedSkeleton />
        ) : showErrorPanel ? (
          <div
            className="rounded-xl border border-gray-200 bg-white p-6 text-center dark:border-gray-700 dark:bg-gray-900"
            role="alert"
          >
            <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
              Couldn’t load this feed
            </p>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              {error?.message || 'Something went wrong. Your network may be offline.'}
            </p>
            <button
              type="button"
              onClick={() => refetch()}
              className={`mt-4 inline-flex min-h-11 items-center justify-center rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-700 dark:bg-primary-500 dark:text-gray-950 dark:hover:bg-primary-400 ${FOCUS_RING}`}
            >
              Retry
            </button>
          </div>
        ) : !hasPosts ? (
          <EmptyFeedPrompts feedType={feedType} />
        ) : (
          <div className="min-w-0 space-y-4" data-feed-updated={dataUpdatedAt || 0}>
            {posts.map((post) => (
              <PostCard key={post.id} post={post} />
            ))}
          </div>
        )}
      </div>

      {/* Discovery rail — desktop; mobile collapses via CSS (CON-278 sheet may wrap later) */}
      <aside className="hidden min-w-0 space-y-4 lg:block">
        <TrendingSidebar />
        <SuggestedConnections />
      </aside>
    </div>
  )
}

export default Home
