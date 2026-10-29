"""晨光心语 · 日程验收（P4）

生成完先自己过一遍安检，不合格就打回去重写：

1. **时间线**：六个时段（清晨/上午/午间/下午/傍晚/晚间）齐不齐、有没有重复、
   有没有乱序；如果条目给的是 `06:00-08:00` 这种明确区间，还要真的从 00:00
   连续铺到 24:00，不许断口、不许重叠。
2. **穿搭**：缺件（空 / 太短 / 写着「未知」这种占位符）。
3. **活动**：空的、整篇重复同一句。
4. **跟内在状态打架**：精力很低却排满激烈活动、写着「通宵/熬夜」那种透支安排。

刻意**不引入新依赖**，规则全是纯函数，好单测。
"""

from __future__ import annotations

import re
from typing import Any

# 六个模糊时段（和注入、天气、状态机用的是同一套顺序）
PERIOD_ORDER = ("清晨", "上午", "午间", "下午", "傍晚", "晚间")

# 明确的时间区间，例如 06:00-08:00 / 6:00~8:00
_RANGE_RE = re.compile(
    r"(\d{1,2})\s*[:：]\s*(\d{1,2})\s*[-~～—–至到]\s*(\d{1,2})\s*[:：]\s*(\d{1,2})"
)

# 占位符 / 没写的信号
_PLACEHOLDER_WORDS = ("未知", "待定", "无", "n/a", "na", "todo", "待补充", "略", "暂无")

# 高强度活动关键词
_DEFAULT_INTENSE = (
    "跑步", "长跑", "健身", "爬山", "徒步", "马拉松", "打球", "剧烈", "高强度",
    "通宵", "熬夜", "加班", "赶工", "极限", "冲刺", "奔波", "连轴", "远行", "搬家",
)

# 休息类关键词（精力低时出现这些就算她给自己留了余地）
_REST_WORDS = (
    "休息", "歇", "小憩", "午睡", "午觉", "打个盹", "发呆", "放空", "慢", "缓",
    "泡澡", "躺", "听音乐", "看剧", "无所事事", "偷懒", "早睡", "养神",
)


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


def _looks_placeholder(text: str) -> bool:
    t = str(text or "").strip().lower()
    if not t:
        return True
    return t in _PLACEHOLDER_WORDS or len(t) <= 2


