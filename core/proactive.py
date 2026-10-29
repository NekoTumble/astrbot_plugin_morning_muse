"""晨光心语 · 主动消息引擎（P1 导火索 + P2 内容生成 + 刹车 + P3 写回历史）

一句话说明白这套东西在干嘛：
    她不再是「主人开口才动」的应答机，而是**自己也会想起你**。
    但每一次开口都要先过八根导火索里的一根，再过一套刹车，
    不然就会从「想你了」变成「骚扰机」。

四块：

1. **导火索**（`detect_triggers`）——急档（疲惫 / 低落 / 大喜 / 承诺兑现）当下就能发；
   缓档（纪念日 / 天气 / 刚醒刚要睡 / 旧事想起）攒到可打扰时段再说。
2. **刹车**（`_brake_check`）——每日预算、最小间隔、安静时段、睡着不发、
   连着几条没人理就自动闭嘴。撞上刹车的可以延后到下一个可发时段。
3. **内容**（`generate_message`）——单独调一次模型，绝不动主对话的上下文；
   人格设定 + 此刻状态 + 现在这一格日程 + 这次的由头，喂进去让她像「突然想到」才开口。
4. **写回历史**（`write_history`）——发完把这条轻轻写进会话历史，
   这样下一轮对话她才记得自己刚说过什么，不会一脸懵。

硬底线：
- 没订阅的会话**永远**不发（白名单由 SubscriptionStore 把关）；
- 所有对外副作用（发消息、写历史）统统 try/except，失败只记日志，不炸插件；
- 本模块**只读** life_bridge / event_ledger / memory_bridge，唯二写的是
  `mark_shared`（她说过的事）和自己那本账。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, NamedTuple

try:
    from astrbot.api import logger
except ImportError:  # 独立自测
    import logging

    logger = logging.getLogger(__name__)

from .proactive_store import (
    ProactiveState,
    SubscriptionStore,
    detect_chat_type,
    format_skipped_line,
)
from .life_bridge import activity_zh, energy_zh, location_zh, mood_zh

# ── 急 / 缓 ──
URGENT = "urgent"
CASUAL = "casual"

# 八根导火索：(key, 中文名, 急缓)
TRIGGER_META = {
    "tired": ("疲惫", URGENT),
    "low": ("低落", URGENT),
    "joy": ("大喜", URGENT),
    "promise": ("承诺兑现", URGENT),
    "anniversary": ("纪念日", CASUAL),
    "weather": ("天气变化", CASUAL),
    "wake_sleep": ("刚醒/刚要睡", CASUAL),
    "wake_sleep_wake": ("刚醒", CASUAL),
    "wake_sleep_sleep": ("刚要睡", CASUAL),
    "old_memory": ("旧事想起", CASUAL),
}

# 手动触发（/晨光 说句话）在没有导火索命中时的兜底由头
_MANUAL_REASONS = (
    "就是突然想跟你说句话",
    "没什么事，只是想起你了",
    "闲着，想跟你说声在做什么",
)

# 日程上的六个模糊时段（跟注入、天气、状态机同一套）
_PERIOD_ORDER = ("清晨", "上午", "午间", "下午", "傍晚", "晚间")

# F8：法定节假日日历按年缓存。
# holidays 是「第一次查询某年时才展开那一整年」的惰性库，每次新建实例都要重来；
# 而「纪念日」探测每小时 × 每个人格都会问一次，纯属白烧 CPU。缓存后结果一致。
_HOLIDAY_CAL_CACHE: dict[int, Any] = {}


def _cn_calendar(year: int):
    """按年缓存一份 holidays.China 日历；拿不到就返回 None，调用方自行降级。"""
    cal = _HOLIDAY_CAL_CACHE.get(year)
    if cal is not None:
        return cal
    try:
        import holidays as _holidays
        cal = _holidays.China(years=[year])
    except Exception:
        return None
    _HOLIDAY_CAL_CACHE[year] = cal
    if len(_HOLIDAY_CAL_CACHE) > 3:          # 只留最近几年，别无限攒
        _HOLIDAY_CAL_CACHE.pop(min(_HOLIDAY_CAL_CACHE))
    return cal

# 出力清洗：这些前缀 / 包装一律剥掉
_SPEAKER_PREFIX_RE = re.compile(r"^\s*[（(]?[^：:（）()\n]{1,12}[）)]?\s*[：:]\s*")
_QUOTE_WRAP_RE = re.compile(r'^\s*[「『"""\'](.*)[」』"""\']\s*$', re.DOTALL)
_META_LINE_RE = re.compile(r"^\s*(?:系统|助手|assistant|user|提示|注)[：:].*$", re.IGNORECASE)


@dataclass
class TriggerHit:
    """一次「想开口」的念头。"""

    key: str
    name: str
    level: str = CASUAL
    detail: str = ""                 # 给 LLM 看的一句话由头
    payload: dict[str, Any] = field(default_factory=dict)   # 发送后要善后的东西

    def to_pending(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "level": self.level,
            "detail": self.detail,
            "payload": dict(self.payload or {}),
        }

    @classmethod
    def from_pending(cls, raw: Any) -> "TriggerHit | None":
        if not isinstance(raw, dict) or not raw.get("key"):
            return None
        key = str(raw.get("key"))
        name, level = TRIGGER_META.get(key, (str(raw.get("name") or key), CASUAL))
        return cls(
            key=key,
            name=str(raw.get("name") or name),
            level=str(raw.get("level") or level),
            detail=str(raw.get("detail") or ""),
            payload=dict(raw.get("payload") or {}),
        )


# 「主人正在聊天」这条刹车的固定说法 —— 会被写进延后队列的缘由，
# 一路带到补发时的提示词里，让她自己判断这句现在还说值不值。
BLOCK_CHATTING = "主人正在跟你说话，等他先说完"


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是")
    if value is None:
        return default
    return bool(value)


def _as_float(value: Any, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return default if v != v else v


def _as_int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[\n,，;；]+", value) if p.strip()]
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def parse_hm(text: str, default: tuple[int, int]) -> tuple[int, int]:
    """把 "23:00" 这种文本解析成 (时, 分)。解析不出来就给默认值。"""
    m = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})", str(text or ""))
    if not m:
        return default
    try:
        h, mi = int(m.group(1)), int(m.group(2))
    except (TypeError, ValueError):
        return default
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return default
    return h, mi


def parse_clock_minutes(text: Any, default_minutes: int) -> int:
    """把 "22:33" / "22" / "7:05" 解析成「从午夜起的分钟数」。

    认不出来就退回默认值。填纯数字按整点算（22 → 22:00）。
    """
    raw = str(text if text is not None else "").strip()
    if not raw:
        return default_minutes % 1440
    m = re.match(r"^(\d{1,2})\s*[:：.．]\s*(\d{1,2})$", raw)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
        return default_minutes % 1440
    if raw.isdigit() and 0 <= int(raw) <= 23:
        return int(raw) * 60
    return default_minutes % 1440


def format_clock(minutes: int) -> str:
    """分钟数 → "HH:MM"。"""
    minutes %= 1440
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _in_sleep_window(minute: int, bedtime_min: int, wake_min: int) -> bool:
    """此刻在不在她的**睡眠窗口**里（跨午夜也算）。

    三个参数都是「从午夜起的分钟数」。
    23:00 睡、07:00 起 → 23:00 到第二天 07:00 之间都算睡着。
    这是「她几点睡」唯一的算法出口——刹车、导火索、以后可能的回血都读它。
    """
    if bedtime_min == wake_min:
        return False
    if bedtime_min > wake_min:          # 跨午夜，比如 23:00 → 07:00
        return minute >= bedtime_min or minute < wake_min
    return bedtime_min <= minute < wake_min


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    """现在是不是安静时段（支持跨零点，比如 23:00–07:00）。"""
    sh, sm = parse_hm(start, (23, 0))
    eh, em = parse_hm(end, (7, 0))
    start_m = sh * 60 + sm
    end_m = eh * 60 + em
    cur = now.hour * 60 + now.minute
    if start_m == end_m:
        return False                     # 起止一样 = 等于没设安静时段
    if start_m < end_m:
        return start_m <= cur < end_m
    return cur >= start_m or cur < end_m


