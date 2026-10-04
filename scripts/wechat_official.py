#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["websockets==16.1"]
# ///
"""Search WeChat official-account articles and fetch visible article text.

Run this file with ``uv run`` so its pinned WebSocket dependency is resolved
without modifying the user's default Python installation.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import hashlib
import html
import io
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import websockets


SCHEMA_VERSION = 2
DEFAULT_PROFILE = Path(os.environ.get("LOCALAPPDATA", os.environ.get("TEMP", "."))) / "Codex" / "wechat-weread-profile"
WECHAT_SEARCH_URL = (
    "https://search.weixin.qq.com/cgi-bin/newsearchweb/userclientjump"
    "?path=page/search/weread&query={query}&platform=pc"
)
LOGIN_OK_MARKERS = ("我的书架", "继续阅读")
LOGIN_PROMPT_MARKERS = ("微信登录", "扫码登录", "登录")
RISK_MARKERS = ("访问频繁", "操作频繁", "环境异常", "系统繁忙", "异常访问")
DELETED_MARKERS = ("已被删除", "该内容已被发布者删除", "已过期")
CLIENT_ONLY_MARKERS = ("请在微信客户端打开", "请在微信中打开")
BLOCKED_MARKERS = ("参数错误", "停止访问", "内容无法查看", "暂时无法打开")
TERMINAL_FULLTEXT_STATUSES = {"ok", "image_only", "deleted", "client_only", "blocked", "invalid_url"}
TERMINAL_QUERY_STATUSES = {"ok", "no_results"}
LOCAL_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CdpError(RuntimeError):
    pass


class LoginTimeoutError(RuntimeError):
    pass


class SearchAuthenticationError(RuntimeError):
    pass


class SearchRiskControlError(RuntimeError):
    pass


@dataclass
class SearchConfig:
    queries: list[str]
    target: int = 60
    min_date: date | None = None
    max_date: date = field(default_factory=date.today)
    contains_any: list[str] = field(default_factory=list)
    contains_all: list[str] = field(default_factory=list)
    exclude_any: list[str] = field(default_factory=list)
    source_include_any: list[str] = field(default_factory=list)
    source_exclude_any: list[str] = field(default_factory=list)
    unknown_date: str = "drop"
    per_query_limit: int = 50
    max_scroll_rounds: int = 6
    batch_size: int = 30
    sort: str = "relevance"

    def validate(self) -> None:
        if not self.queries:
            raise ValueError("Provide at least one query.")
        if self.target <= 0:
            raise ValueError("target must be greater than zero.")
        if self.per_query_limit <= 0:
            raise ValueError("per_query_limit must be greater than zero.")
        if self.max_scroll_rounds <= 0:
            raise ValueError("max_scroll_rounds must be greater than zero.")
        if self.batch_size <= 0:
            raise ValueError("batch_size must be greater than zero.")
        if self.min_date and self.min_date > self.max_date:
            raise ValueError("min_date must not be after max_date.")
        if self.unknown_date not in {"drop", "keep"}:
            raise ValueError("unknown_date must be 'drop' or 'keep'.")
        if self.sort not in {"relevance", "newest"}:
            raise ValueError("sort must be 'relevance' or 'newest'.")

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["min_date"] = self.min_date.isoformat() if self.min_date else None
        data["max_date"] = self.max_date.isoformat()
        return data


@dataclass
class RuntimeConfig:
    browser: str = "edge"
    browser_path: str | None = None
    profile: Path = DEFAULT_PROFILE
    cdp_port: int | None = None
    keep_browser: bool = False
    login_timeout: float = 120.0
    timeout: float = 30.0
    retries: int = 2
    delay_min: float = 1.5
    delay_max: float = 3.0
    headless: bool = False

    def validate(self) -> None:
        if self.timeout <= 0 or self.login_timeout < 0:
            raise ValueError("timeouts must be non-negative and page timeout must be positive.")
        if self.retries < 0:
            raise ValueError("retries must not be negative.")
        if self.delay_min < 0 or self.delay_max < self.delay_min:
            raise ValueError("delay range is invalid.")
        if self.cdp_port is not None and not (1 <= self.cdp_port <= 65535):
            raise ValueError("cdp_port must be between 1 and 65535.")


@dataclass
class CheckpointState:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    input_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    fulltext_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    query_completed: dict[str, dict[str, Any]] = field(default_factory=dict)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def dedupe_list(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for raw in values:
        value = str(raw).strip()
        key = normalize_text(value)
        if value and key not in seen:
            seen.add(key)
            output.append(value)
    return output


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def parse_wechat_date(raw: str, reference_date: date | None = None) -> date | None:
    reference_date = reference_date or date.today()
    raw = normalize_text(raw).replace(" ", "")
    if not raw:
        return None
    if "刚刚" in raw:
        return reference_date
    if "昨天" in raw:
        return reference_date - timedelta(days=1)
    if "前天" in raw:
        return reference_date - timedelta(days=2)
    match = re.search(r"(\d+)", raw)
    if "分钟前" in raw and match:
        return reference_date
    if "小时前" in raw and match:
        return reference_date
    if "天前" in raw and match:
        return reference_date - timedelta(days=int(match.group(1)))
    if "周前" in raw and match:
        return reference_date - timedelta(weeks=int(match.group(1)))
    if "个月前" in raw and match:
        return reference_date - timedelta(days=30 * int(match.group(1)))
    if "年前" in raw and match:
        return reference_date - timedelta(days=365 * int(match.group(1)))
    match = re.search(r"(?<!\d)(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})(?:日)?", raw)
    if match:
        try:
            return date(*(int(value) for value in match.groups()))
        except ValueError:
            return None
    match = re.match(r"(\d{1,2})[月/.-](\d{1,2})(?:日)?", raw)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        for year in range(reference_date.year, reference_date.year - 5, -1):
            try:
                parsed = date(year, month, day)
            except ValueError:
                continue
            if parsed <= reference_date:
                return parsed
    return None


def canonicalize_url(url: str) -> tuple[str, str, bool]:
    raw = html.unescape(str(url or "").strip())
    if not raw:
        return "", "", False
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError:
        return raw, "", False
    host = (parsed.hostname or "").lower()
    valid = (host == "mp.weixin.qq.com" and parsed.scheme in {"http", "https"}
             and not parsed.username and not parsed.password
             and (parsed.path == "/s" or bool(re.fullmatch(r"/s/[\w-]+", parsed.path))))
    if not valid:
        return raw, "", False
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    identity_parts: list[tuple[str, str]] = []
    if query.get("__biz") and query.get("mid") and query.get("idx"):
        identity_parts = [
            ("__biz", query["__biz"][0]),
            ("mid", query["mid"][0]),
            ("idx", query["idx"][0]),
        ]
    elif query.get("sn"):
        identity_parts = [("sn", query["sn"][0])]
    canonical_query = urllib.parse.urlencode(identity_parts)
    if not identity_parts:
        tracking = {"scene", "from", "isappinstalled", "chksm", "clicktime", "enterid", "sessionid", "subscene", "ascene", "wxfrom"}
        canonical_query = urllib.parse.urlencode(sorted(
            (key, value) for key, values in query.items() if key.lower() not in tracking
            for value in values
        ))
    canonical = urllib.parse.urlunparse(
        ("https", "mp.weixin.qq.com", parsed.path, "", canonical_query, "")
    )
    identity = "&".join(f"{key}={value}" for key, value in identity_parts) or canonical
    return canonical, identity, True


def stable_record_id(record: dict[str, Any]) -> str:
    canonical, identity, valid = canonicalize_url(record.get("url") or record.get("article_url") or "")
    if valid:
        record["canonical_url"] = canonical
    if not identity:
        identity = str(record.get("url") or record.get("article_url") or "") + "|" + "|".join(
            normalize_text(record.get(field, ""))
            for field in ("title", "account", "published_date", "date_raw")
        )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"wx_{digest}"


def query_terms(queries: Iterable[str]) -> list[str]:
    terms: list[str] = []
    for query in queries:
        terms.extend(re.findall(r"[\w\u3400-\u9fff]+", normalize_text(query)))
    return dedupe_list(terms)


def passes_filters(
    row: dict[str, Any], config: SearchConfig, reference_date: date
) -> tuple[bool, date | None, str]:
    title = normalize_text(row.get("title"))
    snippet = normalize_text(row.get("snippet") or row.get("desc"))
    account = normalize_text(row.get("account") or row.get("source"))
    combined = f"{title} {snippet} {account}"
    parsed_date = parse_wechat_date(str(row.get("published_date") or row.get("date_raw", "")), reference_date)
    if parsed_date is None and config.unknown_date == "drop":
        return False, None, "date_unparsed"
    if parsed_date and config.min_date and parsed_date < config.min_date:
        return False, parsed_date, "before_min_date"
    if parsed_date and parsed_date > config.max_date:
        return False, parsed_date, "after_max_date"
    if config.contains_any and not any(normalize_text(term) in combined for term in config.contains_any):
        return False, parsed_date, "missing_contains_any"
    if config.contains_all and not all(normalize_text(term) in combined for term in config.contains_all):
        return False, parsed_date, "missing_contains_all"
    if config.exclude_any and any(normalize_text(term) in combined for term in config.exclude_any):
        return False, parsed_date, "excluded_term"
    if config.source_include_any and not any(
        normalize_text(term) in account for term in config.source_include_any
    ):
        return False, parsed_date, "source_not_included"
    if config.source_exclude_any and any(
        normalize_text(term) in account for term in config.source_exclude_any
    ):
        return False, parsed_date, "source_excluded"
    return True, parsed_date, "ok"


def merge_and_rank(records: Iterable[dict[str, Any]], target: int | None = None, sort: str = "relevance") -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for raw in records:
        record = dict(raw)
        record.setdefault("id", stable_record_id(record))
        key = record["id"]
        existing = merged.get(key)
        if existing is None:
            record["matched_queries"] = dedupe_list(record.get("matched_queries", []))
            merged[key] = record
            continue
        existing["matched_queries"] = dedupe_list(
            list(existing.get("matched_queries", [])) + list(record.get("matched_queries", []))
        )
        existing["source_rank"] = min(
            int(existing.get("source_rank") or 10**9), int(record.get("source_rank") or 10**9)
        )
        if existing.get("search_status") != "ok" and record.get("search_status") == "ok":
            keep_queries = existing["matched_queries"]
            best_rank = existing["source_rank"]
            merged[key] = record
            merged[key]["matched_queries"] = keep_queries
            merged[key]["source_rank"] = best_rank
        else:
            for field_name in ("title", "account", "snippet", "published_date", "date_raw", "url"):
                if not existing.get(field_name) and record.get(field_name):
                    existing[field_name] = record[field_name]

    ranked = list(merged.values())
    for record in ranked:
        terms = query_terms(record.get("ranking_queries") or record.get("matched_queries", []))
        title = normalize_text(record.get("title"))
        text = f"{title} {normalize_text(record.get('snippet'))}"
        record["title_term_hits"] = sum(term in title for term in terms)
        record["text_term_hits"] = sum(term in text for term in terms)

    def rank_key(record: dict[str, Any]) -> tuple[Any, ...]:
        published = record.get("published_date") or "0000-00-00"
        relevance = (
            1 if record.get("search_status") == "ok" else 0,
            len(record.get("matched_queries", [])),
            int(record.get("title_term_hits", 0)),
            int(record.get("text_term_hits", 0)),
            -int(record.get("source_rank") or 10**9),
            published,
        )
        return (relevance[0], published, *relevance[1:-1]) if sort == "newest" else relevance

    ranked.sort(key=rank_key, reverse=True)
    return ranked[:target] if target is not None else ranked


def classify_article_text(body_text: str, fulltext: str, *, gate_text: str = "", image_count: int = 0) -> tuple[str, str]:
    body = str(gate_text or (body_text if not str(fulltext or "").strip() and not image_count else ""))
    if any(marker in body for marker in RISK_MARKERS):
        return "risk_control", body[:400]
    if any(marker in body for marker in DELETED_MARKERS):
        return "deleted", body[:400]
    if any(marker in body for marker in CLIENT_ONLY_MARKERS):
        return "client_only", body[:400]
    if any(marker in body for marker in BLOCKED_MARKERS):
        return "blocked", body[:400]
    if not str(fulltext or "").strip() and image_count:
        return "image_only", "Only images are visible; no OCR text was inferred."
    if not str(fulltext or "").strip():
        return "empty", body[:400]
    return "ok", ""


def escape_md(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().replace("|", "\\|")


def safe_heading(value: Any) -> str:
    return re.sub(r"[\r\n]+", " ", str(value or "")).strip().replace("#", "﹟")


class RunBundle:
    def __init__(self, run_dir: Path, resume: bool):
        self.run_dir = run_dir.resolve()
        self.run_json = self.run_dir / "run.json"
        self.checkpoint = self.run_dir / "checkpoint.jsonl"
        self.results_json = self.run_dir / "results.json"
        self.candidates_json = self.run_dir / "candidates.json"
        self.results_csv = self.run_dir / "results.csv"
        self.summary_md = self.run_dir / "summary.md"
        self.articles_dir = self.run_dir / "articles"
        self.diagnostics_dir = self.run_dir / "diagnostics"
        self._checkpoint_ready = False
        if self.run_dir.exists() and any(self.run_dir.iterdir()) and not resume:
            raise SystemExit(f"Run directory is not empty; pass --resume to reuse it: {self.run_dir}")
        if resume and not self.run_dir.exists():
            raise SystemExit(f"Cannot resume a missing run directory: {self.run_dir}")
        if resume and not (self.run_json.exists() or self.checkpoint.exists()):
            raise SystemExit(f"No recoverable run state in: {self.run_dir}")
        self.articles_dir.mkdir(parents=True, exist_ok=True)
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)

    def load_run(self) -> dict[str, Any]:
        if not self.run_json.exists():
            return {}
        return json.loads(self.run_json.read_text(encoding="utf-8"))

    def acquire_lock(self) -> None:
        self._lock_handle = (self.run_dir / ".run.lock").open("a+b")
        self._lock_handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.fstat(self._lock_handle.fileno()).st_size == 0:
                self._lock_handle.write(b"0")
                self._lock_handle.flush()
        except OSError as exc:
            self._lock_handle.close()
            raise ValueError(f"Another process is using this run directory: {self.run_dir}") from exc

    def release_lock(self) -> None:
        handle = getattr(self, "_lock_handle", None)
        if handle and not handle.closed:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            handle.close()

    def write_run(
        self,
        command: str,
        status: str,
        config: dict[str, Any] | None = None,
        counts: dict[str, int] | None = None,
        message: str = "",
        options: dict[str, Any] | None = None,
    ) -> None:
        previous = self.load_run()
        payload = {
            "schema_version": SCHEMA_VERSION,
            "command": command,
            "status": status,
            "started_at": previous.get("started_at") or now_iso(),
            "updated_at": now_iso(),
            "config": config if config is not None else previous.get("config", {}),
            "counts": counts or previous.get("counts", {}),
            "message": message,
            "options": options if options is not None else previous.get("options", {}),
        }
        atomic_write_json(self.run_json, payload)

    def append(self, event: dict[str, Any]) -> None:
        if not self._checkpoint_ready and self.checkpoint.exists():
            data = self.checkpoint.read_bytes()
            lines = data.splitlines(keepends=True)
            if lines and lines[-1].strip():
                try:
                    json.loads(lines[-1])
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if data.endswith(b"\n"):
                        raise ValueError("Malformed completed checkpoint line; preserve the log and inspect it before resuming.")
                    # Repair before append so the next event is valid JSONL.
                    with self.checkpoint.open("r+b") as handle:
                        handle.truncate(sum(len(line) for line in lines[:-1]))
                else:
                    if not data.endswith(b"\n"):
                        with self.checkpoint.open("ab") as handle:
                            handle.write(b"\n")
        self._checkpoint_ready = True
        payload = dict(event)
        payload.setdefault("at", now_iso())
        with self.checkpoint.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def load_checkpoint(self) -> CheckpointState:
        state = CheckpointState()
        if not self.checkpoint.exists():
            return state
        lines = self.checkpoint.read_bytes().splitlines()
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                if number == len(lines) and not self.checkpoint.read_bytes().endswith(b"\n"):
                    break
                raise
            kind = event.get("type")
            if kind == "candidate" and isinstance(event.get("record"), dict):
                state.candidates.append(event["record"])
            elif kind == "input" and isinstance(event.get("record"), dict):
                state.input_records[event["record"]["id"]] = event["record"]
            elif kind == "fulltext" and isinstance(event.get("record"), dict):
                state.fulltext_records[event["record"]["id"]] = event["record"]
            elif kind == "query_complete":
                state.query_completed[str(event.get("query", ""))] = event
        return state

    def write_article(self, record: dict[str, Any]) -> str:
        relative = Path("articles") / f"{record['id']}.md"
        target = self.run_dir / relative
        title = safe_heading(record.get("article_title") or record.get("title") or record["id"])
        lines = [
            f"# {title}",
            "",
            f"- Account: {record.get('article_account') or record.get('account') or ''}",
            f"- Date: {record.get('article_date') or record.get('published_date') or ''}",
            f"- URL: {record.get('article_url') or record.get('url') or ''}",
            f"- Captured: {record.get('fulltext_captured_at') or ''}",
            "",
            str(record.get("fulltext") or "").strip(),
            "",
        ]
        if record.get("images"):
            lines.extend(["## Images", ""])
            lines.extend(f"- [Image {number}]({url})" for number, url in enumerate(record["images"], 1))
            lines.append("")
        if record.get("links"):
            lines.extend(["## Links", ""])
            lines.extend(f"- [{escape_md(link.get('text') or 'Link')}]({link.get('href', '')})" for link in record["links"])
            lines.append("")
        atomic_write_text(target, "\n".join(lines))
        return relative.as_posix()

    def finalize(
        self,
        command: str,
        records: list[dict[str, Any]],
        config: dict[str, Any] | None,
        status: str,
        query_state: dict[str, dict[str, Any]] | None = None,
        message: str = "",
    ) -> None:
        counts = status_counts(records)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": now_iso(),
            "command": command,
            "status": status,
            "config": config or {},
            "counts": counts,
            "records": records,
            "query_state": query_state or {},
            "message": message,
        }
        atomic_write_json(self.results_json, payload)
        atomic_write_text(self.results_csv, "\ufeff" + render_csv(records))
        atomic_write_text(self.summary_md, render_summary(payload, query_state or {}))
        self.write_run(command, status, config, counts, message)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def status_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    counts["records"] = len(records)
    for record in records:
        counts[f"search_{record.get('search_status', 'not_applicable')}"] += 1
        counts[f"fulltext_{record.get('fulltext_status', 'not_requested')}"] += 1
    return dict(counts)


def render_csv(records: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    fields = ["id", "published_date", "account", "title", "url", "search_status", "fulltext_status", "char_count", "matched_queries", "content_path", "error"]
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for record in records:
        row = {key: record.get(key, "") for key in fields}
        row["matched_queries"] = " | ".join(record.get("matched_queries", []))
        # Avoid executing article titles as spreadsheet formulas on import.
        row = {key: ("'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value) for key, value in row.items()}
        writer.writerow(row)
    return stream.getvalue()


def render_summary(payload: dict[str, Any], query_state: dict[str, dict[str, Any]]) -> str:
    records = payload.get("records", [])
    counts = payload.get("counts", {})
    lines = [
        "# WeChat Official Account Collection",
        "",
        f"- Generated: {payload.get('generated_at', '')}",
        f"- Status: {payload.get('status', '')}",
        f"- Records: {len(records)}",
        f"- Message: {payload.get('message', '')}",
        "",
        "## Status counts",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(counts.items()))
    if query_state:
        lines.extend(["", "## Query coverage", ""])
        for query, event in query_state.items():
            lines.append(
                f"- {query}: {event.get('status', 'unknown')}; visible={event.get('visible', 0)}; candidates={event.get('candidates', 0)}; rejected={event.get('rejected', {})}; stop={event.get('stop_reason', '')}"
            )
            if event.get("error"):
                lines.append(f"  - Error: {escape_md(event['error'])}")
    if payload.get("config", {}).get("target"):
        lines.extend(["", "Results are drawn from the visible WeRead index; counts do not establish exhaustive coverage."])
    lines.extend(
        [
            "",
            "## Results",
            "",
            "| # | Date | Account | Title | Search | Full text | Queries | Link | Article |",
            "|---:|---|---|---|---|---|---|---|---|",
        ]
    )
    for index, record in enumerate(records, 1):
        url = record.get("url") or record.get("article_url") or ""
        link = f"[Original]({url})" if url else ""
        content_path = record.get("content_path") or ""
        article = f"[{record['id']}]({content_path})" if content_path else ""
        title = record.get("article_title") or record.get("title") or ""
        account = record.get("article_account") or record.get("account") or ""
        lines.append(
            f"| {index} | {escape_md(record.get('published_date'))} | {escape_md(account)} | "
            f"{escape_md(title)} | {escape_md(record.get('search_status'))} | "
            f"{escape_md(record.get('fulltext_status'))} | "
            f"{escape_md(', '.join(record.get('matched_queries', [])))} | {link} | {article} |"
        )
    return "\n".join(lines) + "\n"


def cdp_base(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def cdp_json(port: int, suffix: str) -> Any:
    with LOCAL_HTTP_OPENER.open(cdp_base(port) + suffix, timeout=5) as response:
        return json.load(response)


def cdp_available(port: int) -> bool:
    try:
        return "webSocketDebuggerUrl" in cdp_json(port, "/json/version")
    except Exception:
        return False


def choose_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_browser(browser: str) -> str:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates: list[Path] = []
    if browser in {"auto", "edge"}:
        candidates.extend(
            [
                Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
                Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
            ]
        )
    if browser in {"auto", "chrome"}:
        candidates.extend(
            [
                local / "Google/Chrome/Application/chrome.exe",
                Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
                Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
            ]
        )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise SystemExit("Could not find Edge or Chrome. Pass --browser-path explicitly.")


class BrowserManager:
    def __init__(self, runtime: RuntimeConfig, start_url: str = "https://weread.qq.com/"):
        self.runtime = runtime
        self.start_url = start_url
        self.port: int | None = None
        self.process: subprocess.Popen[Any] | None = None
        self.started = False

    def start(self) -> int:
        profile = self.runtime.profile.resolve()
        profile.mkdir(parents=True, exist_ok=True)
        if self.runtime.cdp_port is not None and cdp_available(self.runtime.cdp_port):
            self.port = self.runtime.cdp_port
            return self.port
        executable = self.runtime.browser_path or find_browser(self.runtime.browser)
        port_arg = self.runtime.cdp_port if self.runtime.cdp_port is not None else choose_free_port()
        self.port = port_arg
        args = [
            executable,
            f"--remote-debugging-port={port_arg}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            self.start_url,
        ]
        if self.runtime.headless:
            args.insert(-1, "--headless=new")
            args.insert(-1, "--disable-gpu")
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.started = True
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cdp_available(port_arg):
                return self.port
            if self.process.poll() is not None:
                break
            time.sleep(0.25)
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        raise SystemExit("Browser started but its CDP endpoint did not become available.")

    async def close(self) -> None:
        if not self.started or self.runtime.keep_browser:
            return
        if self.port is not None:
            try:
                info = cdp_json(self.port, "/json/version")
                async with websockets.connect(
                    info["webSocketDebuggerUrl"], max_size=2**20, proxy=None
                ) as ws:
                    await ws.send(json.dumps({"id": 1, "method": "Browser.close", "params": {}}))
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=3)
                    except Exception:
                        pass
            except Exception:
                pass
        if self.process and self.process.poll() is None:
            try:
                await asyncio.to_thread(self.process.wait, 5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    await asyncio.to_thread(self.process.wait, 3)
                except subprocess.TimeoutExpired:
                    self.process.kill()


def get_or_create_page_ws(port: int, preferred_url: str = "", *, force_new: bool = False) -> str:
    targets = cdp_json(port, "/json")
    if preferred_url and not force_new:
        for target in targets:
            if target.get("type") == "page" and preferred_url in target.get("url", ""):
                return target["webSocketDebuggerUrl"]
    for target in ([] if force_new else targets):
        if target.get("type") == "page":
            return target["webSocketDebuggerUrl"]
    request = urllib.request.Request(
        cdp_base(port) + "/json/new?" + urllib.parse.quote("about:blank", safe=":/?&="), method="PUT"
    )
    with LOCAL_HTTP_OPENER.open(request, timeout=5) as response:
        return json.load(response)["webSocketDebuggerUrl"]


class CdpPage:
    def __init__(self, ws_url: str, timeout: float = 30.0):
        self.ws_url = ws_url
        self.timeout = timeout
        self.ws: Any = None
        self._id = 0

    async def __aenter__(self) -> "CdpPage":
        self.ws = await websockets.connect(self.ws_url, max_size=2**26, proxy=None)
        await self.call("Runtime.enable")
        await self.call("Page.enable")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.ws:
            await self.ws.close()

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        message_id = self._id
        await self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"CDP call timed out: {method}")
            message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=remaining))
            if message.get("id") != message_id:
                continue
            if message.get("error"):
                raise CdpError(f"{method}: {message['error']}")
            return message

    async def eval(self, expression: str, await_promise: bool = False) -> Any:
        response = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        result = response.get("result", {})
        if result.get("exceptionDetails"):
            raise CdpError(str(result["exceptionDetails"]))
        return result.get("result", {}).get("value")

    async def wait_for(self, expression: str, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while time.monotonic() < deadline:
            try:
                if await self.eval(expression):
                    return True
            except CdpError:
                pass
            await asyncio.sleep(0.25)
        return False

    async def navigate(self, url: str, timeout: float | None = None) -> None:
        old_frame = (await self.call("Page.getFrameTree"))["result"]["frameTree"]["frame"]
        response = await self.call("Page.navigate", {"url": url})
        error = response.get("result", {}).get("errorText")
        if error:
            raise CdpError(f"Navigation failed: {error}")
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        expected_loader = response.get("result", {}).get("loaderId")
        while time.monotonic() < deadline:
            try:
                frame = (await self.call("Page.getFrameTree"))["result"]["frameTree"]["frame"]
                ready = await self.eval("({url: document.URL, ready: document.readyState})")
                loader_ready = not expected_loader or frame.get("loaderId") == expected_loader or frame.get("loaderId") != old_frame.get("loaderId")
                # CDP separates urlFragment, while document.URL includes it.
                same_document = isinstance(ready, dict) and str(ready.get("url", "")).split("#", 1)[0] == str(frame.get("url", "")).split("#", 1)[0]
                if loader_ready and same_document and ready.get("ready") in {"interactive", "complete"}:
                    return
            except CdpError:
                pass
            await asyncio.sleep(0.1)
        raise TimeoutError(f"Page did not become ready: {url}")

    async def screenshot(self, output: Path) -> None:
        response = await self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        data = response.get("result", {}).get("data")
        if data:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(base64.b64decode(data))


async def ensure_login(page: CdpPage, bundle: RunBundle, timeout: float, force: bool = False) -> None:
    await page.navigate("https://weread.qq.com/", timeout=30)

    await page.wait_for(
        "(document.body?.innerText || '').includes('我的书架') || "
        "(document.body?.innerText || '').includes('继续阅读') || "
        "[...document.querySelectorAll('a,button')].some(el => "
        "(el.innerText || el.textContent || '').trim() === '登录' && !!el.offsetParent)",
        timeout=15,
    )
    await asyncio.sleep(1)

    async def is_logged_in() -> bool:
        state = await page.eval(
            """
            (() => {
              const body = document.body?.innerText || '';
              const loginVisible = [...document.querySelectorAll('a,button,div,span')].some(el => {
                const value = (el.innerText || el.textContent || '').trim();
                return value === '登录' && !!el.offsetParent;
              });
              return {
                hasSession: body.includes('我的书架') || body.includes('继续阅读'),
                loginVisible
              };
            })()
            """
        )
        return bool(isinstance(state, dict) and state.get("hasSession") and not state.get("loginVisible"))

    if await is_logged_in():
        return
    clicked = await page.eval(
        """
        (() => {
          for (const el of document.querySelectorAll('a,button')) {
            const value = (el.innerText || el.textContent || '').trim();
            if ((value === '登录' || value.includes('微信登录')) && !!el.offsetParent) {
              el.click();
              return {tag: el.tagName, className: String(el.className || ''), text: value};
            }
          }
          return null;
        })()
        """
    )
    if not clicked:
        raise LoginTimeoutError("WeRead is logged out, but its login control could not be found.")
    qr_ready = await page.wait_for("document.querySelector('.wr_login_modal_qr_img')", timeout=10)
    if not qr_ready:
        diagnostic = bundle.diagnostics_dir / "login-modal-missing.png"
        await page.screenshot(diagnostic)
        raise LoginTimeoutError(
            f"The WeRead login control was clicked but no QR modal appeared. Diagnostic: {diagnostic}"
        )
    qr_path = bundle.run_dir / "login-qr.png"
    src = await page.eval(
        """
        (() => {
          const qr = document.querySelector('.wr_login_modal_qr_img');
          return qr ? qr.src : '';
        })()
        """
    )
    saved = False
    if isinstance(src, str) and src:
        try:
            if src.startswith("data:image"):
                payload = src.split(",", 1)[1]
                qr_path.write_bytes(base64.b64decode(payload + "=" * ((4 - len(payload) % 4) % 4)))
                saved = True
            elif src.startswith(("http://", "https://")):
                urllib.request.urlretrieve(src, qr_path)
                saved = True
        except Exception:
            saved = False
    if not saved:
        diagnostic = bundle.diagnostics_dir / "login-qr-save-failed.png"
        await page.screenshot(diagnostic)
        raise LoginTimeoutError(f"The WeRead QR image could not be saved. Diagnostic: {diagnostic}")
    print(f"WeRead login required. Scan the QR in the browser or image: {qr_path}", file=sys.stderr)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await is_logged_in():
            print("WeRead login confirmed; continuing.", file=sys.stderr)
            return
        await asyncio.sleep(2)
    raise LoginTimeoutError(f"WeRead login was not completed within {timeout:.0f} seconds. QR: {qr_path}")


def search_url(query: str) -> str:
    return WECHAT_SEARCH_URL.format(query=urllib.parse.quote(query))


async def scroll_results(page: CdpPage, limit: int, rounds: int) -> int:
    previous = -1
    stable = 0
    for _ in range(rounds):
        count = await page.eval("document.querySelectorAll('.search_list_item').length")
        if isinstance(count, int) and count >= limit:
            return count
        await page.eval(
            """(() => {
              const last = [...document.querySelectorAll('.search_list_item')].at(-1);
              if (last) last.scrollIntoView({block: 'end'});
              let node = last?.parentElement;
              while (node && node !== document.body) {
                if (node.scrollHeight > node.clientHeight && /auto|scroll/.test(getComputedStyle(node).overflowY)) {
                  node.scrollTop = node.scrollHeight;
                  node.dispatchEvent(new Event('scroll'));
                  break;
                }
                node = node.parentElement;
              }
              window.scrollTo(0, document.body.scrollHeight);
              window.dispatchEvent(new Event('scroll'));
            })()"""
        )
        await asyncio.sleep(1.2)
        count = await page.eval("document.querySelectorAll('.search_list_item').length")
        body = str(await page.eval("document.body ? document.body.innerText : ''") or "")
        if any(marker in body for marker in RISK_MARKERS) and not count:
            raise SearchRiskControlError(body[:400])
        stable = stable + 1 if count == previous else 0
        previous = count if isinstance(count, int) else previous
        if stable >= 2 or "暂无更多内容" in body:
            break
    return max(previous, 0)


async def extract_metadata(page: CdpPage, limit: int) -> list[dict[str, Any]]:
    rows = await page.eval(
        f"""
        (() => {{
          const rows = [];
          [...document.querySelectorAll('.search_list_item')].slice(0, {int(limit)}).forEach((item, i) => {{
            const pick = sel => (item.querySelector(sel)?.textContent || '').trim();
            rows.push({{
              idx: i,
              title: pick('.article__title-text'),
              snippet: pick('.article__desc').slice(0, 500),
              account: pick('.source__title'),
              date_raw: pick('.source__text.date'),
              direct_url: item.querySelector('a[href*="mp.weixin.qq.com"]')?.href || '',
              data_id: item.getAttribute('data-id') || ''
            }});
          }});
          return rows;
        }})()
        """
    )
    return rows if isinstance(rows, list) else []


async def _extract_url_batch(page: CdpPage, indices: list[int], wait_ms: int) -> dict[int, str]:
    rows = await page.eval(
        f"""
        (async () => {{
          const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
          const items = document.querySelectorAll('.search_list_item');
          const targets = {json.dumps(indices)};
          const output = [];
          const originalOpen = window.open;
          try {{
            for (const idx of targets) {{
              const item = items[idx];
              if (!item) {{ output.push({{idx, url: ''}}); continue; }}
              const direct = item.querySelector('a[href*="mp.weixin.qq.com"]');
              if (direct) {{ output.push({{idx, url: direct.href}}); continue; }}
              let captured = '';
              window.open = function(url) {{ if (String(url || '').includes('mp.weixin.qq.com')) captured = String(url); return null; }};
              item.scrollIntoView({{block: 'center', behavior: 'instant'}});
              item.click();
              const deadline = performance.now() + {int(wait_ms)};
              while (!captured && performance.now() < deadline) await sleep(40);
              output.push({{idx, url: captured}});
            }}
          }} finally {{ window.open = originalOpen; }}
          return output;
        }})()
        """,
        await_promise=True,
    )
    return {int(row.get("idx", -1)): str(row.get("url", "")) for row in (rows or [])}


async def extract_urls(page: CdpPage, indices: list[int], batch_size: int) -> dict[int, str]:
    output: dict[int, str] = {}
    effective_batch = min(batch_size, max(1, int(getattr(page, 'timeout', 30) / 1.2)))
    for start in range(0, len(indices), effective_batch):
        output.update(await _extract_url_batch(page, indices[start : start + effective_batch], 800))
    missing = [index for index in indices if not canonicalize_url(output.get(index, ""))[2]]
    for index in missing:
        retry = await _extract_url_batch(page, [index], 1500)
        output[index] = retry.get(index, output.get(index, ""))
    return output


SEARCH_STATE_JS = """
(() => {
  const clone = document.body?.cloneNode(true);
  clone?.querySelectorAll('.search_list_item, script, style').forEach(el => el.remove());
  return {
    card_count: document.querySelectorAll('.search_list_item').length,
    gate_text: clone?.textContent || '',
    broker: window.__codexWxBroker || null,
    url: location.href
  };
})()
"""


def classify_search_state(snapshot: dict[str, Any]) -> tuple[str, str]:
    text = str(snapshot.get("gate_text") or "")
    broker = snapshot.get("broker") or {}
    try:
        data = json.loads(str(broker.get("text") or "{}"))
    except (json.JSONDecodeError, AttributeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if any(marker in text for marker in RISK_MARKERS):
        return "risk_control", text[:400]
    if data.get("errCode") == -2010:
        return "auth_required", str(data.get("errMsg") or "WeRead login expired.")
    if broker.get("status", 200) >= 400 or data.get("errCode") not in {None, 0}:
        return "error", str(data.get("errMsg") or broker.get("text") or "Search request failed.")[:400]
    if snapshot.get("card_count", 0):
        return "ok", ""
    if any(marker in text for marker in ("暂无搜索结果", "没有搜索结果", "暂无相关", "没有找到", "未找到相关", "暂无内容")):
        return "no_results", ""
    return "error", "No result cards or explicit empty state appeared; inspect the diagnostic for login, loading, or selector changes."


def select_records(records: Iterable[dict[str, Any]], config: SearchConfig, reference_date: date) -> list[dict[str, Any]]:
    kept = []
    for raw in records:
        record = dict(raw)
        accepted, published, _reason = passes_filters(record, config, reference_date)
        if accepted:
            record["published_date"] = published.isoformat() if published else ""
            record["ranking_queries"] = config.queries
            kept.append(record)
    return merge_and_rank(kept, config.target, config.sort)


async def collect_candidates(
    page: CdpPage,
    config: SearchConfig,
    bundle: RunBundle,
    reference_date: date,
    runtime: RuntimeConfig | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    runtime = runtime or RuntimeConfig()
    hook = await page.call(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": """
            (() => {
              const originalOpen = XMLHttpRequest.prototype.open;
              const originalSend = XMLHttpRequest.prototype.send;
              XMLHttpRequest.prototype.open = function(method, url) {
                this.__codexUrl = String(url || '');
                return originalOpen.apply(this, arguments);
              };
              XMLHttpRequest.prototype.send = function() {
                this.addEventListener('load', () => {
                  if ((this.__codexUrl || '').includes('wx_search_broker_proxy')) {
                    window.__codexWxBroker = {status: this.status, text: this.responseText || ''};
                  }
                });
                return originalSend.apply(this, arguments);
              };
            })();
            """
        },
    )
    state = bundle.load_checkpoint()
    candidates = list(state.candidates)
    completed = dict(state.query_completed)
    for query in config.queries:
        prior = completed.get(query, {})
        if prior.get("status") in TERMINAL_QUERY_STATUSES and prior.get("per_query_limit", config.per_query_limit) >= config.per_query_limit and prior.get("max_scroll_rounds", config.max_scroll_rounds) >= config.max_scroll_rounds:
            print(f"[query resume] {query}", file=sys.stderr)
            continue
        print(f"[query] {query}", file=sys.stderr)
        query_event: dict[str, Any]
        try:
            await asyncio.sleep(random.uniform(runtime.delay_min, runtime.delay_max))
            await page.navigate(search_url(query))
            await page.wait_for(
                "document.querySelectorAll('.search_list_item').length > 0 || "
                "window.__codexWxBroker || /访问频繁|环境异常|暂无搜索结果|暂无相关|没有找到/.test(document.body?.innerText || '')",
                timeout=min(runtime.timeout, 18),
            )
            snapshot = await page.eval(SEARCH_STATE_JS)
            search_status, search_error = classify_search_state(snapshot)
            # The broker can finish before the UI renders its result cards.
            if search_status == "error" and not search_error.startswith("Search request"):
                await page.wait_for("document.querySelectorAll('.search_list_item').length > 0", timeout=min(runtime.timeout, 3))
                snapshot = await page.eval(SEARCH_STATE_JS)
                search_status, search_error = classify_search_state(snapshot)
            if search_status == "auth_required":
                raise SearchAuthenticationError(search_error)
            if search_status == "risk_control":
                raise SearchRiskControlError(search_error)
            if search_status == "error":
                raise CdpError(search_error)
            if search_status == "no_results":
                query_event = {
                    "type": "query_complete",
                    "query": query,
                    "status": "no_results",
                    "candidates": 0,
                    "stop_reason": "explicit_empty_state",
                }
            else:
                await scroll_results(page, config.per_query_limit, config.max_scroll_rounds)
                final_state, final_error = classify_search_state(await page.eval(SEARCH_STATE_JS))
                if final_state == "risk_control":
                    raise SearchRiskControlError(final_error)
                rows = await extract_metadata(page, config.per_query_limit)
                if rows and not any(row.get("title") for row in rows):
                    raise CdpError("Search card titles are missing; selectors may have changed.")
                rejected: Counter[str] = Counter()
                assessed: list[tuple[dict[str, Any], date | None, bool, str]] = []
                for row in rows:
                    accepted, published, reason = passes_filters(row, config, reference_date)
                    assessed.append((row, published, accepted, reason))
                    if not accepted:
                        rejected[reason] += 1
                urls = await extract_urls(page, [int(row["idx"]) for row in rows], config.batch_size)
                for row, published, accepted, reason in assessed:
                    url = urls.get(int(row["idx"]), "")
                    canonical, _identity, valid = canonicalize_url(url)
                    record = {
                        "title": row.get("title", ""),
                        "account": row.get("account", ""),
                        "date_raw": row.get("date_raw", ""),
                        "published_date": published.isoformat() if published else "",
                        "snippet": row.get("snippet", ""),
                        "matched_queries": [query],
                        "source_rank": int(row.get("idx", 0)) + 1,
                        "data_id": row.get("data_id", ""),
                        "collected_on": reference_date.isoformat(),
                        "filter_passed": accepted,
                        "filter_reason": reason,
                        "url": url,
                        "canonical_url": canonical,
                        "search_status": "ok" if valid else ("no_url" if not url else "invalid_url"),
                        "fulltext_status": "not_requested",
                        "attempts": {"search_url": 2 if not valid else 1, "fulltext": 0},
                        "error": "" if valid else "No valid mp.weixin.qq.com URL was captured.",
                    }
                    record["id"] = stable_record_id(record)
                    candidates.append(record)
                    bundle.append({"type": "candidate", "record": record})
                query_event = {
                    "type": "query_complete",
                    "query": query,
                    "status": "ok",
                    "visible": len(rows),
                    "candidates": sum(item[2] for item in assessed),
                    "rejected": dict(rejected),
                    "stop_reason": "per_query_limit" if len(rows) >= config.per_query_limit else "scroll_exhausted_or_stable",
                }
        except SearchAuthenticationError:
            await page.call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": hook["result"]["identifier"]})
            raise
        except SearchRiskControlError as exc:
            query_event = {"type": "query_complete", "query": query, "status": "risk_control", "candidates": 0, "error": str(exc)}
        except Exception as exc:
            query_event = {
                "type": "query_complete",
                "query": query,
                "status": "error",
                "candidates": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }
        if query_event["status"] in {"error", "risk_control"}:
            diagnostic = bundle.diagnostics_dir / f"query-{hashlib.sha256(query.encode()).hexdigest()[:12]}-{query_event['status']}.png"
            try:
                await page.screenshot(diagnostic)
                query_event["diagnostic_path"] = diagnostic.relative_to(bundle.run_dir).as_posix()
            except Exception:
                pass
        query_event.update({"per_query_limit": config.per_query_limit, "max_scroll_rounds": config.max_scroll_rounds})
        bundle.append(query_event)
        completed[query] = query_event
        print(
            f"  status={query_event['status']} candidates={query_event.get('candidates', 0)}",
            file=sys.stderr,
        )
        if query_event["status"] == "risk_control":
            break
    await page.call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": hook["result"]["identifier"]})
    pool = merge_and_rank(candidates)
    atomic_write_json(bundle.candidates_json, {"schema_version": SCHEMA_VERSION, "generated_at": now_iso(), "config": config.to_json(), "query_state": completed, "records": pool})
    return select_records(pool, config, reference_date), completed


ARTICLE_EXTRACT_JS = """
(() => {
  const text = el => (el && el.getClientRects().length ? el.innerText || '' : '').trim();
  const pick = selectors => {
    for (const selector of selectors) {
      const value = text(document.querySelector(selector));
      if (value) return value;
    }
    return '';
  };
  const content = document.querySelector('#js_content')
    || document.querySelector('.rich_media_content')
    || document.querySelector('article');
  const images = [...document.querySelectorAll('#js_content img, .rich_media_content img, article img')]
    .filter(img => img.getClientRects().length)
    .map(img => img.getAttribute('data-src') || img.getAttribute('src') || '')
    .filter(Boolean)
    .map(src => { try { return new URL(src, location.href).href; } catch { return src; } });
  const links = [...document.querySelectorAll('#js_content a, .rich_media_content a, article a')]
    .map(a => ({text: text(a), href: a.href || ''}))
    .filter(item => item.href);
  return {
    body_text: text(document.body),
    gate_text: [...document.querySelectorAll('#js_verify, .weui-msg__title, .weui_msg_title, .weui-msg__desc, .weui_msg_desc, .page_msg .text_area')].map(text).join(' '),
    fulltext: text(content),
    article_url: location.href,
    article_title: pick(['#activity-name', '.rich_media_title', 'h1']) || document.title,
    article_account: pick(['#js_name', '.profile_nickname', '.rich_media_meta_nickname']),
    article_date: pick(['#publish_time', '#js_time']) || [...document.querySelectorAll('.rich_media_meta_text')].map(text).find(value => /\\d{4}[年/.-]\\d{1,2}[月/.-]\\d{1,2}/.test(value)) || '',
    images: [...new Set(images)],
    links
  };
})()
"""


async def extract_article_once(page: CdpPage, url: str, timeout: float) -> dict[str, Any]:
    await page.navigate(url, timeout=timeout)
    await page.wait_for(
        "[...document.querySelectorAll('#js_content, .rich_media_content, article')].some(el => (el.innerText || '').trim() || el.querySelector('img')) || "
        f"/{'|'.join(map(re.escape, RISK_MARKERS + DELETED_MARKERS + CLIENT_ONLY_MARKERS + BLOCKED_MARKERS))}/.test(document.body?.innerText || '')",
        timeout=timeout,
    )
    await page.eval("window.scrollTo(0, Math.min(document.body.scrollHeight, 1200));")
    await asyncio.sleep(0.4)
    await page.eval("window.scrollTo(0, document.body.scrollHeight);")
    await asyncio.sleep(0.4)
    data = await page.eval(ARTICLE_EXTRACT_JS)
    if not isinstance(data, dict):
        raise CdpError("Article extraction did not return an object.")
    status, reason = classify_article_text(data.pop("body_text", ""), data.get("fulltext", ""), gate_text=data.pop("gate_text", ""), image_count=len(data.get("images") or []))
    data["fulltext_status"] = status
    data["error"] = reason
    data["char_count"] = len(str(data.get("fulltext") or ""))
    data["image_count"] = len(data.get("images") or [])
    data["fulltext_captured_at"] = now_iso()
    data["content_sha256"] = hashlib.sha256(str(data.get("fulltext") or "").encode("utf-8")).hexdigest()
    return data


async def extract_article_with_retries(
    page: CdpPage, url: str, timeout: float, retries: int
) -> dict[str, Any]:
    last: dict[str, Any] = {
        "fulltext_status": "navigation_error",
        "error": "No extraction attempt completed.",
        "fulltext": "",
        "char_count": 0,
    }
    for attempt in range(1, retries + 2):
        try:
            last = await extract_article_once(page, url, timeout)
        except TimeoutError as exc:
            last = {"fulltext_status": "timeout", "error": str(exc), "fulltext": "", "char_count": 0}
        except CdpError as exc:
            last = {"fulltext_status": "eval_error", "error": str(exc), "fulltext": "", "char_count": 0}
        except Exception as exc:
            last = {
                "fulltext_status": "navigation_error",
                "error": f"{type(exc).__name__}: {exc}",
                "fulltext": "",
                "char_count": 0,
            }
        last["attempt_count"] = attempt
        status = last.get("fulltext_status")
        if status in TERMINAL_FULLTEXT_STATUSES or status == "risk_control":
            return last
        if attempt <= retries:
            await asyncio.sleep(2 ** (attempt - 1))
    return last


def normalize_input_record(raw: dict[str, Any]) -> dict[str, Any]:
    record = dict(raw)
    record["url"] = html.unescape(str(record.get("url") or record.get("article_url") or "").strip())
    record.setdefault("title", record.get("article_title", ""))
    record.setdefault("account", record.get("source") or record.get("article_account", ""))
    record.setdefault("snippet", record.get("desc", ""))
    record.setdefault("date_raw", record.get("article_date", ""))
    record.setdefault("published_date", record.get("date", ""))
    record.setdefault("matched_queries", [record["query"]] if record.get("query") else [])
    record.setdefault("source_rank", int(record.get("idx", 0)) + 1)
    record.setdefault("search_status", "ok" if canonicalize_url(record.get("url", ""))[2] else "not_applicable")
    record.setdefault("fulltext_status", "not_requested")
    record.setdefault("attempts", {"search_url": 0, "fulltext": 0})
    if not isinstance(record["attempts"], dict):
        record["attempts"] = {"search_url": 0, "fulltext": 0}
    if record["fulltext_status"] == "ok" and not str(record.get("fulltext") or "").strip():
        record["fulltext_status"] = "not_requested"
    if record["fulltext_status"] == "image_only" and not record.get("images"):
        record["fulltext_status"] = "not_requested"
    record.setdefault("error", "")
    supplied_id = str(record.get("id") or "")
    record["id"] = supplied_id if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", supplied_id) else stable_record_id(record)
    canonical, _identity, valid = canonicalize_url(record.get("url") or record.get("article_url") or "")
    if valid:
        record["canonical_url"] = canonical
    return record


def load_input_records(path: Path | None, urls: list[str] | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if path:
        target = path / "results.json" if path.is_dir() else path
        if not target.exists():
            raise SystemExit(f"Input does not exist: {target}")
        if target.suffix.lower() == ".jsonl":
            for number, line in enumerate(target.read_text(encoding="utf-8-sig").splitlines(), 1):
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError(f"Input JSONL line {number} must be an object.")
                records.append(item)
        elif target.suffix.lower() == ".txt":
            records.extend({"url": line.strip()} for line in target.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#"))
        else:
            data = json.loads(target.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                data = data.get("records", [data] if data.get("url") or data.get("article_url") else [])
            if not isinstance(data, list):
                raise SystemExit("Input JSON must be an array or an object with a records array.")
            records.extend(item for item in data if isinstance(item, dict))
    records.extend({"url": url} for url in (urls or []))
    normalized = [normalize_input_record(record) for record in records]
    return merge_and_rank(normalized)


async def fetch_records(
    page: CdpPage,
    records: list[dict[str, Any]],
    bundle: RunBundle,
    runtime: RuntimeConfig,
    limit: int | None,
    *,
    preserve_saved_scope: bool = True,
) -> tuple[list[dict[str, Any]], str, str]:
    if limit is not None and limit < 0:
        raise ValueError("full-text limit must be non-negative.")
    state = bundle.load_checkpoint()
    inputs = dict(state.input_records)
    if not inputs:
        for record in records:
            inputs[record["id"]] = record
            bundle.append({"type": "input", "record": record})
    else:
        for record in records:
            if record["id"] not in inputs:
                inputs[record["id"]] = record
                bundle.append({"type": "input", "record": record})
    ordered = list(inputs.values()) if preserve_saved_scope else records
    fetch_ids = {
        record["id"] for record in (ordered[:limit] if limit is not None else ordered)
    }
    fetch_total = len(fetch_ids)
    fetch_index = 0
    output: list[dict[str, Any]] = []
    stopped_status = "complete"
    message = ""
    for index, base in enumerate(ordered, 1):
        existing = state.fulltext_records.get(base["id"])
        if existing:
            existing = {**base, **existing, "matched_queries": dedupe_list(list(base.get("matched_queries", [])) + list(existing.get("matched_queries", [])))}
        if not existing and base.get("fulltext_status") in {"ok", "image_only"}:
            existing = dict(base)
        if base["id"] not in fetch_ids:
            saved = dict(existing or base)
            if saved.get("fulltext_status") in {"ok", "image_only"}:
                saved["content_path"] = bundle.write_article(saved)
            else:
                saved.pop("content_path", None)
            output.append(saved)
            continue
        fetch_index += 1
        if existing and existing.get("fulltext_status") in TERMINAL_FULLTEXT_STATUSES:
            if existing.get("fulltext_status") in {"ok", "image_only"}:
                existing["content_path"] = bundle.write_article(existing)
            output.append(existing)
            print(f"[fulltext resume] {fetch_index}/{fetch_total} {base['id']}", file=sys.stderr)
            continue
        url = base.get("url") or base.get("article_url") or ""
        if not canonicalize_url(url)[2]:
            enriched = dict(base)
            enriched.update({"fulltext_status": "invalid_url", "error": "A valid mp.weixin.qq.com URL is required."})
        else:
            print(f"[fulltext] {fetch_index}/{fetch_total} {url[:100]}", file=sys.stderr)
            article = await extract_article_with_retries(page, url, runtime.timeout, runtime.retries)
            enriched = dict(base)
            enriched.update(article)
            attempts = dict(enriched.get("attempts") or {})
            attempts["fulltext"] = int((existing or base).get("attempts", {}).get("fulltext", 0)) + int(article.get("attempt_count", 1))
            enriched["attempts"] = attempts
            parsed = parse_wechat_date(str(enriched.get("article_date") or ""))
            if parsed:
                enriched["published_date"] = parsed.isoformat()
        if enriched.get("fulltext_status") in {"ok", "image_only"}:
            enriched["content_path"] = bundle.write_article(enriched)
        elif enriched.get("fulltext_status") != "invalid_url":
            diagnostic = bundle.diagnostics_dir / f"{enriched['id']}-{enriched.get('fulltext_status', 'error')}.png"
            try:
                await page.screenshot(diagnostic)
                enriched["diagnostic_path"] = diagnostic.relative_to(bundle.run_dir).as_posix()
            except Exception:
                pass
        bundle.append({"type": "fulltext", "record": enriched})
        state.fulltext_records[enriched["id"]] = enriched
        output.append(enriched)
        if enriched.get("fulltext_status") == "risk_control":
            stopped_status = "risk_control"
            message = "WeChat risk control was detected; remaining requests were stopped and checkpointed."
            remaining = [item for item in ordered[index:] if item["id"] not in {row["id"] for row in output}]
            output.extend(state.fulltext_records.get(item["id"], item) for item in remaining)
            break
        if fetch_index < fetch_total:
            await asyncio.sleep(random.uniform(runtime.delay_min, runtime.delay_max))
    existing_ids = {record["id"] for record in output}
    output.extend(record for record in ordered if record["id"] not in existing_ids)
    if stopped_status == "complete" and any(record.get("fulltext_status") not in TERMINAL_FULLTEXT_STATUSES for record in output if record["id"] in fetch_ids):
        stopped_status = "partial"
        message = "Some requested articles have transient failures; inspect diagnostics and resume."
    return output, stopped_status, message


def config_value(args: argparse.Namespace, data: dict[str, Any], name: str, default: Any) -> Any:
    value = getattr(args, name, None)
    return value if value is not None else data.get(name, default)


def load_search_config(args: argparse.Namespace, fallback: dict[str, Any] | None = None) -> SearchConfig:
    data = dict(fallback or {})
    if args.query_file:
        raw = args.query_file.read_text(encoding="utf-8-sig")
        if args.query_file.suffix.lower() == ".json":
            loaded = json.loads(raw)
            if not isinstance(loaded, dict):
                raise SystemExit("Query JSON must be an object.")
            unknown = set(loaded) - set(SearchConfig.__dataclass_fields__)
            if unknown:
                raise ValueError(f"Unknown query config fields: {', '.join(sorted(unknown))}")
            data.update(loaded)
        else:
            data["queries"] = [line.strip() for line in raw.splitlines() if line.strip()]
    for name in ("queries", "contains_any", "contains_all", "exclude_any", "source_include_any", "source_exclude_any"):
        if name in data and (not isinstance(data[name], list) or not all(isinstance(value, str) for value in data[name])):
            raise ValueError(f"{name} must be an array of strings.")
    queries = list(data.get("queries") or []) + list(args.query or [])
    config = SearchConfig(
        queries=dedupe_list(queries),
        target=int(config_value(args, data, "target", 60)),
        min_date=parse_iso_date(config_value(args, data, "min_date", None)),
        max_date=parse_iso_date(config_value(args, data, "max_date", None)) or date.today(),
        contains_any=dedupe_list(list(data.get("contains_any") or []) + list(args.contains_any or [])),
        contains_all=dedupe_list(list(data.get("contains_all") or []) + list(args.contains_all or [])),
        exclude_any=dedupe_list(list(data.get("exclude_any") or []) + list(args.exclude_any or [])),
        source_include_any=dedupe_list(
            list(data.get("source_include_any") or []) + list(args.source_include_any or [])
        ),
        source_exclude_any=dedupe_list(
            list(data.get("source_exclude_any") or []) + list(args.source_exclude_any or [])
        ),
        unknown_date=str(config_value(args, data, "unknown_date", "drop")),
        per_query_limit=int(config_value(args, data, "per_query_limit", 50)),
        max_scroll_rounds=int(config_value(args, data, "max_scroll_rounds", 6)),
        batch_size=int(config_value(args, data, "batch_size", 30)),
        sort=str(config_value(args, data, "sort", "relevance")),
    )
    config.validate()
    return config


def runtime_from_args(args: argparse.Namespace) -> RuntimeConfig:
    runtime = RuntimeConfig(
        browser=args.browser,
        browser_path=args.browser_path,
        profile=args.profile,
        cdp_port=args.cdp_port,
        keep_browser=args.keep_browser,
        login_timeout=args.login_timeout,
        timeout=args.timeout,
        retries=args.retries,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        headless=args.headless,
    )
    runtime.validate()
    return runtime


def default_run_dir(command: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(f"wechat-{command}-{stamp}")


def add_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", type=Path, help="Task directory. Defaults to a timestamped directory.")
    parser.add_argument("--resume", action="store_true", help="Resume from the task directory checkpoint.")
    parser.add_argument("--browser", choices=["auto", "edge", "chrome"], default="edge")
    parser.add_argument("--browser-path")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--cdp-port", type=int, help="Attach to this CDP port or launch on it; dynamic by default.")
    parser.add_argument("--keep-browser", action="store_true")
    parser.add_argument("--headless", action="store_true", help="Use only with an already authenticated profile or local fixtures.")
    parser.add_argument("--login-timeout", type=float, default=120.0)
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-page timeout in seconds.")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--delay-min", type=float, default=1.5)
    parser.add_argument("--delay-max", type=float, default=3.0)


def add_search_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--query", action="append")
    parser.add_argument("--query-file", type=Path)
    parser.add_argument("--target", type=int)
    parser.add_argument("--min-date")
    parser.add_argument("--max-date")
    parser.add_argument("--contains-any", action="append")
    parser.add_argument("--contains-all", action="append")
    parser.add_argument("--exclude-any", action="append")
    parser.add_argument("--source-include-any", action="append")
    parser.add_argument("--source-exclude-any", action="append")
    parser.add_argument("--unknown-date", choices=["drop", "keep"])
    parser.add_argument("--per-query-limit", type=int)
    parser.add_argument("--max-scroll-rounds", type=int)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--sort", choices=["relevance", "newest"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    search = subparsers.add_parser("search", help="Search WeRead and capture article metadata and URLs.")
    add_search_args(search)
    add_runtime_args(search)
    fetch = subparsers.add_parser("fetch", help="Fetch visible article text from results or URLs.")
    fetch.add_argument("--input", type=Path, help="New/legacy JSON, JSONL, text URL list, or run directory.")
    fetch.add_argument("--url", action="append", help="Direct mp.weixin.qq.com URL; repeat as needed.")
    fetch.add_argument("--limit", type=int)
    add_runtime_args(fetch)
    collect = subparsers.add_parser("collect", help="Search and then fetch visible article text.")
    add_search_args(collect)
    collect.add_argument("--fulltext-limit", type=int)
    add_runtime_args(collect)
    rank = subparsers.add_parser("rank", help="Filter and rank saved candidates offline, exporting JSON, CSV, and Markdown.")
    rank.add_argument("--input", type=Path, required=True)
    rank.add_argument("--run-dir", type=Path)
    add_search_args(rank)
    doctor = subparsers.add_parser("doctor", help="Check runtime/browser availability without opening a website.")
    doctor.add_argument("--browser", choices=["auto", "edge", "chrome"], default="edge")
    doctor.add_argument("--browser-path")
    doctor.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    doctor.add_argument("--cdp-port", type=int)
    doctor.add_argument("--output", type=Path)
    return parser


def diagnose(args: argparse.Namespace) -> int:
    checks: dict[str, Any] = {"python": sys.version.split()[0], "websockets": websockets.__version__}
    try:
        browser_path = args.browser_path or find_browser(args.browser)
        checks["browser"] = {"available": Path(browser_path).is_file(), "path": browser_path}
    except SystemExit as exc:
        checks["browser"] = {"available": False, "error": str(exc)}
    checks["profile"] = {"path": str(args.profile.resolve()), "exists": args.profile.exists()}
    if args.cdp_port is not None:
        if not 1 <= args.cdp_port <= 65535:
            raise ValueError("cdp_port must be between 1 and 65535.")
        checks["cdp_available"] = cdp_available(args.cdp_port)
    checks["ok"] = bool(checks["browser"]["available"] and checks.get("cdp_available", True))
    if args.output:
        atomic_write_json(args.output, checks)
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0 if checks["ok"] else 2


def execute_rank(args: argparse.Namespace) -> int:
    source = args.input
    if source.is_dir():
        source = source / ("candidates.json" if (source / "candidates.json").exists() else "results.json")
    data = json.loads(source.read_text(encoding="utf-8-sig"))
    fallback = data.get("config", {}) if isinstance(data, dict) else {}
    records = load_input_records(source, None)
    if args.input.is_dir() and source.name == "candidates.json" and (args.input / "results.json").exists():
        by_id = {record["id"]: record for record in records}
        for enriched in load_input_records(args.input / "results.json", None):
            existing = by_id.get(enriched["id"], {})
            queries = dedupe_list(list(existing.get("matched_queries", [])) + list(enriched.get("matched_queries", [])))
            by_id[enriched["id"]] = {**existing, **enriched, "matched_queries": queries}
        records = list(by_id.values())
    if not fallback.get("queries"):
        fallback["queries"] = dedupe_list(query for record in records for query in record.get("matched_queries", [])) or ["离线筛选"]
    config = load_search_config(args, fallback)
    bundle = RunBundle(args.run_dir or default_run_dir("rank"), resume=False)
    selected = select_records(records, config, date.today())
    for record in selected:
        if record.get("fulltext_status") in {"ok", "image_only"}:
            record["content_path"] = bundle.write_article(record)
        else:
            record.pop("content_path", None)
        record.pop("diagnostic_path", None)
    atomic_write_json(bundle.candidates_json, {"schema_version": SCHEMA_VERSION, "config": config.to_json(), "records": records})
    bundle.finalize("rank", selected, config.to_json(), "complete", data.get("query_state", {}) if isinstance(data, dict) else {}, message=f"Offline selection: {len(selected)} of {len(records)} saved candidates; no website was opened.")
    print(f"Offline results: {bundle.run_dir}", file=sys.stderr)
    return 0


def finalize_recovered(bundle: RunBundle, command: str, config: SearchConfig | None, records: list[dict[str, Any]], status: str, message: str) -> None:
    state = bundle.load_checkpoint()
    bases = list(state.input_records.values())
    if not bases:
        bases = records or state.candidates
        if config:
            bases = select_records(bases, config, date.fromisoformat(bundle.load_run()["started_at"][:10]))
    restored = [state.fulltext_records.get(record["id"], record) for record in bases]
    bundle.finalize(command, restored, config.to_json() if config else bundle.load_run().get("config", {}), status, state.query_completed, message)


async def run_search_stage(
    args: argparse.Namespace,
    runtime: RuntimeConfig,
    bundle: RunBundle,
    config: SearchConfig,
    page: CdpPage,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    state = bundle.load_checkpoint()
    reference = date.fromisoformat(bundle.load_run()["started_at"][:10])
    if all(state.query_completed.get(query, {}).get("status") in TERMINAL_QUERY_STATUSES for query in config.queries):
        return select_records(state.candidates, config, reference), state.query_completed
    await ensure_login(page, bundle, runtime.login_timeout)
    try:
        return await collect_candidates(page, config, bundle, reference, runtime)
    except SearchAuthenticationError:
        await ensure_login(page, bundle, runtime.login_timeout, force=True)
        return await collect_candidates(page, config, bundle, reference, runtime)


async def execute(args: argparse.Namespace) -> int:
    run_dir = args.run_dir or default_run_dir(args.command)
    bundle = RunBundle(run_dir, args.resume)
    runtime = runtime_from_args(args)
    previous = bundle.load_run()
    fallback_config = previous.get("config") if args.resume else None
    config: SearchConfig | None = None
    if args.command in {"search", "collect"}:
        config = load_search_config(args, fallback_config)
    if args.resume and previous.get("command") not in {None, args.command}:
        raise ValueError("Resume using the original command; enrich a search with fetch --input in a new run directory.")
    if args.resume and config and previous.get("config") and config.to_json() != load_search_config(build_parser().parse_args([args.command]), previous["config"]).to_json():
        raise ValueError("Search settings changed on resume; use rank for offline filters or start a new search directory.")
    limit = args.limit if args.command == "fetch" else getattr(args, "fulltext_limit", None)
    if args.resume and limit is None:
        limit = previous.get("options", {}).get("fulltext_limit")
    if limit is not None and limit < 0:
        raise ValueError("full-text limit must be non-negative.")
    if args.command == "fetch":
        records = load_input_records(args.input, args.url)
        state = bundle.load_checkpoint()
        if not records and args.resume:
            records = list(state.input_records.values()) or merge_and_rank(state.candidates)
        if not records:
            raise ValueError("Provide --input or at least one --url.")
    else:
        records = []
    bundle.acquire_lock()
    browser = BrowserManager(runtime)
    query_state: dict[str, dict[str, Any]] = {}
    page: CdpPage | None = None
    try:
        bundle.write_run(args.command, "running", config.to_json() if config else previous.get("config", {}), options={"fulltext_limit": limit})
        port = await asyncio.to_thread(browser.start)
        ws_url = get_or_create_page_ws(port, force_new=True)
        async with CdpPage(ws_url, timeout=runtime.timeout) as page:
            if args.command in {"search", "collect"}:
                assert config is not None
                records, query_state = await run_search_stage(args, runtime, bundle, config, page)
                bundle.finalize(args.command, records, config.to_json(), "search_complete", query_state)
                if any(event.get("status") == "risk_control" for event in query_state.values()):
                    bundle.finalize(args.command, records, config.to_json(), "risk_control", query_state, "Search risk control detected; all further requests were stopped.")
                    return 2
            if args.command == "fetch":
                records, status, message = await fetch_records(page, records, bundle, runtime, limit)
                bundle.finalize(args.command, records, previous.get("config", {}), status, message=message)
                return 0 if status == "complete" else 2
            if args.command == "collect":
                records, status, message = await fetch_records(
                    page, records, bundle, runtime, limit, preserve_saved_scope=False
                )
                if status == "complete" and any(event.get("status") == "error" for event in query_state.values()):
                    status, message = "partial", "Some queries failed; resume to retry them."
                bundle.finalize(args.command, records, config.to_json(), status, query_state, message)
                return 0 if status == "complete" else 2
            status = "partial" if any(event.get("status") == "error" for event in query_state.values()) else "complete"
            bundle.finalize(args.command, records, config.to_json() if config else {}, status, query_state)
            return 0 if status == "complete" else 2
    except (KeyboardInterrupt, asyncio.CancelledError):
        finalize_recovered(bundle, args.command, config, records, "partial", "Interrupted; rerun with --resume.")
        raise
    except LoginTimeoutError as exc:
        finalize_recovered(bundle, args.command, config, records, "partial", str(exc))
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        finalize_recovered(bundle, args.command, config, records, "failed", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        # Only close the newly created task tab when attaching to an existing browser.
        if page and not browser.started and page.ws and not runtime.keep_browser:
            try:
                async with CdpPage(ws_url, timeout=3) as cleanup:
                    await cleanup.call("Page.close")
            except Exception:
                pass
        await browser.close()
        bundle.release_lock()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            return diagnose(args)
        if args.command == "rank":
            return execute_rank(args)
        return asyncio.run(execute(args))
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
