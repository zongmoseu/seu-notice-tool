from __future__ import annotations

import calendar
import datetime as dt
import json
import os
import re
import shutil
import threading
import webbrowser
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template_string, request, url_for
from markupsafe import Markup, escape

import notice_tool as tool


ROOT = tool.repo_root()
app = Flask(__name__)
tool.load_env_file(ROOT)
LECTURE_BLOCKS_PATH = Path("data") / "lecture_blocks.json"


def read_draft(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    return tool.parse_frontmatter(text)


def normalize_lecture_body(body: str) -> str:
    body = body.replace("本周尚有", "本周有")
    body = body.replace("本周还剩", "本周有")
    return body.strip()


def render_body_html(body: str) -> Markup:
    escaped = str(escape(body))
    escaped = escaped.replace("【待核对】", '<span class="verify-mark">【待核对】</span>')
    return Markup(escaped.replace("\n", "<br>"))


def ensure_lecture_polished(path: Path, meta: dict[str, str], body: str, mode: str = "weekly") -> tuple[dict[str, str], str]:
    if meta.get("category") != "lecture" or meta.get("generated_by") == "ai":
        return meta, normalize_lecture_body(body)
    source_url = meta.get("source_url")
    if not source_url or not tool.ai_key_configured():
        return meta, body
    summary = tool.NoticeSummary(
        title=meta.get("title", path.stem),
        url=source_url,
        publish_date=meta.get("publish_date", ""),
        category="lecture",
    )
    detail = tool.parse_detail(summary)
    polished = tool.call_ai_rewriter(
        detail,
        tool.compose_lecture_draft(detail, mode=mode),
        lecture_mode=mode,
    ).strip()
    polished = normalize_lecture_body(polished)
    meta["generated_by"] = "ai"
    meta["lecture_mode"] = mode
    meta["polished_at"] = dt.datetime.now().isoformat(timespec="seconds")
    path.write_text(tool.render_frontmatter(meta) + polished + "\n", encoding="utf-8")
    return meta, polished


def month_label(date_text: str) -> str:
    try:
        date_value = dt.date.fromisoformat(date_text)
        return f"{date_value.year}年{date_value.month:02d}月"
    except ValueError:
        return "未注明月份"


def lecture_group_label(meta: dict[str, str]) -> str:
    title = meta.get("title", "")
    publish_date = meta.get("publish_date", "")
    date_value = parse_date(publish_date)
    prefix = month_label(publish_date)
    week_match = re.search(r"第[一二三四五六七八九十百]+周", title)
    if week_match:
        week_number = chinese_to_int(week_match.group(0).removeprefix("第").removesuffix("周"))
        return f"{prefix} · {week_match.group(0)}{week_range_from_academic_week(week_number)}"
    date_match = re.search(r"\d{1,2}月\d{1,2}日", title)
    if date_match:
        return f"{prefix} · {academic_week_label(date_value)}"
    return f"{prefix} · 研学讲座"


def parse_date(date_text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(date_text)
    except ValueError:
        return None


def week_range_suffix(date_value: dt.date | None) -> str:
    if not date_value:
        return ""
    start = date_value - dt.timedelta(days=date_value.weekday())
    end = start + dt.timedelta(days=6)
    return f"（{start.month:02d}.{start.day:02d}-{end.month:02d}.{end.day:02d}）"


def academic_week_label(date_value: dt.date | None) -> str:
    if not date_value:
        return "研学讲座"
    # SEU 2026 spring examples imply 第十三周 is 2026-05-25 to 2026-05-31.
    reference_start = dt.date(2026, 5, 25)
    reference_week = 13
    week_start = date_value - dt.timedelta(days=date_value.weekday())
    week_number = reference_week + ((week_start - reference_start).days // 7)
    if week_number > 0:
        return f"第{int_to_chinese(week_number)}周{week_range_suffix(date_value)}"
    return f"{date_value.month}月{date_value.day}日"


def week_range_from_academic_week(week_number: int | None) -> str:
    if not week_number:
        return ""
    reference_start = dt.date(2026, 5, 25)
    reference_week = 13
    start = reference_start + dt.timedelta(days=(week_number - reference_week) * 7)
    end = start + dt.timedelta(days=6)
    return f"（{start.month:02d}.{start.day:02d}-{end.month:02d}.{end.day:02d}）"


def chinese_to_int(text: str) -> int | None:
    values = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + values.get(text[1:], 0)
    if "十" in text:
        left, _, right = text.partition("十")
        return values.get(left, 0) * 10 + values.get(right, 0)
    return values.get(text)


def int_to_chinese(value: int) -> str:
    digits = "零一二三四五六七八九"
    if value <= 10:
        return "十" if value == 10 else digits[value]
    if value < 20:
        return "十" + digits[value - 10]
    tens, ones = divmod(value, 10)
    return digits[tens] + "十" + (digits[ones] if ones else "")


def load_lecture_store() -> dict[str, Any]:
    path = ROOT / LECTURE_BLOCKS_PATH
    if not path.exists():
        return {"week_state": {}, "blocks": []}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("week_state", {})
    data.setdefault("blocks", [])
    return data


def save_lecture_store(data: dict[str, Any]) -> None:
    path = ROOT / LECTURE_BLOCKS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def current_week_start() -> dt.date:
    today = dt.date.today()
    return today - dt.timedelta(days=today.weekday())


def current_week_end() -> dt.date:
    return current_week_start() + dt.timedelta(days=6)


def current_week_label() -> str:
    return academic_week_label(current_week_start())


def looks_like_midweek_notice(meta: dict[str, str]) -> bool:
    title = meta.get("title", "")
    has_date_title = bool(re.search(r"\d{1,2}月\d{1,2}日", title))
    has_week_title = bool(re.search(r"第[一二三四五六七八九十百]+周", title))
    return has_date_title and not has_week_title


def source_urls_in_lecture_blocks(store: dict[str, Any], statuses: set[str] | None = None) -> set[str]:
    urls: set[str] = set()
    for block in store.get("blocks", []):
        if statuses and block.get("status") not in statuses:
            continue
        urls.update(tool.normalize_url(url) for url in block.get("source_urls", []))
    return urls


def current_pending_lecture_urls() -> list[str]:
    urls: list[str] = []
    for item_category, path in tool.pending_files(ROOT):
        if item_category != "lecture" or not path.exists():
            continue
        meta, _ = read_draft(path)
        for url in tool.source_urls_from_meta(meta):
            normalized = tool.normalize_url(url)
            if normalized not in urls:
                urls.append(normalized)
    return urls


def parse_event_datetime(text: str) -> dt.datetime | None:
    match = re.search(
        r"(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日.*?"
        r"(?P<hour>\d{1,2})\s*[:：]\s*(?P<minute>\d{2})",
        text,
        re.S,
    )
    if not match:
        return None
    try:
        hour = int(match.group("hour"))
        if re.search(r"(下午|晚上|晚间|夜间)", text) and hour < 12:
            hour += 12
        if "中午" in text and hour < 11:
            hour += 12
        return dt.datetime(
            dt.date.today().year,
            int(match.group("month")),
            int(match.group("day")),
            hour,
            int(match.group("minute")),
        )
    except ValueError:
        return None


def parse_lecture_items_from_body(body: str, meta: dict[str, str], source_url: str) -> list[dict[str, Any]]:
    lines = [line.strip().rstrip("  ") for line in normalize_lecture_body(body).splitlines() if line.strip()]
    items: list[dict[str, Any]] = []
    skip_prefixes = ("[健雄科协]", "本周有", "近期新增", "新增", "详见", "【注】", "【QQ群】")
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(skip_prefixes) or line.startswith("【") or "详见" in line:
            index += 1
            continue
        field_lines: list[str] = []
        cursor = index + 1
        while cursor < len(lines) and lines[cursor].startswith("【"):
            if not lines[cursor].startswith("【注】"):
                field_lines.append(lines[cursor])
            cursor += 1
        if any(field.startswith("【时间】") for field in field_lines):
            text = "\n".join([line, *field_lines])
            items.append(
                {
                    "title": line,
                    "fields": field_lines,
                    "source_url": source_url,
                    "event_at": parse_event_datetime(text).isoformat() if parse_event_datetime(text) else "",
                }
            )
            index = cursor
            continue
        index += 1

    if not items:
        text = normalize_lecture_body(body)
        event_at = parse_event_datetime(text)
        items.append(
            {
                "title": meta.get("title", "研学讲座"),
                "fields": [],
                "source_url": source_url,
                "event_at": event_at.isoformat() if event_at else "",
                "raw_body": text,
            }
        )
    return items


def item_is_future_or_unknown(item: dict[str, Any], now: dt.datetime | None = None) -> bool:
    event_at = item.get("event_at")
    if not event_at:
        return True
    try:
        return dt.datetime.fromisoformat(event_at) >= (now or dt.datetime.now())
    except ValueError:
        return True


def item_in_current_week(item: dict[str, Any]) -> bool:
    event_at = item.get("event_at")
    if not event_at:
        return True
    try:
        event_date = dt.datetime.fromisoformat(event_at).date()
    except ValueError:
        return True
    return current_week_start() <= event_date <= current_week_end()


def compose_midweek_body(items: list[dict[str, Any]], source_urls: list[str]) -> str:
    count = len(items)
    lines = [
        "[健雄科协] 研学讲座",
        "",
        f"本周新增{count}场课外研学讲座，欢迎各位同学参与！",
        "",
    ]
    for item in items:
        if item.get("raw_body") and not item.get("fields"):
            lines.extend([item["raw_body"], ""])
            continue
        lines.append(item.get("title", "研学讲座"))
        lines.extend(item.get("fields", []))
        lines.append("")
    lines.append("详见：")
    lines.extend(source_urls)
    lines.extend(
        [
            "",
            "【注】关于SRTP讲座参与方式、讲座论文提交方式等事项，可参考讲座预告网页的 pdf 附件《本科生课外研学讲座活动指南(2026版)》。",
        ]
    )
    return normalize_lecture_body("\n".join(lines))


def expire_stale_midweek_blocks(store: dict[str, Any]) -> int:
    expired = 0
    now = dt.datetime.now()
    for block in store.get("blocks", []):
        if block.get("status") != "drafted":
            continue
        items = block.get("items", [])
        dated_items = [item for item in items if item.get("event_at")]
        if dated_items and all(not item_is_future_or_unknown(item, now) for item in dated_items):
            block["status"] = "expired"
            block["highlight"] = False
            block["expired_at"] = now.isoformat(timespec="seconds")
            expired += 1
    return expired


def upsert_midweek_blocks(allow_midweek: bool) -> dict[str, int]:
    store = load_lecture_store()
    expired = expire_stale_midweek_blocks(store)
    week_key = current_week_start().isoformat()
    state = store.setdefault("week_state", {}).setdefault(week_key, {})
    created = merged = items_added = 0

    if not allow_midweek:
        state["baseline_done"] = True
        state["baseline_at"] = dt.datetime.now().isoformat(timespec="seconds")
        state["baseline_urls"] = current_pending_lecture_urls()
        save_lecture_store(store)
        return {"blocks_created": 0, "blocks_merged": 0, "items_added": 0, "expired": expired}

    blocked_urls = source_urls_in_lecture_blocks(store, {"archived", "expired"})
    active_block = next(
        (
            block
            for block in store.get("blocks", [])
            if block.get("mode") == "midweek_new"
            and block.get("week_start") == week_key
            and block.get("status") == "drafted"
        ),
        None,
    )
    already_active = set(tool.normalize_url(url) for url in (active_block or {}).get("source_urls", []))
    baseline_urls = set(tool.normalize_url(url) for url in state.get("baseline_urls", []))
    records = seen_by_url()
    new_items: list[dict[str, Any]] = []
    new_urls: list[str] = []

    for item_category, path in tool.pending_files(ROOT):
        if item_category != "lecture" or not path.exists():
            continue
        meta, body = read_draft(path)
        urls = tool.source_urls_from_meta(meta)
        if not urls:
            continue
        url = tool.normalize_url(urls[0])
        record = records.get(url, {})
        is_changed = bool(record.get("highlight") or record.get("change_type") == "updated")
        if not is_changed or url in blocked_urls or url in already_active:
            continue
        if url in baseline_urls and record.get("change_type") != "updated":
            continue
        meta, body = ensure_lecture_polished(path, meta, body, mode="new")
        accepted_items: list[dict[str, Any]] = []
        for lecture_item in parse_lecture_items_from_body(body, meta, url):
            if item_in_current_week(lecture_item) and item_is_future_or_unknown(lecture_item):
                accepted_items.append(lecture_item)
        if not accepted_items:
            continue
        new_items.extend(accepted_items)
        if url not in new_urls:
            new_urls.append(url)

    if new_items:
        if active_block:
            active_block.setdefault("items", []).extend(new_items)
            active_block.setdefault("source_urls", []).extend(
                url for url in new_urls if url not in active_block.get("source_urls", [])
            )
            active_block["body"] = compose_midweek_body(active_block["items"], active_block["source_urls"])
            active_block["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
            active_block["highlight"] = True
            merged = 1
        else:
            block_id = f"lecture-new-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
            active_block = {
                "id": block_id,
                "mode": "midweek_new",
                "week_start": week_key,
                "week_label": current_week_label(),
                "status": "drafted",
                "highlight": True,
                "source_urls": new_urls,
                "items": new_items,
                "body": compose_midweek_body(new_items, new_urls),
                "created_at": dt.datetime.now().isoformat(timespec="seconds"),
                "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
            store.setdefault("blocks", []).append(active_block)
            created = 1
        items_added = len(new_items)

    state["baseline_done"] = True
    state["last_checked_at"] = dt.datetime.now().isoformat(timespec="seconds")
    save_lecture_store(store)
    return {"blocks_created": created, "blocks_merged": merged, "items_added": items_added, "expired": expired}


def seen_by_url() -> dict[str, dict[str, Any]]:
    data = tool.load_seen(ROOT)
    return {tool.normalize_url(key): value for key, value in data.get("items", {}).items()}


def pending_drafts(category: str) -> list[dict[str, Any]]:
    records = seen_by_url()
    blocked_lecture_urls = source_urls_in_lecture_blocks(load_lecture_store()) if category == "lecture" else set()
    rows = []
    for item_category, path in tool.pending_files(ROOT):
        if item_category != category or not path.exists():
            continue
        meta, body = read_draft(path)
        urls = tool.source_urls_from_meta(meta)
        if category == "lecture" and any(url in blocked_lecture_urls for url in urls):
            continue
        if category == "lecture":
            mode = "new" if looks_like_midweek_notice(meta) else "weekly"
            meta, body = ensure_lecture_polished(path, meta, body, mode=mode)
        elif category == "competition" and not body.lstrip().startswith("「健雄科协」竞赛通知"):
            body = "「健雄科协」竞赛通知\n" + body.strip()
        row_records = [records.get(url, {}) for url in urls]
        highlight = any(record.get("highlight") for record in row_records) or any(
            record.get("change_type") == "updated" for record in row_records
        )
        change_type = "updated" if any(record.get("change_type") == "updated" for record in row_records) else "new"
        rows.append(
            {
                "id": str(path.relative_to(ROOT)).replace("\\", "/"),
                "path": path,
                "meta": meta,
                "body": body.strip(),
                "body_html": render_body_html(body.strip()),
                "urls": urls,
                "status": "drafted",
                "highlight": highlight,
                "change_type": change_type,
                "publish_date": meta.get("publish_date", ""),
                "copy_label": "复制文案",
            }
        )
    return rows


def polish_pending_lectures() -> int:
    count = 0
    for item_category, path in tool.pending_files(ROOT):
        if item_category != "lecture" or not path.exists():
            continue
        meta, body = read_draft(path)
        if meta.get("generated_by") == "ai":
            continue
        try:
            mode = "new" if looks_like_midweek_notice(meta) else "weekly"
            ensure_lecture_polished(path, meta, body, mode=mode)
            count += 1
        except Exception:
            continue
    return count


def archived_records(category: str) -> list[dict[str, Any]]:
    rows = []
    for url, record in seen_by_url().items():
        if record.get("category") != category or record.get("status") != "archived":
            continue
        body = archived_body(record, url)
        rows.append(
            {
                "id": url,
                "meta": {
                    "title": record.get("title", ""),
                    "publish_date": record.get("publish_date", ""),
                },
                "body": body,
                "body_html": render_body_html(body),
                "urls": [url],
                "status": "archived",
                "highlight": False,
                "change_type": "",
                "publish_date": record.get("publish_date", ""),
                "sent_date": record.get("sent_date", ""),
                "copy_label": "复制文案",
            }
        )
    return rows


def archived_body(record: dict[str, Any], url: str) -> str:
    if record.get("category") == "competition":
        return f"{record.get('title', '')}\n详见{url}".strip()
    return f"{record.get('title', '')}\n详见：{url}".strip()


def competition_archived_blocks() -> list[dict[str, Any]]:
    records = archived_records("competition")
    records.sort(key=lambda item: (item.get("publish_date", ""), item["meta"].get("title", "")), reverse=True)
    blocks: list[dict[str, Any]] = []
    by_month: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_month.setdefault(month_label(record.get("publish_date", "")), []).append(record)
    for month, month_records in by_month.items():
        for index in range(0, len(month_records), 2):
            pair = month_records[index : index + 2]
            publish_dates = [item.get("publish_date", "") for item in pair if item.get("publish_date")]
            body = "「健雄科协」竞赛通知\n" + "\n".join(item["body"] for item in pair)
            sent_dates = [item.get("sent_date", "") for item in pair if item.get("sent_date")]
            blocks.append(
                {
                    "id": f"archived-competition-{month}-{index}",
                    "meta": {"title": f"竞赛通知（{len(pair)}条）", "publish_date": publish_dates[0] if publish_dates else ""},
                    "body": body,
                    "body_html": render_body_html(body),
                    "urls": [url for item in pair for url in item.get("urls", [])],
                    "status": "archived",
                    "highlight": False,
                    "change_type": "",
                    "publish_date": publish_dates[0] if publish_dates else "",
                    "sent_date": sent_dates[0] if sent_dates else "",
                    "copy_label": "复制文案",
                }
            )
    return blocks


def competition_groups() -> dict[str, list[dict[str, Any]]]:
    rows = pending_drafts("competition") + competition_archived_blocks()
    rows.sort(key=lambda item: (item.get("publish_date", ""), item["meta"].get("title", "")), reverse=True)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(month_label(row.get("publish_date", "")), []).append(row)
    return grouped


def seed_existing_midweek_blocks() -> None:
    store = load_lecture_store()
    week_key = current_week_start().isoformat()
    blocked_urls = source_urls_in_lecture_blocks(store)
    records = seen_by_url()
    items_to_add: list[dict[str, Any]] = []
    urls_to_add: list[str] = []

    for item_category, path in tool.pending_files(ROOT):
        if item_category != "lecture" or not path.exists():
            continue
        meta, body = read_draft(path)
        if not looks_like_midweek_notice(meta):
            continue
        urls = tool.source_urls_from_meta(meta)
        if not urls:
            continue
        url = tool.normalize_url(urls[0])
        if url in blocked_urls or records.get(url, {}).get("status") == "archived":
            continue
        meta, body = ensure_lecture_polished(path, meta, body, mode="new")
        parsed_items = [
            item
            for item in parse_lecture_items_from_body(body, meta, url)
            if item_in_current_week(item) and item_is_future_or_unknown(item)
        ]
        if not parsed_items:
            continue
        items_to_add.extend(parsed_items)
        urls_to_add.append(url)

    if not items_to_add:
        return

    active_block = next(
        (
            block
            for block in store.get("blocks", [])
            if block.get("mode") == "midweek_new"
            and block.get("week_start") == week_key
            and block.get("status") == "drafted"
        ),
        None,
    )
    if active_block:
        active_urls = set(tool.normalize_url(url) for url in active_block.get("source_urls", []))
        active_block.setdefault("items", []).extend(items_to_add)
        active_block.setdefault("source_urls", []).extend(url for url in urls_to_add if url not in active_urls)
        active_block["body"] = compose_midweek_body(active_block["items"], active_block["source_urls"])
        active_block["highlight"] = True
        active_block["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    else:
        block = {
            "id": f"lecture-new-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}",
            "mode": "midweek_new",
            "week_start": week_key,
            "week_label": current_week_label(),
            "status": "drafted",
            "highlight": True,
            "source_urls": urls_to_add,
            "items": items_to_add,
            "body": compose_midweek_body(items_to_add, urls_to_add),
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        store.setdefault("blocks", []).append(block)
    state = store.setdefault("week_state", {}).setdefault(week_key, {})
    state.setdefault("baseline_done", True)
    state.setdefault("baseline_urls", current_pending_lecture_urls())
    save_lecture_store(store)


def lecture_block_rows() -> list[dict[str, Any]]:
    seed_existing_midweek_blocks()
    store = load_lecture_store()
    rows: list[dict[str, Any]] = []
    for block in store.get("blocks", []):
        if block.get("mode") != "midweek_new":
            continue
        body = normalize_lecture_body(block.get("body", ""))
        rows.append(
            {
                "id": f"block:{block.get('id')}",
                "meta": {
                    "title": "周中新增研学讲座",
                    "publish_date": block.get("week_start", ""),
                },
                "body": body,
                "body_html": render_body_html(body),
                "urls": block.get("source_urls", []),
                "status": block.get("status", "drafted"),
                "highlight": bool(block.get("highlight")) and block.get("status") == "drafted",
                "change_type": "new",
                "publish_date": block.get("week_start", ""),
                "sent_date": block.get("sent_date", ""),
                "copy_label": "复制新增文案",
                "group_label": f"周中新增 · {block.get('week_label') or academic_week_label(parse_date(block.get('week_start', '')))}",
            }
        )
    rows.sort(key=lambda item: item.get("publish_date", ""), reverse=True)
    return rows


def lecture_groups() -> dict[str, list[dict[str, Any]]]:
    rows = lecture_block_rows() + pending_drafts("lecture") + archived_records("lecture")
    rows.sort(key=lambda item: (item.get("publish_date", ""), item["meta"].get("title", "")), reverse=True)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row.get("group_label") or lecture_group_label(row["meta"]), []).append(row)
    return grouped


def render_page(kind: str, groups: dict[str, list[dict[str, Any]]], message: str = "") -> str:
    title = "竞赛通知" if kind == "competition" else "研学讲座通知"
    return render_template_string(
        TEMPLATE,
        kind=kind,
        title=title,
        groups=groups,
        message=message,
        today=dt.date.today().isoformat(),
    )


def append_archive_body(category: str, body: str, sent_date: str) -> Path:
    archive_path = ROOT / tool.SENT_DIR / f"{sent_date}.md"
    existing = archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
    section = tool.section_title(category)
    chunks: list[str] = []
    if not existing.strip():
        chunks.append(f"# 「健雄科协」{sent_date}通知")
    if f"## {section}" not in existing:
        chunks.append(f"## {section}")
    chunks.append(body.strip())
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n\n"):
            handle.write("\n\n")
        handle.write("\n\n".join(chunk for chunk in chunks if chunk).strip())
        handle.write("\n")
    return archive_path


def move_source_draft_to_archive(record: dict[str, Any], sent_date: str) -> None:
    draft_path = record.get("draft_path")
    if not draft_path:
        return
    path = ROOT / draft_path
    try:
        resolved = path.resolve()
        if ROOT.resolve() not in resolved.parents or not path.exists() or not path.is_file():
            return
    except OSError:
        return
    backup_dir = ROOT / tool.ARCHIVED_DRAFT_DIR / sent_date / tool.section_title("lecture")
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / path.name
    if target.exists():
        target = backup_dir / f"{path.stem}_{dt.datetime.now().strftime('%H%M%S')}.md"
    shutil.move(str(path), str(target))


def archive_lecture_block(block_id: str, sent_date: str) -> dict[str, Any]:
    store = load_lecture_store()
    raw_id = block_id.removeprefix("block:")
    block = next((item for item in store.get("blocks", []) if item.get("id") == raw_id), None)
    if not block:
        raise ValueError("lecture block not found")
    if block.get("status") == "expired":
        raise ValueError("过期块不能标记为已发")

    archive_path = append_archive_body("lecture", normalize_lecture_body(block.get("body", "")), sent_date)
    block["status"] = "archived"
    block["sent_date"] = sent_date
    block["archive_path"] = str(archive_path.relative_to(ROOT))
    block["highlight"] = False
    block["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
    save_lecture_store(store)

    seen = tool.load_seen(ROOT)
    items = seen.setdefault("items", {})
    for url in block.get("source_urls", []):
        normalized = tool.normalize_url(url)
        record = items.setdefault(normalized, {})
        move_source_draft_to_archive(record, sent_date)
        record.update(
            {
                "category": "lecture",
                "status": "archived",
                "sent_date": sent_date,
                "archive_path": str(archive_path.relative_to(ROOT)),
                "draft_path": "",
                "highlight": False,
                "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )
    tool.save_seen(ROOT, seen)
    return {"archived": len(block.get("source_urls", [])), "archive_path": str(archive_path.relative_to(ROOT))}


@app.get("/")
def index():
    return redirect(url_for("competition_page"))


@app.get("/competition")
def competition_page():
    return render_page("competition", competition_groups(), request.args.get("message", ""))


@app.get("/lectures")
def lectures_page():
    return render_page("lecture", lecture_groups(), request.args.get("message", ""))


@app.post("/api/update/<kind>")
def update_kind(kind: str):
    if kind not in ("competition", "lecture"):
        return jsonify({"ok": False, "error": "unknown kind"}), 400
    since_date = current_week_start() if kind == "lecture" else None
    baseline_done = False
    if kind == "lecture":
        store = load_lecture_store()
        baseline_done = bool(
            store.get("week_state", {}).get(current_week_start().isoformat(), {}).get("baseline_done")
        )
    result = tool.update_notices(
        ROOT,
        category=kind,
        days=tool.DEFAULT_RECENT_DAYS,
        max_pages=5,
        use_ai=False,
        since_date=since_date,
    )
    polished = polish_pending_lectures() if kind == "lecture" else 0
    changed = result["created"] + result["updated"]
    block_result = (
        upsert_midweek_blocks(allow_midweek=baseline_done and changed > 0)
        if kind == "lecture"
        else {"blocks_created": 0, "blocks_merged": 0, "items_added": 0, "expired": 0}
    )
    message = (
        f"新增 {result['created']} 条，更新 {result['updated']} 条，无变化 {result['skipped']} 条"
        if changed
        else "暂无更新"
    )
    if polished:
        message += f"，已排版 {polished} 条"
    if kind == "lecture" and block_result["items_added"]:
        message += f"，周中新增块加入 {block_result['items_added']} 场"
    if kind == "lecture" and block_result["expired"]:
        message += f"，已标记过期 {block_result['expired']} 块"
    return jsonify({"ok": True, "message": message, "polished": polished, **block_result, **result})


@app.post("/api/archive")
def archive():
    payload = request.get_json(force=True)
    draft_id = payload.get("id", "")
    sent_date = payload.get("date") or dt.date.today().isoformat()
    if not draft_id:
        return jsonify({"ok": False, "error": "missing id"}), 400
    try:
        if str(draft_id).startswith("block:"):
            result = archive_lecture_block(str(draft_id), sent_date)
        else:
            result = tool.archive_draft_path(ROOT, Path(draft_id), sent_date)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, **result})


TEMPLATE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }} · 健雄科协</title>
  <style>
    :root { --bg:#f6f7f9; --panel:#fff; --text:#17202a; --muted:#6c7680; --line:#d8dde3; --red:#d93025; --green:#1a7f37; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    header { height: 56px; display:flex; align-items:center; justify-content:space-between; padding:0 22px; border-bottom:1px solid var(--line); background:#fff; position:sticky; top:0; z-index:5; }
    nav a { color: var(--muted); text-decoration:none; padding:8px 12px; border-radius:6px; margin-right:4px; }
    nav a.active { color:#fff; background:#263238; }
    .layout { display:grid; grid-template-columns: 240px 1fr; min-height: calc(100vh - 56px); }
    aside { border-right:1px solid var(--line); background:#fff; padding:14px; position:sticky; top:56px; height:calc(100vh - 56px); overflow:auto; }
    main { padding:18px 24px 40px; }
    .toolbar { display:flex; gap:10px; align-items:center; margin-bottom:16px; }
    button { border:1px solid var(--line); background:#fff; border-radius:6px; padding:8px 12px; cursor:pointer; }
    button.primary { background:#263238; color:#fff; border-color:#263238; }
    button:disabled { opacity:.55; cursor:not-allowed; }
    .message { min-height: 22px; color: var(--green); font-weight:600; }
    .toc a { display:flex; align-items:center; justify-content:space-between; color:var(--text); text-decoration:none; padding:9px 10px; border-radius:6px; margin-bottom:4px; }
    .toc a:hover { background:#eef1f4; }
    .count { color:var(--muted); font-size:12px; }
    section { margin-bottom:22px; }
    h2 { font-size:18px; margin: 6px 0 12px; }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; margin-bottom:12px; display:grid; grid-template-columns: 1fr 170px; gap:16px; position:relative; }
    .card.highlight { border-color:#f1a9a0; box-shadow:0 0 0 2px rgba(217,48,37,.08); }
    .card.archived, .card.expired { background:#eef0f2; color:#7b858d; opacity:.72; }
    .dot { width:10px; height:10px; background:var(--red); border-radius:50%; position:absolute; top:12px; left:12px; }
    .content { white-space:pre-wrap; line-height:1.72; padding-left:18px; }
    .verify-mark { color: var(--red); font-weight: 700; }
    .card.archived .content { color:#727b83; }
    .card.expired .content { color:#727b83; text-decoration-color:#aab2b9; }
    .meta { border-left:1px solid var(--line); padding-left:14px; color:var(--muted); font-size:13px; }
    .meta strong { display:block; color:var(--text); margin-bottom:8px; }
    .card.archived .meta strong { color:#68727a; }
    .actions { display:flex; flex-direction:column; gap:8px; margin-top:12px; }
    .empty { color:var(--muted); background:#fff; border:1px dashed var(--line); border-radius:8px; padding:28px; text-align:center; }
  </style>
</head>
<body>
  <header>
    <nav>
      <a href="/competition" class="{{ 'active' if kind == 'competition' else '' }}">竞赛通知</a>
      <a href="/lectures" class="{{ 'active' if kind == 'lecture' else '' }}">研学讲座通知</a>
    </nav>
    <div>{{ today }}</div>
  </header>
  <div class="layout">
    <aside>
      <div class="toc">
        {% for group, rows in groups.items() %}
        <a href="#g{{ loop.index }}"><span>{{ group }}</span><span class="count">{{ rows|length }}</span></a>
        {% endfor %}
      </div>
    </aside>
    <main>
      <div class="toolbar">
        <button class="primary" onclick="updateKind('{{ kind }}')">更新{{ title }}</button>
        <span id="message" class="message">{{ message }}</span>
      </div>
      {% if not groups %}
      <div class="empty">暂无本地通知。点击更新试试。</div>
      {% endif %}
      {% for group, rows in groups.items() %}
      <section id="g{{ loop.index }}">
        <h2>{{ group }}</h2>
        {% for row in rows %}
        <article class="card {{ 'archived' if row.status == 'archived' else '' }} {{ 'expired' if row.status == 'expired' else '' }} {{ 'highlight' if row.highlight else '' }}">
          {% if row.highlight and row.status not in ['archived', 'expired'] %}<span class="dot" title="新增或更新"></span>{% endif %}
          <div class="content" id="body-{{ loop.index }}-{{ row.id|replace('/', '-')|replace('.', '-') }}">{{ row.body_html }}</div>
          <div class="meta">
            <strong>{{ '已发' if row.status == 'archived' else ('已过期' if row.status == 'expired' else ('更新' if row.change_type == 'updated' else '待发')) }}</strong>
            <div>发布日期：{{ row.publish_date or '未注明' }}</div>
            {% if row.sent_date %}<div>发送日期：{{ row.sent_date }}</div>{% endif %}
            <div>链接数：{{ row.urls|length }}</div>
            <div class="actions">
              <button data-body="{{ row.body|tojson|forceescape }}" onclick="copyText(JSON.parse(this.dataset.body))">{{ row.copy_label or '复制文案' }}</button>
              {% if row.status not in ['archived', 'expired'] %}
              <button data-id="{{ row.id|tojson|forceescape }}" onclick="archiveDraft(JSON.parse(this.dataset.id))">标记已发并归档</button>
              {% elif row.status == 'archived' %}
              <button disabled>已归档</button>
              {% elif row.status == 'expired' %}
              <button disabled>已过期</button>
              {% endif %}
            </div>
          </div>
        </article>
        {% endfor %}
      </section>
      {% endfor %}
    </main>
  </div>
  <script>
    async function updateKind(kind) {
      const message = document.getElementById('message');
      message.textContent = '正在更新...';
      const response = await fetch('/api/update/' + kind, {method: 'POST'});
      const data = await response.json();
      if (!data.ok) { message.textContent = data.error || '更新失败'; return; }
      message.textContent = data.message;
      setTimeout(() => location.reload(), 700);
    }
    async function archiveDraft(id) {
      const date = prompt('发送日期', new Date().toISOString().slice(0,10));
      if (!date) return;
      const response = await fetch('/api/archive', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id, date})
      });
      const data = await response.json();
      if (!data.ok) { alert(data.error || '归档失败'); return; }
      location.reload();
    }
    async function copyText(text) {
      try {
        await navigator.clipboard.writeText(text);
      } catch (error) {
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.left = '-9999px';
        document.body.appendChild(textarea);
        textarea.focus();
        textarea.select();
        document.execCommand('copy');
        textarea.remove();
      }
      const message = document.getElementById('message');
      message.textContent = '已复制';
      setTimeout(() => message.textContent = '', 1200);
    }
  </script>
</body>
</html>
"""


def main() -> None:
    url = "http://127.0.0.1:8765"
    if os.environ.get("NOTICE_WEB_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"通知管理网页已启动：{url}")
    app.run(host="127.0.0.1", port=8765, debug=False)


if __name__ == "__main__":
    main()
