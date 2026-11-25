"""刷 B 站：让她在空闲时段自己看一会儿，再随口跟你说一句。

设计要点
--------
1. 心跳每 60 秒一次，**纯代码闸门**（窗口 / 次数 / 间隔 / 主人在不在聊），
   全部通过才问一次 LLM「想不想刷」——不是每次都花 token。
2. 窗口化而不是定点：默认 9-12 / 17-21。窗口内没醒过就作废，不追。
3. 单次体量：搜 20 条 → 粗筛 5 条 → 读 3 条字幕 → 一次 LLM 生成一句话。
4. 没字幕默认跳过；`asr_enabled` 打开且 asr.py 存在时才本地转写。
5. 不发评论、不点赞、不做视觉理解。
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.star.register import register_on_astrbot_loaded

PLUGIN_DIR = Path(__file__).parent
DATA_DIR = PLUGIN_DIR / "data"
DB_PATH = DATA_DIR / "browse.db"
CRED_PATH = DATA_DIR / "credential.json"
ASR_SCRIPT = PLUGIN_DIR / "asr.py"
ASR_PY = DATA_DIR / "asr_venv" / "Scripts" / "python.exe"

TICK_SECONDS = 60
BROWSE_TIMEOUT = 240          # 单次刷的硬超时
SUBTITLE_FETCH_TIMEOUT = 15
MAX_ASKS_PER_SLOT = 2         # 同一窗口最多问她几次「想不想刷」

PASSPORT_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
PASSPORT_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_SYS_DECIDE = (
    "你是一个判断模块。根据给定情况，只输出一个 JSON 对象，不要输出任何别的文字。"
    '格式：{"browse": true 或 false, "reason": "简短理由"}'
)

_SYS_WRITE = (
    "你在以第一人称写一句『刚刚刷到视频』的闲聊话。要求："
    "1) 像随口说的，不超过 120 字；2) 说清楚看到了什么、你自己的感受；"
    "3) 不要用 Markdown、不要列条目、不要写『总结』『摘要』这类词；"
    "4) 不要提到任何工具、插件、设置、任务、流程；"
    "5) 不确定的细节不要编。"
)


# ============================================================
# 数据库
# ============================================================


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _ensure_column(c: sqlite3.Connection, table: str, col: str, ddl: str) -> None:
    """轻量迁移：列不存在就 ALTER TABLE 加上。"""
    cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    if col not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    with _conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS slot_state(
                date TEXT NOT NULL,
                slot TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                asks INTEGER DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY(date, slot))"""
        )
        # v0.1.6：把「问了几次」拆成 refused（她说不想刷）与 failed（答应了但没刷成）
        _ensure_column(c, "slot_state", "refused", "refused INTEGER DEFAULT 0")
        _ensure_column(c, "slot_state", "failed", "failed INTEGER DEFAULT 0")
        c.execute(
            """CREATE TABLE IF NOT EXISTS browse_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT, slot TEXT, ts TEXT,
                bvid TEXT, title TEXT, up_name TEXT, link TEXT,
                minutes REAL, subtitle_chars INTEGER,
                summary TEXT, spoke INTEGER DEFAULT 0)"""
        )
        # v0.2.0：一次刷 = 一个 batch，用来数「刷了几次」而不是「几个窗口」
        _ensure_column(c, "browse_log", "batch", "batch TEXT")
        c.execute(
            """CREATE TABLE IF NOT EXISTS seen(
                bvid TEXT PRIMARY KEY, title TEXT, first_seen TEXT)"""
        )
        c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_log_date ON browse_log(date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_seen_time ON seen(first_seen)")


def kv_get(k: str, default: str = "") -> str:
    with _conn() as c:
        row = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def kv_set(k: str, v: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
        )


def get_slot(date: str, slot: str) -> sqlite3.Row | None:
    with _conn() as c:
        return c.execute(
            "SELECT * FROM slot_state WHERE date=? AND slot=?", (date, slot)
        ).fetchone()


def ensure_slot(date: str, slot: str) -> sqlite3.Row:
    row = get_slot(date, slot)
    if row is None:
        with _conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO slot_state(date, slot, status, asks, updated_at) "
                "VALUES(?, ?, 'pending', 0, ?)",
                (date, slot, datetime.now().isoformat(timespec="seconds")),
            )
        row = get_slot(date, slot)
    return row


def set_slot(
    date: str,
    slot: str,
    status: str | None = None,
    add_ask: bool = False,
    add_refused: bool = False,
    add_failed: bool = False,
) -> None:
    """更新窗口状态。

    - add_ask：问了她一次（不管她答什么）
    - add_refused：她明确说不想刷
    - add_failed：她答应了，但这次没刷出东西
    """
    sets: list[str] = []
    params: list[Any] = []
    if status is not None:
        sets.append("status=?")
        params.append(status)
    if add_ask:
        sets.append("asks=asks+1")
    if add_refused:
        sets.append("refused=refused+1")
    if add_failed:
        sets.append("failed=failed+1")
    if not sets:
        return
    sets.append("updated_at=?")
    params.append(datetime.now().isoformat(timespec="seconds"))
    params.extend([date, slot])
    with _conn() as c:
        c.execute(
            f"UPDATE slot_state SET {', '.join(sets)} WHERE date=? AND slot=?",
            tuple(params),
        )


def mark_seen(bvid: str, title: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO seen(bvid, title, first_seen) VALUES(?, ?, ?)",
            (bvid, title, datetime.now().isoformat(timespec="seconds")),
        )


