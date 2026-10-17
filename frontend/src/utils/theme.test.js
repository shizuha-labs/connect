import assert from 'node:assert/strict'
import { after, before, test } from 'node:test'

const store = {}
const classes = new Set()

before(async () => {
  globalThis.document = {
    documentElement: {
      classList: {
        toggle(name, on) {
          if (on) classes.add(name)
          else classes.delete(name)
        },
        contains(name) {
          return classes.has(name)
        },
        add(name) {
          classes.add(name)
        },
        remove(name) {
          classes.delete(name)
        },
      },
      dataset: {},
    },
  }
  globalThis.localStorage = {
    getItem: (key) => (Object.hasOwn(store, key) ? store[key] : null),
    setItem: (key, value) => {
      store[key] = String(value)
    },
    removeItem: (key) => {
      delete store[key]
    },
  }
  globalThis.window = globalThis
  globalThis.matchMedia = () => ({
    matches: false,
    addEventListener() {},
    removeEventListener() {},
  })
})

after(() => {
  classes.clear()
  for (const key of Object.keys(store)) delete store[key]
})

test('setConnectTheme persists dark and stamps the root class', async () => {
  const { setConnectTheme, THEME_KEY } = await import('../theme.js')
  assert.equal(setConnectTheme('dark'), 'dark')
  assert.equal(document.documentElement.classList.contains('dark'), true)
  assert.equal(document.documentElement.dataset.theme, 'dark')
  assert.equal(localStorage.getItem(THEME_KEY), 'dark')
  setConnectTheme('light')
  assert.equal(document.documentElement.classList.contains('dark'), false)
  assert.equal(localStorage.getItem(THEME_KEY), 'light')
})
