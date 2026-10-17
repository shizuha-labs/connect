import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Video, Plus, Calendar, Users, Clock, Copy, ExternalLink, Trash2 } from 'lucide-react'
import { format, formatDistanceToNow } from 'date-fns'
import { meetingsApi } from '../services/meetingsApi'

function Meetings() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [createForm, setCreateForm] = useState({
    title: '',
    description: '',
    scheduled_start: '',
    scheduled_end: '',
    waiting_room_enabled: false,
  })

  // Fetch meetings
  const { data: meetings, isLoading } = useQuery({
    queryKey: ['meetings'],
    queryFn: () => meetingsApi.list().then((res) => res.data),
  })

  // Create instant meeting
  const instantMutation = useMutation({
    mutationFn: () => meetingsApi.instant('Quick Meeting'),
    onSuccess: (response) => {
      navigate(`/meeting/${response.data.meeting.meeting_code}`, {
        state: { tokenData: response.data },
      })
    },
  })

  // Create scheduled meeting
  const createMutation = useMutation({
    mutationFn: (data) => meetingsApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries(['meetings'])
      setShowCreateModal(false)
      setCreateForm({
        title: '',
        description: '',
        scheduled_start: '',
        scheduled_end: '',
        waiting_room_enabled: false,
      })
    },
  })

  // Delete meeting
  const deleteMutation = useMutation({
    mutationFn: (id) => meetingsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries(['meetings'])
    },
  })

  const copyMeetingLink = (meetingCode) => {
    const link = `${window.location.origin}/connect/meeting/${meetingCode}`
    navigator.clipboard.writeText(link)
  }

  const handleCreateSubmit = (e) => {
    e.preventDefault()
    createMutation.mutate(createForm)
  }

  const getStatusBadge = (status) => {
    const styles = {
      scheduled: 'bg-indigo-100 dark:bg-indigo-900/50 text-indigo-800 dark:text-indigo-300',
      active: 'bg-green-100 dark:bg-green-950/50 text-green-800 dark:text-green-300',
      ended: 'bg-gray-100 dark:bg-gray-800 text-gray-800 dark:text-gray-200',
      cancelled: 'bg-red-100 dark:bg-red-950/50 text-red-800 dark:text-red-300',
    }
    return (
      <span className={`px-2 py-1 text-xs font-medium rounded-full ${styles[status]}`}>
        {status.charAt(0).toUpperCase() + status.slice(1)}
      </span>
    )
  }

  return (
    <div className="max-w-4xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-4 mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">Meetings</h1>
          <p className="text-gray-600 dark:text-gray-400">Video conferences and scheduled meetings</p>
        </div>
        <div className="flex gap-3">
          <button
            onClick={() => navigate('/meetings/join')}
            className="px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 flex items-center gap-2"
          >
            <ExternalLink className="w-4 h-4" />
            Join Meeting
          </button>
          <button
            onClick={() => instantMutation.mutate()}
            disabled={instantMutation.isPending}
            className="px-4 py-2 bg-primary-600 dark:bg-primary-500 text-white dark:text-gray-950 rounded-lg hover:bg-primary-700 dark:hover:bg-primary-400 flex items-center gap-2 disabled:opacity-50"
          >
            <Video className="w-4 h-4" />
            {instantMutation.isPending ? 'Starting...' : 'New Meeting'}
          </button>
          <button
            onClick={() => setShowCreateModal(true)}
            className="px-4 py-2 border border-primary-600 dark:border-primary-400 text-primary-600 dark:text-primary-400 rounded-lg hover:bg-primary-50 dark:hover:bg-primary-900/20 flex items-center gap-2"
          >
            <Calendar className="w-4 h-4" />
            Schedule
          </button>
        </div>
      </div>

      {/* Meetings List */}
      {isLoading ? (
        <div className="flex justify-center py-12">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600 dark:border-primary-400"></div>
        </div>
      ) : meetings?.results?.length === 0 ? (
        <div className="text-center py-12 bg-white dark:bg-gray-900 rounded-lg border dark:border-gray-700">
          <Video className="w-12 h-12 text-gray-400 dark:text-gray-500 mx-auto mb-4" />
          <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100 mb-2">No meetings yet</h3>
          <p className="text-gray-600 dark:text-gray-400 mb-4">Start a new meeting or schedule one for later</p>
          <button
            onClick={() => instantMutation.mutate()}
            className="px-4 py-2 bg-primary-600 dark:bg-primary-500 text-white dark:text-gray-950 rounded-lg hover:bg-primary-700 dark:hover:bg-primary-400"
          >
            Start Instant Meeting
          </button>
        </div>
      ) : (
        <div className="space-y-4">
          {meetings?.results?.map((meeting) => (
            <div
              key={meeting.id}
              className="bg-white dark:bg-gray-900 rounded-lg border dark:border-gray-700 p-4 hover:shadow-md transition-shadow"
            >
              <div className="flex items-start justify-between">
                <div className="flex-1">
                  <div className="flex items-center gap-3 mb-2">
                    <h3 className="text-lg font-medium text-gray-900 dark:text-gray-100">
                      {meeting.title || 'Untitled Meeting'}
                    </h3>
                    {getStatusBadge(meeting.status)}
                  </div>

                  <div className="flex items-center gap-4 text-sm text-gray-600 dark:text-gray-400 mb-3">
                    <span className="flex items-center gap-1">
                      <Clock className="w-4 h-4" />
                      {meeting.scheduled_start
                        ? format(new Date(meeting.scheduled_start), 'MMM d, yyyy h:mm a')
                        : `Created ${formatDistanceToNow(new Date(meeting.created_at))} ago`}
                    </span>
                    <span className="flex items-center gap-1">
                      <Users className="w-4 h-4" />
                      {meeting.participant_count} participants
                    </span>
                  </div>

                  <div className="flex items-center gap-2 text-sm">
                    <code className="px-2 py-1 bg-gray-100 dark:bg-gray-800 rounded text-gray-700 dark:text-gray-300 font-mono">
                      {meeting.meeting_code}
                    </code>
                    <button
                      onClick={() => copyMeetingLink(meeting.meeting_code)}
                      className="p-1 text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                      title="Copy meeting link"
                    >
                      <Copy className="w-4 h-4" />
                    </button>
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  {meeting.can_join && (
                    <button
                      onClick={() => navigate(`/meeting/${meeting.meeting_code}`)}
                      className="px-4 py-2 bg-primary-600 dark:bg-primary-500 text-white dark:text-gray-950 rounded-lg hover:bg-primary-700 dark:hover:bg-primary-400 text-sm"
                    >
                      {meeting.is_active ? 'Join' : 'Start'}
                    </button>
                  )}
                  {meeting.status === 'scheduled' && (
                    <button
                      onClick={() => deleteMutation.mutate(meeting.id)}
                      className="p-2 text-gray-400 dark:text-gray-500 hover:text-red-600"
                      title="Delete meeting"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Create Meeting Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white dark:bg-gray-900 rounded-lg p-6 w-full max-w-md">
            <h2 className="text-xl font-bold text-gray-900 dark:text-gray-100 mb-4">Schedule Meeting</h2>
            <form onSubmit={handleCreateSubmit}>
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Meeting Title
                  </label>
                  <input
                    type="text"
                    value={createForm.title}
                    onChange={(e) => setCreateForm({ ...createForm, title: e.target.value })}
                    className="w-full px-3 py-2 border dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
                    placeholder="Team Standup"
                  />
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Description (optional)
                  </label>
                  <textarea
                    value={createForm.description}
                    onChange={(e) => setCreateForm({ ...createForm, description: e.target.value })}
                    className="w-full px-3 py-2 border dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
                    rows={2}
                    placeholder="Meeting agenda..."
                  />
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Start Time
                    </label>
                    <input
                      type="datetime-local"
                      value={createForm.scheduled_start}
                      onChange={(e) =>
                        setCreateForm({ ...createForm, scheduled_start: e.target.value })
                      }
                      className="w-full px-3 py-2 border dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">End Time</label>
                    <input
                      type="datetime-local"
                      value={createForm.scheduled_end}
                      onChange={(e) =>
                        setCreateForm({ ...createForm, scheduled_end: e.target.value })
                      }
                      className="w-full px-3 py-2 border dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
                    />
                  </div>
                </div>

                <div className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    id="waiting_room"
                    checked={createForm.waiting_room_enabled}
                    onChange={(e) =>
                      setCreateForm({ ...createForm, waiting_room_enabled: e.target.checked })
                    }
                    className="rounded border-gray-300 dark:border-gray-600 text-primary-600 dark:text-primary-400 focus:ring-primary-500"
                  />
                  <label htmlFor="waiting_room" className="text-sm text-gray-700 dark:text-gray-300">
                    Enable waiting room
                  </label>
                </div>
              </div>

              <div className="flex justify-end gap-3 mt-6">
                <button
                  type="button"
                  onClick={() => setShowCreateModal(false)}
                  className="px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={createMutation.isPending}
                  className="px-4 py-2 bg-primary-600 dark:bg-primary-500 text-white dark:text-gray-950 rounded-lg hover:bg-primary-700 dark:hover:bg-primary-400 disabled:opacity-50"
                >
                  {createMutation.isPending ? 'Creating...' : 'Create Meeting'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}

export default Meetings
