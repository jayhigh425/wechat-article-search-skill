# WeChat Article Search Skill

[简体中文](README.md) · [Skill entry point](SKILL.md) · [Issues](https://github.com/jayhigh425/wechat-article-search-skill/issues)

![Tests](https://github.com/jayhigh425/wechat-article-search-skill/actions/workflows/tests.yml/badge.svg)
[![MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

**Find WeChat official-account articles, read visible content, and keep traceable source links.**

A Codex / Agent Skill and standalone Python CLI for WeChat article research through WeRead. It uses a local Microsoft Edge session to search visible results, capture direct `mp.weixin.qq.com` links, and extract normally readable article content.

## Features

- Multiple search queries with date, account, include/exclude keyword filters.
- Original links, metadata, visible text, image URLs and in-article links.
- All observed candidates retained before filtering and the global target.
- Checkpoint/resume, failed-query retry, imported text reuse and preserved fetch limits.
- Offline filtering/ranking with `rank`; relevance or newest-first ordering.
- Versioned JSON, UTF-8 CSV, article Markdown and per-query diagnostics.
- Explicit deleted, client-only, blocked, image-only and risk-control states.

## Requirements and quick start

Browser collection is tested on **Windows 10/11 with Microsoft Edge**. Install [Git](https://git-scm.com/downloads) and [uv](https://docs.astral.sh/uv/getting-started/installation/). Python 3.11+ and the pinned dependency are declared in each executable script.

```powershell
git clone https://github.com/jayhigh425/wechat-article-search-skill.git
cd wechat-article-search-skill
uv run .\scripts\wechat_official.py doctor

uv run .\scripts\wechat_official.py collect `
  --query-file .\examples\search-config.json `
  --fulltext-limit 5 --run-dir .\outputs\research

uv run .\scripts\wechat_official.py collect `
  --run-dir .\outputs\research --resume

uv run .\scripts\wechat_official.py rank `
  --input .\outputs\research --sort newest --target 10 `
  --run-dir .\outputs\reranked
```

Search requires WeRead login. The first unauthenticated run presents/saves a QR code and waits for the user to scan it. Direct article fetching does not first require WeRead login. Session state stays in a dedicated local browser profile.

For Codex, clone directly into its skill directory:

```powershell
git clone https://github.com/jayhigh425/wechat-article-search-skill.git `
  "$env:USERPROFILE\.codex\skills\wechat-weread-official"
```

Do not overwrite an existing skill directory containing local changes. Invoke `$wechat-weread-official` in a session that has discovered the installed skill. Other `SKILL.md` clients may adapt the entry point; they have not all been tested.

## Commands

| Command | Purpose |
|---|---|
| `search` | Search metadata and capture direct article links |
| `collect` | Search and extract visible content |
| `fetch` | Enrich saved results or supplied URLs |
| `rank` | Re-filter saved candidates offline |
| `doctor` | Check runtime and browser availability without opening a website |

See [SKILL.md](SKILL.md), [output schema](references/output-schema.md) and [troubleshooting](references/runtime-and-troubleshooting.md). [Sample outputs](examples/sample-output/summary.md) use fictional data and are explicitly labeled.

## Validation and limits

Local validation passed 34 unit/workflow tests and an Edge fixture suite covering article states, dynamic content/links, hidden text and URL fragments. A small live check returned six candidates across two queries, selected three records and extracted one article. These results demonstrate a working path, not exhaustive coverage or performance guarantees.

The visible WeRead index does not cover every account or historical article. Keyword filters operate on metadata rather than unread bodies. Images are inventoried without automatic OCR. The tool preserves access restrictions and stops subsequent requests when risk control appears. macOS/Linux browser collection has not been validated.

## Contribute

Useful contributions include reproducible selector changes, date-parsing cases and recovery bugs. Read [CONTRIBUTING.md](CONTRIBUTING.md) and open an [issue](https://github.com/jayhigh425/wechat-article-search-skill/issues/new/choose). If the project helps you, consider starring it or sharing your research workflow.

[MIT License](LICENSE). Collected articles remain the property of their respective authors.
