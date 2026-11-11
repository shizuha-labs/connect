import { createContext, useContext, useState, useEffect } from 'react'

const AuthContext = createContext(null)

// localStorage keys (must match Shizuha ID)
const ACCESS_TOKEN_KEY = 'shizuha_access_token'
const REFRESH_TOKEN_KEY = 'shizuha_refresh_token'
const USER_KEY = 'shizuha_user'

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isAuthenticated, setIsAuthenticated] = useState(false)

  useEffect(() => {
    const initAuth = async () => {
      let token = localStorage.getItem(ACCESS_TOKEN_KEY)
      let userData = localStorage.getItem(USER_KEY)

      if (!(token && userData)) {
        // ID-93: bootstrap from the HttpOnly Shizuha-ID session cookies.
        // localStorage alone leaves a fresh load (cleared site data, browser
        // restart, non-page login) bouncing to /id/login despite valid
        // cookies; this same-origin call lets the cookies seed the session.
        try {
          const response = await fetch('/id/api/auth/session-bootstrap/', {
            credentials: 'include',
          })
          if (response.ok) {
            const data = await response.json()
            localStorage.setItem(ACCESS_TOKEN_KEY, data.tokens.access)
            localStorage.setItem(REFRESH_TOKEN_KEY, data.tokens.refresh)
            localStorage.setItem(USER_KEY, JSON.stringify(data.user))
            token = data.tokens.access
            userData = JSON.stringify(data.user)
          }
        } catch {
          // No/invalid cookies — genuinely signed out.
        }
      }

      if (token && userData) {
        try {
          setUser(JSON.parse(userData))
          setIsAuthenticated(true)
        } catch (e) {
          console.error('Failed to parse user data:', e)
          localStorage.removeItem(ACCESS_TOKEN_KEY)
          localStorage.removeItem(REFRESH_TOKEN_KEY)
          localStorage.removeItem(USER_KEY)
        }
      }
      setIsLoading(false)
    }

    initAuth()
  }, [])

  const logout = () => {
    localStorage.removeItem(ACCESS_TOKEN_KEY)
    localStorage.removeItem(REFRESH_TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
    setUser(null)
    setIsAuthenticated(false)
    // Redirect to Shizuha ID logout
    window.location.href = '/id/logout'
  }

  return (
    <AuthContext.Provider
      value={{
        user,
        isAuthenticated,
        isLoading,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

// Context provider + hook in one file is idiomatic React; fast-refresh's
// only-export-components rule is a false positive for this pattern.
// // The hook intentionally shares the provider's private context.
// eslint-disable-next-line react-refresh/only-export-components
export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}