class ProactiveEngine:
    """主动消息的主控。心跳里被叫一次，就推进一步。"""

    def __init__(self, plugin, subscriptions: SubscriptionStore, state: ProactiveState,
                 life_bridge=None, event_ledger=None, memory_bridge=None,
                 weather_client=None, storage=None, generator=None,
                 context_builder=None):
        self.plugin = plugin
        self.subscriptions = subscriptions
        self.state = state
        self.life_bridge = life_bridge
        self.event_ledger = event_ledger
        self.memory_bridge = memory_bridge
        self.weather_client = weather_client
        self.storage = storage
        self.generator = generator
        self.context_builder = context_builder
        self._send_lock = None       # 懒加载，避免 import 期就建 event loop 对象

    # ─────────────────────── 小工具 ───────────────────────

    def _cfg(self, key: str, default=None):
        try:
            return self.plugin.get_config(key, default)
        except Exception:
            return default

    def _now(self) -> datetime:
        try:
            return self.plugin._get_now()
        except Exception:
            return datetime.now()

    def _today_iso(self) -> str:
        try:
            return self.plugin._get_today().isoformat()
        except Exception:
            return datetime.now().date().isoformat()

    def _context(self):
        return getattr(self.plugin, "context", None)

    def enabled(self) -> bool:
        return _as_bool(self._cfg("proactive.enabled", False), False)

    def available(self) -> bool:
        """能不能真的跑：开关开着 + 内在状态机在跑 + 有订阅。

        状态机是必须的——精力、心情、此刻在做什么全靠它，没有它就没有「想念」可言。
        """
        if not self.enabled():
            return False
        if self.life_bridge is None or not _as_bool(
            self._cfg("life_engine.enabled", False), False
        ):
            return False
        return True

    @staticmethod
    def _iso(dt: datetime) -> str:
        return dt.isoformat(timespec="seconds")

    def _period_now(self, now: datetime) -> str:
        """按当前钟点认出是六个时段里的哪一个（跟注入口径一致）。"""
        hour = now.hour
        if hour < 4:
            return "晚间"
        bounds = [4, 8, 11, 13, 17, 19]
        idx = 0
        for i, b in enumerate(bounds):
            if hour >= b:
                idx = i
        return _PERIOD_ORDER[idx] if 0 <= idx < len(_PERIOD_ORDER) else "晚间"

    async def _schedule_now(self, persona_id: str) -> str:
        """她此刻那一格日程（拿来当「她现在在做什么」的背景）。"""
        if self.storage is None:
            return ""
        try:
            today = self.plugin._get_today()
            schedule = await self.storage.load_schedule(persona_id, today)
            if not schedule:
                return ""
            label = self._period_now(self._now())
            for item in schedule.get("schedule") or []:
                if isinstance(item, dict) and str(item.get("time") or "") == label:
                    return str(item.get("activity") or "")
            outfit = str(schedule.get("outfit") or "")
            return outfit[:60]
        except Exception:
            return ""

    # ─────────────────────── P1 · 导火索 ───────────────────────

    async def detect_triggers(self, persona_id: str,
                              respect_cap: bool = True) -> list[TriggerHit]:
        """按 急 → 缓 的顺序，列出此刻成立的导火索。任何一根出错都不影响其他根。

        respect_cap=False 时无视「每天上限」—— 只给 `/晨光 说句话` 那种手动点火用
        （它本来就不受刹车限制、也不记账，是拿来验通道的）。
        """
        hits: list[TriggerHit] = []
        now = self._now()
        state = None
        if self.life_bridge is not None:
            try:
                state = await self.life_bridge.get_state(persona_id)
            except Exception as e:
                logger.debug(f"[晨光心语] {persona_id} 主动消息：读状态失败 {e}")

        # 注意：「旧事想起」**不在这张表里** —— 它是垫底由头，见循环后面那段。
        for key, probe in (
            ("tired", self._probe_tired),
            ("low", self._probe_low),
            ("joy", self._probe_joy),
            ("promise", self._probe_promise),
            ("anniversary", self._probe_anniversary),
            ("weather", self._probe_weather),
            ("wake_sleep", self._probe_wake_sleep),
        ):
            name, level = TRIGGER_META[key]
            if not _as_bool(self._cfg(f"proactive_triggers.enable_{key}", True), True):
                continue
            try:
                hit = await probe(persona_id, state, now)
            except Exception as e:
                logger.debug(f"[晨光心语] 主动消息：{persona_id} 主动消息：导火索「{name}」判定异常 {e}")
                hit = None
            if hit:
                # ★ 一条探针可以分成好几根导火索（比如「刚醒」和「刚要睡」）：
                #   探针自己声明的 key 说了算，这里只补它没写的名字和等级。
                #   （以前这里是把 key 整个改写成注册表那一根 —— 刚醒和刚要睡
                #     因此挤在同一个名字上，去重一拦，早上的「刚醒」就把晚上的
                #     「刚要睡」连坐掉了。）
                hit.key = hit.key or key
                hit.name = hit.name or name
                hit.level = hit.level or level
                hits.append(hit)

        # ★ 垫底由头：「忽然想起旧事」不跟其他导火索平起平坐。
        #   别的由头一个都没有的时候，才轮到她翻旧账。
        #   原因：接上外部记忆库之后，库里几百条存货、去重名单却只有几条，
        #   这根探针的命中率接近 100% —— 不加限制，她会一天到晚都在"想分享"。
        #   而"忽然想起"本来就该是偶尔的点缀，不是每天的主菜。
        if not hits and _as_bool(self._cfg("proactive_triggers.enable_old_memory", True), True):
            hit = None
            # ★「用完就停止」：今天这根已经发满上限了，**连探都不探** ——
            #   不再每跳去查一次外部记忆库，也不再有任何「兜底旧事想起」。
            #   （上限只管这一根；别的导火索、别的行为一概不动。）
            if respect_cap and await self._old_memory_exhausted(persona_id):
                pass
            else:
                try:
                    hit = await self._probe_old_memory(persona_id, state, now)
                except Exception as e:
                    logger.debug(f"[晨光心语] 主动消息：{persona_id} 垫底导火索「旧事想起」判定异常 {e}")
                    hit = None
            if hit:
                hit.key = hit.key or "old_memory"
                hit.name = hit.name or "旧事想起"
                hit.level = hit.level or CASUAL
                hits.append(hit)

        hits.sort(key=lambda h: 0 if h.level == URGENT else 1)
        return hits

    async def _probe_tired(self, pid, state, now) -> TriggerHit | None:
        if not state:
            return None
        threshold = _as_float(self._cfg("proactive_triggers.energy_threshold", 0.20), 0.20)
        energy = _as_float(state.get("energy"), 1.0)
        if energy > threshold:
            return None
        label = state.get("energy_label") or ""
        return TriggerHit(
            key="tired", name="疲惫", level=URGENT,
            detail=f"精力只剩 {energy:.2f}（{label}），已经没什么力气了",
            payload={"energy": energy},
        )

    async def _probe_low(self, pid, state, now) -> TriggerHit | None:
        if not state:
            return None
        threshold = _as_float(self._cfg("proactive_triggers.mood_low_threshold", -0.5), -0.5)
        valence = _as_float(state.get("mood_valence"), 0.0)
        # 状态机给的是 mood 标签；没有 valence 字段时按标签粗判一下
        if "mood_valence" not in state:
            valence = self._valence_from_label(state.get("mood", ""), state.get("mood_intensity"))
        if valence > threshold:
            return None
        return TriggerHit(
            key="low", name="低落", level=URGENT,
            detail=f"心情「{mood_zh(state.get('mood')) or '不太好'}」，闷得发沉",
            payload={"valence": valence},
        )

    async def _probe_joy(self, pid, state, now) -> TriggerHit | None:
        if not state:
            return None
        v_th = _as_float(self._cfg("proactive_triggers.joy_valence_threshold", 0.6), 0.6)
        i_th = _as_float(self._cfg("proactive_triggers.joy_intensity_threshold", 0.5), 0.5)
        valence = _as_float(state.get("mood_valence"), 0.0)
        if "mood_valence" not in state:
            valence = self._valence_from_label(state.get("mood", ""), state.get("mood_intensity"))
        intensity = _as_float(state.get("mood_intensity"), 0.0)
        if valence < v_th or intensity < i_th:
            return None
        return TriggerHit(
            key="joy", name="大喜", level=URGENT,
            detail=f"心情特别好（「{mood_zh(state.get('mood'))}」），高兴得坐不住",
            payload={"valence": valence, "intensity": intensity},
        )

    # 情绪词 → 愉悦度的分级折算表（不是一刀切）
    _VAL_STRONG_POS = ("joyful", "excited", "happy", "delighted", "thrilled",
                       "ecstatic", "elated", "overjoyed", "euphoric")
    _VAL_MILD_POS = ("content", "calm", "peaceful", "warm", "amused", "curious",
                     "awed", "wonder", "relaxed", "cozy", "satisfied", "grateful",
                     "hopeful", "moved", "touched", "tender")
    _VAL_NEUTRAL = ("neutral", "ordinary", "blank", "平静", "无")
    _VAL_AGITATED = ("restless", "startled", "anxious", "uneasy", "jittery")
    _VAL_MILD_NEG = ("blue", "sad", "lonely", "down", "low", "tired", "weary",
                     "bored", "disappointed", "hurt", "melancholy", "gloomy")
    _VAL_STRONG_NEG = ("raw", "overwhelmed", "frustrated", "angry", "furious",
                       "devastated", "desperate", "heartbroken", "hopeless")
    # 中文情绪词（人格自定义 / 旧数据），同样分级
    _VAL_STRONG_POS_ZH = ("雀跃", "兴奋", "狂喜", "激动", "超级开心", "超开心",
                          "惊喜", "喜出望外", "乐翻", "开怀", "大喜")
    _VAL_MILD_POS_ZH = ("开心", "快乐", "高兴", "期待", "满足", "愉快", "放松",
                        "安心", "温暖", "幸福", "得意", "元气", "感动", "惬意",
                        "舒服", "治愈", "平静", "安稳")
    _VAL_MILD_NEG_ZH = ("低落", "难过", "闷", "疲惫", "累", "不安", "孤单",
                        "失落", "怅然", "委屈", "丧", "空落", "沮丧", "伤心",
                        "无聊", "倦", "焦虑", "烦躁", "生气")
    _VAL_STRONG_NEG_ZH = ("崩溃", "绝望", "愤怒", "暴怒", "心碎", "痛不欲生", "撑不住")

    @classmethod
    def _valence_from_label(cls, label: str, intensity: Any = None) -> float:
        """状态机只给情绪词、没给数值时，把情绪词**分级**折算成一个大致的愉悦度。

        aura-life 的 mood 只有五个离散值（content / neutral / blue / raw / restless），
        没有连续的 valence，所以这里做一张分级表：

        - 大喜（joyful / excited / 雀跃…）        → +0.75
        - 温和正面（content / calm / 满足 / 安心…） → +0.40   ← 这些**不是大喜**
        - 中性（neutral / 平静）                  →  0.00
        - 躁（restless / 不安）                   → -0.30
        - 温和负面（blue / lonely / 闷 / 累…）     → -0.50
        - 强负面（raw / overwhelmed / 崩溃…）      → -0.75

        注意：`content`（满足）以前被一刀切算成 +0.6，恰好卡在「大喜」阈值上，
        所以只要心情稍微好一点点就会误报「大喜」。分级之后不再这样。

        这是近似，不是它的真实数值——哪天状态机给出 `mood_valence` 字段，
        就会直接采用那个真值，不再走这张表。
        """
        text = str(label or "").strip().lower()
        if not text:
            return 0.0

        if text in cls._VAL_STRONG_POS:
            return 0.75
        if text in cls._VAL_MILD_POS:
            return 0.40
        if text in cls._VAL_NEUTRAL:
            return 0.0
        if text in cls._VAL_AGITATED:
            return -0.30
        if text in cls._VAL_MILD_NEG:
            return -0.50
        if text in cls._VAL_STRONG_NEG:
            return -0.75

        # 中文词按「强 → 弱」的顺序扫，避免「超级开心」被「开心」提前吃掉
        for w in cls._VAL_STRONG_POS_ZH:
            if w in text:
                return 0.75
        for w in cls._VAL_MILD_POS_ZH:
            if w in text:
                return 0.40
        for w in cls._VAL_MILD_NEG_ZH:
            if w in text:
                return -0.50
        for w in cls._VAL_STRONG_NEG_ZH:
            if w in text:
                return -0.75
        return 0.0

    async def _probe_promise(self, pid, state, now) -> TriggerHit | None:
        """D · 承诺兑现：账本里某条「没做完」的事，在这一次心跳之前被标成了完成。"""
        if self.event_ledger is None:
            return None
        if not _as_bool(self._cfg("event_ledger.enabled", False), False):
            return None
        try:
            events = await self.event_ledger.load(pid)
        except Exception:
            return None
        if not events:
            return None

        st = await self.state.ensure(pid, self._today_iso())
        last_check = str(st.get("last_ledger_check") or "")
        fresh_closed: list[dict[str, Any]] = []
        for e in events:
            closed_at = str(e.get("closed_at") or "")
            if not closed_at:
                continue
            if str(e.get("status")) != "closed":
                continue
            if not last_check or closed_at > last_check:
                fresh_closed.append(e)
        # 记账：无论有没有命中，都把水位线推到现在，免得下一轮重复捡
        await self.state.set_ledger_check(pid, self._iso(now))
        if not fresh_closed:
            return None
        e = fresh_closed[-1]
        summary = str(e.get("summary") or "")
        if not summary:
            return None
        return TriggerHit(
            key="promise", name="承诺兑现", level=URGENT,
            detail=f"惦记了好几天的「{summary}」，刚刚弄完了",
            payload={
                "event_id": str(e.get("id") or ""),
                "summary": summary,
                # 记住推进前的水位线：这条最终没发出去时，要能退回来。
                "prev_ledger_check": last_check,
            },
        )

    async def _probe_anniversary(self, pid, state, now) -> TriggerHit | None:
        """E · 纪念日：手填的私人日子 → 自动认的节日 → 「去年的今天」。"""
        today = now.date()

        # ① 主人手填的私人纪念日（优先——这是只属于你们俩的日子）
        for raw in _as_list(self._cfg("proactive_triggers.anniversaries", [])):
            parsed = self._parse_anniversary(raw)
            if not parsed:
                continue
            month, day, name = parsed
            if (month, day) == (today.month, today.day):
                return TriggerHit(
                    key="anniversary", name="纪念日", level=CASUAL,
                    detail=f"今天是「{name}」",
                    payload={"anniversary": name, "source": "manual"},
                )

        # ② 自动认的节日（复用日程那边那套三层检测：法定 / 公历洋节 / 农历）
        auto = self._auto_holiday_name(today)
        if auto:
            return TriggerHit(
                key="anniversary", name="纪念日", level=CASUAL,
                detail=f"今天是「{auto}」",
                payload={"anniversary": auto, "source": "auto"},
            )

        # ③ 「去年的今天」
        if _as_bool(self._cfg("proactive_triggers.anniversary_last_year", False), False):
            text = await self._find_last_year_event(pid, today)
            if text:
                return TriggerHit(
                    key="anniversary", name="纪念日", level=CASUAL,
                    detail=f"一年前的今天：{text}（账本里还留着这条）",
                    payload={"last_year": text},
                )
        return None

    def _auto_holiday_name(self, today) -> str:
        """今天是什么节日？直接复用日程那边那套三层检测，不另造一张表。

        排除清单默认挡住「清明 / 中元」这种祭扫的日子——那种日子她不该欢快地开口。
        长假只在头一天提一次，免得七天假期天天说「今天是国庆节」。
        """
        if not _as_bool(self._cfg("proactive_triggers.auto_holidays", True), True):
            return ""
        if self.context_builder is None:
            return ""
        try:
            info = str(self.context_builder._get_holiday_info(today) or "").strip()
        except Exception as e:
            logger.debug(f"[晨光心语] 主动消息：节日检测失败 {e}")
            return ""
        # N1 修复：`_get_holiday_info` 的契约是「只返回节日名、没有节日返回空串」，
        # 所以这里只判空。原先拿中文文案「工作日」当哨兵，R6 把兜底文案改成
        # 「今天为休息日（周末）」之后哨兵就失灵了 —— 周末会被当成「纪念日」冒出来。
        # 教训：别再拿中文文案当哨兵。
        if not info:
            return ""
        exclude = _as_list(self._cfg(
            "proactive_triggers.holiday_exclude", ["清明", "中元"]
        )) or ["清明", "中元"]
        if any(x and x in info for x in exclude):
            return ""
        name = info.replace("法定节假日：", "").strip()
        # 只有法定长假才需要「只提头一天」——洋节都是单日，不用管
        if "法定节假日" in info and _as_bool(
            self._cfg("proactive_triggers.holiday_first_day_only", True), True
        ):
            if not self._is_first_day_of_holiday(today, name):
                return ""
        return name

    @staticmethod
    def _is_first_day_of_holiday(today, name: str) -> bool:
        """今天是不是这个节日的**头一天**？

        不能只看昨天——长假中间常常夹着周末（周末不算「法定节假日」，
        日历里会断档），那样 10 月 7 日会被错当成第一天。
        所以改成：在**本年度**里找这个节日最早出现的那天，是今天才算头一天。
        """
        try:
            # 注意：holidays 只「展开」years 里点名的年份，遍历时看不到别的年份，
            # 所以这里必须点名当年，否则 first 会一直是 None。（F8：改走按年缓存）
            cal = _cn_calendar(today.year)
            if cal is None:
                raise RuntimeError("holidays 日历不可用")   # 保持原来的 fail-closed
            # N4：长假中段的假期名往往带后缀（「国庆节（补假）」「春节（调休）」），
            # 拿全名去比日历里的「国庆节」会匹配不上 → 首日判定失效，
            # 于是长假后半段会把同一个节日再提一遍。剥掉括号后缀，只拿基名比。
            base = re.sub(r"[（(][^）)]*[）)]", "", str(name or "")).strip() or str(name or "")
            first = None
            for d, n in cal.items():
                if base and base in str(n) and d.year == today.year:
                    if first is None or d < first:
                        first = d
            if first is None:
                return True
            return first == today
        except Exception as e:
            # 以前这里 fail-open（return True = 当作节日头一天）—— 依赖一坏，
            # 同一个节日当天就会被反复提起。改成 fail-closed：宁愿这天不报，
            # 也别重复骚扰。（节日提醒本身不靠这一条，少的只是「头一天」那次。）
            logger.debug(f"[晨光心语] 节日首日判定失败（按「不是头一天」处理）: {e}")
            return False

    @staticmethod
    def _parse_anniversary(raw: str) -> tuple[int, int, str] | None:
        """解析一条纪念日配置：`12-25:圣诞节` / `12-25 圣诞节` / `2026-12-25:圣诞节`。"""
        text = str(raw or "").strip()
        if not text:
            return None
        m = re.match(r"^(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})日?\s*[:：,，\-\s]?\s*(.*)$", text)
        if not m:
            return None
        try:
            month, day = int(m.group(2)), int(m.group(3))
        except (TypeError, ValueError):
            return None
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        name = (m.group(4) or "").strip() or f"{month}月{day}日"
        return month, day, name

    async def _find_last_year_event(self, pid: str, today) -> str:
        """翻账本找「去年的今天」。找不到就空着，不算错。"""
        if self.event_ledger is None:
            return ""
        try:
            events = await self.event_ledger.load(pid)
        except Exception:
            return ""
        target = today - timedelta(days=365)
        for e in events:
            try:
                d = datetime.strptime(str(e.get("date") or ""), "%Y-%m-%d").date()
            except Exception:
                continue
            if (d.month, d.day) == (target.month, target.day):
                return str(e.get("summary") or "")
        return ""

    # 天气里认的「确定事件」词 —— 出现就是真发生了。
    # 刻意**不含「霾 / 雾 / 雨」**：那三个是模糊词（预报常写「可能有零星雨」，
    # 逐时段里一格「烟霾」就能点火），改由官方预警和数字指标判。
    _WEATHER_EVENT_WORDS = ("雪", "雷", "冰雹", "沙尘", "扬尘", "浮尘")

    async def _probe_weather(self, pid, state, now) -> TriggerHit | None:
        """F · 天气变化：官方预警 > 数字指标 > 确定事件词，外加明显降温。

        ★ v1.5.16 改口径：以前拿整段中文预报去捞「雨 / 雪 / 雾 / 霾」，
        可那段文本里逐时段全都在，于是「烟霾」天天命中 —— 这根由头每天必点，
        跟真天气没关系。现在分三层：
          ① 官方预警（中央气象台）命中本地 → 无条件算数（「大霾」的正牌口径）；
          ② 数字指标：最低能见度 / 最高降水概率 / 最大风速（wttr.in 自带，不新增接口）；
          ③ 描述词：只认雪、雷、冰雹、沙尘这类「确定事件」。
        主人在设置页填了「什么天气会触发」时，仍旧按他填的词做文本匹配（留后门）。
        """
        if self.weather_client is None:
            return None
        if not _as_bool(self._cfg("weather.weather_enabled", False), False):
            return None
        try:
            text = await self.weather_client.get_weather()
        except Exception:
            return None
        if not text:
            return None

        detail = ""
        hit_word = ""

        # ① 官方预警优先
        hit_word, detail = await self._probe_official_alarm()

        # ② 主人自己填的词表（填了就用他的，保留老行为）
        if not hit_word:
            custom = _as_list(self._cfg("proactive_triggers.weather_keywords", []))
            word = next((w for w in custom if w and w in text), "")
            if word:
                hit_word, detail = word, f"外面{word}"

        # ③ 数字指标 + 确定事件词
        if not hit_word:
            hit_word, detail = self._judge_weather(text)

        # 降温：跟上次记下的温度比一比
        drop_threshold = _as_float(self._cfg("proactive_triggers.weather_temp_drop", 5), 5.0)
        snapshot = self._parse_weather_temp(text)
        st = await self.state.ensure(pid, self._today_iso())
        old = st.get("weather_snapshot") or {}
        old_date = str(old.get("date") or "")
        dropped = None
        try:
            if snapshot and old.get("max") is not None:
                delta = _as_float(old.get("max"), snapshot["max"]) - snapshot["max"]
                if delta >= drop_threshold:
                    dropped = delta
                    hit_word = hit_word or "降温"
        except Exception:
            dropped = None
        if snapshot:
            await self.state.set_weather_snapshot(pid, {
                "date": self._today_iso(), **snapshot, "text": text[:120],
            })

        if not hit_word:
            return None
        if not detail:
            # 走到这里只可能是「降温」—— 预警 / 自定义词 / 指标那三条都自带 detail
            if dropped is None:
                # 命中「降温」但抠不出气温区间（没有旧快照 / 区间解析失败）——
                # 以前这里会直接对 None 做 :.0f 抛 TypeError，被上层的 per-probe
                # except 以 debug 级吞掉，整根天气由头当跳静默死亡。回退成不带数字的说法。
                detail = "外面降温"
            else:
                # 基线快照可能是好几天前写的（跨天不清、停机重载后重取数）——
                # 按真实日期说，不再一律硬说「比昨天」。
                today_iso = str(self._today_iso())
                if not old_date:
                    detail = f"外面降温了快 {dropped:.0f} 度"
                elif old_date == today_iso:
                    detail = f"比今天早些时候冷了快 {dropped:.0f} 度"
                else:
                    label = old_date
                    try:
                        if old_date == (datetime.fromisoformat(today_iso) - timedelta(days=1)).date().isoformat():
                            label = "昨天"
                        else:
                            label = datetime.fromisoformat(old_date).strftime("%m/%d")
                    except Exception:
                        label = old_date
                    detail = f"比{label}冷了快 {dropped:.0f} 度"
        return TriggerHit(
            key="weather", name="天气变化", level=CASUAL,
            detail=f"{detail}（{text[:60]}）",
            payload={"weather": text[:120], "keyword": hit_word},
        )

    async def _probe_official_alarm(self) -> tuple[str, str]:
        """官方预警有没有？有就返回 (预警类型, 一句话)。取不到就当没有。

        这一层才是「大霾」的正牌口径：官方发了通报才开口，
        而不是我们自己在预报文本里猜。取数失败绝不往外抛 ——
        天气这根由头还有别的路可走，不能陪它一起死。
        """
        if not _as_bool(self._cfg("proactive_triggers.weather_alarm_enabled", True), True):
            return "", ""
        client = getattr(self.plugin, "alarm_client", None)
        if client is None:
            return "", ""
        try:
            alerts = await client.get_alerts()
        except Exception as e:
            logger.debug(f"[晨光心语] 官方预警取数失败：{e}")
            return "", ""
        if not alerts:
            return "", ""
        a = alerts[0] if isinstance(alerts[0], dict) else {}
        atype = str(a.get("type") or "预警")
        level = str(a.get("level") or "")
        area = str(a.get("area") or "")
        return atype, f"官方刚发了{atype}{level}预警（{area}）"

    def _judge_weather(self, text: str) -> tuple[str, str]:
        """用数字指标 + 「确定事件」描述词判天气，返回 (命中词, 给人看的一句话)。

        指标从天气客户端拿；拿不到指标就只走事件词那条路，**不退回捞中文模糊词** ——
        宁可这次不开口，也不要又变回天天点。
        """
        getter = getattr(self.weather_client, "get_metrics", None)
        metrics = {}
        if callable(getter):
            try:
                metrics = getter() or {}
            except Exception:
                metrics = {}

        # 霾 / 雾：只看能见度（官方霾黄色预警的口径就是能见度 < 3 km）
        need_vis = _as_float(self._cfg("proactive_triggers.weather_visibility_km", 3.0), 3.0)
        vis = metrics.get("min_visibility")
        if isinstance(vis, (int, float)) and vis < need_vis:
            return "雾霾", f"外面能见度不到 {vis:.0f} 公里"

        # 雪 / 雷 / 冰雹 / 沙尘：这类出现就是真发生了
        descs = metrics.get("descs") or []
        joined = " ".join(str(d) for d in descs) or text
        for word in self._WEATHER_EVENT_WORDS:
            if word in joined:
                return word, f"外面{word}"

        # 雨：看全天最高降水概率（以前「可能有零星雨」也算数）
        need_rain = _as_int(self._cfg("proactive_triggers.weather_rain_prob", 50), 50)
        prob = metrics.get("rain_prob")
        if isinstance(prob, (int, float)) and prob >= need_rain:
            return "雨", f"外面降水概率 {int(prob)}%"

        # 大风：看风速（大风蓝色预警的口径：平均风 ≥ 6 级 ≈ 39 km/h）
        need_wind = _as_float(self._cfg("proactive_triggers.weather_wind_kmph", 39), 39.0)
        wind = metrics.get("max_wind_kmph")
        if isinstance(wind, (int, float)) and wind >= need_wind:
            return "大风", f"外面在刮大风（{int(wind)} km/h）"

        return "", ""

    @staticmethod
    def _parse_weather_temp(text: str) -> dict[str, float] | None:
        """从天气文本里抠出最高 / 最低气温。抠不到就 None。"""
        m = re.search(r"气温\s*(-?\d+(?:\.\d+)?)\s*[~～\-]\s*(-?\d+(?:\.\d+)?)", str(text or ""))
        if not m:
            return None
        try:
            lo, hi = float(m.group(1)), float(m.group(2))
        except (TypeError, ValueError):
            return None
        return {"min": min(lo, hi), "max": max(lo, hi)}

    # ─────────── 作息：她几点睡、几点起（唯一来源，支持 HH:MM） ───────────

    def _wake_minutes(self, persona_id: str = "") -> int:
        """她**今天生效**的起床时间（从午夜起的分钟数）。

        会带上「睡懒觉」——昨夜被打断多久，今天就多睡多久（封顶 2 小时），
        账由 life_bridge 记，这里只是来取。
        """
        if persona_id and self.life_bridge is not None:
            try:
                hour, minute = self.life_bridge.effective_wake_time(persona_id)
                return hour * 60 + minute
            except Exception:
                pass
        return parse_clock_minutes(self._cfg("life_engine.wake_time", "07:00"), 7 * 60)

    def _bed_minutes(self) -> int:
        """她的睡觉时间（从午夜起的分钟数）。来源只有一处：「🫀 内在状态」的「几点睡觉」。"""
        return parse_clock_minutes(self._cfg("life_engine.bedtime_time", "23:00"), 23 * 60)

    def _in_sleep_window_now(self, persona_id: str = "") -> bool:
        """她**此刻**睡着了吗？（按作息算，不猜日程里写没写「睡」）

        注意：aura-life 对 AI 人格会把 is_asleep() 硬编码成 False
        （注释原话：AI personas never sleep），所以这条路只能我们自己走。
        """
        try:
            now = self._now()
        except Exception:
            now = datetime.now()
        return _in_sleep_window(
            now.hour * 60 + now.minute,
            self._bed_minutes(),
            self._wake_minutes(persona_id),
        )

    def _quiet_hours(self, persona_id: str = "") -> tuple[str, str]:
        """安静时段。**留空 = 跟着她的作息走**（她睡了就别吵）。

        她睡懒觉的时候，结束时间也跟着往后延——人还没醒呢。
        想让它跟作息不一样，再自己填 HH:MM。
        """
        start = str(self._cfg("proactive_delivery.quiet_hours_start", "") or "").strip()
        end = str(self._cfg("proactive_delivery.quiet_hours_end", "") or "").strip()
        if not start:
            start = format_clock(self._bed_minutes())
        if not end:
            end = format_clock(self._wake_minutes(persona_id))
        return start, end

    def _quiet_hours_manual(self) -> bool:
        """安静时段是主人**手填**的（不是留空跟随作息）吗？"""
        return bool(
            str(self._cfg("proactive_delivery.quiet_hours_start", "") or "").strip()
            or str(self._cfg("proactive_delivery.quiet_hours_end", "") or "").strip()
        )

    def sleep_summary(self, persona_id: str = "") -> dict:
        """给 /晨光 状态 用：当前**实际生效**的作息与安静时段（含来源）。

        为什么需要它：设置页里那两格留空时就是**空的**——那是「跟随作息」的正确样子，
        设置页本身不会把算出来的时间填回去。要看真正生效的值，看这里。
        """
        q_start, q_end = self._quiet_hours(persona_id)
        manual = bool(
            str(self._cfg("proactive_delivery.quiet_hours_start", "") or "").strip()
            or str(self._cfg("proactive_delivery.quiet_hours_end", "") or "").strip()
        )
        lag = 0
        if persona_id and self.life_bridge is not None:
            try:
                lag = self.life_bridge.wake_lag_minutes(persona_id)
            except Exception:
                lag = 0
        return {
            "bed": format_clock(self._bed_minutes()),
            "wake": format_clock(self._wake_minutes(persona_id)),
            "quiet_start": q_start,
            "quiet_end": q_end,
            "quiet_manual": manual,
            "sleeping_now": self._in_sleep_window_now(persona_id),
            "wake_lag_minutes": lag,
        }

    # ─────────── G · 刚醒 / 刚要睡（时间跟着作息走） ───────────

    def _resolve_wake_minutes(self, persona_id: str = "") -> int:
        """「刚醒」的时间：留空 = 跟随起床时间（含睡懒觉，所以赖床那天早安也晚）。"""
        raw = str(self._cfg("proactive_triggers.wake_hour", "") or "").strip()
        if raw in ("", "-1", "auto", "跟随"):
            return self._wake_minutes(persona_id)
        return parse_clock_minutes(raw, self._wake_minutes(persona_id))

    def _resolve_sleep_minutes(self) -> int:
        """「刚要睡」的时间：留空 = 跟随「🫀 内在状态」的睡觉时间（提前一小时道晚安）。"""
        default = (self._bed_minutes() - 60) % 1440
        raw = str(self._cfg("proactive_triggers.sleep_hour", "") or "").strip()
        if raw in ("", "-1", "auto", "跟随"):
            return default
        return parse_clock_minutes(raw, default)

    @staticmethod
    def _is_asleep(state: dict | None) -> bool:
        """状态机说她正在睡（打盹不算——打盹本来就不该拦着她说句话）。"""
        activity = str((state or {}).get("current_activity") or "").lower()
        return "sleep" in activity and "nap" not in activity

    # ─────────── 「真睡着」：作息窗口 + 不是刚被叫醒 ───────────

    def _disturbed_recently(self, persona_id: str, st: dict, now: datetime) -> bool:
        """她「刚被主人叫醒」了吗？

        跟「睡眠回血」用的是同一个信号源（生活状态机记的「主人最近一次开口」），
        所以两边口径一致：落在清醒尾巴（默认 15 分钟）内就算被吵醒着。
        实在拿不到信号，退回账本里的 last_user_at。
        """
        tail = 15
        last = None
        if self.life_bridge is not None:
            try:
                tail = int(getattr(self.life_bridge, "AWAKE_TAIL_MINUTES", 15) or 15)
            except Exception:
                tail = 15
            try:
                last = self.life_bridge.last_user_at(persona_id)
            except Exception:
                last = None
        if last is None:
            try:
                raw = str((st or {}).get("last_user_at") or "")
                last = datetime.fromisoformat(raw) if raw else None
            except Exception:
                last = None
        if last is None:
            return False
        try:
            if last.tzinfo is None and now.tzinfo is not None:
                last = last.replace(tzinfo=now.tzinfo)
            elif last.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=last.tzinfo)
            return (now - last).total_seconds() <= max(0, tail) * 60
        except Exception:
            return False

    def _in_true_sleep(self, persona_id: str, st: dict, now: datetime) -> bool:
        """她此刻「真睡着」吗？

        ★ 只看**作息窗口**（几点睡~几点起，睡懒觉会顺延），跟「此刻在做什么」无关。
          AI 人格的「睡」在状态机里是「放松一下」，所以**绝不能拿活动名当睡据** ——
          否则白天的放松会被误判成睡着、把日常的由头一起掐掉。
        被主人叫醒着的那段（清醒尾巴内）不算真睡。
        """
        if not self._in_sleep_window_now(persona_id):
            return False
        return not self._disturbed_recently(persona_id, st, now)

    # ─────────── 睡醒重置「连败账」（可选开关，默认关） ───────────

    def wake_reset_enabled(self) -> bool:
        """「每天睡醒把连续未回应清零」开着吗？（设置页开关，默认关）"""
        return _as_bool(
            self._cfg("proactive_delivery.reset_unanswered_on_wake", False), False
        )

    async def _reset_unanswered_on_wake(self, persona_id: str, st: dict,
                                        now: datetime) -> bool:
        """新的一天：她睡醒了，就把「连续几条没被回应」这本账清零（可选）。

        为什么要有这一条：她主动说了两条、主人一整天没回，刹车就落下来闭嘴，
        得等主人下次开口才恢复。可主人清早出门、整天没说话的时候，
        她会被**前一天**攒下的连败压住一整天 —— 明明是新的一天，口气还是那句
        「已自动闭嘴」。

        做法：一天只做一次 ——「今天还没重置过」+「此刻不在睡眠窗口里」→ 清零，
        并记下 `unanswered_reset_date`；重载插件、重启都不会重复刷。
        ★ 只看作息窗口（几点睡~几点起，睡懒觉顺延），不看此刻在做什么；
          半夜被主人叫醒聊两句不算「睡醒」，那会儿还在窗口里，不动。
        ★ 每日额度 / 延后队列 / 说过的由头一概不碰。
        """
        if not self.wake_reset_enabled():
            return False
        today = self._today_iso()
        if str((st or {}).get("unanswered_reset_date") or "") == today:
            return False
        if self._in_sleep_window_now(persona_id):
            return False   # 还在睡（或被半夜叫醒）：等真醒了再说
        try:
            # ensure 返回的是账本里同一个 dict —— 清完这里读到的就是新值
            return await self.state.reset_unanswered(persona_id, today, why="睡醒了")
        except Exception as e:
            logger.debug(f"[晨光心语] {persona_id} 睡醒重置连败账跳过: {e}")
            return False

    # ─────────── 每天上限（目前只有「旧事想起」配了） ───────────

    _DAILY_CAP_KEYS = ("old_memory",)

    def _daily_cap(self, key: str) -> int:
        """这根由头每天最多发几次。0 = 不限（没配的也都算不限）。"""
        k = str(key or "")
        base = ""
        for cand in self._DAILY_CAP_KEYS:
            if k == cand or k.startswith(cand):
                base = cand
                break
        if not base:
            return 0
        return max(0, _as_int(self._cfg(f"proactive_triggers.{base}_daily_cap", 1), 1))

    def _capped(self, st: dict, key: str) -> bool:
        """这根今天已经发满了没？发满了就不再挑它去发（由头照样冒，只是不选它）。"""
        cap = self._daily_cap(key)
        if cap <= 0:
            return False
        used = _as_int(((st or {}).get("trigger_daily_count") or {}).get(str(key or "")), 0)
        return used >= cap

    async def _old_memory_exhausted(self, persona_id: str) -> bool:
        """「旧事想起」今天是不是已经发满上限了？（发满就**不再探它**）"""
        if self._daily_cap("old_memory") <= 0:
            return False
        try:
            st = await self.state.ensure(persona_id, self._today_iso())
        except Exception:
            return False
        return self._capped(st, "old_memory")

    async def _probe_wake_sleep(self, pid, state, now) -> TriggerHit | None:
        """G · 刚醒 / 刚要睡。

        时间默认**跟随**「🫀 内在状态」的作息，两边永远对得上。
        判断用「从目标时刻起过了多久」（取模 24 小时），所以跨午夜也不出错。
        """
        now_min = now.hour * 60 + now.minute
        # 刚醒：起床时间起两小时内，且状态上不是「还在睡」
        if (now_min - self._resolve_wake_minutes(pid)) % 1440 < 120 and not self._is_asleep(state):
            return TriggerHit(
                key="wake_sleep_wake", name="刚醒", level=CASUAL,
                detail="刚睡醒，脑子还糊着",
                payload={"phase": "wake"},
            )
        # 刚要睡：睡前那一个半小时之内
        if (now_min - self._resolve_sleep_minutes()) % 1440 < 90:
            return TriggerHit(
                key="wake_sleep_sleep", name="刚要睡", level=CASUAL,
                detail="准备睡了，眼皮开始打架",
                payload={"phase": "sleep"},
            )
        return None

    async def _probe_old_memory(self, pid, state, now) -> TriggerHit | None:
        """H · 旧事想起：从记忆池 / 旧账里捞一条很久没提过的事。"""
        st = await self.state.ensure(pid, self._today_iso())
        mentioned = set(st.get("mentioned_memories") or [])
        # V7 修复：账本事件的判重原来也查 mentioned_memories，可事件 id 是写进
        # mentioned_events 的（两套并存、写的没人读）→「旧事想起」会把同一件事反复讲。
        mentioned_events = set((st.get("mentioned_events") or {}).keys())

        # ① 优先记忆池（跟此刻的状态最相关的那条）
        if self.memory_bridge is not None:
            query = " ".join([
                str((state or {}).get("thought") or ""),
                " ".join((state or {}).get("goals") or []),
            ]).strip()
            try:
                items = await self.memory_bridge.retrieve(pid, query or "想起", limit=6)
            except Exception:
                items = []
            for it in items:
                fp = str(it.get("fingerprint") or "")
                text = str(it.get("text") or "")
                if not text or fp in mentioned:
                    continue
                return TriggerHit(
                    key="old_memory", name="旧事想起", level=CASUAL,
                    # N5：同上，不二次截断（原来 [:80] 只会把标题栏塞给她当"由头"）
                    detail=f"翻到一件旧事：{text}",
                    payload={"memory": text, "fingerprint": fp},
                )

        # ② 退一步：账本里已了结、且有些年头的旧事
        if self.event_ledger is not None:
            try:
                events = await self.event_ledger.load(pid)
            except Exception:
                events = []
            cutoff = now.date() - timedelta(days=_as_int(
                self._cfg("proactive_triggers.old_memory_days", 30), 30
            ))
            for e in reversed(events):
                if str(e.get("status")) != "closed":
                    continue
                eid = str(e.get("id") or "")
                if not eid or eid in mentioned_events:
                    continue
                try:
                    d = datetime.strptime(str(e.get("date") or ""), "%Y-%m-%d").date()
                except Exception:
                    continue
                if d > cutoff:
                    continue
                return TriggerHit(
                    key="old_memory", name="旧事想起", level=CASUAL,
                    detail=f"忽然想起以前的事：{str(e.get('summary') or '')[:80]}",
                    payload={"memory": str(e.get("summary") or "")[:200], "event_id": eid},
                )
        return None

    # ─────────────────────── 刹车（delivery 层） ───────────────────────

    def _mark_reused(self, st: dict, hit: TriggerHit) -> None:
        """这根导火索今天说过没有？说过就盖个 reused 章 —— **但不淘汰它**。

        ★ V7 改口径：以前这里是「当天用过就不再用」的硬拦。
        它分不清两件事——"翻来覆去讲同一句" 和 "事情变了、值得再提一次"。
        天气突然变恶劣、事情更严重了，正是该开口的时候，却会被去重一起拦死。

        现在改成：只盖个章，把"要不要再说一次"交给下游的自己判断
        （提示词里会写明「这根今天说过了，除非有新变化，否则别说」）。
        还硬拦的，全在 _brake_check 里：连续没回应 / 安静时段 / 睡着了 / 最小间隔 / 每日预算。
        """
        used_iso = (st.get("trigger_used") or {}).get(hit.key)
        if not used_iso:
            return
        payload = hit.payload if isinstance(hit.payload, dict) else {}
        payload["reused"] = True
        payload["reused_at"] = str(used_iso)
        try:
            used = datetime.fromisoformat(str(used_iso))
            now = self._now()
            if used.tzinfo is None and now.tzinfo is not None:
                used = used.replace(tzinfo=now.tzinfo)
            elif used.tzinfo is not None and now.tzinfo is None:
                used = used.replace(tzinfo=None)
            payload["reused_ago_min"] = int((now - used).total_seconds() // 60)
        except Exception:
            pass
        hit.payload = payload

    def _reused_note(self, hit: TriggerHit) -> str:
        """给「她说过的这根」配一句提醒 —— 喂给自由裁量 / 生成那一步。"""
        payload = hit.payload or {}
        if not payload.get("reused"):
            return ""
        when = ""
        try:
            when = datetime.fromisoformat(str(payload.get("reused_at") or "")).strftime("%H:%M")
        except Exception:
            when = ""
        ago = payload.get("reused_ago_min")
        ago_text = f"（约 {int(ago)} 分钟前）" if isinstance(ago, (int, float)) and 0 <= ago < 600 else ""
        return (
            f"【这根由头今天已经说过了】{when}{ago_text}那次就是拿「{hit.name}」开的口。\n"
            "除非这件事**有了新变化**（天气突然变恶劣、事情更严重了、或者有新消息他需要知道），"
            "否则这次就别说了；要说，就只说出「变了」的那部分，别把上次那句换个说法再讲一遍。"
        )

    async def _brake_check(self, persona_id: str, st: dict, umo: str,
                           now: datetime, state: dict | None) -> tuple[bool, str]:
        """全局刹车。返回 (能不能发, 不能发的原因)。"""
        # ① 连续未回应 → 暂停（要等主人开口才恢复）
        if st.get("paused"):
            return False, "连续几条都没回应，已自动闭嘴"
        threshold = _as_int(self._cfg("proactive_delivery.unanswered_pause_threshold", 2), 2)
        if threshold > 0 and _as_int(st.get("consecutive_unanswered"), 0) >= threshold:
            await self.state.set_paused(persona_id, True)
            return False, "连续几条都没回应，已自动闭嘴"

        # ② 安静时段（留空 = 跟着她的作息走，她睡了就别吵）
        q_start, q_end = self._quiet_hours(persona_id)
        if in_quiet_hours(now, q_start, q_end):
            # 安静时段默认跟作息重叠；「刚被主人叫醒」的清醒尾巴里不拦
            # （跟睡眠窗口同一套口径）。手填过安静时段 = 主人自己的规矩，照填的拦。
            if self._quiet_hours_manual() or not self._disturbed_recently(persona_id, st, now):
                return False, f"现在在安静时段（{q_start}~{q_end}）"

        # ③ 她真睡着了就别吵 —— 按**作息窗口**算（不看活动名），睡懒觉会顺延；
        #    刚被主人叫醒的那段不算真睡。
        if _as_bool(self._cfg("proactive_delivery.skip_when_sleeping", True), True):
            if self._in_true_sleep(persona_id, st, now) or self._is_asleep(state):
                return False, "这个点还在睡"

        # ④ 最小间隔
        last_sent = str(st.get("last_sent_at") or "")
        if last_sent:
            try:
                last = datetime.fromisoformat(last_sent)
                if last.tzinfo is None and now.tzinfo is not None:
                    last = last.replace(tzinfo=now.tzinfo)
                elif last.tzinfo is not None and now.tzinfo is None:
                    last = last.replace(tzinfo=None)
                gap_min = _as_int(self._cfg("proactive_delivery.min_interval_minutes", 60), 60)
                if (now - last).total_seconds() < max(0, gap_min) * 60:
                    return False, "离上一条太近了"
            except Exception:
                pass

        # ⑤ 每日预算（私聊 / 群聊分开算）
        chat_type = detect_chat_type(umo)
        if chat_type == "group":
            budget = _as_int(self._cfg("proactive_delivery.daily_budget_group", 3), 3)
            used = _as_int(st.get("daily_count_group"), 0)
        else:
            budget = _as_int(self._cfg("proactive_delivery.daily_budget_private", 6), 6)
            used = _as_int(st.get("daily_count_private"), 0)
        budget = max(0, min(10, budget))   # 上限 10：再多就不像"想起你"而像刷屏了
        if used >= budget:
            return False, "今天的话已经说完了（每日预算）"

        # ⑥ 主人正在跟你说话 → 先别插嘴。
        #    等他安静下来，到点会**另行叫醒她一次**（见 _arm_chat_alarm）。
        if self._chatting_now(persona_id, umo, st, now):
            return False, BLOCK_CHATTING

        return True, ""

    # ─────────── 聊天保护：主人正说着话，就先别插嘴 ───────────

    def _chat_idle_minutes(self) -> int:
        """主人停下多久之后，她才算「可以开口了」。0 = 不拦。"""
        return max(0, _as_int(self._cfg("proactive_delivery.chat_idle_minutes", 15), 15))

    def _last_user_at_umo(self, persona_id: str, umo: str,
                          st: dict | None) -> datetime | None:
        """**这个会话里**主人最后开口的时刻。

        ★ 只认会话级那份记录（note_user_activity 每条消息顺手写的）。
          **故意不退回人格级** —— 人格级是「所有会话里最近一次」，拿它兜底
          会让 A 群的一句话把 B 群也一起压住，正好跟这个功能的初衷相反。
          会话级没记录（比如刚重载、或这个会话还没人说过话）= 不拦。
        """
        raw = ""
        try:
            raw = self.state.last_user_at_umo(persona_id, umo)
        except Exception:
            raw = ""
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except Exception:
            return None

    def _chatting_now(self, persona_id: str, umo: str,
                      st: dict | None, now: datetime) -> bool:
        """主人现在是不是正跟她说得起劲（刚开口没多久）。"""
        idle = self._chat_idle_minutes()
        if idle <= 0:
            return False
        last = self._last_user_at_umo(persona_id, umo, st)
        if last is None:
            return False
        try:
            if last.tzinfo is None and now.tzinfo is not None:
                last = last.replace(tzinfo=now.tzinfo)
            elif last.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=last.tzinfo)
            return (now - last).total_seconds() < idle * 60
        except Exception:
            return False

    def _chat_wait_seconds(self, persona_id: str, umo: str,
                           st: dict | None, now: datetime) -> float:
        """还要等多久才算「他安静下来了」。"""
        idle = max(1, self._chat_idle_minutes()) * 60
        last = self._last_user_at_umo(persona_id, umo, st)
        if last is None:
            return 0.0
        try:
            if last.tzinfo is None and now.tzinfo is not None:
                last = last.replace(tzinfo=now.tzinfo)
            elif last.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=last.tzinfo)
            return max(0.0, idle - (now - last).total_seconds())
        except Exception:
            return 0.0

    def _min_interval_wait_seconds(self, st: dict | None, now: datetime) -> float:
        """离「够最小间隔」还差多久（秒）。0 = 这道闸已经过了。

        注意：这道闸是**人格级**的（「两条主动消息之间至少隔多久」），
        跟具体哪个会话无关 —— 防的是同一个人的注意力被反复打扰。
        """
        gap_min = _as_int(self._cfg("proactive_delivery.min_interval_minutes", 60), 60)
        if gap_min <= 0:
            return 0.0
        last = str((st or {}).get("last_sent_at") or "")
        if not last:
            return 0.0
        try:
            t = datetime.fromisoformat(last)
            if t.tzinfo is None and now.tzinfo is not None:
                t = t.replace(tzinfo=now.tzinfo)
            elif t.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=t.tzinfo)
            return max(0.0, gap_min * 60 - (now - t).total_seconds())
        except Exception:
            return 0.0

    def _arm_chat_alarm(self, persona_id: str, umo: str, st: dict | None) -> None:
        """排一个「到时候再来看一眼」的闹钟。同一会话只留一个 —— 重排 = 刷新时间。

        到点后**不是**把攒着那句硬发出去，而是重新跑一次判定：
        素材是那一刻的，她可以挑队列里那件、也可以重新想一句、也可以不说。

        ★ 定的时刻要**两道时间闸一起算、取更晚的那个**：
          ①「你安静够窗口」  ②「离上一条主动消息够最小间隔」。
          只算①的话会出现错位 —— 闹钟在你安静够 15 分钟时响，可那时候
          「间隔」还没满，于是被拦、退货，只能干等下一跳心跳（最长一小时），
          明明可能只差几分钟。

        排闹钟一直是**无限**的（主人定的）—— 收敛交给延后队列的保质期：
        念头过期了，闹钟醒来时队列里自然就空了。
        """
        alarms = getattr(self, "_chat_alarms", None)
        if not isinstance(alarms, dict):
            alarms = self._chat_alarms = {}
        old = alarms.get(umo)
        if old is not None and not old.done():
            old.cancel()
        now = self._now()
        delay = max(
            self._chat_wait_seconds(persona_id, umo, st, now),
            self._min_interval_wait_seconds(st, now),
        )
        delay = max(60.0, delay)   # 至少歇一分钟再看，别忙等
        alarms[umo] = self._spawn_bg(self._chat_alarm_run(persona_id, umo, delay))
        logger.debug(
            f"[晨光心语] ⏰ {persona_id} 现在还不能开口，"
            f"{self._wait_text(delay)}之后她再来看一眼（{umo}）"
        )

    async def _chat_alarm_run(self, persona_id: str, umo: str, delay: float) -> None:
        """闹钟到点：重新跑一次判定 —— 只针对这一个会话。"""
        try:
            await asyncio.sleep(max(1.0, float(delay)))
        except Exception:
            return
        try:
            alarms = getattr(self, "_chat_alarms", None)
            if isinstance(alarms, dict):
                alarms.pop(umo, None)
            await self.tick_persona(persona_id, only_umo=umo)
        except Exception as e:
            logger.debug(f"[晨光心语] ⏰ {persona_id} 聊天闹钟收尾跳过: {e}")

    async def _pick_target(self, persona_id: str, st: dict, targets: list[str],
                           now: datetime, state: dict | None) -> tuple[str | None, str]:
        """在订阅过的会话里挑一个此刻能发的。私聊优先。"""
        last_reason = "没有可用的会话"
        for umo in targets:
            ok, reason = await self._brake_check(persona_id, st, umo, now, state)
            if ok:
                return umo, ""
            last_reason = reason
        return None, last_reason

    # ─────────────────────── 心跳主流程 ───────────────────────

    @staticmethod
    def _wait_text(seconds: Any) -> str:
        """把秒数说成人话 —— 日志里别出现 568s 这种要心算的数字。"""
        try:
            sec = float(seconds)
        except (TypeError, ValueError):
            return "一会儿"
        if sec < 60:
            return "不到 1 分钟"
        return f"约 {int(round(sec / 60.0))} 分钟"

    def _jitter_minutes(self, hit: TriggerHit) -> float:
        """E1：这一条开口前随机等几分钟。0 = 不延迟（默认）。

        急事（疲惫/低落这类 URGENT）最多只等 3 分钟 —— 免得"急着想说话"被拖成凉了。
        """
        try:
            base = float(self._cfg("proactive_delivery.jitter_minutes", 0) or 0)
        except (TypeError, ValueError):
            base = 0.0
        if base <= 0:
            return 0.0
        cap = min(base, 3.0) if getattr(hit, "level", CASUAL) == URGENT else base
        return random.uniform(0.0, cap)

    def _spawn_bg(self, coro):
        """起个后台任务并攥住引用（否则 asyncio 只持弱引用，可能被提前回收）。"""
        tasks = getattr(self, "_bg_tasks", None)
        if not isinstance(tasks, set):
            tasks = self._bg_tasks = set()
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    async def _after_blocked(self, persona_id: str, st: dict, targets: list[str],
                             chosen: TriggerHit, reason: str, now: datetime) -> None:
        """撞刹车之后的收尾 —— **心跳**和「开口前随机等一会儿」两条路共用。

        ① 能延后就延后：那件进待发列表，顺手记下**为什么被拦**（补发时提示词会用）；
        ② 要是因为「主人正聊着」被拦的 —— 再排个闹钟，等他安静下来单独叫醒她。
           （其它刹车不排：安静时段 / 额度那类，等下一跳心跳就好。）
        """
        if _as_bool(self._cfg("proactive_delivery.defer_enabled", True), True):
            await self.state.defer(
                persona_id, chosen.to_pending(), self._iso(now),
                _as_int(self._cfg("proactive_delivery.defer_max_hours", 6), 6),
                _as_int(self._cfg("proactive_delivery.defer_max_items", 3), 3),
                reason=reason,
            )
        chat_umo = next(
            (u for u in targets if self._chatting_now(persona_id, u, st, now)), ""
        )
        # 卡在「时间」这两道闸上的（你正聊着 / 离上一条还太近）→ 排个闹钟，到点再来看；
        # 别的刹车（额度用完 / 睡着 / 安静时段）排了也白排，就不排。
        if targets and (chat_umo or self._min_interval_wait_seconds(st, now) > 0):
            self._arm_chat_alarm(persona_id, chat_umo or targets[0], st)

    async def _delayed_deliver(self, persona_id: str, chosen: TriggerHit, umo: str,
                               state: dict | None, delay_seconds: float) -> None:
        """E1：等一会儿再开口。等的时候万一踩了刹车（进安静时段/没额度）就作罢。"""
        try:
            await asyncio.sleep(max(1.0, float(delay_seconds)))
        except Exception:
            return
        try:
            targets = self.subscriptions.targets_for_persona(persona_id)
            if not targets:
                return
            now = self._now()
            st = await self.state.ensure(persona_id, self._today_iso())
            new_umo, reason = await self._pick_target(persona_id, st, targets, now, state)
            if new_umo is None:
                await self._after_blocked(persona_id, st, targets, chosen, reason, now)
                logger.debug(f"[晨光心语] 🎲 {persona_id} 多等了一会儿再看，还是不能说，原因是：{reason}")
                return
            logger.debug(
                f"[晨光心语] 🎲 {persona_id} 等了{self._wait_text(delay_seconds)}，开口了"
            )
            await self._deliver(persona_id, chosen, new_umo, state)
        except Exception as e:
            # 这一步的特殊之处：消息**可能已经发出去了**，只是收尾（记账 / 清延后队列 /
            # 推水位线）失败。「发了不记账」会让所有刹车都以为"没发过"——间隔、预算、
            # 聊天窗口全失效。所以不能再拿 debug 吞掉，升到 warning 让它自己喊人。
            logger.warning(
                f"[晨光心语] ⚠️ {persona_id} 延迟发送收尾失败（消息可能已发出、但账没记）: {e}"
            )

    async def _rollback_ledger(self, persona_id: str, hit: TriggerHit | None) -> None:
        """把账本水位线退回这条 hit 记下的位置 —— 「本该发却没发成」时用。

        水位线一旦推走，那些带 closed_at 的旧事就再也捡不回来了（探针只认
        closed_at > last_check）。所以宁可退回去让它下一跳再被试一次：
        发成功才 mark_sent / 推水位线，退回去不会造成重复发送。
        """
        if hit is None or getattr(hit, "key", "") != "promise":
            return
        prev = (hit.payload or {}).get("prev_ledger_check")
        if prev is None:
            return
        try:
            await self.state.set_ledger_check(persona_id, str(prev))
        except Exception as e:
            logger.debug(f"[晨光心语] 账本水位线回退跳过: {e}")

    async def _deliver(self, persona_id: str, chosen: TriggerHit, umo: str,
                       state: dict | None) -> None:
        """投递的总入口 —— 先过一道「同一个人格同一时刻只发一条」的锁。

        ★ 为什么需要这把锁：心跳、聊天闹钟、「开口前随机等一会儿」三条路各自独立，
          醒来时刻**可能撞在一起** —— 各自都查到「还没发过」就都往外发，于是刷两条。
          生成那一步要调模型（中途会让出控制权），光靠 `_pick_target` 里的
          「离上一条太近」拦不住这种**同时起跑**。
        """
        sending = getattr(self, "_sending", None)
        if not isinstance(sending, set):
            sending = self._sending = set()
        if persona_id in sending:
            logger.debug(f"[晨光心语] 🚦 {persona_id} 已经有一条在路上了，这条先让开")
            await self._rollback_ledger(persona_id, chosen)
            return
        sending.add(persona_id)
        try:
            await self._deliver_locked(persona_id, chosen, umo, state)
        finally:
            sending.discard(persona_id)

    async def _deliver_locked(self, persona_id: str, chosen: TriggerHit, umo: str,
                              state: dict | None) -> None:
        """生成 → 发送 → 善后。E1 抽出来的，即时发和延迟发共用。"""
        now = self._now()
        result = await self.generate_message(persona_id, chosen, state=state)
        if result.skipped:
            # 她自己决定这次不说：记进「跳过列表」，下次摆回她眼前。
            # 不 mark_sent → 当天额度不消耗、这个由头也不算用过。
            await self._record_skip(persona_id, chosen, result.why, now)
            # 这件已经记进「没说出口」了，从延后队列撤掉，免得下一轮又冒出来
            await self.state.clear_deferred(persona_id, chosen.key)
            await self._rollback_ledger(persona_id, chosen)  # 没真发出去，水位线退回去
            return
        text = result.text
        if not text:
            await self._rollback_ledger(persona_id, chosen)
            return
        if not await self.send_text(umo, text):
            await self._rollback_ledger(persona_id, chosen)
            return
        await self.state.mark_sent(
            persona_id, chosen.key, self._iso(now), umo,
            detect_chat_type(umo), note=chosen.detail, text=text,
        )
        await self.write_history(umo, persona_id, chosen, text)
        await self._after_send(persona_id, chosen)
        logger.info(
            f"[晨光心语] 📣 主动消息已发出（{persona_id} · {chosen.name} → {umo}）："
            f"{text[:40]}"
        )

    async def tick_persona(self, persona_id: str, only_umo: str = ""):
        """一次心跳：判定 → 刹车 → 生成 → 发送 → 记账。全程不抛异常。

        `only_umo` 给了就只处理那一个会话 —— 「聊天闹钟」到点时会这么调。
        """
        # 心跳和聊天闹钟可能撞在一起 —— 同一个人格同一时刻只跑一次。
        running = getattr(self, "_ticking", None)
        if not isinstance(running, set):
            running = self._ticking = set()
        if persona_id in running:
            return
        running.add(persona_id)
        try:
            if not self.available():
                return
            targets = self.subscriptions.targets_for_persona(persona_id)
            if only_umo:
                targets = [u for u in targets if u == only_umo]
            if not targets:
                return

            now = self._now()
            st = await self.state.ensure(persona_id, self._today_iso())

            # ★ 真睡着那一整段（作息窗口内、且不是刚被叫醒）：由头这套整个不打烊。
            #   熟睡不想事才合理；也省掉睡眠期每小时那次外部记忆库查询。
            #   只看作息窗口、不看此刻在做啥 —— 白天的「放松」不受影响。
            if (_as_bool(self._cfg("proactive_delivery.skip_when_sleeping", True), True)
                    and self._in_true_sleep(persona_id, st, now)):
                logger.debug(f"[晨光心语] 😴 {persona_id} 睡着了，这次不叫她")
                return

            # ★ 睡醒重置（可选开关，默认关）：新的一天，先把「连续几条没被回应」清零。
            #   放在睡着早退**之后** —— 睡着那一整段什么都不该动；
            #   也放在挑由头之前 —— 免得刚醒来这一跳就被昨天的连败挡回去。
            await self._reset_unanswered_on_wake(persona_id, st, now)

            hits = await self.detect_triggers(persona_id)

            # 承诺兑现这根单独留个引用：这一跳万一没轮上它说（被别的急档挤掉、
            # 被日上限挡住、或干脆没被选中），得把账本水位线退回去 —— 否则那条
            # 「报平安」被水位线推走就再也捡不回来了。
            # ★ 必须在下面 _capped 过滤之前留：过滤后它可能就不在 hits 里了。
            promise_hit = next((h for h in hits if h.key == "promise"), None)

            # 上一轮被刹车拦下的那几件，还在有效期内的补发
            # R2-2 修复：急档的新鲜由头排在前、旧缓档居中 —— 不再无条件插到最前，
            # 免得一件旧缓档一直压着今天新冒出来的急事。
            # （挑哪件说仍交给下游的她自己判断，这里只决定先后。）
            pending = self._live_deferred(st, now)
            if pending:
                urgent = [h for h in hits if h.level == URGENT]
                rest = [h for h in hits if h.level != URGENT]
                hits = urgent + pending + rest
            elif st.get("deferred"):
                # 全过期了：清空，别把陈年旧事翻出来说
                await self.state.clear_deferred(persona_id)

            # 每天上限：这根今天已经发满了，就不再挑它（由头照样冒，只是不选它去发）
            hits = [h for h in hits if not self._capped(st, h.key)]

            # 逐根盖个章：这根今天说过没有（说过≠不能用，只是记一笔）
            for h in hits:
                self._mark_reused(st, h)
            # 没用过的优先；全说过，就按原顺序挑一根 —— 判断留给下游的自己
            fresh = [h for h in hits if not (h.payload or {}).get("reused")]
            chosen = (fresh or hits)[0] if hits else None
            # 承诺兑现这根：探针捡到了、这一跳却没轮上它说 —— 把水位线退回去。
            # 覆盖三种情形：被别的急档挤掉 / 被日上限挡住 / hits 过滤后为空。
            if promise_hit is not None and (chosen is None or chosen.key != "promise"):
                await self._rollback_ledger(persona_id, promise_hit)
            if chosen is None:
                return

            state = None
            try:
                state = await self.life_bridge.get_state(persona_id)
            except Exception:
                state = None

            umo, reason = await self._pick_target(persona_id, st, targets, now, state)
            if umo is None:
                await self._after_blocked(persona_id, st, targets, chosen, reason, now)
                logger.debug(f"[晨光心语] ⏸️ {persona_id} 这次没开口，原因是：{reason}")
                return

            # E1：随机延迟 —— 别让她每次都卡在心跳整点冒出来。
            # 默认 0（不延迟、行为不变）；把「开口前随机等一会儿」调大才有活感。
            delay_min = self._jitter_minutes(chosen)
            if delay_min > 0:
                self._spawn_bg(
                    self._delayed_deliver(persona_id, chosen, umo, state, delay_min * 60)
                )
                return

            await self._deliver(persona_id, chosen, umo, state)
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 主动消息心跳异常（已忽略）: {e}")
        finally:
            running.discard(persona_id)

    # R2-4 修复：有独立开关的导火索 key（与 detect_triggers 的注册表一致）。
    # 拆出来的子由头（wake_sleep_wake / wake_sleep_sleep）回落到父键 wake_sleep。
    _ENABLE_KEYS = ("tired", "low", "joy", "promise",
                    "anniversary", "weather", "wake_sleep", "old_memory")

    def _trigger_enabled(self, key: str) -> bool:
        """这根导火索现在开着吗？关掉的由头不该从延后队列里复活。"""
        k = str(key or "")
        if k not in self._ENABLE_KEYS:
            for parent in self._ENABLE_KEYS:
                if k.startswith(parent):
                    k = parent
                    break
        return _as_bool(self._cfg(f"proactive_triggers.enable_{k}", True), True)

    def _live_deferred(self, st: dict, now: datetime) -> list[TriggerHit]:
        """延后队列里还**没过期**的那几件（老格式的「单条」也认）。

        一件一件各自算保质期 —— 排队早的先老，不该因为后面又排了新队就跟着续命。
        R2-4 修复：补发前先看这根由头现在开着没，关掉的直接不发。
        """
        raw = st.get("deferred")
        if not raw:
            return []
        hours = max(1, _as_int(self.state.defer_hours(raw), 6) or 6)
        out: list[TriggerHit] = []
        for item in self.state.defer_items(raw):
            if not self._queued_alive(item.get("queued_at"), now, hours):
                continue
            hit = TriggerHit.from_pending(item.get("payload"))
            # 开着的才发；今天已经发满上限的，也别再拎出来试
            if (hit is not None and self._trigger_enabled(hit.key)
                    and not self._capped(st, hit.key)):
                out.append(hit)
        return out

    def pending_preview(self, persona_id: str) -> list[str]:
        """E3：延后队列里还活着的那几件的名字 —— 给 /晨光 状态 显示用。"""
        try:
            st = self.state.raw(persona_id) or {}
        except Exception:
            return []
        return [h.name for h in self._live_deferred(st, self._now())]

    @staticmethod
    def _queued_alive(queued_at, now: datetime, hours: int) -> bool:
        """这一件排的队过期了吗？"""
        text = str(queued_at or "")
        if not text:
            return False
        try:
            q = datetime.fromisoformat(text)
            if q.tzinfo is None and now.tzinfo is not None:
                q = q.replace(tzinfo=now.tzinfo)
            elif q.tzinfo is not None and now.tzinfo is None:
                q = q.replace(tzinfo=None)
        except Exception:
            return False
        return (now - q).total_seconds() <= hours * 3600

    async def _record_skip(self, persona_id: str, hit: TriggerHit,
                           why: str, now: datetime):
        """她这次自己决定不说：只记一笔 —— 额度、由头、刹车，什么都不动。"""
        try:
            await self.state.record_skipped(
                persona_id, hit.key, hit.detail or hit.name, why, self._iso(now)
            )
        except Exception as e:
            logger.debug(f"[晨光心语] {persona_id} 跳过记录写入失败（忽略）: {e}")
        logger.info(
            f"[晨光心语] 🤐 {persona_id} 起念了「{hit.name}」，想了想还是没说：{why or '（没给理由）'}"
        )

    async def _after_send(self, persona_id: str, hit: TriggerHit):
        """发完善后：标记「这件事说过了」，免得翻来覆去讲同一件。"""
        payload = hit.payload or {}
        try:
            if payload.get("shareable_id") is not None and self.life_bridge is not None:
                await self.life_bridge.mark_shared(persona_id, payload["shareable_id"])
        except Exception as e:
            logger.debug(f"[晨光心语] {persona_id} 标记已分享跳过: {e}")
        try:
            if payload.get("event_id"):
                await self.state.mark_mentioned_event(
                    persona_id, str(payload["event_id"]), self._iso(self._now())
                )
        except Exception:
            pass
        try:
            if payload.get("fingerprint"):
                await self.state.mark_mentioned_memory(persona_id, str(payload["fingerprint"]))
            elif payload.get("memory"):
                await self.state.mark_mentioned_memory(
                    persona_id, f"mem::{str(payload['memory'])[:60]}"
                )
        except Exception:
            pass

    # ─────────────────────── P2 · 内容生成 ───────────────────────

    async def generate_message(self, persona_id: str, hit: TriggerHit,
                               state: dict | None = None) -> GenerateResult:
        """单独调一次模型，写出发出去的那句话。

        `proactive.self_veto` 开着时，她也可以选择**这次不说** ——
        返回 `GenerateResult(skipped=True, why=...)`，由调用方记进「跳过列表」。
        """
        provider, model = await self._resolve_provider()
        if provider is None:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 主动消息：拿不到可用的模型，本次不发")
            return GenerateResult()

        if state is None and self.life_bridge is not None:
            try:
                state = await self.life_bridge.get_state(persona_id)
            except Exception:
                state = None

        persona_prompt = await self._persona_prompt(persona_id)
        schedule_now = await self._schedule_now(persona_id)
        max_len = max(20, _as_int(self._cfg("proactive.max_length", 120), 120))

        # separate 档：先单独问一次「现在要不要说」，不想说就直接收工
        mode = self._self_veto_mode()
        if mode == "separate":
            why = await self._ask_should_speak(
                provider, model, persona_id, hit, state, persona_prompt, schedule_now
            )
            if why is not None:
                return GenerateResult(None, True, why)

        prompt = self._render_prompt(
            persona_id, hit, state, schedule_now, max_len, persona_prompt
        )
        raw_text = await self._call_llm(provider, model, prompt, persona_prompt, persona_id)
        text = self._clean_output(raw_text, max_len)
        if not text:
            return GenerateResult()
        # 她自己可以选择这次不说（只有 merge 档才在同一段文字里认这个信号）
        # R4 修复：清洗会把「SKIP: 理由」这种前缀剥掉，所以**清洗前后各认一次**。
        if mode == "merge":
            veto = self._parse_veto(text)
            if veto is None:
                veto = self._parse_veto(raw_text)
            if veto is not None:
                return GenerateResult(None, True, veto)
        # 太长了就再问一次，明确要求更短
        if len(text) > max_len * 2:
            retry_prompt = (
                prompt
                + f"\n\n【补充要求】上一条太长了（{len(text)} 字）。"
                  f"现在只写一句话，{max_len} 字以内，不许超过。"
            )
            again_raw = await self._call_llm(provider, model, retry_prompt, persona_prompt, persona_id)
            again = self._clean_output(again_raw, max_len)
            if again:
                # R4 修复：重试结果同样可能说「不想说」，也过一遍 veto（原来漏了）。
                if mode == "merge":
                    veto2 = self._parse_veto(again)
                    if veto2 is None:
                        veto2 = self._parse_veto(again_raw)
                    if veto2 is not None:
                        return GenerateResult(None, True, veto2)
                text = again
        return GenerateResult(text[:max_len] if len(text) > max_len else text)

    async def _ask_should_speak(self, provider, model, persona_id: str, hit: TriggerHit,
                                state: dict | None, persona_prompt: str,
                                schedule_now: str) -> str | None:
        """separate 档专用：单独调一次小模型，**只问「现在要不要说」**。

        返回理由字符串 = 她决定这次不说；返回 None = 该说，继续走正常生成。
        """
        prompt = (
            "你现在**不是**在写消息，只是在回答一个问题：此刻要不要主动开口跟他说句话。\n\n"
            f"【你是谁】\n{persona_prompt or '（按你平时说话的口气来）'}\n\n"
            f"【你现在的状态】\n{self._state_text(state)}\n\n"
            f"【你此刻在做什么】\n{schedule_now or '（这一刻没什么特别的安排）'}\n\n"
            f"【你最近已经说过】\n{self._recent_said_text(persona_id)}\n\n"
            f"【你最近想说、但最后没说出口的】\n{self._recent_skipped_text(persona_id)}\n\n"
            f"【这次起念的由头】\n{hit.name}：{hit.detail or hit.name}\n"
            f"{self._reused_note(hit)}\n"
            f"【你攒着、还没说出口的其他几件】\n{self._deferred_others_text(persona_id, hit)}\n\n"
            "由头只是「你想起他了」，不是命令。你要是觉得此刻开口不合适、或者只是被由头推着、"
            "其实并不真的想说话，那就别说。拿不准的时候偏向不说。\n"
            "上面「没说出口的」里如果有哪件你现在还惦记着，就趁这次说。\n\n"
            "只用一行回答，别的什么都别写：\n"
            "- 不想说 → SKIP｜（一句话说清为什么此刻不说）\n"
            "- 想说   → SEND"
        )
        try:
            raw = await self._call_llm(provider, model, prompt, persona_prompt, persona_id)
        except Exception as e:
            logger.debug(f"[晨光心语] {persona_id} 单独判断失败，当「想说」处理: {e}")
            return None
        why = self._parse_veto(self._clean_output(raw, 120))
        if why is None:
            why = self._parse_veto(raw)   # R4 修复：清洗剥掉前缀时，对原文再认一次
        return why if why is not None else None

    async def _resolve_provider(self):
        """用哪家的模型写主动消息：优先 proactive.proactive_model，否则用会话默认。"""
        if self.generator is not None:
            try:
                return await self.generator._resolve_provider_by_config(
                    "proactive.proactive_model", "主动消息"
                )
            except Exception as e:
                logger.warning(f"[晨光心语] ⚠️ 解析主动消息模型失败: {e}")
        try:
            getter = getattr(self._context(), "get_using_provider_async", None)
            if getter:
                return await getter(), None
        except Exception:
            pass
        return None, None

    async def _persona_prompt(self, persona_id: str) -> str:
        builder = self.context_builder
        if builder is None:
            return ""
        try:
            return await builder._get_persona_prompt(persona_id)
        except Exception:
            return ""

    async def _call_llm(self, provider, model, prompt: str,
                        system_prompt: str, persona_id: str) -> str:
        """一次独立调用。绝不带主对话的上下文，也不会污染它。"""
        kwargs: dict[str, Any] = {"prompt": prompt}
        if model:
            kwargs["model"] = model
        if system_prompt:
            kwargs["system_prompt"] = system_prompt
        resp = None
        try:
            resp = await provider.text_chat(
                session_id=f"proactive::{persona_id}", **kwargs
            )
        except TypeError:
            # 有的 provider 不吃 session_id 这个参数
            resp = await provider.text_chat(**kwargs)
        return _extract_text(resp)

    def _render_prompt(self, persona_id: str, hit: TriggerHit, state: dict | None,
                       schedule_now: str, max_len: int, persona_prompt: str) -> str:
        template = str(self._cfg("proactive_prompt.template", "") or "").strip()
        if not template:
            template = DEFAULT_PROMPT_TEMPLATE
        now = self._now()
        values = {
            "persona": persona_id,
            "persona_prompt": persona_prompt or "（这个人格没有单独设定，就按平时跟你说话的口气来）",
            "state_text": self._state_text(state),
            "schedule_now": schedule_now or "（这一刻没什么特别的安排）",
            "recent_said": self._recent_said_text(persona_id),
            "recent_skipped": self._recent_skipped_text(persona_id),
            "deferred_others": self._deferred_others_text(persona_id, hit),
            # 只有 merge 档才把「先问自己」写进同一次提示词；separate 档单独问，off 档不问。
            "veto_step": VETO_STEP_ON if self._self_veto_mode() == "merge" else VETO_STEP_OFF,
            "trigger_name": hit.name,
            "trigger_detail": hit.detail or hit.name,
            "reused_note": self._reused_note(hit),
            "blocked_note": self._blocked_note(hit),
            "time_str": now.strftime("%Y年%m月%d日 %H:%M"),
            "weekday": "一二三四五六日"[now.weekday()],
            "max_length": str(max_len),
        }
        try:
            return template.format(**values)
        except (KeyError, IndexError) as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 主动消息提示词模板占位符有问题（{e}），用内置模板")
            return DEFAULT_PROMPT_TEMPLATE.format(**values)

    def _self_veto_mode(self) -> str:
        """「让她自己决定这次要不要发」的档位：off / merge / separate。

        - off      ：不问，命中就发（老行为）
        - merge    ：并进同一次生成调用里自问（不额外花钱，默认）
        - separate ：单独多调一次小模型专门判断（更稳，但多花调用）

        兼容老配置：self_veto 曾经是个 bool（true/false），读到了就翻译过来。
        """
        raw = self._cfg("proactive.self_veto", "merge")
        if isinstance(raw, bool):
            return "merge" if raw else "off"
        mode = str(raw or "").strip().lower()
        return mode if mode in ("off", "merge", "separate") else "merge"

    # 分隔符改可选：她只回一个光秃秃的 "SKIP" 时也要认出来，
    # 否则那句 SKIP 会被当正文直接发给主人（"｜理由"仍照旧解析）。
    _VETO_RE = re.compile(r"^\s*(?:SKIP|skip|跳过)\s*(?:[｜|:：]\s*(.*))?$")

    @classmethod
    def _parse_veto(cls, text: str) -> str | None:
        """她是不是决定这次不说了？是的话返回理由（可能为空串），不是返回 None。"""
        lines = str(text or "").strip().splitlines()
        if not lines:
            return None
        m = cls._VETO_RE.match(lines[0].strip())
        if not m:
            return None
        return (m.group(1) or "").strip()[:80]

    def _blocked_note(self, hit: TriggerHit) -> str:
        """这件是「被拦下来、等空闲」的吗？是的话跟她说清楚为什么。"""
        why = str((hit.payload or {}).get("blocked_reason") or "").strip()
        if not why:
            return ""
        return "【这件为什么拖到现在】" + why + "。现在他安静下来了。" + chr(10) * 2

    def _recent_said_text(self, persona_id: str) -> str:
        """最近说过的话 —— 提醒她别重复。"""
        try:
            items = self.state.recent_said(persona_id, limit=2)
        except Exception:
            items = []
        if not items:
            return "（还没有，这是你今天第一次开口）"
        return "\n".join(f"· {t}" for t in items)

    def _recent_skipped_text(self, persona_id: str) -> str:
        """最近想说、但没说出口的 —— 给她一次补上的机会，别一直憋着。"""
        try:
            # N2 配套：只捞 48 小时以内的 —— 三天前憋的话别摆出来装作今早刚发生
            items = self.state.recent_skipped(persona_id, limit=3, max_age_hours=48)
        except Exception:
            items = []
        if not items:
            return "（没有）"
        out = [line for line in (format_skipped_line(it) for it in items) if line]
        return "\n".join(f"· {line}" for line in out) or "（没有）"

    def _deferred_others_text(self, persona_id: str, current: TriggerHit) -> str:
        """延后队列里**除了当前这件**以外还攒着的 —— 摆到她眼前，让她自己挑。

        被拦下的事放久了会过时（雨停了、气消了、事已经过去了），所以这里把
        「什么时候起的念头」一起给她，好让她自己判断哪件还值得现在说。
        """
        try:
            st = self.state.raw(persona_id) or {}
        except Exception:
            st = {}
        raw = st.get("deferred")
        items = self.state.defer_items(raw)
        hours = max(1, _as_int(self.state.defer_hours(raw), 6) or 6)
        now = self._now()
        lines: list[str] = []
        for item in items:
            # 过期的就别再摆到她眼前了 —— 雨都停了，提了也尴尬
            if not self._queued_alive(item.get("queued_at"), now, hours):
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            key = str(payload.get("key") or "")
            if key == str(current.key or ""):
                continue
            # 已经封顶、或者开关已经关掉的，别再摆到她眼前 —— 省提示词，
            # 也免得她挑中一件根本发不出去的。
            if key and (self._capped(st, key) or not self._trigger_enabled(key)):
                continue
            detail = str(payload.get("detail") or "").strip() or str(payload.get("name") or "")
            if not detail:
                continue
            when = str(item.get("queued_at") or "")[11:16]
            lines.append(f"· {when} 起就惦记着：{detail}" if when else f"· 还惦记着：{detail}")
        return "\n".join(lines) if lines else "（没有）"

    @staticmethod
    def _state_text(state: dict | None) -> str:
        if not state:
            return "（暂时读不到你此刻的状态）"
        lines = [f"精力：{state.get('energy')}"
                 f"（实际 {state.get('energy_effective', state.get('energy'))}"
                 f" · {energy_zh(state.get('energy_label') or '正常')}）",
                 f"心情：{mood_zh(state.get('mood') or '平静')}（强度 {state.get('mood_intensity')}）"]
        if state.get("current_activity"):
            lines.append(f"此刻在做：{activity_zh(state['current_activity'])}")
        if state.get("location"):
            lines.append(f"所在：{location_zh(state['location'])}")
        if state.get("thought"):
            lines.append(f"脑子里在想：{state['thought']}")
        if state.get("goals"):
            lines.append("惦记的事：" + "；".join(state["goals"]))
        return "\n".join(lines)

    @staticmethod
    def _clean_output(text: str, max_len: int) -> str:
        """把模型的话打理成「能直接发出去的一句」。"""
        s = str(text or "").strip()
        if not s:
            return ""
        s = s.strip("`").strip()
        if s.startswith("```"):
            parts = s.split("```")
            if len(parts) >= 2:
                s = parts[1]
            s = s[4:] if s.lower().startswith("json") else s
            s = s.strip()
        lines = [ln for ln in s.splitlines() if not _META_LINE_RE.match(ln)]
        s = "\n".join(lines).strip()
        m = _QUOTE_WRAP_RE.match(s)
        if m:
            s = m.group(1).strip()
        s = _SPEAKER_PREFIX_RE.sub("", s, count=1) if len(s) < 60 else s
        s = re.sub(r"\n{3,}", "\n\n", s).strip()
        return s

    # ─────────────────────── 发送通道（P0） ───────────────────────

    async def send_text(self, umo: str, text: str) -> bool:
        """往指定会话发一条纯文本。失败只记日志，返回 False。"""
        ctx = self._context()
        if ctx is None:
            logger.warning(f"[晨光心语] ⚠️ 主动消息：{umo} 拿不到 context，发不出去")
            return False
        try:
            chain = _build_text_chain(text)
            if chain is None:
                return False
            ok = await ctx.send_message(umo, chain)
            if not ok:
                logger.warning(f"[晨光心语] ⚠️ 主动消息发送未成功（会话 {umo} 没有匹配的平台）")
            return bool(ok)
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ 主动消息发送异常（{umo}）: {e}")
            return False

    # ─────────────────────── P3 · 写回对话历史 ───────────────────────

    async def write_history(self, umo: str, persona_id: str, hit: TriggerHit, text: str):
        """把这条主动消息轻轻写进该会话的历史，让后续对话接得上。

        - 只有「主动消息总开关开着」时才写（手动验证通道那几次不污染历史）；
        - 写法很轻：一句缘由注记 + 正文，绝不写成「系统指令」的口吻；
        - 拿不到 conversation 就跳过，只记 debug。
        """
        if not self.enabled():
            return
        if not _as_bool(self._cfg("proactive.write_history", True), True):
            return
        cm = getattr(self._context(), "conversation_manager", None)
        if cm is None:
            logger.debug(f"[晨光心语] 主动消息：{umo} 没有 conversation_manager，跳过写历史")
            return
        try:
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                logger.debug(f"[晨光心语] 主动消息：{umo} 没有当前对话，跳过写历史")
                return
            conv = await cm.get_conversation(umo, cid)
            history: list[dict] = []
            if conv is not None and getattr(conv, "history", None):
                try:
                    parsed = json.loads(conv.history)
                except Exception as e:
                    # 解析失败时绝不能拿"只含这一条"的空列表去整体替换历史，
                    # 否则一次坏数据就把整个会话历史抹成一条主动消息。
                    logger.debug(f"[晨光心语] 主动消息写历史跳过（历史解析失败）: {e}")
                    return
                if not isinstance(parsed, list):
                    logger.debug("[晨光心语] 主动消息写历史跳过（历史不是列表）")
                    return
                history = [h for h in parsed if isinstance(h, dict)]
            note_tpl = str(self._cfg(
                "proactive_prompt.history_note_template", "（你主动发起的：{reason}）"
            ) or "").strip()
            try:
                note = note_tpl.format(reason=hit.name, detail=hit.detail, trigger=hit.name)
            except (KeyError, IndexError):
                note = f"（你主动发起的：{hit.name}）"
            content = f"{note}\n{text}"
            history.append({"role": "assistant", "content": content})
            await cm.update_conversation(umo, conversation_id=cid, history=history)
            logger.debug(f"[晨光心语] 主动消息已写回对话历史（{cid}）")
        except Exception as e:
            logger.debug(f"[晨光心语] {persona_id} 主动消息写历史跳过: {e}")

    # ─────────────────────── 主人开口 ───────────────────────

    async def note_user_activity(self, persona_id: str, umo: str = ""):
        """主人说话了 → 解除「连续未回应暂停」。不改变任何回复行为。"""
        try:
            await self.state.note_user_activity(persona_id, umo, self._iso(self._now()))
        except Exception as e:
            logger.debug(f"[晨光心语] {persona_id} 记录主人开口跳过: {e}")

    # ─────────────────────── 手动触发（/晨光 说句话） ───────────────────────

    async def manual_speak(self, persona_id: str, umo: str) -> tuple[bool, str]:
        """立刻生成一条主动消息并发到当前会话（通道自检）。

        刻意不受刹车限制（这是手动试火花），也不写对话历史——
        免得测试把主人的聊天记录搅浑。
        """
        try:
            hits = await self.detect_triggers(persona_id, respect_cap=False)
        except Exception:
            hits = []
        hit = hits[0] if hits else TriggerHit(
            key="manual", name="手动触发", level=CASUAL,
            detail=random.choice(_MANUAL_REASONS),
        )
        result = await self.generate_message(persona_id, hit)
        if result.skipped:
            return False, (f"🤐 她自己决定这次不说了：{result.why or '（没给理由）'}"
                           f"（手动点火也尊重她的意愿喵）")
        text = result.text
        if not text:
            return False, "⚠️ 这一次没能生成出内容（模型没返回 / 调用失败），去看看日志喵。"
        if not await self.send_text(umo, text):
            return False, "⚠️ 生成好了，但发送失败——检查一下这个会话的平台连接喵。"
        try:
            await self._after_send(persona_id, hit)
        except Exception:
            pass
        suffix = f"（由头：{hit.name}）" if hit.key != "manual" else "（手动触发，无导火索）"
        # 正文已经发到会话里了，这里只回一句「发出去了」，不再复读一遍
        return True, f"✅ 已发出 {suffix}"


