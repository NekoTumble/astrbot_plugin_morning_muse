"""晨光心语 · 内在状态机桥（Phase 1）

把 `aura-life` 当成一个「内在状态机」接进来：它只负责提供状态数值和一份
粗糙的日用骨架，文案仍然全部交给原有的 LLM 管线去写。

设计要点（都踩过点，别随手改）：

1. **不常驻、零后台线程**：不调用 `LifeService.start()`（那会起 AsyncIOScheduler）。
   时钟由插件自己掌握——每次生成日程前 tick 一次，可选每 N 分钟轻量 tick 一次。
2. **私有 API 只有一个封装点**：`force_all_ticks()` 属于私有面，只在 `_tick_sync()`
   里调一次，并整体用 `asyncio.to_thread()` 包住（它是同步阻塞的，直接调会卡住对话）。
3. **所有失败都降级**：任何一步抛异常都只记 warning 并返回 None，上层自动回落到
   Phase 0 的 state（也就是「没有状态机也能跑」）。
4. **不碰 AstrBot 的人格系统**：人格映射单独存在 persona_defs/<persona>.json。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .state_store import safe_persona_id as _safe_persona_id

try:
    from aura_life import LifeService
    from aura_life import hooks as aura_hooks
    from aura_life.personas.personality_config import PersonalityDefinition

    _HAS_AURA = True
except ImportError:  # 没装上也不该拖垮插件
    _HAS_AURA = False
    logger.warning("[晨光心语] ⚠️ 未安装 aura-life，内在状态机（Phase 1）不可用，已自动降级")

# 我们自己的 6 个模糊时段 ↔ 一天中的整点（和 weather_client.period_map 同一套）
PERIOD_SLOTS = [
    ("清晨", 6),
    ("上午", 9),
    ("午间", 12),
    ("下午", 15),
    ("傍晚", 18),
    ("晚间", 21),
]

# aura-life 的 hook 是模块级全局的，只配置一次
_hooks_configured = False


class _LifeCfg:
    """`get_config` hook 的返回值。不填这个 hook，每个 activity tick 都会刷一条 WARNING。"""

    def __init__(self, data_dir: Path, timezone: str):
        self.data_dir = str(data_dir)
        self.timezone = timezone
        # V1 修复：aura-life 每次 world tick 都会读 `get_config().place_enabled`；
        # 缺这个属性会抛 AttributeError（被 aura 自己 catch 成 debug 日志，且位置功能永久关闭）。
        # 先给 False —— 只消报错、不改变现有行为（等价于「位置功能关闭」的现状）。
        self.place_enabled = False


def _clean_activity(raw: str) -> str:
    """把计划行里的备注（[want: xxx]、(planned)）摘掉，只留活动名。"""
    s = (raw or "").strip()
    s = re.sub(r"\s*\((?:planned|done)\)\s*$", "", s)
    s = re.sub(r"\s*\[[^\]]*\]\s*$", "", s)
    return s.strip()


def _parse_plan(schedule_text: str) -> dict[int, str]:
    """把 planner.schedule 的文本解析成 {小时: 活动名}。"""
    out: dict[int, str] = {}
    for line in (schedule_text or "").splitlines():
        m = re.match(r"\s*(\d{1,2}):\d{2}\s*-\s*(.+)", line)
        if not m:
            continue
        try:
            hour = int(m.group(1))
        except ValueError:
            continue
        act = _clean_activity(m.group(2))
        if act:
            out[hour] = act
    return out


# ── Step 1：日程 → 状态机 · 回灌的规则表 ──
# 六个时段各从几点算「到点」（和 weather_client.period_map、PERIOD_SLOTS 同一套）
_PERIOD_HOUR = {"清晨": 6, "上午": 9, "午间": 12, "下午": 15, "傍晚": 18, "晚间": 21}

# (关键词, 映射到的活动名, 允许出现的时段标签)
# 只收录「一眼就能确定」的说法；拿不准的一律不进表 —— 宁可少做，不可乱做。
# 顺序即优先级：「吃了午饭睡了个午觉」要先落进 napping，别被 sleeping 抢走。
_REPLAY_RULES = (
    (("午休", "午睡", "午觉", "小憩", "打个盹", "打了个盹", "眯一会儿", "眯一会"), "napping", ("午间", "下午")),
    (("瑜伽",), "yoga", ("清晨", "上午")),
    (("伸展", "拉伸"), "stretching", ("清晨", "上午", "下午", "傍晚")),
    (("散步", "出门走走", "出去走走", "遛弯"), "going for a walk", ("上午", "午间", "下午", "傍晚")),
    # AI 人格走不到「自主睡觉」那条分支，这里只认晚间明确要睡的，且当天只回灌一次
    (("睡觉", "睡了", "睡下", "入睡"), "sleeping", ("晚间",)),
)


# 关键词前面的「否定小尾巴」：日程是自由文本，"没去拿瑜伽垫"里也有「瑜伽」两个字。
# 只在关键词前面几个字里找，不做过度联想。
_NEGATIONS = ("没", "不", "未", "别", "懒得", "忘了", "顾不上", "算了", "省了")


def _match_replay_activity(text: str, slot: str) -> str:
    """从一条日程活动文本里认出高置信的活动名；认不出就返回空串。

    「宁可少做，不可乱做」两条闸门：
    1. 时段不对不认（"早点睡觉"写在上午，不算）；
    2. 关键词前面带着否定词也不认 —— "爬起来也没去拿瑜伽垫" 可不等于她做了瑜伽。
    """
    s = str(text or "")
    for words, name, slots in _REPLAY_RULES:
        if slots and slot not in slots:
            continue
        for w in words:
            idx = s.find(w)
            while idx >= 0:
                head = s[max(0, idx - 4):idx]
                if not any(neg in head for neg in _NEGATIONS):
                    return name
                idx = s.find(w, idx + 1)
    return ""


# 状态机的活动名是英文（relaxing / napping …），直接塞进中文提示词读着别扭。
# 这里给常见活动配一张小译名表，没收录的原样返回 —— 只影响注入的读感，不碰任何状态。
_ACTIVITY_ZH = {
    "relaxing": "放松一下",
    "resting": "歇着",
    "napping": "小憩",
    "sleeping": "睡觉",
    "stretching": "伸展一下",
    "yoga": "做瑜伽",
    "meditating": "冥想",
    "reading": "看书",
    "journaling": "写日记",
    "writing poetry": "写点东西",
    "sketching ideas": "随手画点东西",
    "learning something new": "学点新东西",
    "listening to music": "听音乐",
    "creating a playlist": "整理歌单",
    "watching tv": "看电视",
    "scrolling": "刷手机",
    "online shopping": "逛网店",
    "stargazing": "看星星",
    "watching the snow fall": "看雪",
    "watching sunset": "看日落",
    "daydreaming": "发呆",
    "thinking about user": "想着主人",
    "remembering happy moments": "回忆开心的事",
    "preparing something to share": "准备想跟你说的事",
    "texting a friend": "跟朋友发消息",
    "video call with a friend": "跟朋友视频",
    "catching up with family": "跟家人聊聊天",
    "puzzles and games": "拼图、玩点小游戏",
    "morning shower": "冲个澡",
    "taking a long bath": "泡个澡",
    "skincare routine": "护肤",
    "evening routine": "晚间例行事",
    "tidying up": "收拾屋子",
    "cooking": "做饭",
    "baking": "烘焙",
    "eating": "吃饭",
    "breakfast": "吃早饭",
    "lunch": "吃午饭",
    "dinner": "吃晚饭",
    "groceries": "买菜",
    "going for a walk": "散步",
    "going for a run": "跑步",
    "gym workout": "健身",
    "dancing": "跳舞",
    "visiting memory beach": "去那片回忆的海边",
    "wandering": "闲逛",
    "exploring": "到处探索",
}


def activity_zh(name: str) -> str:
    """把状态机的英文活动名翻成中文；没收录的原样返回。"""
    if not name:
        return ""
    raw = str(name).strip()
    return _ACTIVITY_ZH.get(raw.lower(), raw)


# 心情是 aura-life 的 affect 系统给的五个词：neutral / content / blue / raw / restless。
_MOOD_ZH = {
    "neutral": "平静",
    "content": "满足",
    "blue": "低落",
    "raw": "心里发涩",
    "restless": "坐立不安",
}


def mood_zh(name: str) -> str:
    """把状态机的英文心情翻成中文；没收录的原样返回。"""
    if not name:
        return ""
    raw = str(name).strip()
    return _MOOD_ZH.get(raw.lower(), raw)


# 精力档是 EnergyLevel 枚举：exhausted / tired / resting / comfortable / alert / energized。
_ENERGY_ZH = {
    "exhausted": "快没电了",
    "tired": "有点累",
    "resting": "半歇着",
    "comfortable": "还撑得住",
    "alert": "挺精神",
    "energized": "满电",
}


def energy_zh(name: str) -> str:
    """把状态机的英文精力档翻成中文；没收录的原样返回。"""
    if not name:
        return ""
    raw = str(name).strip()
    return _ENERGY_ZH.get(raw.lower(), raw)


# 所在是位置键：默认那 14 个 + 常见居家房间（legacy 键）。
_LOCATION_ZH = {
    "home": "家",
    "in transit": "路上",
    "cafe": "咖啡馆",
    "park": "公园",
    "gym": "健身房",
    "library": "图书馆",
    "street": "街上",
    "workplace": "工作的地方",
    "restaurant": "餐厅",
    "bar": "小酒馆",
    "rooftop": "天台",
    "beach": "海边",
    "school": "学校",
    "campus": "校园",
    "bookstore": "书店",
    "museum": "美术馆",
    "bedroom": "卧室",
    "living room": "客厅",
    "kitchen": "厨房",
    "garden": "花园",
    "study nook": "书房角落",
    "balcony": "阳台",
}


def location_zh(name: str) -> str:
    """把状态机的英文位置键翻成中文；没收录的原样返回。"""
    if not name:
        return ""
    raw = str(name).strip()
    return _LOCATION_ZH.get(raw.lower(), raw)


class LifeBridgeManager:
    """按 persona 惰性创建 / 持有 / 销毁 `LifeService`。"""

    def __init__(self, plugin, data_dir: Path, max_personas: int = 5):
        self.plugin = plugin
        self.data_dir = Path(data_dir)
        self.max_personas = max(1, int(max_personas or 5))
        self._services: OrderedDict[str, Any] = OrderedDict()
        # P6：主人最后一次说话的时间（用来判断「她此刻醒着没」）
        self._last_user_msg: dict[str, datetime] = {}
        self._lock = asyncio.Lock()
        # V10 修复：日程回灌「读记号→执行→写记号」专用锁。
        # 与 _lock 分开，避免与 _get_service 内部取同一把锁而死锁。
        self._replay_lock = asyncio.Lock()
        # ★ get_state 的短 TTL 缓存（见 get_state 注释）。只缓存 get_state，
        # 不碰 shareable —— 后者带 mark_shared 的记账副作用，缓存结果会漏记。
        self._state_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._STATE_TTL = 40.0
        self._configure_hooks()

    # ─────────────────────── 基础设施 ───────────────────────

    def available(self) -> bool:
        return _HAS_AURA

    def _timezone(self) -> str:
        try:
            return self.plugin.get_config("schedule.timezone", "Asia/Shanghai") or "Asia/Shanghai"
        except Exception:
            return "Asia/Shanghai"

    def _configure_hooks(self):
        """配置 aura-life 的全局 hook。只填「必填的两个 + 天气」，其余留不填。"""
        global _hooks_configured
        if not _HAS_AURA or _hooks_configured:
            return

        def _persona_now(timezone: str | None = None) -> datetime:
            # 和日程用同一条时区，避免状态机的「现在」跟日程差几个小时
            try:
                from .timezone_utils import get_now

                return get_now(timezone or self._timezone())
            except Exception:
                return datetime.now()

        def _noop_visual_update(*args, **kwargs) -> None:
            # aura-life 每结束一次活动 tick，都会起个后台线程调这个钩子去「重新
            # 生成角色的外观描述」。本插件没有图像服务，配个空实现即可——不配的话
            # 它会每次抛异常、在日志里刷一条 WARNING（不影响状态，只是脏日志）。
            return None

        try:
            aura_hooks.configure(
                get_config=lambda: _LifeCfg(self.data_dir, self._timezone()),
                persona_now=_persona_now,
                generate_and_update=_noop_visual_update,
            )
            _hooks_configured = True
            logger.info(
                "[晨光心语] 🔌 aura-life hooks 已配置（get_config / persona_now / generate_and_update）"
            )
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ 配置 aura-life hooks 失败: {e}")

    def _def_path(self, persona_id: str) -> Path:
        return self.data_dir / "persona_defs" / f"{_safe_persona_id(persona_id)}.json"

    def _db_path(self, persona_id: str) -> Path:
        return self.data_dir / "life" / _safe_persona_id(persona_id) / "life.db"

    def _load_definition(self, persona_id: str) -> Any:
        """读人格映射；没有就按当前风格生成一份骨架（用户可以自己改）。"""
        path = self._def_path(persona_id)
        raw: dict[str, Any] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8")) or {}
            except Exception as e:
                logger.warning(f"[晨光心语] ⚠️ {persona_id} 读取人格映射 {path.name} 失败，改用默认: {e}")
                raw = {}
        else:
            try:
                style = self.plugin.get_style_for_persona(persona_id)
            except Exception:
                style = ""
            raw = {
                "id": _safe_persona_id(persona_id),
                "name": persona_id,
                # "ai" = 关掉生理 / 金钱 / 职业 / 通勤这些物理子系统，也不会自己睡觉
                "persona_type": "ai",
                "occupation": "女仆",
                "core_traits": [],
                "interests": [],
                "avoids": [],
                "style_vibe": style or "",
                # R5 数值化性格：0~1，越接近 1 越强。想让她的性格不随日子漂，调这里。
                "ocean": {
                    "openness": 0.70,          # 开放：愿不愿意尝试新东西
                    "conscientiousness": 0.55,  # 尽责：做事有没有条理
                    "extraversion": 0.35,       # 外向：喜不喜欢往外跑
                    "agreeableness": 0.75,      # 亲和：对人软不软
                    "neuroticism": 0.60,        # 敏感：情绪起伏大不大
                },
            }
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
                logger.info(f"[晨光心语] 📝 {persona_id} 已生成人格映射骨架: {path}")
            except Exception as e:
                logger.warning(f"[晨光心语] ⚠️ {persona_id} 写入人格映射失败: {e}")

        fields = {
            "id", "name", "persona_type", "occupation", "core_traits", "interests",
            "avoids", "style_vibe", "communication_traits", "quirks",
            "relationship_style", "relationship_with_user", "emotional_baseline",
        }
        kwargs = {k: v for k, v in raw.items() if k in fields}
        # 内部 id 必须是 aura-life 认的英文 id；显示名保留中文人格名
        kwargs["id"] = _safe_persona_id(persona_id)
        kwargs.setdefault("name", persona_id)
        # 设置页说了算：这样「存在形式」那个开关才不是摆设
        configured_type = ""
        try:
            configured_type = (self.plugin.get_config("life_engine.persona_type", "") or "").strip()
        except Exception:
            configured_type = ""
        kwargs["persona_type"] = configured_type or raw.get("persona_type") or "ai"
        # R5：把数值化性格喂给状态机当情绪基线（没配 emotional_baseline 时才用 ocean）
        if not kwargs.get("emotional_baseline"):
            ocean = raw.get("ocean")
            if isinstance(ocean, dict):
                baseline = {}
                for k, v in ocean.items():
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        continue
                    baseline[str(k)] = max(0.0, min(1.0, fv))
                if baseline:
                    kwargs["emotional_baseline"] = baseline
        return PersonalityDefinition(**kwargs)

    def _build_service(self, persona_id: str):
        """同步构造函数，调用方负责 to_thread。"""
        db = self._db_path(persona_id)
        db.parent.mkdir(parents=True, exist_ok=True)
        svc = LifeService(
            db_path=str(db),
            definition=self._load_definition(persona_id),
            persona_id=persona_id,
        )
        # ★ Step 0.5 的真病灶：补上 `start()` 里的「读档」那半边。
        # 我们刻意不调 `start()`（它会起 AsyncIOScheduler），但整份状态**全靠**
        # `_load_state()` 从库里读回来——不读，每建一次 service 就整体回到出厂默认：
        # 精力永远 0.7、活动日志/目标/心情/位置全丢，数值当然「飘」。
        # 这里只做 start() 的读档部分，不起调度器、不起初始 tick 线程、不 assign_home。
        try:
            if not getattr(svc, "_is_initialized", False):
                svc._load_state()
                svc._goal_engine.initialize_goals()
                svc._is_initialized = True
                logger.info(f"[晨光心语] 📂 已读回 {persona_id} 的状态存档")
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 读取状态存档失败（按默认值跑）: {e}")
        return svc

    # ---------- Step 0.5：精力落盘 ----------
    @staticmethod
    def _ensure_energy_row(svc) -> bool:
        """确保 `life_energy_state` 里有 id=1 那一行（读档之后的「补落盘」）。

        aura-life 的两个坑凑在一起：`_load_state()` 发现空表时只 INSERT、
        **不 commit**（关闭连接时整个事务回滚，那一行永远不落盘）；而
        `_save_state()` 走的是 `UPDATE ... WHERE id = 1`（更新不到不存在的行）。
        两头一夹 → 精力从来没写进过数据库，状态机一重建就回到默认 0.7。
        读档（`_build_service` 里）之后再补上这一行：已有行就不动，
        之后交给 aura-life 自己的 `_save_state` 更新。
        """
        _pid = getattr(svc, "_persona_id", "") or "?"
        energy = getattr(svc, "_energy", None)
        db_path = getattr(svc, "_db_path", None)
        if energy is None or not db_path:
            return False
        # V2 修复：老数据里可能存着带时区的 last_sleep_time（历史遗留），会让每轮
        # energy tick 一直报 naive/aware 相减。读档后先就地归一化成 naive 并写回。
        try:
            # 这个方法是 staticmethod，没有 self —— 之前写 self. 直接 NameError，
            # 被 except 吞成 debug 日志，等于 V2 归一化从来没跑过。
            LifeBridgeManager._normalize_energy_clock(svc, db_path)
        except Exception as e:
            logger.debug(f"[晨光心语] {_pid} 精力时间戳归一化跳过: {e}")
        try:
            data = energy.to_dict()
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {_pid} 读取精力快照失败: {e}")
            return False
        try:
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM life_energy_state WHERE id = 1")
                if cur.fetchone()[0]:
                    return False  # 已经有行了，别覆盖
                cur.execute(
                    "INSERT INTO life_energy_state "
                    "(id, level, fatigue, caffeine_boost, inspiration_boost, social_boost, "
                    " hours_awake, last_sleep_time, last_update) "
                    "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        data.get("level", 0.7),
                        data.get("fatigue", 0.0),
                        data.get("caffeine_boost", 0.0),
                        data.get("inspiration_boost", 0.0),
                        data.get("social_boost", 0.0),
                        data.get("hours_awake", 0.0),
                        data.get("last_sleep_time"),
                        data.get("last_update"),
                    ),
                )
                conn.commit()
            logger.info(f"[晨光心语] 💾 {_pid} 精力已补落盘（当前 {data.get('level')}）")
            return True
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {_pid} 补写精力行失败（已降级）: {e}")
            return False

    @staticmethod
    def _normalize_energy_clock(svc, db_path) -> bool:
        """V2 修复：把内存态/库里带时区的历史时间戳归一化成 naive（引擎全库 naive）。

        历史遗留专用：老版本会把带时区的本地时间写进 last_sleep_time。归一化后，
        energy tick 里的 (now - last_sleep_time) 才不会抛 naive/aware 相减。失败只降级。
        """
        _pid = getattr(svc, "_persona_id", "") or "?"
        energy = getattr(svc, "_energy", None)
        st = getattr(energy, "state", None) if energy is not None else None
        if st is None:
            return False
        changed = False
        for attr in ("last_sleep_time", "last_update"):
            val = getattr(st, attr, None)
            if isinstance(val, datetime) and val.tzinfo is not None:
                setattr(st, attr, val.replace(tzinfo=None))
                changed = True
        if not changed:
            return False
        try:
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.execute(
                    "UPDATE life_energy_state SET last_sleep_time = ?, last_update = ? WHERE id = 1",
                    (
                        st.last_sleep_time.isoformat() if getattr(st, "last_sleep_time", None) else None,
                        st.last_update.isoformat() if getattr(st, "last_update", None) else None,
                    ),
                )
                conn.commit()
            logger.info(f"[晨光心语] 💾 {_pid} 精力时间戳已归一化（naive）")
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {_pid} 归一化时间戳写回失败（已降级）: {e}")
        return True

    async def _get_service(self, persona_id: str):
        if not _HAS_AURA:
            return None
        if persona_id in self._services:
            self._services.move_to_end(persona_id)
            return self._services[persona_id]
        async with self._lock:
            if persona_id in self._services:
                self._services.move_to_end(persona_id)
                return self._services[persona_id]
            # 超出上限就淘汰最久没用过的那个
            while len(self._services) >= self.max_personas:
                old_pid, old_svc = self._services.popitem(last=False)
                try:
                    await asyncio.to_thread(old_svc.stop)
                except Exception:
                    pass
                logger.info(f"[晨光心语] ♻️ 状态机超出上限，已释放 {old_pid}")
            try:
                svc = await asyncio.to_thread(self._build_service, persona_id)
            except Exception as e:
                logger.warning(f"[晨光心语] ⚠️ 创建 {persona_id} 的状态机失败: {e}")
                return None
            # Step 0.5：刚建好的状态机补一条精力行，精力才真的落盘（见方法注释）
            try:
                await asyncio.to_thread(self._ensure_energy_row, svc)
            except Exception as e:
                logger.warning(f"[晨光心语] ⚠️ {persona_id} 精力落盘检查失败（已降级）: {e}")
            self._services[persona_id] = svc
            logger.info(f"[晨光心语] 🫀 已为 {persona_id} 创建内在状态机")
            return svc

    # ─────────────────────── 对外接口（全部异步、全部会降级） ───────────────────────

    async def catch_up(self, persona_id: str, downtime_hours: float) -> bool:
        """停机补算。服务器关了一夜 / 一个周末后靠它把状态推回现在。"""
        if not _HAS_AURA:
            return False
        if downtime_hours is None or downtime_hours < 1:
            return True
        svc = await self._get_service(persona_id)
        if svc is None:
            return False
        try:
            info = await asyncio.to_thread(svc.catch_up_from_downtime, float(downtime_hours))
            logger.info(f"[晨光心语] ⏱️ {persona_id} 补算 {downtime_hours:.1f}h: {info.get('mode')} → {info.get('activities')}")
            return True
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 停机补算失败: {e}")
            return False

    async def ensure(self, persona_id: str) -> bool:
        """只把状态机创建出来、不推进——启动时预热用，让日志连成一串。"""
        if not _HAS_AURA:
            return False
        return (await self._get_service(persona_id)) is not None

    async def tick(self, persona_id: str) -> bool:
        """推进一次状态机（world → energy → plan → activity → goal）。"""
        if not _HAS_AURA:
            return False
        svc = await self._get_service(persona_id)
        if svc is None:
            return False
        try:
            await asyncio.to_thread(self._tick_sync, svc)
            return True
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 状态机 tick 失败: {e}")
            return False

    @staticmethod
    def _tick_sync(svc):
        """★ 私有 API 的唯一封装点。`force_all_ticks()` 是同步阻塞调用，必须先 to_thread。"""
        svc._scheduler.force_all_ticks()

    # ---------- Step 1（睡眠硬接管）：睡着那几格，钉成「睡觉 @ 家」 ----------

    async def enforce_sleep_slot(self, persona_id: str) -> bool:
        """睡眠窗口里，把「当前这一格」钉成 `sleeping @ home`。

        为什么需要它：AI 人格走不到 aura-life 的自主睡眠分支，它的计划表里
        夜里那几格会被填成「放松 / 看雪 / 发呆」，地点还可能漂到咖啡馆去。

        分寸（别随手改）：
        - **只看钟点**（`in_sleep_window_now`，跟「睡着不点火」同一套口径，
          睡懒觉会顺延），绝不看活动名 —— 拿活动名当睡据会把白天的放松一起掐掉；
        - 只在窗口内动手，窗口外一律不碰；
        - **必须在 `tick()` 之后调用**：tick 里的 `_on_plan_tick` 会重排计划，
          先 tick 后钉，才盖得住。

        ⚠️ 已知的脆弱依赖（外部复核 P3-3，评估后选择**只加注释、不改逻辑**）：
        被钉的 `sleeping` 槽在 aura 侧 `energy_cost = -0.7`（≈ 设计值 0.09/h 的 8 倍）。
        当前心跳相位下它**永远不会被执行** —— 钉格发生在本小时的 activity tick 之后，
        而引擎只查「当前小时」那一格，所以这条大回血落不了地。
        但撑住它的是**时序，不是契约**：
          · 一旦 `tick_interval_minutes` 调到 30（一小时两跳），或
          · 夜里任何一次日程生成（generate → _prepare_life_state → tick）触发额外 tick，
        配合 upsert 把 `completed` 重置，这条 sleeping 槽就会真的跑 → 精力一次 +0.7。
        要修得钉格逻辑上做（如「本小时已钉过就不再钉」），那会改变 `/状态` 的展示，
        需单独一轮评估，本轮先留声明。
        """
        if not _HAS_AURA:
            return False
        try:
            if not self.in_sleep_window_now(persona_id):
                return False
        except Exception:
            return False
        svc = await self._get_service(persona_id)
        if svc is None:
            return False
        try:
            return await asyncio.to_thread(self._enforce_sleep_sync, svc)
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 睡眠接管失败: {e}")
            return False

    @staticmethod
    def _enforce_sleep_sync(svc) -> bool:
        """★ 私有 API 封装点之四：把「当前这一格」写成 sleeping @ home。

        `set_planned_activity` 会**跳过已经过去的小时**，所以这里取当前小时
        （心跳相位通常在 :38，当格还没过），刚好落在它的允许范围内。
        只动这一格 —— 不碰别的时段、不碰别的引擎。
        """
        planner = getattr(svc, "_daily_planner", None)
        world = getattr(svc, "_world", None)
        if planner is None:
            return False
        hour = datetime.now().hour
        try:
            planner.set_planned_activity(
                hour, "sleeping", location="home", reason="night sleep",
            )
        except Exception as e:
            logger.debug(f"[晨光心语] 睡眠接管: set_planned_activity 失败: {e}")
            return False
        if world is not None:
            try:
                world.move_to("home")
            except Exception:
                pass
        return True

    # ---------- 睡醒补算（AI 人格专属） ----------
    def _sleep_marker(self, persona_id: str) -> Path:
        """「今天补过睡醒了吗」的记号，跟人格的 life.db 放在同一层。"""
        return self._db_path(persona_id).parent / "sleep_reset.json"

    def _already_woke_today(self, persona_id: str, today: str) -> bool:
        try:
            p = self._sleep_marker(persona_id)
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8")).get("date") == today
        except Exception:
            pass
        return False

    def _mark_woke(self, persona_id: str, today: str) -> None:
        try:
            self._sleep_marker(persona_id).write_text(
                json.dumps({"date": today}, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.debug(f"[晨光心语] {persona_id} 睡醒记号写入失败（不影响运行）: {e}")

    async def morning_wake(self, persona_id: str) -> bool:
        """★ 私有 API 封装点之二：替 AI 人格补一次「睡醒」。

        aura-life 对 persona_type='ai' 会整段跳过睡眠分支（注释原话：AI personas
        never sleep），于是 energy.sleep() 永远轮不到执行 —— 而它是唯一会把
        「清醒时长」清零的函数。不清零，「醒得太久」的惩罚就一天天叠上去，把精力
        一路压住：爱早睡的人反而早上更累，就是这么来的。
        这里手动补一次 sleep()（清清醒时长 + 回血）。每天只在早晨那一段补一次，
        用一个人格层的小记号文件记「今天补过了没」，重载也不会重复刷。
        """
        if not _HAS_AURA:
            return False
        svc = await self._get_service(persona_id)
        if svc is None:
            return False
        now = self._local_now()
        # 用今天**生效**的起床时间——昨夜被吵过，她就该多睡会儿
        wake_h, wake_m = self.effective_wake_time(persona_id, now)
        # 只在「醒来后 5 小时内」补，别在半夜/深夜乱动她的精力
        elapsed = (now.hour * 60 + now.minute - (wake_h * 60 + wake_m)) % 1440
        if elapsed >= 5 * 60:
            return False
        today = now.date().isoformat()
        if self._already_woke_today(persona_id, today):
            return False  # 今天已经补过了
        try:
            keep = bool(self.plugin.get_config("life_engine.sleep_recover", True))
            ok = await asyncio.to_thread(self._morning_wake_sync, svc, keep)
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 睡醒补算失败: {e}")
            return False
        if ok:
            self._mark_woke(persona_id, today)
        return ok

    def _local_now(self) -> datetime:
        """按插件设置的时区取「现在」；取不到就退回本机时间。"""
        try:
            from .timezone_utils import get_now

            return get_now(self._timezone())
        except Exception:
            return datetime.now()

    def _bed_time(self) -> tuple[int, int]:
        """她的睡觉时间 (时, 分)。支持 "23:00" / "22:30"，也兼容纯数字 "23"。"""
        raw = str(self.plugin.get_config("life_engine.bedtime_time", "23:00") or "").strip()
        m = re.match(r"^(\d{1,2})\s*[:：.．]\s*(\d{1,2})$", raw)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        if raw.isdigit() and 0 <= int(raw) <= 23:
            return int(raw), 0
        return 23, 0

    def in_sleep_window_now(self, persona_id: str = "") -> bool:
        """她此刻在不在睡眠窗口里（按作息算，跨午夜也算）。

        带上 persona_id 时，用**今天生效的**起床时间——她睡懒觉那天，
        窗口也就跟着往后延，不会七点一到就"醒"了。
        """
        now = self._local_now()
        if persona_id:
            wake_h, wake_m = self.effective_wake_time(persona_id, now)
        else:
            wake_h, wake_m = self._wake_time()
        bed_h, bed_m = self._bed_time()
        wake_min, bed_min = wake_h * 60 + wake_m, bed_h * 60 + bed_m
        cur = now.hour * 60 + now.minute
        if bed_min == wake_min:
            return False
        if bed_min > wake_min:          # 跨午夜，比如 23:00 → 07:00
            return cur >= bed_min or cur < wake_min
        return bed_min <= cur < wake_min

    def _hour_in_sleep_window(self, persona_id: str, hour: int, now=None) -> bool:
        """判断某个**整点**在不在睡眠窗口里（跟 `in_sleep_window_now` 同一套口径）。"""
        now = now or self._local_now()
        if persona_id:
            wake_h, wake_m = self.effective_wake_time(persona_id, now)
        else:
            wake_h, wake_m = self._wake_time()
        bed_h, bed_m = self._bed_time()
        wake_min, bed_min = wake_h * 60 + wake_m, bed_h * 60 + bed_m
        cur = hour * 60
        if bed_min == wake_min:
            return False
        if bed_min > wake_min:          # 跨午夜，比如 23:00 → 07:00
            return cur >= bed_min or cur < wake_min
        return bed_min <= cur < wake_min

    def _wake_time(self) -> tuple[int, int]:
        """她的起床时间 (时, 分)。支持 "07:00" / "7:30"，也兼容纯数字 "7"。"""
        raw = str(self.plugin.get_config("life_engine.wake_time", "07:00") or "").strip()
        m = re.match(r"^(\d{1,2})\s*[:：.．]\s*(\d{1,2})$", raw)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return hour, minute
        if raw.isdigit() and 0 <= int(raw) <= 23:
            return int(raw), 0
        return 7, 0

    @staticmethod
    def _morning_wake_sync(svc, keep_energy: bool = False) -> bool:
        """补一次「睡醒」。

        keep_energy=True 时：只保留「清清醒时长」的效果，**不把精力重新刷满**
        ——因为夜里已经在窗口内慢慢回过了，再刷一次就等于白做。
        （只有在精力还明显偏低时才允许它兜底补上去）
        """
        _pid = getattr(svc, "_persona_id", "") or "?"
        energy = getattr(svc, "_energy", None)
        if energy is None:
            return False
        st = energy.state
        before = float(getattr(st, "level", 0.0) or 0.0)
        awake = float(getattr(st, "hours_awake", 0.0) or 0.0)
        energy.sleep()  # 清 hours_awake + 回血
        if keep_energy and before >= 0.5:
            st.level = before
        logger.info(
            f"[晨光心语] 🌅 {_pid} 补了一次「睡醒」：精力 {before:.2f} → {st.level:.2f}，"
            f"清醒时长 {awake:.1f}h → 0"
        )
        return True

    # ---------- P6：夜间被打断的记录（第二天要睡懒觉） ----------

    def note_user_activity(self, persona_id: str, when: datetime | None = None) -> None:
        """主人刚说了话。只写内存，非常轻——每条消息都会调。"""
        try:
            self._last_user_msg[persona_id] = when or self._local_now()
        except Exception:
            pass

    def last_user_at(self, persona_id: str):
        """主人最近一次开口的时间（内存里那份，够轻）。

        给「睡眠窗口里判断她刚被叫醒」用——跟睡眠回血用的是同一个信号源，
        两边口径一致（都认那 15 分钟的清醒尾巴）。
        """
        try:
            return self._last_user_msg.get(str(persona_id))
        except Exception:
            return None

    # ---------- 中文关键词桥 ----------
    # 底座那张 CONVERSATION_KEYWORD_MAP 整张是英文的，中文一句都命中不了。
    # 这里在插件侧补一层映射：中文命中 → 手动喂给对应引擎（不动底座，升级不丢）。

    _ZH_ENGINE_ATTRS = {
        "energy": "_energy",
        "affect": "_affect",
        "desire": "_desire_system",
        "cognitive": "_cognitive",
        "identity": "_identity",
        "drive": "_drive",
    }

    def _zh_extra_keywords(self) -> str:
        """主人自己加的中文信号词（内置表之外的「黑话」）。留空 = 只用内置表。"""
        try:
            return str(self.plugin.get_config("life_engine.zh_extra_keywords", "") or "")
        except Exception:
            return ""

    def zh_hit_stats(self, persona_id: str = "") -> list[tuple[str, int]]:
        """/晨光 状态 用：各中文信号命中过几次（内存计数，重载清零）。"""
        counts = getattr(self, "_zh_hit_counts", None)
        if not isinstance(counts, dict):
            return []
        agg: dict[str, int] = {}
        for (pid, item), n in counts.items():
            if persona_id and pid != persona_id:
                continue
            agg[str(item)] = agg.get(str(item), 0) + int(n or 0)
        return sorted(agg.items(), key=lambda kv: -kv[1])

    def zh_triggers_enabled(self) -> bool:
        try:
            return bool(self.plugin.get_config("life_engine.zh_triggers", True))
        except Exception:
            return True

    def _zh_cooldown_minutes(self) -> float:
        """同一个信号隔多久才认第二次。防"哈哈"一晚上把灵感刷爆。"""
        try:
            v = float(self.plugin.get_config("life_engine.zh_trigger_cooldown_minutes", 10) or 0)
        except (TypeError, ValueError):
            v = 10.0
        return max(0.0, v)

    @classmethod
    def _zh_engine(cls, svc, engine_name: str):
        """拿到某个引擎对象；拿不到就返回 None（降级，绝不抛）。"""
        attr = cls._ZH_ENGINE_ATTRS.get(str(engine_name or ""))
        return getattr(svc, attr, None) if attr else None

    async def apply_zh_triggers(self, persona_id: str, text: str) -> list[str]:
        """主人说了句中文 → 替底座把该触发的信号补上。

        只补「能量 / 情绪 / 亲密 / 认知 / 身份 / 驱动」这六类里的对话信号，
        和英文表逐条对齐；命中同一信号只算一次，跨消息还有冷却。
        失败一律降级 —— 这只是锦上添花，不能影响主流程。
        """
        if not _HAS_AURA or not str(text or "").strip():
            return []
        if not self.zh_triggers_enabled():
            return []
        try:
            from .zh_triggers import match_zh_triggers, parse_extra_keywords

            extra = parse_extra_keywords(self._zh_extra_keywords())
            hits = match_zh_triggers(text, extra)
        except Exception as e:
            logger.debug(f"[晨光心语] 中文关键词扫描跳过: {e}")
            return []
        if not hits:
            return []
        svc = await self._get_service(persona_id)
        if svc is None:
            return []

        now = self._local_now()
        cooldown = self._zh_cooldown_minutes()
        last = getattr(self, "_zh_last", None)
        if not isinstance(last, dict):
            last = self._zh_last = {}

        fired: list[str] = []
        for engine_name, trigger in hits:
            slot = (persona_id, engine_name, trigger)
            prev = last.get(slot)
            if prev is not None and cooldown > 0:
                try:
                    if (now - prev).total_seconds() < cooldown * 60:
                        continue
                except Exception:
                    pass
            engine = self._zh_engine(svc, engine_name)
            if engine is None:
                continue
            try:
                await asyncio.to_thread(engine.process_conversation_trigger, trigger)
                last[slot] = now
                fired.append(f"{engine_name}.{trigger}")
            except Exception as e:
                logger.debug(f"[晨光心语] 中文触发 {engine_name}.{trigger} 失败（忽略）: {e}")
        if fired:
            # E5：顺手记个命中计数，给 /晨光 状态 看（内存态，重载清零）
            counts = getattr(self, "_zh_hit_counts", None)
            if not isinstance(counts, dict):
                counts = self._zh_hit_counts = {}
            for item in fired:
                slot = (persona_id, item)
                counts[slot] = counts.get(slot, 0) + 1
            logger.info(f"[晨光心语] 🈶 {persona_id} 中文触发：{'、'.join(fired)}")
        return fired

    def _night_key(self, now: datetime) -> str:
        """这一夜算哪一天：以「就寝日」为准（凌晨的那半天算昨晚那一夜）。"""
        bed_h, _ = self._bed_time()
        day = now.date()
        if now.hour < bed_h:
            day = day - timedelta(days=1)
        return day.isoformat()

    def _watch_path(self, persona_id: str) -> Path:
        return self._db_path(persona_id).parent / "night_watch.json"

    def _load_watch(self, persona_id: str) -> dict:
        try:
            path = self._watch_path(persona_id)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save_watch(self, persona_id: str, data: dict) -> None:
        try:
            path = self._watch_path(persona_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            for old_key in sorted(data.keys())[:-3]:   # 只留最近三夜
                data.pop(old_key, None)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.debug(f"[晨光心语] {persona_id} 夜间记录落盘跳过: {e}")

    def _record_awake_span(self, persona_id: str, start: datetime, end: datetime) -> None:
        """把她「醒着」的一段并进今夜记录（重叠的自动合并，不会重复算）。"""
        if end <= start:
            return
        night = self._night_key(end)
        data = self._load_watch(persona_id)
        spans = list(data.get(night) or [])
        spans.append([start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")])

        merged: list[list[datetime]] = []
        try:
            parsed = []
            for sp in spans:
                parsed.append([datetime.fromisoformat(str(sp[0])), datetime.fromisoformat(str(sp[1]))])
            parsed.sort(key=lambda x: x[0])
            for a, b in parsed:
                if merged and a <= merged[-1][1]:
                    if b > merged[-1][1]:
                        merged[-1][1] = b
                else:
                    merged.append([a, b])
        except Exception:
            merged = []

        if merged:
            data[night] = [[a.isoformat(timespec="seconds"), b.isoformat(timespec="seconds")]
                           for a, b in merged]
        else:
            data[night] = spans
        self._save_watch(persona_id, data)

    def night_lag_minutes(self, persona_id: str, now: datetime | None = None) -> int:
        """昨夜被打断的总时长（分钟）——合并以后所有清醒区间求和。"""
        now = now or self._local_now()
        spans = self._load_watch(persona_id).get(self._night_key(now)) or []
        total = 0.0
        for sp in spans:
            try:
                a = datetime.fromisoformat(str(sp[0]))
                b = datetime.fromisoformat(str(sp[1]))
                total += max(0.0, (b - a).total_seconds())
            except Exception:
                continue
        return int(total / 60.0)

    def night_interrupt_count(self, persona_id: str, now: datetime | None = None) -> int:
        """昨夜被吵醒了几次（合并后的清醒区间数）—— 给 /状态 显示睡懒觉的因果用。"""
        now = now or self._local_now()
        spans = self._load_watch(persona_id).get(self._night_key(now)) or []
        return len(spans)

    def wake_lag_minutes(self, persona_id: str, now: datetime | None = None) -> int:
        """今天该睡懒觉多久：昨夜被打断多久就多睡多久，**封顶 2 小时**。"""
        return max(0, min(120, self.night_lag_minutes(persona_id, now)))

    def effective_wake_time(self, persona_id: str, now: datetime | None = None) -> tuple[int, int]:
        """今天**实际生效**的起床时间 = 作息里的起床时间 + 睡懒觉。

        这一处是「睡懒觉」的总出口：导火索的「刚醒」、早上补睡醒、
        刹车的安静时段，全都从这里取时间——所以她赖床的时候，整套都会跟着晚。
        """
        wake_h, wake_m = self._wake_time()
        lag = self.wake_lag_minutes(persona_id, now)
        total = wake_h * 60 + wake_m + lag
        return (total // 60) % 24, total % 60

    # ---------- P5：睡眠窗口内缓慢回血 ----------

    async def sleep_recover(self, persona_id: str) -> bool:
        """★ 她睡着的每一轮心跳，精力往上走一小口。

        一觉醒来是**慢慢暖回来**的，不是闹钟一响突然满格。
        这也是她在你半夜说话时，精力看起来合理的原因。

        aura-life 对 AI 人格整段跳过睡眠分支，所以这活儿只能我们自己干。

        注意：不能用 energy.sleep()——它是「直接把精力设成 0.3+时长×0.1」，
        每小时调一次只会把她按在同一个数字上，不是慢慢涨。所以这里直接推数值。
        """
        if not _HAS_AURA:
            return False
        if not self.in_sleep_window_now(persona_id):
            return False
        svc = await self._get_service(persona_id)
        if svc is None:
            return False
        try:
            return await asyncio.to_thread(self._sleep_recover_sync, svc, persona_id)
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 睡眠回血失败: {e}")
            return False

    # 多久没说话就算她"睡回去了"
    AWAKE_TAIL_MINUTES = 15

    def _sleep_recover_sync(self, svc, persona_id: str = "") -> bool:
        """她睡着的每一轮心跳，精力往上走一小口——**但被吵醒的那部分不算**。

        活儿很细，分三种情况：
        - 整轮没人打扰 → 正常回血，顺手清掉「清醒时长」（那是「醒太久会累」的源头）
        - 本轮里被叫醒过 → 只按**真正睡着的那段**折算回血，并且**不清清醒时长**
          ——让 aura-life 自己把「醒得太久」的账记上，第二天起点自然就低
        - 刚被叫醒（15 分钟内）→ 同上，还会把这段记进「今夜被打断」，明天拿来睡懒觉
        """
        energy = getattr(svc, "_energy", None)
        state = getattr(energy, "state", None) if energy is not None else None
        if state is None:
            return False

        try:
            per_hour = float(self.plugin.get_config("life_engine.sleep_recover_per_hour", 0.09) or 0.09)
        except (TypeError, ValueError):
            per_hour = 0.09
        try:
            tick_min = float(self.plugin.get_config("life_engine.tick_interval_minutes", 60) or 60)
        except (TypeError, ValueError):
            tick_min = 60.0
        # 心跳没那么快时也不会算过头（跟 _life_tick_loop 里 max(60, …) 对齐）
        tick_hours = max(60.0, tick_min * 60.0) / 3600.0

        now = self._local_now()
        window_start = now - timedelta(hours=tick_hours)

        # 本轮里她醒着多久：从最后一次被叫醒算起（再留 15 分钟清醒尾巴）
        awake_hours = 0.0
        last = self._last_user_msg.get(persona_id) if persona_id else None
        if last is not None:
            try:
                if last >= window_start:
                    awake_hours = (now - last).total_seconds() / 3600.0 + self.AWAKE_TAIL_MINUTES / 60.0
                    awake_hours = min(tick_hours, awake_hours)
            except Exception:
                awake_hours = 0.0
        sleep_hours = max(0.0, tick_hours - awake_hours)

        cap = 0.95
        before = float(getattr(state, "level", 0.0) or 0.0)
        gain = max(0.0, per_hour) * sleep_hours
        changed = False
        if gain > 0 and before < cap:
            state.level = min(cap, before + gain)
            changed = True

        if awake_hours > 0:
            # 被吵醒了：不清「清醒时长」，也不把 last_sleep_time 往后推
            if persona_id:
                try:
                    self._record_awake_span(persona_id, max(last, window_start), now)
                except Exception:
                    pass
            if changed:
                logger.info(
                    f"[晨光心语] 🌙 {persona_id} 睡眠被打断：本轮醒着 {awake_hours * 60:.0f} 分钟，"
                    f"只回 {gain:.3f}（精力 {before:.2f} → {state.level:.2f}）"
                )
        else:
            # 正常睡着：清账 + 回血
            if hasattr(state, "hours_awake"):
                state.hours_awake = 0.0
            if hasattr(state, "last_sleep_time"):
                try:
                    # V2 修复：引擎内部所有 datetime 都是 naive，而 now 是带时区的本地时间；
                    # 直接写进去会让下一刻的 (now - last_sleep_time) 抛「naive/aware 相减」，
                    # 每轮 energy tick 整段跳过。这里就地归一化成 naive。
                    state.last_sleep_time = now.replace(tzinfo=None)
                except Exception:
                    pass
            if changed:
                logger.info(
                    f"[晨光心语] 🌙 {persona_id} 睡眠回血：精力 {before:.2f} → {state.level:.2f}"
                )

        return changed or awake_hours > 0

    # ---------- Step 1：日程 → 状态机（回灌） ----------
    def _replay_marker(self, persona_id: str) -> Path:
        """「今天回灌过哪些安排」的记号，跟人格的 life.db 放同一层。"""
        return self._db_path(persona_id).parent / "schedule_replay.json"

    def _load_replay_done(self, persona_id: str, today: str) -> set:
        try:
            p = self._replay_marker(persona_id)
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8")) or {}
                if data.get("date") == today:
                    return set(data.get("done") or [])
        except Exception:
            pass
        return set()

    def _save_replay_done(self, persona_id: str, today: str, done: set) -> None:
        try:
            self._replay_marker(persona_id).write_text(
                json.dumps({"date": today, "done": sorted(done)}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug(f"[晨光心语] {persona_id} 回灌记号写入失败（不影响运行）: {e}")

    async def replay_schedule(self, persona_id: str, items: list[dict[str, Any]],
                              now_hour: int | None = None) -> list[str]:
        """日程 → 状态机：**接管**（Step 3，2026-10-06 改）。

        以前这里是「按一次按钮」——只让状态机做一次活动、**刻意不碰计划表**，
        所以 `/状态` 的「此刻在做」永远对不上日程。

        现在改成「铺格子」：到点的时段，按 `beats` 把活动 + 地点写进它那 3 个格子。
        每跳重铺（幂等覆盖），因为状态机每天换日会重排整张表、当天内还会 revise。
        开关仍是 `life_engine.schedule_replay`（关掉即回到旧行为）。
        """
        try:
            if not self.plugin.get_config("life_engine.schedule_replay", True):
                return []
        except Exception:
            pass
        return await self.align_schedule_slots(persona_id, items, now_hour)

    async def align_schedule_slots(self, persona_id: str, items: list[dict[str, Any]],
                                   now_hour: int | None = None) -> list[str]:
        """按日程把「已经到点」的时段铺进状态机的计划表。

        - 一段 3 格：清晨=6/7/8、上午=9/10/11、午间=12/13/14、
          下午=15/16/17、傍晚=18/19/20、晚间=21/22/23
        - `beats` 分摊：1 个 → 铺满 3 格；2 个 → 2+1；3 个 → 一格一个
        - 只铺「当前小时及以后」的格（更早的由 `set_planned_activity` 自己跳过）
        - 词表外的地点 → 顺手注册进名册（自动注册）
        - 活动认不出的节拍 → 这一格整个不写（活动、地点都不写，
          只把地点名注册进名册）；状态机自己原来的安排原样保留
        """
        if not _HAS_AURA or not items:
            return []
        now = self._local_now()
        if now_hour is None:
            now_hour = now.hour
        svc = await self._get_service(persona_id)
        if svc is None:
            return []
        # 睡眠窗口里的小时让给「睡眠硬接管」（enforce_sleep_slot），免得两边互相盖
        try:
            sleep_hours = {h for h in range(24) if self._hour_in_sleep_window(persona_id, h, now)}
        except Exception:
            sleep_hours = set()
        try:
            return await asyncio.to_thread(self._align_sync, svc, items, now_hour, sleep_hours)
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 日程接管失败: {e}")
            return []

    @staticmethod
    def _align_sync(svc, items: list[dict[str, Any]], now_hour: int,
                    sleep_hours: set | None = None) -> list[str]:
        """★ 私有 API 封装点之五：把日程铺进 planner 的格子（同步，调用方负责 to_thread）。

        只铺「还没过完」的时段（当前段 + 未来的段），**睡眠窗口里的小时让路**
        —— 那几格归「睡眠硬接管」（`enforce_sleep_slot`）管，别互相盖。
        """
        planner = getattr(svc, "_daily_planner", None)
        if planner is None:
            return []
        skip = sleep_hours or set()
        # 跟 set_planned_activity 内部**同一把尺**：它用 datetime.now().hour 判断
        # 「这个小时过了没」。这里先自己筛一遍，免得它悄悄跳过了、我们却记了假账。
        real_hour = datetime.now().hour
        touched: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            slot_label = str(item.get("time") or "").strip()
            start = _PERIOD_HOUR.get(slot_label)
            if start is None:
                continue
            if now_hour >= start + 3:
                continue  # 这个时段已经整体过去了，别白费调用（也免得刷日志）
            loc = str(item.get("location") or "").strip() or "home"
            beats = item.get("beats")
            if not isinstance(beats, list) or not beats:
                beats = [{"do": "", "at": loc}]
            spread = LifeBridgeManager._spread_beats(beats, 3)
            for offset, entry in enumerate(spread):
                hour = start + offset
                if hour > 23 or hour in skip or hour < real_hour:
                    continue  # 过了的格不碰（也别记假账）
                act = str(entry.get("do") or "").strip()
                at = str(entry.get("at") or "").strip() or loc
                LifeBridgeManager._ensure_location(svc, at)
                if not act:
                    continue
                try:
                    planner.set_planned_activity(
                        hour, act, location=at, reason="aligned with schedule",
                    )
                    touched.append(f"{hour:02d}:{act}@{at}")
                except Exception as e:
                    logger.debug(f"[晨光心语] 接管 {hour:02d} 点失败: {e}")
        return touched

    @staticmethod
    def _spread_beats(beats: list, n: int = 3) -> list[dict]:
        """把 1~3 个节拍摊到 n 个格子里（1 个→全铺；2 个→2+1；3 个→一格一个）。"""
        bs = [b for b in beats if isinstance(b, dict)][:3]
        if not bs:
            return [{} for _ in range(n)]
        if len(bs) == 1:
            return [bs[0]] * n
        if len(bs) == 2:
            return [bs[0], bs[0], bs[1]][:n]
        return bs[:n]

    @staticmethod
    def _ensure_location(svc, key: str) -> None:
        """词表外的地点 → 顺手注册进名册（「自动注册」，见施工方案）。"""
        k = str(key or "").strip()
        if not k:
            return
        reg = getattr(svc, "_location_registry", None)
        if reg is None:
            return
        if k.lower().replace(" ", "_") in reg:
            return
        fn = getattr(svc, "_register_user_location", None)
        if callable(fn):
            try:
                fn(k)
            except Exception:
                pass

    @staticmethod
    def _perform_activity_sync(svc, activity_name: str) -> bool:
        """★ 私有 API 封装点之三：让状态机「真的做一次」指定活动。

        与 `_on_activity_tick()` 那条主路径保持同一套副作用：执行活动 → 加减精力 →
        情绪与压力 → 记进 activity_logs。刻意**不碰位置和每日计划表**——回灌只负责
        「这一下发生过」，不抢状态机自己的日程安排。
        """
        _pid = getattr(svc, "_persona_id", "") or "?"
        engine = getattr(svc, "_activity_engine", None)
        energy = getattr(svc, "_energy", None)
        world = getattr(svc, "_world", None)
        if engine is None or energy is None or world is None:
            return False
        activity = engine.get_activity(activity_name)
        if activity is None:
            logger.debug(f"[晨光心语] {_pid} 回灌：状态机里没有活动「{activity_name}」，跳过")
            return False
        # 精力不够就不硬做（和它自己 tick 的规矩保持一致）
        if energy.effective_level < getattr(activity, "min_energy", 0.0):
            logger.info(f"[晨光心语] {_pid} 回灌：精力不足，跳过「{activity_name}」")
            return False
        affect = getattr(svc, "_affect", None)
        # 位置对齐：活动有「适合的地点」而她现在不在这儿，就先挪过去
        # （跟 `_on_activity_tick()` 一样，只挪不定行程；没有适合地点就不管）
        suitable = list(getattr(activity, "suitable_locations", None) or [])
        try:
            if suitable and world.current_location not in suitable:
                world.move_to(suitable[0])
        except Exception:
            pass
        try:
            log = engine.execute_activity(
                activity=activity,
                location=world.current_location,
                weather=world.weather,
                energy_before=energy.effective_level,
            )
            if activity.energy_cost < 0:
                energy.restore_energy(abs(activity.energy_cost))
            else:
                energy.consume_energy(activity.energy_cost)
            if log.emotions_triggered and affect is not None:
                for emotion, intensity in log.emotions_triggered.items():
                    try:
                        affect.on_emotion_event(emotion, intensity)
                    except Exception:
                        pass
            if affect is not None:
                try:
                    affect.on_activity(activity.name)
                except Exception:
                    pass
            svc._record_activity(log)
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {_pid} 执行回灌活动「{activity_name}」失败: {e}")
            return False
        logger.info(
            f"[晨光心语] 🔁 {_pid} 回灌活动 {activity_name}："
            f"精力 {log.energy_before:.2f} → {energy.effective_level:.2f}"
        )
        return True

    async def get_state(self, persona_id: str, force: bool = False) -> dict[str, Any] | None:
        """取一份精简的内在状态切片（给 {life_state} 用）。

        ★ 加一层 40 秒的短 TTL 缓存：这个函数被 @filter.on_llm_request **每条消息**
        都调一次，每次都要跨线程读一遍全量 status；但状态机是**每小时**才 tick
        一次，一分钟内状态不可能真的变 —— 所以短缓存功能上等于没有，
        却能把每条对话的 1~3 次线程跳省掉。
        `/状态` 这种「我现在就要看」的地方传 force=True 绕过缓存。
        """
        if not _HAS_AURA:
            return None
        if not force:
            hit = self._state_cache.get(persona_id)
            if hit is not None and (time.monotonic() - hit[0]) < self._STATE_TTL:
                return hit[1]
        svc = await self._get_service(persona_id)
        if svc is None:
            return None
        try:
            st = await asyncio.to_thread(svc.get_status)
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 读取状态失败: {e}")
            return None

        _energy_raw = st.get("energy") or {}
        energy = _energy_raw.get("level", 0.6)
        # C 方案：界面要「基础值 + 实际值」两个都印。基础值是 level；实际值是
        # 含咖啡因/灵感/社交三种加成的 effective_level —— 精力档标签本来就按它换算，
        # 所以只印基础值会跟标签打架（0.2 却报 resting 就是这么来的）。
        energy_eff = _energy_raw.get("effective_level")
        if energy_eff is None:
            energy_eff = energy
        affect = st.get("affect") or {}
        cognitive = st.get("cognitive") or {}
        world = st.get("world") or {}
        chaos = st.get("chaos") or {}
        last_chaos = chaos.get("last_event") or {}
        goals = [g.get("title") for g in (st.get("goals") or {}).get("active_goals") or [] if g.get("title")]
        out = {
            "energy": round(float(energy or 0.0), 2),
            "energy_effective": round(float(energy_eff or 0.0), 2),
            "energy_label": _energy_raw.get("energy_level", ""),
            "mood": affect.get("mood", "平静"),
            "mood_intensity": round(float(affect.get("mood_intensity") or 0.0), 2),
            "stress": round(float(affect.get("stress") or 0.0), 2),
            "current_activity": st.get("current_activity") or "",
            "location": st.get("location") or "",
            "goals": goals[:3],
            "thought": cognitive.get("current_thought", ""),
            # R8 事实校验：这些是状态机已经定下的事实，日程不许和它们打架
            "weather": world.get("weather", ""),
            "season": world.get("season", ""),
            "location_desc": world.get("location_description", ""),
            # R6 白天改主意：今天有没有冒出计划外的事
            "chaos_today": int(chaos.get("events_today") or 0),
            "chaos_last": (last_chaos.get("text") or "").strip(),
        }
        self._state_cache[persona_id] = (time.monotonic(), out)
        return out

    async def get_plan(self, persona_id: str) -> dict[str, str] | None:
        """把状态机的今日计划映射到我们自己的 6 个时段，得到一份粗糙骨架。"""
        if not _HAS_AURA:
            return None
        svc = await self._get_service(persona_id)
        if svc is None:
            return None
        try:
            st = await asyncio.to_thread(svc.get_status)
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 读取计划失败: {e}")
            return None
        slots = _parse_plan((st.get("planner") or {}).get("schedule", ""))
        if not slots:
            return None
        hours = sorted(slots.keys())
        out: dict[str, str] = {}
        for label, target in PERIOD_SLOTS:
            # 取不晚于目标时段、且离它最近的那一条
            cand = [h for h in hours if h <= target]
            hour = cand[-1] if cand else hours[0]
            out[label] = slots[hour]
        return out

    async def shareable(self, persona_id: str, limit: int = 3) -> list[dict[str, str]]:
        """主动分享钩子（R4）：她今天有没有想跟你说的事。"""
        if not _HAS_AURA:
            return []
        svc = await self._get_service(persona_id)
        if svc is None:
            return []
        try:
            items = await asyncio.to_thread(svc.get_shareable_experiences, limit)
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 读取分享项失败: {e}")
            return []
        out = []
        for it in items or []:
            content = getattr(it, "content", "")
            if content:
                out.append({"id": getattr(it, "id", None), "content": content})
        return out

    async def mark_shared(self, persona_id: str, experience_id) -> bool:
        if not _HAS_AURA or experience_id is None:
            return False
        svc = await self._get_service(persona_id)
        if svc is None:
            return False
        try:
            await asyncio.to_thread(svc.mark_experience_shared, experience_id)
            return True
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 标记分享失败: {e}")
            return False

    async def on_user_message(self, persona_id: str, text: str,
                              emotion: str = "", intensity: float = 0.0) -> None:
        """把对话喂给状态机——它不存原话，只累积「关系的厚度」。"""
        if not _HAS_AURA or not text:
            return
        svc = await self._get_service(persona_id)
        if svc is None:
            return
        try:
            await asyncio.to_thread(svc.on_user_message, text, emotion, float(intensity or 0.0))
        except Exception as e:
            logger.debug(f"[晨光心语] ⚠️ {persona_id} 喂状态机失败（忽略）: {e}")

    async def last_state_update(self, persona_id: str) -> datetime | None:
        """上一次状态更新时间，用来算停机时长。"""
        if not _HAS_AURA:
            return None
        svc = await self._get_service(persona_id)
        if svc is None:
            return None
        try:
            return await asyncio.to_thread(svc.get_last_state_update)
        except Exception:
            return None

    async def reset(self, persona_id: str) -> bool:
        """重置该人格的状态（/状态重置 用）。"""
        if not _HAS_AURA:
            return False
        svc = await self._get_service(persona_id)
        if svc is None:
            return False
        try:
            await asyncio.to_thread(svc.reset_state)
            return True
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ 重置 {persona_id} 状态失败: {e}")
            return False

    async def close_all(self):
        """插件卸载时释放。"""
        for pid, svc in list(self._services.items()):
            try:
                await asyncio.to_thread(svc.stop)
            except Exception:
                pass
        self._services.clear()

    # ─────────────────────── 文本渲染 ───────────────────────

    @staticmethod
    def render_state(state: dict[str, Any] | None, max_len: int = 800) -> str:
        """把状态切片渲染成给 LLM 看的一段话（限长，别把 prompt 撑爆）。"""
        if not state:
            return ""
        lines = [
            f"精力 {state.get('energy', 0.6)}"
            f"（实际 {state.get('energy_effective', state.get('energy', 0.6))}"
            f" · {energy_zh(state.get('energy_label') or '正常')}）"
            f" / 心情 {mood_zh(state.get('mood') or '平静')}（强度 {state.get('mood_intensity', 0)}）"
        ]
        if state.get("current_activity"):
            lines.append(f"此刻正在做：{activity_zh(state['current_activity'])}")
        if state.get("location"):
            lines.append(f"此刻所在：{location_zh(state['location'])}")
        if state.get("weather"):
            season = state.get("season") or ""
            lines.append(f"外面的天：{state['weather']}{'，' + season if season else ''}")
        if state.get("chaos_today"):
            # R6：允许她今天「改主意」——计划外的插曲照样是生活
            lines.append(
                f"今天冒出过 {state['chaos_today']} 件计划外的事"
                + (f"（{state['chaos_last']}）" if state.get("chaos_last") else "")
            )
        if state.get("goals"):
            lines.append("今天惦记的目标：" + "；".join(state["goals"]))
        if state.get("stress") and float(state["stress"]) >= 0.3:
            lines.append(f"压力偏高（{state['stress']}），需要给自己留点余地")
        text = "\n".join(lines)
        return text[:max_len]

    @staticmethod
    def render_plan(plan: dict[str, str] | None) -> str:
        if not plan:
            return ""
        return "\n".join(f"- {label}：{plan.get(label, '')}" for label, _ in PERIOD_SLOTS if plan.get(label))
