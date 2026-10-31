import json
import random
from datetime import date, datetime, timedelta
from typing import Any

from astrbot.api import logger

from .state_store import StateStore
from .local_geo import build_text as build_local_geo_text, ensure_block as ensure_local_geo_block
from .storage import ScheduleStorage
from .timezone_utils import get_today

try:
    from lunar_python import Lunar
    _HAS_LUNAR = True
except ImportError:
    _HAS_LUNAR = False


# ── H1（v1.5.29）：activity 不许在开头自报时段 ────────────────────────────
# 现象：渲染时是「时段 · 正文」，而生成器写 activity 时开头又报了时段，
# 于是念出「晚间 · 晚间烟霾16°C，天黑了…」。
# 治本：提示词里把这条写明。但**模板是用户自己的东西**（配置项 prompt_template
# 可能被改过、也可能是旧版本留下来的），所以不能靠改模板文本去覆盖它 ——
# 这里做一层兜底：模板里没有这条约束，就在「开始生成」之前补一段，
# 有就一个字都不加。默认模板和配置模板都走这条路。
_ACTIVITY_RULE_MARK = "开头别再报一遍时段"
_ACTIVITY_RULE_BLOCK = (
    "\n## 格式补充（这一格正文怎么写）\n"
    "- 开头别再报一遍时段（「晚间 · 」「上午，」这种都算）——外面已经写着是哪个时段了。\n"
    "- 天气可以写，但别把「时段＋天气」捆成一句引子；直接从这一格发生的事说起。\n"
)

