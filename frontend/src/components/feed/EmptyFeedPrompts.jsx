import { Link } from 'react-router-dom'
import { Hash, Search, UserPlus, Users } from 'lucide-react'
import SectionKicker from '../common/SectionKicker'

const FOCUS_RING =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-950'

const COPY = {
  home: {
    kicker: 'Feed · Empty',
    title: 'Your feed is ready for a first spark',
    body: 'Follow people, connect with the community, or post an intro so Connect can start filling this space.',
    primary: { to: '/search?type=people', label: 'Find people to follow', Icon: UserPlus },
  },
  following: {
    kicker: 'Following · Empty',
    title: 'Follow a few people to build this feed',
    body: 'The Following feed only shows posts from people you follow. Start with suggested connections or search for teammates.',
    primary: { to: '/search?type=people', label: 'Find suggestions', Icon: UserPlus },
  },
  trending: {
    kicker: 'Trending · Empty',
    title: 'No trending posts yet',
    body: 'Trends use recent public posts. Explore hashtags or publish a post with a topic tag to start one.',
    // One earned CTA — unavailable trending must not look populated (CON-276 §4.7).
    primary: { to: '/search?type=posts', label: 'Explore hashtags', Icon: Hash },
  },
}

export default function EmptyFeedPrompts({ feedType = 'home' }) {
  const copy = COPY[feedType] || COPY.home
  const { Icon } = copy.primary

  return (
    <div className="rounded-xl bg-white p-8 text-center shadow-[0_1px_2px_rgb(0_0_0/0.04),0_0_0_1px_rgb(0_0_0/0.04)] dark:bg-gray-900 dark:shadow-[0_1px_2px_rgb(0_0_0/0.2),0_0_0_1px_rgb(255_255_255/0.06)]">
      <SectionKicker className="mb-3">{copy.kicker}</SectionKicker>
      <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-primary-50 dark:bg-primary-900/20">
        <Users className="h-6 w-6 text-primary-600 dark:text-primary-400" />
      </div>
      <h3 className="text-lg font-semibold tracking-tight text-gray-900 dark:text-gray-100">
        {copy.title}
      </h3>
      <p className="mx-auto mt-2 max-w-md text-sm text-gray-500 dark:text-gray-400">{copy.body}</p>

      <div className="mt-5 flex justify-center">
        <Link
          to={copy.primary.to}
          className={`inline-flex min-h-11 items-center justify-center gap-2 rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-700 dark:bg-primary-500 dark:text-gray-950 dark:hover:bg-primary-400 ${FOCUS_RING}`}
        >
          <Icon size={16} />
          {copy.primary.label}
        </Link>
      </div>

      {feedType !== 'trending' && (
        <div className="mt-3">
          <Link
            to="/search"
            className={`inline-flex items-center gap-1 text-sm text-gray-500 hover:text-primary-600 dark:text-gray-400 dark:hover:text-primary-400 ${FOCUS_RING}`}
          >
            <Search size={14} />
            Explore search
          </Link>
        </div>
      )}
    </div>
  )
}
