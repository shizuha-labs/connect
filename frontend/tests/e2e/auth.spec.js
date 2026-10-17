import { test, expect, TEST_USER } from './fixtures.js'

/**
 * Authentication tests for Shizuha Connect
 *
 * The login flow works as follows:
 * 1. User visits /connect/ (unauthenticated)
 * 2. ProtectedRoute redirects to /id/login?continue=/connect/
 * 3. User logs in at Shizuha ID (/id/login)
 * 4. After successful login, redirects back to /connect/
 */

test.describe('Authentication', () => {

  test.describe('Unified Login Flow', () => {

    test('should redirect from /connect/ to /id/login when unauthenticated', async ({ page }) => {
      // Clear any existing auth state
      await page.goto('/id/login')
      await page.evaluate(() => {
        localStorage.clear()
      })

      // Try to access Connect directly
      await page.goto('/connect/')

      // Should redirect to Shizuha ID login with continue parameter
      await page.waitForURL(/.*\/id\/login.*continue/, { timeout: 15000 })

      // Verify we're on the login page
      await expect(page.locator('h1')).toContainText('Shizuha ID')
    })

    test('should login with valid credentials and redirect to Connect', async ({ page }) => {
      // Start at Shizuha ID login with continue parameter
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

      // The redirect happens via window.location.href, wait for it
      await page.waitForURL(/.*connect.*/, { timeout: 30000 })

      // Wait for page to load
      await page.waitForLoadState('domcontentloaded', { timeout: 15000 })

      // Verify we're on the Connect home page - check for feed elements
      await expect(page.locator('button, div').filter({ hasText: /For You|Following|Trending/i }).first()).toBeVisible({ timeout: 15000 })
    })

    test('should show error with invalid credentials', async ({ page }) => {
      await page.goto('/id/login')

      // Fill in wrong credentials
      await page.fill('input[id="username"]', 'wronguser')
      await page.fill('input[id="password"]', 'wrongpassword')

      // Submit form
      await page.click('button[type="submit"]')

      // Wait for error response from API
      await page.waitForResponse(response =>
        response.url().includes('/auth/login/') && response.status() >= 400,
        { timeout: 10000 }
      )

      // Wait a moment for state update
      await page.waitForTimeout(1000)

      // Should show error message
      await expect(
        page.locator('div').filter({ hasText: /Login failed|No active account|Invalid|credentials/i }).first()
      ).toBeVisible({ timeout: 5000 })

      // Should stay on login page
      await expect(page).toHaveURL(/.*\/id\/login/)
    })

  })

  test.describe('Session Persistence', () => {

    test('should persist authentication across page reload', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Verify we're authenticated
      await page.waitForLoadState('networkidle', { timeout: 15000 })

      // Store current URL
      const currentUrl = page.url()

      // Reload the page
      await page.reload()

      // Should still be authenticated (not redirected to login)
      await page.waitForLoadState('networkidle', { timeout: 15000 })
      await expect(page).not.toHaveURL(/.*\/id\/login/)
    })

    test('should maintain session when navigating between pages', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Navigate to different pages within Connect
      await page.goto('/connect/jobs')
      await page.waitForLoadState('domcontentloaded')
      await expect(page).not.toHaveURL(/.*login/)

      await page.goto('/connect/messages')
      await page.waitForLoadState('domcontentloaded')
      await expect(page).not.toHaveURL(/.*login/)

      await page.goto('/connect/notifications')
      await page.waitForLoadState('domcontentloaded')
      await expect(page).not.toHaveURL(/.*login/)
    })

  })

  test.describe('API Authentication', () => {

    test('should make authenticated API calls without 401 errors', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Listen for API responses
      const apiResponses = []
      page.on('response', response => {
        if (response.url().includes('/connect/api/') || response.url().includes('/id/api/')) {
          apiResponses.push({
            url: response.url(),
            status: response.status()
          })
        }
      })

      // Navigate to trigger some API calls
      await page.goto('/connect/')
      await page.waitForLoadState('networkidle', { timeout: 15000 })

      // Wait for API calls to complete
      await page.waitForTimeout(3000)

      // Check that API calls were made and returned successful status
      const successfulCalls = apiResponses.filter(r => r.status >= 200 && r.status < 400)
      const failedAuth = apiResponses.filter(r => r.status === 401)

      // Log for debugging
      console.log('API responses:', apiResponses)

      // There should be some successful API calls
      expect(successfulCalls.length).toBeGreaterThan(0)

      // There should be no 401 errors after authentication
      expect(failedAuth.length).toBe(0)
    })

    // CON-271: access-expiry mid-session must silently refresh (valid refresh
    // token preserved) and stay on the route — NOT hard-redirect to /id/login.
    // The expired JWT carries exp=1 so the backend rejects it with 401; the
    // interceptor then refreshes via the still-valid refresh token and retries.
    test('should silently refresh on access-token expiry and stay on route', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Forge an expired same-session access token (exp=1) while preserving the
      // valid refresh token in localStorage.
      const expiredJwt = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjF9.invalid'
      await page.evaluate((token) => {
        localStorage.setItem('shizuha_access_token', token)
      }, expiredJwt)

      // Capture API responses to prove hydration happened after silent refresh.
      const apiResponses = []
      page.on('response', response => {
        if (response.url().includes('/connect/api/')) {
          apiResponses.push({ url: response.url(), status: response.status() })
        }
      })

      // Reload /connect/ with the expired access token.
      await page.goto('/connect/')
      await page.waitForLoadState('networkidle', { timeout: 20000 })

      // CON-271 diagnostics: name the exact statuses the backend returned for
      // the post-reload hydration calls and the token state the app left.
      console.log('CON-271 diag [silent-refresh] apiResponses:', JSON.stringify(apiResponses))
      console.log('CON-271 diag [silent-refresh] url:', page.url())
      console.log('CON-271 diag [silent-refresh] access token:', await page.evaluate(() => localStorage.getItem('shizuha_access_token')))
      console.log('CON-271 diag [silent-refresh] refresh token present:', await page.evaluate(() => !!localStorage.getItem('shizuha_refresh_token')))

      // Must NOT be redirected to the ID login page.
      await expect(page).not.toHaveURL(/.*\/id\/login.*/, { timeout: 15000 })

      // The refresh must have minted a new access token (not the forged one).
      const newAccess = await page.evaluate(() => localStorage.getItem('shizuha_access_token'))
      expect(newAccess).not.toBe(expiredJwt)
      expect(newAccess).toBeTruthy()

      // API hydration must have RECOVERED via the silent refresh. The flow's
      // signature is 401 (forged/expired token rejected) -> single-flight
      // refresh -> successful retries of the same endpoints. Assert recovery,
      // not the absence of 401s: every /connect/api/ endpoint that 401'd must
      // eventually be followed by a successful (2xx/3xx) response for it.
      const endpoints401 = new Set(
        apiResponses.filter(r => r.status === 401 && r.url.includes('/connect/api/')).map(r => r.url)
      )
      expect(endpoints401.size).toBeGreaterThan(0)
      for (const url of endpoints401) {
        const recovered = apiResponses.some(r => r.url === url && r.status >= 200 && r.status < 400)
        expect(recovered, `endpoint ${url} was not successfully retried after silent refresh`).toBe(true)
      }
      // And no 401 may arrive AFTER the first successful retry (the recovered
      // session must hold for the rest of hydration).
      const firstSuccessIdx = apiResponses.findIndex(r => r.status >= 200 && r.status < 400 && r.url.includes('/connect/api/'))
      expect(firstSuccessIdx).toBeGreaterThanOrEqual(0)
      const late401s = apiResponses.slice(firstSuccessIdx + 1).filter(r => r.status === 401 && r.url.includes('/connect/api/'))
      expect(late401s.length).toBe(0)
    })

    // CON-271: invalid/missing refresh token must FAIL CLOSED to /id/login —
    // never silently continue with a dead session.
    test('should fail closed to login when refresh token is invalid', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Corrupt BOTH tokens: expired access + invalid refresh.
      const expiredJwt = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjF9.invalid'
      await page.evaluate((token) => {
        localStorage.setItem('shizuha_access_token', token)
        localStorage.setItem('shizuha_refresh_token', 'invalid-refresh-token')
      }, expiredJwt)

      // Capture API responses to prove the fail-closed path actually fired.
      const apiResponses = []
      page.on('response', response => {
        if (response.url().includes('/connect/api/') || response.url().includes('/id/api/')) {
          apiResponses.push({ url: response.url(), status: response.status() })
        }
      })

      // Reload /connect/ — the refresh attempt must fail and redirect to login.
      await page.goto('/connect/')
      await page.waitForURL(/.*\/id\/login.*/, { timeout: 20000 }).catch(() => {
        console.log('CON-271 diag [fail-closed] no login redirect within 20s')
      })
      console.log('CON-271 diag [fail-closed] url:', page.url())
      console.log('CON-271 diag [fail-closed] apiResponses:', JSON.stringify(apiResponses))
      console.log('CON-271 diag [fail-closed] access token:', await page.evaluate(() => localStorage.getItem('shizuha_access_token')))
      await expect(page).toHaveURL(/.*\/id\/login.*/, { timeout: 5000 })

      // Session must be cleared (fail-closed).
      const access = await page.evaluate(() => localStorage.getItem('shizuha_access_token'))
      const refresh = await page.evaluate(() => localStorage.getItem('shizuha_refresh_token'))
      expect(access).toBeNull()
      expect(refresh).toBeNull()
    })

    test('should authenticate via API fixture', async ({ authenticatedApi }) => {
      // Test that authenticatedApi fixture works - use posts endpoint since profiles needs username
      const response = await authenticatedApi.get('posts/')

      expect(response.ok()).toBeTruthy()
      const data = await response.json()
      expect(data).toBeDefined()
    })

  })

  test.describe('Feed Access', () => {

    test('should load home feed after authentication', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Wait for feed to load
      await page.waitForLoadState('networkidle', { timeout: 15000 })

      // Check for feed tab buttons
      await expect(page.locator('button').filter({ hasText: 'For You' })).toBeVisible({ timeout: 10000 })
      await expect(page.locator('button').filter({ hasText: 'Following' })).toBeVisible({ timeout: 10000 })
      await expect(page.locator('button').filter({ hasText: 'Trending' })).toBeVisible({ timeout: 10000 })
    })

    test('should switch between feed types', async ({ authenticatedPage }) => {
      const page = authenticatedPage

      // Wait for feed to load
      await page.waitForLoadState('networkidle', { timeout: 15000 })

      // Click Following tab
      await page.click('button:has-text("Following")')
      await page.waitForTimeout(500)

      // Click Trending tab
      await page.click('button:has-text("Trending")')
      await page.waitForTimeout(500)

      // Click For You tab
      await page.click('button:has-text("For You")')
      await page.waitForTimeout(500)

      // Should still be on Connect (not redirected)
      await expect(page).toHaveURL(/.*connect.*/)
    })

    test('should get feed data via API', async ({ authenticatedApi }) => {
      // Test home feed
      const homeResponse = await authenticatedApi.get('feed/home/')
      expect(homeResponse.ok()).toBeTruthy()

      // Test following feed
      const followingResponse = await authenticatedApi.get('feed/following/')
      expect(followingResponse.ok()).toBeTruthy()

      // Test trending feed
      const trendingResponse = await authenticatedApi.get('feed/trending/')
      expect(trendingResponse.ok()).toBeTruthy()
    })

  })

})
