const SUPPORTED_THEMES = new Set(['light', 'dark'])
export const THEME_KEY = 'shizuha-connect-theme'

/**
 * CON-274 QA seam + CON-285 user preference.
 * Tests: window.__SHIZUHA_CONNECT_SET_THEME__('dark' | 'light')
 */
export function readStoredTheme() {
  try {
    const stored = localStorage.getItem(THEME_KEY) || localStorage.getItem('shizuha_theme')
    if (stored === 'dark' || stored === 'light') return stored
  } catch {
    /* private mode */
  }
  try {
    if (window.matchMedia('(prefers-color-scheme: dark)').matches) return 'dark'
  } catch {
    /* no matchMedia */
  }
  return 'light'
}

export function setConnectTheme(theme, { persist = true } = {}) {
  if (!SUPPORTED_THEMES.has(theme)) {
    throw new TypeError(`Unsupported Connect theme: ${theme}`)
  }

  const root = document.documentElement
  root.classList.toggle('dark', theme === 'dark')
  root.classList.toggle('light', theme === 'light')
  root.dataset.theme = theme
  if (persist) {
    try {
      localStorage.setItem(THEME_KEY, theme)
    } catch {
      /* private mode */
    }
  }
  return theme
}

export function initConnectTheme() {
  return setConnectTheme(readStoredTheme(), { persist: false })
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  window.__SHIZUHA_CONNECT_SET_THEME__ = (theme) => setConnectTheme(theme, { persist: true })
  initConnectTheme()
}
