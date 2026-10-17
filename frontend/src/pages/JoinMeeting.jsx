import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { Video, ArrowLeft } from 'lucide-react'
import { meetingsApi } from '../services/meetingsApi'

function JoinMeeting() {
  const navigate = useNavigate()
  const [meetingCode, setMeetingCode] = useState('')
  const [error, setError] = useState('')

  const joinMutation = useMutation({
    mutationFn: (code) => meetingsApi.joinByCode(code),
    onSuccess: (response) => {
      if (response.data.status === 'waiting') {
        // User is in waiting room
        navigate(`/meeting/${response.data.meeting.meeting_code}`, {
          state: { waiting: true, meeting: response.data.meeting },
        })
      } else {
        // User can join directly
        navigate(`/meeting/${response.data.meeting.meeting_code}`, {
          state: { tokenData: response.data },
        })
      }
    },
    onError: (error) => {
      setError(error.response?.data?.meeting_code?.[0] || 'Failed to join meeting')
    },
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    setError('')

    // Normalize meeting code
    const code = meetingCode.toLowerCase().trim()

    // Validate format
    if (!/^[a-z0-9]{3}-[a-z0-9]{4}-[a-z0-9]{3}$/.test(code)) {
      setError('Invalid meeting code format. Expected: xxx-xxxx-xxx')
      return
    }

    joinMutation.mutate(code)
  }

  const formatMeetingCode = (value) => {
    // Remove non-alphanumeric characters except hyphens
    let cleaned = value.toLowerCase().replace(/[^a-z0-9-]/g, '')

    // Auto-insert hyphens
    if (cleaned.length > 3 && cleaned[3] !== '-') {
      cleaned = cleaned.slice(0, 3) + '-' + cleaned.slice(3)
    }
    if (cleaned.length > 8 && cleaned[8] !== '-') {
      cleaned = cleaned.slice(0, 8) + '-' + cleaned.slice(8)
    }

    // Limit length
    return cleaned.slice(0, 12)
  }

  return (
    <div className="max-w-md mx-auto mt-12">
      <button
        onClick={() => navigate('/meetings')}
        className="flex items-center gap-2 text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 mb-6"
      >
        <ArrowLeft className="w-4 h-4" />
        Back to Meetings
      </button>

      <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 p-8">
        <div className="text-center mb-6">
          <div className="w-16 h-16 bg-primary-100 dark:bg-primary-900/50 rounded-full flex items-center justify-center mx-auto mb-4">
            <Video className="w-8 h-8 text-primary-600 dark:text-primary-400" />
          </div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Join Meeting</h1>
          <p className="text-gray-600 dark:text-gray-400 mt-2">Enter the meeting code to join</p>
        </div>

        <form onSubmit={handleSubmit}>
          <div className="mb-4">
            <label htmlFor="meetingCode" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
              Meeting Code
            </label>
            <input
              id="meetingCode"
              type="text"
              value={meetingCode}
              onChange={(e) => setMeetingCode(formatMeetingCode(e.target.value))}
              placeholder="abc-defg-hij"
              className="w-full px-4 py-3 border border-gray-300 dark:border-gray-600 rounded-lg text-center text-lg font-mono focus:outline-none focus:ring-2 focus:ring-primary-500"
              autoComplete="off"
              autoFocus
            />
            {error && <p className="mt-2 text-sm text-red-600 dark:text-red-400">{error}</p>}
          </div>

          <button
            type="submit"
            disabled={joinMutation.isPending || meetingCode.length < 12}
            className="w-full py-3 bg-primary-600 dark:bg-primary-500 text-white dark:text-gray-950 rounded-lg hover:bg-primary-700 dark:hover:bg-primary-400 disabled:opacity-50 disabled:cursor-not-allowed font-medium"
          >
            {joinMutation.isPending ? 'Joining...' : 'Join Meeting'}
          </button>
        </form>

        <div className="mt-6 pt-6 border-t border-gray-200 dark:border-gray-700 text-center">
          <p className="text-sm text-gray-600 dark:text-gray-400">
            Don't have a meeting code?{' '}
            <button
              onClick={() => navigate('/meetings')}
              className="text-primary-600 dark:text-primary-400 hover:text-primary-700 dark:hover:text-primary-300 font-medium"
            >
              Create a new meeting
            </button>
          </p>
        </div>
      </div>
    </div>
  )
}

export default JoinMeeting
