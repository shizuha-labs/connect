import { Bell } from 'lucide-react'

function Notifications() {
  return (
    <div className="max-w-2xl mx-auto">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100 mb-6">Notifications</h1>

      <div className="bg-white dark:bg-gray-900 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-8 text-center">
        <Bell className="w-16 h-16 mx-auto mb-4 text-gray-300 dark:text-gray-600" />
        <p className="text-gray-500 dark:text-gray-400">No notifications yet</p>
        <p className="text-sm text-gray-400 dark:text-gray-400 mt-2">
          When someone likes, comments, or follows you, you'll see it here
        </p>
      </div>
    </div>
  )
}

export default Notifications
