import { useEffect, useState } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/common/Layout'
import ProtectedRoute from './components/common/ProtectedRoute'
import Home from './pages/Home'
import Profile from './pages/Profile'
import Messages from './pages/Messages'
import Search from './pages/Search'
import HashtagFeed from './pages/HashtagFeed'
import Notifications from './pages/Notifications'
import Meetings from './pages/Meetings'
import JoinMeeting from './pages/JoinMeeting'
import MeetingRoom from './pages/MeetingRoom'
import { ShizuhaChatWidget } from './components/chat'
import { useAuth } from './contexts/AuthContext'

function GuildDestination() {
  const url = import.meta.env.VITE_GUILD_URL || '/guild/'
  return <section className="rounded-2xl border border-gray-200 bg-white p-8 dark:border-gray-700 dark:bg-gray-900">
    <h1 className="text-2xl font-semibold text-gray-900 dark:text-gray-100">Find jobs and companies in Guild</h1>
    <p className="mt-3 text-gray-600 dark:text-gray-300">Explore opportunities and employer profiles in Guild.</p>
    <a href={url} className="mt-5 inline-flex rounded-xl bg-primary-600 px-5 py-3 font-medium text-white hover:bg-primary-500">Open Guild</a>
  </section>
}

function App() {
  const { isAuthenticated } = useAuth()
  const [theme, setTheme] = useState(() => (
    document.documentElement.classList.contains('dark') ? 'dark' : 'light'
  ))

  useEffect(() => {
    const syncTheme = () => setTheme(
      document.documentElement.classList.contains('dark') ? 'dark' : 'light'
    )
    const observer = new MutationObserver(syncTheme)
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
    return () => observer.disconnect()
  }, [])

  return (
    <>
      <Routes>
        <Route path="meeting/:meetingCode" element={<ProtectedRoute><MeetingRoom /></ProtectedRoute>} />

        <Route path="/" element={<Layout />}>
          <Route path="jobs/*" element={<GuildDestination />} />
          <Route path="companies/*" element={<GuildDestination />} />
          <Route element={<ProtectedRoute />}>
            <Route index element={<Home />} />
            <Route path="profile/:username" element={<Profile />} />
            <Route path="messages" element={<Messages />} />
            <Route path="messages/:conversationId" element={<Messages />} />
            <Route path="search" element={<Search />} />
            <Route path="hashtag/:tag" element={<HashtagFeed />} />
            <Route path="notifications" element={<Notifications />} />
            <Route path="meetings" element={<Meetings />} />
            <Route path="meetings/join" element={<JoinMeeting />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Route>
      </Routes>
      {isAuthenticated ? (
        <ShizuhaChatWidget
          sourceService="connect"
          theme={theme}
          welcomeMessage="Hello! I can help you with social features, connections, and posts."
          showToolCalls={true}
        />
      ) : null}
    </>
  )
}

export default App
