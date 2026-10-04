# Selectors and statuses

## Search

Cards: `.search_list_item`; title `.article__title-text`; snippet `.article__desc`; account `.source__title`; date `.source__text.date`. A card may provide a direct `a[href*="mp.weixin.qq.com"]`. Otherwise the script temporarily intercepts `window.open`, polls a bounded time, retries missing links individually, and restores the original function. Nested scrolling containers are supported. Virtualized/replaced cards are not guaranteed to be exhaustively retained: do not infer full coverage.

The script observes the existing page's `wx_search_broker_proxy` response for authentication/errors; it does not issue a separate private API request. The temporary new-document hook is removed after search. Login-expired `errCode=-2010` triggers at most one relogin attempt. Missing cards, upstream errors and selector drift are `error`; `no_results` requires an explicit visible empty state. Search risk control stops remaining queries and fulltext requests.

Query events include `visible`, accepted `candidates`, `rejected` reason counts, `stop_reason`, capacity settings, errors and failure diagnostics. Resume skips only `ok`/`no_results` query states.

Search record statuses: `ok`, `no_url`, `invalid_url`, `not_applicable` (direct input). Query statuses: `ok`, `no_results`, `error`, `risk_control`.

## Articles

Body: `#js_content`, `.rich_media_content`, `article`; title: `#activity-name`, `.rich_media_title`, `h1`; account: `#js_name`, `.profile_nickname`, `.rich_media_meta_nickname`. Dates prefer `#publish_time`/`#js_time`, then a date-like `.rich_media_meta_text` rather than the first author label.

Wait for readable body/images or an error state, not merely an empty container. Extract rendered text, visible image source URLs, and links; hidden text is excluded. Error wording inside readable content does not classify the article as blocked. Explicit visible gate elements have precedence; without readable content, inspect visible page text.

| Fulltext status | Meaning | Resume behavior |
|---|---|---|
| `not_requested` | Outside requested fulltext selection | Fetch when selected |
| `ok` | Readable visible text | Reuse |
| `image_only` | Images but no readable text; no OCR performed | Reuse with image links |
| `deleted` | Deleted/expired message | Reuse |
| `client_only` | WeChat client required | Reuse |
| `blocked` | Parameters/access rules prevent display | Reuse |
| `invalid_url` | Not an HTTP(S) article URL on mp.weixin.qq.com | Reuse; correct input in a new run |
| `risk_control` | Frequent/abnormal access gate | Stop task; retry after access recovers |
| `empty` | No readable body or images | Retry |
| `timeout`, `eval_error`, `navigation_error` | Technical failure | Retry |

The retry budget is `--retries + 1` attempts per invocation, with backoff. `attempts.fulltext` accumulates across resumes. A requested transient failure makes the task `partial`; deliberate fulltext limits do not. Terminal unavailable articles remain visibly marked even when the task is `complete`.
