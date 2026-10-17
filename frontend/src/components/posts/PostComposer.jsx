import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { postsApi } from '../../services/api'
import { Image, Video, Hash, AtSign, Send } from 'lucide-react'

const FOCUS_RING =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-950'

function PostComposer() {
  const [content, setContent] = useState('')
  const [isExpanded, setIsExpanded] = useState(false)
  const queryClient = useQueryClient()

  const createPost = useMutation({
    mutationFn: (data) => postsApi.create(data),
    onSuccess: () => {
      setContent('')
      setIsExpanded(false)
      queryClient.invalidateQueries({ queryKey: ['feed'] })
    },
  })

  const handleSubmit = () => {
    if (content.trim()) {
      createPost.mutate({ content })
    }
  }

  return (
    <div className="rounded-xl bg-white p-4 shadow-[0_1px_2px_rgb(0_0_0/0.04),0_0_0_1px_rgb(0_0_0/0.04)] dark:bg-gray-900 dark:shadow-[0_1px_2px_rgb(0_0_0/0.2),0_0_0_1px_rgb(255_255_255/0.06)]">
      <div className="flex gap-3">
        <div
          className="h-10 w-10 flex-shrink-0 rounded-full bg-primary-100 dark:bg-primary-900/40"
          aria-hidden
        />

        <div className="min-w-0 flex-1">
          <textarea
            placeholder="Share an update with your network…"
            value={content}
            onChange={(e) => setContent(e.target.value)}
            onFocus={() => setIsExpanded(true)}
            rows={isExpanded ? 4 : 1}
            className={`w-full resize-none rounded-lg border-0 bg-transparent px-1 py-2 text-[15px] text-gray-800 placeholder-gray-400 focus:ring-0 dark:text-gray-200 dark:placeholder-gray-500 ${FOCUS_RING} ${
              isExpanded ? 'min-h-[6rem]' : 'min-h-11'
            }`}
            aria-label="Compose a post"
          />

          {isExpanded && (
            <>
              <div className="mb-2 text-right text-xs text-gray-400 dark:text-gray-500">
                {content.length}/3000
              </div>

              <div className="flex items-center justify-between gap-2 border-t border-gray-100 pt-3 dark:border-gray-800">
                <div className="flex gap-1" role="toolbar" aria-label="Post attachments">
                  {[
                    { Icon: Image, label: 'Add image' },
                    { Icon: Video, label: 'Add video' },
                    { Icon: Hash, label: 'Add hashtag' },
                    { Icon: AtSign, label: 'Mention someone' },
                  ].map(({ Icon, label }) => (
                    <button
                      key={label}
                      type="button"
                      aria-label={label}
                      className={`rounded-lg p-2 text-gray-500 transition-colors hover:bg-primary-50 hover:text-primary-600 dark:text-gray-400 dark:hover:bg-primary-900/20 dark:hover:text-primary-400 ${FOCUS_RING}`}
                    >
                      <Icon className="h-5 w-5" />
                    </button>
                  ))}
                </div>

                <button
                  type="button"
                  onClick={handleSubmit}
                  disabled={!content.trim() || createPost.isPending}
                  className={`inline-flex min-h-11 items-center gap-2 rounded-full bg-primary-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-primary-500 dark:text-gray-950 dark:hover:bg-primary-400 ${FOCUS_RING}`}
                >
                  <Send className="h-4 w-4" />
                  Post
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

export default PostComposer