class ScheduleValidator:
    """规矩都收在这一个类里，改阈值只动配置，不动逻辑。"""

    def __init__(self, plugin):
        self.plugin = plugin

    # ─────────────────────── 配置 ───────────────────────

    def _cfg(self, key: str, default=None):
        try:
            return self.plugin.get_config(key, default)
        except Exception:
            return default

    def enabled(self) -> bool:
        return _as_bool(self._cfg("schedule_validation.enabled", False), False)

    @property
    def max_retries(self) -> int:
        return max(0, min(5, _as_int(self._cfg("schedule_validation.max_retries", 2), 2)))

    @property
    def min_outfit_len(self) -> int:
        return max(1, _as_int(self._cfg("schedule_validation.min_outfit_len", 8), 8))

    @property
    def low_energy_threshold(self) -> float:
        return _as_float(self._cfg("schedule_validation.low_energy_threshold", 0.35), 0.35)

    @property
    def intense_keywords(self) -> tuple[str, ...]:
        return tuple(_as_list(self._cfg("schedule_validation.intense_keywords", []))) or _DEFAULT_INTENSE

    # ─────────────────────── 主入口 ───────────────────────

    def validate(self, schedule_data: Any, live_state: dict | None = None) -> tuple[bool, list[str]]:
        """验一份日程。返回 (是否通过, 问题清单)。数据根本不成形时也算不过。"""
        errors: list[str] = []
        if not isinstance(schedule_data, dict):
            return False, ["返回的不是一份日程（不是 JSON 对象）"]

        items = schedule_data.get("schedule")
        if not isinstance(items, list) or not items:
            return False, ["日程列表是空的"]

        errors.extend(self._check_outfit(schedule_data.get("outfit")))
        errors.extend(self._check_items(items))
        errors.extend(self._check_timeline(items))
        errors.extend(self._check_state_conflict(schedule_data, live_state))
        return (not errors), errors

    # ─────────────────────── 分项规则 ───────────────────────

    def _check_outfit(self, outfit: Any) -> list[str]:
        text = str(outfit or "").strip()
        if _looks_placeholder(text):
            return ["穿搭缺失或只写了占位符"]
        if len(text) < self.min_outfit_len:
            return [f"穿搭太笼统（只有 {len(text)} 个字，至少要 {self.min_outfit_len} 个字）"]
        return []

    def _check_items(self, items: list) -> list[str]:
        errors: list[str] = []
        seen: dict[str, int] = {}
        empty = 0
        for it in items:
            if isinstance(it, dict):
                act = str(it.get("activity") or "").strip()
            else:
                act = str(it or "").strip()
            if not act:
                empty += 1
                continue
            seen[act] = seen.get(act, 0) + 1
        if empty:
            errors.append(f"有 {empty} 个时段的活动是空的")
        repeated = [a for a, n in seen.items() if n >= 3]
        if repeated:
            errors.append(f"有一段活动被重复写了 {seen[repeated[0]]} 次（像是凑数）：{repeated[0][:24]}")
        return errors

    def _check_timeline(self, items: list) -> list[str]:
        """模糊时段要齐；如果给了明确区间，还要真的连续覆盖 0 点到 24 点。"""
        errors: list[str] = []
        labels = []
        for it in items:
            label = str(it.get("time") or "").strip() if isinstance(it, dict) else ""
            labels.append(label)

        known = [lb for lb in labels if lb in PERIOD_ORDER]
        unknown = [lb for lb in labels if lb and lb not in PERIOD_ORDER]
        strict = _as_bool(self._cfg("schedule_validation.strict_periods", True), True)

        if strict:
            missing = [p for p in PERIOD_ORDER if p not in known]
            duplicated = [p for p in PERIOD_ORDER if known.count(p) > 1]
            if missing:
                errors.append("缺少时段：" + "、".join(missing))
            if duplicated:
                errors.append("时段重复：" + "、".join(sorted(set(duplicated))))
            if not missing and not duplicated:
                idx = [PERIOD_ORDER.index(lb) for lb in known]
                if idx != sorted(idx):
                    errors.append("六个时段的顺序乱了")

        if unknown:
            errors.append("认不出的时段名：" + "、".join(unknown[:3]))

        errors.extend(self._check_explicit_ranges(labels))
        return errors

    def _check_explicit_ranges(self, labels: list[str]) -> list[str]:
        """条目带明确区间时，验一遍 00:00 → 24:00 连续、不重叠。"""
        ranges = []
        for lb in labels:
            m = _RANGE_RE.search(lb or "")
            if not m:
                continue
            try:
                sh, sm, eh, em = (int(g) for g in m.groups())
            except (TypeError, ValueError):
                continue
            if not (0 <= sh <= 24 and 0 <= eh <= 24 and sm < 60 and em < 60):
                continue
            start = sh * 60 + sm
            end = eh * 60 + em
            if end <= start:
                end = start  # 跨零点的写法这里不追究，交给下面的一致性检查
            ranges.append((start, end, lb))
        if len(ranges) < 2:
            return []      # 只有一两条明确区间，说明这是混着写的，不做硬性要求

        errors: list[str] = []
        ranges.sort(key=lambda r: r[0])
        if ranges[0][0] > 0:
            errors.append(f"时间线没从 00:00 开始（最早从 {ranges[0][0] // 60:02d}:{ranges[0][0] % 60:02d} 起）")
        cursor = 0
        for start, end, lb in ranges:
            if start > cursor:
                errors.append(
                    f"时间线有断口：{cursor // 60:02d}:{cursor % 60:02d} → "
                    f"{start // 60:02d}:{start % 60:02d}"
                )
            elif start < cursor:
                errors.append(f"时间线有重叠：{lb}")
            cursor = max(cursor, end)
        if cursor < 24 * 60:
            errors.append(f"时间线没铺到 24:00（只到 {cursor // 60:02d}:{cursor % 60:02d}）")
        return errors[:3]

    def _check_state_conflict(self, schedule_data: dict, live_state: dict | None) -> list[str]:
        """跟内在状态打架：精力很低却排满激烈活动。"""
        state = dict(schedule_data.get("state") or {})
        energy = None
        if isinstance(live_state, dict) and live_state.get("energy") is not None:
            energy = _as_float(live_state.get("energy"), 1.0)
        elif state.get("energy") is not None:
            energy = _as_float(state.get("energy"), 1.0)
        if energy is None or energy > self.low_energy_threshold:
            return []

        errors: list[str] = []
        intense = self.intense_keywords
        blob_parts = []
        for it in schedule_data.get("schedule") or []:
            if isinstance(it, dict):
                blob_parts.append(str(it.get("activity") or ""))
            else:
                blob_parts.append(str(it or ""))
        blob = " ".join(blob_parts)
        hits = [w for w in intense if w in blob]
        if len(hits) >= 2:
            errors.append(
                f"精力只有 {energy:.2f}，却安排了 {len(hits)} 项激烈/透支的活动（{'、'.join(hits[:3])}）"
            )
        elif hits and not any(w in blob for w in _REST_WORDS):
            errors.append(f"精力只有 {energy:.2f}，却安排了「{hits[0]}」这样的硬安排，一天里也没留休息")

        # 六格全排满、一点余地不留
        items = [it for it in (schedule_data.get("schedule") or []) if it]
        if len(items) >= 6 and energy <= self.low_energy_threshold and not any(w in blob for w in _REST_WORDS):
            errors.append(f"精力只有 {energy:.2f}，六个时段却排得满满的，没留一点休息")
        return errors

    # ─────────────────────── 兜底 ───────────────────────

    @staticmethod
    def safe_placeholder(live_state: dict | None, seed_state: dict | None = None,
                         reason: str = "") -> dict:
        """验不过又不肯好好改时，落一份「安全牌」日程——不精彩，但一定不出错。"""
        energy = _as_float((live_state or {}).get("energy"),
                           _as_float((seed_state or {}).get("energy"), 0.6))
        mood = ""
        if isinstance(live_state, dict):
            mood = str(live_state.get("mood") or "")
        elif isinstance(seed_state, dict):
            mood = str((seed_state.get("mood") or {}).get("label") or "")

        threads = _as_list((seed_state or {}).get("open_threads")) + _as_list(
            (seed_state or {}).get("carried_over")
        )
        thread = threads[0] if threads else ""

        if energy < 0.4:
            schedule = [
                {"time": "清晨", "activity": "慢慢醒过来，先给自己一点缓冲。"},
                {"time": "上午", "activity": "做点不费神的小事，边做边歇。"},
                {"time": "午间", "activity": "好好吃一顿，然后小憩半小时。"},
                {"time": "下午", "activity": f"接上没做完的那件小事：{thread}。" if thread else "挑一件最轻的事慢慢做。"},
                {"time": "傍晚", "activity": "出门透透气，走一小段。"},
                {"time": "晚间", "activity": "早点收工，泡个澡就躺下。"},
            ]
        else:
            schedule = [
                {"time": "清晨", "activity": "起床梳洗，看看窗外是什么天气。"},
                {"time": "上午", "activity": f"先把这件惦记的事推进一点：{thread}。" if thread else "按着自己的节奏做点正事。"},
                {"time": "午间", "activity": "吃饭，顺便放空一会儿。"},
                {"time": "下午", "activity": "接着上午的线头往下做，中间伸个懒腰。"},
                {"time": "傍晚", "activity": "收拾一下手边的东西，让屋里清清爽爽。"},
                {"time": "晚间", "activity": "安静下来，写两行今天的事，然后早点睡。"},
            ]

        note = "（安全兜底日程）这一份没让模型发挥，先稳稳当当过一天。"
        if reason:
            note = f"（安全兜底日程：{reason[:40]}）先稳稳当当过一天。"

        return {
            "intent": "稳稳当当过一天",
            "outfit": "按天气挑了舒服的一身：柔软的上衣配长裤，外面加一件方便穿脱的外套。",
            "schedule": schedule,
            "note": note,
            "state": seed_state or {},
            "mood_hint": mood,
        }

    @staticmethod
    def describe(errors: list[str]) -> str:
        """把问题清单压成一行，给日志 / 纠错提示词用。"""
        return "；".join(str(e) for e in errors if e)[:300]
