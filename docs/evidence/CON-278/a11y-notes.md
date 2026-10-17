# CON-278 — Responsive + a11y evidence notes

Historical accessibility checks from a branch preview. Authenticated screenshots
and machine-readable captures are retained privately, outside the source tree.

## Matrix covered

| Viewport | Themes | Routes |
|---|---|---|
| desktop 1440×900 | light, dark | feed, messages list, messages thread |
| mobile 390×844 | light, dark | feed, messages list, messages thread |

State matrix (messages): **ready / empty / loading / error-no-cache / error-degraded (fail-soft)**.

Generated screenshots and reports must not be committed; they can contain private messages.

## Findings (programmatic + visual)

### 1. Contrast (WCAG 2.1 AA, 4.5:1 normal text)

**Result: 0 fails across 4 viewport×theme combos (13 pairs each).**

Method fix vs first pass: earlier harness treated `bg-primary-900/20` as solid `rgb(49,46,129)` and reported a false 3.83 fail on the active tab. Correct measurement alpha-composites translucent backgrounds onto the nearest opaque ancestor (canvas/panel):

| Pair | Dark ratio | Light ratio |
|---|---|---|
| body text on canvas | 18.30 | 16.98 |
| heading on panel | 16.12 | 17.74 |
| secondary text on panel | 6.99 | 4.83 |
| **active tab label on tinted bg** | **5.58** (blended `rgb(23,28,57)`) | **5.62** |
| text-secondary on canvas | 7.93 | 4.63 |

Tokens from CON-274 (`--color-text-primary/secondary`, canvas/panel) hold AA in both themes. No product contrast change required this pass.

### 2. Horizontal overflow

**Result: 0 overflow issues.** `documentElement.scrollWidth ≤ clientWidth` on every matrix cell. Mobile bottom nav present (`nav[aria-label="Mobile primary"]`) at 390px; desktop rail present at 1440px.

### 3. Focus rings

Keyboard focus on primary nav links yields a visible double-ring via Tailwind `FOCUS_RING`:

- light: `box-shadow: white 0 0 0 2px, indigo-500 0 0 0 4px`
- dark: `box-shadow: gray-950 0 0 0 2px, indigo-400 0 0 0 4px`

CSS utility `.connect-focus-ring` also binds to `--color-panel` + `--color-focus` for non-Tailwind surfaces.

### 4. Landmarks / names / current page

| Check | Result |
|---|---|
| Mobile primary nav | `nav[aria-label="Mobile primary"]` with 5 tabs |
| Desktop primary nav | `nav[aria-label="Primary"]` |
| Active tab | `aria-current="page"` on the active item |
| Messages listbox | `role="listbox"` + options `role="option"` + `aria-selected` |
| Thread region | `role="region"` `aria-label="Conversation"` when a thread is open |
| Empty thread | `role="status"` copy (“Select a conversation…”) |
| Reduced motion | `@media (prefers-reduced-motion: reduce)` rule present in stylesheet |

### 5. Messages IA (mobile)

- **List route** (`/connect/messages`): full-width conversation list; no empty dual-pane ghost.
- **Thread route** (`/connect/messages/:id`): full-width thread with back control; list is not a squeezed side column.
- Desktop keeps the dual-pane list | thread layout.

### 6. Fail-soft (error + cached list)

**Proven on all 4 viewport×theme cells + dedicated SPA proof.**

Sequence:

1. Load `/connect/messages` successfully → populated `role="option"` rows; component-local `cachedConversations` seeded.
2. Route Connect conversation APIs → 503/500.
3. In-place invalidate via test seam `window.__SHIZUHA_CONNECT_QUERY_CLIENT__` (added in `frontend/src/main.jsx`, mirrors CON-274 theme hook). Full reload cannot prove this path because it wipes `useState` cache.

Observed UI:

- Banner: **“Offline copy · Retry”**
- Cached list remains (the existing options)
- Retry button present and wired to `refetchConversations`

Error-no-cache (cold load with API broken) shows compact **“Could not load conversations. / Retry”** — not a full-page dead end. Empty and loading skeletons also captured.

### 7. Discovery sheet (mobile)

Network tab opens the discovery sheet over the feed; bottom nav remains reachable. Private visual evidence was reviewed.

## Product changes in this PR (summary)

- Mobile bottom IA: Feed / Network / Messages / Alerts / Me
- Messages list↔detail full-width on small screens; desktop dual-pane retained
- Fail-soft conversations: keep last good list + “Offline copy · Retry”
- Shared `FOCUS_RING` + `.connect-focus-ring` tokenized rings
- `aria-current="page"` on active nav items
- Test seam: `window.__SHIZUHA_CONNECT_QUERY_CLIENT__` for in-place RQ invalidation (evidence harness only; no user-facing behavior)

## Residual / out of scope

- Full axe-core automated suite not run (manual landmark + contrast + focus evidence instead).
- Composer / rich-text a11y beyond focus ring not re-audited (CON-265 owns composer scroll).
- Live production roll still pending Origin CI after merge (this evidence is branch preview + live API).

## How to re-run evidence

```bash
# from frontend/
npx vite build --config vite.config.con278.js   # base=/connect/, proxy APIs
npx vite preview --config vite.config.con278.js --host 127.0.0.1 --port 5179
# harness (not shipped): Playwright against preview with agent login
```
