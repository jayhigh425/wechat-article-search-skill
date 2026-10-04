#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets==16.1"]
# ///

from __future__ import annotations

import importlib.util
import csv
import io
import json
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("wechat_official", ROOT / "scripts" / "wechat_official.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class DateTests(unittest.TestCase):
    def test_relative_and_partial_dates_use_collection_date(self) -> None:
        reference = date(2026, 7, 15)
        self.assertEqual(MODULE.parse_wechat_date("昨天", reference), date(2026, 7, 14))
        self.assertEqual(MODULE.parse_wechat_date("前天", reference), date(2026, 7, 13))
        self.assertEqual(MODULE.parse_wechat_date("3天前", reference), date(2026, 7, 12))
        self.assertEqual(MODULE.parse_wechat_date("12月30日", reference), date(2025, 12, 30))
        self.assertEqual(MODULE.parse_wechat_date("7月1日", reference), date(2026, 7, 1))

    def test_invalid_date_returns_none(self) -> None:
        self.assertIsNone(MODULE.parse_wechat_date("日期未知", date(2026, 7, 15)))


class FilteringAndRankingTests(unittest.TestCase):
    def test_unicode_filter_normalization(self) -> None:
        config = MODULE.SearchConfig(queries=["A股 研究"], contains_any=["Ａ股"])
        accepted, parsed, reason = MODULE.passes_filters(
            {"title": "A股公司研究", "snippet": "", "account": "测试", "date_raw": "昨天"},
            config,
            date(2026, 7, 15),
        )
        self.assertTrue(accepted)
        self.assertEqual(parsed, date(2026, 7, 14))
        self.assertEqual(reason, "ok")

    def test_unknown_date_policy(self) -> None:
        dropped = MODULE.SearchConfig(queries=["测试"], unknown_date="drop")
        kept = MODULE.SearchConfig(queries=["测试"], unknown_date="keep")
        row = {"title": "测试", "snippet": "", "account": "", "date_raw": "未知"}
        self.assertFalse(MODULE.passes_filters(row, dropped, date(2026, 7, 15))[0])
        self.assertTrue(MODULE.passes_filters(row, kept, date(2026, 7, 15))[0])

    def test_query_coverage_outranks_single_query(self) -> None:
        url = "https://mp.weixin.qq.com/s?__biz=abc&mid=1&idx=1&sn=first"
        records = [
            {
                "title": "数字化转型研究",
                "account": "甲",
                "published_date": "2026-07-01",
                "url": url + "&chksm=one",
                "matched_queries": ["数字化 转型"],
                "source_rank": 8,
                "search_status": "ok",
            },
            {
                "title": "数字化转型研究",
                "account": "甲",
                "published_date": "2026-07-01",
                "url": url + "&chksm=two",
                "matched_queries": ["公司治理 数字化"],
                "source_rank": 2,
                "search_status": "ok",
            },
            {
                "title": "数字化转型研究",
                "account": "乙",
                "published_date": "2026-07-02",
                "url": "https://mp.weixin.qq.com/s?__biz=def&mid=2&idx=1&sn=second",
                "matched_queries": ["数字化 转型"],
                "source_rank": 1,
                "search_status": "ok",
            },
        ]
        ranked = MODULE.merge_and_rank(records)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(len(ranked[0]["matched_queries"]), 2)
        self.assertEqual(ranked[0]["source_rank"], 2)


class UrlAndStatusTests(unittest.TestCase):
    def test_canonical_identity_ignores_tracking_parameters(self) -> None:
        first = {"url": "https://mp.weixin.qq.com/s?__biz=abc&mid=1&idx=2&sn=x&chksm=one"}
        second = {"url": "http://mp.weixin.qq.com/s?__biz=abc&mid=1&idx=2&sn=x&scene=two"}
        self.assertEqual(MODULE.stable_record_id(first), MODULE.stable_record_id(second))
        canonical, identity, valid = MODULE.canonicalize_url(first["url"])
        self.assertTrue(valid)
        self.assertEqual(canonical, "https://mp.weixin.qq.com/s?__biz=abc&mid=1&idx=2")
        self.assertEqual(identity, "__biz=abc&mid=1&idx=2")

    def test_rejects_non_wechat_host(self) -> None:
        self.assertFalse(MODULE.canonicalize_url("https://example.com/s?mid=1")[2])

    def test_status_precedence(self) -> None:
        self.assertEqual(MODULE.classify_article_text("访问频繁 已被删除", "正文", gate_text="访问频繁 已被删除")[0], "risk_control")
        self.assertEqual(MODULE.classify_article_text("该内容已被发布者删除", "")[0], "deleted")
        self.assertEqual(MODULE.classify_article_text("请在微信客户端打开", "")[0], "client_only")
        self.assertEqual(MODULE.classify_article_text("普通页面", "")[0], "empty")
        self.assertEqual(MODULE.classify_article_text("普通页面", "可读正文")[0], "ok")

    def test_markdown_escape(self) -> None:
        self.assertEqual(MODULE.escape_md("a|b\n c"), "a\\|b c")


class CheckpointTests(unittest.TestCase):
    def test_resume_replays_events_and_ignores_truncated_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = MODULE.RunBundle(Path(temporary) / "run", resume=False)
            record = MODULE.normalize_input_record(
                {"url": "https://mp.weixin.qq.com/s?__biz=abc&mid=1&idx=1"}
            )
            bundle.append({"type": "input", "record": record})
            bundle.append({"type": "query_complete", "query": "测试", "status": "ok", "candidates": 1})
            with bundle.checkpoint.open("a", encoding="utf-8") as handle:
                handle.write('{"type":"fulltext"')
            state = bundle.load_checkpoint()
            self.assertIn(record["id"], state.input_records)
            self.assertEqual(state.query_completed["测试"]["status"], "ok")

    def test_results_are_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = MODULE.RunBundle(Path(temporary) / "run", resume=False)
            bundle.finalize("fetch", [], {}, "complete")
            data = json.loads(bundle.results_json.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 2)
            self.assertEqual(data["records"], [])


class FetchLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_limit_keeps_unfetched_metadata_records(self) -> None:
        class FakePage:
            async def screenshot(self, _output: Path) -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary:
            bundle = MODULE.RunBundle(Path(temporary) / "run", resume=False)
            records = [
                MODULE.normalize_input_record({"title": "一", "url": "https://example.com/one"}),
                MODULE.normalize_input_record({"title": "二", "url": "https://example.com/two"}),
            ]
            runtime = MODULE.RuntimeConfig(delay_min=0, delay_max=0)
            output, status, _message = await MODULE.fetch_records(
                FakePage(), records, bundle, runtime, limit=1
            )
            self.assertEqual(status, "complete")
            self.assertEqual(len(output), 2)
            self.assertEqual(output[0]["fulltext_status"], "invalid_url")
            self.assertEqual(output[1]["fulltext_status"], "not_requested")


class ReliabilityTests(unittest.TestCase):
    def test_legitimate_article_can_discuss_error_states(self) -> None:
        body = "本文讨论访问频繁、内容已被删除和系统繁忙的用户体验。"
        self.assertEqual(MODULE.classify_article_text(body, body)[0], "ok")

    def test_image_only_is_distinct_from_empty(self) -> None:
        self.assertEqual(MODULE.classify_article_text("", "", image_count=2)[0], "image_only")

    def test_short_links_dedupe_tracking_and_reject_non_articles(self) -> None:
        one = {"url": "https://mp.weixin.qq.com/s/AbC_def?scene=1&from=singlemessage"}
        two = {"url": "http://mp.weixin.qq.com/s/AbC_def?scene=2#wechat_redirect"}
        self.assertEqual(MODULE.stable_record_id(one), MODULE.stable_record_id(two))
        for url in ("ftp://mp.weixin.qq.com/s/abc", "https://mp.weixin.qq.com/cgi-bin/login", "https://user@mp.weixin.qq.com/s/abc", "https://mp.weixin.qq.com.evil.test/s/abc"):
            self.assertFalse(MODULE.canonicalize_url(url)[2], url)

    def test_invalid_input_urls_do_not_collapse_to_one_record(self) -> None:
        records = MODULE.load_input_records(None, ["https://example.com/1", "https://example.com/2"])
        self.assertEqual(len(records), 2)

    def test_chinese_datetime_and_leap_day(self) -> None:
        self.assertEqual(MODULE.parse_wechat_date("2026年7月1日 08:30 北京", date(2026, 10, 4)), date(2026, 7, 1))
        self.assertEqual(MODULE.parse_wechat_date("2月29日", date(2024, 1, 1)), date(2020, 2, 29))
        self.assertIsNone(MODULE.parse_wechat_date("2026-02-30", date(2026, 10, 4)))

    def test_newest_sort_and_saved_date_filtering(self) -> None:
        config = MODULE.SearchConfig(queries=["研究"], sort="newest", min_date=date(2026, 7, 2))
        records = [{"title": "研究", "url": f"https://mp.weixin.qq.com/s/{day}", "published_date": f"2026-07-0{day}", "matched_queries": ["研究"], "search_status": "ok"} for day in (1, 3, 2)]
        selected = MODULE.select_records(records, config, date(2026, 10, 4))
        self.assertEqual([row["published_date"] for row in selected], ["2026-07-03", "2026-07-02"])

    def test_search_errors_are_not_no_results(self) -> None:
        self.assertEqual(MODULE.classify_search_state({"card_count": 0, "gate_text": "正在加载"})[0], "error")
        self.assertEqual(MODULE.classify_search_state({"card_count": 0, "gate_text": "暂无搜索结果"})[0], "no_results")
        self.assertEqual(MODULE.classify_search_state({"broker": {"text": '{"errCode":-2010}'}})[0], "auth_required")
        self.assertEqual(MODULE.classify_search_state({"broker": {"status": 500}})[0], "error")
        self.assertEqual(MODULE.classify_search_state({"gate_text": "访问频繁"})[0], "risk_control")

    def test_checkpoint_repair_survives_another_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run"
            bundle = MODULE.RunBundle(path, False)
            bundle.append({"type": "query_complete", "query": "一", "status": "ok"})
            with bundle.checkpoint.open("ab") as handle:
                handle.write(b'{"type":"candidate","title":"\xe4\xb8')
            resumed = MODULE.RunBundle(path, True)
            resumed.load_checkpoint()
            resumed.append({"type": "query_complete", "query": "二", "status": "ok"})
            self.assertEqual(set(MODULE.RunBundle(path, True).load_checkpoint().query_completed), {"一", "二"})

    def test_completed_corrupt_checkpoint_is_not_silently_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = MODULE.RunBundle(Path(temporary) / "run", False)
            bundle.checkpoint.write_bytes(b'{broken}\n')
            with self.assertRaises(json.JSONDecodeError):
                bundle.load_checkpoint()
            with self.assertRaises(ValueError):
                bundle.append({"type": "query_complete"})

    def test_checkpoint_accepts_valid_last_line_without_newline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = MODULE.RunBundle(Path(temporary) / "run", False)
            bundle.checkpoint.write_text('{"type":"query_complete","query":"a","status":"ok"}', encoding="utf-8")
            bundle.append({"type": "query_complete", "query": "b", "status": "ok"})
            self.assertEqual(len(bundle.load_checkpoint().query_completed), 2)

    def test_config_rejects_string_lists_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "query.json"
            args = MODULE.build_parser().parse_args(["search", "--query-file", str(path)])
            for data in ({"queries": "公司治理"}, {"queries": ["研究"], "min_data": "2026-01-01"}):
                path.write_text(json.dumps(data), encoding="utf-8-sig")
                with self.assertRaises(ValueError):
                    MODULE.load_search_config(args)

    def test_input_bom_single_object_and_unsafe_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "input.json"
            path.write_text(json.dumps({"id": "../../escape", "article_url": "https://mp.weixin.qq.com/s/abc"}), encoding="utf-8-sig")
            record = MODULE.load_input_records(path, None)[0]
            self.assertEqual(record["url"], "https://mp.weixin.qq.com/s/abc")
            self.assertTrue(record["id"].startswith("wx_"))

    def test_offline_rank_exports_readable_csv_and_keeps_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            records = [MODULE.normalize_input_record({"title": title, "account": "研究号", "url": f"https://mp.weixin.qq.com/s/{i}", "published_date": "2026-07-15", "matched_queries": ["研究"]}) for i, title in enumerate(["研究成果", "培训广告", "=1+1"])]
            (source / "candidates.json").write_text(json.dumps({"config": {"queries": ["研究"]}, "records": records}, ensure_ascii=False), encoding="utf-8")
            output = Path(temporary) / "rank"
            args = MODULE.build_parser().parse_args(["rank", "--input", str(source), "--run-dir", str(output), "--exclude-any", "培训", "--target", "2"])
            self.assertEqual(MODULE.execute_rank(args), 0)
            result = json.loads((output / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(len(result["records"]), 2)
            self.assertEqual(len(json.loads((output / "candidates.json").read_text(encoding="utf-8"))["records"]), 3)
            rows = list(csv.DictReader(io.StringIO((output / "results.csv").read_text(encoding="utf-8-sig"))))
            self.assertIn("'=1+1", [row["title"] for row in rows])

    def test_run_lock_prevents_simultaneous_writers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = MODULE.RunBundle(Path(temporary) / "run", False)
            first.write_run("search", "running")
            second = MODULE.RunBundle(first.run_dir, True)
            first.acquire_lock()
            try:
                with self.assertRaises(ValueError):
                    second.acquire_lock()
            finally:
                first.release_lock()
            second.acquire_lock()
            second.release_lock()

    def test_offline_rank_combines_pool_with_already_fetched_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            record = MODULE.normalize_input_record({"title": "研究", "url": "https://mp.weixin.qq.com/s/saved", "published_date": "2026-07-15", "matched_queries": ["研究"]})
            (source / "candidates.json").write_text(json.dumps({"config": {"queries": ["研究"]}, "records": [record]}), encoding="utf-8")
            enriched = dict(record, fulltext_status="ok", fulltext="已经读取的正文", published_date="2026-07-12")
            (source / "results.json").write_text(json.dumps({"records": [enriched]}), encoding="utf-8")
            output = Path(temporary) / "rank"
            args = MODULE.build_parser().parse_args(["rank", "--input", str(source), "--run-dir", str(output)])
            MODULE.execute_rank(args)
            result = json.loads((output / "results.json").read_text(encoding="utf-8"))["records"][0]
            self.assertEqual(result["fulltext"], "已经读取的正文")
            self.assertEqual(result["published_date"], "2026-07-12")
            self.assertTrue((output / result["content_path"]).exists())


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_retains_original_fetch_limit_without_more_requests(self) -> None:
        class FakePage:
            ws = None
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_args):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "urls.txt"
            source.write_text("https://mp.weixin.qq.com/s/first\nhttps://mp.weixin.qq.com/s/second\n", encoding="utf-8")
            run_dir = Path(temporary) / "run"
            manager = Mock(start=Mock(return_value=9222), close=AsyncMock(), started=True)
            first = MODULE.build_parser().parse_args(["fetch", "--input", str(source), "--limit", "1", "--run-dir", str(run_dir)])
            resumed = MODULE.build_parser().parse_args(["fetch", "--run-dir", str(run_dir), "--resume"])
            extractor = AsyncMock(return_value={"fulltext_status": "ok", "fulltext": "正文", "attempt_count": 1})
            with patch.object(MODULE, "BrowserManager", return_value=manager), patch.object(MODULE, "CdpPage", return_value=FakePage()), patch.object(MODULE, "get_or_create_page_ws", return_value="ws://fixture"), patch.object(MODULE, "extract_article_with_retries", extractor):
                self.assertEqual(await MODULE.execute(first), 0)
                self.assertEqual(await MODULE.execute(resumed), 0)
            self.assertEqual(extractor.await_count, 1)
            data = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(data["counts"].get("fulltext_not_requested"), 1)

    async def test_transient_failure_returns_partial_and_preserves_other_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = MODULE.RunBundle(Path(temporary) / "run", False)
            records = MODULE.load_input_records(None, ["https://mp.weixin.qq.com/s/first", "https://mp.weixin.qq.com/s/second"])
            responses = [{"fulltext_status": "timeout", "error": "slow", "attempt_count": 2}, {"fulltext_status": "ok", "fulltext": "正文", "attempt_count": 1}]
            with patch.object(MODULE, "extract_article_with_retries", AsyncMock(side_effect=responses)):
                result, status, _ = await MODULE.fetch_records(type("Page", (), {"screenshot": AsyncMock()})(), records, bundle, MODULE.RuntimeConfig(delay_min=0, delay_max=0), None)
            self.assertEqual(status, "partial")
            self.assertEqual([row["fulltext_status"] for row in result], ["timeout", "ok"])

    async def test_risk_control_stops_requests_and_keeps_prior_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = MODULE.RunBundle(Path(temporary) / "run", False)
            records = MODULE.load_input_records(None, ["https://mp.weixin.qq.com/s/first", "https://mp.weixin.qq.com/s/second"])
            saved = dict(records[1], fulltext_status="ok", fulltext="已保存")
            bundle.append({"type": "fulltext", "record": saved})
            fetch = AsyncMock(return_value={"fulltext_status": "risk_control", "error": "访问频繁", "attempt_count": 1})
            with patch.object(MODULE, "extract_article_with_retries", fetch):
                result, status, _ = await MODULE.fetch_records(type("Page", (), {"screenshot": AsyncMock()})(), records, bundle, MODULE.RuntimeConfig(delay_min=0, delay_max=0), None)
            self.assertEqual(status, "risk_control")
            self.assertEqual(fetch.await_count, 1)
            self.assertEqual(result[1]["fulltext_status"], "ok")

    async def test_imported_fulltext_is_reused_in_new_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = MODULE.RunBundle(Path(temporary) / "run", False)
            record = MODULE.normalize_input_record({"url": "https://mp.weixin.qq.com/s/first", "fulltext_status": "ok", "fulltext": "保存正文", "content_path": "old/path.md"})
            with patch.object(MODULE, "extract_article_with_retries", AsyncMock()) as fetch:
                result, status, _ = await MODULE.fetch_records(object(), [record], bundle, MODULE.RuntimeConfig(), None)
            self.assertEqual(status, "complete")
            self.assertEqual(fetch.await_count, 0)
            self.assertTrue((bundle.run_dir / result[0]["content_path"]).exists())

    async def test_added_fetch_inputs_are_checkpointed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = MODULE.RunBundle(Path(temporary) / "run", False)
            first = MODULE.load_input_records(None, ["https://mp.weixin.qq.com/s/first"])[0]
            second = MODULE.load_input_records(None, ["https://mp.weixin.qq.com/s/second"])[0]
            bundle.append({"type": "input", "record": first})
            await MODULE.fetch_records(object(), [first, second], bundle, MODULE.RuntimeConfig(), 0)
            self.assertEqual(len(bundle.load_checkpoint().input_records), 2)

    async def test_collect_resume_keeps_current_target_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = MODULE.RunBundle(Path(temporary) / "run", False)
            old = MODULE.load_input_records(None, ["https://mp.weixin.qq.com/s/old"])[0]
            current = MODULE.load_input_records(None, ["https://mp.weixin.qq.com/s/current"])[0]
            bundle.append({"type": "input", "record": old})
            result, _, _ = await MODULE.fetch_records(object(), [current], bundle, MODULE.RuntimeConfig(), 0, preserve_saved_scope=False)
            self.assertEqual([row["id"] for row in result], [current["id"]])
            self.assertIn(old["id"], bundle.load_checkpoint().input_records)

    async def test_failed_query_is_retried_and_filtered_pool_is_retained(self) -> None:
        class FakePage:
            timeout = 5
            call = AsyncMock(return_value={"result": {"identifier": "hook"}})
            navigate = AsyncMock()
            wait_for = AsyncMock(return_value=True)
            screenshot = AsyncMock()
            async def eval(self, expression, **_kwargs):
                return {"card_count": 2, "gate_text": ""}

        with tempfile.TemporaryDirectory() as temporary:
            bundle = MODULE.RunBundle(Path(temporary) / "run", False)
            bundle.append({"type": "query_complete", "query": "研究", "status": "error"})
            rows = [{"idx": 0, "title": "研究成果", "date_raw": "2026-07-15"}, {"idx": 1, "title": "培训广告", "date_raw": "2026-07-15"}]
            config = MODULE.SearchConfig(queries=["研究"], exclude_any=["培训"], target=1)
            page = FakePage()
            with patch.object(MODULE, "scroll_results", AsyncMock(return_value=2)), patch.object(MODULE, "extract_metadata", AsyncMock(return_value=rows)), patch.object(MODULE, "extract_urls", AsyncMock(return_value={0: "https://mp.weixin.qq.com/s/first", 1: "https://mp.weixin.qq.com/s/second"})):
                selected, states = await MODULE.collect_candidates(page, config, bundle, date(2026, 10, 4), MODULE.RuntimeConfig(delay_min=0, delay_max=0))
            self.assertEqual(page.navigate.await_count, 1)
            self.assertEqual(states["研究"]["rejected"], {"excluded_term": 1})
            self.assertEqual(len(selected), 1)
            self.assertEqual(len(json.loads(bundle.candidates_json.read_text(encoding="utf-8"))["records"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
