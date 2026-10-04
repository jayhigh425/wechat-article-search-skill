#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets==16.1"]
# ///

from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SPEC = importlib.util.spec_from_file_location("wechat_official", ROOT / "scripts" / "wechat_official.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class BrowserFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_and_article_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = MODULE.RuntimeConfig(
                profile=Path(temporary) / "profile",
                timeout=6,
                login_timeout=0,
                delay_min=0,
                delay_max=0,
                headless=True,
            )
            manager = MODULE.BrowserManager(runtime, start_url=(FIXTURES / "search.html").as_uri())
            try:
                port = await asyncio.to_thread(manager.start)
                async with MODULE.CdpPage(MODULE.get_or_create_page_ws(port), timeout=6) as page:
                    await page.navigate((FIXTURES / "search.html").as_uri())
                    self.assertTrue(await page.wait_for("document.querySelectorAll('.search_list_item').length === 2"))
                    rows = await MODULE.extract_metadata(page, 10)
                    self.assertEqual([row["title"] for row in rows], ["数字化转型研究", "公司治理案例"])
                    urls = await MODULE.extract_urls(page, [0, 1], 2)
                    self.assertTrue(all(MODULE.canonicalize_url(url)[2] for url in urls.values()))

                    cases = {
                        "article-ok.html": "ok",
                        "article-empty.html": "empty",
                        "article-deleted.html": "deleted",
                        "article-client-only.html": "client_only",
                        "article-risk.html": "risk_control",
                        "article-error-words.html": "ok",
                        "article-image-only.html": "image_only",
                        "article-delayed.html": "ok",
                    }
                    for filename, expected in cases.items():
                        result = await MODULE.extract_article_once(page, (FIXTURES / filename).as_uri(), 4)
                        self.assertEqual(result["fulltext_status"], expected, filename)
                    ok = await MODULE.extract_article_once(page, (FIXTURES / "article-ok.html").as_uri(), 4)
                    self.assertGreater(ok["char_count"], 0)
                    self.assertEqual(ok["image_count"], 1)
                    self.assertEqual(ok["article_date"], "2026-07-15")
                    anchored = await MODULE.extract_article_once(page, (FIXTURES / "article-ok.html").as_uri() + "#rd", 4)
                    self.assertEqual(anchored["fulltext_status"], "ok")
                    words = await MODULE.extract_article_once(page, (FIXTURES / "article-error-words.html").as_uri(), 4)
                    self.assertNotIn("隐藏内容", words["fulltext"])
                    self.assertEqual(words["article_date"], "2026-07-15 北京")
                    await page.navigate((FIXTURES / "search-delayed.html").as_uri())
                    urls = await MODULE.extract_urls(page, [0, 1], 20)
                    self.assertEqual(urls[0], "https://mp.weixin.qq.com/s/delayed")
                    self.assertEqual(urls[1], "https://mp.weixin.qq.com/s/direct")
                    self.assertTrue(await page.eval("window.open === window.originalOpen"))
            finally:
                await manager.close()
                await asyncio.sleep(0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
