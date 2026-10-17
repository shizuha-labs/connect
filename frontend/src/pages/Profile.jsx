import { useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { profileApi, integrationsApi } from '../services/api'
import { MapPin, Link as LinkIcon, Calendar, Briefcase } from 'lucide-react'
import LoadingSpinner from '../components/common/LoadingSpinner'

function Profile() {
  const { username } = useParams()

  const { data: profile, isLoading } = useQuery({
    queryKey: ['profile', username],
    queryFn: () => username === 'me' ? profileApi.getMe() : profileApi.getProfile(username),
  })

  const { data: linkedinResp } = useQuery({
    queryKey: ['integrations', 'linkedin'],
    queryFn: () => integrationsApi.linkedin(),
    enabled: username === 'me',
  })
  const linkedin = linkedinResp?.data

  if (isLoading) {
    return <LoadingSpinner />
  }

  const user = profile?.data

  return (
    <div className="max-w-4xl mx-auto">
      {/* Cover and Avatar */}
      <div className="bg-white dark:bg-gray-900 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 overflow-hidden">
        {/* Banner */}
        <div className="h-48 bg-gradient-to-r from-primary-500 to-primary-700">
          {user?.banner_url && (
            <img src={user.banner_url} alt="" className="w-full h-full object-cover" />
          )}
        </div>

        {/* Profile Info */}
        <div className="px-6 pb-6">
          {/* Avatar */}
          <div className="relative -mt-16 mb-4">
            <div className="w-32 h-32 rounded-full border-4 border-white bg-gray-200 dark:bg-gray-700 overflow-hidden">
              {user?.avatar_url ? (
                <img src={user.avatar_url} alt="" className="w-full h-full object-cover" />
              ) : (
                <div className="w-full h-full flex items-center justify-center text-4xl font-bold text-gray-400 dark:text-gray-400">
                  {user?.display_name?.[0] || '?'}
                </div>
              )}
            </div>
            {user?.is_verified && (
              <div className="absolute bottom-2 right-2 w-6 h-6 bg-primary-600 dark:bg-primary-500 rounded-full flex items-center justify-center">
                <svg className="w-4 h-4 text-white dark:text-gray-950" fill="currentColor" viewBox="0 0 20 20">
                  <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                </svg>
              </div>
            )}
          </div>

          {/* Name and Username */}
          <div className="flex justify-between items-start">
            <div>
              <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">{user?.display_name}</h1>
              <p className="text-gray-500 dark:text-gray-400">@{user?.username}</p>
              {user?.headline && (
                <p className="mt-1 text-gray-700 dark:text-gray-300">{user.headline}</p>
              )}
            </div>
            <button className="px-4 py-2 bg-primary-600 dark:bg-primary-500 text-white dark:text-gray-950 rounded-full font-medium hover:bg-primary-700 dark:hover:bg-primary-400 transition-colors">
              Follow
            </button>
          </div>

          {/* Bio */}
          {user?.bio && (
            <p className="mt-4 text-gray-700 dark:text-gray-300">{user.bio}</p>
          )}

          {/* Meta Info */}
          <div className="mt-4 flex flex-wrap gap-4 text-sm text-gray-500 dark:text-gray-400">
            {user?.current_position && user?.current_company && (
              <div className="flex items-center">
                <Briefcase className="w-4 h-4 mr-1" />
                {user.current_position} at {user.current_company}
              </div>
            )}
            {user?.location && (
              <div className="flex items-center">
                <MapPin className="w-4 h-4 mr-1" />
                {user.location}
              </div>
            )}
            {user?.website && (
              <div className="flex items-center">
                <LinkIcon className="w-4 h-4 mr-1" />
                <a href={user.website} className="text-primary-600 dark:text-primary-400 hover:underline" target="_blank" rel="noopener noreferrer">
                  {user.website.replace(/^https?:\/\//, '')}
                </a>
              </div>
            )}
            {linkedin?.status === 'connected' ? (
              <div className="flex items-center">
                LinkedIn connected{linkedin.email ? ` · ${linkedin.email}` : ''}
              </div>
            ) : null}
            <div className="flex items-center">
              <Calendar className="w-4 h-4 mr-1" />
              Joined {new Date(user?.created_at).toLocaleDateString('en-US', { month: 'long', year: 'numeric' })}
            </div>
          </div>

          {/* Stats */}
          <div className="mt-4 flex gap-6">
            <div>
              <span className="font-bold text-gray-900 dark:text-gray-100">{user?.following_count || 0}</span>
              <span className="text-gray-500 dark:text-gray-400 ml-1">Following</span>
            </div>
            <div>
              <span className="font-bold text-gray-900 dark:text-gray-100">{user?.followers_count || 0}</span>
              <span className="text-gray-500 dark:text-gray-400 ml-1">Followers</span>
            </div>
            <div>
              <span className="font-bold text-gray-900 dark:text-gray-100">{user?.connections_count || 0}</span>
              <span className="text-gray-500 dark:text-gray-400 ml-1">Connections</span>
            </div>
          </div>
        </div>
      </div>

      {/* Tabs for Posts, Activity, etc. */}
      <div className="mt-6 bg-white dark:bg-gray-900 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4">
        <p className="text-gray-500 dark:text-gray-400 text-center">Posts and activity coming soon...</p>
      </div>
    </div>
  )
}

export default Profile
