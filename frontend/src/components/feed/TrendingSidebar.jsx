import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { searchApi } from '../../services/api'
import { TrendingUp } from 'lucide-react'
import SectionKicker from '../common/SectionKicker'

const FOCUS_RING =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-950'

function TrendingSidebar() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['trendingHashtags'],
    queryFn: () => searchApi.trendingHashtags(),
  })

  const topics = Array.isArray(data?.data) ? data.data : []
  const hasTopics = topics.length > 0

  return (
    <div className="sticky top-20 space-y-4 rounded-xl bg-white p-4 shadow-[0_1px_2px_rgb(0_0_0/0.04),0_0_0_1px_rgb(0_0_0/0.04)] dark:bg-gray-900 dark:shadow-[0_1px_2px_rgb(0_0_0/0.2),0_0_0_1px_rgb(255_255_255/0.06)]">
      <div>
        <SectionKicker className="mb-1">Discover · Trending</SectionKicker>
        <h3 className="flex items-center gap-2 font-semibold tracking-tight text-gray-900 dark:text-gray-100">
          <TrendingUp className="h-4 w-4 text-primary-600 dark:text-primary-400" />
          Trending
        </h3>
      </div>

      <div className="space-y-3">
        {isLoading && !hasTopics ? (
          <div className="space-y-3 animate-pulse" aria-busy="true">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-10 rounded-lg bg-gray-100 dark:bg-gray-800" />
            ))}
          </div>
        ) : isError ? (
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Trending is temporarily unavailable.
          </p>
        ) : hasTopics ? (
          topics.slice(0, 10).map((topic, index) => (
            <Link
              key={topic.id || index}
              to={`/hashtag/${encodeURIComponent(topic.name)}`}
              className={`block rounded-lg px-1 py-1 transition-colors hover:bg-gray-50 dark:hover:bg-gray-800/80 ${FOCUS_RING}`}
            >
              <p className="text-[10px] font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                Trending
              </p>
              <p className="font-medium text-gray-900 dark:text-gray-100">#{topic.name}</p>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                {topic.post_count?.toLocaleString()} posts
              </p>
            </Link>
          ))
        ) : (
          <p className="text-sm text-gray-500 dark:text-gray-400">No trending topics yet</p>
        )}
      </div>

      <div className="border-t border-gray-200 pt-4 dark:border-gray-700">
        <SectionKicker className="mb-2">Discover · People</SectionKicker>
        <h3 className="mb-2 font-semibold tracking-tight text-gray-900 dark:text-gray-100">
          Who to follow
        </h3>
        <Link
          to="/search?type=people"
          className={`inline-flex min-h-11 items-center justify-center rounded-lg border border-primary-300 px-3 py-2 text-sm font-medium text-primary-700 transition-colors hover:bg-primary-50 dark:border-primary-700 dark:text-primary-300 dark:hover:bg-primary-950/40 ${FOCUS_RING}`}
        >
          Find people
        </Link>
      </div>
    </div>
  )
}

export default TrendingSidebar
