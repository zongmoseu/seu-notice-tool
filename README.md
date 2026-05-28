# 健雄科协通知整理工具

本项目把东南大学教务处实践教学栏目中的竞赛通知、课外研学讲座通知整理成本地网页，支持更新、复制、标记已发和归档。

## 启动

双击：

```text
启动通知整理工具.bat
```

或命令行运行：

```powershell
python scripts\notice_web.py
```

打开后访问：

```text
http://127.0.0.1:8765
```

## API 配置

复制 `.env.example` 为 `.env`，填写自己的接口：

```env
API_URL=https://your-api-provider.example
API_KEY=replace-with-your-key
MODEL=gpt-5.5
```

`API_URL` 可以是根地址，也可以是 OpenAI 兼容的 `/v1` 地址；程序会自动补齐 `/chat/completions`。`.env` 已写入 `.gitignore`，不要公开分享。

## 免责声明

本工具只整理官网公开通知，生成文案仅供健雄科协内部发布前参考，不代表东南大学或教务处官方口径。涉及时间、地点、预约方式、报名截止、QQ群和附件要求等关键信息，请发布前以官网原文和 PDF 附件为准；AI 排版内容尤其需要人工核对。

## 页面规则

- 竞赛通知页：按月份展示，每 1-2 条组成一个可直接发送的文案块；凑不到两条时一条也单独成块。
- 研学讲座页：按官网周次展示整周预告，左侧目录显示月份和周次范围，例如 `第十三周（05.25-05.31）`。
- 红点表示新增或更新；标记已发后会归档到 `已发/YYYY-MM-DD.md`，并在页面变灰。
- `【待核对】` 会在页面中标红；复制时仍保留纯文本，方便人工核对后修改。

## 研学讲座更新逻辑

- 每周第一次点击“更新研学讲座通知”时，视为建立本周整周预告基线，文案使用“本周有 X 场课外研学讲座”。
- 同一周后续再点击更新：如果官网没有新通知或标题/发布日期变化，提示“暂无更新”。
- 同一周后续如发现新增或更新，会生成单独的“周中新增”区块，文案使用“本周新增 X 场课外研学讲座”，不打乱原来的整周预告。
- 如果“周中新增”区块没有标记已发，下次又发现新的讲座，会合并进这个未发区块。
- 如果未发的周中新增区块里讲座时间已经过去，会自动标记为“已过期”并变灰；过期不是已发，不会写入归档。

## 命令行

保留命令行工具，便于调试：

```powershell
python scripts\notice_tool.py update --category competition
python scripts\notice_tool.py update --category lecture
python scripts\notice_tool.py list
python scripts\notice_tool.py archive --date 2026-05-28
```

通常直接用本地网页即可。