def is_seen(bvid: str) -> bool:
    with _conn() as c:
        return (
            c.execute("SELECT 1 FROM seen WHERE bvid=?", (bvid,)).fetchone() is not None
        )


def add_log(**kw: Any) -> None:
    cols = ",".join(kw.keys())
    marks = ",".join("?" * len(kw))
    with _conn() as c:
        c.execute(f"INSERT INTO browse_log({cols}) VALUES({marks})", tuple(kw.values()))


def count_today(date: str) -> int:
    """今天真正刷了几次。

    v0.2.0 起一次刷会写同一个 batch，所以数 DISTINCT batch；
    旧数据没有 batch 字段，退回用 ts 近似。
    """
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(DISTINCT COALESCE(NULLIF(batch, ''), ts)) AS n "
            "FROM browse_log WHERE date=? AND slot<>'手动'",
            (date,),
        ).fetchone()
    return int(row["n"]) if row else 0


def _fmt_when(date_str: str, slot: str) -> str:
    """把日期+窗口格式化成『今天傍晚』这种。"""
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    if date_str == today:
        day = "今天"
    elif date_str == yesterday:
        day = "昨天"
    else:
        day = date_str[5:] if len(date_str) >= 10 else date_str
    part = {"9-12": "上午", "17-21": "傍晚"}.get(slot, "")
    return f"{day}{part}"


def recent_browse_lines(days: int = 3, limit: int = 6, title_len: int = 28) -> list[str]:
    """最近几天刷到过什么，给对话注入用。按 bvid 去重，新的在前。"""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _conn() as c:
        rows = c.execute(
            "SELECT date, slot, bvid, title, up_name FROM browse_log "
            "WHERE date >= ? ORDER BY id DESC",
            (since,),
        ).fetchall()

    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        bvid = r["bvid"]
        if bvid in seen:
            continue
        seen.add(bvid)
        title = clip(r["title"] or "", title_len)
        up = (r["up_name"] or "").strip()
        when = _fmt_when(r["date"], r["slot"])
        out.append(f"{when}：《{title}》" + (f" by {up}" if up else ""))
        if len(out) >= limit:
            break
    return out


_MEM_LINE = (
    "（这些你本来就知道，不用假装「查了记录」。细节记不清就直说记不清，别编；"
    "也不用主动提，话题对上了再说。）"
)


def next_window_label(windows: list[tuple[int, int, str]], now: datetime) -> str:
    """下一次能刷的时段，形如『今天 17-21』或『明天 9-12』。"""
    for h1, _h2, label in windows:
        if h1 > now.hour:
            return f"今天 {label}"
    if windows:
        return f"明天 {windows[0][2]}"
    return "明天"


def browse_status_line(
    max_per_day: int, windows: list[tuple[int, int, str]], now: datetime
) -> str:
    """她自己的『现在还能不能刷』状态，一句话。

    v0.2.0 起不再按窗口状态判断，改成「今天刷了几次 + 距上次多久」。
    """
    date = now.strftime("%Y-%m-%d")
    used = count_today(date)
    remain = max(0, max_per_day - used)
    cur = slot_of(now, windows)
    nxt = next_window_label(windows, now)

    # 先看时段：凌晨要说「太晚了」，而不是「额度用完了」
    if not cur:
        return f"今天已经刷过 {used}/{max_per_day} 次；现在不在可刷时段，下次可刷是{nxt}。"

    if remain <= 0:
        return f"今天已经刷过 {used}/{max_per_day} 次，额度用完了；下次可刷是{nxt}。"

    gap_h = None
    last = kv_get("last_browse_at", "")
    if last:
        try:
            gap_h = (now - datetime.fromisoformat(last)).total_seconds() / 3600
        except Exception:
            gap_h = None

    if gap_h is not None and gap_h < 4:
        return (
            f"今天已经刷过 {used}/{max_per_day} 次；刚刷过 {gap_h:.1f} 小时，"
            f"要再等 {4 - gap_h:.1f} 小时。"
        )

    return f"今天已经刷过 {used}/{max_per_day} 次；现在还能刷 {remain} 次。"


# ============================================================
# 时间与窗口
# ============================================================

_WIN_RE = re.compile(r"^(\d{1,2})\s*-\s*(\d{1,2})$")


def parse_windows(raw: str) -> list[tuple[int, int, str]]:
    """'8-23' -> [(8,23,'8-23')]；也兼容 '9-12,17-21' 这种多段写法"""
    out: list[tuple[int, int, str]] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = _WIN_RE.match(part)
        if not m:
            logger.warning(f"[刷B站] 窗口格式无效，忽略：{part}")
            continue
        h1, h2 = int(m.group(1)), int(m.group(2))
        if not (0 <= h1 < h2 <= 24):
            logger.warning(f"[刷B站] 窗口区间无效，忽略：{part}")
            continue
        out.append((h1, h2, f"{h1}-{h2}"))
    return out


def slot_of(now: datetime, windows: list[tuple[int, int, str]]) -> str | None:
    for h1, h2, label in windows:
        if h1 <= now.hour < h2:
            return label
    return None


def hhmm(now: datetime) -> str:
    return now.strftime("%H:%M")


# ============================================================
# 小工具
# ============================================================

_EM_RE = re.compile(r"<[^>]+>")


def clean_text(s: str) -> str:
    return _EM_RE.sub("", s or "").strip()


def parse_duration(v: Any) -> int:
    """'2:30' / '1:02:03' / 150 -> 秒"""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    parts = s.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return 0


def fmt_minutes(seconds: int) -> str:
    m, s = divmod(max(0, seconds), 60)
    return f"{m}分{s:02d}秒" if m else f"{s}秒"


def clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.72)
    tail = limit - head - 5
    return text[:head] + " …… " + text[-tail:]


# ============================================================
# B 站能力层
# ============================================================


class Bili:
    """薄封装：搜索 / 详情 / 字幕。凭证可选。"""

    def __init__(self) -> None:
        self._cred = None

    # ---------- 凭证 ----------

    def load_cred(self) -> bool:
        if not CRED_PATH.exists():
            return False
        try:
            from bilibili_api import Credential

            raw = json.loads(CRED_PATH.read_text("utf-8"))
            self._cred = Credential(
                sessdata=raw.get("sessdata"),
                bili_jct=raw.get("bili_jct"),
                buvid3=raw.get("buvid3"),
                buvid4=raw.get("buvid4"),
                dedeuserid=raw.get("dedeuserid"),
                ac_time_value=raw.get("ac_time_value"),
            )
            return True
        except Exception as e:
            logger.warning(f"[刷B站] 读取凭证失败：{e}")
            return False

    def save_cred(self, cred) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessdata": getattr(cred, "sessdata", None),
            "bili_jct": getattr(cred, "bili_jct", None),
            "buvid3": getattr(cred, "buvid3", None),
            "buvid4": getattr(cred, "buvid4", None),
            "dedeuserid": getattr(cred, "dedeuserid", None),
            "ac_time_value": getattr(cred, "ac_time_value", None),
        }
        CRED_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        self._cred = cred

    @property
    def logged_in(self) -> bool:
        return bool(getattr(self._cred, "sessdata", None))

    # ---------- 登录 ----------
    #
    # 不用 bilibili_api.login_v2：它的 check_state 从回调 URL 里抠 cookie，
    # 实测在 17.4.2 + 当前 B 站接口下会把 sessdata 抠成空串（ac_time_value 却正常），
    # 结果就是"扫码成功但等于没登录"。这里直接走 passport 的三个接口，自己解析。

    async def new_login(self) -> tuple[str, str]:
        """返回 (二维码图片路径, qrcode_key)。"""
        import aiohttp
        import qrcode

        async with aiohttp.ClientSession() as s:
            async with s.get(PASSPORT_GEN, headers={"User-Agent": _UA}) as r:
                d = await r.json(content_type=None)
        if int(d.get("code", -1)) != 0:
            raise RuntimeError(f"生成二维码失败：{d.get('message')}")
        data = d.get("data") or {}
        key = data.get("qrcode_key") or ""
        url = data.get("url") or ""
        if not key or not url:
            raise RuntimeError("二维码接口返回为空")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        p = DATA_DIR / "login_qr.png"
        qrcode.make(url).save(str(p))
        return str(p), key

    async def poll_login(self, key: str) -> str:
        """返回 pending / scanned / done / timeout / error。

        成功时优先从响应头 Set-Cookie（aiohttp cookie jar）拿凭证，
        拿不到再回退解析 data.url。两条路都试过才判失败。
        """
        import aiohttp

        async with aiohttp.ClientSession() as s:
            async with s.get(
                PASSPORT_POLL,
                params={"qrcode_key": key},
                headers={"User-Agent": _UA},
            ) as r:
                d = await r.json(content_type=None)
            jar = {c.key: c.value for c in s.cookie_jar}

        data = d.get("data") or {}
        try:
            code = int(data.get("code", -1))
        except (TypeError, ValueError):
            code = -1

        if code == 0:
            refresh = data.get("refresh_token") or ""
            sessdata = jar.get("SESSDATA") or ""
            if sessdata:
                self._save_cred(
                    sessdata,
                    jar.get("bili_jct") or "",
                    jar.get("DedeUserID") or "",
                    refresh,
                )
                logger.info("[刷B站] 登录成功（cookie jar），sessdata 长度=%d", len(sessdata))
                return "done"
            if self._save_from_cookie_url(data.get("url") or "", refresh):
                return "done"
            logger.error(
                "[刷B站] 登录成功但没拿到 SESSDATA；cookie keys=%s；url 前 100 字=%s",
                list(jar.keys()),
                (data.get("url") or "")[:100],
            )
            return "error"
        if code == 86038:
            return "timeout"
        if code == 86090:
            return "scanned"
        return "pending"  # 86101 = 还没扫

    def _save_cred(
        self, sessdata: str, bili_jct: str, dedeuserid: str, ac_time_value: str
    ) -> bool:
        if not sessdata:
            return False
        try:
            from bilibili_api import Credential

            cred = Credential(
                sessdata=sessdata,
                bili_jct=bili_jct or None,
                dedeuserid=dedeuserid or None,
                ac_time_value=ac_time_value or None,
            )
        except Exception as e:
            logger.error(f"[刷B站] 构造凭证失败：{e}")
            return False
        self.save_cred(cred)
        return True

    def _save_from_cookie_url(self, cred_url: str, refresh_token: str = "") -> bool:
        """从登录回调 URL 的 query 里抠 cookie。保留原始编码，不再 quote。"""
        query = cred_url.split("?", 1)[1] if "?" in cred_url else ""
        raw: dict[str, str] = {}
        for kv in query.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                raw[k] = v
        sessdata = raw.get("SESSDATA", "")
        if not sessdata:
            return False
        ok = self._save_cred(
            sessdata,
            raw.get("bili_jct") or "",
            raw.get("DedeUserID") or "",
            refresh_token,
        )
        if ok:
            logger.info("[刷B站] 登录成功（url 解析），sessdata 长度=%d", len(sessdata))
        return ok

    # ---------- 搜索 ----------

    async def search(self, keyword: str, page: int = 1) -> list[dict]:
        from bilibili_api import search

        r = await search.search_by_type(
            keyword, search.SearchObjectType.VIDEO, page=page
        )
        items = r.get("result") or []
        out: list[dict] = []
        for it in items:
            bvid = it.get("bvid") or ""
            if not bvid:
                continue
            out.append(
                {
                    "bvid": bvid,
                    "title": clean_text(it.get("title")),
                    "author": clean_text(it.get("author")),
                    "duration": parse_duration(it.get("duration")),
                    "play": int(it.get("play") or 0),
                    "desc": clean_text(it.get("description")),
                    "pubdate": int(it.get("pubdate") or 0),
                }
            )
        return out

    # ---------- 详情 ----------

    async def info(self, bvid: str) -> dict:
        from bilibili_api import video

        v = video.Video(bvid=bvid, credential=self._cred)
        data = await v.get_info()
        pages = data.get("pages") or [{}]
        return {
            "bvid": bvid,
            "title": clean_text(data.get("title")),
            "author": clean_text((data.get("owner") or {}).get("name")),
            "duration": int(data.get("duration") or 0),
            "view": int((data.get("stat") or {}).get("view") or 0),
            "desc": clean_text(data.get("desc")),
            "cid": pages[0].get("cid"),
        }

    # ---------- 字幕 ----------

    async def subtitle_text(self, bvid: str, cid: int) -> str:
        """返回字幕正文（已拼好），拿不到返回空串。"""
        if not cid:
            return ""
        from bilibili_api import video

        v = video.Video(bvid=bvid, credential=self._cred)
        try:
            data = await v.get_subtitle(cid)
        except Exception as e:
            logger.info(f"[刷B站] {bvid} 字幕列表失败：{type(e).__name__}: {e}")
            return ""

        tracks = (data or {}).get("subtitles") or []
        if not tracks:
            return ""
        # 优先中文（ai-zh / zh-CN / zh-Hans）
        tracks.sort(key=lambda t: 0 if "zh" in (t.get("lan") or "").lower() else 1)
        url = tracks[0].get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return ""
        return await self._fetch_subtitle_body(url)

    async def _fetch_subtitle_body(self, url: str) -> str:
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=SUBTITLE_FETCH_TIMEOUT)
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com/",
            }
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url, headers=headers) as r:
                    if r.status != 200:
                        logger.info(f"[刷B站] 字幕正文 HTTP {r.status}")
                        return ""
                    data = await r.json(content_type=None)
            body = data.get("body") or []
            return " ".join(
                (p.get("content") or "").strip() for p in body if p.get("content")
            )
        except Exception as e:
            logger.info(f"[刷B站] 字幕正文抓取失败：{type(e).__name__}: {e}")
            return ""


