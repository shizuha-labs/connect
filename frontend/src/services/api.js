import axios from 'axios'

const API_BASE = import.meta.env.VITE_API_BASE || '/connect/api'

const api = axios.create({
  baseURL: API_BASE,
  headers: {
    'Content-Type': 'application/json',
  },
})

const ACCESS_TOKEN_KEY = 'shizuha_access_token'
const REFRESH_TOKEN_KEY = 'shizuha_refresh_token'
const USER_KEY = 'shizuha_user'

const attachAuthToken = (config) => {
  // Use shizuha_access_token (set by Shizuha ID login)
  const token = localStorage.getItem(ACCESS_TOKEN_KEY)
  if (token) {
    config.headers = config.headers || {}
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
}

// CON-271: silent refresh on access-token expiry. A single-flight promise so
// concurrent 401s (feed + sidebar hydrate together) share one refresh call
// instead of racing each other to the refresh endpoint.
let refreshPromise = null

const refreshAccessToken = () => {
  if (!refreshPromise) {
    const refreshToken = localStorage.getItem(REFRESH_TOKEN_KEY)
    if (!refreshToken) {
      refreshPromise = Promise.reject(new Error('no refresh token available'))
    } else {
      // Bare axios (not `api`) so this call never re-enters the interceptor.
      refreshPromise = axios
        .post('/id/api/auth/refresh/', { refresh: refreshToken })
        .then((response) => {
          const newAccess = response.data?.access
          if (!newAccess) throw new Error('refresh response missing access token')
          localStorage.setItem(ACCESS_TOKEN_KEY, newAccess)
          // SimpleJWT may rotate the refresh token (HS256 migration path).
          if (response.data?.refresh) {
            localStorage.setItem(REFRESH_TOKEN_KEY, response.data.refresh)
          }
          return newAccess
        })
    }
    refreshPromise.finally(() => { refreshPromise = null })
  }
  return refreshPromise
}

const clearSession = () => {
  localStorage.removeItem(ACCESS_TOKEN_KEY)
  localStorage.removeItem(REFRESH_TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

const redirectToLogin = () => {
  window.location.href = '/id/login?continue=/connect/'
}

const handleAuthError = async (error) => {
  const original = error.config
  // Only attempt silent refresh on 401 for a retryable request that has not
  // already been retried once and still holds a refresh token.
  if (
    error.response?.status === 401 &&
    original &&
    !original._retried &&
    localStorage.getItem(REFRESH_TOKEN_KEY)
  ) {
    original._retried = true
    try {
      const newAccess = await refreshAccessToken()
      original.headers = original.headers || {}
      original.headers.Authorization = `Bearer ${newAccess}`
      return api(original)
    } catch (refreshError) {
      // Refresh failed — the session is genuinely invalid. Fail closed to the
      // login screen exactly as before (no silent fallback).
      clearSession()
      redirectToLogin()
      return Promise.reject(refreshError)
    }
  }
  // Non-401, or a 401 with no refresh path available: preserve prior
  // fail-closed behavior.
  if (error.response?.status === 401) {
    clearSession()
    redirectToLogin()
  }
  return Promise.reject(error)
}

// Request interceptor for auth token
api.interceptors.request.use(attachAuthToken)

// Response interceptor for error handling
api.interceptors.response.use((response) => response, handleAuthError)

// Profile API
export const profileApi = {
  getMe: () => api.get('/me/'),
  updateMe: (data) => api.patch('/me/', data),
  getProfile: (username) => api.get(`/profiles/${username}/`),
  getProfilePosts: (username) => api.get(`/profiles/${username}/posts/`),
  getFollowers: (username) => api.get(`/profiles/${username}/followers/`),
  getFollowing: (username) => api.get(`/profiles/${username}/following/`),
}

// Onboarding API (CON-24)
export const onboardingApi = {
  getStatus: () => api.get('/onboarding/status/'),
  completeStep: (step) => api.post(`/onboarding/steps/${step}/complete/`),
  getSuggestions: (limit = 10) => api.get('/onboarding/suggestions/', { params: { limit } }),
}

// Posts API
export const postsApi = {
  list: (params = {}) => api.get('/posts/', { params }),
  get: (id) => api.get(`/posts/${id}/`),
  create: (data) => api.post('/posts/', data),
  update: (id, data) => api.patch(`/posts/${id}/`, data),
  delete: (id) => api.delete(`/posts/${id}/`),
  react: (id, reactionType = 'like') => api.post(`/posts/${id}/react/`, { reaction_type: reactionType }),
  unreact: (id) => api.post(`/posts/${id}/unreact/`),
  repost: (id) => api.post(`/posts/${id}/repost/`),
  quote: (id, content) => api.post(`/posts/${id}/quote/`, { content }),
  bookmark: (id) => api.post(`/posts/${id}/bookmark/`),
  unbookmark: (id) => api.post(`/posts/${id}/unbookmark/`),
  getComments: (id) => api.get(`/posts/${id}/comments/`),
  addComment: (id, content) => api.post(`/posts/${id}/comments/`, { content }),
}

// Feed API
export const feedApi = {
  home: (params = {}) => api.get('/feed/home/', { params }),
  following: (params = {}) => api.get('/feed/following/', { params }),
  trending: (params = {}) => api.get('/feed/trending/', { params }),
}

// Social API
export const socialApi = {
  follow: (userId) => api.post(`/follow/${userId}/`),
  unfollow: (userId) => api.post(`/unfollow/${userId}/`),
  getConnections: () => api.get('/connections/'),
  getConnectionRequests: () => api.get('/connections/requests/'),
  sendConnectionRequest: (userId, message = '') => api.post(`/connections/request/${userId}/`, { message }),
  acceptConnection: (id) => api.post(`/connections/${id}/accept/`),
  rejectConnection: (id) => api.post(`/connections/${id}/reject/`),
  block: (userId) => api.post(`/block/${userId}/`),
  unblock: (userId) => api.post(`/unblock/${userId}/`),
}

export const integrationsApi = {
  list: () => api.get('/integrations/'),
  linkedin: () => api.get('/integrations/linkedin/'),
}

// Messaging API

// CON-408: `crypto.randomUUID` exists only in secure contexts (HTTPS/localhost).
// The deployed frontend is reached over plain HTTP on the LAN, where it is
// undefined — every sendMessage mutationFn then threw before any fetch, so
// Send clicked dead: zero network requests, no error feedback, composer never
// cleared. Generate an RFC4122 v4 UUID via getRandomValues (available in every
// browser context), falling back to Math.random only if even that is missing.
const clientUuid = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  const bytes = new Uint8Array(16)
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(bytes)
  } else {
    for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256)
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export const messagingApi = {
  getConversations: () => api.get('/conversations/'),
  getConversation: (id) => api.get(`/conversations/${id}/`),
  createConversation: (data) => api.post('/conversations/', data),
  getHiveAgents: () => api.get('/hive/agents/'),
  openHiveAgentConversation: (stableId) => api.post('/hive/agents/conversation/', { stable_id: stableId }),
  getMessages: (conversationId) => api.get(`/conversations/${conversationId}/messages/`),
  sendMessage: (conversationId, data) => api.post(`/conversations/${conversationId}/messages/`, {
    client_message_id: clientUuid(),
    ...data,
  }),
  markRead: (conversationId) => api.post(`/conversations/${conversationId}/read/`),
}

// Stories API
export const storiesApi = {
  list: () => api.get('/stories/'),
  get: (id) => api.get(`/stories/${id}/`),
  create: (data) => api.post('/stories/', data),
  view: (id) => api.post(`/stories/${id}/view/`),
  react: (id, reaction, message = '') => api.post(`/stories/${id}/react/`, { reaction, message }),
  getMyStories: () => api.get('/stories/my_stories/'),
}

// Search API
export const searchApi = {
  universal: (query) => api.get('/search/', { params: { q: query } }),
  people: (query, params = {}) => api.get('/search/people/', { params: { q: query, ...params } }),
  posts: (query, params = {}) => api.get('/search/posts/', { params: { q: query, ...params } }),
  trendingHashtags: () => api.get('/hashtags/trending/'),
}

// Re-export meetings API
export { meetingsApi } from './meetingsApi'

export default api
