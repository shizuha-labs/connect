import { useEffect, useState } from 'react'
import { Moon, Sun } from 'lucide-react'
import { setConnectTheme } from '../../theme'

const FOCUS_RING =
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-primary-500 dark:focus-visible:ring-primary-400 focus-visible:ring-offset-white dark:focus-visible:ring-offset-gray-950'

export default function ThemeToggle({ className = '' }) {
  const [isDark, setIsDark] = useState(
    () => typeof document !== 'undefined' && document.documentElement.classList.contains('dark'),
  )

  useEffect(() => {
    const root = document.documentElement
    const sync = () => setIsDark(root.classList.contains('dark'))
    const observer = new MutationObserver(sync)
    observer.observe(root, { attributes: true, attributeFilter: ['class'] })
    return () => observer.disconnect()
  }, [])

  const toggle = () => {
    const next = document.documentElement.classList.contains('dark') ? 'light' : 'dark'
    setConnectTheme(next, { persist: true })
  }

  return (
    <button
      type="button"
      onClick={toggle}
      className={`inline-flex items-center justify-center min-h-11 min-w-11 rounded-lg text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 ${FOCUS_RING} ${className}`}
      aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
      title={isDark ? 'Light mode' : 'Dark mode'}
    >
      {isDark ? <Sun className="w-5 h-5" aria-hidden="true" /> : <Moon className="w-5 h-5" aria-hidden="true" />}
    </button>
  )
}
