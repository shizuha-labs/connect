import { useEffect, useId, useRef, useState } from 'react'
import { Outlet, Link, useLocation, useNavigate } from 'react-router-dom'
import {
  Home,
  Search,
  Briefcase,
  MessageSquare,
  Bell,
  User,
  Video,
  BrainCircuit,
  Users,
  MoreHorizontal,
  X,
} from 'lucide-react'
import AppSwitcher from './AppSwitcher'
import ThemeToggle from './ThemeToggle'
import { useEnabledServices } from '@shizuha/ui'
import { useAuth } from '../../contexts/AuthContext'

const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-primary-500 dark:focus-visible:ring-primary-400 focus-visible:ring-offset-white dark:focus-visible:ring-offset-gray-950'

/** Primary mobile destinations per CON-278 / wiki §4.5 */
const MOBILE_TABS = [
  { path: '/', icon: Home, label: 'Feed', match: (p) => p === '/' },
  { path: '/search', icon: Users, label: 'Network', match: (p) => p.startsWith('/search') || p.startsWith('/hashtag') },
  { path: '/messages', icon: MessageSquare, label: 'Messages', match: (p) => p.startsWith('/messages') },
  { path: '/notifications', icon: Bell, label: 'Alerts', match: (p) => p.startsWith('/notifications') },
  { path: '/profile/me', icon: User, label: 'Me', match: (p) => p.startsWith('/profile') },
]

/** Secondary destinations opened from the discovery sheet */
const DISCOVERY_ITEMS = [
  { path: '/search', icon: Search, label: 'Search' },
  { path: '/meetings', icon: Video, label: 'Meetings' },
  { path: import.meta.env.VITE_GUILD_URL || `${window.location.origin}/guild/`, icon: Briefcase, label: 'Guild' },
  // Dojo is now a standalone SPA at shizuha.com/dojo (decoupled from Connect).
  { path: 'https://shizuha.com/dojo', icon: BrainCircuit, label: 'Dojo' },
]

