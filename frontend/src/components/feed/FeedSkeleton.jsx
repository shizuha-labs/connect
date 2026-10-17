/** First-paint card skeletons — never replace known posts with a spinner (CON-276 §4.7). */
export default function FeedSkeleton({ count = 3 }) {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading feed">
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="rounded-xl bg-white dark:bg-gray-900 p-4 shadow-[0_1px_2px_rgb(0_0_0/0.04),0_0_0_1px_rgb(0_0_0/0.04)] dark:shadow-[0_1px_2px_rgb(0_0_0/0.2),0_0_0_1px_rgb(255_255_255/0.06)] animate-pulse"
        >
          <div className="flex gap-3">
            <div className="h-10 w-10 flex-shrink-0 rounded-full bg-gray-200 dark:bg-gray-700" />
            <div className="min-w-0 flex-1 space-y-2">
              <div className="h-3 w-1/3 rounded bg-gray-200 dark:bg-gray-700" />
              <div className="h-3 w-1/4 rounded bg-gray-100 dark:bg-gray-800" />
              <div className="mt-3 h-3 w-full rounded bg-gray-100 dark:bg-gray-800" />
              <div className="h-3 w-5/6 rounded bg-gray-100 dark:bg-gray-800" />
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}
