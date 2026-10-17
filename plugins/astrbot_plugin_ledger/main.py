import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.star.register import register_on_astrbot_loaded


DB_PATH = Path(__file__).parent / "ledger.db"
DEFAULT_PROACTIVE_DB = Path(__file__).parent.parent / "astrbot_plugin_proactive_contact" / "proactive.db"

CATEGORY_LABELS = {
    "promise": "约定",
    "wish": "愿望",
    "thought": "心事",
}
VALID_CATEGORIES = set(CATEGORY_LABELS.keys())

STATUS_LABELS = {
    "in_progress": "进行中",
    "active": "生效中",
    "achieved": "已实现",
    "void": "作废",
}
VALID_STATUSES = set(STATUS_LABELS.keys())


def _status_mark(status):
    return {
        "in_progress": "",
        "active": "⚡",
        "achieved": "✓",
        "void": "✗",
    }.get(status, f"[{status}]")


# ==================== 数据库 ====================

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                umo TEXT NOT NULL,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT DEFAULT 'in_progress',
                due_at TEXT,
                pushed_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ledger_umo ON ledger(umo, status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ledger_due ON ledger(status, due_at)")


def add_entry(umo, category, content, due_at=None, status="in_progress"):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO ledger (umo, category, content, due_at, status) VALUES (?, ?, ?, ?, ?)",
            (umo, category, content, due_at, status),
        )
        return cur.lastrowid


def get_entry(entry_id):
    with _conn() as c:
        return c.execute("SELECT * FROM ledger WHERE id=?", (entry_id,)).fetchone()


def list_entries(umo, category=None, status=None, limit=50):
    sql = "SELECT * FROM ledger WHERE umo=?"
    params = [umo]
    if category:
        sql += " AND category=?"
        params.append(category)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        return c.execute(sql, params).fetchall()


def search_entries(umo, keyword, limit=20):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM ledger WHERE umo=? AND content LIKE ? ORDER BY id DESC LIMIT ?",
            (umo, f"%{keyword}%", limit),
        ).fetchall()


def update_entry(entry_id, **kwargs):
    fields = []
    values = []
    for k in ("status", "content", "due_at", "category"):
        if k in kwargs:
            fields.append(f"{k}=?")
            values.append(kwargs[k])
    if not fields:
        return False
    fields.append("updated_at=CURRENT_TIMESTAMP")
    values.append(entry_id)
    with _conn() as c:
        cur = c.execute(f"UPDATE ledger SET {', '.join(fields)} WHERE id=?", values)
        return cur.rowcount > 0


def entries_due_within(umo, within_hours, limit=3):
    """未完成（进行中）、有 due_at、且在 within_hours 内到期（含已过期）"""
    threshold = (datetime.now() + timedelta(hours=within_hours)).isoformat(timespec="seconds")
    with _conn() as c:
        return c.execute(
            "SELECT * FROM ledger WHERE umo=? AND status='in_progress' "
            "AND due_at IS NOT NULL AND due_at <= ? "
            "ORDER BY due_at LIMIT ?",
            (umo, threshold, limit),
        ).fetchall()


def entries_overdue(umo, limit=10):
    """已过期、进行中、且未推送过的条目"""
    now_str = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        return c.execute(
            "SELECT * FROM ledger WHERE umo=? AND status='in_progress' "
            "AND due_at IS NOT NULL AND due_at <= ? "
            "AND pushed_at IS NULL "
            "ORDER BY due_at LIMIT ?",
            (umo, now_str, limit),
        ).fetchall()


def recent_entries(umo, limit=3):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM ledger WHERE umo=? ORDER BY id DESC LIMIT ?",
            (umo, limit),
        ).fetchall()


def all_active_umos():
    with _conn() as c:
        return [r["umo"] for r in c.execute("SELECT DISTINCT umo FROM ledger").fetchall()]


# ==================== 推送到 proactive_contact ====================

