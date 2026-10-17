import { Outlet } from 'react-router-dom'
import { useAuth } from '../../contexts/AuthContext'

function ProtectedRoute({ children }) {
  const { isAuthenticated, isLoading } = useAuth()

  if (isLoading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600 dark:border-primary-400"></div>
      </div>
    )
  }

  if (!isAuthenticated) {
    const currentUrl = window.location.pathname + window.location.search
    window.location.href = `/id/login?continue=${encodeURIComponent(currentUrl)}`
    return null
  }

  return children || <Outlet />
}

export default ProtectedRoute
