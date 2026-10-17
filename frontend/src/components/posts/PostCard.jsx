import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { postsApi } from '../../services/api'
import { Heart, MessageCircle, Repeat2, Bookmark, Share } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import { renderTextWithHashtags } from '../../utils/hashtags'

function PostCard({ post, detail = false }) {
  const [isLiked, setIsLiked] = useState(!!post.user_reaction)
  const [likesCount, setLikesCount] = useState(post.likes_count)
  const [isBookmarked, setIsBookmarked] = useState(post.is_bookmarked)
  const [expanded, setExpanded] = useState(false)
  const queryClient = useQueryClient()

  const likeMutation = useMutation({
    mutationFn: () => isLiked ? postsApi.unreact(post.id) : postsApi.react(post.id),
    onSuccess: () => {
      setIsLiked(!isLiked)
      setLikesCount(isLiked ? likesCount - 1 : likesCount + 1)
    },
  })

  const bookmarkMutation = useMutation({
    mutationFn: () => isBookmarked ? postsApi.unbookmark(post.id) : postsApi.bookmark(post.id),
    onSuccess: () => {
      setIsBookmarked(!isBookmarked)
    },
  })

  // CON-91: in the feed, clamp long posts to a word-boundary preview with a
  // "Read more" expander so content is never cut off mid-word and stays fully
  // readable. The API returns the full content; `detail` (single-post view)
  // always renders it in full.
  const CONTENT_PREVIEW_LIMIT = 280
  const fullContent = post.content || ''
  const isLong = fullContent.length > CONTENT_PREVIEW_LIMIT
  const showFullContent = detail || expanded || !isLong
  let displayContent = fullContent
  if (!showFullContent) {
    const sliced = fullContent.slice(0, CONTENT_PREVIEW_LIMIT)
    const lastSpace = sliced.lastIndexOf(' ')
    displayContent = `${(lastSpace > 0 ? sliced.slice(0, lastSpace) : sliced).trimEnd()}…`
  }

  return (
    <div className="min-w-0 rounded-xl bg-white p-4 shadow-[0_1px_2px_rgb(0_0_0/0.04),0_0_0_1px_rgb(0_0_0/0.04)] transition-colors hover:bg-gray-50/40 dark:bg-gray-900 dark:shadow-[0_1px_2px_rgb(0_0_0/0.2),0_0_0_1px_rgb(255_255_255/0.06)] dark:hover:bg-gray-900/80">
      {/* Header */}
      <div className="flex items-start gap-3">
        <Link to={`/profile/${post.author_email?.split('@')[0] || 'user'}`}>
          <div className="w-10 h-10 bg-gray-200 dark:bg-gray-700 rounded-full" />
        </Link>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <Link to={`/profile/${post.author_email?.split('@')[0] || 'user'}`} className="font-medium text-gray-900 dark:text-gray-100 hover:underline">
              {post.author_email?.split('@')[0] || 'User'}
            </Link>
            <span className="text-gray-400 dark:text-gray-500">·</span>
            <span className="text-sm text-gray-500 dark:text-gray-400">
              {formatDistanceToNow(new Date(post.created_at), { addSuffix: true })}
            </span>
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="mt-3">
        <p className="text-gray-700 dark:text-gray-300 whitespace-pre-wrap">{renderTextWithHashtags(displayContent)}</p>
        {!showFullContent && (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="mt-1 text-sm font-medium text-primary-600 dark:text-primary-400 hover:underline"
          >
            Read more
          </button>
        )}

        {/* Media */}
        {post.media?.length > 0 && (
          <div className={`mt-3 grid gap-2 ${
            post.media.length === 1 ? 'grid-cols-1' :
            post.media.length === 2 ? 'grid-cols-2' :
            'grid-cols-2'
          }`}>
            {post.media.map((media) => (
              <div key={media.id} className="aspect-video bg-gray-100 dark:bg-gray-800 rounded-lg overflow-hidden">
                {media.media_type === 'image' ? (
                  <img src={media.file_url} alt={media.alt_text || ''} className="w-full h-full object-cover" />
                ) : media.media_type === 'video' ? (
                  <video src={media.file_url} controls className="w-full h-full object-cover" />
                ) : null}
              </div>
            ))}
          </div>
        )}

        {/* Quoted Post */}
        {post.original_post_data && post.post_type === 'quote' && (
          <div className="mt-3 p-3 border border-gray-200 dark:border-gray-700 rounded-lg">
            <div className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400 mb-2">
              <div className="w-5 h-5 bg-gray-200 dark:bg-gray-700 rounded-full" />
              <span>{post.original_post_data.author_email?.split('@')[0]}</span>
            </div>
            <p className="text-gray-600 dark:text-gray-400 text-sm">{renderTextWithHashtags(post.original_post_data.content)}</p>
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="mt-4 flex items-center justify-between">
        <div className="flex items-center gap-6">
          {/* Like */}
          <button
            onClick={() => likeMutation.mutate()}
            className={`flex items-center gap-1 ${isLiked ? 'text-red-500' : 'text-gray-500 dark:text-gray-400 hover:text-red-500'} transition-colors`}
          >
            <Heart className={`w-5 h-5 ${isLiked ? 'fill-current' : ''}`} />
            <span className="text-sm">{likesCount}</span>
          </button>

          {/* Comment */}
          <button className="flex items-center gap-1 text-gray-500 dark:text-gray-400 hover:text-primary-600 dark:hover:text-primary-400 transition-colors">
            <MessageCircle className="w-5 h-5" />
            <span className="text-sm">{post.comments_count}</span>
          </button>

          {/* Repost */}
          <button className="flex items-center gap-1 text-gray-500 dark:text-gray-400 hover:text-green-500 transition-colors">
            <Repeat2 className="w-5 h-5" />
            <span className="text-sm">{post.reposts_count + post.quotes_count}</span>
          </button>
        </div>

        <div className="flex items-center gap-2">
          {/* Bookmark */}
          <button
            onClick={() => bookmarkMutation.mutate()}
            className={`p-2 ${isBookmarked ? 'text-primary-600 dark:text-primary-400' : 'text-gray-500 dark:text-gray-400 hover:text-primary-600 dark:hover:text-primary-400'} transition-colors`}
          >
            <Bookmark className={`w-5 h-5 ${isBookmarked ? 'fill-current' : ''}`} />
          </button>

          {/* Share */}
          <button className="p-2 text-gray-500 dark:text-gray-400 hover:text-primary-600 dark:hover:text-primary-400 transition-colors">
            <Share className="w-5 h-5" />
          </button>
        </div>
      </div>
    </div>
  )
}

export default PostCard