def push_timer_to_proactive(proactive_db_path, umo, entry):
    """把一条到期账本写入 proactive_contact 的 timers 表，避免重复"""
    if not proactive_db_path.exists():
        return False
    label = CATEGORY_LABELS.get(entry["category"], entry["category"])
    event_desc = f"[账本-{label}] {entry['content']}"
    try:
        conn = sqlite3.connect(proactive_db_path)
        # 去重：同 umo 同事件描述且 pending 的不重复推
        exist = conn.execute(
            "SELECT 1 FROM timers WHERE umo=? AND type='oneshot' "
            "AND event_context=? AND status='pending' LIMIT 1",
            (umo, event_desc),
        ).fetchone()
        if exist:
            conn.close()
            return False
        conn.execute(
            "INSERT INTO timers (umo, type, fire_at, event_context, reason) "
            "VALUES (?, 'oneshot', ?, ?, ?)",
            (
                umo,
                datetime.now().isoformat(timespec="seconds"),
                event_desc,
                f"账本条目 #{entry['id']} 到期",
            ),
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.warning(f"HerLedger: 推送到 proactive_contact 失败: {e}")
        return False


# ==================== 插件主体 ====================

_plugin_instance = None


@register_on_astrbot_loaded()
async def _on_astrbot_loaded(*args, **kwargs):
    if _plugin_instance is not None:
        _plugin_instance._ensure_task()
        logger.info("HerLedger: AstrBot 加载完成，后台扫描已启动")


@register("astrbot_plugin_ledger", "Mephisto", "她的私人账本", "1.0.0")
class HerLedger(Star):

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        init_db()
        self.config = config
        self.inject_recent = max(0, int(config.get("inject_recent_count", 3)))
        self.inject_due = max(0, int(config.get("inject_due_count", 3)))
        self.due_soon_hours = int(config.get("due_soon_hours", 24))
        self.scan_interval = max(30, int(config.get("scan_interval_seconds", 60)))
        custom_path = (config.get("proactive_db_path") or "").strip()
        self.proactive_db_path = Path(custom_path) if custom_path else DEFAULT_PROACTIVE_DB
        self._task = None
        self._stop = False
        self._inject_cache = None
        self._inject_turn = 0
        global _plugin_instance
        _plugin_instance = self
        logger.info(f"HerLedger 已加载，proactive db: {self.proactive_db_path}")

    # ---------- 后台扫描 ----------

    def _ensure_task(self):
        if self._task and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._loop())
        logger.info("HerLedger 后台扫描已启动")

    async def _loop(self):
        while not self._stop:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"HerLedger 扫描异常: {e}")
            await asyncio.sleep(self.scan_interval)

    async def _tick(self):
        for umo in all_active_umos():
            due = entries_overdue(umo, limit=10)
            for entry in due:
                pushed = push_timer_to_proactive(self.proactive_db_path, umo, entry)
                if pushed:
                    with _conn() as c:
                        c.execute(
                            "UPDATE ledger SET pushed_at=CURRENT_TIMESTAMP WHERE id=?",
                            (entry["id"],),
                        )
                    logger.info(f"账本到期推送: {umo} #{entry['id']} {entry['content']}")

    # ---------- 每轮注入 ----------

    @filter.on_llm_request()
    async def inject_ledger(self, event: AstrMessageEvent, req):
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return

        self._inject_turn += 1

        # 每 5 轮刷新一次；其余轮次用缓存
        if self._inject_turn % 5 != 1 and self._inject_cache is not None:
            req.system_prompt = (req.system_prompt or "") + "\n\n" + self._inject_cache
            return

        due = entries_due_within(umo, within_hours=self.due_soon_hours, limit=self.inject_due) if self.inject_due else []
        recent = recent_entries(umo, limit=self.inject_recent) if self.inject_recent else []

        parts = []
        if due:
            lines = []
            for r in due:
                label = CATEGORY_LABELS.get(r["category"], r["category"])
                due_str = (r["due_at"] or "")[:16]
                lines.append(f"  #{r['id']} [{label}] {r['content']}（{due_str}）")
            parts.append("【快要到或已到期的账本条目】\n" + "\n".join(lines))
        if recent:
            lines = []
            for r in recent:
                label = CATEGORY_LABELS.get(r["category"], r["category"])
                mark = _status_mark(r["status"])
                lines.append(f"  #{r['id']} [{label}]{mark} {r['content']}")
            parts.append("【你最近写进账本的条目】\n" + "\n".join(lines))

        if parts:
            block = (
                "你的私人账本（记录：约定 / 愿望 / 心事）：\n"
                + "\n\n".join(parts)
                + "\n\n"
            )
        else:
            block = "你的私人账本目前是空的。\n\n"

        block += (
            "账本分类：promise（约定）/ wish（愿望）/ thought（心事）\n"
            "账本状态：in_progress（进行中）/ active（生效中）/ "
            "achieved（已实现）/ void（作废）\n"
            "你可以用工具管理：ledger_write / ledger_list / ledger_update / ledger_search。\n"
            "当约定或愿望到期时，你会在主动联系时被唤醒决定要不要提。\n"
            "遇到值得记下的约定、愿望、心事时，主动 ledger_write。"
        )

        self._inject_cache = block
        req.system_prompt = (req.system_prompt or "") + "\n\n" + block

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="ledger_write")
    async def tool_ledger_write(self, event: AstrMessageEvent,
                                category: str, content: str,
                                due_at: str = "", status: str = ""):
        """往你自己的私人账本里写一条记录。

        用第一人称"我"来写，对话的另一方是"用户"。

        category 三选一：
        - promise：你和用户之间的约定
        - wish：你自己许下的愿望
        - thought：你的心事、情绪、想记下来的念头

        status 四选一（不填则默认 in_progress）：
        - in_progress：进行中（有终点，会做到某件事后结束；通常带 due_at）
        - active：生效中（长期有效，没有终点，例如"不互相骗对方"）
        - achieved：已实现（已经完成了）
        - void：作废（取消了）

        判断建议：
        - "周末陪他看电影" → promise + in_progress，带 due_at
        - "以后不互相骗对方" → promise + active
        - "想学会写诗" → wish + in_progress，可带 due_at
        - "今天很开心" → thought + active（一种情绪状态）

        Args:
            category (str): promise / wish / thought
            content (str): 一句话写清楚，第一人称（用"我"）
            due_at (str): 到期时间 YYYY-MM-DD HH:MM，可留空。进行中的约定/愿望通常要填
            status (str): in_progress / active / achieved / void，可留空
        """
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return "无法获取会话标识"

        category = (category or "").strip().lower()
        if category not in VALID_CATEGORIES:
            return "category 必须是 promise / wish / thought 之一"

        content = (content or "").strip()
        if not content:
            return "内容为空"
        if len(content) > 300:
            return "内容过长，请压缩到 300 字以内"

        status_clean = (status or "").strip().lower() or "in_progress"
        if status_clean not in VALID_STATUSES:
            return "status 必须是 in_progress / active / achieved / void 之一"

        due_clean = None
        raw = (due_at or "").strip()
        if raw:
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    due_clean = datetime.strptime(raw, fmt).isoformat(timespec="seconds")
                    break
                except ValueError:
                    continue
            if due_clean is None:
                return f"时间格式不对：{due_at}。请用 YYYY-MM-DD HH:MM"

        entry_id = add_entry(umo, category, content, due_clean, status_clean)
        label = CATEGORY_LABELS[category]
        status_label = STATUS_LABELS[status_clean]
        due_hint = f"（{due_clean[:16]} 到期）" if due_clean else ""
        return f"已记入账本 #{entry_id} [{label}/{status_label}]：{content}{due_hint}"

    @filter.llm_tool(name="ledger_list")
    async def tool_ledger_list(self, event: AstrMessageEvent,
                               category: str = "", status: str = ""):
        """查看你的私人账本。

        Args:
            category (str): 可选，筛选分类：promise / wish / thought
            status (str): 可选，筛选状态：in_progress / active / achieved / void
        """
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return "无法获取会话标识"

        cat = (category or "").strip().lower() or None
        if cat and cat not in VALID_CATEGORIES:
            return "category 必须是 promise / wish / thought 或留空"

        st = (status or "").strip().lower() or None
        if st and st not in VALID_STATUSES:
            return "status 必须是 in_progress / active / achieved / void 或留空"

        rows = list_entries(umo, category=cat, status=st, limit=50)
        if not rows:
            return "账本为空或没有符合条件的条目"

        lines = []
        for r in rows:
            label = CATEGORY_LABELS.get(r["category"], r["category"])
            mark = _status_mark(r["status"])
            due = f" @{r['due_at'][:16]}" if r["due_at"] else ""
            lines.append(f"#{r['id']} [{label}]{mark} {r['content']}{due}")
        return "账本条目：\n" + "\n".join(lines)

    @filter.llm_tool(name="ledger_update")
    async def tool_ledger_update(self, event: AstrMessageEvent,
                                 entry_id: int, status: str = "",
                                 content: str = "", due_at: str = ""):
        """修改账本中的某一条。可用于标记状态、修改内容或到期时间。

        Args:
            entry_id (int): 要修改的条目 ID
            status (str): 可选，新状态：in_progress / active / achieved / void
            content (str): 可选，新内容
            due_at (str): 可选，新到期时间 YYYY-MM-DD HH:MM；传 "clear" 清除
        """
        entry = get_entry(int(entry_id))
        if not entry:
            return f"找不到 #{entry_id}"

        kwargs = {}
        if status:
            status = status.strip().lower()
            if status not in VALID_STATUSES:
                return "status 必须是 in_progress / active / achieved / void"
            kwargs["status"] = status
        if content:
            content = content.strip()
            if len(content) > 300:
                return "内容过长"
            kwargs["content"] = content
        if due_at:
            raw = due_at.strip()
            if raw.lower() == "clear":
                kwargs["due_at"] = None
            else:
                parsed = None
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                    try:
                        parsed = datetime.strptime(raw, fmt).isoformat(timespec="seconds")
                        break
                    except ValueError:
                        continue
                if parsed is None:
                    return f"时间格式不对：{due_at}"
                kwargs["due_at"] = parsed

        if not kwargs:
            return "没有要修改的字段"

        ok = update_entry(int(entry_id), **kwargs)
        return f"已更新 #{entry_id}" if ok else f"更新失败"

    @filter.llm_tool(name="ledger_search")
    async def tool_ledger_search(self, event: AstrMessageEvent, keyword: str):
        """在账本里按关键词搜索。

        Args:
            keyword (str): 搜索关键词
        """
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return "无法获取会话标识"
        kw = (keyword or "").strip()
        if not kw:
            return "关键词为空"
        rows = search_entries(umo, kw, limit=20)
        if not rows:
            return f"没找到包含“{kw}”的条目"
        lines = []
        for r in rows:
            label = CATEGORY_LABELS.get(r["category"], r["category"])
            mark = _status_mark(r["status"])
            due = f" @{r['due_at'][:16]}" if r["due_at"] else ""
            lines.append(f"#{r['id']} [{label}]{mark} {r['content']}{due}")
        return "找到：\n" + "\n".join(lines)

    # ---------- 用户命令 /ledger ----------

    @filter.command("ledger")
    async def cmd_ledger(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        for p in ("/ledger", "／ledger", "ledger"):
            if raw.startswith(p):
                raw = raw[len(p):].strip()
                break
        parts = raw.split(maxsplit=2)
        sub = parts[0].lower() if parts else ""
        umo = getattr(event, "unified_msg_origin", "") or ""

        if sub in ("", "help"):
            yield event.plain_result(
                "/ledger list [分类]       查看账本（promise/wish/thought）\n"
                "/ledger add <分类> <内容>   手动添加一条\n"
                "/ledger doing <ID>        标记为进行中\n"
                "/ledger active <ID>       标记为生效中\n"
                "/ledger done <ID>         标记为已实现\n"
                "/ledger void <ID>         标记为作废\n"
                "/ledger del <ID>          删除\n"
                "/ledger search <关键词>    搜索\n"
                "/ledger due <ID> <时间>    设置到期时间（YYYY-MM-DD HH:MM）"
            )
            return

        if sub == "list":
            cat = parts[1].strip().lower() if len(parts) > 1 else None
            if cat and cat not in VALID_CATEGORIES:
                yield event.plain_result("分类必须是 promise / wish / thought")
                return
            rows = list_entries(umo, category=cat, limit=50)
            if not rows:
                yield event.plain_result("账本为空")
                return
            lines = []
            for r in rows:
                label = CATEGORY_LABELS.get(r["category"], r["category"])
                mark = _status_mark(r["status"])
                due = f" @{r['due_at'][:16]}" if r["due_at"] else ""
                lines.append(f"#{r['id']} [{label}]{mark} {r['content']}{due}")
            yield event.plain_result("\n".join(lines)[:3500])
            return

        if sub == "add":
            if len(parts) < 3:
                yield event.plain_result("用法：/ledger add <promise|wish|thought> <内容>")
                return
            cat = parts[1].strip().lower()
            if cat not in VALID_CATEGORIES:
                yield event.plain_result("分类必须是 promise / wish / thought")
                return
            content = parts[2].strip()
            eid = add_entry(umo, cat, content, None, "in_progress")
            yield event.plain_result(f"已添加 #{eid} [{CATEGORY_LABELS[cat]}] {content}")
            return

        if sub in ("doing", "active", "done", "void"):
            if len(parts) < 2 or not parts[1].isdigit():
                yield event.plain_result(f"用法：/ledger {sub} <ID>")
                return
            status_map = {
                "doing": "in_progress",
                "active": "active",
                "done": "achieved",
                "void": "void",
            }
            new_status = status_map[sub]
            ok = update_entry(int(parts[1]), status=new_status)
            label = STATUS_LABELS[new_status]
            yield event.plain_result(f"#{parts[1]} 已标记为 [{label}]" if ok else f"找不到 #{parts[1]}")
            return

        if sub == "del":
            if len(parts) < 2 or not parts[1].isdigit():
                yield event.plain_result("用法：/ledger del <ID>")
                return
            with _conn() as c:
                cur = c.execute("DELETE FROM ledger WHERE id=?", (int(parts[1]),))
            yield event.plain_result(f"已删除 #{parts[1]}" if cur.rowcount else "找不到")
            return

        if sub == "search":
            if len(parts) < 2:
                yield event.plain_result("用法：/ledger search <关键词>")
                return
            rows = search_entries(umo, parts[1])
            if not rows:
                yield event.plain_result("没找到")
                return
            lines = []
            for r in rows:
                label = CATEGORY_LABELS.get(r["category"], r["category"])
                mark = _status_mark(r["status"])
                lines.append(f"#{r['id']} [{label}]{mark} {r['content']}")
            yield event.plain_result("\n".join(lines)[:3500])
            return

        if sub == "due":
            if len(parts) < 3 or not parts[1].isdigit():
                yield event.plain_result("用法：/ledger due <ID> <YYYY-MM-DD HH:MM>")
                return
            raw_time = parts[2].strip()
            parsed = None
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(raw_time, fmt).isoformat(timespec="seconds")
                    break
                except ValueError:
                    continue
            if not parsed:
                yield event.plain_result("时间格式不对，请用 YYYY-MM-DD HH:MM")
                return
            ok = update_entry(int(parts[1]), due_at=parsed)
            yield event.plain_result(f"已设置 #{parts[1]} 到期时间 {parsed[:16]}" if ok else "找不到")
            return

        yield event.plain_result("未知子命令，发 /ledger help 查看用法")