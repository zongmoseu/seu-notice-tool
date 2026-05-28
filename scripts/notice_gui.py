from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import notice_tool as tool


class NoticeGui(tk.Tk):
    def __init__(self, root_path: Path):
        super().__init__()
        self.root_path = root_path
        tool.load_env_file(self.root_path)
        self.selected_path: Path | None = None
        self.selected_category: str | None = None

        self.title("健雄科协通知整理")
        self.geometry("900x640")
        self.minsize(760, 520)

        self.status_var = tk.StringVar(value="就绪")
        self.summary_var = tk.StringVar(value="")

        self._build_ui()
        self.refresh_summary()

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=8)
        toolbar.pack(fill=tk.X)

        ttk.Button(toolbar, text="更新：扫描近7天", command=self.update_notices).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="获取两条竞赛通知", command=lambda: self.load_earliest("competition")).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(toolbar, text="新一周研学讲座预告", command=lambda: self.load_lecture("weekly")).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(toolbar, text="新增研学讲座通知", command=lambda: self.load_lecture("new")).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(toolbar, text="保存修改（不归档）", command=self.save_selected_preview).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="加入已发", command=self.archive_selected).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="刷新", command=self.refresh_summary).pack(side=tk.LEFT)

        info = ttk.Frame(self, padding=(8, 0, 8, 6))
        info.pack(fill=tk.X)
        ttk.Label(info, textvariable=self.summary_var).pack(side=tk.LEFT)
        ttk.Label(info, textvariable=self.status_var).pack(side=tk.RIGHT)

        self.preview = tk.Text(self, wrap=tk.WORD, undo=False, font=("Microsoft YaHei UI", 10))
        self.preview.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

    def set_status(self, text: str) -> None:
        self.status_var.set(text)
        self.update_idletasks()

    def pending_rows(self) -> list[tuple[str, Path, dict[str, str], str]]:
        rows: list[tuple[str, Path, dict[str, str], str]] = []
        for category, path in tool.pending_files(self.root_path):
            text = path.read_text(encoding="utf-8")
            meta, body = tool.parse_frontmatter(text)
            if not tool.is_within_recent_days(meta.get("publish_date", ""), tool.DEFAULT_RECENT_DAYS):
                continue
            rows.append((category, path, meta, body))
        return rows

    def refresh_summary(self) -> None:
        rows = self.pending_rows()
        competition = sum(1 for category, *_ in rows if category == "competition")
        lecture = sum(1 for category, *_ in rows if category == "lecture")
        self.summary_var.set(f"近 7 天待发：竞赛 {competition} 个文档，研学讲座 {lecture} 个文档")
        self.set_status("已刷新")

    def load_earliest(self, category: str) -> None:
        if category == "competition":
            tool.bundle_competition_drafts(self.root_path, max_items=2)
        candidates = [row for row in self.pending_rows() if row[0] == category]
        if not candidates:
            messagebox.showinfo("没有待发", "该类型暂无待发通知。")
            return
        candidates.sort(key=lambda row: (row[2].get("publish_date", ""), row[1].name))
        selected_category, path, meta, body = candidates[0]
        if category == "lecture" and tool.ai_key_configured() and meta.get("source_url"):
            self.set_status("正在用 AI 整理研学讲座排版...")
            summary = tool.NoticeSummary(
                title=meta.get("title", path.stem),
                url=meta["source_url"],
                publish_date=meta.get("publish_date", ""),
                category="lecture",
            )
            try:
                detail = tool.parse_detail(summary)
                body = tool.call_ai_rewriter(detail, tool.compose_lecture_draft(detail)).strip() + "\n"
                meta["generated_by"] = "ai"
                meta["polished_at"] = dt.datetime.now().isoformat(timespec="seconds")
                path.write_text(tool.render_frontmatter(meta) + body, encoding="utf-8")
            except Exception as exc:
                messagebox.showwarning("AI整理失败", f"将显示规则版草稿。\n{exc}")
        self.selected_category = selected_category
        self.selected_path = path
        self.preview.delete("1.0", tk.END)
        self.preview.insert(tk.END, body.strip() + "\n")
        label = "竞赛通知" if category == "competition" else "研学讲座通知"
        self.refresh_summary()
        self.set_status(f"已获取{label}：{path.name}。不归档则会保留在待发。")

    def load_lecture(self, mode: str) -> None:
        candidates = [row for row in self.pending_rows() if row[0] == "lecture"]
        if not candidates:
            messagebox.showinfo("没有待发", "暂无近 7 天内发布的研学讲座通知。")
            return
        if mode == "weekly":
            candidates.sort(key=lambda row: (row[2].get("publish_date", ""), row[1].name), reverse=True)
        else:
            candidates.sort(key=lambda row: (row[2].get("publish_date", ""), row[1].name))

        selected_category, path, meta, body = candidates[0]
        if tool.ai_key_configured() and meta.get("source_url"):
            label = "本周研学讲座预告" if mode == "weekly" else "周中新增讲座"
            self.set_status(f"正在用 AI 整理{label}...")
            summary = tool.NoticeSummary(
                title=meta.get("title", path.stem),
                url=meta["source_url"],
                publish_date=meta.get("publish_date", ""),
                category="lecture",
            )
            try:
                detail = tool.parse_detail(summary)
                body = tool.call_ai_rewriter(
                    detail,
                    tool.compose_lecture_draft(detail, mode=mode),
                    lecture_mode=mode,
                ).strip() + "\n"
                meta["generated_by"] = "ai"
                meta["lecture_mode"] = mode
                meta["polished_at"] = dt.datetime.now().isoformat(timespec="seconds")
                path.write_text(tool.render_frontmatter(meta) + body, encoding="utf-8")
            except Exception as exc:
                messagebox.showwarning("AI整理失败", f"将显示规则版草稿。\n{exc}")

        self.selected_category = selected_category
        self.selected_path = path
        self.preview.delete("1.0", tk.END)
        self.preview.insert(tk.END, body.strip() + "\n")
        self.refresh_summary()
        label = "本周研学讲座预告" if mode == "weekly" else "周中新增讲座"
        self.set_status(f"已获取{label}：{path.name}。不归档则会保留在待发。")

    def update_notices(self) -> None:
        def worker() -> None:
            try:
                self.after(0, lambda: self.set_status("正在更新：扫描近 7 天发布的通知..."))
                args = SimpleNamespace(
                    root=str(self.root_path),
                    ai=False,
                    bundle_size=2,
                    days=tool.DEFAULT_RECENT_DAYS,
                    max_pages=5,
                )
                tool.update_command(args)
                self.after(0, self.refresh_summary)
                self.after(0, lambda: self.set_status("更新完成"))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("更新失败", str(exc)))
                self.after(0, lambda: self.set_status("更新失败"))

        threading.Thread(target=worker, daemon=True).start()

    def archive_selected(self) -> None:
        if not self.selected_path or not self.selected_category:
            messagebox.showinfo("未选择", "请先抽取一条待发通知。")
            return
        if not self.selected_path.exists():
            messagebox.showwarning("文件不存在", "当前选中的待发文件已不存在，请刷新后重试。")
            self.refresh_summary()
            return

        send_date = simpledialog.askstring("发送日期", "归档到哪个发送日期？", initialvalue=dt.date.today().isoformat())
        if not send_date:
            return
        if not messagebox.askyesno("确认加入已发", f"确认把当前通知加入 已发/{send_date}.md 吗？"):
            return

        self.save_selected_preview(show_message=False)
        self._archive_path(self.selected_category, self.selected_path, send_date)
        self.preview.delete("1.0", tk.END)
        self.selected_path = None
        self.selected_category = None
        self.refresh_summary()
        self.set_status(f"已加入已发/{send_date}.md")

    def save_selected_preview(self, show_message: bool = True) -> None:
        if not self.selected_path or not self.selected_path.exists():
            if show_message:
                messagebox.showinfo("未选择", "请先获取一条待发通知。")
            return
        text = self.selected_path.read_text(encoding="utf-8")
        meta, _ = tool.parse_frontmatter(text)
        body = self.preview.get("1.0", tk.END).strip()
        self.selected_path.write_text(tool.render_frontmatter(meta) + body + "\n", encoding="utf-8")
        if show_message:
            self.set_status(f"已保存修改：{self.selected_path.name}")

    def _archive_path(self, category: str, path: Path, send_date: str) -> None:
        text = path.read_text(encoding="utf-8")
        meta, body = tool.parse_frontmatter(text)
        urls = tool.source_urls_from_meta(meta)
        archive_path = self.root_path / tool.SENT_DIR / f"{send_date}.md"
        existing = archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
        section = tool.section_title(category)

        if urls and all(url in existing for url in urls):
            if not messagebox.askyesno("可能重复", "该通知链接已经出现在已发文档中，仍然加入吗？"):
                return

        chunks: list[str] = []
        if not existing.strip():
            chunks.append(f"# 「健雄科协」{send_date}通知")
        if f"## {section}" not in existing:
            chunks.append(f"## {section}")
        chunks.append(body.strip())

        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with archive_path.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n\n"):
                handle.write("\n\n")
            handle.write("\n\n".join(chunks).strip())
            handle.write("\n")

        seen = tool.load_seen(self.root_path)
        items = seen.setdefault("items", {})
        for url in urls:
            record = items.setdefault(url, {})
            record.update(
                {
                    "status": "archived",
                    "sent_date": send_date,
                    "archive_path": str(archive_path.relative_to(self.root_path)),
                    "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                }
            )
        tool.save_seen(self.root_path, seen)

        backup_dir = self.root_path / tool.ARCHIVED_DRAFT_DIR / send_date / section
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / path.name
        if target.exists():
            target = backup_dir / f"{path.stem}_{hashlib.sha1(str(path).encode()).hexdigest()[:6]}.md"
        shutil.move(str(path), str(target))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="健雄科协通知整理图形界面。")
    parser.add_argument("--root", default=str(tool.repo_root()), help="项目根目录。")
    args = parser.parse_args(argv)
    app = NoticeGui(Path(args.root).resolve())
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
