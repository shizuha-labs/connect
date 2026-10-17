import { useParams, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'
import { postsApi } from '../services/api'
import PostCard from '../components/posts/PostCard'
import TrendingSidebar from '../components/feed/TrendingSidebar'
import LoadingSpinner from '../components/common/LoadingSpinner'

// Feed filtered to a single hashtag. Reached from the clickable #hashtags in
// post text (see utils/hashtags.jsx) and backed by the existing posts filter
// endpoint: GET /api/.../posts/?hashtag=<tag>.
function HashtagFeed() {
  const { tag } = useParams()

  const { data, isLoading, error } = useQuery({
    queryKey: ['hashtag-feed', tag],
    queryFn: () => postsApi.list({ hashtag: tag }),
    enabled: !!tag,
  })

  const posts = data?.data?.results ?? data?.data ?? []

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      {/* Main Feed */}
      <div className="lg:col-span-2 space-y-4">
        <div className="bg-white dark:bg-gray-900 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4 flex items-center gap-3">
          <Link to="/" className="text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200" aria-label="Back to feed">
            <ArrowLeft className="w-5 h-5" />
          </Link>
          <div>
            <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100">#{tag}</h1>
            <p className="text-sm text-gray-500 dark:text-gray-400">Posts tagged #{tag}</p>
          </div>
        </div>

        {isLoading ? (
          <LoadingSpinner />
        ) : error ? (
          <div className="bg-white dark:bg-gray-900 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-8 text-center text-gray-500 dark:text-gray-400">
            Error loading posts for #{tag}. Please try again.
          </div>
        ) : posts.length === 0 ? (
          <div className="bg-white dark:bg-gray-900 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-8 text-center text-gray-500 dark:text-gray-400">
            No posts tagged #{tag} yet.
          </div>
        ) : (
          <div className="space-y-4">
            {posts.map((post) => (
              <PostCard key={post.id} post={post} />
            ))}
          </div>
        )}
      </div>

      {/* Sidebar */}
      <div className="hidden lg:block space-y-4">
        <TrendingSidebar />
      </div>
    </div>
  )
}

export default HashtagFeed