class ContextBuilder:

    # ── 公历节日（仅非法定节假日） ──
    SOLAR_FESTIVALS = {
        (2, 14): "情人节 💕",
        (3, 8): "妇女节 🌸",
        (4, 1): "愚人节 🤪",
        (5, 20): "520网络情人节 💖",
        (6, 1): "儿童节 🧒",
        (10, 31): "万圣节 🎃",
        (12, 24): "平安夜 🎄",
        (12, 25): "圣诞节 🎄",
    }

    # ── lunar-python 未覆盖的农历节日 ──
    LUNAR_EXTRA = {
        (7, 7): "七夕节 🥰",
        (7, 15): "中元节 🕯️",
    }

    # ── 已在法定节假日中，第三层需跳过的农历节日 ──
    LEGAL_LUNAR = {"春节", "清明节", "端午节", "中秋节", "除夕", "农历除夕"}

    def __init__(self, plugin, storage: ScheduleStorage, holidays_module,
                 state_store: StateStore = None, life_bridge=None,
                 event_ledger=None, memory_bridge=None):
        self.plugin = plugin
        self.storage = storage
        self.holidays = holidays_module
        # 状态存储（Phase 0）：给 prompt 提供 {yesterday_state} / {open_threads}
        self.state_store = state_store or StateStore(storage)
        # 内在状态机桥（Phase 1）：给 prompt 提供 {life_state} / {life_plan}
        self.life_bridge = life_bridge
        # 生活账本（Phase 2）：补全「还没了结的事」
        self.event_ledger = event_ledger
        # 记忆搭桥（Phase 2 · C 模式）：自建池 + 主动检索
        self.memory_bridge = memory_bridge
        # F8：法定节假日日历按年缓存（`holidays.China()` 每次都会重展开一整年）
        self._cn_cache: dict[int, Any] = {}
        # H1：兜底补「别自报时段」这条只说明一次，别每次生成都刷一行日志
        self._activity_rule_noted = False
        # J1：本地环境参考补进模板的说明也只打一次
        self._local_geo_noted = False

    def _ensure_activity_rule(self, prompt: str) -> str:
        """模板里没有「activity 开头别报时段」这条时，替它补上（H1 · v1.5.29）。

        为什么不直接改模板：`prompt_template` 是配置项，可能早就被主人改过、
        也可能还是旧版本留下来的文本，插件没有权利覆盖它。所以做法是——
        **有就不动，没有就在「开始生成」之前插一小段**；插的位置尽量靠前，
        别落在「请按照以上约束输出 JSON」这句之后。

        补过之后打一次日志（只一次），排查时能看出这句是插件加的、不是模板里的。
        """
        text = prompt or ""
        if not text or _ACTIVITY_RULE_MARK in text:
            return text
        if "## 开始生成" in text:
            text = text.replace("## 开始生成", _ACTIVITY_RULE_BLOCK.strip() + "\n\n## 开始生成", 1)
        else:
            text = text.rstrip() + "\n" + _ACTIVITY_RULE_BLOCK
        if not self._activity_rule_noted:
            self._activity_rule_noted = True
            logger.info(
                "[晨光心语] 📝 当前提示词模板里没有「activity 开头别报时段」这条，"
                "已在生成时自动补一段（不改模板本身）"
            )
        return text

    def _get_today(self) -> date:
        """获取当前日期（使用配置的时区）"""
        tz_name = self.plugin.get_config("schedule.timezone", "Asia/Shanghai")
        return get_today(tz_name)

    # ── 三层节日检测 ───────────────────────────────────────
    def _cn_calendar(self, year: int):
        """F8：按年缓存一份 holidays 日历。

        `self.holidays.China()` 每次新建实例，第一次查询时会为那一年**重新展开**
        整份日历；而本函数被日程生成 / /晨光报告 / 每小时的纪念日探测反复调用。
        缓存后一天里只展开一次，**查询结果完全一致**（today 就落在 year 里）。
        """
        cal = self._cn_cache.get(year)
        if cal is not None:
            return cal
        try:
            cal = self.holidays.China(years=[year])
        except Exception:
            cal = self.holidays.China()      # 万一注入了老版本，退回原写法
        self._cn_cache[year] = cal
        if len(self._cn_cache) > 3:          # 只留最近几年
            self._cn_cache.pop(min(self._cn_cache))
        return cal

    def _get_holiday_info(self, today: date) -> str:
        """三层节日检测：法定节假日 → 公历节日 → 农历节日

        ⚠️ **契约：只返回「节日名」，没有节日一律返回空串。**
        任何「今天为工作日 / 休息日」这类非节日文案都不许从这里出去 ——
        主动消息那边（`ProactiveEngine._auto_holiday_name`）会把这个返回值
        当成「节日名」用，混进非节日文案就会被误判成「纪念日」（N1 那次就是这么栽的）。
        要「工作日 / 休息日」，请用 `_get_day_kind()`，由日程模板自己拼。
        """
        parts = []

        # 第一层：法定节假日（holidays）
        cn = self._cn_calendar(today.year)
        if today in cn:
            parts.append(f"法定节假日：{cn.get(today)}")

        # 第二层：公历节日（固定 + 浮动）
        solar = self._get_solar_festival(today)
        if solar:
            parts.append(solar)

        # 第三层：农历节日（lunar-python）
        lunar = self._get_lunar_festival(today)
        if lunar:
            parts.append(lunar)

        if parts:
            return "，".join(parts)
        # N1 修复：没有节日就返回空串 —— 只报节日，不报「今天是工作日/休息日」。
        # 那句话改由下面的 `_get_day_kind()` 提供、日程那边自己拼；本函数还有一个
        # 「消费者」是主动消息的节日探测，见上面的契约说明。
        return ""

    @staticmethod
    def _get_day_kind(today: date) -> str:
        """今天算工作日还是休息日 —— **只给日程模板用**，别拿去当「节日名」。

        R6：没节日时也要区分周末，不然周六周日会被写成「上班日」。
        """
        return "今天为休息日（周末）" if today.weekday() >= 5 else "今天为工作日"

    # ── 公历节日 ──────────────────────────────────────────
    @staticmethod
    def _get_nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
        """获取某月第 n 个星期 weekday（0=周一, 6=周日）"""
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))

    def _get_solar_festival(self, today: date) -> str:
        """检查公历节日（固定日期 + 浮动日期）"""
        # 固定日期
        key = (today.month, today.day)
        if key in self.SOLAR_FESTIVALS:
            return self.SOLAR_FESTIVALS[key]
        # 浮动日期
        y = today.year
        if today == self._get_nth_weekday(y, 5, 6, 2):   # 5月第2个周日
            return "母亲节 🌸"
        if today == self._get_nth_weekday(y, 6, 6, 3):   # 6月第3个周日
            return "父亲节 👔"
        if today == self._get_nth_weekday(y, 11, 3, 4):  # 11月第4个周四
            return "感恩节 🦃"
        return ""

    # ── 农历节日 ──────────────────────────────────────────
    def _get_lunar_festival(self, today: date) -> str:
        """用 lunar-python 查农历节日，跳过已在法定节假日中的"""
        if not _HAS_LUNAR:
            return ""
        try:
            lunar = Lunar.fromDate(datetime.combine(today, datetime.min.time()))
            month, day = lunar.getMonth(), lunar.getDay()

            results = []
            # lunar-python 自带节日（已跳过法定节日的覆盖）
            for f in lunar.getFestivals():
                if f not in self.LEGAL_LUNAR:
                    results.append(f)

            # 补充 lunar-python 未覆盖的
            extra = self.LUNAR_EXTRA.get((month, day))
            if extra:
                raw = extra.split(" ")[0]
                if raw not in results:
                    results.append(extra)

            return "，".join(results) if results else ""
        except Exception as e:
            logger.error(f"[晨光心语] 获取农历节日异常: {e}")
            return ""

    def _random_pick(self, pool, fallback: str) -> str:
        if isinstance(pool, list):
            items = [item.strip() for item in pool if isinstance(item, str) and item.strip()]
            return random.choice(items) if items else fallback
        if isinstance(pool, str) and pool.strip():
            items = [item.strip() for item in pool.split(",") if item.strip()]
            return random.choice(items) if items else fallback
        return fallback

    def get_known_persona_ids(self) -> list[str]:
        ids = []
        for i in range(1, 4):
            name = str(self.plugin.get_config(f"persona_styles.persona_name_{i}", "") or "").strip()
            if name:
                ids.append(name)
        if hasattr(self.plugin, "dynamic_styles"):
            for name in self.plugin.dynamic_styles.keys():
                if name.strip() and name.strip() not in ids:
                    ids.append(name.strip())
        if not ids:
            ids.append("default")
        return ids

    async def _get_persona_prompt(self, persona_id: str) -> str:
        """
        获取指定人格的完整提示词。
        先按 AstrBot 的解析方式查（get_persona_v3_by_id 按人格名匹配，也认识配置里的人格），
        再退回数据库按 ID 精确查找。找不到就当作“该人格没有提示词”，返回空字符串——
        只记一条 info，不再把正常的“查不到”当成异常抛出来。
        """
        persona_mgr = getattr(self.plugin.context, "persona_manager", None)
        if not persona_mgr or not persona_id:
            return ""

        # ① 按 AstrBot 的规则解析；内置的 "default" 只是占位人格，跳过。
        v3 = None
        resolver = getattr(persona_mgr, "get_persona_v3_by_id", None)
        if resolver:
            try:
                v3 = resolver(persona_id)
            except Exception as e:
                logger.warning(f"[晨光心语] 解析人格 {persona_id} 失败: {e}")

        if isinstance(v3, dict) and v3.get("name") != "default":
            prompt = (v3.get("prompt") or "").strip()
            if prompt:
                logger.info(f"[晨光心语] ✅ 已加载 {persona_id} 的人格提示词 (长度: {len(prompt)})")
                return prompt
            dialogs = v3.get("begin_dialogs") or []
            text = "\n".join([str(d) for d in dialogs if d]) if isinstance(dialogs, list) else str(dialogs)
            if text.strip():
                logger.info(f"[晨光心语] ✅ 从 begin_dialogs 加载 {persona_id} 的人格提示词 (长度: {len(text)})")
                return text
            return ""

        # ② 数据库精确查找（v4 的 Persona 对象）
        try:
            persona = await persona_mgr.get_persona(persona_id)
        except ValueError:
            logger.info(f"[晨光心语] 人格 {persona_id} 不存在，本次不使用人格提示词")
            return ""
        except Exception as e:
            logger.warning(f"[晨光心语] {persona_id} 获取人格提示词异常: {e}")
            return ""

        if not persona:
            return ""
        try:
            # 按优先级尝试多种字段
            if hasattr(persona, "prompt") and persona.prompt:
                prompt = persona.prompt
                logger.info(f"[晨光心语] ✅ 已加载 {persona_id} 的人格提示词 (长度: {len(prompt)})")
                return prompt
            if hasattr(persona, "begin_dialogs") and persona.begin_dialogs:
                dialogs = persona.begin_dialogs
                prompt = "\n".join([str(d) for d in dialogs if d]) if isinstance(dialogs, list) else str(dialogs)
                if prompt.strip():
                    logger.info(f"[晨光心语] ✅ 从 begin_dialogs 加载 {persona_id} 的人格提示词 (长度: {len(prompt)})")
                    return prompt
            if hasattr(persona, "system_prompt") and persona.system_prompt:
                prompt = persona.system_prompt
                logger.info(f"[晨光心语] ✅ 从 system_prompt 加载 {persona_id} 的人格提示词 (长度: {len(prompt)})")
                return prompt
            for attr in ["description", "content", "text"]:
                val = getattr(persona, attr, None)
                if val and isinstance(val, str) and len(val.strip()) > 10:
                    logger.info(f"[晨光心语] ✅ 从 {attr} 加载 {persona_id} 的人格提示词 (长度: {len(val)})")
                    return val
            if hasattr(persona, "dict"):
                try:
                    d = persona.dict()
                    for key in ["prompt", "begin_dialogs", "system_prompt", "description", "content"]:
                        if key in d and d[key]:
                            val = d[key]
                            if isinstance(val, list):
                                val = "\n".join([str(v) for v in val if v])
                            if isinstance(val, str) and len(val.strip()) > 10:
                                logger.info(f"[晨光心语] ✅ 从 dict.{key} 加载 {persona_id} 的人格提示词 (长度: {len(val)})")
                                return val
                except Exception:
                    pass
            return ""
        except Exception as e:
            logger.error(f"[晨光心语] {persona_id} 获取人格提示词异常: {e}")
            return ""

    async def build_context(self, persona_id: str,
                            recent_messages: list[str] = None) -> dict[str, Any]:
        today = self._get_today()
        style = self.plugin.get_style_for_persona(persona_id)

        ref_days = self.plugin.get_config("context.reference_history_days", 3)
        history = await self.storage.get_history_schedules(persona_id, ref_days, today=today)
        history_text = ""
        for h in history:
            # 2026-10-16：历史日程原来把整份 JSON 塞进提示词，里面带着 events / resolved_events。
            # 其中 `resolved_events` 是「了结旧事」的老字段 —— 她天天照着这五份"标准答案"学样，
            # 新加的 `resolved_ids` 反倒没人认（实测 10-16 生成：字段压根没出现，销账仍为 0）。
            # 这里把这两个销账字段剥掉，别让历史带偏她；其余（intent/穿搭/日程/note/state）照旧。
            hist = h["schedule"] if isinstance(h["schedule"], dict) else {}
            slim = {k: v for k, v in hist.items() if k not in ("events", "resolved_events")}
            history_text += f"{h['date']}: {json.dumps(slim, ensure_ascii=False)}\n"

        ref_count = self.plugin.get_config("context.reference_recent_count", 5)
        try:
            ref_count = int(ref_count)
        except (TypeError, ValueError):
            ref_count = 5
        recent_text = ""
        # A4 修复：填 0（或负）= 不参考 —— 原来 recent_messages[-0:] 等于全量，行为与说明相反。
        if recent_messages and ref_count > 0:
            recent_text = "\n".join(recent_messages[-ref_count:])

        # 记忆来源（主路=外部记忆库 / 兜底=本地池）：捞出跟当前话题相关的事
        # 判据交给门面自己 ready() —— 外部库开着就用外部库，跟池子开关无关。
        if self.memory_bridge is not None and (
            self.memory_bridge.ready()
            if hasattr(self.memory_bridge, "ready")
            else self.plugin.get_config("bridge.enabled", False)
        ):
            try:
                query = recent_messages[-1] if recent_messages else ""
                hits = await self.memory_bridge.retrieve(
                    persona_id, query,
                    limit=self.plugin.get_config("bridge.max_events", 3),
                )
                if hits:
                    # N5：不再二次截断 —— 池子里存多少（≤400）就给她看多少。
                    # 原来取 [:120]，120 字基本只够装「记忆 #N / Importance / Topics / Participants」
                    # 那行标题栏，真正的 Key facts 一个字都进不来。
                    extra = "\n".join(f"- {h.get('text', '')}" for h in hits)
                    note = "\n（她还记着这些事，可以顺其自然地提及，不必硬用）\n"
                    recent_text = (recent_text + note + extra) if recent_text else extra
            except Exception as e:
                logger.warning(f"[晨光心语] {persona_id} 记忆池检索失败（已降级）: {e}")

        # ── 昨日状态 → 今日因果（Phase 0） ──
        yesterday_state = await self.state_store.get_yesterday_state(persona_id, today)
        seed_state = StateStore.seed_from_yesterday(yesterday_state, today)
        yesterday_state_text = StateStore.summarize(yesterday_state)
        max_threads = self.plugin.get_config("state.max_open_threads", 3)
        open_threads_text = StateStore.threads_text(seed_state, limit=max_threads)

        # 生活账本（Phase 2）：把账本里还没了结的事也接上
        if self.event_ledger is not None and self.plugin.get_config("event_ledger.enabled", False):
            try:
                # 2026-10-15：闸门放宽 + 清单带编号、按年龄分堆。
                # 病根：老代码里候选一旦超过 8 条就只挑 retrieve_limit（默认 3）条给她，
                # 候选池 cap=30 又按日期倒序取 —— 最老那批（恰恰是「早该结的」）既进不了
                # 候选池、更进不了清单。实测建账 15 天 28 条 open，closed_at 一条都没有。
                # 现在：候选不超过 full_list_threshold 就整包给她看，超过才退回关键词挑；
                # 每条前面挂账本编号，她就能直接点名销账（见 resolved_ids），
                # 不必再让措辞跟原文一字不差。
                # 下限钳到 50：这格设小了就等于白改 —— 实测设 20 时账本涨到 21 条就立刻越线，
                # 清单退回"只挑几条"，老账一条都看不见。宁可多带点，也别让该结的账藏起来。
                full_threshold = max(50, int(self.plugin.get_config("event_ledger.full_list_threshold", 50) or 50))
                limit = int(self.plugin.get_config("event_ledger.retrieve_limit", 8) or 8)
                cap = int(self.plugin.get_config("event_ledger.candidate_cap", 50) or 50)
                cand = await self.event_ledger.candidates(persona_id, days=30, cap=cap)
                if len(cand) <= full_threshold:
                    ledger_events = cand
                else:
                    ledger_events = await self.event_ledger.retrieve(
                        persona_id, seed_state.get("focus", ""), limit=limit, today=today
                    )
                    # 2026-10-17：retrieve 带 7 天半衰期，挑出来清一色是新的 ——
                    # 实测 24 条 open 时清单里一条老账都没有，等于"该结的全看不见"。
                    # 这里保底：最老的那几条一定要露面（最老的才最该了结）。
                    _picked = {e.get("id") for e in ledger_events}
                    _oldest = sorted(cand, key=lambda e: str(e.get("date")))[:max(3, limit // 2)]
                    ledger_events = ledger_events + [e for e in _oldest if e.get("id") not in _picked]
                merged = open_threads_text.splitlines() if open_threads_text else []
                seen = set(merged)
                fresh: list[str] = []    # 近 3 天新记的
                hanging: list[str] = []  # 挂着的
                stale: list[str] = []    # 挂了一周以上的旧账
                for ev in ledger_events:
                    summary = (ev.get("summary") or "").strip()
                    if not summary:
                        continue
                    ev_id = str(ev.get("id") or "").strip()
                    line = f"- [{ev_id}] {summary}" if ev_id else f"- {summary}"
                    if line in seen:
                        continue
                    seen.add(line)
                    try:
                        age = (today - date.fromisoformat(str(ev.get("date")))).days
                    except Exception:
                        age = 0
                    (fresh if age <= 3 else (hanging if age < 7 else stale)).append(line)
                blocks: list[str] = []
                if fresh:
                    blocks.append("【这两天新记的】" + "\n" + "\n".join(fresh))
                if hanging:
                    blocks.append("【还挂着的】" + "\n" + "\n".join(hanging))
                if stale:
                    blocks.append("【挂了一周以上的旧账】" + "\n" + "\n".join(stale))
                open_threads_text = "\n".join(merged + blocks)
            except Exception as e:
                logger.warning(f"[晨光心语] {persona_id} 生活账本检索失败（已降级）: {e}")

        # ── 内在状态机切片（Phase 1）→ {life_state} ──
        # 注：{life_plan}「今天的骨架」已于 2026-10-06 摘掉（日程改为自由发挥、
        #     状态机跟着日程走，见 CHANGELOG v1.5.11）。要恢复骨架，把下面这两行和
        #     模板里的「🗓️ 今天的骨架」段一起加回来即可：
        #         life_plan = await self.life_bridge.get_plan(persona_id)
        #         life_plan_text = self.life_bridge.render_plan(life_plan)
        life_state_text = ""
        if self.life_bridge is not None and self.plugin.get_config("life_engine.enabled", False):
            try:
                life_state = await self.life_bridge.get_state(persona_id)
                life_state_text = self.life_bridge.render_state(life_state)
            except Exception as e:
                logger.warning(f"[晨光心语] {persona_id} 读取内在状态失败，本次跳过: {e}")

        daily_theme = self._random_pick(self.plugin.get_config("daily_themes", ""), "元气满满")
        mood_color = self._random_pick(self.plugin.get_config("mood_colors", ""), "暖橙")
        outfit_style = self._random_pick(self.plugin.get_config("outfit_styles", ""), "休闲")
        schedule_type = self._random_pick(self.plugin.get_config("schedule_types", ""), "学习")

        # 获取天气信息（每次都重新读配置，改了城市/开关不用重启插件）
        weather_text = ""
        weather_client = getattr(self.plugin, "weather_client", None)
        if weather_client:
            try:
                new_enabled = self.plugin.get_config("weather.weather_enabled", weather_client.enabled)
                new_location = self.plugin.get_config("weather.weather_location", weather_client.location)
                if new_location != weather_client.location:
                    # 城市换了，旧缓存作废（指标是跟缓存同一次解析出来的，一起清）
                    weather_client._cache = None
                    weather_client._cache_date = None
                    weather_client._metrics = None
                weather_client.enabled = new_enabled
                weather_client.location = new_location
                weather_text = await weather_client.get_weather()
            except Exception as e:
                logger.debug(f"[晨光心语] 天气获取失败: {e}")

        weekday_names = ["一", "二", "三", "四", "五", "六", "日"]
        date_str = f"{today.year}年{today.month}月{today.day}日"
        weekday = "星期" + weekday_names[today.weekday()]
        # 先看有没有节日；没有节日再退回「工作日 / 休息日」（两句话在这里合流，日程模板照旧）
        holiday = self._get_holiday_info(today) or self._get_day_kind(today)

        if style:
            persona_desc = f"人格：{persona_id}，风格：{style}"
        else:
            persona_desc = f"人格：{persona_id}"

        persona_prompt = await self._get_persona_prompt(persona_id)

        # J1 · 本地现实地址参考（默认关）：关着时这里是空串，后面一个字都不会动
        local_geo_text = ""
        if self.plugin.get_config("local_geo.enabled", False):
            pois = []
            poi_client = getattr(self.plugin, "poi_client", None)
            if poi_client is not None:
                try:
                    # 一天最多查一次（自己带缓存）；没填 Key / 查不到 都是空列表，不报错
                    pois = await poi_client.day_pois()
                except Exception as e:
                    logger.debug(f"[晨光心语] 本地地点查询跳过: {e}")
            local_geo_text = build_local_geo_text(self.plugin, pois)

        prompt_template = self.plugin.get_config("prompt_template", "")
        if not prompt_template:
            prompt_template = (
                "# Role: Life Scheduler\n"
                "请根据以下信息，为自己规划一份今天的生活安排。\n\n"
                "## Context\n"
                "- 日期：{date_str} {weekday} {holiday}\n"
                "- 今日天气：{weather_info}  ← 若此处为空，请完全忽略天气因素，默认适宜日常活动\n"
                "{local_geo}"
                "- 人设标签：{persona_desc}  ← 若为空，则仅以人格提示词为准\n"
                "- 人格参考：{persona_prompt}\n\n"
                "## 🎲 今日创意调料（灵感参考，不必照搬）\n"
                "- 今日主题（灵感）：【{daily_theme}】\n"
                "- 心情色彩（灵感）：【{mood_color}】\n"
                "- 穿搭风格（建议融合方向）：【{outfit_style}】\n"
                "- 日程类型（灵感参考，非强制）：【{schedule_type}】\n\n"
                "## 🚫 需要避免的重复内容\n"
                "请确保今天的穿搭、活动与以下历史日程有明显差异，避免单调重复：\n"
                "{history_schedules}\n"
                "（若无历史记录，则忽略此约束）\n\n"
                "## 🔗 因果延续（重要）\n"
                "你昨天留下的状态：\n"
                "{yesterday_state}\n"
                "\n"
                "昨天还没了结的事（这是她的待办池，挑跟今天相关的接上，无关的忽略）：\n"
                "{open_threads}\n"
                "（今天要是把其中哪件做完了、或者它本来就到期作废了，就照你自己的话说一句，\n"
                " 写进 resolved_events —— 说清「哪一件、做完了什么」就行，措辞不必跟原文一样。\n"
                " 挂在老账里那些「还没等到回音」「攒着没说出口」的，不算没做完，不用销账，\n"
                " 想继续惦记着就让它挂着。）\n"
                "\n"
                "今天的安排必须与上面的状态自洽：累了就别排满，没做完的事要接上，\n"
                "心情没恢复就允许自己慢一点。（若为空，视为新的一天，自由安排。）\n"
                "\n"
                "## 🫀 你此刻真实的内在状态\n"
                "{life_state}\n"
                "日程必须与它自洽：累了就别安排一整天外出，心情低落就别写得元气满满。\n"
                "这是你自己的状态，不是别人告诉你的设定。\n"
                "\n"
                "## 💡 参考信息\n"
                "- 近期聊天（可反映当前心境，非强制采用）：\n"
                "{recent_chats}\n\n"
                "## 强制约束（按优先级从高到低执行）\n\n"
                "### 1. 人格核心身份（最高优先级）\n"
                "你的人格设定由 `{persona_prompt}` 完整定义。  \n"
                "该身份必须贯穿所有输出：包括穿搭偏好、日常习惯、说话语气、情感反应、活动选择。  \n"
                "若 `{persona_desc}` 提供了额外风格描述（尤其是穿搭偏好），它与人设原设同属最高优先级；"
                "当日穿搭风格应以该偏好为底色，在保留 1-2 个标志性元素的前提下，大胆融合主题风格进行设计，"
                "允许在配色、材质、廓形或配饰上做出惊喜尝试。  \n"
                "当任何其他约束与此冲突时，优先保留人格身份特征。\n\n"
                "### 2. 内在状态（第二优先级）\n你此刻的精力、心情、正在做的事由 `{life_state}` 给出。\n当它与今日主题、穿搭风格或日程类型冲突时，一律以内在状态为准：\n累了就少排，心情不好就允许自己慢一点，不要为了迎合抽到的主题而硬撑。\n（若 `{life_state}` 为空，说明这一条暂时不适用，看下一条即可。）\n\n### 3. 当日主题与穿搭风格（第三优先级）\n"
                "- 今日主题（灵感）：【{daily_theme}】  \n"
                "- 穿搭风格（建议方向）：【{outfit_style}】  \n\n"
                "处理冲突的规则：\n"
                "- 若 `{outfit_style}` 与人格偏好不矛盾 → 直接融合。\n"
                "- 若 `{outfit_style}` 与人格偏好明显冲突 → 依然保留人格偏好的核心元素，仅在局部适应主题。\n"
                "- 允许角色在内心吐槽“为什么今天的主题是这个”，但实际穿搭必须让读者能识别出人格偏好的延续。\n"
                "- 输出字段 `outfit_style` 的值必须原样保留为 `{outfit_style}`（这是一个标签），"
                "而 `outfit` 描述中的实际风格为人格偏好与主题融合后的真实上身效果。\n"
                "- 穿搭同时需考虑今日天气（若 `{weather_info}` 非空），"
                "在不破坏人格偏好的前提下做出适当调整。\n\n"
                "### 4. 日程类型与活动安排（建议参考，非强制）\n"
                "- 提供的日程类型【{schedule_type}】仅作为灵感方向，"
                "你可以完全根据自己的性格、当下心情以及今日天气来决定实际活动。\n"
                "- 如果性格强烈抵触该类型，或者天气完全不适宜，可以自由调整、替代甚至忽略，"
                "用最符合人格的方式度过今天。\n"
                "- 在具体安排活动时，请综合考虑：角色性格的自然反应、"
                "当日心情色彩 `{mood_color}` 带来的情绪基调、以及天气的影响（若天气信息有效），"
                "让日程既忠于人格又贴合现实。\n\n"
                "### 5. 输出中需自然融入的元素\n"
                "- 心情色彩 `{mood_color}` 应渗透在活动选择、场景描述或 `note` 的语气中。\n"
                "- 今日主题 `{daily_theme}` 可作为整日氛围的底色，但不必每项活动都机械点题。\n\n"
                "## 输出格式要求（严格遵循）\n"
                "- 你必须只输出 JSON 对象本体（不要包含任何 Markdown 代码块标记，不要有多余的解释文字，直接以 `{` 开始，以 `}` 结束）。\n"
                "- JSON 必须包含字段：\n"
                '  - `"intent"`: 一句话写清今天的主线目标。先定主线，再铺细节，后面的活动都要围绕它。\n'
                '  - `"outfit_style"`: 值必须严格等于给定的 {outfit_style}\n'
                '  - `"outfit"`: 今日穿搭描述（从里到外、从上到下，包含内衣裤、鞋袜、饰品等），'
                "第一行必须以“风格：{outfit_style}”开头，"
                "后续描述必须体现人格偏好与主题、天气的融合。\n"
                '  - `"schedule"`: 数组，每个元素包含 `"time"`、`"activity"`、`"location"`、`"beats"` 四个字段。\n'
                '    · `"time"`：模糊时段（清晨/上午/午间/下午/傍晚/晚间），不要写具体时间点。\n'
                '    · `"activity"`：中文散文，自由发挥，写这一格里发生了什么，自然融入角色性格与当日心境。\n'
                '      开头别再报一遍时段（「晚间 · 」「上午，」这种都算）——外面已经写着是哪个时段了；\n'
                '      天气可以写，但别把「时段＋天气」捆成一句引子，直接从这一格发生的事说起。\n'
                '    · `"location"`：这一格她人在哪儿。优先从下面这张地点对照表里挑（英文小写）；\n'
                '      表外的地点也可以写，只要那是她真的会去的地方：\n'
                '        home / cafe / park / gym / library / street / workplace /\n'
                '        restaurant / bar / rooftop / beach / school / campus /\n'
                '        bedroom / living room / kitchen / garden / balcony\n'
                '      原则只有一条：这一格的活动，得能在这个地方发生。\n'
                '      晚间那一格，除非有明确的在外理由，填 home。\n'
                '    · `"beats"`：这一格里的**小节拍**，1~3 个（最多 3 个，对应这一格的三个小时），\n'
                '      按时间顺序排列，每个写成 {"do": "<活动名>", "at": "<地点>"}：\n'
                '        - `do` 用英文，优先从下面这张活动对照表里挑；表里没有的，挑最接近的那个：\n'
                '          reading / learning something new / listening to music / puzzles and games /\n'
                '          writing poetry / daydreaming / creating a playlist / sketching ideas /\n'
                '          journaling / meditating / remembering happy moments / people watching /\n'
                '          thinking about user / preparing something to share / napping / sleeping /\n'
                '          relaxing / stargazing / tending to plants / making tea / yoga /\n'
                '          going for a run / gym workout / stretching / morning shower /\n'
                '          skincare routine / cooking a meal / baking something / trying a new recipe /\n'
                '          making hot chocolate / collecting autumn leaves / picnic in the park /\n'
                '          watching the snow fall / texting a friend / having coffee with a friend /\n'
                '          lunch with coworkers / catching up with family / video call with a friend /\n'
                '          going for a walk / running errands / tidying up / online shopping /\n'
                '          beach day / visiting memory beach / exploring the infinite library\n'
                '        - `at` 用上面那张地点对照表里的词。\n'
                '        - 这一格只发生一件事时，`beats` 就写一个（这一格三个小时都会是它）。\n'
                '  - `"note"`: 一段生活感悟或今日寄语，一两句中文，'
                '  - `events`: 数组——今天**新发生**的事，每条写成 {"summary":"一句话","keywords":["关键词"],"status":"open或closed"}。\n'
                "    status 填 open = 这事还没了结（后面还有后续）；填 closed = 已经彻底做完。没有新事就填空数组。\n"
                "  - `resolved_events`: 数组——今天**做完或了结**的旧事，每条用一句话说清「是哪一件、做完了什么」。\n"
                "    例：「压在旧杂志里的落叶干透了，挑了三片做成了书签」。措辞不必跟原文一样，说清是哪一件就行。\n"
                "    没有就填空数组 []。\n"
                '  - `"state"`: 今日状态快照，供明天继承（写成 JSON 对象）：\n'
                "    含 date（今天日期）、energy（0~1 的精力）、mood（情绪词 + 愉悦度 -1~1 + 强度 0~1）、focus（今天最挂心的一件事）、open_threads（今天没做完的事）、carried_over（拖着还没处理的事）、tomorrow_hint（对明天的想法）。\n"
                "    必须如实反映今天的真实状态，不许写成套话；确实没有的项留空。\n"
                "语气必须完全符合人格设定（从 `{persona_prompt}` 提取），也可适当提及天气、心情色彩或日程安排的感受。\n\n"
                "## 开始生成\n"
                "请按照以上约束，输出今日的 JSON 日程。"
            )

        replacements = {
            "{date_str}": date_str,
            "{weekday}": weekday,
            "{holiday}": holiday,
            "{weather_info}": weather_text,
            "{persona_desc}": persona_desc,
            "{persona_prompt}": persona_prompt,
            "{daily_theme}": daily_theme,
            "{mood_color}": mood_color,
            "{outfit_style}": outfit_style,
            "{schedule_type}": schedule_type,
            "{history_schedules}": history_text,
            "{recent_chats}": recent_text,
            "{date_info}": f"{date_str} {weekday}",
            "{holiday_info}": holiday,
            "{style}": style,
            "{theme}": daily_theme,
            "{color}": mood_color,
            "{outfit}": outfit_style,
            "{history}": history_text,
            "{yesterday_state}": yesterday_state_text,
            "{open_threads}": open_threads_text,
            "{life_state}": life_state_text,
            "{local_geo}": local_geo_text,
        }

        prompt = prompt_template
        for key, value in replacements.items():
            prompt = prompt.replace(key, str(value) if value else "")

        # J1：模板里没有 {local_geo} 这个占位符时，把画像补进去（补在「开始生成」之前）。
        # 没开这个开关时 local_geo_text 是空串，下面整段都不执行 —— 生成结果与开功能前一致。
        if local_geo_text and "{local_geo}" not in prompt_template:
            prompt = ensure_local_geo_block(prompt, local_geo_text)
            if not self._local_geo_noted:
                self._local_geo_noted = True
                logger.info(
                    "[晨光心语] 📍 当前提示词模板里没有 {local_geo}，"
                    "本地环境参考已补在「开始生成」之前（不改模板本身）"
                )

        # H1（v1.5.29）：把「别在正文开头自报时段和天气」这条兜上来。
        # 见 _ensure_activity_rule 的说明——模板是用户自己的，不能被覆盖。
        prompt = self._ensure_activity_rule(prompt)

        return {
            "prompt": prompt,
            "style": style,
            "yesterday_state": yesterday_state,
            "seed_state": seed_state,
            "random_elements": {
                "theme": daily_theme,
                "color": mood_color,
                "outfit": outfit_style,
                "schedule_type": schedule_type
            }
        }
