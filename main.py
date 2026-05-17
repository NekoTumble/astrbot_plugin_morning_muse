import json
import datetime
import asyncio
from pathlib import Path
from typing import Dict, List, Optional

import aiofiles
import holidays

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest

from .core.storage import ScheduleStorage
from .core.context_builder import ContextBuilder
from .core.generator import ScheduleGenerator
from .core.scheduler import MorningScheduler
from .commands.schedule import ScheduleCommands, PersonaCommands
from .commands.debug import DebugCommands


class ChatHistoryCache:
    """按人格ID缓存聊天记录，带持久化存储。优先读缓存 -> 再读持久化 -> 没有再跳过。"""
    
    def __init__(self, data_dir: Path, max_per_persona: int = 5):
        self.max_per_persona = max_per_persona
        self._cache: Dict[str, List[str]] = {}  # {persona_id: [msg1, msg2, ...]}
        self._file = data_dir / "chat_history_cache.json"
    
    async def load(self):
        """从持久化文件加载缓存"""
        if self._file.exists():
            try:
                async with aiofiles.open(self._file, 'r', encoding='utf-8') as f:
                    content = await f.read()
                data = json.loads(content)
                self._cache = data.get("cache", {})
                logger.info(f"[晨光心语] 聊天缓存已从持久化加载: {len(self._cache)} 个人格")
            except Exception as e:
                logger.warning(f"[晨光心语] 加载聊天缓存持久化失败: {e}")
                self._cache = {}
    
    async def save(self):
        """持久化缓存到文件"""
        try:
            async with aiofiles.open(self._file, 'w', encoding='utf-8') as f:
                await f.write(json.dumps({"cache": self._cache}, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.warning(f"[晨光心语] 保存聊天缓存持久化失败: {e}")
    
    def update(self, persona_id: str, messages: List[str]):
        """更新某人格的聊天缓存（保留最新N条）"""
        if not messages:
            return
        # 如果已有该人格的消息，追加新消息后取最后N条
        existing = self._cache.get(persona_id, [])
        combined = existing + messages
        self._cache[persona_id] = combined[-self.max_per_persona:]
    
    def get(self, persona_id: str) -> List[str]:
        """获取某人格的聊天缓存。优先读内存 -> 没有返回空。"""
        return self._cache.get(persona_id, [])

class MorningMusePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        self._config = config
        
        self.storage: Optional[ScheduleStorage] = None
        self.generator: Optional[ScheduleGenerator] = None
        self.scheduler: Optional[MorningScheduler] = None
        self.dynamic_styles: Dict[str, str] = {}
        self.persona_map: Dict[str, str] = {}
        self.debug_logs: List[Dict] = []
        self._ready = False
        self.chat_cache: Optional[ChatHistoryCache] = None
        
        asyncio.ensure_future(self._async_init())

    async def _async_init(self):
        try:
            data_dir = Path("data/plugin_data/astrbot_plugin_morning_muse")
            data_dir.mkdir(parents=True, exist_ok=True)
            logger.info("✅ 数据目录: " + str(data_dir.absolute()))
            
            schedules_dir = data_dir / "schedules"
            self.storage = ScheduleStorage(schedules_dir)

            self.context_builder = ContextBuilder(self, self.storage, holidays)

            self.generator = ScheduleGenerator(
                self,
                self.storage,
                self.context_builder,
                context=self.context,
                debug_logs=self.debug_logs
            )

            self.scheduler = MorningScheduler(self.generator, self)
            self.scheduler.start()

            await self._load_dynamic_data()
            
            # 初始化聊天记录缓存（带持久化）
            self.chat_cache = ChatHistoryCache(data_dir)
            await self.chat_cache.load()
            
            # 延迟后台补齐所有已知人格的今日日程
            asyncio.create_task(self._delayed_generate_all())
            
            self._ready = True
            logger.info("✅ 晨光心语插件已初始化")
        except Exception as e:
            logger.error(f"❌ 插件初始化失败: {e}", exc_info=True)

    async def _delayed_generate_all(self):
        """延迟 10 秒后，检查并补齐所有已知人格的今日日程"""
        await asyncio.sleep(10)  # 避开启动高峰
        
        try:
            # --- 以设置页设定的时间为"一天的起点" ---
            schedule_time = self.get_config("schedule.schedule_time", "08:00")
            try:
                hour, minute = map(int, schedule_time.split(":"))
                now = datetime.datetime.now()
                day_start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if now < day_start:
                    logger.info(f"[晨光心语] ⏰ 当前时间 {now.strftime('%H:%M')} 未到每日起点 {schedule_time}，跳过后台补齐，等待定时任务触发")
                    return
            except Exception:
                pass  # 配置解析失败时正常补齐，不阻塞
            
            all_personas = await self._get_active_persona_ids()
            if not all_personas:
                return
            
            today = datetime.date.today()
            tasks = []
            for pid in all_personas:
                existing = await self.storage.load_schedule(pid, today)
                if existing:
                    # 检查日程文件生成时间
                    file_path = self.storage._get_path(pid, today)
                    if file_path.exists():
                        mtime = file_path.stat().st_mtime
                        file_time = datetime.datetime.fromtimestamp(mtime)
                        if file_time < day_start:
                            # 日程生成时间早于每日起点，这是旧日程，重新生成
                            logger.info(f"[晨光心语] 🔄 {pid} 日程生成于 {file_time.strftime('%H:%M:%S')}，早于每日起点 {schedule_time}，重新生成")
                            cached = self.chat_cache.get(pid) if self.chat_cache else []
                            tasks.append(self.generator.generate(pid, self.dynamic_styles, recent_messages=cached, force=True))
                        else:
                            logger.info(f"[晨光心语] 📅 {pid} 今日日程已存在（{file_time.strftime('%H:%M:%S')} 生成），跳过")
                    else:
                        logger.info(f"[晨光心语] 📅 {pid} 今日日程已存在，跳过")
                else:
                    logger.info(f"[晨光心语] 🆕 后台补齐 {pid} 的今日日程...")
                    cached = self.chat_cache.get(pid) if self.chat_cache else []
                    tasks.append(self.generator.generate(pid, self.dynamic_styles, recent_messages=cached, force=False))
            
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                success = sum(1 for r in results if r and not isinstance(r, Exception))
                logger.info(f"[晨光心语] ✅ 后台补齐完成：{success}/{len(tasks)} 成功")
            else:
                logger.info("[晨光心语] ✅ 所有已知人格今日日程已就绪")
        except Exception as e:
            logger.error(f"[晨光心语] ❌ 后台补齐失败: {e}")

    async def _load_dynamic_data(self):
        dynamic_path = Path("data/plugin_data/astrbot_plugin_morning_muse") / "dynamic_data.json"
        if dynamic_path.exists():
            try:
                async with aiofiles.open(dynamic_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                data = json.loads(content)
                self.dynamic_styles = data.get("styles", {})
                self.persona_map = data.get("personas", {})
            except Exception:
                pass

    async def _save_dynamic_data(self):
        dynamic_path = Path("data/plugin_data/astrbot_plugin_morning_muse") / "dynamic_data.json"
        try:
            async with aiofiles.open(dynamic_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps({
                    "styles": self.dynamic_styles,
                    "personas": self.persona_map
                }, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"保存动态数据失败: {e}")

    async def _get_active_persona_ids(self) -> List[str]:
        """获取插件已知且仍在AstrBot中活跃的人格ID列表，过滤掉已关闭的人格"""
        known = self.context_builder.get_known_persona_ids()
        try:
            persona_mgr = self.context.persona_manager
            if persona_mgr:
                all_personas = await persona_mgr.get_all_personas()
                active_ids = {p.persona_id for p in all_personas}
                # 取交集，保留活跃人格 + "default"兜底
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
        """获取当前会话绑定的人格ID。优先查询AstrBot原生会话绑定。"""
        # 先查 AstrBot 原生会话绑定的人格
        try:
            umo = event.unified_msg_origin
            curr_id = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if curr_id:
                conv = await self.context.conversation_manager.get_conversation(umo, curr_id)
                if conv and conv.persona_id:
                    logger.info(f"[晨光心语] 从AstrBot会话系统获取到人格: {conv.persona_id}")
                    return conv.persona_id
        except Exception as e:
            logger.warning(f"[晨光心语] 查询会话人格异常，回退persona_map: {e}")
        
        # 兜底：查插件自己的persona_map
        pid = self.persona_map.get(event.message_obj.session_id, "default")
        if pid != "default":
            logger.info(f"[晨光心语] 从persona_map获取到人格: {pid}")
        return pid

    async def _get_recent_messages(self, event: AstrMessageEvent, limit: int = 5) -> List[str]:
        try:
            messages = await self.context.get_recent_messages(event.message_obj.session_id, limit)
            if messages:
                return [msg.message_str for msg in messages]
        except Exception:
            pass
        return []

    # ---------- 配置访问（支持嵌套键）----------
    def get_config(self, key: str, default=None):
        """支持点号分隔的嵌套键，例如 'persona_styles.persona_name_1'"""
        # 先尝试直接取
        if key in self._config:
            return self._config[key]
        # 尝试嵌套路径
        keys = key.split('.')
        node = self._config
        try:
            for k in keys:
                if isinstance(node, dict):
                    node = node[k]
                else:
                    return default
            return node
        except (KeyError, TypeError):
            return default

    async def set_config(self, key: str, value):
        self._config[key] = value
        await self.save_config()

    def get_style_for_persona(self, persona_name: str) -> str:
        if persona_name in self.dynamic_styles:
            return self.dynamic_styles[persona_name]
        for i in range(1, 4):
            pname = self.get_config(f"persona_styles.persona_name_{i}", "").strip()
            style = self.get_config(f"persona_styles.style_control_{i}", "").strip()
            if pname and pname == persona_name:
                return style
        return self.get_config("persona_styles.style_control_default", "自然亲切的语气")

    # ---------- 命令 ----------
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
        cmd = DebugCommands(self)
        reply = await cmd.cmd_show_log(event.message_str, event)
        yield event.plain_result(reply)

    @filter.command("晨光报告", desc="查看插件的整体运行状态和今日日程摘要")
    async def report(self, event: AstrMessageEvent):
        if not self._ready:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        cmd = DebugCommands(self)
        reply = await cmd.cmd_report(event.message_str, event)
        yield event.plain_result(reply)

    @filter.command_group("风格", desc="管理与日程生成相关的风格设定")
    def style_group(self):
        pass

    @style_group.command("绑定", desc="为指定人格绑定穿搭/语气风格描述")
    async def bind_style(self, event: AstrMessageEvent):
        if not self._ready:
            yield event.plain_result("✅ 插件仍在初始化，请稍后重试～")
            return
        cmd = PersonaCommands(self.persona_map, self.dynamic_styles, self)
        reply = await cmd.cmd_bind_style(event.message_str, event)
        await self._save_dynamic_data()
        yield event.plain_result(reply)

    @filter.on_llm_request(desc="在LLM请求前注入当前人格的今日AI日程（穿搭+活动安排）到系统提示词")
    async def inject_schedule(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._ready:
            return
        persona_id = await self._get_persona_id(event)
        today = datetime.date.today()
        schedule = await self.storage.load_schedule(persona_id, today)
        if not schedule:
            try:
                recent = await self._get_recent_messages(event)
                # 更新该人格的聊天记录缓存
                if self.chat_cache:
                    self.chat_cache.update(persona_id, recent)
                    await self.chat_cache.save()
                schedule = await self.generator.generate(persona_id, self.dynamic_styles, recent_messages=recent)
            except Exception as e:
                logger.error(f"注入时生成日程失败: {e}")
        if schedule:
            injection = self._format_for_injection(schedule)
            if req.system_prompt:
                req.system_prompt += f"\n{injection}"
            else:
                req.system_prompt = injection

    def _format_for_injection(self, schedule: dict) -> str:
        lines = ["[今日AI日程]"]
        lines.append(f"穿搭：{schedule.get('outfit', '')}")
        for item in schedule.get("schedule", []):
            lines.append(f"- {item.get('time')}: {item.get('activity')}")
        return "\n".join(lines)

    async def terminate(self):
        if self.scheduler:
            self.scheduler.shutdown()
        logger.info("✅ 晨光心语插件已停止")