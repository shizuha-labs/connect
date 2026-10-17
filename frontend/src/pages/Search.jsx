import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { searchApi } from '../services/api'
import { Search as SearchIcon } from 'lucide-react'
import LoadingSpinner from '../components/common/LoadingSpinner'

function Search() {
  // Query is driven by the URL (?q=) so links — e.g. clickable #hashtags in the
  // feed — land here and run the search directly, and results stay shareable.
  const [searchParams, setSearchParams] = useSearchParams()
  const query = searchParams.get('q') || ''
  const setQuery = (value) => setSearchParams(value ? { q: value } : {}, { replace: true })
  const [searchType, setSearchType] = useState('all')

  const { data, isLoading } = useQuery({
    queryKey: ['search', query, searchType],
    queryFn: () => searchApi.universal(query),
    enabled: query.length > 0,
  })

  return (
    <div className="max-w-4xl mx-auto">
      {/* Search Input */}
      <div className="bg-white dark:bg-gray-900 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4 mb-6">
        <div className="relative">
          <SearchIcon className="absolute left-4 top-1/2 transform -translate-y-1/2 w-5 h-5 text-gray-400 dark:text-gray-500" />
          <input
            type="text"
            placeholder="Search for people and posts..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="w-full pl-12 pr-4 py-3 border border-gray-300 dark:border-gray-600 rounded-lg text-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
          />
        </div>

        {/* Search Type Tabs */}
        <div className="mt-4 flex gap-2">
          {['all', 'people', 'posts'].map((type) => (
            <button
              key={type}
              onClick={() => setSearchType(type)}
              className={`px-4 py-2 rounded-full text-sm font-medium transition-colors ${
                searchType === type
                  ? 'bg-primary-600 dark:bg-primary-500 text-white dark:text-gray-950'
                  : 'bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-700'
              }`}
            >
              {type.charAt(0).toUpperCase() + type.slice(1)}
            </button>
          ))}
        </div>
      </div>

      {/* Results */}
      {query.length === 0 ? (
        <div className="bg-white dark:bg-gray-900 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-8 text-center text-gray-500 dark:text-gray-400">
          <SearchIcon className="w-16 h-16 mx-auto mb-4 text-gray-300 dark:text-gray-600" />
          <p>Enter a search term to find people, posts, and more</p>
        </div>
      ) : isLoading ? (
        <LoadingSpinner />
      ) : (
        <div className="space-y-6">
          {/* People Results */}
          {searchType !== 'posts' && data?.data?.people?.length > 0 && (
            <div className="bg-white dark:bg-gray-900 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4">
              <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-4">People</h3>
              <div className="space-y-3">
                {data.data.people.map((person) => (
                  <div key={person.id} className="flex items-center gap-3 p-2 hover:bg-gray-50 dark:hover:bg-gray-800 rounded-lg">
                    <div className="w-10 h-10 bg-gray-200 dark:bg-gray-700 rounded-full" />
                    <div>
                      <p className="font-medium text-gray-900 dark:text-gray-100">{person.display_name}</p>
                      <p className="text-sm text-gray-500 dark:text-gray-400">@{person.username}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Posts Results */}
          {searchType !== 'people' && data?.data?.posts?.length > 0 && (
            <div className="bg-white dark:bg-gray-900 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4">
              <h3 className="font-semibold text-gray-900 dark:text-gray-100 mb-4">Posts</h3>
              <div className="space-y-3">
                {data.data.posts.map((post) => (
                  <div key={post.id} className="p-3 border border-gray-100 dark:border-gray-800 rounded-lg">
                    <p className="text-gray-700 dark:text-gray-300">{post.content.substring(0, 200)}...</p>
                  </div>
                ))}
              </div>
            </div>
          )}

        </div>
      )}
    </div>
  )
}

export default Search
