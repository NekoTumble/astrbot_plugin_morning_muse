"""晨光心语 · 状态存储（Phase 0）

日程 JSON 里新增一个 `state` 块，用来记录「可以被明天继承的内在状态」，
把「每天平行、互不相干」的抽签式日程，改成「从一条持续运行的线里长出来」。

设计原则：
- 对老数据完全透明：没有 `state` 键的历史 JSON 照常读取，不会崩；
- 不引入新依赖：只用标准库；
- LLM 没给 state 时，用规则从日程文本兜一个（关键词 + 时段启发式），
  这样即使断网走兜底，跨天因果也不会断。
"""

from __future__ import annotations

import hashlib
import random
import re
from datetime import date, timedelta
from typing import Any

from .timezone_utils import get_today

try:
    from astrbot.api import logger
except ImportError:  # 独立自测时（不在 AstrBot 运行环境内）
    import logging

    logger = logging.getLogger(__name__)


# 心情极性（用来在没给出 valence 时，从文字里猜一个方向）
_POSITIVE_WORDS = (
    "开心", "愉快", "满足", "兴奋", "期待", "雀跃", "放松", "安心",
    "温暖", "甜", "幸福", "得意", "元气", "轻快", "踏实",
)
_NEGATIVE_WORDS = (
    "低落", "难过", "闷", "疲惫", "累", "烦躁", "焦虑", "不安",
    "孤单", "失落", "怅然", "委屈", "丧", "怅", "空落",
)

# 「还没了结」的信号词——兜底时用它在活动文本里找线头
_OPEN_HINTS = (
    "还没", "没有", "未", "打算", "想要", "准备", "计划",
    "明天", "下次", "回头", "继续", "拖", "留到", "待",
)

# 「放慢 / 透支」的信号词——兜底时用它微调精力
_TIRED_WORDS = ("疲惫", "累", "放慢", "休息", "慵懒", "慢一点", "歇", "缓")
_VIGOR_WORDS = ("元气", "满满", "干劲", "兴奋", "精神")

# 最多记几条线头（写进 JSON 的上限，注入时另有上限）
_MAX_THREADS_IN_JSON = 5


