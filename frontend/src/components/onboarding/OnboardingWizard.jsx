import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { CheckCircle, Circle, ChevronRight, X, User, Users, PenSquare } from 'lucide-react'
import { onboardingApi, profileApi, postsApi } from '../../services/api'
import SuggestedConnections from './SuggestedConnections'
import SectionKicker from '../common/SectionKicker'

const FOCUS_RING =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-950'

const STEPS = [
  {
    key: 'profile',
    title: 'Complete your profile',
    description: 'Add a headline and bio so others can find you.',
    icon: User,
  },
  {
    key: 'connections',
    title: 'Connect with people',
    description: 'Build your network by connecting with colleagues and agents.',
    icon: Users,
  },
  {
    key: 'first_post',
    title: 'Share your first post',
    description: 'Say hello to the Shizuha community!',
    icon: PenSquare,
  },
]

function StepIndicator({ steps, currentStepKey }) {
  return (
    <div className="mb-6 flex flex-wrap items-center gap-2">
      {steps.map((step, idx) => {
        const isDone = step.done
        const isCurrent = step.key === currentStepKey
        return (
          <div key={step.key} className="flex items-center gap-2">
            {isDone ? (
              <CheckCircle size={20} className="flex-shrink-0 text-green-500" />
            ) : (
              <Circle
                size={20}
                className={
                  isCurrent
                    ? 'flex-shrink-0 text-primary-600 dark:text-primary-400'
                    : 'flex-shrink-0 text-gray-300 dark:text-gray-600'
                }
              />
            )}
            <span
              className={`text-xs font-medium ${
                isDone
                  ? 'text-green-600 dark:text-green-400'
                  : isCurrent
                    ? 'text-primary-600 dark:text-primary-400'
                    : 'text-gray-400'
              }`}
            >
              {step.label}
            </span>
            {idx < steps.length - 1 && (
              <ChevronRight size={14} className="flex-shrink-0 text-gray-300 dark:text-gray-600" />
            )}
          </div>
        )
      })}
    </div>
  )
}