# ─────────────────────── 模块级小工具 ───────────────────────

VETO_STEP_ON = """【第一步 · 先问自己：现在真的想说吗】
- 由头只是「你想起他了」，不是命令。要是觉得此刻说这个不合适、或者只是被由头推着、其实并不真的想开口，那就别说。
- 拿不准的时候**偏向不说** —— 只回下面这一行，别的什么都别写：
  SKIP｜（一句话说清你为什么此刻不说）
- 上面「你最近想说、但最后没说出口的」里，如果有哪件你现在还惦记着，就趁这次说出来，别一直憋着。
- 「你攒着、还没说出口的其他几件」里如果有哪件你现在更想说，就**直接说那一件**，不必管下面那个由头。放久了的事可能已经过时了（雨停了、气消了），要不要提由你自己判断。"""

VETO_STEP_OFF = ""

DEFAULT_PROMPT_TEMPLATE = """先是你自己：
{persona_prompt}

────── 此刻 ──────
你在这儿：{schedule_now}

身体和心里是这样的（这些都是数字，你自己翻译成感觉）：
{state_text}

现在是 {time_str}，星期{weekday}。

────── 心里动了一下的那点事 ──────
{trigger_name}：{trigger_detail}

{blocked_note}{reused_note}
【你最近想说、但最后没说出口的】
{recent_skipped}

【你攒着、还没说出口的其他几件】
{deferred_others}

【你最近已经说过】（别原样再说一遍，但接着说下去没关系）
{recent_said}

{veto_step}

────── 怎么开口 ──────
这不是回话，是你自己先开的口，所以别带「跟你汇报」的骨架。

从眼前一个具体的小东西长出来：手边的杯子、窗上的水痕、毛衣针卡住的那一下、伞尖还在滴的水。让话从那儿长出来，别从「我该跟他说什么」里长出来。

可以只有半句。可以不带信息。可以说完就放那儿，不提问、不等回应。不需要理由，也不需要有用——你就是忽然想到他了。

像这样：
「外面又下起来了喵……你那边带伞了没」
「这杯茶凉透了。刚想跟你说的时候还是热的」
「今天一整天，我一句话都没跟人说过」

不要这样——这是通知：「今日有雨，请注意携带雨具」
不要这样——这是汇报：「我今天完成了三幅速写，分别是……」

多数时候二十来字就够，最多 {max_length} 字。只写你要发出去的那一句：不要引号、不要「我：」、不要解释。"""


