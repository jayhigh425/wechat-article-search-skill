# Runtime and troubleshooting

## Environment

Use `uv run scripts/wechat_official.py`; PEP 723 pins `websockets==16.1`. Run `doctor` before investigating browser startup problems. It checks the executable, runtime and optional CDP port without opening a website. It does not verify authentication.

Edge is the default; Chrome is an explicit fallback. Login state persists under `%LOCALAPPDATA%\Codex\wechat-weread-profile`, overridden by `--profile`. Never copy that profile into deliverables or diagnostics.

By default launch a dedicated browser with a dynamic port. `--cdp-port` attaches when the endpoint is available, otherwise launches on that port. Each command opens its own task tab, preserving existing tabs. Only close a browser launched by this command; when attached, close only its new task tab. `--keep-browser` retains it for inspection. `--headless` is for already authenticated profiles or fixtures; do not use it when interactive login is needed.

## Login and progress

Search/collect need WeRead authentication. Direct-URL fetch does not navigate to WeRead. An unauthenticated search saves `login-qr.png` and waits up to `--login-timeout`; present it or the browser QR to the user. QR and failure screenshots can contain session-sensitive information and stay within the task directory.

The script reports each query/article on stderr. Delays between queries/articles are bounded by `--delay-min/--delay-max`; do not reduce them to evade risk control. URL capture prefers existing links and waits for delayed click handlers, with a smaller effective batch when the CDP timeout is short.

## Resume

Use the original command with `--run-dir ORIGINAL --resume`. Search settings must match the saved settings. Use offline `rank` for new filters or a new search directory for new queries/limits. An original `--fulltext-limit` or `--limit` is retained if omitted on resume; explicitly raise it to enrich more of the saved selection. The limit means the first N records, including completed ones, not N additional requests.

Successful/explicitly empty queries are skipped. Failed and risk-stopped queries are retryable after the cause clears. Completed article states are reused; transient failures retry. Interrupted candidate records are replayed and deduplicated. Relative dates are anchored to the original run date. Imported readable fulltext is reused and written into the new bundle.

The checkpoint repairs an unterminated malformed final line before append, including a partial UTF-8 character. Malformed completed lines or interior corruption raise an error instead of being discarded. Preserve the file for inspection; do not edit it while active. A per-directory OS lock prevents concurrent writers and is automatically released on process exit.

Legacy v1 runs did not save a fulltext limit. When resuming one, explicitly supply its original `--limit`/`--fulltext-limit`; the script cannot recover a limit that was never recorded.

## Diagnose from evidence

- `error` query: inspect `diagnostics/query-*.png` and the error in `summary.md`. Missing cards without an explicit empty state do not prove zero results.
- `date_unparsed`: inspect `date_raw`; optionally use `rank --unknown-date keep` without new network calls.
- `no_url`: inspect current card DOM and direct anchors; delayed capture is retried once. Do not invent a URL from title or data-id.
- `timeout` article: inspect screenshot and error; retry with a larger `--timeout` if the page is legitimately slow. URLs with `#rd` are supported.
- Locked profile/CDP unavailable: inspect `doctor`, close only the dedicated instance you own, or choose a separate profile. Never kill the user's unrelated browser processes.
- Risk control: the command stops subsequent requests, writes saved results, returns 2. Resume only after normal access becomes available.

Use `results.json` for downstream work; `checkpoint.jsonl` is a recovery log, not a bare article collection.