def safe_persona_id(name: str) -> str:
    """把人格名映射成一个稳定的英文 id。

    aura-life 只认 [a-z0-9_-]{1,64}，而我们的 persona 经常是中文名；
    统一在这里做映射，保证 db 路径、事件账本、记忆池三个目录始终一致。
    对外（日志、命令）照旧用中文名。
    """
    raw = (name or "").strip() or "default"
    if re.fullmatch(r"[a-z0-9_-]{1,64}", raw):
        return raw
    return "p_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _clamp(value: Any, lo: float, hi: float, default: float) -> float:
    """把数值钳制到 [lo, hi]；非数值一律给 default。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def _as_str_list(value: Any, limit: int = _MAX_THREADS_IN_JSON) -> list[str]:
    """把 LLM 可能给的字符串 / 列表，统一成干净的字符串列表。"""
    if isinstance(value, str):
        # 只按分号 / 换行拆——中文逗号、顿号在正常句子里太常见，拆了会把一条线头剁成两截
        items = [p.strip() for p in re.split(r"[;；\n]", value) if p.strip()]
    elif isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value if str(x).strip()]
    else:
        items = []
    # 去重、保序
    seen, out = set(), []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out[:limit]


def _guess_valence(text: str) -> float:
    """从一段文字里粗判愉悦度（-1 ~ +1）。"""
    if not text:
        return 0.0
    score = 0.0
    for w in _POSITIVE_WORDS:
        if w in text:
            score += 0.35
    for w in _NEGATIVE_WORDS:
        if w in text:
            score -= 0.35
    return max(-1.0, min(1.0, score))


def _guess_label(text: str) -> str:
    """从一段文字里挑一个情绪词当 label，挑不到就是「平静」。"""
    for w in _NEGATIVE_WORDS + _POSITIVE_WORDS:
        if w in text:
            return w
    return "平静"


class StateStore:
    """`state` 块的读写 + 跨天取用。（依赖 ScheduleStorage，但不改动它）"""

    def __init__(self, storage):
        self.storage = storage

    # ─────────────────────────── 读 ───────────────────────────

    @staticmethod
    def extract(schedule_data: dict | None) -> dict | None:
        """从一份日程里取出 state 块；没有 / 不合法 → None。"""
        if not isinstance(schedule_data, dict):
            return None
        state = schedule_data.get("state")
        if isinstance(state, dict) and state:
            return state
        return None

    async def get_state(self, persona_id: str, d: date) -> dict | None:
        """读某一天的 state 块（没有就 None）。"""
        try:
            data = await self.storage.load_schedule(persona_id, d)
        except Exception as e:  # storage 自己吞异常，这里再兜一层
            logger.warning(f"[晨光心语] 读取 {persona_id} {d} 状态失败: {e}")
            return None
        return self.extract(data)

    async def get_yesterday_state(self, persona_id: str,
                                  today: date | None = None) -> dict | None:
        """读「昨天」的 state。跨天因果的入口。"""
        today = today or get_today()
        return await self.get_state(persona_id, today - timedelta(days=1))

    # ─────────────────────────── 规整 ───────────────────────────

    @classmethod
    def normalize(cls, raw: Any, today: date) -> dict[str, Any]:
        """把 LLM 给的（或半成品的）state 补成标准结构。"""
        raw = raw if isinstance(raw, dict) else {}
        mood_raw = raw.get("mood")

        if isinstance(mood_raw, dict):
            label = str(mood_raw.get("label") or "").strip() or "平静"
            valence = _clamp(mood_raw.get("valence"), -1.0, 1.0, _guess_valence(label))
            intensity = _clamp(mood_raw.get("intensity"), 0.0, 1.0, 0.5)
        else:
            label = str(mood_raw or "").strip() or "平静"
            valence = _guess_valence(label)
            intensity = 0.5

        return {
            "date": str(raw.get("date") or today.isoformat()),
            "energy": round(_clamp(raw.get("energy"), 0.0, 1.0, 0.6), 2),
            "mood": {
                "label": label,
                "valence": round(valence, 2),
                "intensity": round(intensity, 2),
            },
            "focus": str(raw.get("focus") or "").strip(),
            "open_threads": _as_str_list(raw.get("open_threads")),
            "carried_over": _as_str_list(raw.get("carried_over")),
            "tomorrow_hint": str(raw.get("tomorrow_hint") or "").strip(),
            # 昨天穿的是什么——兜底生成时用来延续，别在 normalize 里丢掉
            "outfit_hint": str(raw.get("outfit_hint") or "").strip(),
        }

    # ─────────────────── 规则兜底：从日程文本合成 ───────────────────

    @classmethod
    def from_schedule(cls, schedule_data: dict | None, today: date) -> dict[str, Any]:
        """LLM 没给 state 时，用规则从日程文本里兜一个。

        质量当然不如 LLM 直接给的，但足够让「跨天连续性」不断线——
        这正是断网兜底日程里最容易被看穿的那一环。
        """
        data = schedule_data if isinstance(schedule_data, dict) else {}

        activities: list[str] = []
        for item in data.get("schedule") or []:
            if isinstance(item, dict):
                act = str(item.get("activity") or "").strip()
            else:
                act = str(item or "").strip()
            if act:
                activities.append(act)

        note = str(data.get("note") or "").strip()
        outfit = str(data.get("outfit") or "").strip()
        blob = " ".join(activities) + " " + note

        # 线头：活动里带「还没 / 打算 / 明天…」这类未完成信号的
        threads = [a for a in activities if any(h in a for h in _OPEN_HINTS)]

        # 精力：默认 0.6，按文字线索小幅上下浮动
        energy = 0.6
        for w in _TIRED_WORDS:
            if w in blob:
                energy -= 0.12
        for w in _VIGOR_WORDS:
            if w in blob:
                energy += 0.12

        focus = activities[0][:40] if activities else ""
        label = _guess_label(blob)

        state = {
            "date": today.isoformat(),
            "energy": energy,
            "mood": {
                "label": label,
                "valence": _guess_valence(blob),
                "intensity": 0.4,
            },
            "focus": focus,
            "open_threads": threads,
            "carried_over": [],
            "tomorrow_hint": "",
        }
        # 顺手把 outfit 的存在感带上（部分人格很在意穿搭的延续）
        if outfit:
            state["outfit_hint"] = outfit[:40]
        return cls.normalize(state, today)

    # ─────────────── 昨天 → 今天：兜底生成的起点状态 ───────────────

    @classmethod
    def seed_from_yesterday(cls, prev: dict | None, today: date) -> dict[str, Any]:
        """由昨日 state 推出今日起点状态，供兜底生成使用。

        - 精力：昨日 ± 0.15（睡一觉不会满血，也不会原地不动）
        - 情绪：愉悦度向 0 回归 30%（情绪会淡，但不会一夜清空）
        - 线头：原样顺延，今天接着做
        """
        if not prev:
            return cls.default(today)

        prev = cls.normalize(prev, today)
        mood = prev["mood"]

        # 精力：昨日 ± 0.15，并带一点极轻的「向 0.6 回归」，防止长期随机游走跑到极端
        energy = _clamp(prev["energy"] * 0.85 + 0.09 + random.uniform(-0.15, 0.15), 0.05, 0.95, 0.6)
        valence = _clamp(mood["valence"] * 0.7, -1.0, 1.0, 0.0)
        intensity = _clamp(mood["intensity"] * 0.8, 0.0, 1.0, 0.4)

        threads = (prev.get("open_threads") or []) + (prev.get("carried_over") or [])
        threads = _as_str_list(threads)

        return {
            "date": today.isoformat(),
            "energy": round(energy, 2),
            "mood": {
                "label": mood["label"],
                "valence": round(valence, 2),
                "intensity": round(intensity, 2),
            },
            "focus": prev.get("focus") or (threads[0] if threads else ""),
            "open_threads": threads[:3],
            "carried_over": _as_str_list(prev.get("carried_over")),
            "tomorrow_hint": prev.get("tomorrow_hint", ""),
            "outfit_hint": prev.get("outfit_hint", ""),
        }

    @staticmethod
    def default(today: date) -> dict[str, Any]:
        """新人格首日 / 完全没有历史时的空状态。"""
        return {
            "date": today.isoformat(),
            "energy": 0.6,
            "mood": {"label": "平静", "valence": 0.0, "intensity": 0.4},
            "focus": "",
            "open_threads": [],
            "carried_over": [],
            "tomorrow_hint": "",
        }

    # ─────────────────── 渲染成给 LLM 看的文本 ───────────────────

    @classmethod
    def summarize(cls, state: dict | None) -> str:
        """把 state 压成一段「昨日状态」文本，填进 {yesterday_state}。"""
        if not state:
            return ""
        s = cls.normalize(state, get_today())
        mood = s["mood"]
        lines = [
            f"精力：{s['energy']:.2f}（0=透支，1=满格）",
            f"心情：{mood['label']}（愉悦度 {mood['valence']:+.1f}，强度 {mood['intensity']:.1f}）",
        ]
        if s["focus"]:
            lines.append(f"当时最挂心的事：{s['focus']}")
        if s["tomorrow_hint"]:
            lines.append(f"当时对第二天的想法：{s['tomorrow_hint']}")
        return "\n".join(lines)

    @classmethod
    def threads_text(cls, state: dict | None, limit: int = 3) -> str:
        """把未了结的线头渲染成列表文本，填进 {open_threads}。"""
        if not state:
            return ""
        items = (state.get("open_threads") or []) + (state.get("carried_over") or [])
        items = _as_str_list(items, limit=limit)
        if not items:
            return ""
        return "\n".join(f"- {t}" for t in items)
