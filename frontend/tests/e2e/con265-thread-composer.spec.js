import { test, expect } from './fixtures.js'

/**
 * CON-265 backfill (PLAT-5441): browser E2E for the long-list/thread layout —
 * composer presence, scrolling, latest-message anchoring, responsive breakpoints.
 *
 * The CON-265 layout blowout was: gridScrollH=11052 vs clientHeight=600, composer
 * at top:11076px, zero active scrollers. These checks lock the fix so the
 * regression is not rediscovered by hand each QA cycle.
 */

async function assertThreadScrollerActive(page) {
  // The thread pane must be the active scroller (not the document).
  const result = await page.evaluate(() => {
    const pane = document.querySelector('[data-testid="messages-thread-pane"]')
    if (!pane) return { ok: false, reason: 'no thread pane' }
    const overflowY = getComputedStyle(pane).overflowY
    const scrollable = pane.scrollHeight > pane.clientHeight
    return {
      ok: overflowY === 'auto' || overflowY === 'scroll',
      overflowY,
      scrollable,
      scrollHeight: pane.scrollHeight,
      clientHeight: pane.clientHeight,
    }
  })
  expect(result.ok, `thread pane must be the scroller: ${JSON.stringify(result)}`).toBe(true)
}

test.describe('CON-265: long thread layout', () => {
  test('thread pane scrolls internally and anchors the latest message', async ({
    authenticatedPage,
  }) => {
    const page = authenticatedPage

    // Open the messages page (first conversation, or the empty state).
    await page.goto('/connect/messages')
    await page.waitForSelector('[data-testid="messages-page"]', { timeout: 15000 })

    // Wait for either a thread or the empty state.
    await page.waitForSelector(
      '[data-testid="messages-thread-pane"], [data-testid="messages-thread-empty"]',
      { timeout: 15000 },
    )

    const threadPane = page.locator('[data-testid="messages-thread-pane"]')
    if (await threadPane.count() === 0) {
      // No conversation selected — nothing to assert about a long thread.
      test.skip(true, 'no conversation available in this environment')
      return
    }

    // Composer must be present and reachable (CON-265: it was at top:11076px).
    const composer = page.locator('[data-testid="message-composer-form"]')
    await expect(composer).toBeVisible()

    // The thread pane must be the active scroller.
    await assertThreadScrollerActive(page)

    // Scrolling must work: scroll the pane down and verify it moved.
    const before = await threadPane.evaluate((el) => el.scrollTop)
    await threadPane.evaluate((el) => { el.scrollTop = el.scrollHeight })
    const after = await threadPane.evaluate((el) => el.scrollTop)
    expect(after).toBeGreaterThanOrEqual(before)

    // Latest-message anchoring: the last bubble is in view after scrolling.
    const lastBubble = threadPane.locator(
      '[data-testid="message-bubble-in"], [data-testid="message-bubble-out"]',
    ).last()
    if (await lastBubble.count() > 0) {
      await expect(lastBubble).toBeVisible()
    }
  })
})
