import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from difflib import SequenceMatcher

from openai import AsyncOpenAI

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


DB_PATH = Path(__file__).parent / "memory.db"
DEDUP_THRESHOLD = 0.85   # 相似度高于这个值视为重复


# ---------- 数据库操作 ----------

def _conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # v2：区分「临时事件」和「永久事实」，并给事件加发生日期
        cols = {r[1] for r in c.execute("PRAGMA table_info(memories)")}
        if "kind" not in cols:
            c.execute("ALTER TABLE memories ADD COLUMN kind TEXT DEFAULT 'fact'")
        if "event_date" not in cols:
            c.execute("ALTER TABLE memories ADD COLUMN event_date TEXT")
        if "status" not in cols:
            c.execute("ALTER TABLE memories ADD COLUMN status TEXT DEFAULT 'active'")
        if "last_used_at" not in cols:
            c.execute("ALTER TABLE memories ADD COLUMN last_used_at TIMESTAMP")


def all_memories():
    with _conn() as c:
        return c.execute("SELECT id, content FROM memories ORDER BY id").fetchall()


def is_duplicate(text: str) -> bool:
    for _, existing in all_memories():
        if SequenceMatcher(None, text, existing).ratio() >= DEDUP_THRESHOLD:
            return True
    return False


def add_memory(text: str, kind: str = "fact", event_date: str | None = None) -> bool:
    """返回 True 表示写入成功，False 表示重复跳过。

    kind: 'fact' 永久事实 / 'event' 临时事件
    event_date: 事件发生日期（YYYY-MM-DD），event 才有
    """
    if is_duplicate(text):
        return False
    with _conn() as c:
        c.execute(
            "INSERT INTO memories (content, kind, event_date) VALUES (?, ?, ?)",
            (text, kind, event_date),
        )
    return True


