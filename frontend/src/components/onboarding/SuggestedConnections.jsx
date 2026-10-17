import { useQuery } from '@tanstack/react-query'
import { Users, UserPlus } from 'lucide-react'
import { onboardingApi } from '../../services/api'
import { socialApi } from '../../services/api'

export default function SuggestedConnections({ onConnectSent }) {
  const { data, isLoading } = useQuery({
    queryKey: ['onboarding-suggestions'],
    queryFn: () => onboardingApi.getSuggestions(8),
  })

  const suggestions = data?.data?.suggestions || []

  const handleConnect = async (userId) => {
    try {
      await socialApi.sendConnectionRequest(userId)
      onConnectSent?.()
    } catch {
      // ignore — user may already be connected
    }
  }

  if (isLoading) {
    return (
      <div className="rounded-xl bg-white p-4 shadow-[0_1px_2px_rgb(0_0_0/0.04),0_0_0_1px_rgb(0_0_0/0.04)] dark:bg-gray-900 dark:shadow-[0_1px_2px_rgb(0_0_0/0.2),0_0_0_1px_rgb(255_255_255/0.06)]">
        <div className="animate-pulse space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="flex items-center gap-3">
              <div className="w-10 h-10 bg-gray-200 dark:bg-gray-700 rounded-full" />
              <div className="flex-1 space-y-1">
                <div className="h-3 bg-gray-200 dark:bg-gray-700 rounded w-1/2" />
                <div className="h-3 bg-gray-200 dark:bg-gray-700 rounded w-2/3" />
              </div>
            </div>
          ))}
        </div>
      </div>
    )
  }

  if (!suggestions.length) return null

  return (
    <div className="rounded-xl bg-white p-4 shadow-[0_1px_2px_rgb(0_0_0/0.04),0_0_0_1px_rgb(0_0_0/0.04)] dark:bg-gray-900 dark:shadow-[0_1px_2px_rgb(0_0_0/0.2),0_0_0_1px_rgb(255_255_255/0.06)]">
      <div className="mb-3 flex items-center gap-2">
        <Users size={16} className="text-primary-600 dark:text-primary-400" />
        <div>
          <div className="font-mono text-[10px] font-medium uppercase tracking-[0.16em] text-gray-600 dark:text-gray-400">
            Discover · People
          </div>
          <h3 className="text-sm font-semibold tracking-tight text-gray-900 dark:text-gray-100">
            People you may know
          </h3>
        </div>
      </div>
      <div className="space-y-3">
        {suggestions.map((person) => (
          <div key={person.user_id} className="flex items-center gap-3">
            <div className="relative flex-shrink-0">
              {person.avatar_url ? (
                <img
                  src={person.avatar_url}
                  alt={person.display_name}
                  className="w-10 h-10 rounded-full object-cover"
                />
              ) : (
                <div className="w-10 h-10 rounded-full bg-primary-100 dark:bg-primary-900/50 flex items-center justify-center text-primary-600 dark:text-primary-400 font-semibold text-sm">
                  {(person.display_name || person.username || '?')[0].toUpperCase()}
                </div>
              )}
              {person.is_verified && (
                <span className="absolute -bottom-0.5 -right-0.5 w-4 h-4 bg-primary-600 dark:bg-primary-500 rounded-full flex items-center justify-center text-white dark:text-gray-950 text-xs">✓</span>
              )}
            </div>
            <div className="flex-1 min-w-0">
              <div className="font-medium text-gray-900 dark:text-gray-100 text-sm truncate">{person.display_name}</div>
              <div className="text-gray-500 dark:text-gray-400 text-xs truncate">@{person.username}</div>
              {person.headline && (
                <div className="text-gray-600 dark:text-gray-400 text-xs truncate">{person.headline}</div>
              )}
            </div>
            <button
              onClick={() => handleConnect(person.user_id)}
              className="flex-shrink-0 flex items-center gap-1 text-xs text-primary-600 dark:text-primary-400 border border-primary-300 dark:border-primary-700 rounded-full px-2 py-1 hover:bg-primary-50 dark:hover:bg-primary-900/30 transition-colors"
            >
              <UserPlus size={12} />
              Connect
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}
