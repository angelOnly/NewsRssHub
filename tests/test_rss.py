from __future__ import annotations

import unittest

from app.plugins.rss import parse_feed_payload


RSS = b'''<?xml version="1.0"?><rss version="2.0"><channel><title>Example</title><item><guid>one</guid><title>Model launch</title><link>https://example.test/one</link><description><![CDATA[<p>A useful update.</p>]]></description><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item></channel></rss>'''

MEDIA_RSS = b'''<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Example</title><item><guid>media-one</guid><title>Media launch</title><link>https://www.youtube.com/watch?v=abc_123-XYZ</link><description><![CDATA[<p>Watch this.</p><img src="https://cdn.example.test/cover.jpg" alt="Cover"><img src="javascript:alert(1)"><video poster="https://cdn.example.test/poster.jpg"><source src="https://video.example.test/movie.mp4" type="video/mp4"></video><a href="https://www.bilibili.com/video/BV1xx411c7mD/">Bilibili</a>]]></description><media:content url="https://cdn.example.test/main.webp" medium="image"/><media:thumbnail url="https://cdn.example.test/thumb.jpg"/><enclosure url="https://video.example.test/clip.webm" type="video/webm"/></item></channel></rss>'''


class RssTests(unittest.TestCase):
    def test_parse_normalizes_html_and_guid(self) -> None:
        title, items = parse_feed_payload(RSS)
        self.assertEqual(title, "Example")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Model launch")
        self.assertEqual(items[0].content, "A useful update.")
        self.assertEqual(len(items[0].guid), 64)
        self.assertEqual(items[0].media, [])

    def test_parse_extracts_safe_images_videos_and_trusted_embeds(self) -> None:
        _, items = parse_feed_payload(MEDIA_RSS)

        self.assertEqual(len(items), 1)
        media_by_url = {asset["url"]: asset for asset in items[0].media}
        self.assertEqual(media_by_url["https://cdn.example.test/main.webp"]["kind"], "image")
        self.assertEqual(media_by_url["https://cdn.example.test/thumb.jpg"]["kind"], "image")
        self.assertEqual(media_by_url["https://cdn.example.test/cover.jpg"]["alt"], "Cover")
        self.assertEqual(media_by_url["https://video.example.test/movie.mp4"]["kind"], "video")
        self.assertEqual(
            media_by_url["https://video.example.test/movie.mp4"]["poster_url"],
            "https://cdn.example.test/poster.jpg",
        )
        self.assertEqual(media_by_url["https://video.example.test/clip.webm"]["kind"], "video")
        self.assertIn("https://www.youtube-nocookie.com/embed/abc_123-XYZ", media_by_url)
        self.assertIn(
            "https://player.bilibili.com/player.html?bvid=BV1xx411c7mD&page=1",
            media_by_url,
        )
        self.assertFalse(any("javascript:" in asset["url"] for asset in items[0].media))
