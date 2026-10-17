import { Link } from 'react-router-dom'

// Mirrors the backend hashtag definition (connect/signals.py HASHTAG_RE):
// a "#" not preceded by a word character, followed by 1-100 word characters.
const HASHTAG_RE = /(?<!\w)#(\w{1,100})/g

/**
 * Render plain post text with #hashtags turned into links to the hashtag
 * feed (/hashtag/:tag — posts filtered to that tag). Returns an array of strings and
 * <Link> elements, suitable for use directly as React children. Non-hashtag
 * text is preserved verbatim (whitespace-pre-wrap on the container keeps
 * newlines/spacing intact).
 */
export function renderTextWithHashtags(text) {
  if (!text) return text

  const nodes = []
  let lastIndex = 0
  HASHTAG_RE.lastIndex = 0

  for (let match = HASHTAG_RE.exec(text); match !== null; match = HASHTAG_RE.exec(text)) {
    const full = match[0]
    const tag = match[1]
    const start = match.index

    if (start > lastIndex) {
      nodes.push(text.slice(lastIndex, start))
    }

    nodes.push(
      <Link
        key={`hashtag-${start}-${tag}`}
        to={`/hashtag/${encodeURIComponent(tag)}`}
        className="text-primary-600 dark:text-primary-400 hover:underline"
      >
        {full}
      </Link>,
    )

    lastIndex = start + full.length
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex))
  }

  return nodes
}
