import { useState, useEffect, useCallback } from 'react'
import { useParams, useNavigate, useLocation } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  LiveKitRoom,
  VideoConference,
  RoomAudioRenderer,
  ControlBar,
  useTracks,
  useRoomContext,
  useParticipants,
} from '@livekit/components-react'
import '@livekit/components-styles'
import {
  Copy,
  Users,
  PhoneOff,
  Loader2,
  AlertCircle,
  Clock,
  Check,
} from 'lucide-react'
import { meetingsApi } from '../services/meetingsApi'

function WaitingRoom({ meeting, onAdmitted }) {
  return (
    <div className="min-h-screen bg-gray-900 flex items-center justify-center">
      <div className="bg-white dark:bg-gray-900 rounded-lg p-8 max-w-md text-center">
        <div className="w-16 h-16 bg-indigo-100 dark:bg-indigo-900/50 rounded-full flex items-center justify-center mx-auto mb-4">
          <Clock className="w-8 h-8 text-primary-600 dark:text-primary-400" />
        </div>
        <h2 className="text-xl font-bold text-gray-900 dark:text-gray-100 mb-2">Waiting Room</h2>
        <p className="text-gray-600 dark:text-gray-400 mb-4">
          You're in the waiting room for <strong>{meeting?.title || 'this meeting'}</strong>.
          <br />
          The host will let you in shortly.
        </p>
        <div className="flex items-center justify-center gap-2 text-sm text-gray-500 dark:text-gray-400">
          <Loader2 className="w-4 h-4 animate-spin" />
          Waiting for host approval...
        </div>
      </div>
    </div>
  )
}

function MeetingControls({ meetingCode, participantCount }) {
  const [copied, setCopied] = useState(false)

  const copyLink = () => {
    const link = `${window.location.origin}/connect/meeting/${meetingCode}`
    navigator.clipboard.writeText(link)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div className="absolute top-4 left-4 flex items-center gap-4 z-10">
      <button
        onClick={copyLink}
        className="flex items-center gap-2 px-3 py-2 bg-gray-800 bg-opacity-75 text-white rounded-lg text-sm hover:bg-opacity-100 transition-all"
      >
        {copied ? (
          <>
            <Check className="w-4 h-4 text-green-400" />
            Copied!
          </>
        ) : (
          <>
            <Copy className="w-4 h-4" />
            Copy Link
          </>
        )}
      </button>
      <div className="flex items-center gap-2 px-3 py-2 bg-gray-800 bg-opacity-75 text-white rounded-lg text-sm">
        <Users className="w-4 h-4" />
        {participantCount}
      </div>
    </div>
  )
}

function MeetingRoomContent({ meeting, onLeave }) {
  const participants = useParticipants()

  return (
    <>
      <MeetingControls
        meetingCode={meeting.meeting_code}
        participantCount={participants.length}
      />
      <VideoConference />
      <RoomAudioRenderer />
    </>
  )
}

function MeetingRoom() {
  const { meetingCode } = useParams()
  const navigate = useNavigate()
  const location = useLocation()

  const [token, setToken] = useState(location.state?.tokenData?.token || null)
  const [serverUrl, setServerUrl] = useState(location.state?.tokenData?.url || null)
  const [meeting, setMeeting] = useState(location.state?.tokenData?.meeting || location.state?.meeting || null)
  const [isWaiting, setIsWaiting] = useState(location.state?.waiting || false)
  const [error, setError] = useState(null)

  // Join meeting mutation (if we don't have token from navigation)
  const joinMutation = useMutation({
    mutationFn: () => meetingsApi.joinByCode(meetingCode),
    onSuccess: (response) => {
      if (response.data.status === 'waiting') {
        setIsWaiting(true)
        setMeeting(response.data.meeting)
      } else {
        setToken(response.data.token)
        setServerUrl(response.data.url)
        setMeeting(response.data.meeting)
        setIsWaiting(false)
      }
    },
    onError: (error) => {
      setError(error.response?.data?.meeting_code?.[0] || 'Failed to join meeting')
    },
  })
  const joinMeeting = joinMutation.mutate

  // Leave meeting
  const leaveMutation = useMutation({
    mutationFn: () => {
      if (meeting?.id) {
        return meetingsApi.leave(meeting.id)
      }
      return Promise.resolve()
    },
    onSuccess: () => {
      navigate('/meetings')
    },
  })

  // Join on mount if we don't have token
  useEffect(() => {
    if (!token && !isWaiting && meetingCode) {
      joinMeeting()
    }
  }, [meetingCode, token, isWaiting, joinMeeting])

  // Poll for waiting room status
  useEffect(() => {
    if (!isWaiting || !meeting) return

    const interval = setInterval(async () => {
      try {
        const response = await meetingsApi.joinByCode(meetingCode)
        if (response.data.status !== 'waiting') {
          setToken(response.data.token)
          setServerUrl(response.data.url)
          setMeeting(response.data.meeting)
          setIsWaiting(false)
        }
      } catch (err) {
        // Ignore errors during polling
      }
    }, 3000)

    return () => clearInterval(interval)
  }, [isWaiting, meeting, meetingCode])

  const handleDisconnect = useCallback(() => {
    leaveMutation.mutate()
  }, [leaveMutation])

  // Loading state
  if (joinMutation.isPending) {
    return (
      <div className="min-h-screen bg-gray-900 flex items-center justify-center">
        <div className="text-center text-white">
          <Loader2 className="w-12 h-12 animate-spin mx-auto mb-4" />
          <p className="text-lg">Joining meeting...</p>
        </div>
      </div>
    )
  }

  // Error state
  if (error) {
    return (
      <div className="min-h-screen bg-gray-900 flex items-center justify-center">
        <div className="bg-white dark:bg-gray-900 rounded-lg p-8 max-w-md text-center">
          <div className="w-16 h-16 bg-red-100 dark:bg-red-950/50 rounded-full flex items-center justify-center mx-auto mb-4">
            <AlertCircle className="w-8 h-8 text-red-600 dark:text-red-400" />
          </div>
          <h2 className="text-xl font-bold text-gray-900 dark:text-gray-100 mb-2">Unable to Join</h2>
          <p className="text-gray-600 dark:text-gray-400 mb-4">{error}</p>
          <button
            onClick={() => navigate('/meetings')}
            className="px-4 py-2 bg-primary-600 dark:bg-primary-500 text-white dark:text-gray-950 rounded-lg hover:bg-primary-700 dark:hover:bg-primary-400"
          >
            Back to Meetings
          </button>
        </div>
      </div>
    )
  }

  // Waiting room
  if (isWaiting) {
    return <WaitingRoom meeting={meeting} />
  }

  // No token yet
  if (!token || !serverUrl) {
    return (
      <div className="min-h-screen bg-gray-900 flex items-center justify-center">
        <div className="text-center text-white">
          <Loader2 className="w-12 h-12 animate-spin mx-auto mb-4" />
          <p className="text-lg">Connecting...</p>
        </div>
      </div>
    )
  }

  // LiveKit room
  return (
    <div className="h-screen bg-gray-900">
      <LiveKitRoom
        serverUrl={serverUrl}
        token={token}
        connect={true}
        video={true}
        audio={true}
        onDisconnected={handleDisconnect}
        data-lk-theme="default"
        style={{ height: '100%' }}
      >
        <MeetingRoomContent meeting={meeting} onLeave={handleDisconnect} />
      </LiveKitRoom>
    </div>
  )
}

export default MeetingRoom
