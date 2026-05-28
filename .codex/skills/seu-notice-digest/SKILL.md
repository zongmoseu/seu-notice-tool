---
name: seu-notice-digest
description: Organize Southeast University JWC practice-teaching notices into reusable Markdown drafts and a local web UI for 健雄科协. Use when scraping https://jwc.seu.edu.cn/sjjx/list.htm, generating competition or SRTP/研学讲座 notices, deduplicating drafts, archiving sent notices, or maintaining this project.
---

# SEU Notice Digest

Use this skill for the local project that turns 东南大学教务处“实践教学” notices into 健雄科协 sendable drafts.

## Main Workflow

Run from the project root.

Start the local web UI:

```powershell
python scripts/notice_web.py
```

The launcher `启动通知整理工具.bat` opens the same UI at `http://127.0.0.1:8765`.

The web UI has two pages:

- `竞赛通知`: update competition notices, show them by month, and group every 1-2 items into one sendable block.
- `研学讲座通知`: update lecture notices, show official weekly previews by academic week, and manage separate midweek-new blocks.

## Directory Contract

- `待发/竞赛通知/`: pending competition Markdown drafts.
- `待发/研学讲座通知/`: pending lecture Markdown drafts.
- `已发/YYYY-MM-DD.md`: daily archive.
- `已发/_原始草稿/YYYY-MM-DD/`: source drafts moved after archive.
- `data/seen.json`: source URL state.
- `data/lecture_blocks.json`: local state for weekly lecture baseline and midweek-new blocks.

## Update Rules

- List source: `https://jwc.seu.edu.cn/sjjx/list.htm`, plus `list2.htm`, `list3.htm`, etc.
- List selector: `#wp_news_w8 table.main tr`.
- Detail selectors: `.Article_Title`, `.Article_PublishDate`, `.wp_articlecontent`.
- Normalize `page.htm` and `page.psp` as the same article URL.
- Deduplicate by normalized URL plus scanned title and publish date. If title or publish date changes, treat the notice as updated and highlight it again.

Competition notices:

- Only include upcoming/start/registration notices.
- Exclude results, publicity, awards, lists, finalist announcements, completion, acceptance, recommendation announcements, and similar non-upcoming notices.
- Sendable format is intentionally short:

```markdown
「健雄科协」竞赛通知
关于举办某某竞赛的通知
详见https://jwc.seu.edu.cn/...
关于 2026 某某竞赛报名的通知
详见https://jwc.seu.edu.cn/...
```

Lecture notices:

- Lecture updates scan from the current system week Monday, not a rolling 7-day window.
- Every week’s first lecture update is the baseline weekly preview and should use “本周有 X 场课外研学讲座”.
- Later updates in the same week only produce a separate “周中新增” block when new/updated notices appear.
- If a midweek-new block is not sent, later new lectures merge into that same block.
- If an unsent midweek-new block’s dated lectures have all passed, mark the block `expired`; it becomes grey in the UI and is not archived.
- Reservation status must be explicit when possible: `需提前预约`, `无需提前预约`, or `未注明`. Do not infer beyond the source/PDF.
- `【待核对】` is allowed for uncertain facts and is highlighted red in the UI.

## AI Drafting

AI drafting is optional. The project reads `.env` automatically.

Supported variables:

```env
API_URL=https://your-api-provider.example
API_KEY=replace-with-your-key
MODEL=gpt-5.5
AI_SOURCE_MAX_CHARS=6000
```

OpenAI-style aliases also work: `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`.

AI output is still a draft. Always verify lecture title, time, location, reservation link, QQ group, and competition registration details against the official URL/PDF.

## CLI Fallback

```powershell
python scripts/notice_tool.py update --category competition
python scripts/notice_tool.py update --category lecture
python scripts/notice_tool.py list
python scripts/notice_tool.py archive --date 2026-05-28
python scripts/notice_tool.py polish --category lecture --limit 2
```
