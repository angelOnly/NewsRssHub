from __future__ import annotations

import unittest

from app.plugins.rss import parse_feed_payload


RSS = b'''<?xml version="1.0"?><rss version="2.0"><channel><title>Example</title><item><guid>one</guid><title>Model launch</title><link>https://example.test/one</link><description><![CDATA[<p>A useful update.</p>]]></description><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item></channel></rss>'''


class RssTests(unittest.TestCase):
    def test_parse_normalizes_html_and_guid(self) -> None:
        title, items = parse_feed_payload(RSS)
        self.assertEqual(title, "Example")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Model launch")
        self.assertEqual(items[0].content, "A useful update.")
        self.assertEqual(len(items[0].guid), 64)