function Layout() {
  const location = useLocation()
  const navigate = useNavigate()
  const enabledServices = useEnabledServices()
  const { isAuthenticated } = useAuth()
  const continuePath = `/connect${location.pathname}${location.search}`
  const loginHref = `/id/login?continue=${encodeURIComponent(continuePath)}`
  const [sheetOpen, setSheetOpen] = useState(false)
  const sheetTitleId = useId()
  const closeBtnRef = useRef(null)

  const desktopNav = [
    { path: '/', icon: Home, label: 'Feed' },
    { path: '/search', icon: Users, label: 'Network' },
    { path: '/messages', icon: MessageSquare, label: 'Messages' },
    { path: '/notifications', icon: Bell, label: 'Alerts' },
    { path: '/meetings', icon: Video, label: 'Meetings' },
    { path: import.meta.env.VITE_GUILD_URL || `${window.location.origin}/guild/`, icon: Briefcase, label: 'Guild' },
    // Dojo is now a standalone SPA at shizuha.com/dojo (decoupled from Connect).
    { path: 'https://shizuha.com/dojo', icon: BrainCircuit, label: 'Dojo' },
  ]

  useEffect(() => {
    // Close discovery sheet on route change
    setSheetOpen(false)
  }, [location.pathname])

  useEffect(() => {
    if (!sheetOpen) return undefined
    const onKey = (e) => {
      if (e.key === 'Escape') setSheetOpen(false)
    }
    document.addEventListener('keydown', onKey)
    // Focus close control when sheet opens
    closeBtnRef.current?.focus()
    return () => document.removeEventListener('keydown', onKey)
  }, [sheetOpen])

  const isActivePath = (path) =>
    location.pathname === path || (path !== '/' && location.pathname.startsWith(path))

  return (
    <div className="min-h-screen bg-gray-100 dark:bg-gray-950 overflow-x-hidden">
      {/* Desktop header — hidden below lg */}
      <header className="hidden lg:block bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700 sticky top-0 z-50">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between items-center h-14">
            <div className="flex items-center space-x-2">
              <AppSwitcher currentAppId="connect" enabledServices={enabledServices} />
              <Link
                to="/"
                className={`flex items-center space-x-2 rounded-lg ${FOCUS_RING}`}
                aria-label="Connect home"
              >
                <div className="w-8 h-8 bg-primary-600 dark:bg-primary-500 rounded-lg flex items-center justify-center">
                  <span className="text-white dark:text-gray-950 font-bold text-lg" aria-hidden="true">
                    C
                  </span>
                </div>
                <span className="font-semibold text-gray-900 dark:text-gray-100">Connect</span>
              </Link>
            </div>

            <div className="flex-1 max-w-md mx-4">
              <label className="relative block">
                <span className="sr-only">Search Connect</span>
                <Search
                  className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 dark:text-gray-500 pointer-events-none"
                  aria-hidden="true"
                />
                <input
                  type="search"
                  placeholder="Search..."
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      const q = e.currentTarget.value.trim()
                      navigate(q ? `/search?q=${encodeURIComponent(q)}` : '/search')
                    }
                  }}
                  className={`w-full pl-10 pr-4 py-2 bg-gray-100 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg text-sm text-gray-900 dark:text-gray-100 ${FOCUS_RING}`}
                />
              </label>
            </div>

            <nav className="flex items-center space-x-1" aria-label="Primary">
              {desktopNav.map((item) => {
                const Icon = item.icon
                const isActive = isActivePath(item.path)
                return (
                  <Link
                    key={item.path + item.label}
                    to={item.path}
                    aria-label={item.label}
                    aria-current={isActive ? 'page' : undefined}
                    className={`p-2 min-h-11 min-w-11 inline-flex items-center justify-center rounded-lg transition-colors motion-safe:transition-colors ${FOCUS_RING} ${
                      isActive
                        ? 'text-primary-600 dark:text-primary-400 bg-primary-50 dark:bg-primary-900/20'
                        : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800'
                    }`}
                    title={item.label}
                  >
                    <Icon className="w-5 h-5" aria-hidden="true" />
                  </Link>
                )
              })}

              <ThemeToggle />
              {isAuthenticated ? (
              <Link
                to="/profile/me"
                aria-label="My profile"
                aria-current={location.pathname.startsWith('/profile') ? 'page' : undefined}
                className={`ml-2 p-1 min-h-11 min-w-11 inline-flex items-center justify-center rounded-full hover:bg-gray-100 dark:hover:bg-gray-800 ${FOCUS_RING}`}
              >
                <div className="w-8 h-8 bg-gray-300 dark:bg-gray-700 rounded-full flex items-center justify-center">
                  <User className="w-4 h-4 text-gray-600 dark:text-gray-400" aria-hidden="true" />
                </div>
              </Link>
              ) : (
                <a
                  href={loginHref}
                  className={`ml-2 px-3 py-1.5 text-sm font-medium text-primary-600 dark:text-primary-400 rounded-lg ${FOCUS_RING}`}
                >
                  Sign in
                </a>
              )}
            </nav>
          </div>
        </div>
      </header>

      {/* Mobile top bar — logo + discovery only */}
      <header className="lg:hidden bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700 sticky top-0 z-50">
        <div className="flex items-center justify-between h-12 px-3">
          <div className="flex items-center gap-2 min-w-0">
            <AppSwitcher currentAppId="connect" enabledServices={enabledServices} />
            <Link to="/" className={`flex items-center gap-2 rounded-lg ${FOCUS_RING}`} aria-label="Connect home">
              <div className="w-7 h-7 bg-primary-600 dark:bg-primary-500 rounded-lg flex items-center justify-center">
                <span className="text-white dark:text-gray-950 font-bold text-sm" aria-hidden="true">
                  C
                </span>
              </div>
              <span className="font-semibold text-gray-900 dark:text-gray-100 text-sm">Connect</span>
            </Link>
          </div>
          <div className="flex items-center gap-1">
            <ThemeToggle />
            <button
              type="button"
              onClick={() => setSheetOpen(true)}
              className={`inline-flex items-center justify-center min-h-11 min-w-11 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 ${FOCUS_RING}`}
              aria-haspopup="dialog"
              aria-expanded={sheetOpen}
              aria-label="Open discovery"
            >
              <MoreHorizontal className="w-5 h-5" aria-hidden="true" />
            </button>
          </div>
        </div>
      </header>

      {/* Main Content — pb for mobile tab bar */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6 pb-20 lg:pb-6 min-w-0">
        <Outlet />
      </main>

      {/* Mobile bottom tabs */}
      <nav
        className="lg:hidden fixed bottom-0 inset-x-0 z-50 bg-white dark:bg-gray-900 border-t border-gray-200 dark:border-gray-700"
        aria-label="Mobile primary"
      >
        <ul className="grid grid-cols-5 h-16">
          {MOBILE_TABS.map((item) => {
            const Icon = item.icon
            const isActive = item.match(location.pathname)
            return (
              <li key={item.label} className="min-w-0">
                <Link
                  to={item.path}
                  aria-label={item.label}
                  aria-current={isActive ? 'page' : undefined}
                  className={`flex flex-col items-center justify-center h-full min-h-11 gap-0.5 text-[10px] font-medium ${FOCUS_RING} ${
                    isActive
                      ? 'text-primary-600 dark:text-primary-400'
                      : 'text-gray-500 dark:text-gray-400'
                  }`}
                >
                  <Icon className="w-5 h-5 shrink-0" aria-hidden="true" />
                  <span className="truncate max-w-full px-0.5">{item.label}</span>
                </Link>
              </li>
            )
          })}
        </ul>
      </nav>

      {/* Discovery sheet */}
      {sheetOpen && (
        <div
          className="lg:hidden fixed inset-0 z-[60] flex items-end"
          role="dialog"
          aria-modal="true"
          aria-labelledby={sheetTitleId}
        >
          <button
            type="button"
            className="absolute inset-0 bg-black/40"
            aria-label="Dismiss discovery"
            onClick={() => setSheetOpen(false)}
          />
          <div className="relative w-full max-h-[70vh] overflow-y-auto rounded-t-2xl bg-white dark:bg-gray-900 border-t border-gray-200 dark:border-gray-700 p-4 pb-8 shadow-xl">
            <div className="flex items-center justify-between mb-3">
              <h2 id={sheetTitleId} className="text-base font-semibold text-gray-900 dark:text-gray-100">
                Discover
              </h2>
              <button
                ref={closeBtnRef}
                type="button"
                onClick={() => setSheetOpen(false)}
                className={`inline-flex items-center justify-center min-h-11 min-w-11 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 ${FOCUS_RING}`}
                aria-label="Close discovery"
              >
                <X className="w-5 h-5" aria-hidden="true" />
              </button>
            </div>
            <ul className="grid grid-cols-2 gap-2">
              {DISCOVERY_ITEMS.map((item) => {
                const Icon = item.icon
                return (
                  <li key={item.path + item.label}>
                    <Link
                      to={item.path}
                      onClick={() => setSheetOpen(false)}
                      className={`flex items-center gap-3 min-h-11 rounded-xl border border-gray-200 dark:border-gray-700 px-3 py-3 text-sm font-medium text-gray-900 dark:text-gray-100 hover:bg-gray-50 dark:hover:bg-gray-800 ${FOCUS_RING}`}
                    >
                      <Icon className="w-5 h-5 text-primary-600 dark:text-primary-400 shrink-0" aria-hidden="true" />
                      {item.label}
                    </Link>
                  </li>
                )
              })}
            </ul>
          </div>
        </div>
      )}
    </div>
  )
}

export default Layout
