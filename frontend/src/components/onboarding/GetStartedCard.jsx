import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Search, X, Sparkles } from 'lucide-react'
import { socialApi } from '../../services/api'
import SectionKicker from '../common/SectionKicker'

const DISMISSED_KEY = 'connect_get_started_dismissed'
const FOCUS_RING =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-950'

/** Slim network-empty banner — never a multi-field form (CON-276). */
export default function GetStartedCard() {
  const [dismissed, setDismissed] = useState(
    () => localStorage.getItem(DISMISSED_KEY) === '1',
  )

  const { data, isLoading } = useQuery({
    queryKey: ['connections-count'],
    queryFn: () => socialApi.getConnections(),
    staleTime: 2 * 60 * 1000,
    enabled: !dismissed,
  })

  if (dismissed || isLoading) return null

  const count =
    data?.data?.count ?? (Array.isArray(data?.data) ? data.data.length : null) ?? 0
  if (count > 0) return null

  const handleDismiss = () => {
    localStorage.setItem(DISMISSED_KEY, '1')
    setDismissed(true)
  }

  return (
    <div className="flex min-h-12 items-center gap-3 rounded-xl border border-primary-100 bg-white px-3 py-2 shadow-[0_1px_2px_rgb(0_0_0/0.04),0_0_0_1px_rgb(0_0_0/0.04)] dark:border-primary-900/50 dark:bg-gray-900 dark:shadow-[0_1px_2px_rgb(0_0_0/0.2),0_0_0_1px_rgb(255_255_255/0.06)]">
      <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-primary-50 dark:bg-primary-900/30">
        <Sparkles size={16} className="text-primary-600 dark:text-primary-400" />
      </div>
      <div className="min-w-0 flex-1">
        <SectionKicker>Network · Empty</SectionKicker>
        <p className="truncate text-sm text-gray-700 dark:text-gray-300">
          You haven&apos;t connected with anyone yet — find people to follow.
        </p>
      </div>
      <Link
        to="/search?type=people"
        className={`inline-flex min-h-11 flex-shrink-0 items-center gap-1.5 rounded-lg border border-primary-300 px-3 py-1.5 text-sm font-medium text-primary-700 hover:bg-primary-50 dark:border-primary-700 dark:text-primary-300 dark:hover:bg-primary-950/40 ${FOCUS_RING}`}
      >
        <Search size={14} />
        Find people
      </Link>
      <button
        type="button"
        onClick={handleDismiss}
        className={`flex-shrink-0 rounded-lg p-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 ${FOCUS_RING}`}
        title="Dismiss"
        aria-label="Dismiss get started banner"
      >
        <X size={16} />
      </button>
    </div>
  )
}
