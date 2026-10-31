import asyncio
import datetime
import json
import os
import time
from pathlib import Path

import aiofiles
import holidays

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.message import TextPart

from .commands.debug import DebugCommands
from .commands.schedule import PersonaCommands, ScheduleCommands
from .core.context_builder import ContextBuilder
from .core.event_ledger import EventLedger
from .core.generator import ScheduleGenerator
from .core.life_bridge import (
    LifeBridgeManager,
    activity_zh,
    energy_zh,
    location_zh,
    mood_zh,
)
from .core.external_memory import ExternalMemory
from .core.memory_bridge import MemoryBridge
from .core.memory_source import MemorySource
from .core.proactive import ProactiveEngine
from .core.proactive_store import (
    ProactiveState,
    SubscriptionStore,
    format_skipped_line,
)
from .core.schedule_text import strip_legacy_period_prefix
from .core.scheduler import MorningScheduler
from .core.state_store import StateStore
from .core.storage import ScheduleStorage
from .core.timezone_utils import get_now, get_today, set_default_tz
from .core.view_data import (
    build_overview,
    collect_status,
    period_index_of,
    regenerate_today,
    render_status_text,
)
from .core.weather_client import AlarmClient, WeatherClient, aclose_client

# 插件标识：页面接口的路由前缀就用它（AstrBot 的插件页面 SDK 会按这个拼地址）
PLUGIN_NAME = "astrbot_plugin_morning_muse"


def _collect_memory_texts(req) -> list[str]:
    """把这一轮请求里「可能装着记忆块」的文本全捞出来，交给 MemoryBridge.extract 按标签挑。

    B11 修复 —— **为什么不能只看 system_prompt**：
    记忆插件现在各有各的注入位置，谁都不一定写 system_prompt 了：
    - livingmemory（`<RAG-Faiss-Memory>`）默认塞进 `extra_user_content_parts`（用户消息末尾）；
    - Mnemosyne（`<Mnemosyne>`）默认往 `contexts` 里插一条 role=system 的消息。
    所以这三处都扫。`extract()` 只认名字里带 memory/mnemo/rag 字样的标签，
    不会把 `<tools>`、`<context>` 这些不相干的块捞进池子。
    """
    out: list[str] = []

    def _add(v):
        if isinstance(v, str) and v.strip():
            out.append(v)

    _add(getattr(req, "system_prompt", ""))

    for part in getattr(req, "extra_user_content_parts", None) or []:
        if isinstance(part, dict):
            _add(part.get("text"))
        else:
            _add(getattr(part, "text", None))

    # ⚠️ **只扫 contexts 里 role=system 的那几条**，绝不扫整段 contexts、更不扫 prompt。
    # 原因（第一版的教训）：contexts 和 prompt 是**对话历史**，谁在里面贴过
    # `<RAG-Faiss-Memory>` 这几个字（日志、代码、截图、报错），就会被当成记忆块捞进池子 ——
    # 池子会变成垃圾场。记忆插件是把记忆作为一条 role=system 的消息插进来的
    # （Mnemosyne 就是 `contexts.insert(0, {"role": "system", ...})`），只认这一种。
    for msg in getattr(req, "contexts", None) or []:
        if not isinstance(msg, dict) or msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            _add(content)
        elif isinstance(content, list):
            for seg in content:
                if isinstance(seg, dict):
                    _add(seg.get("text"))
    return out


class ChatHistoryCache:
    """按人格ID缓存聊天记录，带持久化存储。优先读缓存 -> 再读持久化 -> 没有再跳过。"""

    def __init__(self, data_dir: Path, max_per_persona: int = 5):
        self.max_per_persona = max_per_persona
        self._cache: dict[str, list[str]] = {}  # {persona_id: [msg1, msg2, ...]}
        self._file = data_dir / "chat_history_cache.json"
        self._save_lock = asyncio.Lock()

    async def load(self):
        """从持久化文件加载缓存"""
        if self._file.exists():
            try:
                async with aiofiles.open(self._file, encoding="utf-8") as f:
                    content = await f.read()
                data = json.loads(content)
                self._cache = data.get("cache", {})
                logger.info(f"[晨光心语] 聊天缓存已从持久化加载: {len(self._cache)} 个人格")
            except Exception as e:
                logger.warning(f"[晨光心语] 加载聊天缓存持久化失败: {e}")
                self._cache = {}

    async def save(self):
        """持久化缓存到文件（带锁 + 原子替换，防止写坏 JSON）"""
        async with self._save_lock:
            try:
                payload = json.dumps({"cache": self._cache}, ensure_ascii=False, indent=2)
                tmp_file = self._file.with_suffix(".json.tmp")
                async with aiofiles.open(tmp_file, "w", encoding="utf-8") as f:
                    await f.write(payload)
                os.replace(tmp_file, self._file)
            except Exception as e:
                logger.warning(f"[晨光心语] 保存聊天缓存持久化失败: {e}")

    def update(self, persona_id: str, messages: list[str]):
        """更新某人格的聊天缓存（保留最新N条）"""
        if not messages:
            return
        existing = self._cache.get(persona_id, [])
        combined = existing + messages
        self._cache[persona_id] = combined[-self.max_per_persona:]

    def get(self, persona_id: str) -> list[str]:
        """获取某人格的聊天缓存。优先读内存 -> 没有返回空。"""
        return self._cache.get(persona_id, [])

class MorningMusePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        self._config = config

        self.storage: ScheduleStorage | None = None
        self.generator: ScheduleGenerator | None = None
        self.scheduler: MorningScheduler | None = None
        self.dynamic_styles: dict[str, str] = {}
        self.persona_map: dict[str, str] = {}
        self.debug_logs: list[dict] = []
        self._max_debug_logs = 100
        self._ready = False
        self.chat_cache: ChatHistoryCache | None = None
        self.weather_client: WeatherClient | None = None
        self._init_task: asyncio.Task | None = None
        self._delayed_task: asyncio.Task | None = None
        # 日程兜底：生成那一跳被打断（重启/报错）时，由心跳顺手补一次。
        # 计数按人格记，一天最多补 _SCHEDULE_CATCH_UP_MAX 次，防死磕。
        self._schedule_catch_up_count: dict[str, int] = {}
        self._schedule_catch_up_running = False
        # 内在状态机桥（Phase 1）：未启用时为 None，全插件按「没有它」跑
        self.life_bridge: LifeBridgeManager | None = None
        self._life_tick_task: asyncio.Task | None = None
        # 攥住后台小任务的引用，否则 asyncio 只持弱引用，可能被垃圾回收提前吃掉
        self._bg_tasks: set = set()
        # 生活账本 / 记忆搭桥（Phase 2）
        self.event_ledger: EventLedger | None = None
        self.memory_bridge: MemoryBridge | None = None
        # 外部记忆库直连（只读 · 主路）：没配就整个是 off，不碰任何外部服务
        self.external_memory: ExternalMemory | None = None
        self.memory_source: MemorySource | None = None
        self._last_hook_texts: dict[str, list[str]] = {}
        # 主动消息引擎（P0）：订阅白名单 + 手动点火。默认关；开着也只在订阅过的会话开口
        self.subscriptions: SubscriptionStore | None = None
        self.proactive_state: ProactiveState | None = None
        self.proactive: ProactiveEngine | None = None
        # 插件页面（v1.5.29）：重写的防连点锁 + 每人格 60 秒节流
        self._view_rewrite_lock: set[str] = set()
        self._view_rewrite_at: dict[str, float] = {}

        # 持有引用，防止未完成的任务被垃圾回收提前吃掉
        self._init_task = asyncio.create_task(self._async_init())

    def _get_today(self) -> datetime.date:
        """获取配置时区的当前日期"""
        tz_name = self.get_config("schedule.timezone", "Asia/Shanghai")
        return get_today(tz_name)

    def _get_now(self) -> datetime.datetime:
        """获取配置时区的当前时间"""
        tz_name = self.get_config("schedule.timezone", "Asia/Shanghai")
        return get_now(tz_name)

    def _sync_runtime_config(self):
        """把「支持热更新」的配置同步给运行时对象。

        目前只有「存储缓存上限」这一项：它在插件初始化时传给了 ScheduleStorage，
        但用户改了设置页之后不会自动生效。这里在每轮对话前顺手对一次——配置读取
        是 O(1)，只有值真的变了才去动缓存，开销可以忽略。
        """
        try:
            size = self.get_config("storage.storage_cache_size", 200)
            try:
                size = int(size)
            except (TypeError, ValueError):
                return
            # A5 修复：比的是「钳制后实际会生效的值」——否则配置填 0 时
            # 每轮都是 0 != 10，会反复重设 + 刷日志。
            want = max(10, size)
            if self.storage is not None and want != self.storage._max_cache_size:
                self.storage.set_max_cache_size(want)
                logger.info(f"[晨光心语] 💾 缓存上限已热更新为 {want}")
        except Exception:
            pass

    async def _async_init(self):
        try:
            # E15：先把「配置时区」定成全局默认，供账本 / 状态种子 / 历史日程
            # 这些拿不到 plugin 引用的模块用，免得它们各按系统时区算「今天」。
            set_default_tz(self.get_config("schedule.timezone", "Asia/Shanghai"))

            data_dir = Path("data/plugin_data/astrbot_plugin_morning_muse")
            data_dir.mkdir(parents=True, exist_ok=True)
            logger.info("✅ 数据目录: " + str(data_dir.absolute()))

            schedules_dir = data_dir / "schedules"
            cache_size = self.get_config("storage.storage_cache_size", 200)
            self.storage = ScheduleStorage(schedules_dir, max_cache_size=cache_size)

            # 内在状态机（Phase 1）：没装 aura-life、或开关没开时，这里是空转的
            life_enabled = self.get_config("life_engine.enabled", False)
            try:
                self.life_bridge = LifeBridgeManager(
                    self, data_dir, max_personas=self.get_config("life_engine.max_personas", 5)
                )
                if not self.life_bridge.available():
                    self.life_bridge = None
            except Exception as e:
                logger.warning(f"[晨光心语] ⚠️ 内在状态机初始化失败，已跳过: {e}")
                self.life_bridge = None

            # 生活账本 + 记忆搭桥（Phase 2）
            self.event_ledger = EventLedger(data_dir)
            self.memory_bridge = MemoryBridge(
                data_dir,
                source_tag=self.get_config("bridge.source_tag", ""),
                max_items=self.get_config("bridge.max_items", 2000),
            )

            # 外部记忆库直连（只读 · 主路）+ 统一来源门面（主路 → 兜底池）
            # backend=off（默认）时这里什么都不做，行为与老版本完全一致
            try:
                self.external_memory = ExternalMemory(self, data_dir)
                self.memory_source = MemorySource(
                    plugin=self, external=self.external_memory, pool=self.memory_bridge
                )
                if self.external_memory.available():
                    logger.info(f"[晨光心语] 🌐 外部记忆库已启用（只读）: {self.external_memory.describe()}")
                else:
                    logger.info("[晨光心语] 🌐 外部记忆库未启用（backend=off），记忆只走本地池")
            except Exception as e:
                self.external_memory = None
                self.memory_source = MemorySource(plugin=self, external=None, pool=self.memory_bridge)
                logger.warning(f"[晨光心语] ⚠️ 外部记忆库初始化失败，已跳过（不影响主流程）: {e}")

            self.context_builder = ContextBuilder(
                self, self.storage, holidays, life_bridge=self.life_bridge,
                event_ledger=self.event_ledger, memory_bridge=self.memory_source
            )

            self.generator = ScheduleGenerator(
                self,
                self.storage,
                self.context_builder,
                context=self.context,
                debug_logs=self.debug_logs,
                life_bridge=self.life_bridge,
                event_ledger=self.event_ledger
            )

            self.scheduler = MorningScheduler(self.generator, self)
            self.scheduler.start()

            await self._load_dynamic_data()

            # 初始化聊天记录缓存（带持久化）
            self.chat_cache = ChatHistoryCache(data_dir)
            await self.chat_cache.load()

            # 初始化天气客户端
            weather_enabled = self.get_config("weather.weather_enabled", False)
            weather_location = self.get_config("weather.weather_location", "Beijing")
            weather_tz = self.get_config("schedule.timezone", "Asia/Shanghai")
            self.weather_client = WeatherClient(
                enabled=weather_enabled, location=weather_location, tz_name=weather_tz
            )

            # 官方预警客户端（v1.5.16）：只有它能在「今天算不算大霾」上说了算。
            # 自己带缓存（一份、内存里、跨天作废）、只白天刷、取不到就当没有 ——
            # 详见 core/weather_client.py 里 AlarmClient 的文档串。
            self.alarm_client = AlarmClient(
                enabled=self.get_config("proactive_triggers.weather_alarm_enabled", True),
                areas=self.get_config("proactive_triggers.weather_alarm_areas", None),
                types=self.get_config("proactive_triggers.weather_alarm_types", None),
                levels=self.get_config("proactive_triggers.weather_alarm_levels", None),
                tz_name=weather_tz,
            )

            # 主动消息引擎（P0）：把零件装上机器。这一步只打通「订阅 + 手动发一条」，
            # 自动触发要等接上心跳才开跑。
            try:
                self.subscriptions = SubscriptionStore(data_dir)
                await self.subscriptions.load()
                self.proactive_state = ProactiveState(data_dir)
                await self.proactive_state.load()
                self.proactive = ProactiveEngine(
                    self,
                    self.subscriptions,
                    self.proactive_state,
                    life_bridge=self.life_bridge,
                    event_ledger=self.event_ledger,
                    memory_bridge=self.memory_source,
                    weather_client=self.weather_client,
                    storage=self.storage,
                    generator=self.generator,
                    context_builder=self.context_builder,
                )
                logger.info(
                    f"[晨光心语] 📣 主动消息引擎就绪（已订阅会话 {self.subscriptions.count()} 个）"
                )
            except Exception as e:
                self.proactive = None
                self.subscriptions = None
                self.proactive_state = None
                logger.warning(f"[晨光心语] ⚠️ 主动消息引擎初始化失败，已跳过: {e}")

            # 延迟后台补齐所有已知人格的今日日程
            self._delayed_task = asyncio.create_task(self._delayed_generate_all())

            # 内在状态机的轻量心跳（0 = 不常驻，只在写日程时推进一次）
            tick_minutes = self.get_config("life_engine.tick_interval_minutes", 60)
            if life_enabled and self.life_bridge is not None and tick_minutes and tick_minutes > 0:
                self._life_tick_task = asyncio.create_task(self._life_tick_loop(int(tick_minutes)))
                logger.info(f"[晨光心语] 🫀 内在状态机已启用，每 {tick_minutes} 分钟推进一次")
            elif life_enabled and self.life_bridge is not None:
                # ★ 防「静默停摆」：填 0 = 只在写日程时推进一次（设计如此）；填**负数** = 等同关闭。
                # 以前这里什么都不说 —— 插件照常加载、日志照常打「插件已初始化」，
                # 但心跳任务根本没建：主动消息 / 停机补算 / 睡眠回血 / 账本清理 / 日程接管
                # 全都不会跑，排查时完全看不出原因。所以补这条 warning。
                logger.warning(
                    f"[晨光心语] ⚠️ 内在状态机心跳未启动（tick_interval_minutes={tick_minutes}）："
                    "主动消息、停机补算、睡眠回血、账本清理、日程接管都不会跑。"
                    "（填 0 = 只在写日程时推进一次；负数额同于关闭）"
                )

            self._ready = True
            # 插件页面接口：放在 _ready 之后挂，页面一打开就取得到数
            self._register_view_api()
            logger.info("✅ 晨光心语插件已初始化")
        except Exception as e:
            logger.error(f"❌ 插件初始化失败: {e}", exc_info=True)

    # ---------- 内在状态机的时钟（Phase 1） ----------
    def _keep_task(self, task: asyncio.Task):
        """攥住后台小任务的引用——asyncio 只持弱引用，不然可能跑一半就被回收掉。"""
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _life_tick_loop(self, interval_minutes: int):
        """轻量心跳：每 N 分钟把所有活跃人格的状态往前推一步。

        刻意不启用 aura-life 自带的后台调度器（那会起 AsyncIOScheduler）——
        时钟由插件自己掌握，零常驻开销。
        """
        await asyncio.sleep(45)  # 避开启动高峰
        while True:
            try:
                await self._life_tick_once()
            except Exception as e:
                logger.warning(f"[晨光心语] ⚠️ 状态机心跳异常: {e}")
            # 顺手看一眼今天的日程在不在 —— 生成那一跳要是被打断了（重启/报错），
            # 这里最多两个小时就补上，不用干等到第二天。
            try:
                await self._catch_up_today_schedule()
            except Exception as e:
                logger.debug(f"[晨光心语] 心跳兜底日程检查跳过: {e}")
            await asyncio.sleep(max(60, interval_minutes * 60))

    async def _life_tick_once(self):
        if self.life_bridge is None or not self.get_config("life_engine.enabled", False):
            return
        for pid in await self._get_active_persona_ids():
            # 停过机就先把落下的补回来（服务器关了一夜 / 一个周末）
            if self.get_config("life_engine.catch_up", True):
                downtime = 0.0
                try:
                    last = await self.life_bridge.last_state_update(pid)
                    if last is not None:
                        now = self._get_now()
                        if now.tzinfo is None and last.tzinfo is not None:
                            last = last.replace(tzinfo=None)
                        elif now.tzinfo is not None and last.tzinfo is None:
                            now = now.replace(tzinfo=None)
                        downtime = max(0.0, (now - last).total_seconds() / 3600.0)
                except Exception:
                    downtime = 0.0
                if downtime >= 1:
                    await self.life_bridge.catch_up(pid, downtime)
            # 替 AI 人格补一次「睡醒」：清清醒时长 + 回血（每天只补一次）
            if self.get_config("life_engine.sleep_reset", True):
                try:
                    if await self.life_bridge.morning_wake(pid):
                        logger.info(f"[晨光心语] 🌅 {pid} 补了一次晨间睡醒")
                except Exception as e:
                    logger.debug(f"[晨光心语] {pid} 睡醒补算跳过: {e}")
            await self.life_bridge.tick(pid)
            # Step 1（睡眠硬接管）：睡着那几格钉成「睡觉 · 家」。
            # 必须在 tick 之后 —— tick 里的 _on_plan_tick 会重排计划，先 tick 后钉才盖得住。
            if self.get_config("life_engine.schedule_replay", True):
                try:
                    if await self.life_bridge.enforce_sleep_slot(pid):
                        logger.info(f"[晨光心语] 😴 {pid} 睡眠接管：当前这一格已钉成「睡觉 · 家」")
                except Exception as e:
                    logger.debug(f"[晨光心语] {pid} 睡眠接管跳过: {e}")
            # 睡眠窗口内缓慢回血：她睡着了就慢慢暖回来，不是早上突然满格
            if self.get_config("life_engine.sleep_recover", True):
                try:
                    await self.life_bridge.sleep_recover(pid)
                except Exception as e:
                    logger.debug(f"[晨光心语] {pid} 睡眠回血跳过: {e}")
            # Step 1（日程 → 状态机）：把当天日程里到点、还没回灌过的安排补上（幂等）
            await self._replay_today_schedule(pid)
            # 主动消息心跳：她自己挑合适的时机冒个泡（没订阅、没开开关就什么都不做）
            if self.proactive is not None:
                try:
                    await self.proactive.tick_persona(pid)
                except Exception as e:
                    logger.debug(f"[晨光心语] {pid} 主动消息心跳跳过: {e}")
            # 顺手做低频维护：清掉过期旧账（未了结的一律留着）
            try:
                if self.event_ledger is not None:
                    await self.event_ledger.prune(
                        pid, self.get_config("event_ledger.retention_days", 30)
                    )
                if self.memory_bridge is not None:
                    await self.memory_bridge.prune(
                        pid, self.get_config("bridge.retention_days", 30)
                    )
            except Exception as e:
                logger.debug(f"[晨光心语] {pid} 账本维护跳过: {e}")

    async def _replay_today_schedule(self, persona_id: str):
        """Step 3（日程 → 状态机）：心跳里把日程**铺进**状态机的计划表。

        日程早上生成时大多还没到点；这里每小时跑一次，把「还没过完的时段」
        按 `beats` 铺进它那 3 个格子（幂等，每跳重铺，防它自己 revise 抢回去）。
        睡眠窗口里的小时由 `enforce_sleep_slot` 管，这里会让路。
        """
        if self.life_bridge is None or self.storage is None:
            return
        if not self.get_config("life_engine.schedule_replay", True):
            return
        try:
            schedule = await self.storage.load_schedule(persona_id, self._get_today())
            if not schedule:
                return
            done = await self.life_bridge.replay_schedule(
                persona_id, schedule.get("schedule") or []
            )
            if done:
                logger.info(f"[晨光心语] 🧭 {persona_id} 日程接管：{'、'.join(done)}")
        except Exception as e:
            logger.debug(f"[晨光心语] {persona_id} 日程接管跳过: {e}")

    async def _delayed_generate_all(self):
        """延迟 10 秒后，检查并补齐所有已知人格的今日日程"""
        await asyncio.sleep(10)  # 避开启动高峰

        try:
            all_personas = await self._get_active_persona_ids()
            if not all_personas:
                return

            today = self._get_today()
            yesterday = today - datetime.timedelta(days=1)
            tasks = []
            for pid in all_personas:
                existing = await self.storage.load_schedule(pid, today)
                if existing:
                    logger.info(f"[晨光心语] 📅 {pid} 今日日程已存在，跳过")
                else:
                    yesterday_schedule = await self.storage.load_schedule(pid, yesterday)
                    if yesterday_schedule:
                        logger.info(f"[晨光心语] ⏰ {pid} 今日日程尚未生成（昨日日程存在），等待定时任务触发")
                    else:
                        logger.info(f"[晨光心语] 🆕 新用户 {pid}，自动生成首日日程...")
                        tasks.append(self.generator.generate(pid, self.dynamic_styles, recent_messages=[], force=False))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                success = sum(1 for r in results if r and not isinstance(r, Exception))
                logger.info(f"[晨光心语] ✅ 后台补齐完成：{success}/{len(tasks)} 成功")
            else:
                logger.info("[晨光心语] ✅ 所有已知人格今日日程已就绪")

            # 顺手把内在状态机也预热起来——让启动日志连成一串，不必等第一次心跳
            if self.life_bridge is not None and self.get_config("life_engine.enabled", False):
                for pid in await self._get_active_persona_ids():
                    await self.life_bridge.ensure(pid)
        except Exception as e:
            logger.error(f"[晨光心语] ❌ 后台补齐失败: {e}")

        # 启动时如果已经过了生成时间、今天日程还缺着，顺手补一次（别干等一小时后的心跳）
        try:
            await self._catch_up_today_schedule()
        except Exception:
            pass

    _SCHEDULE_CATCH_UP_MAX = 3

    async def _catch_up_today_schedule(self):
        """心跳顺手兜底：今天的日程缺了、又已经过了生成时间 → 补一次。

        「瞄一眼」很轻（读一次文件），只有真缺了才花一次模型调用：
          · 还没到生成时间 → 什么都不做（免得手快，把明天的日程提前生了）
          · 今天已经有了 → 计数归零，什么都不做
          · 新人格（昨天也没有）→ 交给启动补齐那条路，这里不管
          · 补了 _SCHEDULE_CATCH_UP_MAX 次还没成 → 收手，明天再说
        """
        if not self._ready or self._schedule_catch_up_running:
            return
        try:
            all_personas = await self._get_active_persona_ids()
        except Exception:
            return
        if not all_personas:
            return

        try:
            hh, mm = (int(x) for x in str(
                self.get_config("schedule.schedule_time", "08:00")).split(":"))
        except Exception:
            hh, mm = 8, 0
        now = self._get_now()
        if (now.hour, now.minute) < (hh, mm):
            return  # 还没到生成时间

        today = self._get_today()
        yesterday = today - datetime.timedelta(days=1)
        self._schedule_catch_up_running = True
        try:
            for pid in all_personas:
                try:
                    if await self.storage.load_schedule(pid, today):
                        self._schedule_catch_up_count.pop(pid, None)  # 有了就归零
                        continue
                    if not await self.storage.load_schedule(pid, yesterday):
                        continue  # 新人格，走启动补齐那条路
                    used = self._schedule_catch_up_count.get(pid, 0)
                    if used >= self._SCHEDULE_CATCH_UP_MAX:
                        continue
                    self._schedule_catch_up_count[pid] = used + 1
                    logger.warning(
                        f"[晨光心语] 🩹 {pid} 今日日程缺失（已过 {hh:02d}:{mm:02d}），"
                        f"心跳兜底补生成（第 {used + 1}/{self._SCHEDULE_CATCH_UP_MAX} 次）"
                    )
                    await self.generator.generate(
                        pid, self.dynamic_styles, recent_messages=[], force=False
                    )
                except Exception as e:
                    logger.debug(f"[晨光心语] {pid} 心跳兜底补生成跳过: {e}")
        finally:
            self._schedule_catch_up_running = False

    async def _load_dynamic_data(self):
        dynamic_path = Path("data/plugin_data/astrbot_plugin_morning_muse") / "dynamic_data.json"
        if dynamic_path.exists():
            try:
                async with aiofiles.open(dynamic_path, encoding="utf-8") as f:
                    content = await f.read()
                data = json.loads(content)
                self.dynamic_styles = data.get("styles", {})
                self.persona_map = data.get("personas", {})
            except Exception:
                pass

    async def _save_dynamic_data(self):
        dynamic_path = Path("data/plugin_data/astrbot_plugin_morning_muse") / "dynamic_data.json"
        try:
            async with aiofiles.open(dynamic_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps({
                    "styles": self.dynamic_styles,
                    "personas": self.persona_map
                }, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"保存动态数据失败: {e}")

    async def _get_active_persona_ids(self) -> list[str]:
        """获取插件已知且仍在AstrBot中活跃的人格ID列表，过滤掉已关闭的人格"""
        known = self.context_builder.get_known_persona_ids()
        try:
            persona_mgr = self.context.persona_manager
            if persona_mgr:
                all_personas = await persona_mgr.get_all_personas()
                active_ids = {p.persona_id for p in all_personas}
                filtered = [pid for pid in known if pid in active_ids or pid == "default"]
                if filtered:
                    skipped = set(known) - set(filtered)
                    if skipped:
                        logger.info(f"[晨光心语] 以下人格已从AstrBot关闭，跳过生成: {skipped}")
                    return filtered
        except Exception as e:
            logger.warning(f"[晨光心语] 查询活跃人格失败，回退到全部已知人格: {e}")
        return known

    async def _get_persona_id(self, event: AstrMessageEvent) -> str:
        """获取当前会话绑定的人格ID。

        顺序：AstrBot 会话绑定 → persona_map → AstrBot 的默认人格。
        注意：会话没绑定人格时，AstrBot 真正使用的是配置里的默认人格
        （agent_runner.config.persona.persona_id）。以前这里硬编码成字面量
        "default"，可人格库里根本没有这个名字，导致后面取人格提示词必然报错。
        """
        umo = event.unified_msg_origin
        try:
            curr_id = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if curr_id:
                conv = await self.context.conversation_manager.get_conversation(umo, curr_id)
                if conv and conv.persona_id:
                    logger.info(f"[晨光心语] 从AstrBot会话系统获取到人格: {conv.persona_id}")
                    return conv.persona_id
        except Exception as e:
            logger.warning(f"[晨光心语] 查询会话人格异常，回退persona_map: {e}")

        pid = self.persona_map.get(event.message_obj.session_id, "")
        if pid:
            logger.info(f"[晨光心语] 从persona_map获取到人格: {pid}")
            return pid

        fallback = self._get_default_persona_id(umo)
        logger.info(f"[晨光心语] 会话未绑定人格，按AstrBot默认人格解析为: {fallback}")
        return fallback

    def _get_default_persona_id(self, umo: str = "") -> str:
        """按 AstrBot 的规则解析默认人格ID：会话配置优先，其次全局默认人格。"""
        try:
            persona_mgr = getattr(self.context, "persona_manager", None)
            if persona_mgr:
                if umo:
                    try:
                        cfg = persona_mgr.acm.get_conf(umo)
                        agent_runner = cfg.get("agent_runner", {}) or {}
                        runner_config = agent_runner.get("config", {}) or {}
                        if agent_runner.get("runner_type") == "local":
                            pid = (runner_config.get("persona", {}) or {}).get("persona_id", "")
                        else:
                            pid = runner_config.get("persona_id", "")
                        if pid:
                            return pid
                    except Exception as e:
                        logger.debug(f"[晨光心语] 读取会话配置的默认人格失败: {e}")
                pid = getattr(persona_mgr, "default_persona", "") or ""
                if pid:
                    return pid
        except Exception as e:
            logger.warning(f"[晨光心语] 解析默认人格失败: {e}")
        return "default"

    async def _get_recent_messages(self, event: AstrMessageEvent, limit: int = 5) -> list[str]:
        """读取当前会话的最近几条对话（人类可读文本）。

        AstrBot 的 Context 并没有 get_recent_messages 这个方法，
        以前直接调用它会抛 AttributeError 又被 except 吞掉，导致「近期聊天参考」
        一直是空的。这里改走 conversation_manager 的 get_human_readable_context。
        """
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return []
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            return []
        try:
            curr_id = await cm.get_curr_conversation_id(umo)
            if not curr_id:
                return []
            contexts, _ = await cm.get_human_readable_context(
                umo, curr_id, page=1, page_size=limit
            )
            messages = [c for c in (contexts or []) if isinstance(c, str) and c.strip()]
            return messages[-limit:]
        except Exception as e:
            logger.debug(f"[晨光心语] 读取最近消息失败: {e}")
            return []

    # ---------- 配置访问（支持嵌套键）----------
    def get_config(self, key: str, default=None):
        """支持点号分隔的嵌套键，例如 'persona_styles.persona_name_1'"""
        if "." in key:
            node = self._config
            for k in key.split("."):
                if isinstance(node, dict) and k in node:
                    node = node[k]
                else:
                    return default
            return node
        if key in self._config:
            return self._config[key]
        return default

    async def set_config(self, key: str, value):
        """支持点号分隔的嵌套键，例如 'schedule.schedule_time'。

        以前是把整串 "schedule.schedule_time" 当成顶层键写进去的，
        配置文件里会多出一个脏键，WebUI 上读到的还是旧值。
        """
        # 清理历史遗留的、被塞在顶层的点号脏键
        if "." in key and key in self._config:
            try:
                del self._config[key]
            except Exception:
                pass
        keys = key.split(".")
        node = self._config
        for k in keys[:-1]:
            child = node.get(k) if isinstance(node, dict) else None
            if not isinstance(child, dict):
                child = {}
                node[k] = child
            node = child
        node[keys[-1]] = value
        # V3 修复：Star 基类**没有** save_config（那是 AstrBotConfig 的方法），
        # 原来的 `await self.save_config()` 必抛 AttributeError → 命令没有回执、
        # 配置也不落盘。配置对象挂在 self._config 上，优先用它的异步版。
        saver = getattr(self._config, "save_config_async", None)
        if callable(saver):
            await saver()
        else:
            self._config.save_config()

    def get_style_for_persona(self, persona_name: str) -> str:
        if persona_name in self.dynamic_styles:
            return self.dynamic_styles[persona_name]
        for i in range(1, 4):
            pname = str(self.get_config(f"persona_styles.persona_name_{i}", "") or "").strip()
            style = str(self.get_config(f"persona_styles.style_control_{i}", "") or "").strip()
            if pname and pname == persona_name:
                return style
        return self.get_config("persona_styles.style_control_default", "自然亲切的语气")

    # ---------- 命令 ----------
    # ---------- 插件页面（v1.5.29 · views/晨光心语/） ----------
    #
    # 入口是插件卡片上的「插件页面」，页面只负责取数 + 渲染，业务逻辑全在这边：
    #   · 两个读接口（今日日程 / 此刻状态 / 插件配置，一次取全）
    #   · 一个写接口（重写单人今日日程，等价 /重写日程，绝不碰 /重写所有人）
    # 路由前缀必须是 /{插件名}/...，AstrBot 的页面 SDK 会把它拼成
    # /api/v1/plugins/extensions/{插件名}/{endpoint}，前端那边只写 'view/overview'。

    VIEW_REWRITE_COOLDOWN = 60  # 同一人格两次重写至少隔 60 秒

    def _register_view_api(self) -> None:
        routes = (
            (
                f"/{PLUGIN_NAME}/view/overview",
                self.page_view_overview,
                ["GET"],
                "晨光心语：插件页面取数（只读）",
            ),
            (
                f"/{PLUGIN_NAME}/view/rewrite",
                self.page_view_rewrite,
                ["POST"],
                "晨光心语：重写当前人格的今日日程",
            ),
        )
        mounted = 0
        for route, handler, methods, desc in routes:
            try:
                self.context.register_web_api(route, handler, methods, desc)
                mounted += 1
            except Exception as e:
                # 挂不上不影响命令：页面进不去，但 /查看日程 这些照旧
                logger.warning(f"[晨光心语] ⚠️ 页面接口注册失败（不影响命令）：{e}")
        if mounted:
            logger.info(f"[晨光心语] 🖼 插件页面接口已就绪（{mounted}/{len(routes)}）")

    async def page_view_overview(self):
        """页面一打开取一次：人格清单 + 今日日程 + 此刻状态 + 配置概览。"""
        if not self._ready:
            return error_response("插件还在初始化，等几秒再刷新")
        persona = str(request.query.get("persona", "") or "").strip()
        try:
            data = await build_overview(self, persona)
        except Exception as e:
            logger.warning(f"[晨光心语] 🖼 页面取数失败: {e}")
            return error_response(f"取数失败：{type(e).__name__}: {e}")
        return json_response(data)

    async def page_view_rewrite(self):
        """重写一个人格的今日日程 —— 会真花一次模型调用，所以三道闸门。

        闸门：① 只认活跃人格 ② 正在生成时不许再点 ③ 同人格 60 秒内只放一次。
        失败一律把原因照实回给页面，绝不静默当成功。
        """
        if not self._ready or self.generator is None:
            return error_response("插件还在初始化，等几秒再试")

        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")

        persona = str(payload.get("persona", "") or "").strip()
        if not persona:
            return error_response("没说要重写哪个人格（页面没传 persona）")

        try:
            active = [str(x) for x in (await self._get_active_persona_ids() or [])]
        except Exception as e:
            logger.warning(f"[晨光心语] 🖼 取活跃人格失败: {e}")
            active = []
        if persona not in active:
            return error_response("这个人格不在活跃名单里（可能已经在 AstrBot 里关掉了）")

        if persona in self._view_rewrite_lock:
            return error_response("正在生成中，等它跑完喵")

        now = time.monotonic()
        left = self.VIEW_REWRITE_COOLDOWN - (now - self._view_rewrite_at.get(persona, 0.0))
        if left > 0:
            return error_response(f"刚重写过，{int(left) + 1} 秒后再点")

        self._view_rewrite_lock.add(persona)
        self._view_rewrite_at[persona] = now
        try:
            ok, note, schedule = await regenerate_today(self, persona)
        finally:
            self._view_rewrite_lock.discard(persona)

        if not ok:
            # 失败了让人能立刻重试，不被那 60 秒卡着
            self._view_rewrite_at.pop(persona, None)
            logger.warning(f"[晨光心语] 🖼 页面重写 {persona} 失败：{note}")
            return error_response(note)

        logger.info(f"[晨光心语] 🖼 页面重写了 {persona} 的今日日程")
        return json_response(
            {
                "ok": True,
                "persona": persona,
                "message": note,
                "schedule": schedule,
            }
        )

    @filter.command("状态", desc="查看当前人格的内在状态（精力 / 心情 / 此刻在做什么）")
    async def show_state(self, event: AstrMessageEvent):
        if not self._ready:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        persona_id = await self._get_persona_id(event)
        # 取数在 core/view_data.py，插件页面的「此刻状态」读的是同一份 collect_status()
        try:
            data = await collect_status(self, persona_id)
        except Exception as e:
            logger.warning(f"[晨光心语] {persona_id} 读取状态失败: {e}")
            data = {"persona_id": persona_id, "live": None, "shares": []}
        yield event.plain_result(render_status_text(data))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("状态重置", desc="把当前人格的内在状态重置为初始值")
    async def reset_persona_state(self, event: AstrMessageEvent):
        if not self._ready:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        if self.life_bridge is None:
            yield event.plain_result("⚠️ 内在状态没有启用，没什么可重置的喵。")
            return
        persona_id = await self._get_persona_id(event)
        ok = await self.life_bridge.reset(persona_id)
        if self.event_ledger is not None:
            self.event_ledger.forget_cache(persona_id)
        if self.memory_bridge is not None:
            self.memory_bridge.forget_cache(persona_id)
        if ok:
            yield event.plain_result(f"🔄 已把「{persona_id}」的内在状态重置为初始值喵。")
        else:
            yield event.plain_result("⚠️ 重置失败，稍后再试试～")

    @filter.command("查看日程", desc="查看当前人格的今日日程安排")
    async def view_schedule(self, event: AstrMessageEvent):
        if not self._ready:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        cmd = ScheduleCommands(
            self.storage, self.generator, self,
            self.dynamic_styles,
            self._get_persona_id,
            self._get_recent_messages
        )
        reply = await cmd.cmd_view_schedule(event.message_str, event)
        yield event.plain_result(reply)

    @filter.command("重写日程", desc="仅重新生成当前会话绑定人格的今日日程")
    async def rewrite_schedule(self, event: AstrMessageEvent):
        if not self._ready:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        cmd = ScheduleCommands(
            self.storage, self.generator, self,
            self.dynamic_styles,
            self._get_persona_id,
            self._get_recent_messages
        )
        reply = await cmd.cmd_regenerate(event.message_str, event)
        yield event.plain_result(reply)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重写所有人", desc="重新生成所有已知人格的今日日程（全量重写）")
    async def rewrite_all_schedule(self, event: AstrMessageEvent):
        if not self._ready:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        cmd = ScheduleCommands(
            self.storage, self.generator, self,
            self.dynamic_styles,
            self._get_persona_id,
            self._get_recent_messages
        )
        reply = await cmd.cmd_regenerate_all(event.message_str, event)
        yield event.plain_result(reply)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日程时间", desc="设置每日自动生成日程的时间")
    async def set_schedule_time(self, event: AstrMessageEvent):
        if not self._ready:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        cmd = ScheduleCommands(
            self.storage, self.generator, self,
            self.dynamic_styles,
            self._get_persona_id
        )
        reply = await cmd.cmd_set_time(event.message_str, event)
        self.scheduler.update_job()
        yield event.plain_result(reply)

    @filter.command("日程日志", desc="查看插件最近的运行日志和生成记录")
    async def show_log(self, event: AstrMessageEvent):
        if not self._ready:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        # 兜底：这条命令以前没有 try —— 内部一抛错（比如配置里塞了非字符串），
        # 消息就石沉大海、一点回执都没有，排查时只会看到「命令没反应」。
        try:
            cmd = DebugCommands(self)
            reply = await cmd.cmd_show_log(event.message_str, event)
        except Exception as e:
            logger.warning(f"[晨光心语] /日程日志 出错: {e}")
            yield event.plain_result(f"⚠️ 读取日志时出错：{type(e).__name__}: {e}")
            return
        yield event.plain_result(reply)

    @filter.command("晨光报告", desc="查看插件的整体运行状态和今日日程摘要")
    async def report(self, event: AstrMessageEvent):
        if not self._ready:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        try:
            cmd = DebugCommands(self)
            reply = await cmd.cmd_report(event.message_str, event)
        except Exception as e:
            logger.warning(f"[晨光心语] /晨光报告 出错: {e}")
            yield event.plain_result(f"⚠️ 生成报告时出错：{type(e).__name__}: {e}")
            return
        yield event.plain_result(reply)

    # ---------- 主动消息（P0：订阅 + 手动点火） ----------
    @filter.command_group("晨光", desc="主动消息：她自己也会想起你")
    def morning_group(self):
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @morning_group.command("订阅", desc="允许她主动来这个会话找你")
    async def proactive_subscribe(self, event: AstrMessageEvent):
        if not self._ready or self.subscriptions is None:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        persona_id = await self._get_persona_id(event)
        _, reply = await self.subscriptions.subscribe(event.unified_msg_origin, persona_id)
        if self.proactive is not None and not self.proactive.enabled():
            reply += "\n（提醒喵：主动消息的总开关还没开，开了她才会真的自己开口。）"
        yield event.plain_result(reply)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @morning_group.command("退订", desc="不再让她主动来这个会话")
    async def proactive_unsubscribe(self, event: AstrMessageEvent):
        if not self._ready or self.subscriptions is None:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        _, reply = await self.subscriptions.unsubscribe(event.unified_msg_origin)
        yield event.plain_result(reply)

    @morning_group.command("订阅列表", desc="看看哪些会话被订阅了")
    async def proactive_list(self, event: AstrMessageEvent):
        if not self._ready or self.subscriptions is None:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        rows = self.subscriptions.list_all()
        if not rows:
            yield event.plain_result("🔕 还没有任何会话被订阅喵。\n用 /晨光 订阅 把当前会话加进来～")
            return
        lines = ["🔔 已订阅的会话："]
        for r in rows[:20]:
            kind = "群聊" if r.get("chat_type") == "group" else "私聊"
            lines.append(f"- [{kind}] {r.get('persona_id') or '默认人格'} · {r.get('umo')}")
        lines.append("（私聊优先；群聊容易被别人看到，她会说得更含蓄）")
        yield event.plain_result("\n".join(lines))

    @morning_group.command("状态", desc="看看主动消息引擎此刻的状态（开关 / 订阅 / 今天发了几条）")
    async def proactive_status(self, event: AstrMessageEvent):
        if not self._ready or self.proactive is None:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        persona_id = await self._get_persona_id(event)
        eng = self.proactive
        lines = []
        lines.append("📣 主动消息：" + ("✅ 总开关已开" if eng.enabled() else "❌ 总开关没开（设置页「📣 主动消息」里打开）"))
        if self.subscriptions is not None:
            here = "✅ 本会话已订阅" if self.subscriptions.is_subscribed(
                event.unified_msg_origin) else "❌ 本会话还没订阅（发 /晨光 订阅）"
            lines.append("🔔 " + here)
            lines.append(f"📮 「{persona_id}」能主动发话的会话：{len(self.subscriptions.targets_for_persona(persona_id))} 个")
        try:
            info = eng.sleep_summary(persona_id)
            lines.append(
                f"🛏 作息：{info['bed']} 睡 → {info['wake']} 起"
                + ("（此刻她睡着了）" if info["sleeping_now"] else "（此刻她醒着）")
            )
            lag = info.get("wake_lag_minutes") or 0
            if lag > 0:
                lines.append(f"🛌 昨晚被打断，今天多睡 {lag} 分钟（睡懒觉中）")
            src = "手动指定" if info["quiet_manual"] else "跟随作息"
            lines.append(
                f"🌙 安静时段：{info['quiet_start']} ~ {info['quiet_end']}（{src}）"
                + ("" if info["quiet_manual"] else "——设置页那两格留空是正常的")
            )
        except Exception:
            pass
        if self.proactive_state is not None:
            try:
                st = await self.proactive_state.ensure(persona_id, self._get_today().isoformat())
                lines.append(
                    f"📊 今日已发：私聊 {st.get('daily_count_private', 0)} 条 / 群聊 {st.get('daily_count_group', 0)} 条"
                )
                # ⑧ 每根由头的「每天上限」用量（目前只有「旧事想起」配了）
                try:
                    _names = {"old_memory": "旧事想起"}
                    _used = st.get("trigger_daily_count") or {}
                    _parts = []
                    for _k in getattr(eng, "_DAILY_CAP_KEYS", ("old_memory",)):
                        _cap = eng._daily_cap(_k)
                        if _cap <= 0:
                            continue
                        _parts.append(f"{_names.get(_k, _k)} {int(_used.get(_k) or 0)}/{_cap}")
                    if _parts:
                        lines.append("🔥 每日上限：" + "；".join(_parts))
                except Exception:
                    pass
                unanswered = st.get("consecutive_unanswered", 0)
                paused = "（已自动闭嘴，等你开口）" if st.get("paused") else ""
                lines.append(f"🔇 连续没被回应：{unanswered} 次{paused}")
                # 睡醒重置（可选开关）：绿色说明今天这一格是「已清零」还是「还等着」
                try:
                    if eng.wake_reset_enabled():
                        _today = self._get_today().isoformat()
                        if str(st.get("unanswered_reset_date") or "") == _today:
                            lines.append("🌅 睡醒重置连败：今天已经清零过了")
                        else:
                            lines.append("🌅 睡醒重置连败：开着，今天睡醒后生效")
                    else:
                        lines.append("🌅 睡醒重置连败：关着（设置页可开）")
                except Exception:
                    pass
                if st.get("last_sent_at"):
                    lines.append(f"🕐 上次开口：{st.get('last_sent_at')}")
            except Exception:
                pass
        # E3：把她攒着想说的、和最近没说出口的摆出来（原来只闷在队列里，看不见）
        try:
            pend = eng.pending_preview(persona_id)
            if pend:
                lines.append(f"⏳ 攒着想说的：{len(pend)} 件（{'、'.join(pend[:3])}）")
            # N2 修复：条目字段是 detail/why（不是已发出历史的 text/note）；
            # 顺带限 48 小时，别把三天前憋的话摆成今早的
            sk = eng.state.recent_skipped(persona_id, limit=2, max_age_hours=48)
            sk_texts = [t for t in (format_skipped_line(h) for h in sk) if t]
            if sk_texts:
                lines.append("💭 最近想说没说出口的：" + "；".join(sk_texts))
        except Exception:
            pass
        # E5：中文信号命中计数（看看她到底听不听得见主人说的话）
        try:
            if self.life_bridge is not None:
                stats = self.life_bridge.zh_hit_stats(persona_id)
                if stats:
                    top = "、".join(f"{k}×{v}" for k, v in stats[:5])
                    lines.append(f"🈶 中文信号命中（本次运行累计）：{top}")
                else:
                    lines.append("🈶 中文信号命中：本次运行还没触发过")
        except Exception:
            pass
        lines.append("（想知道这一轮有没有导火索成立，发一次 /晨光 说句话 看回执的由头）")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @morning_group.command("说句话", desc="立刻让她发一条（通道自检，不受刹车限制）")
    async def proactive_say(self, event: AstrMessageEvent):
        if not self._ready or self.proactive is None:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        persona_id = await self._get_persona_id(event)
        _, reply = await self.proactive.manual_speak(persona_id, event.unified_msg_origin)
        yield event.plain_result(reply)

    @morning_group.command("帮助", desc="列出晨光心语的全部命令")
    async def morning_help(self, event: AstrMessageEvent):
        # E16：14+ 条命令原来只活在 README 里，群里没人发现得了
        lines = [
            "🌅 晨光心语 · 命令一览",
            "━━━━━━━━━━━━━━━━━━",
            "【日程】",
            "  /查看日程 — 看她今天的安排",
            "  /重写日程 — 重新生成当前人格今日日程",
            "  /重写所有人 — 重新生成所有人格今日日程",
            "  /日程时间 <HH:MM> — 设置每日自动生成时间",
            "  /日程日志 — 看最近的生成记录",
            "  /晨光报告 — 插件整体运行状态",
            "【内在状态】",
            "  /状态 — 她此刻的精力 / 心情 / 在做什么",
            "  /状态重置 — 把状态重置为初始值",
            "【主动消息】",
            "  /晨光 订阅 — 允许她主动来这个会话",
            "  /晨光 退订 — 不再让她主动来",
            "  /晨光 订阅列表 — 看看哪些会话被订阅了",
            "  /晨光 状态 — 主动消息引擎此刻的状态",
            "  /晨光 说句话 — 立刻让她发一条（通道自检）",
            "  /晨光 日报 — 一屏看昨天",
            "  /晨光 帮助 — 就是这条",
            "【风格】",
            "  /风格 绑定 — 为指定人格绑定穿搭/语气风格",
            "（带 < > 的是要填的参数；管理类命令只有管理员能用）",
        ]
        yield event.plain_result("\n".join(lines))

    @morning_group.command("日报", desc="一屏看昨天：日程 / 事件 / 心情")
    async def morning_dailyreport(self, event: AstrMessageEvent):
        # E17：一屏看昨天（顺带替代重载即丢的内存态 /日程日志）
        if not self._ready:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        persona_id = await self._get_persona_id(event)
        y = self._get_today() - datetime.timedelta(days=1)
        lines = [f"📰 {persona_id} 的昨日小报（{y.strftime('%m/%d')}）", "━━━━━━━━━━━━━━━━━━"]

        sched = None
        try:
            sched = await self.storage.load_schedule(persona_id, y)
        except Exception:
            sched = None
        if sched:
            lines.append("📅 昨天日程：✅ 已生成")
            intent = str(sched.get("intent") or "").strip()
            if intent:
                lines.append(f"  主线：{intent}")
            note = str(sched.get("note") or "").strip()
            if note:
                lines.append(f"  寄语：{note}")
            state = sched.get("state") or {}
            if state:
                lines.append("🫀 昨天状态：" + StateStore.summarize(state))
        else:
            lines.append("📅 昨天日程：❌ 没生成")

        try:
            if self.event_ledger is not None:
                evs = await self.event_ledger.load(persona_id)
                new_y = [e for e in evs if str(e.get("date")) == y.isoformat()]
                open_n = sum(1 for e in evs if e.get("status") == "open")
                closed_n = sum(1 for e in evs if e.get("status") == "closed")
                lines.append(f"📒 昨天新记：{len(new_y)} 件 · 至今没了结 {open_n} 件 · 已了结 {closed_n} 件")
                for e in new_y[:3]:
                    lines.append(f"  · {e.get('summary')}")
        except Exception:
            pass

        try:
            if self.proactive is not None:
                # N2 修复：同上 —— 读 detail/why，并限 48 小时
                sk = self.proactive.state.recent_skipped(persona_id, limit=3, max_age_hours=48)
                sk_texts = [t for t in (format_skipped_line(h) for h in sk) if t]
                if sk_texts:
                    lines.append("💭 最近想说没说出口的：" + "；".join(sk_texts))
        except Exception:
            pass

        yield event.plain_result("\n".join(lines))

    @filter.command_group("风格", desc="管理与日程生成相关的风格设定")
    def style_group(self):
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @style_group.command("绑定", desc="为指定人格绑定穿搭/语气风格描述")
    async def bind_style(self, event: AstrMessageEvent):
        if not self._ready:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        cmd = PersonaCommands(self.persona_map, self.dynamic_styles, self)
        reply = await cmd.cmd_bind_style(event.message_str, event)
        await self._save_dynamic_data()
        yield event.plain_result(reply)

    # priority=-1：AstrBot 的钩子按 priority **降序**跑。给个负数，保证自己排在记忆插件
    # 后面 —— 它们刚塞进 req 的记忆块，我们才看得见（从此不靠插件加载顺序碰运气）。
    @filter.on_llm_request(
        desc="在LLM请求前注入当前人格的今日日程（穿搭+活动安排）到本轮上下文",
        priority=-1,
    )
    async def inject_schedule(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._ready:
            return
        self._sync_runtime_config()
        persona_id = await self._get_persona_id(event)
        # 主人说话了 → 记一笔。她睡着时被吵醒的那段就不算睡，也不白攒回血，
        # 还会记进「今夜被打断」，明天拿来睡懒觉。（这一步不依赖今天的日程有没有生成）
        if self.life_bridge is not None:
            try:
                self.life_bridge.note_user_activity(persona_id)
            except Exception:
                pass
            # 中文关键词桥：底座那张表整张是英文的，中文环境一句都命中不了。
            # 这里补一层 —— 主人夸她、逗她笑、说累，状态机才真的收得到。
            try:
                if self.life_bridge.zh_triggers_enabled():
                    self._keep_task(asyncio.create_task(
                        self.life_bridge.apply_zh_triggers(persona_id, event.message_str)
                    ))
            except Exception:
                pass

        # 主人开口了 → 解除「连续未回应暂停」（只动主动消息那本账，不改变任何回复行为）
        if self.proactive is not None and self.proactive.enabled():
            try:
                self._keep_task(asyncio.create_task(
                    self.proactive.note_user_activity(persona_id, event.unified_msg_origin)
                ))
            except Exception:
                pass
        # V9 修复：正常对话路径也刷新聊天缓存。原来只有「新人格首日 / 全量重写」
        # 才会写缓存，于是定时生成时的 recent_messages 基本永远是空的。
        # 每人格最多 2 分钟刷一次，避免每句都读历史 + 写盘。
        if self.chat_cache is not None:
            try:
                bucket = getattr(self, "_chat_cache_at", None)
                if bucket is None:
                    bucket = self._chat_cache_at = {}
                now_ts = datetime.datetime.now().timestamp()
                if now_ts - bucket.get(persona_id, 0.0) >= 120.0:
                    bucket[persona_id] = now_ts
                    fresh_msgs = await self._get_recent_messages(event)
                    if fresh_msgs:
                        self.chat_cache.update(persona_id, fresh_msgs)
                        await self.chat_cache.save()
            except Exception:
                pass

        today = self._get_today()
        schedule = await self.storage.load_schedule(persona_id, today)
        if not schedule:
            yesterday = today - datetime.timedelta(days=1)
            yesterday_schedule = await self.storage.load_schedule(persona_id, yesterday)
            if not yesterday_schedule:
                try:
                    recent = await self._get_recent_messages(event)
                    if self.chat_cache:
                        self.chat_cache.update(persona_id, recent)
                        await self.chat_cache.save()
                    schedule = await self.generator.generate(persona_id, self.dynamic_styles, recent_messages=recent)
                except Exception as e:
                    logger.error(f"注入时生成日程失败: {e}")
            else:
                logger.info(f"[晨光心语] ⏰ {persona_id} 注入时今日日程尚未生成（昨日日程存在），等待定时任务触发")
        # 记忆搭桥（Phase 2 · R9）：把这一轮被注入的记忆攒进本地池（只读，绝不回写）
        if self.memory_bridge is not None and self.get_config("bridge.enabled", False):
            try:
                texts = _collect_memory_texts(req)
                got = 0
                for t in texts:
                    got += await self.memory_bridge.capture(persona_id, t)
                if not got:
                    # 万一这轮还是跑在记忆插件前面——拿上一轮看到的整包兜个底
                    for t in (self._last_hook_texts.get(persona_id) or []):
                        await self.memory_bridge.capture(persona_id, t)
                self._last_hook_texts[persona_id] = texts
            except Exception:
                pass

        if schedule:
            # 内在状态切片（Phase 1）：让注入带上「此刻」，而不只是「今天」
            life_state = None
            if self.life_bridge is not None and self.get_config("life_engine.enabled", False):
                try:
                    life_state = await self.life_bridge.get_state(persona_id)
                except Exception:
                    life_state = None
                # 顺手把这轮对话喂给状态机——它不存原话，只累积「关系的厚度」
                try:
                    msg = (event.message_str or "").strip()
                    if msg:
                        self._keep_task(asyncio.create_task(
                            self.life_bridge.on_user_message(persona_id, msg[:200])
                        ))
                except Exception:
                    pass

            # 主动分享钩子（R4）：她今天有没有想主动跟你说的事
            share_lines = []
            if self.life_bridge is not None and self.get_config("life_engine.enabled", False):
                try:
                    shares = await self.life_bridge.shareable(persona_id, limit=2)
                    share_lines = [s.get("content") for s in shares if s.get("content")]
                    # 交到她手上之后就标成「已分享」，免得每一轮都塞同一件事
                    for s in shares:
                        if s.get("id") is not None:
                            self._keep_task(asyncio.create_task(
                                self.life_bridge.mark_shared(persona_id, s["id"])
                            ))
                except Exception:
                    share_lines = []

            injection = self._format_for_injection(schedule, life_state=life_state)
            if share_lines:
                injection += ("\n（她今天有件想跟你说的事，合适的时候可以自然提起："
                              + "；".join(share_lines) + "）")
            self._append_dynamic_context(req, injection)

    # 六个模糊时段 → 大致钟点（和天气、状态机用的是同一套）
    _PERIOD_ORDER = ["清晨", "上午", "午间", "下午", "傍晚", "晚间"]

    # 动态上下文的固定开头：既当给模型看的标签，也当「这轮塞过了」的幂等标记
    _DYN_CTX_MARK = "<daily_context>"

    def _append_dynamic_context(self, req, text: str) -> None:
        """把每轮都会变的动态内容挂到「本轮用户消息末尾」，不碰 system_prompt。

        为什么不能写 system_prompt（v1.5.27，上架审核）：
        服务端提示词缓存是按**前缀**匹配的，系统提示词每轮一变，整段历史缓存就全废了。
        官方插件文档写明这类写法会让成本涨 7-20 倍。

        为什么用 mark_as_temp：标记后这段只在本次请求里给模型看，**不写进会话历史**——
        所以下一轮请求的前缀与「完全不做注入」时完全一致，注入本身不额外损失缓存。
        实测（AstrBot 4.29.3）：写历史时按 part 的 `_no_save` 剔除，用户原话照常保留。
        """
        text = (text or "").strip()
        if not text:
            return

        block = f"{self._DYN_CTX_MARK}\n{text}\n</daily_context>"

        parts = getattr(req, "extra_user_content_parts", None)
        if parts is None:
            # 极老版本没有这个字段：宁可退化回 system_prompt，也不要抛异常让整轮对话挂掉
            req.system_prompt = ((req.system_prompt or "") + "\n" + text).strip()
            return

        # 幂等：同一轮里钩子若被触发两次，别叠第二份
        for p in parts:
            body = p.get("text") if isinstance(p, dict) else getattr(p, "text", "")
            if isinstance(body, str) and body.lstrip().startswith(self._DYN_CTX_MARK):
                return

        parts.append(TextPart(text=block).mark_as_temp())

    @staticmethod
    def _period_index_of(hour: int) -> int:
        """把 0~23 点钟映射到六个时段的第几段。

        实现在 core/view_data.py（插件页面的「此刻高亮」用的是同一个函数），
        这里只是保留老调用点。
        """
        return period_index_of(hour)

    @staticmethod
    def _activity_text(item: dict) -> str:
        """这一格的正文。

        H1 窄兼容：v1.5.29 之前生成的日程，正文开头可能还带着「晚间 · 」「晚间烟霾…」
        这种自报时段的写法，渲染时再拼一次时段就重复了。提示词那边已经治本，
        这里只管升级前落盘的老文件（见 core/schedule_text.py）。
        """
        if not isinstance(item, dict):
            return ""
        return strip_legacy_period_prefix(
            str(item.get("activity") or ""), str(item.get("time") or "")
        )

    def _format_for_injection(self, schedule: dict, life_state: dict = None) -> str:
        """把日程整理成一段上下文，交给本轮对话（不写 system_prompt）。

        - `inject.realtime_slice` 开着：按当前时刻切三刀（今天早些时候 / 此刻 / 接下来）。
          真人不会把已经做过的事当成还没发生——这是「像真实生活」最容易露馅的地方。
        - 关着：退回原来的整日平铺格式。
        """
        if not self.get_config("inject.realtime_slice", True):
            lines = ["[今日日程]"]
            lines.append(f"穿搭：{schedule.get('outfit', '')}")
            for item in schedule.get("schedule", []):
                lines.append(f"- {item.get('time')}: {self._activity_text(item)}")
            note = schedule.get("note", "").strip()
            if note:
                lines.append(f"寄语：{note}")
            return "\n".join(lines)

        try:
            now_hour = self._get_now().hour
        except Exception:
            now_hour = datetime.datetime.now().hour
        cur = self._period_index_of(now_hour)

        past, here, future = [], None, []
        for i, item in enumerate(schedule.get("schedule", []) or []):
            label = item.get("time", "")
            idx = self._PERIOD_ORDER.index(label) if label in self._PERIOD_ORDER else min(i, 5)
            if idx < cur:
                past.append(item)
            elif idx == cur:
                here = item
            else:
                future.append(item)

        lines = ["[今日日程 · 此刻]"]
        lines.append(f"穿搭：{schedule.get('outfit', '')}")
        if past:
            lines.append("（今天早些时候，已经做过）")
            for item in past:
                lines.append(f"- {item.get('time')}: {self._activity_text(item)}")
        lines.append("（此刻）")
        # 优先级对调：状态机才是「真·此刻」，日程那条降级成「原本的打算」。
        # 日程是早上一次写死的，状态机每小时都在动；不这样的话会出现
        # 「日程写着在外面玩、人其实早窝回家里了」这种对不上。
        cur_act = activity_zh((life_state or {}).get("current_activity", ""))
        if cur_act:
            if here:
                # 两行平视：状态机给的是「现在」，日程正文是「今天排的」——
                # 别再把后者塞进括弧当注释（v1.5.27 按实测反馈拆平）。
                lines.append(f"- 现在：{cur_act}")
                lines.append(
                    f"- 今天排的是：{here.get('time', '')} · {self._activity_text(here)}"
                )
            else:
                lines.append(f"- 现在：{cur_act}")
        elif here:
            lines.append(f"- {here.get('time')}: {self._activity_text(here)}")
        if life_state:
            lines.append(f"- 精力 {life_state.get('energy')}，心情 {mood_zh(life_state.get('mood', ''))}")
        if future:
            lines.append("（接下来）")
            for item in future:
                lines.append(f"- {item.get('time')}: {self._activity_text(item)}")
        note = schedule.get("note", "").strip()
        if note:
            lines.append(f"寄语：{note}")
        return "\n".join(lines)

    async def terminate(self):
        # 整段包 try：scheduler 没启动过时 shutdown() 会抛 SchedulerNotRunningError，
        # 而它排在所有清理之前 —— 一抛，后面的 cancel / close 全被跳过。
        try:
            if self.scheduler:
                self.scheduler.shutdown()
        except Exception as e:
            logger.debug(f"[晨光心语] 调度器关闭跳过: {e}")
        for task in (self._life_tick_task, self._delayed_task):
            if task is not None and not task.done():
                task.cancel()
        # ★ 重载安全：proactive 自己的后台任务也要取消 —— 它们不归上面那两个 task 管。
        #   「开口前随机等一会儿」的延迟发送（_bg_tasks）和聊天刹车的闹钟（也在 _bg_tasks）
        #   如果留着，重载后旧实例还会继续跑：用**旧内存**判刹车（判不准、该拦的放行），
        #   发完消息记的账还会被新实例从磁盘加载的旧快照盖回去。
        #   （实测 2026-10-12 20:01 就是这么丢的账：旧实例的延迟任务发了消息、账却没留下。）
        if self.proactive is not None:
            for t in list(getattr(self.proactive, "_bg_tasks", ()) or ()):
                if not t.done():
                    t.cancel()
        if self.life_bridge is not None:
            try:
                await self.life_bridge.close_all()
            except Exception:
                pass
        # F4：天气那条共用的 AsyncClient 也要关，免得重载时留一条没关的连接
        try:
            await aclose_client()
        except Exception:
            pass
        logger.info("✅ 晨光心语插件已停止")
