"""CON-93 / CON-91: regression tests for _preview() — the feed/preview truncation
helper. A feed/list preview must never cut mid-word silently; it trims to a word
boundary within the limit and appends an explicit truncation marker. Self-contained
(extracts the pure function from source) so it runs without the MCP runtime deps."""
import os
import re
import unittest

_SRC = os.path.join(os.path.dirname(__file__), '..', 'connect_server.py')
_m = re.search(r"def _preview\(.*?\n\n\n", open(_SRC).read(), re.S)
_ns: dict = {}
exec(_m.group(0), _ns)  # noqa: S102 — pure helper, no side effects
_preview = _ns['_preview']


class PreviewTest(unittest.TestCase):
    def test_short_passthrough(self):
        self.assertEqual(_preview('hello world'), 'hello world')
        self.assertEqual(_preview(''), '')
        self.assertEqual(_preview(None), '')

    def test_marker_and_word_boundary(self):
        out = _preview('word ' * 100, 280)          # 500 chars
        self.assertIn('more chars', out)             # explicit marker
        self.assertIn('…', out)                 # ellipsis
        head = out.split(' [')[0].rstrip('… ')
        self.assertFalse(head.endswith('wor'))       # not cut mid-word
        self.assertLessEqual(len(out.split(' [')[0]), 281)

    def test_custom_limit(self):
        out = _preview('alpha beta gamma delta epsilon zeta', 12)
        self.assertIn('more chars', out)
        self.assertLessEqual(len(out.split(' [')[0]), 13)

    def test_no_marker_when_exactly_at_limit(self):
        s = 'x' * 50
        self.assertEqual(_preview(s, 50), s)


if __name__ == '__main__':
    unittest.main()