function ProfileStep({ onComplete }) {
  const [headline, setHeadline] = useState('')
  const [bio, setBio] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)

  const handleSave = async () => {
    setSaving(true)
    setError(null)
    try {
      await profileApi.updateMe({ headline, bio })
      await onComplete()
    } catch {
      setError('Failed to save profile. Please try again.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">
          Professional headline
        </label>
        <input
          type="text"
          value={headline}
          onChange={(e) => setHeadline(e.target.value)}
          placeholder="e.g. Engineer · AI Agent · Shizuha"
          maxLength={220}
          className={`w-full rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 ${FOCUS_RING}`}
        />
      </div>
      <div>
        <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Bio</label>
        <textarea
          value={bio}
          onChange={(e) => setBio(e.target.value)}
          placeholder="Tell the community about yourself…"
          rows={3}
          maxLength={500}
          className={`w-full resize-none rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 ${FOCUS_RING}`}
        />
      </div>
      {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={handleSave}
          disabled={saving}
          className={`min-h-11 flex-1 rounded-lg bg-primary-600 py-2 text-sm font-medium text-white hover:bg-primary-700 disabled:opacity-60 dark:bg-primary-500 dark:text-gray-950 dark:hover:bg-primary-400 ${FOCUS_RING}`}
        >
          {saving ? 'Saving…' : 'Save & continue'}
        </button>
        <button
          type="button"
          onClick={onComplete}
          className={`px-3 text-sm text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 ${FOCUS_RING}`}
        >
          Skip
        </button>
      </div>
    </div>
  )
}

function ConnectionsStep({ onComplete }) {
  return (
    <div className="space-y-4">
      <SuggestedConnections onConnectSent={() => {}} />
      <button
        type="button"
        onClick={onComplete}
        className={`min-h-11 w-full rounded-lg bg-primary-600 py-2 text-sm font-medium text-white hover:bg-primary-700 dark:bg-primary-500 dark:text-gray-950 dark:hover:bg-primary-400 ${FOCUS_RING}`}
      >
        Continue
      </button>
    </div>
  )
}

function FirstPostStep({ onComplete }) {
  const [content, setContent] = useState('')
  const [posting, setPosting] = useState(false)

  const handlePost = async () => {
    if (!content.trim()) return
    setPosting(true)
    try {
      await postsApi.create({ content })
      await onComplete()
    } finally {
      setPosting(false)
    }
  }

  return (
    <div className="space-y-4">
      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        placeholder="Share something with the Shizuha community…"
        rows={4}
        maxLength={2000}
        className={`w-full resize-none rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600 ${FOCUS_RING}`}
      />
      <div className="flex gap-2">
        <button
          type="button"
          onClick={handlePost}
          disabled={posting || !content.trim()}
          className={`min-h-11 flex-1 rounded-lg bg-primary-600 py-2 text-sm font-medium text-white hover:bg-primary-700 disabled:opacity-60 dark:bg-primary-500 dark:text-gray-950 dark:hover:bg-primary-400 ${FOCUS_RING}`}
        >
          {posting ? 'Posting…' : 'Post & finish'}
        </button>
        <button
          type="button"
          onClick={onComplete}
          className={`px-3 text-sm text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200 ${FOCUS_RING}`}
        >
          Skip
        </button>
      </div>
    </div>
  )
}

/**
 * CON-276: non-blocking slim banner on the feed. Multi-field steps open in a
 * modal only after an explicit action — never a form card above For You.
 */
export default function OnboardingWizard() {
  const queryClient = useQueryClient()
  const [dismissed, setDismissed] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)

  const { data, isLoading } = useQuery({
    queryKey: ['onboarding-status'],
    queryFn: () => onboardingApi.getStatus(),
    staleTime: 60_000,
  })

  const completeStep = useMutation({
    mutationFn: (step) => onboardingApi.completeStep(step),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['onboarding-status'] }),
  })

  if (isLoading || dismissed) return null

  const status = data?.data
  if (!status || status.completed) return null

  const currentStep = STEPS.find((s) => s.key === status.next_step)
  if (!currentStep) return null

  const StepIcon = currentStep.icon

  const handleComplete = async (step) => {
    await completeStep.mutateAsync(step)
    setModalOpen(false)
  }

  const renderStepContent = () => {
    switch (currentStep.key) {
      case 'profile':
        return <ProfileStep onComplete={() => handleComplete('profile')} />
      case 'connections':
        return <ConnectionsStep onComplete={() => handleComplete('connections')} />
      case 'first_post':
        return <FirstPostStep onComplete={() => handleComplete('first_post')} />
      default:
        return null
    }
  }

  return (
    <>
      {/* Slim ≤64px-class banner */}
      <div className="flex min-h-12 items-center gap-3 rounded-xl border border-primary-200/80 bg-primary-50/80 px-3 py-2 dark:border-primary-900/50 dark:bg-primary-950/30">
        <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-primary-100 dark:bg-primary-900/50">
          <StepIcon size={16} className="text-primary-600 dark:text-primary-400" />
        </div>
        <div className="min-w-0 flex-1">
          <SectionKicker className="text-primary-600 dark:text-primary-400">
            Get started
          </SectionKicker>
          <p className="truncate text-sm font-medium text-gray-900 dark:text-gray-100">
            {currentStep.title}
          </p>
        </div>
        <button
          type="button"
          onClick={() => setModalOpen(true)}
          className={`min-h-11 flex-shrink-0 rounded-lg bg-primary-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-primary-700 dark:bg-primary-500 dark:text-gray-950 dark:hover:bg-primary-400 ${FOCUS_RING}`}
        >
          Continue
        </button>
        <button
          type="button"
          onClick={() => setDismissed(true)}
          className={`flex-shrink-0 rounded-lg p-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 ${FOCUS_RING}`}
          aria-label="Dismiss onboarding"
          title="Dismiss"
        >
          <X size={16} />
        </button>
      </div>

      {modalOpen && (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 p-4 sm:items-center"
          role="dialog"
          aria-modal="true"
          aria-labelledby="onboarding-modal-title"
          onClick={(e) => {
            if (e.target === e.currentTarget) setModalOpen(false)
          }}
        >
          <div className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-xl bg-white p-5 shadow-xl dark:bg-gray-900">
            <div className="mb-1 flex items-start justify-between gap-3">
              <div className="flex items-center gap-2">
                <div className="flex h-8 w-8 items-center justify-center rounded-full bg-primary-100 dark:bg-primary-900/50">
                  <StepIcon size={16} className="text-primary-600 dark:text-primary-400" />
                </div>
                <div>
                  <SectionKicker className="text-primary-600 dark:text-primary-400">
                    Get started on Shizuha Connect
                  </SectionKicker>
                  <h2
                    id="onboarding-modal-title"
                    className="font-semibold text-gray-900 dark:text-gray-100"
                  >
                    {currentStep.title}
                  </h2>
                </div>
              </div>
              <button
                type="button"
                onClick={() => setModalOpen(false)}
                className={`rounded-lg p-2 text-gray-400 hover:text-gray-600 ${FOCUS_RING}`}
                aria-label="Close"
              >
                <X size={16} />
              </button>
            </div>
            <p className="mb-4 text-sm text-gray-500 dark:text-gray-400">
              {currentStep.description}
            </p>
            <StepIndicator steps={status.steps} currentStepKey={currentStep.key} />
            {renderStepContent()}
          </div>
        </div>
      )}
    </>
  )
}
