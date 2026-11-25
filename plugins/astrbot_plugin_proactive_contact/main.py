import asyncio
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openai import AsyncOpenAI

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.star.register import register_on_astrbot_loaded


DB_PATH = Path(__file__).parent / "proactive.db"
CHECK_INTERVAL = 30   # 秒


# ---------------- 数据库 ----------------

def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS timers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                umo TEXT NOT NULL,
                type TEXT NOT NULL,
                fire_at TEXT NOT NULL,
                event_context TEXT,
                reason TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                umo TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                umo TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_timers_due ON timers(status, fire_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_messages_umo ON messages(umo, id)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                umo TEXT NOT NULL,
                timer_type TEXT NOT NULL,
                action TEXT NOT NULL,
                text TEXT,
                thinking TEXT,
                reason TEXT,
                next_minutes INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_decisions_umo ON decisions(umo, id)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_context (
                umo TEXT NOT NULL,
                date TEXT NOT NULL,
                summary TEXT,
                last_compressed_at TEXT,
                last_msg_id INTEGER DEFAULT 0,
                PRIMARY KEY (umo, date)
            )
        """)


def create_timer(umo, type_, fire_at, event_context=None, reason=None):
    with _conn() as c:
        c.execute(
            "INSERT INTO timers (umo, type, fire_at, event_context, reason) VALUES (?, ?, ?, ?, ?)",
            (umo, type_, fire_at.isoformat(timespec="seconds"), event_context, reason),
        )


def get_due_timers(now):
    with _conn() as c:
        return c.execute(
            "SELECT * FROM timers WHERE status='pending' AND fire_at <= ? ORDER BY fire_at",
            (now.isoformat(timespec="seconds"),),
        ).fetchall()


def mark_timer(tid, status):
    with _conn() as c:
        c.execute("UPDATE timers SET status=? WHERE id=?", (status, tid))


def pending_oneshot_count(umo):
    with _conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM timers WHERE umo=? AND type='oneshot' AND status='pending'",
            (umo,),
        ).fetchone()[0]


def cancel_oneshots(umo, keyword=None):
    """取消某会话下的所有一次性 Timer；带 keyword 时只取消事件描述包含它的。"""
    with _conn() as c:
        if keyword:
            cur = c.execute(
                "DELETE FROM timers WHERE umo=? AND type='oneshot' AND status='pending' AND event_context LIKE ?",
                (umo, f"%{keyword}%"),
            )
        else:
            cur = c.execute(
                "DELETE FROM timers WHERE umo=? AND type='oneshot' AND status='pending'",
                (umo,),
            )
        return cur.rowcount


def record_message(umo, role, content):
    if not content:
        return
    now_local = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        c.execute(
            "INSERT INTO messages (umo, role, content, created_at) VALUES (?, ?, ?, ?)",
            (umo, role, content, now_local),
        )

def last_message_time(umo):
    """返回该会话最近一条消息的时间（本地时间），没有则 None。"""
    with _conn() as c:
        row = c.execute(
            "SELECT created_at FROM messages WHERE umo=? ORDER BY id DESC LIMIT 1",
            (umo,),
        ).fetchone()
    if not row or not row["created_at"]:
        return None
    try:
        s = row["created_at"]
        if "T" in s:
            return datetime.fromisoformat(s)
        else:
            dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc).astimezone()
            return dt.replace(tzinfo=None)
    except Exception:
        return None

def has_recent_activity(umo, minutes=5):
    """最近 N 分钟内有没有消息往来。"""
    dt = last_message_time(umo)
    if dt is None:
        return False
    return (datetime.now() - dt).total_seconds() < minutes * 60

def recent_messages(umo, limit):
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content, created_at FROM messages WHERE umo=? ORDER BY id DESC LIMIT ?",
            (umo, limit),
        ).fetchall()
    return list(reversed(rows))

def is_session_enabled(umo):
    with _conn() as c:
        row = c.execute("SELECT enabled FROM sessions WHERE umo=?", (umo,)).fetchone()
        return bool(row and row["enabled"])


def set_session(umo, enabled):
    with _conn() as c:
        c.execute(
            "INSERT INTO sessions (umo, enabled) VALUES (?, ?) "
            "ON CONFLICT(umo) DO UPDATE SET enabled=excluded.enabled",
            (umo, 1 if enabled else 0),
        )


def all_enabled_sessions():
    with _conn() as c:
        return [r["umo"] for r in c.execute("SELECT umo FROM sessions WHERE enabled=1").fetchall()]


def has_pending_chain(umo):
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM timers WHERE umo=? AND type='chain' AND status='pending' LIMIT 1",
            (umo,),
        ).fetchone()
        return bool(row)

def record_decision(umo, timer_type, action, text, thinking, reason, next_minutes):
    with _conn() as c:
        c.execute(
            "INSERT INTO decisions (umo, timer_type, action, text, thinking, reason, next_minutes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (umo, timer_type, action, text or "", thinking or "", reason or "", next_minutes),
        )


def recent_decisions(umo, limit=5):
    with _conn() as c:
        rows = c.execute(
            "SELECT action, reason, next_minutes, created_at FROM decisions "
            "WHERE umo=? ORDER BY id DESC LIMIT ?",
            (umo, limit),
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            dt = datetime.fromisoformat(d["created_at"]).replace(tzinfo=timezone.utc).astimezone()
            d["created_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
        result.append(d)
    return list(reversed(result))


# ---------------- 今日摘要（中期记忆层） ----------------

def get_daily_context(umo, date_str):
    """读某天的摘要，没有则返回 None。"""
    with _conn() as c:
        return c.execute(
            "SELECT summary, last_compressed_at, last_msg_id FROM daily_context "
            "WHERE umo=? AND date=?",
            (umo, date_str),
        ).fetchone()


def get_latest_context_before(umo, date_str):
    """读 date_str（含当天）之前最近的一条摘要，用来确定"已经压到哪里了"。"""
    with _conn() as c:
        return c.execute(
            "SELECT date, summary, last_compressed_at, last_msg_id FROM daily_context "
            "WHERE umo=? AND date<=? ORDER BY date DESC LIMIT 1",
            (umo, date_str),
        ).fetchone()


def upsert_daily_context(umo, date_str, summary, compressed_at, last_msg_id):
    """写入或更新某天的摘要。"""
    with _conn() as c:
        c.execute(
            "INSERT INTO daily_context (umo, date, summary, last_compressed_at, last_msg_id) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(umo, date) DO UPDATE SET "
            "summary=excluded.summary, "
            "last_compressed_at=excluded.last_compressed_at, "
            "last_msg_id=excluded.last_msg_id",
            (umo, date_str, summary, compressed_at, last_msg_id),
        )


def messages_since(umo, after_id, limit=300):
    """取 id 大于 after_id 的消息（按 id 正序）。

    以消息 id 为界而不是按自然日过滤，跨零点也不会漏掉没来得及压缩的对话，
    顺便绕开了 messages.created_at 两种历史格式（带 T 的本地时间 / 带空格的 UTC）的坑。
    after_id <= 0 时退化成"最近 limit 条"，避免首次运行时把全表拉进来。
    """
    with _conn() as c:
        if after_id and int(after_id) > 0:
            rows = c.execute(
                "SELECT id, role, content, created_at FROM messages "
                "WHERE umo=? AND id>? ORDER BY id ASC LIMIT ?",
                (umo, int(after_id), limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, role, content, created_at FROM messages "
                "WHERE umo=? ORDER BY id DESC LIMIT ?",
                (umo, limit),
            ).fetchall()
            rows = list(reversed(rows))
    return rows


# ---------------- 对话文本提取 ----------------

# AstrBot 会把"当前时间"塞在每条 user 消息的 content 里，形如：
#   <system_reminder>Current datetime: 2026-10-15 22:00 (CST), Weekday: Thursday</system_reminder>
_DATETIME_RE = re.compile(r"Current datetime:\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")

_REMINDER_PREFIX = "<system_reminder>"
MAX_THINK_CHARS = 500   # 单条"内心"的上限
MAX_BODY_CHARS = 600    # 单条消息正文的上限


def _content_parts(content):
    """把 AstrBot 的结构化 content 拆成 (内心, 正文)。

    content 有两种形态：
      - 纯字符串（插件自己写进去的，或 tool 的返回结果）
      - 一组片段：[{"type": "text", "text": ...}, {"type": "think", "think": ...}]
    <system_reminder> 是框架塞进来的时间提示，不算对话内容。
    """
    if isinstance(content, str):
        return "", content.strip()[:MAX_BODY_CHARS]
    if not isinstance(content, list):
        return "", ""

    texts, thinks = [], []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            t = (part.get("text") or "").strip()
            if t and not t.startswith(_REMINDER_PREFIX):
                texts.append(t)
        elif ptype == "think":
            t = (part.get("think") or "").strip()
            if t:
                thinks.append(t)

    think_text = "\n".join(thinks).strip()
    if len(think_text) > MAX_THINK_CHARS:
        think_text = think_text[:MAX_THINK_CHARS] + "…"

    body = "\n".join(texts).strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "…"
    return think_text, body


def _content_datetime(content):
    """从 content 的 system_reminder 里抠出这条消息发生的时间。

    返回本地时间的 ISO 形式（带 T），好让 _fmt_ts 走"本地时间"那条分支。
    """
    if isinstance(content, str):
        candidates = [content]
    elif isinstance(content, list):
        candidates = [
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
    else:
        return ""
    for text in candidates:
        m = _DATETIME_RE.search(text or "")
        if m:
            return f"{m.group(1)}T{m.group(2)}:00"
    return ""


# ---------------- 插件主体 ----------------
_plugin_instance = None


@register_on_astrbot_loaded()
async def _on_astrbot_loaded(*args, **kwargs):
    """AstrBot 加载完成时自动启动后台循环。"""
    if _plugin_instance is not None:
        _plugin_instance._ensure_task()
        logger.info("ProactiveContact: AstrBot 加载完成，后台循环已自动启动")


@register("astrbot_plugin_proactive_contact", "Mephisto", "主动联系", "1.0.0")
class ProactiveContact(Star):

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        init_db()
        self.config = config
        self._client = None
        self._task = None
        self._stop = False
        # 内存态的"最近活动"表：umo -> datetime。
        # 比查库及时——写库要等整轮回复结束（on_llm_response），而用户在
        # on_llm_request 那一刻就该被记下来。
        self._last_activity = {}
        self.model = config.get("deepseek_model", "deepseek-chat")
        self.history_window = int(config.get("history_window", 20))
        self.chain_default = int(config.get("chain_default_interval_minutes", 120))
        self.chain_min = int(config.get("chain_min_interval_minutes", 15))
        self.chain_max = int(config.get("chain_max_interval_minutes", 720))
        self.quiet_start = int(config.get("quiet_hours_start", 23))
        self.quiet_end = int(config.get("quiet_hours_end", 7))
        global _plugin_instance
        _plugin_instance = self        
        logger.info("ProactiveContact v1.0.2  已加载")

    # ---------- 生命周期 ----------

    def _ensure_task(self):
        if self._task and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()      # 只在这里能成功；__init__ 时没有循环，会抛异常
            self._task = loop.create_task(self._loop())
            logger.info("ProactiveContact 后台循环已启动")
        except RuntimeError:
            # 不在事件循环里（比如插件加载时），交给首次 on_response 再启动
            return

    async def _loop(self):
        # 启动后先等 60 秒，给平台适配器留连接时间
        await asyncio.sleep(60)
        while not self._stop:
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"ProactiveContact tick 异常: {e}")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _tick(self):
        now = datetime.now()
        self._dedupe_chains()
        # 自动为已启用但没有链式 Timer 的会话创建第一条
        for umo in all_enabled_sessions():
            if not has_pending_chain(umo):
                fire_at = now + timedelta(minutes=self.chain_default)
                create_timer(umo, "chain", fire_at, reason="自动补种")
                logger.info(f"链式 Timer 初始化: {umo} -> {fire_at}")

        due = get_due_timers(now)
        for t in due:
            try:
                await self._handle_timer(t)
            except Exception as e:
                logger.error(f"处理 Timer {t['id']} 失败: {e}")
                mark_timer(t["id"], "failed")

    def _dedupe_chains(self):
        """每个会话只保留最早的一条 pending 链式 Timer，多余的删掉。"""
        with _conn() as c:
            rows = c.execute("""
                SELECT umo, MIN(fire_at) AS keep, COUNT(*) AS n
                FROM timers
                WHERE type='chain' AND status='pending'
                GROUP BY umo
                HAVING n > 1
            """).fetchall()
            for r in rows:
                c.execute(
                    "DELETE FROM timers WHERE umo=? AND type='chain' "
                    "AND status='pending' AND fire_at != ?",
                    (r["umo"], r["keep"]),
                )
                logger.info(f"清理重复链式 Timer: {r['umo']} 保留 {r['keep']}")
    # ---------- 唤醒 ----------

    def _get_client(self):
        if self._client is not None:
            return self._client
        api_key = (self.config.get("deepseek_api_key") or "").strip()
        if not api_key:
            return None
        try:
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=self.config.get("deepseek_base_url", "https://api.deepseek.com/v1"),
            )
        except Exception as e:
            logger.error(f"创建 openai 客户端失败: {e}")
            return None
        return self._client

    def _in_quiet_hours(self, dt):
        h = dt.hour
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= h < self.quiet_end
        return h >= self.quiet_start or h < self.quiet_end

    def _build_prompt(self, timer, messages, last_msg_time=None):
        now = datetime.now()
        weekday_cn = ["一", "二", "三", "四", "五", "六", "日"][now.weekday()]
        now_str = f"{now.strftime('%Y-%m-%d %H:%M')} 星期{weekday_cn}"

        # 距上次对话多久
        if last_msg_time:
            delta = now - last_msg_time
            total_min = int(delta.total_seconds() // 60)
            if total_min < 1:
                gap_str = "刚刚"
            elif total_min < 60:
                gap_str = f"{total_min} 分钟前"
            elif total_min < 1440:
                gap_str = f"{total_min // 60} 小时 {total_min % 60} 分钟前"
            else:
                days = total_min // 1440
                hours = (total_min % 1440) // 60
                gap_str = f"{days} 天 {hours} 小时前"
            time_context = (
                f"【当前时间】{now_str}\n"
                f"【上次和用户说话】{last_msg_time.strftime('%Y-%m-%d %H:%M')}（{gap_str}）"
            )
        else:
            time_context = f"【当前时间】{now_str}\n【上次和用户说话】（没有记录）"

        # 每条消息带时间戳，避免她误以为昨晚说的话是"刚刚说的"
        def _fmt_ts(s):
            try:
                if "T" in s:
                    dt = datetime.fromisoformat(s)
                    return dt.strftime("%m-%d %H:%M")
                else:
                    dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc).astimezone()
                    return dt.strftime("%m-%d %H:%M")
            except Exception:
                return "??"

        lines = []
        for m in messages:
            ts = _fmt_ts(m["created_at"]) if "created_at" in m.keys() else "??"
            lines.append(f"[{ts}] {m['role']}: {m['content']}")
        history = "\n".join(lines) or "（无近期对话）"

        if timer["type"] == "chain":
            situation = (
                "这是一次链式主动关心。你被唤醒，重新判断当前是否适合联系用户，"
                "并自由决定下一次何时再被唤醒。"
            )
            next_field = '"next_fire_in_minutes": 数字（必填，单位分钟）,'
        else:
            situation = f"这是一次一次性回访。当时的事件是：{timer['event_context']}。请先看最近对话里这个事件是否已经有结果。"
            next_field = '"next_fire_in_minutes": 0（一次性 Timer 固定为 0）,'

        quiet = self._in_quiet_hours(now)
        quiet_hint = "（当前处于免打扰时段，除非有特别明确的理由，否则应倾向 no_reply）" if quiet else "（当前不在免打扰时段）"
        # 历史决策：只留结论，去掉 thinking，并且降权
        history_decisions = recent_decisions(timer["umo"], limit=5)
        if history_decisions:
            lines = []
            for d in history_decisions:
                raw_ts = d["created_at"] or ""
                try:
                    if "T" in raw_ts:
                        dt = datetime.fromisoformat(raw_ts)
                    else:
                        dt = datetime.fromisoformat(raw_ts).replace(tzinfo=timezone.utc).astimezone()
                    ts = dt.strftime("%m-%d %H:%M")
                except Exception:
                    ts = raw_ts[11:16] if len(raw_ts) > 16 else raw_ts
                tag = "发了" if d["action"] == "reply" else "没发"
                reason_short = (d["reason"] or "")[:60]
                lines.append(f"[{ts}] {tag}｜{reason_short}")
            past = "\n".join(lines)
            decision_history_block = (
                "过去的判断（**那只是你当时判断的时刻，不是你和用户说话的时刻**，"
                "仅供参考，每次都要重新看现在的情况，不要照搬）：\n"
                f"{past}\n\n"
            )
        else:
            decision_history_block = ""
        
        # 今日摘要（中期记忆层）：补上"今天聊过什么"，避免她一滑出短期窗口就忘事
        daily_block = ""
        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            ctx = get_daily_context(timer["umo"], today_str)
            if ctx and ctx["summary"]:
                daily_block = f"【今天（{today_str}）发生的事】\n{ctx['summary']}\n\n"
        except Exception as e:
            logger.warning(f"[今日摘要] 读取失败: {e}")

        # 长期记忆（memory 插件）与账本（Ledger 插件）：唤醒判断也该知道这些
        memory_block = ""
        try:
            memory_db = Path(__file__).parent.parent / "memory" / "memory.db"
            if memory_db.exists():
                with sqlite3.connect(f"file:{memory_db.as_posix()}?mode=ro", uri=True) as mc:
                    rows = mc.execute(
                        "SELECT content FROM memories ORDER BY id DESC LIMIT 30"
                    ).fetchall()
                lines = [r[0] for r in rows if r[0]]
                if lines:
                    memory_block = (
                        "【你记得的关于用户的事】\n"
                        + "\n".join(f"- {x}" for x in lines)
                        + "\n\n"
                    )
        except Exception as e:
            logger.warning(f"[唤醒] 读记忆库失败: {e}")

        ledger_block = ""
        try:
            ledger_db = Path(__file__).parent.parent / "Ledger" / "ledger.db"
            if ledger_db.exists():
                with sqlite3.connect(f"file:{ledger_db.as_posix()}?mode=ro", uri=True) as lc:
                    rows = lc.execute(
                        "SELECT category, content FROM ledger "
                        "WHERE umo=? AND status IN ('in_progress','active') "
                        "ORDER BY id DESC LIMIT 15",
                        (timer["umo"],),
                    ).fetchall()
                label = {"promise": "约定", "wish": "愿望", "thought": "心事"}
                lines = [f"- [{label.get(c, c)}] {t}" for c, t in rows if t]
                if lines:
                    ledger_block = "【你的账本】\n" + "\n".join(lines) + "\n\n"
        except Exception as e:
            logger.warning(f"[唤醒] 读账本失败: {e}")

        logger.info(f"=== 时间上下文 ===\n{time_context}")
        logger.info(f"=== 今日摘要 ===\n{daily_block.strip() or '（空）'}")
        logger.info(f"=== 长期记忆 ===\n{memory_block.strip()[:700] or '（空）'}")
        logger.info(f"=== 账本 ===\n{ledger_block.strip() or '（空）'}")
        logger.info(f"=== 最近对话 ===\n{history[:800]}")

        return f"""{time_context}{quiet_hint}

{situation}

{daily_block}{memory_block}{ledger_block}【第一步：先看时间】
现在是 {now_str}，距离上次和用户说话已经过了 {gap_str if last_msg_time else '未知'}。
先判断时间跨度：
- 1~3 小时：可以轻轻接一句
- 超过半天：正是可以主动的时候
- 深夜（免打扰时段）：除非有明确理由，倾向 no_reply

【第二步：看最近的对话】
最近与用户的对话（按时间顺序，最新在后）：
{history}

{decision_history_block}【第三步：做判断】
综合上面三点，判断现在是否适合主动联系。

默认倾向是 reply（如果距离上次聊天有段时间、或最近有值得聊的话题）。
只有下列情况才选 no_reply：
- 用户明确说过"别打扰""在忙""晚点说"
- 深夜且没有明确理由
- 一次性回访的事件在最近对话里已经出现结果

以下**不是** no_reply 的理由：
- 用户说"去吃饭了""去洗澡"这类临时状态
- 上一轮是"好好好""喵喵喵"这类轻松收尾
- 距离上次聊天几小时没互动——这正是该主动的时候

说话风格：像恋人随口提一句，1~3 句话。自然、不端着、不客套。
一次只说一件事，不追问、不重复上次的话题、不复述用户刚说过的话。

【关于你自己的状态】
- 不要编造你此刻正在做的事、刚忙完的事、去过哪里、经历了什么。
- 你可以表达情绪、想法、想用户，但不要编具体事件
  （比如"刚和同学吃完饭""在图书馆写作业""学校今天发生了某事"）。
- 主动联系时如果没什么具体的事要说，就只说想他、问他近况，
  或者提一提你们之间的约定和共同记忆。
- 宁可简单，不要编。

即使不发，链式 Timer 也要给出下次重新判断的时间。

只输出 JSON，不要任何其他内容：
{{
  "action": "reply" | "no_reply",
  "text": "要说的话（action=reply 时必填；no_reply 时留空字符串）",
  "thinking": "你此刻的内心活动（第一人称，20~100 字，像自言自语，可以犹豫、可以有情绪）",
  {next_field}
  "reason": "一句话说明判断理由，方便排查"
}}
"""

    def _note_activity(self, umo):
        """记下该会话最近一次有来有往的时刻（内存态）。"""
        if umo:
            self._last_activity[umo] = datetime.now()

    def _has_recent_activity(self, umo, minutes=5):
        """最近 N 分钟内有没有对话。

        内存态与 messages 表取较新的那个：内存态在用户开口那一刻
        （on_llm_request）就更新，而写库要等整轮回复结束，所以两个都看才可靠。
        """
        candidates = []
        mem = self._last_activity.get(umo)
        if mem is not None:
            candidates.append(mem)
        db_time = last_message_time(umo)
        if db_time is not None:
            candidates.append(db_time)
        if not candidates:
            return False
        return (datetime.now() - max(candidates)).total_seconds() < minutes * 60

    async def _compress_today(self, umo):
        """把"上次压缩之后的新消息"并进今日摘要，覆盖写回。

        - 只把增量喂给模型（配合 last_msg_id），省 token
        - 以消息 id 为界，所以跨零点也不会漏掉"压缩前一刻"的对话
        - 没有新消息时直接返回，不调模型
        """
        try:
            client = self._get_client()
            if client is None:
                return

            today_str = datetime.now().strftime("%Y-%m-%d")
            today_ctx = get_daily_context(umo, today_str)
            anchor = today_ctx or get_latest_context_before(umo, today_str)
            old_summary = ((today_ctx["summary"] if today_ctx else "") or "").strip()
            last_id = int(anchor["last_msg_id"] or 0) if anchor else 0

            # 首次没有锚点，只取最近一小段，免得把好几天的对话都当成"今天"
            first_run = last_id <= 0
            new_msgs = messages_since(umo, last_id, limit=60 if first_run else 300)
            if not new_msgs:
                logger.info("[今日摘要] 没有新消息，跳过")
                return

            lines = []
            for m in new_msgs:
                role = "用户" if m["role"] == "user" else "我"
                lines.append(f"{role}：{m['content']}")
            conversation = "\n".join(lines)

            if old_summary:
                body = (
                    "你在续写今天的日记。下面是已经写好的部分，"
                    "请把新发生的对话并进去，输出更新后的完整日记。\n\n"
                    f"【已经写好的日记】\n{old_summary}\n\n"
                    f"【刚刚新发生的对话】\n{conversation}\n\n"
                )
            else:
                body = f"【今天的对话】\n{conversation}\n\n"

            prompt = (
                "你在整理今天的对话记录，写成一段简短的日记式摘要。\n\n"
                "规则：\n"
                    '- 用"我"的第一人称视角写\n'
                "- 只保留重要的事：聊了什么话题、有什么约定、发生了什么情绪转折\n"
                "- 压缩到 200 字以内\n"
                "- 不要逐条复述对话，要概括\n"
                "- 如果是连续日常闲聊，就简短概括，不用细写\n"
                "- 格式：几段短句，每句一行，不要编号\n\n"
                + body
                + "摘要："
            )

            r = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                timeout=60,
            )
            usage = getattr(r, "usage", None)
            if usage:
                logger.info(
                    f"[今日摘要] 输入={usage.prompt_tokens} "
                    f"输出={usage.completion_tokens} 总计={usage.total_tokens}"
                )
            summary = (r.choices[0].message.content or "").strip()
            if not summary:
                return

            now_str = datetime.now().isoformat(timespec="seconds")
            upsert_daily_context(umo, today_str, summary, now_str, new_msgs[-1]["id"])
            logger.info(
                f"[今日摘要] 已更新，新增 {len(new_msgs)} 条消息 → {len(summary)} 字"
            )
        except Exception as e:
            logger.warning(f"[今日摘要] 压缩失败: {e}")

    async def _handle_timer(self, timer):
        umo = timer["umo"]
        # 最近 5 分钟有对话 → 跳过本次主动判断
        if self._has_recent_activity(umo, minutes=5):
            logger.info(f"Timer {timer['id']} 最近 5 分钟有对话，跳过本次主动联系")
            mark_timer(timer["id"], "fired")
            if timer["type"] == "chain":
                next_at = datetime.now() + timedelta(minutes=self.chain_default)
                create_timer(umo, "chain", next_at, reason="活跃对话跳过")
                logger.info(f"链式续期: {umo} -> {next_at} (活跃对话跳过)")
            return
        client = self._get_client()
        if client is None:
            logger.warning("未配置 deepseek_api_key，跳过唤醒")
            mark_timer(timer["id"], "failed")
            return

        # 今日摘要：距上次压缩满 1 小时才考虑；到底有没有新消息由 _compress_today 判断
        try:
            today_str = datetime.now().strftime("%Y-%m-%d")
            ctx = get_daily_context(umo, today_str)
            need_compress = True
            if ctx and ctx["last_compressed_at"]:
                try:
                    last = datetime.fromisoformat(ctx["last_compressed_at"])
                    if (datetime.now() - last).total_seconds() < 3600:
                        need_compress = False
                except Exception:
                    pass
            if need_compress:
                await self._compress_today(umo)
        except Exception as e:
            logger.warning(f"[今日摘要] 触发检查失败: {e}")

        messages = await self._get_recent_history(umo, self.history_window)
        last_msg_time = last_message_time(umo)
        prompt = self._build_prompt(timer, messages, last_msg_time)

        try:
            r = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.85,
                response_format={"type": "json_object"},
            )
            usage = getattr(r, "usage", None)
            if usage:
                logger.info(
                    f"[主动判断] 输入={usage.prompt_tokens} "
                    f"输出={usage.completion_tokens} 总计={usage.total_tokens}"
                )
            raw = (r.choices[0].message.content or "").strip()
            decision = json.loads(raw)
        except Exception as e:
            logger.warning(f"唤醒调用失败: {e}")
            mark_timer(timer["id"], "failed")
            # 失败也要为链式续期，避免断链
            if timer["type"] == "chain":
                create_timer(umo, "chain",
                             datetime.now() + timedelta(minutes=self.chain_default),
                             reason="唤醒失败回退")
            return

        action = (decision.get("action") or "no_reply").lower()
        text = (decision.get("text") or "").strip()
        reason = decision.get("reason", "")
        logger.info(f"Timer {timer['id']} 决策: {action} | {reason}")

        # 二次检查：从这里往上，中间隔着"压缩摘要 + 判断"两次大模型调用
        # （实测可以到 40 秒以上）。用户完全可能在这段时间里开口了，
        # 这时候再把消息发出去，就会插进正在进行的对话里。
        if action == "reply" and text and self._has_recent_activity(umo, minutes=5):
            logger.warning(f"Timer {timer['id']} 判断期间用户又开始聊了，放弃本次发送")
            action = "no_reply"
            text = ""
            reason = f"[判断期间用户又开始聊了，已放弃发送] 原判断：{reason}"

        next_minutes_for_log = decision.get("next_fire_in_minutes")
        try:
            next_minutes_for_log = int(next_minutes_for_log) if next_minutes_for_log else None
        except Exception:
            next_minutes_for_log = None
        thinking = (decision.get("thinking") or "").strip()
        record_decision(umo, timer["type"], action, text, thinking, reason, next_minutes_for_log)

        send_failed = False
        if action == "reply" and text:
            ok = await self._send_message(umo, text)
            if ok:
                record_message(umo, "assistant", text)
                await self._append_to_history(umo, text)
            else:
                send_failed = True

        if send_failed:
            # 发送失败：不标记 fired，改成 5 分钟后再试一次
            logger.warning(f"Timer {timer['id']} 发送失败，5 分钟后重试")
            mark_timer(timer["id"], "failed")
            create_timer(umo, timer["type"], datetime.now() + timedelta(minutes=5),
                         event_context=timer["event_context"] if "event_context" in timer.keys() else None,
                         reason="发送失败重试")
        else:
            mark_timer(timer["id"], "fired")

        # 链式续期
        if timer["type"] == "chain":
            minutes = decision.get("next_fire_in_minutes") or self.chain_default
            try:
                minutes = int(minutes)
            except Exception:
                minutes = self.chain_default
            minutes = max(self.chain_min, min(minutes, self.chain_max))
            next_at = datetime.now() + timedelta(minutes=minutes)
            create_timer(umo, "chain", next_at, reason=f"链式续期 {minutes}min")
            logger.info(f"链式续期: {umo} -> {next_at} ({minutes}min)")

    async def _send_message(self, umo, text):
        """主动发送一条纯文本消息到指定会话。"""
        try:
            from astrbot.core.platform.message_session import MessageSession
            from astrbot.core.message.message_event_result import MessageChain
            from astrbot.core.message.components import Plain
        except ImportError as e:
            logger.error(f"导入发送相关模块失败: {e}")
            return False

        try:
            session = MessageSession.from_str(umo)
        except Exception as e:
            logger.error(f"解析 session 失败: {umo} -> {e}")
            return False

        chain = MessageChain(chain=[Plain(text=text)])
        try:
            sent = await self.context.send_message(session, chain)
            logger.info(f"主动发送 {'成功' if sent else '失败'}: {umo}")
            return bool(sent)
        except Exception as e:
            logger.error(f"主动发送失败: {e}")
            return False


    async def _append_to_history(self, umo, text):
        """把主动发送的消息写入 AstrBot 的对话历史。
        否则她下次回复时看不到自己刚才主动说过什么。"""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                logger.warning(f"[主动消息] 找不到会话 {umo} 的对话 ID")
                return
            conv = await cm.get_conversation(umo, cid)
            if not conv:
                logger.warning(f"[主动消息] 找不到对话 {cid}")
                return

            # conv.history 是 JSON 字符串，转成 list
            history = json.loads(conv.history or "[]")
            # 追加她的这条主动消息
            history.append({"role": "assistant", "content": text})

            # 防止无限增长，保留最近 100 条
            if len(history) > 100:
                history = history[-100:]

            # update_conversation 接收 list，不是字符串
            await cm.update_conversation(umo, cid, history=history)
            logger.info(f"[主动消息] 已写入对话历史: {text[:30]}...")
        except Exception as e:
            logger.warning(f"[主动消息] 写入历史失败: {e}")
    
    # ---------- 消息记录 ----------

    @filter.on_llm_response()
    async def on_response(self, event: AstrMessageEvent, resp):
        self._ensure_task()
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return
        self._note_activity(umo)
        user_msg = (event.message_str or "").strip()
        reply = (getattr(resp, "completion_text", "") or "").strip()
        if user_msg:
            record_message(umo, "user", user_msg)
        if reply:
            record_message(umo, "assistant", reply)

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="proactive_schedule_oneshot")
    async def tool_schedule_oneshot(self, event: AstrMessageEvent,
                                    minutes: int, event_description: str):
        """安排一次一次性回访：在指定分钟后唤醒你，回访某件具体事件。

        Args:
            minutes (int): 多少分钟后触发，范围 5-1440
            event_description (str): 具体事件描述，例如"用户去开会"、"用户准备测试新功能"
        """
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return "无法获取会话标识"
        try:
            minutes = int(minutes)
        except Exception:
            return "minutes 必须是整数"
        minutes = max(5, min(minutes, 1440))
        fire_at = datetime.now() + timedelta(minutes=minutes)
        create_timer(umo, "oneshot", fire_at, event_context=event_description,
                     reason="LLM 工具创建")
        return f"已安排 {minutes} 分钟后回访：{event_description}"

    @filter.llm_tool(name="proactive_cancel_oneshot")
    async def tool_cancel_oneshot(self, event: AstrMessageEvent, keyword: str = ""):
        """取消尚未触发的一次性回访。用户提前告知了结果，或明确不需要再回访时调用。

        Args:
            keyword (str): 事件关键词，匹配事件描述；留空则取消全部未触发的一次性回访
        """
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return "无法获取会话标识"
        n = cancel_oneshots(umo, keyword or None)
        return f"已取消 {n} 条一次性回访"

    @filter.llm_tool(name="proactive_adjust_chain")
    async def tool_adjust_chain(self, event: AstrMessageEvent, minutes: int):
        """调整下一次链式主动关心的触发时间。用户说"晚点再聊""别太频繁"等时可用。

        Args:
            minutes (int): 多少分钟后再重新判断，范围 15-1440
        """
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return "无法获取会话标识"
        try:
            minutes = int(minutes)
        except Exception:
            return "minutes 必须是整数"
        minutes = max(self.chain_min, min(minutes, self.chain_max))
        # 取消现有链式，重新种一条
        with _conn() as c:
            c.execute("DELETE FROM timers WHERE umo=? AND type='chain' AND status='pending'", (umo,))
        create_timer(umo, "chain", datetime.now() + timedelta(minutes=minutes),
                     reason="LLM 调整链式")
        return f"下一次链式判断已调整为 {minutes} 分钟后"


    @filter.llm_tool(name="recall_my_decisions")
    async def tool_recall_decisions(self, event: AstrMessageEvent, limit: int = 10):
        """查询你最近做过的主动联系判断记录。

        当你想知道"我最近都做过什么判断"、"上次为什么没找用户"，
        或者用户问你"你之前是不是想过要联系我"时调用。

        Args:
            limit (int): 返回最近多少条，默认 10，范围 1-30
        """
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return "无法获取会话标识"
        try:
            limit = max(1, min(int(limit), 30))
        except Exception:
            limit = 10

        with _conn() as c:
            rows = c.execute(
                "SELECT action, text, reason, next_minutes, created_at "
                "FROM decisions WHERE umo=? ORDER BY id DESC LIMIT ?",
                (umo, limit),
            ).fetchall()

        if not rows:
            return "你还没有主动判断记录"

        lines = []
        for r in rows:
            ts = (r["created_at"] or "")[:16]
            if r["action"] == "reply":
                lines.append(f"[{ts}] 主动发了：「{r['text']}」｜理由：{r['reason']}")
            else:
                nxt = r["next_minutes"]
                nxt_str = f"，{nxt} 分钟后再判断" if nxt else ""
                lines.append(f"[{ts}] 没发{nxt_str}｜理由：{r['reason']}")
        return "你的主动判断记录（最新在上）：\n" + "\n".join(lines)


    @filter.on_llm_request()
    async def inject_tool_hint(self, event: AstrMessageEvent, req):
        """在 system prompt 里轻量提示：你有查自己决策的工具"""
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return
        # 用户开口的那一刻就记下活动时间，供链式判断做"正在聊天就跳过"
        self._note_activity(umo)
        # 只在有过决策历史的会话注入
        with _conn() as c:
            row = c.execute(
                "SELECT 1 FROM decisions WHERE umo=? LIMIT 1", (umo,)
            ).fetchone()
        if not row:
            return
        hint = (
            "你有一个工具 recall_my_decisions，可以查你过去主动联系用户的判断记录"
            "（什么时候发了、什么时候决定不发、理由是什么）。"
            "当用户问'你最近想过联系我吗''你之前做过什么判断'，"
            "或你自己想确认上次为什么没联系时，调用它。"
        )
        req.system_prompt = (req.system_prompt or "") + "\n\n" + hint

    # ---------- 命令 ----------

    @filter.command("proactive")
    async def cmd_proactive(self, event: AstrMessageEvent):
        self._ensure_task()
        raw = (event.message_str or "").strip()
        for p in ("/proactive", "／proactive", "proactive"):
            if raw.startswith(p):
                raw = raw[len(p):].strip()
                break
        parts = raw.split(maxsplit=2)
        sub = parts[0].lower() if parts else ""

        umo = getattr(event, "unified_msg_origin", "") or ""

        if sub in ("", "help"):
            yield event.plain_result(
                "/proactive on          在当前会话启用\n"
                "/proactive off         停用\n"
                "/proactive status      查看状态\n"
                "/proactive fire_now    立即触发一次（测试用）\n"
                "/proactive oneshot <分钟> <事件>  手动安排一次性回访\n"
                "/proactive cancel      取消所有未触发的一次性回访\n"
                "/proactive chain <分钟>  手动调整下次链式触发"
            )
            return

        if sub == "on":
            set_session(umo, True)
            if not has_pending_chain(umo):
                create_timer(umo, "chain",
                             datetime.now() + timedelta(minutes=self.chain_default),
                             reason="手动启用")
            yield event.plain_result(
                f"已启用。首次判断将在 {self.chain_default} 分钟后。"
            )
            return

        if sub == "off":
            set_session(umo, False)
            with _conn() as c:
                c.execute("DELETE FROM timers WHERE umo=? AND status='pending'", (umo,))
            yield event.plain_result("已停用，所有待触发 Timer 已清除")
            return

        if sub == "status":
            enabled = is_session_enabled(umo)
            with _conn() as c:
                rows = c.execute(
                    "SELECT id, type, fire_at, event_context, status FROM timers "
                    "WHERE umo=? AND status='pending' ORDER BY fire_at", (umo,)
                ).fetchall()
            lines = [f"启用状态: {'开' if enabled else '关'}"]
            if not rows:
                lines.append("当前无待触发 Timer")
            else:
                for r in rows:
                    desc = f" [{r['event_context']}]" if r["event_context"] else ""
                    lines.append(f"#{r['id']} {r['type']} @ {r['fire_at']}{desc}")
            yield event.plain_result("\n".join(lines))
            return

        if sub == "fire_now":
            with _conn() as c:
                c.execute(
                    "INSERT INTO timers (umo, type, fire_at, reason) VALUES (?, 'chain', ?, ?)",
                    (umo, datetime.now().isoformat(timespec="seconds"), "手动测试"),
                )
            yield event.plain_result("已插入，下次 tick（30 秒内）会触发")
            return

        if sub == "oneshot":
            if len(parts) < 3 or not parts[1].isdigit():
                yield event.plain_result("用法：/proactive oneshot <分钟> <事件描述>")
                return
            minutes = max(1, int(parts[1]))
            desc = parts[2]
            create_timer(umo, "oneshot",
                         datetime.now() + timedelta(minutes=minutes),
                         event_context=desc, reason="手动创建")
            yield event.plain_result(f"已安排 {minutes} 分钟后回访：{desc}")
            return

        if sub == "cancel":
            n = cancel_oneshots(umo)
            yield event.plain_result(f"已取消 {n} 条一次性回访")
            return
        
        if sub == "test_send":
            if len(parts) < 2:
                yield event.plain_result("用法：/proactive test_send 要发送的文本")
                return
            text = " ".join(parts[1:])
            ok = await self._send_message(umo, text)
            yield event.plain_result(f"发送{'成功' if ok else '失败'}，看日志")
            return
        
        if sub == "chain":
            if len(parts) < 2 or not parts[1].isdigit():
                yield event.plain_result("用法：/proactive chain <分钟>")
                return
            minutes = max(self.chain_min, min(int(parts[1]), self.chain_max))
            with _conn() as c:
                c.execute("DELETE FROM timers WHERE umo=? AND type='chain' AND status='pending'", (umo,))
            create_timer(umo, "chain", datetime.now() + timedelta(minutes=minutes),
                         reason="手动调整链式")
            yield event.plain_result(f"下次链式触发已设为 {minutes} 分钟后")
            return

        yield event.plain_result("未知子命令，发 /proactive help 查看用法")
        
    async def _get_recent_history(self, umo, limit=30):
        """优先读 AstrBot 对话历史（有压缩），失败时回退 messages 表。"""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if cid:
                conv = await cm.get_conversation(umo, cid)
                if conv and conv.history:
                    history = json.loads(conv.history)
                    if history:
                        result = self._convert_to_simple(history[-limit:])
                        if len(result) >= 3:
                            logger.info(f"[唤醒] 读 AstrBot 对话历史 {len(result)} 条")
                            return result
                        logger.info(
                            f"[唤醒] AstrBot 对话历史只解析出 {len(result)} 条，改用 messages 表"
                        )
        except Exception as e:
            logger.warning(f"[唤醒] 读对话历史失败，回退 messages 表: {e}")

        # 兜底：messages 表
        logger.info("[唤醒] 使用 messages 表兜底")
        return recent_messages(umo, self.history_window)

    def _convert_to_simple(self, history):
        """把 AstrBot 的对话历史转成简单结构。

        AstrBot 的消息 content 是结构化的（text / think 片段），不能只认字符串。
        system 角色留着（万一框架以后往里写摘要）；tool 也留着——那是她的"操作回执"，
        比如"已安排 57 分钟后回访"，这种信息别处没有。
        时间戳从 user 消息的 system_reminder 里抠，同一轮里的回复与工具结果沿用该时间。
        """
        result = []
        last_ts = ""
        for m in history:
            role = m.get("role", "")
            if role not in ("user", "assistant", "system", "tool"):
                continue
            content = m.get("content")

            ts = _content_datetime(content) or last_ts
            if ts:
                last_ts = ts

            think_text, body = _content_parts(content)
            if think_text:
                result.append(
                    {"role": role, "content": f"（心里）{think_text}", "created_at": ts}
                )
            if body:
                result.append({"role": role, "content": body, "created_at": ts})
        return result