def archive_old_events(days: int = 7) -> int:
    """超过 N 天的临时事件标成 archived：不删除，只是不再注入。"""
    with _conn() as c:
        cur = c.execute(
            "UPDATE memories SET status = 'archived' "
            "WHERE kind = 'event' AND status = 'active' "
            "AND created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        return cur.rowcount


def recent_facts(limit: int = 20):
    with _conn() as c:
        return c.execute(
            "SELECT id, content FROM memories "
            "WHERE (kind IS NULL OR kind = 'fact') AND (status IS NULL OR status = 'active') "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def recent_events(days: int = 3, limit: int = 10):
    with _conn() as c:
        return c.execute(
            "SELECT id, content, event_date FROM memories "
            "WHERE kind = 'event' AND (status IS NULL OR status = 'active') "
            "AND created_at >= datetime('now', ?) "
            "ORDER BY id DESC LIMIT ?",
            (f"-{days} days", limit),
        ).fetchall()


def bump_used(ids: list[int]) -> None:
    """记录这些记忆被注入过（留给以后做冷热排序用）。"""
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    with _conn() as c:
        try:
            c.execute(f"UPDATE memories SET last_used_at = CURRENT_TIMESTAMP WHERE id IN ({marks})", ids)
        except sqlite3.OperationalError:
            pass  # 老库没有这一列就跳过


def delete_memory(mid: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM memories WHERE id = ?", (mid,))
        return cur.rowcount > 0


def clear_memories():
    with _conn() as c:
        c.execute("DELETE FROM memories")


def search_memories(kw: str):
    with _conn() as c:
        return c.execute(
            "SELECT id, content FROM memories WHERE content LIKE ? ORDER BY id",
            (f"%{kw}%",),
        ).fetchall()


def search_facts(kw: str, limit: int = 10):
    """只搜永久事实（事件有时效，不适合按关键词捞）。"""
    with _conn() as c:
        return c.execute(
            "SELECT id, content FROM memories "
            "WHERE content LIKE ? AND (kind IS NULL OR kind = 'fact') "
            "AND (status IS NULL OR status = 'active') LIMIT ?",
            (f"%{kw}%", limit),
        ).fetchall()


# ---------- 插件主体 ----------

@register("astrbot_plugin_simple_memory", "Mephisto", "SQLite 长期记忆", "1.0.0")
class SimpleMemory(Star):

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        init_db()
        self.config = config
        self._client = None                      # 惰性初始化
        self.model = config.get("deepseek_model", "deepseek-chat")
        self.only_private = bool(config.get("only_private", True))
        self.extract_every = max(1, int(config.get("extract_every_n_turns", 1)))
        self.turn_counter = 0
        self._inject_cache = None      # 注入块缓存
        self._inject_turn = 0          # 注入刷新计数器
        logger.info(f"SimpleMemory 已加载，当前记忆条数: {len(all_memories())}")

    def _get_client(self):
        """第一次调用时才创建 openai 客户端；Key 为空返回 None。"""
        if self._client is not None:
            return self._client
        api_key = (self.config.get("deepseek_api_key") or "").strip()
        if not api_key:
            logger.warning("SimpleMemory: 还没配置 deepseek_api_key，记忆功能暂停")
            return None
        try:
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=self.config.get("deepseek_base_url", "https://api.deepseek.com/v1"),
            )
        except Exception as e:
            logger.error(f"SimpleMemory: 创建 openai 客户端失败: {e}")
            return None
        return self._client

    def _should_handle(self, event: AstrMessageEvent) -> bool:
        if not self.only_private:
            return True
        if hasattr(event, "is_private_chat"):
            try:
                return event.is_private_chat()
            except Exception:
                pass
        umo = getattr(event, "unified_msg_origin", "") or ""
        return "FriendMessage" in umo or "PrivateMessage" in umo

    # ---------- 请求前：注入记忆 ----------

    @filter.on_llm_request()
    async def inject_memories(self, event: AstrMessageEvent, req):
        if not self._should_handle(event):
            return
        user_msg = (event.message_str or "").strip()

        self._inject_turn += 1

        # 每 5 轮刷新一次；其余轮次用缓存，保持 system prompt 前缀稳定
        # 这样 DeepSeek 的 prefix cache 命中率会显著提高
        if self._inject_turn % 5 != 1 and self._inject_cache is not None:
            req.system_prompt = (req.system_prompt or "") + "\n\n" + self._inject_cache
            return

        # 刷新时顺手把过期的临时事件归档（只标状态，不删除）
        try:
            n_arch = archive_old_events(7)
            if n_arch:
                logger.info(f"[记忆] 归档过期事件 {n_arch} 条")
        except Exception as e:
            logger.warning(f"[记忆] 归档失败: {e}")

        # 1) 永久事实：最近 20 条
        facts = recent_facts(20)

        # 2) 临时事件：最近 3 天，最多 10 条，带日期
        events = recent_events(3, 10)

        # 3) 关键词匹配（只搜事实）
        matched = []
        if user_msg:
            import re
            keywords = re.findall(r"[\u4e00-\u9fa5]{2,}", user_msg)
            seen = {content for _, content in facts}
            for kw in keywords:
                for mid, content in search_facts(kw):
                    if content not in seen:
                        seen.add(content)
                        matched.append((mid, content))

        lines = []
        if facts:
            lines.append("【长期记住的】")
            lines.extend(f"- {content}" for _, content in facts)
        if events:
            lines.append("【最近几天发生的（括号里是日期）】")
            for _mid, content, edate in events:
                d = (edate or "")[5:] or "?"
                lines.append(f"- [{d}] {content}")
        if matched:
            lines.append("【和当前话题相关的】")
            lines.extend(f"- {content}" for _, content in matched[:10])

        if lines:
            memory_block = (
                "以下是你记住的关于用户的信息（相关部分）：\n"
                + "\n".join(lines)
                + "\n\n"
            )
        else:
            memory_block = "你目前还没有关于用户的长期记忆。\n\n"

        prefix = (
            memory_block
            + "带日期的条目是「那几天发生的事」，已经过去的就别当成现在还在发生。\n"
            "回答时自然地使用这些信息，不要生硬复述，也不要主动列出全部。\n"
            "如果你需要按关键词精确定位某条记忆，可以调用 recall_memory 工具；\n"
            "想知道记忆规模时，可以调用 memory_stats 工具。\n"
            "当你判断某条关于用户的稳定事实值得长期保留时，主动调用 remember 写入。"
        )

        self._inject_cache = prefix
        req.system_prompt = (req.system_prompt or "") + "\n\n" + prefix

    # ---------- 回复后：提取记忆 ----------

    @filter.on_llm_response()
    async def extract_memories(self, event: AstrMessageEvent, resp):
        if not self._should_handle(event):
            return

        self.turn_counter += 1
        if self.turn_counter % self.extract_every != 0:
            return

        client = self._get_client()
        if client is None:
            return

        user_msg = (event.message_str or "").strip()
        reply = (getattr(resp, "completion_text", "") or "").strip()
        if not user_msg or not reply:
            return

        # v2 补丁：提取 prompt 需要「今天」的具体日期（原来漏了这段定义）
        now = datetime.now()
        today_s = now.strftime("%Y-%m-%d")
        today_md = now.strftime("%m-%d")
        tomorrow_md = (now + timedelta(days=1)).strftime("%m-%d")

        prompt = f"""你在阅读一段对话。
[用户] 是正在和你对话的人，[我] 是你自己。
今天是 {today_s}。

从这段对话里，提取值得长期记住的关于**用户**的稳定事实。

每条先判断类型，只输出下面两种格式之一：
- [fact] 长期成立的事实
- [event] MM-DD 只在某几天发生的事

规则：
1. **只从 [用户] 说的话里提取。** [我] 说的话只是上下文，不要从里面提取。
2. **[fact]**：身份、偏好、习惯、重要关系、长期计划。
   例：[fact] 用户养了一只叫咪咪的猫
   例：[fact] 用户住在杭州，做后端开发
3. **[event]**：发生在具体日子里的事——上课、考试、出门、生病、临时安排。
   **必须把「今天/明天/昨天/这周」换算成具体日期 MM-DD。**
   例：用户说「我今天有课」→ [event] {today_md} 用户有课
   例：用户说「明天要考试」→ [event] {tomorrow_md} 用户有考试
4. 代词（我/你）指代不明确 → 跳过这条，不要瞎猜。
5. 主语必须明确，用"用户"或"我"；不要用"助手""机器人"。
6. 不要提取寒暄、当次提问、纯情绪发泄。
7. 表达模糊（"可能""大概""听说"）→ 不提取。
8. 一行一条，不要编号，不要解释。没有可提取的就输出：无

对话内容：
[用户] {user_msg}
[我] {reply}

提取结果："""

        try:
            r = await client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
            usage = getattr(r, "usage", None)
            if usage:
                logger.info(
                    f"[记忆提取] 输入={usage.prompt_tokens} "
                    f"输出={usage.completion_tokens} 总计={usage.total_tokens}"
                )
            text = (r.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning(f"记忆提取失败: {e}")
            return

        if not text or text == "无":
            return

        saved = 0
        for line in text.splitlines():
            line = line.strip(" -•·\t")
            if not line or len(line) > 220:
                continue

            m = re.match(r"^\[(fact|event)\]\s*(.*)$", line)
            kind, body = "fact", line
            if m:
                kind, body = m.group(1), m.group(2).strip()

            event_date = None
            if kind == "event":
                dm = re.match(r"^(\d{1,2})-(\d{1,2})\s*(.*)$", body)
                if dm:
                    mm, dd, rest = int(dm.group(1)), int(dm.group(2)), dm.group(3).strip()
                    event_date = f"{now.year:04d}-{mm:02d}-{dd:02d}"
                    body = rest
                else:
                    event_date = today_s
                if not body:
                    continue

            if body and add_memory(body, kind=kind, event_date=event_date):
                saved += 1
        if saved:
            logger.info(f"新增 {saved} 条记忆")

    # ---------- 命令：/mem ----------

    @filter.command("mem")
    async def mem_cmd(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        for p in ("/mem", "／mem", "mem"):
            if raw.startswith(p):
                raw = raw[len(p):].strip()
                break

        parts = raw.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("", "help"):
            yield event.plain_result(
                "/mem list            列出全部记忆\n"
                "/mem count           显示条数\n"
                "/mem search 关键词   搜索\n"
                "/mem add 内容        手动添加\n"
                "/mem del ID          删除指定 ID\n"
                "/mem clear 确认      清空全部"
            )
            return

        if sub == "list":
            rows = all_memories()
            if not rows:
                yield event.plain_result("暂无记忆")
                return
            text = "\n".join(f"[{i}] {c}" for i, c in rows)
            yield event.plain_result(text[:3500])
            return

        if sub == "count":
            yield event.plain_result(f"当前共 {len(all_memories())} 条记忆")
            return

        if sub == "search":
            if not arg:
                yield event.plain_result("用法：/mem search 关键词")
                return
            rows = search_memories(arg)
            if not rows:
                yield event.plain_result("没有匹配的记忆")
                return
            text = "\n".join(f"[{i}] {c}" for i, c in rows)
            yield event.plain_result(text[:3500])
            return

        if sub == "add":
            if not arg:
                yield event.plain_result("用法：/mem add 要记住的内容")
                return
            if add_memory(arg):
                yield event.plain_result("已记住")
            else:
                yield event.plain_result("这条记忆已存在（相似度过高），跳过")
            return

        if sub == "del":
            if not arg.isdigit():
                yield event.plain_result("用法：/mem del ID")
                return
            if delete_memory(int(arg)):
                yield event.plain_result(f"已删除 [{arg}]")
            else:
                yield event.plain_result("没有这条 ID")
            return

        if sub == "clear":
            if arg != "确认":
                yield event.plain_result("确认清空请发送：/mem clear 确认")
                return
            clear_memories()
            yield event.plain_result("已清空全部记忆")
            return

        yield event.plain_result("未知子命令，发送 /mem help 查看用法")
        # ---------- LLM 工具：让 AI 主动查记忆 ----------

    @filter.llm_tool(name="recall_memory")
    async def tool_recall_memory(self, event: AstrMessageEvent, keyword: str = ""):
        """搜索长期记忆库，按关键词精确定位相关的记忆条目。

        当你想确认"之前有没有记过关于某件事的细节"、用户问"你还记得我说过什么吗"、
        或者需要比当前上下文更精确的回忆时调用。平时不用调用，因为每轮对话你
        已经能看到全部记忆。

        Args:
            keyword (str): 搜索关键词，例如"猫"、"工作"、"杭州"。留空时返回最近若干条记忆。
        """
        if keyword.strip():
            rows = search_memories(keyword.strip())
        else:
            rows = all_memories()[-10:]

        if not rows:
            return f"没有匹配「{keyword}」的记忆" if keyword else "记忆库为空"

        lines = [f"[{mid}] {content}" for mid, content in rows]
        return "找到以下记忆：\n" + "\n".join(lines)

    @filter.llm_tool(name="memory_stats")
    async def tool_memory_stats(self, event: AstrMessageEvent):
        """查看记忆库当前状态：总共有多少条记忆。

        用户问"你记得多少关于我的事"、你自己想知道记忆规模时调用。
        """
        rows = all_memories()
        return f"记忆库当前共 {len(rows)} 条记忆"

    @filter.llm_tool(name="remember")
    async def tool_remember(self, event: AstrMessageEvent, content: str):
        """主动往长期记忆库写入一条关于用户的稳定事实。

        当你判断某条信息值得长期保留时调用，例如：
        - 用户明确说"记住……"
        - 用户透露了身份、偏好、习惯、重要关系、长期计划
        - 出现了以后可能会用到的稳定事实
        注意：你是对话助手。
        写事实时主语用"用户"或"我"

        - 提取前想一想：这条是不是和"用户请我吃饭""用户答应我某事"这类已经说过很多次的重复？
            如果是同一件事的复述，跳过。

        
        不要写入：
        - 寒暄、临时情绪、当次提问内容
        - 你自己说过的话（这不是关于用户的事实）
        - 已经在记忆库里的事实（不要重复记）

        Args:
            content (str): 一条简洁、独立、可检索的事实句，例如"用户养了一只叫咪咪的猫"
        """
        content = (content or "").strip()
        if not content:
            return "内容为空，未写入"
        if len(content) > 200:
            return "内容过长，请压缩为一句简洁的事实"
        if add_memory(content):
            return f"已记住：{content}"
        return f"这条记忆已存在（相似度过高），跳过：{content}"
