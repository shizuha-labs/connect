import { test as base } from '@playwright/test'

/**
 * Test fixtures for Shizuha Connect E2E tests
 * Provides authenticated user context, test data helpers, and API access
 *
 * Authentication flow:
 * - Login happens at /id/login (Shizuha ID)
 * - After login, user is redirected to /connect/ (Shizuha Connect)
 * - Tokens are stored in localStorage with shared keys
 */

// Test user credentials (same as other Shizuha services)
export const TEST_USER = {
  username: 'testuser',
  password: 'TestPass123',
  email: 'testuser@example.com'
}

// Extend base test with authentication and data fixtures
export const test = base.extend({
  // Authenticated page fixture - logs in via Shizuha ID
  authenticatedPage: async ({ page }, use) => {
    // Go to Shizuha ID login page with continue parameter
    await page.goto('/id/login?continue=/connect/')

    // Wait for form to be ready
    await page.waitForSelector('input[id="username"]', { timeout: 10000 })

    // Fill in credentials
    await page.fill('input[id="username"]', TEST_USER.username)
    await page.fill('input[id="password"]', TEST_USER.password)

    // Click submit
    await page.click('button[type="submit"]')

    // Wait for successful login API response
    await page.waitForResponse(
      response => response.url().includes('/auth/login/') && response.ok(),
      { timeout: 15000 }
    )

    // Wait for redirect to Connect
    await page.waitForURL(/.*connect.*/, { timeout: 30000 })

    // Wait for page to fully load and React to hydrate
    await page.waitForLoadState('networkidle', { timeout: 20000 })
    await page.waitForTimeout(1000)

    // Verify we're on the connect app - check we're not still on login
    const url = page.url()
    if (url.includes('/id/login')) {
      throw new Error('Still on login page after authentication')
    }

    // Use the authenticated page
    await use(page)
  },

  // API context for direct API testing (unauthenticated)
  apiContext: async ({ playwright }, use) => {
    const baseUrl = process.env.BASE_URL || 'http://shizuha-nginx'
    const apiContext = await playwright.request.newContext({
      baseURL: process.env.API_URL || `${baseUrl}/connect/api`,
      extraHTTPHeaders: {
        'Content-Type': 'application/json',
      },
    })

    await use(apiContext)

    await apiContext.dispose()
  },

  // Authenticated API context - uses Shizuha ID for login
  authenticatedApi: async ({ playwright }, use) => {
    const baseUrl = process.env.BASE_URL || 'http://shizuha-nginx'
    const connectApiUrl = process.env.API_URL || `${baseUrl}/connect/api/`
    const idApiUrl = process.env.ID_API_URL || `${baseUrl}/id/api/`

    const apiContext = await playwright.request.newContext({
      baseURL: idApiUrl,
      extraHTTPHeaders: {
        'Content-Type': 'application/json',
      },
    })

    // Login via Shizuha ID to get token
    const loginResponse = await apiContext.post('auth/login/', {
      data: {
        username: TEST_USER.username,
        password: TEST_USER.password
      }
    })

    // Check if login was successful
    if (!loginResponse.ok()) {
      const text = await loginResponse.text()
      throw new Error(`Login failed: ${loginResponse.status()} - ${text.substring(0, 200)}`)
    }

    const { tokens } = await loginResponse.json()

    // Create new context with auth header for Connect API
    const authContext = await playwright.request.newContext({
      baseURL: connectApiUrl,
      extraHTTPHeaders: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${tokens.access}`
      },
    })

    // Store the token for later use
    authContext.token = tokens.access

    await use(authContext)

    await apiContext.dispose()
    await authContext.dispose()
  },

  // Fixture to get current user's profile
  userProfile: async ({ authenticatedApi }, use) => {
    const response = await authenticatedApi.get('profile/')
    let profile = null

    if (response.ok()) {
      profile = await response.json()
    }

    await use(profile)
  },

  // Fixture to get user's posts
  userPosts: async ({ authenticatedApi }, use) => {
    const response = await authenticatedApi.get('posts/')
    let posts = []

    if (response.ok()) {
      const data = await response.json()
      posts = data.results || data || []
    }

    await use(posts)
  },

  // Fixture for feed data
  homeFeed: async ({ authenticatedApi }, use) => {
    const response = await authenticatedApi.get('feed/home/')
    let feed = []

    if (response.ok()) {
      const data = await response.json()
      feed = data.results || data || []
    }

    await use(feed)
  },
})

export { expect } from '@playwright/test'

/**
 * Helper function to create unique test data
 */
export function uniqueId(prefix = 'test') {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`
}

/**
 * Helper to format date as YYYY-MM-DD
 */
export function formatDate(date = new Date()) {
  return date.toISOString().split('T')[0]
}

/**
 * Helper to wait for API response with retries
 */
export async function waitForApiResponse(page, urlPattern, options = {}) {
  const { timeout = 10000, expectedStatus = 200 } = options

  return page.waitForResponse(
    response => {
      const matchesUrl = typeof urlPattern === 'string'
        ? response.url().includes(urlPattern)
        : urlPattern.test(response.url())

      return matchesUrl && response.status() === expectedStatus
    },
    { timeout }
  )
}

/**
 * Helper to create a test post
 */
export async function createTestPost(authenticatedApi, content) {
  const postContent = content || `Test post ${uniqueId()}`
  const response = await authenticatedApi.post('posts/', {
    data: {
      content: postContent
    }
  })

  if (!response.ok()) {
    const text = await response.text()
    throw new Error(`Failed to create post: ${text}`)
  }

  return response.json()
}
