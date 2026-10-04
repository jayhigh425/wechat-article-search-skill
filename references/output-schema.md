# Output schema v2

`results.json` is an object:

```json
{
  "schema_version": 2,
  "generated_at": "ISO-8601 timestamp",
  "command": "search | fetch | collect | rank",
  "status": "complete | search_complete | risk_control | partial | failed",
  "config": {},
  "counts": {},
  "query_state": {},
  "message": "",
  "records": []
}
```

v2 retains the v1 record fields; consumers must allow the new `image_only` status and added properties. `fetch` accepts v1/v2 objects, legacy arrays, one article object, JSONL records, and URL text. UTF-8 BOM is accepted. External unsafe record IDs are regenerated before writing files.

## Records

- Identity: `id` (stable hash of `__biz+mid+idx`, otherwise `sn` or normalized short URL/metadata), `url` (original access URL), `canonical_url` (deduplication identity; never replace the original navigation URL with it). Short-URL tracking/fragment differences are ignored.
- Metadata: `title`, `account`, `date_raw`, `published_date`, `snippet`, `data_id`, `collected_on`.
- Retrieval: `matched_queries`, `source_rank`, `title_term_hits`, `text_term_hits`, `ranking_queries`. Query coverage records actual retrieval; ranking queries are local scoring terms and do not imply another website search.
- Filters: `filter_passed`, `filter_reason` capture the original search's assessment in the candidate pool; offline rank recalculates selection under its own `config`.
- State: `search_status`, `fulltext_status`, `attempts`, `error`. `attempts.fulltext` is cumulative; `attempt_count` is for the latest invocation.
- Opened article: `article_title`, `article_account`, `article_date`, `article_url`, `fulltext_captured_at`. A readable article date updates selected `published_date`; search `date_raw` remains for comparison.
- Content: `fulltext`, `char_count`, `images`, `image_count`, `links`, `content_sha256` (visible plain-text hash, not proof of original authorship).
- Artifacts: `content_path`, `diagnostic_path`, relative to the containing bundle. Image-only article Markdown contains image links; images are not downloaded automatically.

## Files

- `run.json`: latest state, original start time, settings/counts and `options.fulltext_limit`; atomically written.
- `checkpoint.jsonl`: append-only candidate, input, query completion and fulltext events; flushed to disk. Not a downstream record array.
- `candidates.json`: deduplicated observed pool before filters/target, with config/query state. Available for search/collect/rank. Includes rejected candidates for later offline re-filtering.
- `results.json`: canonical current selected/enriched records; atomically written.
- `results.csv`: compact metadata/status index, UTF-8 BOM for Excel, quoting and spreadsheet-formula protection.
- `summary.md`: selected result table, status counts, per-query coverage/rejections/stops/errors.
- `articles/<id>.md`: readable article text or image-only inventory, with metadata/image/link references.
- `diagnostics/`: screenshots for failed article attempts and failed/risk-stopped queries.
- `.run.lock`: OS-managed writer lock; presence does not mean a process is active.
- `login-qr.png`: only when login is needed; keep local.

`complete` means the configured processing scope finished, not every record yielded text, the target was reached, or the index was exhaustive. Check `counts` and `query_state`.
