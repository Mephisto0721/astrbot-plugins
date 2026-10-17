"""表情包插件。

她在合适的时候主动发一张表情包：自己判断要不要发，
按情绪分类从 stickers/<情绪>/ 里随机抽一张，通过聊天平台发出去。
"""

import random
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


STICKER_DIR = Path(__file__).parent / "stickers"

EMOTIONS = ["happy", "shy", "angry", "sad", "smug"]

EMOTION_LABELS = {
    "happy": "开心、高兴、兴奋",
    "shy": "害羞、脸红、被戳中心事",
    "angry": "生气、鼓脸、炸毛",
    "sad": "难过、委屈、低落",
    "smug": "得意、坏笑、捉弄人",
}

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _as_bool(value, default=True) -> bool:
    """配置里的开关可能是真布尔，也可能存成了字符串。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if value is None:
        return default
    return bool(value)


def _list_stickers(emotion: str) -> list:
    """列出某个情绪分类下所有可用的表情包文件（按文件名排序）。"""
    folder = STICKER_DIR / emotion
    if not folder.is_dir():
        return []
    return sorted(
        p
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in ALLOWED_EXT
    )


@register("astrbot_plugin_sticker", "Mephisto", "表情包", "1.0.0")
class StickerPlugin(Star):

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

        self.enabled = _as_bool(config.get("enabled", True))
        try:
            self.cooldown = max(0, int(config.get("cooldown_turns", 5)))
        except (TypeError, ValueError):
            self.cooldown = 5

        # 冷却用的轮数计数：每轮对话结束后 +1
        self._turn_counter = 0
        self._last_sent_turn = -(10 ** 9)
        # 每个情绪各自记一条，避免同一分类连续发同一张
        self._last_sent_path = {}

        # 目录不存在就补上（不会动已有的图片）
        for emotion in EMOTIONS:
            (STICKER_DIR / emotion).mkdir(parents=True, exist_ok=True)

        logger.info(f"StickerPlugin 已加载，表情目录: {STICKER_DIR}")

    # ---------- 内部逻辑 ----------

    def _can_send(self) -> bool:
        """距离上次发表情是否已经隔够了轮数。

        _turn_counter 要等本轮结束（on_llm_response）才会 +1，
        所以差值严格大于 cooldown 时，中间正好完整空出 cooldown 轮对话。
        """
        return self._turn_counter - self._last_sent_turn > self.cooldown

    def _pick(self, emotion: str):
        """随机抽一张图；同一情绪下尽量不和上一次重复。"""
        files = _list_stickers(emotion)
        if not files:
            return None
        last = self._last_sent_path.get(emotion)
        candidates = [f for f in files if f != last] or files
        return random.choice(candidates)

    async def _send_sticker_image(self, event: AstrMessageEvent, path: Path) -> None:
        """把一张本地图片发出去。"""
        await event.send(MessageChain([Image.fromFileSystem(path=str(path))]))

    # ---------- LLM 工具 ----------

    @filter.llm_tool(name="send_sticker")
    async def tool_send_sticker(self, event: AstrMessageEvent, emotion: str):
        """发一张表情包。当情绪用图片表达比文字更合适时调用。

        什么时候用：
        - 开心、害羞、生气、难过、得意这些情绪比较明显，想撒娇、想逗他、想闹一下
        - 话已经说完，只想留个表情收尾

        什么时候不要用：
        - 正在认真聊正事，或者对方情绪低落、需要好好说话的时候
        - 刚刚才发过（有冷却，会被拒绝，不用再试）
        - 情绪平平的普通对话，不要为了发表情而发表情

        Args:
            emotion (str): 情绪分类，只能填其中一个单词：happy（开心、高兴、兴奋）、shy（害羞、脸红、被戳中心事）、angry（生气、鼓脸、炸毛）、sad（难过、委屈、低落）、smug（得意、坏笑、捉弄人）
        """
        if not self.enabled:
            return "表情包功能已被关闭，现在不要发表情包"

        emo = (emotion or "").strip().lower()
        if emo not in EMOTIONS:
            return f"emotion 只能是以下之一：{', '.join(EMOTIONS)}"

        if not self._can_send():
            return "刚刚才发过表情包，还在冷却中，这次先不发"

        path = self._pick(emo)
        if path is None:
            return f"{emo} 分类下还没有表情包，这次先不发"

        try:
            await self._send_sticker_image(event, path)
        except Exception as e:
            logger.warning(f"发送表情包失败: {e}")
            return f"发送表情包失败：{e}"

        self._last_sent_turn = self._turn_counter
        self._last_sent_path[emo] = path
        logger.info(f"已发送 {emo} 表情包: {path.name}")
        return f"已发送 {emo} 表情包（{path.name}），继续用文字把话说完"

    # ---------- 事件钩子 ----------

    @filter.on_llm_response()
    async def count_turn(self, event: AstrMessageEvent, response) -> None:
        """每轮对话结束时把轮数 +1，供冷却判断使用。"""
        self._turn_counter += 1

    # ---------- 命令（测试用） ----------

    @filter.command("sticker")
    async def cmd_sticker(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        for prefix in ("/sticker", "／sticker", "sticker"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        parts = raw.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""

        if sub in ("", "help"):
            yield event.plain_result(
                "表情包用法：\n"
                "/sticker list        查看各情绪分类各有多少张\n"
                "/sticker <情绪>      手动发一张\n"
                f"情绪可选：{'、'.join(EMOTIONS)}"
            )
            return

        if sub == "list":
            lines = [
                f"表情包库存（功能：{'开' if self.enabled else '关'}，冷却 {self.cooldown} 轮）："
            ]
            total = 0
            for emotion in EMOTIONS:
                count = len(_list_stickers(emotion))
                total += count
                lines.append(f"  {emotion}（{EMOTION_LABELS[emotion]}）：{count} 张")
            lines.append(f"  合计：{total} 张")
            yield event.plain_result("\n".join(lines))
            return

        if sub in EMOTIONS:
            path = self._pick(sub)
            if path is None:
                yield event.plain_result(
                    f"{sub} 分类下还没有图，先把图片放进 {STICKER_DIR / sub} 吧"
                )
                return
            # 手动发图不占用 LLM 的冷却，只记下这张，免得紧接着又抽到它
            self._last_sent_path[sub] = path
            try:
                await self._send_sticker_image(event, path)
            except Exception as e:
                yield event.plain_result(f"发送失败：{e}")
            return

        yield event.plain_result(
            f"没有「{sub}」这个分类，可选：{'、'.join(EMOTIONS)}，或发 /sticker help"
        )
