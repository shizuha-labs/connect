import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AuthProvider } from './contexts/AuthContext'
import App from './App'
import './theme'
import './index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 1000 * 60 * 5, // 5 minutes
      retry: 1,
    },
  },
})

// Test/evidence seam (CON-278): allow Playwright harnesses to invalidate
// queries in-place so fail-soft (cached list + Retry) can be proven without
// a full reload that wipes component-local cache. Mirrors the theme hook in
// theme.js (`window.__SHIZUHA_CONNECT_SET_THEME__`).
if (typeof window !== 'undefined') {
  window.__SHIZUHA_CONNECT_QUERY_CLIENT__ = queryClient
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <BrowserRouter basename="/connect">
          <App />
        </BrowserRouter>
      </AuthProvider>
    </QueryClientProvider>
  </React.StrictMode>,
)
