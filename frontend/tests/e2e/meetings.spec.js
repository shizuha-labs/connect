import { test, expect, uniqueId } from './fixtures'

/**
 * E2E tests for video meeting functionality
 *
 * Note: Full video/audio tests require LiveKit server to be running.
 * These tests focus on the UI interactions and API integration.
 */

test.describe('Meetings', () => {
  test.describe('Meetings List Page', () => {
    test('should display meetings page', async ({ authenticatedPage }) => {
      await authenticatedPage.goto('/connect/meetings')
      await authenticatedPage.waitForLoadState('networkidle')

      // Should show meetings header
      await expect(authenticatedPage.locator('h1:has-text("Meetings")')).toBeVisible()

      // Should show action buttons
      await expect(authenticatedPage.locator('button:has-text("New Meeting")')).toBeVisible()
      await expect(authenticatedPage.locator('button:has-text("Schedule")')).toBeVisible()
      await expect(authenticatedPage.locator('button:has-text("Join Meeting")')).toBeVisible()
    })

    test('should show empty state when no meetings', async ({ authenticatedPage }) => {
      await authenticatedPage.goto('/connect/meetings')
      await authenticatedPage.waitForLoadState('networkidle')

      // May show empty state or list depending on existing meetings
      const emptyState = authenticatedPage.locator('text=No meetings yet')
      const meetingsList = authenticatedPage.locator('[class*="space-y"]')

      // Either empty state or meetings list should be present
      const hasEmptyState = await emptyState.isVisible().catch(() => false)
      const hasMeetings = await meetingsList.isVisible().catch(() => false)

      expect(hasEmptyState || hasMeetings).toBeTruthy()
    })

    test('should open schedule modal', async ({ authenticatedPage }) => {
      await authenticatedPage.goto('/connect/meetings')
      await authenticatedPage.waitForLoadState('networkidle')

      // Click schedule button
      await authenticatedPage.click('button:has-text("Schedule")')

      // Modal should appear
      await expect(authenticatedPage.locator('h2:has-text("Schedule Meeting")')).toBeVisible()

      // Form fields should be present
      await expect(authenticatedPage.locator('input[placeholder="Team Standup"]')).toBeVisible()
      await expect(authenticatedPage.locator('input[type="datetime-local"]').first()).toBeVisible()
    })

    test('should create scheduled meeting', async ({ authenticatedPage }) => {
      await authenticatedPage.goto('/connect/meetings')
      await authenticatedPage.waitForLoadState('networkidle')

      // Open schedule modal
      await authenticatedPage.click('button:has-text("Schedule")')

      // Fill form
      const title = `Test Meeting ${uniqueId()}`
      await authenticatedPage.fill('input[placeholder="Team Standup"]', title)

      // Set start time (1 hour from now)
      const startTime = new Date()
      startTime.setHours(startTime.getHours() + 1)
      const startTimeStr = startTime.toISOString().slice(0, 16)
      await authenticatedPage.fill('input[type="datetime-local"]', startTimeStr)

      // Submit
      await authenticatedPage.click('button:has-text("Create Meeting")')

      // Wait for modal to close
      await authenticatedPage.waitForSelector('h2:has-text("Schedule Meeting")', {
        state: 'hidden',
        timeout: 10000
      })

      // Meeting should appear in list
      await expect(authenticatedPage.locator(`text=${title}`)).toBeVisible({ timeout: 5000 })
    })
  })

  test.describe('Join Meeting Page', () => {
    test('should display join meeting page', async ({ authenticatedPage }) => {
      await authenticatedPage.goto('/connect/meetings/join')
      await authenticatedPage.waitForLoadState('networkidle')

      // Should show join header
      await expect(authenticatedPage.locator('h1:has-text("Join Meeting")')).toBeVisible()

      // Should show meeting code input
      await expect(authenticatedPage.locator('input[placeholder="abc-defg-hij"]')).toBeVisible()

      // Should show join button
      await expect(authenticatedPage.locator('button:has-text("Join Meeting")')).toBeVisible()
    })

    test('should format meeting code as user types', async ({ authenticatedPage }) => {
      await authenticatedPage.goto('/connect/meetings/join')
      await authenticatedPage.waitForLoadState('networkidle')

      const input = authenticatedPage.locator('input[placeholder="abc-defg-hij"]')

      // Type without hyphens
      await input.type('abcdefghijk')

      // Should auto-format
      await expect(input).toHaveValue('abc-defg-hij')
    })

    test('should show error for invalid meeting code', async ({ authenticatedPage }) => {
      await authenticatedPage.goto('/connect/meetings/join')
      await authenticatedPage.waitForLoadState('networkidle')

      const input = authenticatedPage.locator('input[placeholder="abc-defg-hij"]')

      // Enter valid format but non-existent meeting
      await input.fill('xxx-yyyy-zzz')

      // Click join
      await authenticatedPage.click('button:has-text("Join Meeting")')

      // Should show error
      await expect(authenticatedPage.locator('text=Meeting not found')).toBeVisible({
        timeout: 10000
      })
    })

    test('should navigate back to meetings list', async ({ authenticatedPage }) => {
      await authenticatedPage.goto('/connect/meetings/join')
      await authenticatedPage.waitForLoadState('networkidle')

      // Click back link
      await authenticatedPage.click('text=Back to Meetings')

      // Should be on meetings page
      await expect(authenticatedPage).toHaveURL(/.*meetings$/)
    })
  })

  test.describe('Navigation', () => {
    test('should have meetings in navigation', async ({ authenticatedPage }) => {
      await authenticatedPage.goto('/connect/')
      await authenticatedPage.waitForLoadState('networkidle')

      // Should have meetings icon/link in nav
      const meetingsLink = authenticatedPage.locator('a[href="/meetings"]')
      await expect(meetingsLink).toBeVisible()
    })

    test('should navigate to meetings from nav', async ({ authenticatedPage }) => {
      await authenticatedPage.goto('/connect/')
      await authenticatedPage.waitForLoadState('networkidle')

      // Click meetings nav item
      await authenticatedPage.click('a[href="/meetings"]')

      // Should be on meetings page
      await expect(authenticatedPage).toHaveURL(/.*meetings/)
      await expect(authenticatedPage.locator('h1:has-text("Meetings")')).toBeVisible()
    })

    test('should navigate from meetings to join', async ({ authenticatedPage }) => {
      await authenticatedPage.goto('/connect/meetings')
      await authenticatedPage.waitForLoadState('networkidle')

      // Click join button
      await authenticatedPage.click('button:has-text("Join Meeting")')

      // Should be on join page
      await expect(authenticatedPage).toHaveURL(/.*meetings\/join/)
    })
  })

  test.describe('API Integration', () => {
    test('should list meetings via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.get('meetings/')

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data).toHaveProperty('results')
      expect(Array.isArray(data.results)).toBeTruthy()
    })

    test('should create meeting via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.post('meetings/', {
        data: {
          title: `API Test Meeting ${uniqueId()}`,
          description: 'Created via E2E test'
        }
      })

      expect(response.ok()).toBeTruthy()

      const meeting = await response.json()
      expect(meeting).toHaveProperty('id')
      expect(meeting).toHaveProperty('meeting_code')
      expect(meeting.meeting_code).toMatch(/^[a-z0-9]{3}-[a-z0-9]{4}-[a-z0-9]{3}$/)
    })

    test('should create instant meeting via API', async ({ authenticatedApi }) => {
      const response = await authenticatedApi.post('meetings/instant/', {
        data: {
          title: 'Instant API Test'
        }
      })

      expect(response.ok()).toBeTruthy()

      const data = await response.json()
      expect(data).toHaveProperty('token')
      expect(data).toHaveProperty('url')
      expect(data).toHaveProperty('meeting')
      expect(data.meeting.status).toBe('active')
    })

    test('should get meeting participants', async ({ authenticatedApi }) => {
      // Create a meeting first
      const createResponse = await authenticatedApi.post('meetings/instant/', {
        data: { title: 'Participants Test' }
      })
      const { meeting } = await createResponse.json()

      // Get participants
      const response = await authenticatedApi.get(`meetings/${meeting.id}/participants/`)

      expect(response.ok()).toBeTruthy()

      const participants = await response.json()
      expect(Array.isArray(participants)).toBeTruthy()
      // Host should be a participant
      expect(participants.length).toBeGreaterThanOrEqual(1)
    })

    test('should end meeting via API', async ({ authenticatedApi }) => {
      // Create an instant meeting
      const createResponse = await authenticatedApi.post('meetings/instant/', {
        data: { title: 'End Test Meeting' }
      })
      const { meeting } = await createResponse.json()

      // End the meeting
      const endResponse = await authenticatedApi.post(`meetings/${meeting.id}/end/`)

      expect(endResponse.ok()).toBeTruthy()

      const endedMeeting = await endResponse.json()
      expect(endedMeeting.status).toBe('ended')
    })
  })
})
