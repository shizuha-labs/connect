/**
 * Node built-in test runner — `node --test src/utils/messagePreview.test.js`
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { sanitizeMessagePreview } from './messagePreview.js'

test('strips markdown headings and emphasis', () => {
  const { text, chip } = sanitizeMessagePreview('## 🔴 ANDON — stuck on deploy')
  assert.equal(text.startsWith('##'), false)
  assert.ok(!text.includes('**'))
  assert.equal(chip, 'Andon')
  assert.match(text, /ANDON|stuck/i)
})

test('collapses whitespace and clamps long previews', () => {
  const long = 'word '.repeat(80)
  const { text } = sanitizeMessagePreview(long)
  assert.ok(text.length <= 165)
  assert.ok(!/\s{2,}/.test(text))
})

test('empty / null safe', () => {
  assert.deepEqual(sanitizeMessagePreview(null), { text: '', chip: null })
  assert.deepEqual(sanitizeMessagePreview(''), { text: '', chip: null })
})

test('links become labels', () => {
  const { text } = sanitizeMessagePreview('See [docs](https://example.com) please')
  assert.equal(text.includes('https://'), false)
  assert.match(text, /docs/)
})