# ============================================================
# 插件主体
# ============================================================

_plugin_instance: "BiliBrowse | None" = None


@register_on_astrbot_loaded()
async def _on_astrbot_loaded(*args, **kwargs):
    if _plugin_instance is not None:
        _plugin_instance._ensure_task()
        logger.info("[刷B站] AstrBot 加载完成，心跳已启动")


@register(
    "astrbot_plugin_bili_browse",
    "Mephisto",
    "刷 B 站：空闲时自己看一会儿，再随口说一句",
    "0.2.0",
)
class BiliBrowse(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        init_db()
        self.config = config
        self.bili = Bili()
        self.bili.load_cred()
        # 扫码万一还是不行，允许在配置里手填 SESSDATA 兜底
        manual = (config.get("sessdata") or "").strip()
        if manual and not self.bili.logged_in:
            try:
                from bilibili_api import Credential

                self.bili.save_cred(Credential(sessdata=manual))
                logger.info("[刷B站] 用配置里的 sessdata 兜底登录，长度=%d", len(manual))
            except Exception as e:
                logger.warning(f"[刷B站] 配置里的 sessdata 不可用：{e}")

        self._task: asyncio.Task | None = None
        self._stop = False
        self._last_user_at: dict[str, datetime] = {}
        self._pending_say: str = kv_get("pending_say", "")
        self._busy = False
        self._mem_cache: tuple[float, list[str]] = (0.0, [])

        global _plugin_instance
        _plugin_instance = self
        logger.info(
            f"[刷B站] 已加载，登录态={'有' if self.bili.logged_in else '无'}，"
            f"时段={self.config.get('windows', '8-23')}"
        )

    # ---------------- 生命周期 ----------------

    def _ensure_task(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._loop())
            logger.info("[刷B站] 心跳循环已创建")
        except RuntimeError:
            logger.warning("[刷B站] 没有运行中的事件循环，心跳未启动")

    async def _loop(self) -> None:
        await asyncio.sleep(20)  # 启动后先等一会儿，别和别的插件抢
        while not self._stop:
            try:
                await self._tick()
            except Exception:
                logger.exception("[刷B站] tick 异常")
            await asyncio.sleep(TICK_SECONDS)

    async def terminate(self) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()

    # ---------------- 心跳 ----------------

    async def _tick(self) -> None:
        if not self.config.get("enabled", True):
            return
        now = datetime.now()
        windows = parse_windows(self.config.get("windows", "8-23"))

        # 1) 有攒着没说的话，等安静了再说；没说之前不再刷新的（一次只说一件）
        if self._pending_say:
            umo = self._target_umo()
            if umo and self._quiet_enough(umo):
                ok = await self._deliver(umo, self._pending_say)
                if ok:
                    logger.info("[刷B站] 攒着的话已发出")
                    self._pending_say = ""
                    kv_set("pending_say", "")
                    self._mark_spoken()
            return

        # 2) 没登录就别开刷：字幕接口会直接拒，白耗一个窗口
        if not self.bili.logged_in:
            return

        # 3) 时段判断
        slot = slot_of(now, windows)
        if not slot:
            return

        date = now.strftime("%Y-%m-%d")
        # v0.2.0：不再用「窗口状态」控制流程，改成每天一行统计 + 次数/间隔驱动
        row = ensure_slot(date, "day")
        max_asks = int(self.config.get("max_asks_per_day", 6))
        if int(row["asks"] or 0) >= max_asks:
            return

        # 3) 硬闸门（零 token）：次数 / 间隔 / 询问节流
        if self._busy:
            return
        if not self._gates_pass(date, now):
            return

        umo = self._target_umo()
        if not umo:
            logger.warning("[刷B站] 找不到目标会话，跳过")
            return

        remain = self._remain_today(date)
        want, reason = await self._ask_want(umo, slot, remain, now)
        logger.info(f"[刷B站] 决策：{'刷' if want else '不刷'}｜{reason}")
        set_slot(date, "day", add_ask=True)
        kv_set("last_ask_at", now.isoformat(timespec="seconds"))

        if not want:
            set_slot(date, "day", add_refused=True)
            return

        self._busy = True
        try:
            summary = await asyncio.wait_for(
                self._browse_once(date, slot, umo), timeout=BROWSE_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning("[刷B站] 本次刷超时，放弃")
            summary = ""
        except Exception:
            logger.exception("[刷B站] 本次刷失败")
            summary = ""
        finally:
            self._busy = False

        if not summary:
            # 她答应了，但这轮没刷出东西（全无字幕/候选全被过滤/生成失败）
            set_slot(date, "day", add_failed=True)
            return

        set_slot(date, "day", status="done")
        self._pending_say = summary
        kv_set("pending_say", summary)
        logger.info(f"[刷B站] 刷完，攒了一句：{summary[:40]}…")

        # 立刻试着说一次（安静的话）
        if self._quiet_enough(umo):
            ok = await self._deliver(umo, summary)
            if ok:
                self._pending_say = ""
                kv_set("pending_say", "")
                self._mark_spoken()

    # ---------------- 闸门 ----------------

    def _expire_slots(self, now: datetime, windows: list[tuple[int, int, str]]) -> None:
        """把今天已经结束、还挂着 pending 的窗口标成 skipped。"""
        date = now.strftime("%Y-%m-%d")
        for _h1, h2, label in windows:
            if now.hour < h2:
                continue
            row = get_slot(date, label)
            if row is not None and row["status"] == "pending":
                logger.info(f"[刷B站] 窗口 {label} 已经过去，标记作废")
                set_slot(date, label, status="skipped")

    def _gates_pass(self, date: str, now: datetime) -> bool:
        if self._remain_today(date) <= 0:
            return False
        last = kv_get("last_browse_at", "")
        if last:
            try:
                gap = now - datetime.fromisoformat(last)
                if gap < timedelta(hours=int(self.config.get("min_gap_hours", 4))):
                    return False
            except Exception:
                pass
        # 同一窗口两次询问之间至少隔 1 小时，别 60 秒问一次把额度问没
        last_ask = kv_get("last_ask_at", "")
        if last_ask:
            try:
                if now - datetime.fromisoformat(last_ask) < timedelta(minutes=60):
                    return False
            except Exception:
                pass
        # v0.2.0：刷是静默的，不会插进对话，所以不再用「主人在说话」去拦
        return True

    def _remain_today(self, date: str) -> int:
        return max(0, int(self.config.get("max_per_day", 3)) - count_today(date))

    def _quiet_enough(self, umo: str) -> bool:
        minutes = int(self.config.get("quiet_if_user_active_minutes", 30))
        t = self._last_user_at.get(umo)
        if t is None:
            return True
        return (datetime.now() - t) >= timedelta(minutes=minutes)

    def _mark_spoken(self) -> None:
        with _conn() as c:
            c.execute(
                "UPDATE browse_log SET spoke=1 WHERE id=(SELECT MAX(id) FROM browse_log)"
            )

    # ---------------- 决策 ----------------

    async def _ask_want(self, umo: str, slot: str, remain: int, now: datetime) -> tuple[bool, str]:
        date = now.strftime("%Y-%m-%d")
        used = count_today(date)
        prompt = (
            f"现在是 {now.strftime('%Y-%m-%d %H:%M')}（{['周一','周二','周三','周四','周五','周六','周日'][now.weekday()]}）。\n"
            f"今天已经刷过 {used} 次，上限 {used + remain} 次，还能刷 {remain} 次。"
            f"刷一次大概两三分钟。\n"
            "你可以选择现在去 B 站随便看看，也可以选择不刷、继续待着。\n"
            '只输出 JSON：{"browse": true/false, "reason": "一句话理由"}'
        )
        try:
            text = await self._llm(umo, prompt, _SYS_DECIDE)
            m = re.search(r"\{.*\}", text, re.S)
            data = json.loads(m.group(0)) if m else {}
            return bool(data.get("browse")) if "browse" in data else False, str(data.get("reason", ""))[:80]
        except Exception as e:
            logger.warning(f"[刷B站] 决策调用失败：{type(e).__name__}: {e}")
            return False, "决策失败"

    # ---------------- 执行一次 ----------

    async def _browse_once(self, date: str, slot: str, umo: str) -> str:
        t0 = time.time()
        read_n = int(self.config.get("read_count", 3))
        max_chars = int(self.config.get("subtitle_max_chars", 1500))
        max_min = int(self.config.get("max_video_minutes", 15))

        keywords = await self._keywords(umo)
        logger.info(f"[刷B站] 关键词：{keywords}")

        pool: list[dict] = []
        for kw in keywords[:2]:
            try:
                got = await self.bili.search(kw)
                logger.info(f"[刷B站] 搜『{kw}』拿到 {len(got)} 条")
                pool.extend(got)
            except Exception as e:
                logger.warning(f"[刷B站] 搜索失败『{kw}』：{type(e).__name__}: {e}")
        if not pool:
            return ""

        # 去重 + 过滤
        uniq: dict[str, dict] = {}
        for it in pool:
            uniq.setdefault(it["bvid"], it)

        cands: list[dict] = []
        for it in uniq.values():
            if is_seen(it["bvid"]):
                continue
            if it["duration"] and it["duration"] > max_min * 60:
                continue
            cands.append(it)

        cands.sort(key=lambda x: (-x["play"], -x["pubdate"]))
        picks = cands[:read_n]
        logger.info(f"[刷B站] 候选 {len(cands)} 条，准备读 {len(picks)} 条")

        read_ok: list[dict] = []
        for it in picks:
            try:
                det = await self.bili.info(it["bvid"])
            except Exception as e:
                logger.info(f"[刷B站] 详情失败 {it['bvid']}：{type(e).__name__}")
                continue
            if det["duration"] and det["duration"] > max_min * 60:
                continue
            body = await self.bili.subtitle_text(det["bvid"], det["cid"] or 0)
            text = clip(body, max_chars)
            if not text:
                if await self._try_asr(det):
                    text = clip(kv_get("last_asr", ""), max_chars)
                if not text:
                    mark_seen(det["bvid"], det["title"])
                    logger.info(f"[刷B站] 无字幕跳过：{det['title'][:30]}")
                    continue
            det["subtitle"] = text
            read_ok.append(det)
            mark_seen(det["bvid"], det["title"])

        hit = len(read_ok)
        logger.info(
            f"[刷B站] 字幕命中 {hit}/{len(picks)}（耗时 {time.time()-t0:.1f}s）"
        )
        if not read_ok:
            return ""

        said = await self._write_say(umo, read_ok)
        if not said:
            return ""

        batch = datetime.now().strftime("%Y%m%d%H%M%S%f")
        for det in read_ok:
            add_log(
                batch=batch,
                date=date,
                slot=slot,
                ts=datetime.now().isoformat(timespec="seconds"),
                bvid=det["bvid"],
                title=det["title"],
                up_name=det["author"],
                link=f"https://www.bilibili.com/video/{det['bvid']}",
                minutes=round(det["duration"] / 60, 1),
                subtitle_chars=len(det.get("subtitle") or ""),
                summary=said,
                spoke=0,
            )
        kv_set("last_browse_at", datetime.now().isoformat(timespec="seconds"))
        kv_set("last_browse_summary", said)
        logger.info(
            f"[刷B站] 本次完成：读 {hit} 条，总耗时 {time.time()-t0:.1f}s，"
            f"字幕命中率 {hit}/{len(picks)}"
        )
        return said

    # ---------------- 本地转写（可选） ----------------

    async def _try_asr(self, det: dict) -> bool:
        if not self.config.get("asr_enabled", False):
            return False
        if not ASR_SCRIPT.exists():
            logger.info("[刷B站] 开了转写但没装 asr.py，跳过")
            return False
        max_min = int(self.config.get("asr_max_minutes", 10))
        if det["duration"] and det["duration"] > max_min * 60:
            logger.info(f"[刷B站] 视频超 {max_min} 分钟，不转写")
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                str(ASR_PY if ASR_PY.exists() else "python"),
                str(ASR_SCRIPT),
                det["bvid"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
            text = (out or b"").decode("utf-8", "ignore").strip()
            if not text:
                logger.info(f"[刷B站] 转写为空（可能没语音）：{err[-200:]!r}")
                return False
            kv_set("last_asr", text)
            return True
        except Exception as e:
            logger.warning(f"[刷B站] 转写失败：{type(e).__name__}: {e}")
            return False

    # ---------------- 关键词 ----------------

    async def _keywords(self, umo: str) -> list[str]:
        raw = (self.config.get("keywords") or "").strip()
        if raw:
            return [k.strip() for k in raw.split(",") if k.strip()][:3]

        cached = kv_get("keywords_cache", "")
        if cached:
            try:
                data = json.loads(cached)
                if data.get("date") == datetime.now().strftime("%Y-%m-%d"):
                    return data.get("words") or ["DeepSeek"]
            except Exception:
                pass

        if not self.config.get("auto_keywords", True):
            return ["DeepSeek"]

        words = await self._auto_keywords(umo)
        kv_set(
            "keywords_cache",
            json.dumps(
                {"date": datetime.now().strftime("%Y-%m-%d"), "words": words},
                ensure_ascii=False,
            ),
        )
        return words

    async def _auto_keywords(self, umo: str) -> list[str]:
        recent = self._recent_messages(umo, 30)
        if not recent:
            return ["DeepSeek"]
        prompt = (
            "下面是用户最近说的话（可能很零碎）。请提炼出 3 个他最近关心的、"
            "适合拿到 B 站搜索的话题关键词。只输出 JSON 数组，例如 "
            '["DeepSeek", "独立游戏", "机械键盘"]。不要解释。\n\n'
            + "\n".join(f"- {m}" for m in recent[-30:])
        )
        try:
            text = await self._llm(umo, prompt, "你只输出 JSON 数组，不要别的文字。")
            m = re.search(r"\[.*?\]", text, re.S)
            arr = json.loads(m.group(0)) if m else []
            words = [str(w).strip() for w in arr if str(w).strip()][:3]
            return words or ["DeepSeek"]
        except Exception as e:
            logger.warning(f"[刷B站] 关键词生成失败：{type(e).__name__}: {e}")
            return ["DeepSeek"]

    def _recent_messages(self, umo: str, limit: int) -> list[str]:
        """从 proactive_contact 的库里读最近对话（只读，失败就空）。"""
        db = PLUGIN_DIR.parent / "proactive_contact" / "proactive.db"
        if not db.exists():
            return []
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            c.row_factory = sqlite3.Row
            rows = c.execute(
                "SELECT content, role FROM messages WHERE umo=? ORDER BY id DESC LIMIT ?",
                (umo, limit),
            ).fetchall()
            c.close()
        except Exception as e:
            logger.info(f"[刷B站] 读对话历史失败：{e}")
            return []

        out: list[str] = []
        for r in reversed(rows):
            content = r["content"]
            text = self._flatten_content(content)
            if text:
                out.append(text[:120])
        return out

    @staticmethod
    def _flatten_content(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get("type") == "text":
                    t = (p.get("text") or "").strip()
                    if t and "<system_reminder>" not in t:
                        parts.append(t)
            return " ".join(parts).strip()
        return ""

    # ---------------- 生成她的话 ----------------

    async def _write_say(self, umo: str, items: list[dict]) -> str:
        lines = []
        for it in items:
            lines.append(
                f"标题：{it['title']}\nUP主：{it['author']}　时长：{fmt_minutes(it['duration'])}"
                f"　播放：{it['view']}\n字幕节选：{it.get('subtitle', '')}"
            )
        prompt = (
            "你刚刚在 B 站刷到下面这些视频，看完了。用一句自然的话跟我说你看到了什么。\n"
            "可以只提最想说的那一个，像随口聊天一样。\n\n" + "\n\n".join(lines)
        )
        # 用她平时的人格，别用匿名写手的口吻——否则投递出去的话不像她说的
        persona = await self._persona_prompt(umo)
        system = (persona + "\n\n" + _SYS_WRITE) if persona else _SYS_WRITE
        try:
            return (await self._llm(umo, prompt, system)).strip()[:300]
        except Exception as e:
            logger.warning(f"[刷B站] 生成话语失败：{type(e).__name__}: {e}")
            return ""

    async def _persona_prompt(self, umo: str) -> str:
        """读 AstrBot 里配的人格文本（只读，不改配置）。"""
        try:
            pm = self.context.persona_manager
            persona = await pm.get_default_persona_v3(umo)
            return (getattr(persona, "system_prompt", "") or "").strip()
        except Exception as e:
            logger.info(f"[刷B站] 读人格失败，用默认口吻：{e}")
            return ""

    # ---------------- LLM ----------------

    async def _llm(self, umo: str, prompt: str, system: str) -> str:
        prov = await self.context.get_using_provider_async(umo)
        if prov is None:
            raise RuntimeError("没有可用的对话模型")
        resp = await prov.text_chat(prompt=prompt, system_prompt=system)
        return (getattr(resp, "completion_text", "") or "").strip()

    # ---------------- 投递 ----------------

    async def _deliver(self, umo: str, text: str) -> bool:
        try:
            from astrbot.core.platform.message_session import MessageSession
            from astrbot.core.message.message_event_result import MessageChain
            from astrbot.core.message.components import Plain
        except ImportError as e:
            logger.error(f"[刷B站] 导入发送模块失败：{e}")
            return False
        try:
            session = MessageSession.from_str(umo)
            chain = MessageChain(chain=[Plain(text=text)])
            ok = await self.context.send_message(session, chain)
            logger.info(f"[刷B站] 发送{'成功' if ok else '失败'}：{umo}")
            if ok:
                # 不写回历史，她下次被问「你刷了吗」会一脸茫然（10-24 实测过）
                await self._append_to_history(umo, text)
            return bool(ok)
        except Exception as e:
            logger.error(f"[刷B站] 发送异常：{e}")
            return False

    async def _append_to_history(self, umo: str, text: str) -> None:
        """把主动发出的这句话补进 AstrBot 的对话历史。"""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                logger.warning("[刷B站] 找不到会话的对话 ID，历史未写回")
                return
            conv = await cm.get_conversation(umo, cid)
            if not conv:
                logger.warning(f"[刷B站] 找不到对话 {cid}，历史未写回")
                return
            history = json.loads(conv.history or "[]")
            history.append({"role": "assistant", "content": text})
            if len(history) > 100:
                history = history[-100:]
            await cm.update_conversation(umo, cid, history=history)
            logger.info(f"[刷B站] 已写入对话历史：{text[:24]}…")
        except Exception as e:
            logger.warning(f"[刷B站] 写入历史失败：{e}")

    # ---------------- 目标会话 ----------------

    def _target_umo(self) -> str:
        cfg = (self.config.get("target_umo") or "").strip()
        if cfg:
            return cfg
        db = PLUGIN_DIR.parent / "proactive_contact" / "proactive.db"
        if not db.exists():
            return ""
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = c.execute(
                "SELECT umo FROM sessions WHERE enabled=1 ORDER BY created_at LIMIT 1"
            ).fetchone()
            c.close()
            return row[0] if row else ""
        except Exception as e:
            logger.info(f"[刷B站] 探测会话失败：{e}")
            return ""

    # ---------------- 事件钩子 ----------------

    def _browse_memory_block(self) -> str:
        """『最近几天刷到什么 + 现在还能不能刷』的注入块。

        列表 5 分钟缓存；状态每次都现算（很便宜，刷完要立刻反映出来）。
        """
        now = time.time()
        ts, lines = self._mem_cache
        if now - ts >= 300:
            try:
                lines = recent_browse_lines(
                    days=int(self.config.get("memory_days", 3)),
                    limit=int(self.config.get("memory_lines", 6)),
                )
            except Exception as e:
                logger.info(f"[刷B站] 读刷视频记忆失败：{e}")
                lines = []
            self._mem_cache = (now, lines)

        if not lines:
            return ""

        dt = datetime.now()
        try:
            status = browse_status_line(
                int(self.config.get("max_per_day", 3)),
                parse_windows(self.config.get("windows", "8-23")),
                dt,
            )
        except Exception as e:
            logger.info(f"[刷B站] 状态行计算失败：{e}")
            status = ""

        parts = ["【你最近刷 B 站看到的】"]
        parts.extend(f"· {x}" for x in lines)
        if status:
            parts.append(status)
        parts.append(_MEM_LINE)
        return "\n".join(parts)

    @filter.on_llm_request()
    async def _on_llm_request(self, event: AstrMessageEvent, req):
        umo = getattr(event, "unified_msg_origin", "") or ""
        if umo:
            self._last_user_at[umo] = datetime.now()
        block = self._browse_memory_block()
        if block:
            req.system_prompt = (req.system_prompt or "") + "\n\n" + block

    # ============================================================
    # 命令
    # ============================================================

    @filter.command("bili")
    async def cmd_bili(self, event: AstrMessageEvent):
        parts = (event.message_str or "").split()
        sub = parts[1].lower() if len(parts) > 1 else "status"

        if sub == "status":
            yield event.plain_result(self._status_text())
            return

        if sub == "now":
            date = datetime.now().strftime("%Y-%m-%d")
            slot = "手动"  # 手动刷不占当天额度
            umo = getattr(event, "unified_msg_origin", "") or self._target_umo()
            if not umo:
                yield event.plain_result("找不到目标会话")
                return
            if not self.bili.logged_in:
                yield event.plain_result("还没登录 B 站，先发 /bili login 扫码")
                return
            yield event.plain_result("好，我去刷一会儿，刷完跟你说。")
            self._busy = True
            try:
                summary = await asyncio.wait_for(
                    self._browse_once(date, slot, umo), timeout=BROWSE_TIMEOUT
                )
            finally:
                self._busy = False
            if summary:
                ok = await self._deliver(umo, summary)
                yield event.plain_result(
                    ("发出去了：" if ok else "生成好了但没发出去：") + summary
                )
            else:
                yield event.plain_result("这次没刷到合适的（可能都没字幕），看日志。")
            return

        if sub == "login":
            if self.bili.logged_in:
                yield event.plain_result("已经是登录状态了。要换号就删 data/credential.json 再来。")
                return
            try:
                path, key = await self.bili.new_login()
            except Exception as e:
                yield event.plain_result(f"生成二维码失败：{e}")
                return
            yield event.image_result(path)
            told_scanned = False
            for _ in range(90):  # 二维码有效期 180 秒
                await asyncio.sleep(2)
                try:
                    state = await self.bili.poll_login(key)
                except Exception as e:
                    logger.warning(f"[刷B站] 轮询登录失败：{e}")
                    continue
                if state == "done":
                    yield event.plain_result("登录成功，凭证已保存。现在可以 /bili now 了。")
                    return
                if state == "timeout":
                    yield event.plain_result(
                        "二维码过期了（只有 3 分钟），再发一次 /bili login，扫完后立刻点确认。"
                    )
                    return
                if state == "error":
                    yield event.plain_result(
                        "扫码确认了，但没拿到登录凭证。别重试了，"
                        "去 WebUI 的插件配置里手动填 sessdata 兜底（见 README）。"
                    )
                    return
                if state == "scanned" and not told_scanned:
                    told_scanned = True
                    yield event.plain_result("扫到了，在手机上点一下「确认登录」。")
            yield event.plain_result("等太久没确认，算了，再发一次 /bili login。")
            return

        if sub in ("on", "off"):
            self.config["enabled"] = sub == "on"
            yield event.plain_result(f"刷 B 站已{'开启' if sub == 'on' else '关闭'}（重启后需在配置里改）。")
            return

        if sub == "say":
            self._pending_say = ""
            kv_set("pending_say", "")
            yield event.plain_result("清掉了攒着没说的话。")
            return

        yield event.plain_result(
            "用法：/bili status | now | login | on | off | say"
        )

    def _status_text(self) -> str:
        now = datetime.now()
        date = now.strftime("%Y-%m-%d")
        windows = parse_windows(self.config.get("windows", "8-23"))
        cur = slot_of(now, windows)
        row = get_slot(date, "day")
        asks = int(row["asks"] or 0) if row else 0
        refused = int(row["refused"] or 0) if row else 0
        failed = int(row["failed"] or 0) if row else 0
        lines = [
            f"登录：{'已登录' if self.bili.logged_in else '未登录'}",
            f"时段：{self.config.get('windows')}　当前：{cur or '不在时段内'}",
            f"今天已刷：{count_today(date)}/{self.config.get('max_per_day', 3)}",
            f"今天问过：{asks} 次（拒绝 {refused}，失败 {failed}）",
            f"上次刷：{kv_get('last_browse_at', '无')}",
            f"攒着的话：{self._pending_say or '无'}",
        ]
        return "\n".join(lines)