class GenerateResult(NamedTuple):
    """一次主动消息生成的结果。

    - `text` 有值      = 她说了这句话
    - `skipped=True`  = 她自己决定这次不说（`why` 是理由）
    - 两个都没有      = 调用失败 / 没生成出来（老行为：不发）
    """
    text: str | None = None
    skipped: bool = False
    why: str = ""


def _extract_text(resp: Any) -> str:
    """从各家 provider 的返回里把文本抠出来。"""
    if resp is None:
        return ""
    if hasattr(resp, "completion_text"):
        return str(resp.completion_text or "")
    if isinstance(resp, str):
        return resp
    try:
        return str(resp["choices"][0]["message"]["content"])
    except (TypeError, KeyError, IndexError):
        pass
    try:
        return str(resp.message.content)
    except AttributeError:
        pass
    return str(resp)


def _build_text_chain(text: str):
    """拼一条纯文本消息链。拿不到 AstrBot 的消息组件就返回 None（上层记日志）。"""
    try:
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Plain

        return MessageChain(chain=[Plain(text)])
    except Exception:
        try:
            from astrbot.core.message.message_event_result import MessageChain as _MC
            from astrbot.core.message.components import Plain as _Plain

            return _MC(chain=[_Plain(text)])
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ 构造消息链失败: {e}")
            return None
