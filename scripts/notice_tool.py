from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import io
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as html_to_markdown


BASE_URL = "https://jwc.seu.edu.cn"
LIST_URL = f"{BASE_URL}/sjjx/list.htm"
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_RECENT_DAYS = 7
DEFAULT_AI_SOURCE_MAX_CHARS = 6000
logging.getLogger("pypdf").setLevel(logging.ERROR)
_LOADED_ENV_FILES: set[Path] = set()

COMPETITION_DIR = Path("待发") / "竞赛通知"
LECTURE_DIR = Path("待发") / "研学讲座通知"
SENT_DIR = Path("已发")
SEEN_PATH = Path("data") / "seen.json"
CRAWL_STATE_PATH = Path("data") / "crawl_state.json"
ARCHIVED_DRAFT_DIR = SENT_DIR / "_原始草稿"

COMPETITION_KEYWORDS = (
    "竞赛",
    "大赛",
    "比赛",
    "参赛",
    "报名",
    "数学建模",
    "程序设计",
    "挑战杯",
    "创新创业",
    "节能减排",
)
COMPETITION_EXCLUDE_KEYWORDS = (
    "结果",
    "公示",
    "获奖",
    "名单",
    "入围",
    "十佳",
    "结题",
    "验收",
    "拟推荐",
    "公布",
)
LECTURE_KEYWORDS = ("课外研学讲座", "研学讲座", "srtp讲座", "讲座预告")


@dataclass
class NoticeSummary:
    title: str
    url: str
    publish_date: str
    category: str


@dataclass
class NoticeDetail:
    title: str
    url: str
    publish_date: str
    category: str
    html_markdown: str
    pdf_text: str
    attachments: list[dict[str, str]]

    @property
    def source_text(self) -> str:
        chunks = [self.html_markdown.strip(), self.pdf_text.strip()]
        return "\n\n".join(chunk for chunk in chunks if chunk)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_root(root_arg: str | None) -> Path:
    return Path(root_arg).resolve() if root_arg else repo_root()


def load_env_file(root: Path) -> None:
    env_path = root / ".env"
    if env_path in _LOADED_ENV_FILES or not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    _LOADED_ENV_FILES.add(env_path)


def configured_api_key() -> str | None:
    return os.environ.get("OPENAI_API_KEY") or os.environ.get("AI_API_KEY") or os.environ.get("API_KEY")


def configured_api_endpoint() -> str:
    base_url = (
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("API_URL")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    return f"{base_url}/chat/completions"


def http_get(url: str) -> bytes:
    response = requests.get(
        url,
        timeout=30,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 notice-tool/1.0"
            )
        },
    )
    response.raise_for_status()
    return response.content


def read_html(url: str) -> BeautifulSoup:
    content = http_get(url)
    text = content.decode("utf-8", errors="replace")
    return BeautifulSoup(text, "lxml")


def normalize_url(url: str) -> str:
    absolute = urljoin(BASE_URL, url)
    parts = urlsplit(absolute)
    path = re.sub(r"/page\.psp$", "/page.htm", parts.path, flags=re.IGNORECASE)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def list_page_url(page: int) -> str:
    if page <= 1:
        return LIST_URL
    return f"{BASE_URL}/sjjx/list{page}.htm"


def classify_title(title: str) -> str | None:
    lowered = title.lower()
    if any(keyword.lower() in lowered for keyword in LECTURE_KEYWORDS):
        return "lecture"
    if any(keyword.lower() in lowered for keyword in COMPETITION_KEYWORDS):
        if any(keyword.lower() in lowered for keyword in COMPETITION_EXCLUDE_KEYWORDS):
            return None
        return "competition"
    return None


def is_within_recent_days(publish_date: str, days: int | None = DEFAULT_RECENT_DAYS) -> bool:
    if days is None or days < 0:
        return True
    try:
        notice_date = dt.date.fromisoformat(publish_date)
    except ValueError:
        return False
    cutoff = dt.date.today() - dt.timedelta(days=days)
    return notice_date >= cutoff


def is_on_or_after(publish_date: str, start_date: dt.date | None) -> bool:
    if start_date is None:
        return True
    try:
        notice_date = dt.date.fromisoformat(publish_date)
    except ValueError:
        return False
    return notice_date >= start_date


