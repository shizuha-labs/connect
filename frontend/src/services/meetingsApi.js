import api from './api'

/**
 * Meetings API for video conferencing
 */
export const meetingsApi = {
  /**
   * List user's meetings
   */
  list: (params = {}) => api.get('/meetings/', { params }),

  /**
   * Get a single meeting by ID
   */
  get: (id) => api.get(`/meetings/${id}/`),

  /**
   * Create a scheduled meeting
   */
  create: (data) => api.post('/meetings/', data),

  /**
   * Update a meeting
   */
  update: (id, data) => api.patch(`/meetings/${id}/`, data),

  /**
   * Delete a meeting
   */
  delete: (id) => api.delete(`/meetings/${id}/`),

  /**
   * Create and start an instant meeting
   */
  instant: (title = '') => api.post('/meetings/instant/', { title }),

  /**
   * Start a scheduled meeting (host only)
   */
  start: (id) => api.post(`/meetings/${id}/start/`),

  /**
   * End a meeting (host only)
   */
  end: (id) => api.post(`/meetings/${id}/end/`),

  /**
   * Join a meeting by ID and get LiveKit token
   */
  join: (id) => api.post(`/meetings/${id}/join/`),

  /**
   * Join a meeting by meeting code
   */
  joinByCode: (meetingCode, displayName = '') =>
    api.post('/meetings/join-by-code/', {
      meeting_code: meetingCode,
      display_name: displayName,
    }),

  /**
   * Leave a meeting
   */
  leave: (id) => api.post(`/meetings/${id}/leave/`),

  /**
   * Get meeting participants
   */
  participants: (id) => api.get(`/meetings/${id}/participants/`),

  /**
   * Get participants in waiting room (host only)
   */
  waitingRoom: (id) => api.get(`/meetings/${id}/waiting_room/`),

  /**
   * Send meeting invitations (host only)
   */
  invite: (id, emails, message = '') =>
    api.post(`/meetings/${id}/invite/`, { emails, message }),

  /**
   * Admit a participant from waiting room (host only)
   */
  admit: (id, participantId) =>
    api.post(`/meetings/${id}/admit/`, { participant_id: participantId }),

  /**
   * Remove a participant from meeting (host only)
   */
  remove: (id, participantId) =>
    api.post(`/meetings/${id}/remove/`, { participant_id: participantId }),
}

export default meetingsApi