def parse_list_page_with_dates(page: int) -> tuple[list[NoticeSummary], list[str]]:
    soup = read_html(list_page_url(page))
    results: list[NoticeSummary] = []
    page_dates: list[str] = []
    for row in soup.select("#wp_news_w8 table.main tr"):
        link = None
        for candidate in row.select("a[title][href]"):
            href = candidate.get("href", "").strip()
            title = candidate.get("title", "").strip()
            if href and title:
                link = candidate
                break
        if not link:
            continue

        date_match = re.search(r"\d{4}-\d{2}-\d{2}", row.get_text(" ", strip=True))
        publish_date = date_match.group(0) if date_match else ""
        if publish_date:
            page_dates.append(publish_date)

        title = html.unescape(link.get("title", "").strip())
        category = classify_title(title)
        if not category:
            continue

        results.append(
            NoticeSummary(
                title=title,
                url=normalize_url(link["href"]),
                publish_date=publish_date,
                category=category,
            )
        )
    return results, page_dates


def parse_list_page(page: int) -> list[NoticeSummary]:
    summaries, _ = parse_list_page_with_dates(page)
    return summaries


def text_or_empty(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


def parse_attachment_title(raw: str | None) -> str:
    if not raw:
        return ""
    match = re.search(r"'title'\s*:\s*'([^']+)'", raw)
    return html.unescape(match.group(1)) if match else ""


def unique_attachments(items: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for item in items:
        url = normalize_url(item["url"])
        if url in seen:
            continue
        seen.add(url)
        result.append({"url": url, "title": item.get("title") or Path(urlsplit(url).path).name})
    return result


def extract_attachments(content_node) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    if not content_node:
        return items

    for node in content_node.select("[pdfsrc]"):
        pdf_url = node.get("pdfsrc", "").strip()
        if pdf_url:
            title = parse_attachment_title(node.get("sudyfile-attr"))
            items.append({"url": pdf_url, "title": title})

    for link in content_node.select("a[href]"):
        href = link.get("href", "").strip()
        if ".pdf" not in href.lower():
            continue
        title = (
            link.get("title")
            or parse_attachment_title(link.get("sudyfile-attr"))
            or link.get_text(" ", strip=True)
        )
        items.append({"url": href, "title": title})

    return unique_attachments(items)


def extract_pdf_text(pdf_url: str, max_chars: int = 18000) -> str:
    data = http_get(pdf_url)
    chunks: list[str] = []

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                chunks.append(text)
            if sum(len(chunk) for chunk in chunks) >= max_chars:
                break
    except Exception:
        chunks = []

    if not chunks:
        try:
            import fitz

            with fitz.open(stream=data, filetype="pdf") as doc:
                for page in doc:
                    text = page.get_text("text") or ""
                    if text.strip():
                        chunks.append(text)
                    if sum(len(chunk) for chunk in chunks) >= max_chars:
                        break
        except Exception as exc:
            return f"[PDF文本提取失败：{exc}]"

    text = "\n".join(chunks)
    return normalize_text(text)[:max_chars]


def parse_detail(summary: NoticeSummary) -> NoticeDetail:
    soup = read_html(summary.url)
    title = text_or_empty(soup.select_one(".Article_Title")) or summary.title
    publish_date = text_or_empty(soup.select_one(".Article_PublishDate")) or summary.publish_date
    content_node = soup.select_one(".wp_articlecontent .Article_Content") or soup.select_one(
        ".wp_articlecontent"
    )
    attachments = extract_attachments(content_node)

    content_html = str(content_node) if content_node else ""
    markdown = html_to_markdown(content_html, heading_style="ATX").strip()
    markdown = normalize_markdown(markdown)

    pdf_chunks = []
    for attachment in attachments:
        if attachment["url"].lower().endswith(".pdf"):
            pdf_chunks.append(f"### {attachment['title']}\n{extract_pdf_text(attachment['url'])}")

    return NoticeDetail(
        title=title,
        url=summary.url,
        publish_date=publish_date,
        category=summary.category,
        html_markdown=markdown,
        pdf_text="\n\n".join(pdf_chunks),
        attachments=attachments,
    )


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def normalize_markdown(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\[!\[\]\([^)]+\)\]\([^)]+\)", "", text)
    return normalize_text(text)


def extract_relevant_lines(text: str, keywords: Iterable[str], limit: int = 16) -> list[str]:
    lines = normalize_text(text).splitlines()
    result: list[str] = []
    for line in lines:
        if len(line) < 4:
            continue
        if any(keyword.lower() in line.lower() for keyword in keywords):
            result.append(line)
        if len(result) >= limit:
            break
    return result


def compact_ai_source_text(detail: NoticeDetail, lecture_mode: str | None = None) -> str:
    max_chars = int(os.environ.get("AI_SOURCE_MAX_CHARS", DEFAULT_AI_SOURCE_MAX_CHARS))
    text = normalize_text(detail.source_text)
    if detail.category != "lecture":
        return text[:max_chars]

    front_matter = "\n".join(text.splitlines()[:18])
    keywords = (
        "题目",
        "讲座",
        "时间",
        "地点",
        "预报名",
        "预约",
        "无需",
        "报名",
        "QQ群",
        "qq",
        "问卷",
        "http://",
        "https://",
        "【注】",
        "活动指南",
    )
    pieces: list[str] = [front_matter]
    for line in text.splitlines():
        parts = re.split(r"(?=（[一二三四五六七八九十]+）)|(?=【时间】)|(?=【地点】)|(?=【预报名】)|(?=【交流QQ群】)", line)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if any(keyword.lower() in part.lower() for keyword in keywords):
                pieces.append(part[:900])
            if sum(len(piece) for piece in pieces) >= max_chars:
                return "\n".join(pieces)[:max_chars]

    return (("\n".join(pieces)) if pieces else text)[:max_chars]


def attachment_block(attachments: list[dict[str, str]]) -> str:
    if not attachments:
        return ""
    lines = ["附件："]
    for item in attachments:
        lines.append(f"- {item['title']}：{item['url']}")
    return "\n".join(lines)


def compose_competition_draft(detail: NoticeDetail) -> str:
    return f"「健雄科协」竞赛通知\n{detail.title}\n详见{detail.url}\n"


def compose_lecture_draft(detail: NoticeDetail, mode: str = "weekly") -> str:
    keywords = (
        "题目",
        "报告题目",
        "讲座题目",
        "时间",
        "地点",
        "承办",
        "主讲",
        "预报名",
        "预约",
        "无需",
        "报名",
        "QQ群",
        "qq",
        "问卷",
        "https://",
        "http://",
    )
    lines = extract_relevant_lines(detail.source_text, keywords, limit=28)
    body_lines = [
        "[健雄科协] 研学讲座",
        "",
        "本周有课外研学讲座举行，欢迎各位同学参与！"
        if mode == "weekly"
        else "近期新增课外研学讲座通知如下，欢迎各位同学参与！",
        f"详情：{detail.url}",
    ]
    if lines:
        body_lines.extend(["", "自动提取的讲座信息：", *[f"- {line}" for line in lines]])
    else:
        body_lines.extend(["", "自动提取正文较少，请打开官网通知和 PDF 附件核对讲座安排。"])
    attachments = attachment_block(detail.attachments)
    if attachments:
        body_lines.extend(["", attachments])
    body_lines.extend(["", "【注】关于SRTP讲座参与方式、讲座论文提交方式等事项，请以讲座预告网页或 PDF 附件为准。"])
    return "\n".join(body_lines).strip() + "\n"


def call_ai_rewriter(detail: NoticeDetail, rule_draft: str, lecture_mode: str | None = None) -> str:
    api_key = configured_api_key()
    if not api_key:
        print("AI requested but OPENAI_API_KEY/AI_API_KEY/API_KEY is not set; using rule draft.", file=sys.stderr)
        return rule_draft

    endpoint = configured_api_endpoint()
    model = os.environ.get("OPENAI_MODEL") or os.environ.get("MODEL") or DEFAULT_MODEL
    category_name = "竞赛通知" if detail.category == "competition" else "研学讲座"
    today = dt.date.today()
    week_start = today - dt.timedelta(days=today.weekday())
    week_end = week_start + dt.timedelta(days=6)
    date_context = f"系统当前日期：{today.isoformat()}；本周范围：{week_start.isoformat()} 至 {week_end.isoformat()}。"
    lecture_mode_instruction = ""
    if detail.category == "lecture":
        if lecture_mode == "weekly":
            lecture_mode_instruction = "本次是“本周研学讲座预告”入口。整理本周内尚未发生的讲座；如果原文明确是下一周预告，则按原文使用“下周”。已经发生的讲座不要放进通知。"
        elif lecture_mode == "new":
            lecture_mode_instruction = "本次是“周中新增研学讲座通知”入口。只整理本周范围内、且相对系统当前日期尚未发生的讲座；已经发生的讲座直接忽略。不要写成整周预告。若没有符合条件的讲座，只输出“【无可发送讲座】当前没有本周内且尚未发生的新增研学讲座。”"
    prompt = f"""
请把下面的官网通知资料整理成适合学生组织发群的中文 Markdown 草稿。

要求：
1. 标题必须是“[健雄科协] {category_name}”。
2. 口吻简洁、通知式，排版适合直接复制到 QQ/微信，不要输出 YAML frontmatter。
3. 只保留学生最关心的信息，省略长篇背景、章程全文、专家履历、论文提交说明细节。
4. 比赛/讲座时间、报名截止、地点、QQ群、预报名链接、联系方式等必须忠于原文。
5. 无法确认的信息用“【待核对】”标出，不要编造。
6. 保留官网通知链接：{detail.url}
7. 只输出最终 Markdown 草稿。
{date_context}
{lecture_mode_instruction}

竞赛通知必须使用短格式，不展开正文：
「健雄科协」竞赛通知
{{官网通知标题}}
详见{detail.url}

研学讲座参考结构：
[健雄科协] 研学讲座

本周有{{数量}}场课外研学讲座举行，欢迎各位同学参与！
{{讲座题目1}}
【时间】...
【地点】...
{{讲座题目2}}
【时间】...
【地点】...
详见：{detail.url}
【注】关于SRTP讲座参与方式、讲座论文提交方式等事项，可参考讲座预告网页的 pdf 附件《本科生课外研学讲座活动指南(2026版)》。

研学讲座排版要求：
- 每个讲座独立换行，讲座标题单独一行。
- 每场讲座默认只保留【时间】和【地点】；只有原文明确有预报名、预约方式或QQ群时才额外保留。
- 必须区分是否需要预约：有“预报名/预约/报名链接/问卷链接”时写【预约】需提前预约：链接；原文或附件明确无需提前报名时写【预约】无需提前预约；无法判断时写【预约】未注明。
- 不要输出主讲人简介、内容提要、论文提交流程等长段落。
- “本周有X场”里的 X 必须按原文讲座条目数量计算；不确定则写“本周有课外研学讲座举行”。

规则版草稿：
{rule_draft}

压缩后的原始提取文本：
{compact_ai_source_text(detail, lecture_mode)}
""".strip()

    response = requests.post(
        endpoint,
        timeout=60,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "你是严谨的校园通知整理助手。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip() + "\n"


def compose_draft(detail: NoticeDetail, use_ai: bool) -> str:
    if detail.category == "competition":
        rule_draft = compose_competition_draft(detail)
    else:
        rule_draft = compose_lecture_draft(detail)
    return call_ai_rewriter(detail, rule_draft) if use_ai else rule_draft


def ai_key_configured() -> bool:
    return bool(configured_api_key())


def safe_filename(title: str, publish_date: str, url: str) -> str:
    article_match = re.search(r"a(\d+)/page\.(?:htm|psp)", url)
    article_id = article_match.group(1) if article_match else hashlib.sha1(url.encode()).hexdigest()[:8]
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = cleaned[:70] or "notice"
    prefix = publish_date or dt.date.today().isoformat()
    return f"{prefix}_{article_id}_{cleaned}.md"


def metadata_block(detail: NoticeDetail, generated_by: str) -> str:
    category_name = "competition" if detail.category == "competition" else "lecture"
    payload = {
        "title": detail.title,
        "category": category_name,
        "source_url": detail.url,
        "publish_date": detail.publish_date,
        "status": "drafted",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "generated_by": generated_by,
    }
    lines = ["---"]
    for key, value in payload.items():
        escaped = str(value).replace('"', '\\"')
        lines.append(f'{key}: "{escaped}"')
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def load_seen(root: Path) -> dict:
    path = root / SEEN_PATH
    if not path.exists():
        return {"items": {}}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_seen(root: Path, seen: dict) -> None:
    path = root / SEEN_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(seen, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def load_crawl_state(root: Path) -> dict:
    path = root / CRAWL_STATE_PATH
    if not path.exists():
        return {"last_fetched_page": 0}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_crawl_state(root: Path, state: dict) -> None:
    path = root / CRAWL_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def ensure_structure(root: Path) -> None:
    for rel in (COMPETITION_DIR, LECTURE_DIR, SENT_DIR, SEEN_PATH.parent):
        (root / rel).mkdir(parents=True, exist_ok=True)


def category_dir(category: str) -> Path:
    return COMPETITION_DIR if category == "competition" else LECTURE_DIR


def fetch_command(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    load_env_file(root)
    ensure_structure(root)
    seen = load_seen(root)
    items = seen.setdefault("items", {})
    created = 0
    skipped = 0
    updated = 0
    skipped_old = 0
    start_page = getattr(args, "start_page", 1)
    end_page = start_page + args.pages - 1
    recent_days = getattr(args, "days", DEFAULT_RECENT_DAYS)

    for page in range(start_page, end_page + 1):
        summaries, _ = parse_list_page_with_dates(page)
        page_created, page_skipped, page_updated, page_skipped_old = fetch_summaries(root, seen, summaries, args)
        created += page_created
        skipped += page_skipped
        updated += page_updated
        skipped_old += page_skipped_old

    save_seen(root, seen)
    bundled = 0
    if args.bundle_competitions:
        bundled, _ = bundle_competition_drafts(root, max_items=args.bundle_size)
    if getattr(args, "update_crawl_state", True):
        state = load_crawl_state(root)
        state["last_fetched_page"] = max(int(state.get("last_fetched_page", 0)), end_page)
        state["updated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        save_crawl_state(root, state)
    print(f"created={created} updated={updated} skipped={skipped}")
    if skipped_old:
        print(f"skipped_old={skipped_old} days={recent_days}")
    if bundled:
        print(f"competition_bundles={bundled}")
    return 0


def fetch_summaries(
    root: Path, seen: dict, summaries: list[NoticeSummary], args: argparse.Namespace
) -> tuple[int, int, int, int]:
    items = seen.setdefault("items", {})
    created = 0
    skipped = 0
    updated = 0
    skipped_old = 0
    recent_days = getattr(args, "days", DEFAULT_RECENT_DAYS)
    since_date = getattr(args, "since_date", None)
    for summary in summaries:
        if since_date is not None and not is_on_or_after(summary.publish_date, since_date):
            skipped_old += 1
            continue
        if since_date is None and not is_within_recent_days(summary.publish_date, recent_days):
            skipped_old += 1
            continue
        key = normalize_url(summary.url)
        record = items.get(key)
        changed = bool(
            record
            and (
                record.get("title") != summary.title
                or record.get("publish_date") != summary.publish_date
            )
        )
        if record and not args.refresh and not changed:
            skipped += 1
            continue
        if changed:
            remove_stale_draft(root, record)
        detail = parse_detail(summary)
        draft = compose_draft(detail, use_ai=args.ai)
        rel_dir = category_dir(detail.category)
        filename = safe_filename(detail.title, detail.publish_date, detail.url)
        output_path = root / rel_dir / filename
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            metadata_block(detail, "ai" if args.ai else "rules") + draft,
            encoding="utf-8",
        )
        items[key] = {
            "title": detail.title,
            "category": detail.category,
            "publish_date": detail.publish_date,
            "status": "drafted",
            "draft_path": str(output_path.relative_to(root)),
            "source_url": detail.url,
            "highlight": True,
            "change_type": "updated" if changed else "new",
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        if changed:
            updated += 1
        else:
            created += 1
    return created, skipped, updated, skipped_old


def update_notices(
    root: Path,
    category: str | None = None,
    days: int = DEFAULT_RECENT_DAYS,
    max_pages: int = 5,
    use_ai: bool = False,
    bundle_size: int = 2,
    since_date: dt.date | None = None,
) -> dict[str, int]:
    load_env_file(root)
    ensure_structure(root)
    seen = load_seen(root)
    created = skipped = updated = skipped_old = 0
    cutoff = since_date or (dt.date.today() - dt.timedelta(days=days))

    for page in range(1, max(1, max_pages) + 1):
        summaries, page_dates = parse_list_page_with_dates(page)
        if category:
            summaries = [summary for summary in summaries if summary.category == category]
        page_args = argparse.Namespace(ai=use_ai, refresh=False, days=days, since_date=since_date)
        page_created, page_skipped, page_updated, page_skipped_old = fetch_summaries(
            root, seen, summaries, page_args
        )
        created += page_created
        skipped += page_skipped
        updated += page_updated
        skipped_old += page_skipped_old

        parsed_dates = []
        for value in page_dates:
            try:
                parsed_dates.append(dt.date.fromisoformat(value))
            except ValueError:
                pass
        if parsed_dates and min(parsed_dates) < cutoff:
            break

    save_seen(root, seen)
    bundles = 0
    moved = 0
    if category in (None, "competition"):
        bundles, moved = bundle_competition_drafts(root, max_items=bundle_size)
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "skipped_old": skipped_old,
        "bundles": bundles,
        "moved": moved,
    }


def remove_stale_draft(root: Path, record: dict) -> None:
    draft_path = record.get("draft_path")
    if not draft_path or record.get("status") != "drafted":
        return
    path = root / draft_path
    try:
        resolved = path.resolve()
        if root.resolve() not in resolved.parents and resolved != root.resolve():
            return
        if path.exists() and path.is_file():
            path.unlink()
    except OSError:
        return


def fetch_next_command(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    load_env_file(root)
    state = load_crawl_state(root)
    args.start_page = int(state.get("last_fetched_page", 0)) + 1
    args.update_crawl_state = True
    print(f"fetching_from_page={args.start_page}")
    return fetch_command(args)


def update_command(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    result = update_notices(
        root,
        category=getattr(args, "category", None),
        days=args.days,
        max_pages=args.max_pages,
        use_ai=args.ai,
        bundle_size=args.bundle_size,
    )
    print(f"created={result['created']} updated={result['updated']} skipped={result['skipped']}")
    if result["skipped_old"]:
        print(f"skipped_old={result['skipped_old']} days={args.days}")
    if result["bundles"]:
        print(f"competition_bundles={result['bundles']}")
    return 0


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end].strip()
    body = text[end + 4 :].lstrip()
    meta: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip('"')
    return meta, body


def render_frontmatter(meta: dict[str, str]) -> str:
    lines = ["---"]
    for key, value in meta.items():
        escaped = str(value).replace('"', '\\"')
        lines.append(f'{key}: "{escaped}"')
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def source_urls_from_meta(meta: dict[str, str]) -> list[str]:
    if meta.get("source_urls"):
        raw_urls = re.split(r"\s*,\s*", meta["source_urls"].strip())
    elif meta.get("source_url"):
        raw_urls = [meta["source_url"]]
    else:
        raw_urls = []
    return [normalize_url(url) for url in raw_urls if url]


def competition_item_from_meta(meta: dict[str, str], body: str) -> str:
    title = meta.get("title", "").strip()
    url = meta.get("source_url", "").strip()
    if title and url:
        return f"{title}\n详见{url}"

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if lines and lines[0] == "「健雄科协」竞赛通知":
        lines = lines[1:]
    return "\n".join(lines)


def bundle_competition_drafts(root: Path, max_items: int = 2) -> tuple[int, int]:
    directory = root / COMPETITION_DIR
    if not directory.exists():
        return 0, 0

    candidates: list[tuple[str, Path, dict[str, str], str]] = []
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        if meta.get("category") != "competition":
            continue
        if meta.get("source_urls"):
            continue
        if not meta.get("source_url"):
            continue
        candidates.append((meta.get("publish_date", ""), path, meta, body))

    if not candidates:
        return 0, 0

    candidates.sort(key=lambda item: (item[0], item[2].get("title", "")))
    seen = load_seen(root)
    seen_items = seen.setdefault("items", {})
    backup_dir = directory / "_单条备份"
    backup_dir.mkdir(parents=True, exist_ok=True)

    created = 0
    moved = 0
    for index in range(0, len(candidates), max(1, max_items)):
        group = candidates[index : index + max(1, max_items)]
        dates = [item[2].get("publish_date", "") for item in group if item[2].get("publish_date")]
        titles = [item[2].get("title", "") for item in group]
        urls = [normalize_url(item[2]["source_url"]) for item in group]
        body_items = [competition_item_from_meta(meta, body) for _, _, meta, body in group]
        body = "「健雄科协」竞赛通知\n" + "\n".join(item for item in body_items if item).strip() + "\n"

        if len(dates) >= 2 and dates[0] != dates[-1]:
            date_part = f"{dates[0]}_to_{dates[-1]}"
        else:
            date_part = dates[0] if dates else dt.date.today().isoformat()
        filename = f"{date_part}_竞赛通知_{created + 1}.md"
        output_path = directory / filename
        if output_path.exists():
            digest = hashlib.sha1("|".join(urls).encode()).hexdigest()[:6]
            output_path = directory / f"{date_part}_竞赛通知_{digest}.md"

        bundle_meta = {
            "title": f"竞赛通知（{len(group)}条）",
            "category": "competition",
            "source_urls": ", ".join(urls),
            "publish_date": dates[0] if dates else "",
            "status": "drafted",
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "generated_by": "bundle",
        }
        output_path.write_text(render_frontmatter(bundle_meta) + body, encoding="utf-8")
        created += 1

        for _, path, meta, _ in group:
            source_url = normalize_url(meta["source_url"])
            record = seen_items.setdefault(source_url, {})
            record.update(
                {
                    "title": meta.get("title", ""),
                    "category": "competition",
                    "publish_date": meta.get("publish_date", ""),
                    "status": "drafted",
                    "draft_path": str(output_path.relative_to(root)),
                    "source_url": source_url,
                    "highlight": record.get("highlight", True),
                    "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                }
            )
            target = backup_dir / path.name
            if target.exists():
                target = backup_dir / f"{path.stem}_{hashlib.sha1(str(path).encode()).hexdigest()[:6]}.md"
            shutil.move(str(path), str(target))
            moved += 1

    save_seen(root, seen)
    return created, moved


def pending_files(root: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for category, rel_dir in (("competition", COMPETITION_DIR), ("lecture", LECTURE_DIR)):
        directory = root / rel_dir
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            files.append((category, path))
    return files


def list_command(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    rows = pending_files(root)
    if not rows:
        print("No pending drafts.")
        return 0
    for category, path in rows:
        text = path.read_text(encoding="utf-8")
        meta, _ = parse_frontmatter(text)
        label = "竞赛通知" if category == "competition" else "研学讲座通知"
        print(f"[{label}] {meta.get('title', path.stem)}")
        print(f"  file: {path.relative_to(root)}")
        for url in source_urls_from_meta(meta):
            print(f"  url:  {url}")
    return 0


def bundle_command(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    ensure_structure(root)
    created, moved = bundle_competition_drafts(root, max_items=args.size)
    print(f"competition_bundles={created} moved_singles={moved}")
    return 0


def polish_command(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    load_env_file(root)
    if not ai_key_configured():
        print("OPENAI_API_KEY, AI_API_KEY, or API_KEY is required for polish.", file=sys.stderr)
        return 2

    rows = pending_files(root)
    if args.category:
        rows = [(category, path) for category, path in rows if category == args.category]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("No pending drafts to polish.")
        return 0

    changed = 0
    for category, path in rows:
        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        source_url = meta.get("source_url")
        if not source_url:
            print(f"skip missing source_url: {path}", file=sys.stderr)
            continue
        summary = NoticeSummary(
            title=meta.get("title", path.stem),
            url=source_url,
            publish_date=meta.get("publish_date", ""),
            category=category,
        )
        detail = parse_detail(summary)
        polished = call_ai_rewriter(detail, body)
        meta["generated_by"] = "ai"
        meta["polished_at"] = dt.datetime.now().isoformat(timespec="seconds")

        if args.dry_run:
            print(f"--- {path.relative_to(root)} ---")
            print(polished)
            continue

        path.write_text(render_frontmatter(meta) + polished, encoding="utf-8")
        changed += 1

    print(f"polished={changed}")
    return 0


def section_title(category: str) -> str:
    return "竞赛通知" if category == "competition" else "研学讲座通知"


def archive_draft_path(root: Path, draft_path: Path, sent_date: str | None = None) -> dict[str, object]:
    ensure_structure(root)
    path = draft_path if draft_path.is_absolute() else root / draft_path
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if resolved_root not in resolved_path.parents and resolved_path != resolved_root:
        raise ValueError(f"Draft path is outside project root: {draft_path}")
    if not path.exists():
        raise FileNotFoundError(path)

    text = path.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    category = meta.get("category")
    if category not in ("competition", "lecture"):
        raise ValueError(f"Unknown draft category: {category}")

    date_value = sent_date or dt.date.today().isoformat()
    archive_path = root / SENT_DIR / f"{date_value}.md"
    existing = archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
    section = section_title(category)
    chunks: list[str] = []
    if not existing.strip():
        chunks.append(f"# 「健雄科协」{date_value}通知")
    if f"## {section}" not in existing:
        chunks.append(f"## {section}")
    chunks.append(body.strip())

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n\n"):
            handle.write("\n\n")
        handle.write("\n\n".join(chunk for chunk in chunks if chunk).strip())
        handle.write("\n")

    urls = source_urls_from_meta(meta)
    seen = load_seen(root)
    items = seen.setdefault("items", {})
    for url in urls:
        record = items.setdefault(normalize_url(url), {})
        record.update(
            {
                "title": record.get("title") or meta.get("title", ""),
                "category": category,
                "publish_date": record.get("publish_date") or meta.get("publish_date", ""),
                "status": "archived",
                "sent_date": date_value,
                "archive_path": str(archive_path.relative_to(root)),
                "draft_path": "",
                "highlight": False,
                "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )
    save_seen(root, seen)

    backup_dir = root / ARCHIVED_DRAFT_DIR / date_value / section
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / path.name
    if target.exists():
        target = backup_dir / f"{path.stem}_{hashlib.sha1(str(path).encode()).hexdigest()[:6]}.md"
    shutil.move(str(path), str(target))

    return {
        "archived": len(urls),
        "archive_path": str(archive_path.relative_to(root)),
        "backup_path": str(target.relative_to(root)),
        "category": category,
    }


def archive_command(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    ensure_structure(root)
    rows = pending_files(root)
    if not rows:
        print("No pending drafts to archive.")
        return 0

    grouped: dict[str, list[tuple[Path, dict[str, str], str]]] = {"competition": [], "lecture": []}
    for category, path in rows:
        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        grouped[category].append((path, meta, body.strip()))

    archive_path = root / SENT_DIR / f"{args.date}.md"
    existing = archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
    blocks: list[str] = []
    if not existing.strip():
        blocks.append(f"# 「健雄科协」{args.date}通知\n")

    archived_urls: list[str] = []
    for category in ("competition", "lecture"):
        entries = grouped[category]
        if not entries:
            continue
        section_blocks = [f"## {section_title(category)}"]
        for _, meta, body in entries:
            source_urls = source_urls_from_meta(meta)
            if source_urls and all(source_url in existing for source_url in source_urls):
                continue
            section_blocks.append(body)
            archived_urls.extend(source_urls)
        if len(section_blocks) > 1:
            blocks.append("\n\n".join(section_blocks))

    if len(blocks) == 1 and existing.strip():
        print("All pending drafts already appear in archive.")
        return 0

    if args.dry_run:
        print("\n\n".join(blocks).strip())
        return 0

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with archive_path.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n\n"):
            handle.write("\n\n")
        handle.write("\n\n".join(blocks).strip())
        handle.write("\n")

    seen = load_seen(root)
    items = seen.setdefault("items", {})
    for category, entries in grouped.items():
        backup_dir = root / ARCHIVED_DRAFT_DIR / args.date / section_title(category)
        backup_dir.mkdir(parents=True, exist_ok=True)
        for path, meta, _ in entries:
            for source_url in source_urls_from_meta(meta):
                record = items.setdefault(source_url, {})
                record.update(
                    {
                        "status": "archived",
                        "sent_date": args.date,
                        "archive_path": str(archive_path.relative_to(root)),
                        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                    }
                )
            if args.keep_drafts:
                continue
            target = backup_dir / path.name
            if target.exists():
                target = backup_dir / f"{path.stem}_{hashlib.sha1(str(path).encode()).hexdigest()[:6]}.md"
            shutil.move(str(path), str(target))

    save_seen(root, seen)
    print(f"archived={len(archived_urls)} file={archive_path.relative_to(root)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="整理东南大学竞赛与研学讲座通知。")
    parser.add_argument("--root", help="项目根目录，默认取脚本所在仓库根目录。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser("fetch", help="抓取官网通知并生成待发草稿。")
    fetch_parser.add_argument("--pages", type=int, default=1, help="抓取列表页数量，默认 1。")
    fetch_parser.add_argument("--start-page", type=int, default=1, help="从第几页开始抓取，默认 1。")
    fetch_parser.add_argument("--days", type=int, default=DEFAULT_RECENT_DAYS, help="只抓取最近几天发布的通知，默认 7；传 -1 关闭。")
    fetch_parser.add_argument("--ai", action="store_true", help="配置 API Key 后调用大模型润色草稿。")
    fetch_parser.add_argument("--refresh", action="store_true", help="重新生成已见过通知的草稿。")
    fetch_parser.add_argument(
        "--no-bundle-competitions",
        dest="bundle_competitions",
        action="store_false",
        help="不自动把竞赛草稿按 1-2 条合并。",
    )
    fetch_parser.add_argument("--bundle-size", type=int, default=2, help="每条竞赛待发文档最多包含几条，默认 2。")
    fetch_parser.set_defaults(bundle_competitions=True)
    fetch_parser.set_defaults(update_crawl_state=True)
    fetch_parser.set_defaults(func=fetch_command)

    update_parser = subparsers.add_parser("update", help="从第一页开始扫描，抓取最近几天内发布且未处理的通知。")
    update_parser.add_argument("--days", type=int, default=DEFAULT_RECENT_DAYS, help="只抓取最近几天发布的通知，默认 7；传 -1 关闭。")
    update_parser.add_argument("--max-pages", type=int, default=5, help="最多扫描列表页数，默认 5，通常会在遇到七天前内容后提前停止。")
    update_parser.add_argument("--ai", action="store_true", help="配置 API Key 后调用大模型润色草稿。")
    update_parser.add_argument("--bundle-size", type=int, default=2, help="每条竞赛待发文档最多包含几条，默认 2。")
    update_parser.set_defaults(func=update_command)

    list_parser = subparsers.add_parser("list", help="列出待发草稿。")
    list_parser.set_defaults(func=list_command)

    bundle_parser = subparsers.add_parser("bundle", help="把竞赛待发草稿按相近日期两两合并。")
    bundle_parser.add_argument("--size", type=int, default=2, help="每条合并文档最多包含几条，默认 2。")
    bundle_parser.set_defaults(func=bundle_command)

    polish_parser = subparsers.add_parser("polish", help="用大模型重新排版待发草稿。")
    polish_parser.add_argument(
        "--category",
        choices=("competition", "lecture"),
        help="只处理指定类型；默认处理全部待发草稿。",
    )
    polish_parser.add_argument("--limit", type=int, help="最多处理多少个草稿。")
    polish_parser.add_argument("--dry-run", action="store_true", help="只打印 AI 改写结果，不写回文件。")
    polish_parser.set_defaults(func=polish_command)

    archive_parser = subparsers.add_parser("archive", help="把待发草稿合并到指定日期的已发文档。")
    archive_parser.add_argument("--date", required=True, help="发送日期，如 2026-05-28。")
    archive_parser.add_argument("--dry-run", action="store_true", help="只打印归档内容，不写文件。")
    archive_parser.add_argument("--keep-drafts", action="store_true", help="归档后保留待发草稿。")
    archive_parser.set_defaults(func=archive_command)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
