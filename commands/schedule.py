import asyncio
import datetime
import re

from ..core.generator import ScheduleGenerator
from ..core.life_bridge import location_zh
from ..core.storage import ScheduleStorage


# 「家」是个容器：阳台 / 厨房 / 卧室这些都装在它里面。
# 所以一段里如果同时列了「家」和屋里的细地点，就把那个笼统的「家」省掉。
# 但「公园/家」这种要留着 —— 那是真的跑了两个地方。
_HOME_SUB = {"balcony", "kitchen", "bedroom", "living room", "study nook", "garden"}


class ScheduleCommands:
    def __init__(self, storage: ScheduleStorage, generator: ScheduleGenerator, plugin,
                 dynamic_styles: dict, get_persona_id_func, get_recent_messages_func=None):
        self.storage = storage
        self.generator = generator
        self.plugin = plugin
        self.dynamic_styles = dynamic_styles
        self.get_persona_id = get_persona_id_func
        self.get_recent_messages = get_recent_messages_func

    def _get_today(self) -> datetime.date:
        """按插件配置的时区取“今天”，别再直接用系统时区了"""
        try:
            return self.plugin._get_today()
        except Exception:
            return datetime.date.today()

    async def cmd_view_schedule(self, message, event):
        persona_id = await self.get_persona_id(event)
        today = self._get_today()
        schedule = await self.storage.load_schedule(persona_id, today)
        if not schedule:
            # 检查昨天日程是否存在，区分新老用户
            yesterday = today - datetime.timedelta(days=1)
            yesterday_schedule = await self.storage.load_schedule(persona_id, yesterday)
            if yesterday_schedule:
                # 老用户：展示昨天的日程，提示今天日程将在08:00生成
                yesterday_text = self._format_schedule(yesterday_schedule)
                sched_time = self.plugin.get_config("schedule.schedule_time", "08:00")
                # F5(b)：跟 _get_today() 一样走**配置时区**，别再拿系统时区比
                #（容器 TZ 与 schedule.timezone 不一致时，会给错「今天生成了没」的指引）。
                try:
                    now = self.plugin._get_now()
                except Exception:
                    now = datetime.datetime.now()
                try:
                    hh, mm = (int(x) for x in str(sched_time).split(":"))
                    missed = (now.hour, now.minute) >= (hh, mm)
                except Exception:
                    missed = False
                if missed:
                    # 已经过了生成时间却还没有今天的日程 —— 说明生成那一跳被打断了
                    # （重启 / 报错）。这时候回「将在 08:00 生成」是错误指引，那天已过。
                    head = (f"今天的日程还没生成喵……{sched_time} 那一跳好像错过了，"
                            f"小白下一次心跳就把它补上～先看看昨天的吧：")
                else:
                    head = f"今天的日程将在{sched_time}生成，先看看昨天的日程吧喵～"
                return (f"{head}"
                        f"\n\n"
                        f"【昨天 {yesterday.strftime('%m/%d')} 的日程】"
                        f"\n"
                        f"{yesterday_text}")
            # 新用户：自动生成首日日程
            recent = await self.get_recent_messages(event) if self.get_recent_messages else None
            schedule = await self.generator.generate(persona_id, self.dynamic_styles, recent_messages=recent)
        if schedule:
            return self._format_schedule(schedule)
        return "今天还没有日程安排哦。"

    async def cmd_regenerate(self, message, event):
        """只重写当前会话绑定的人格"""
        current_persona = await self.get_persona_id(event)
        recent = await self.get_recent_messages(event) if self.get_recent_messages else None

        schedule = await self.generator.generate(current_persona, self.dynamic_styles, recent_messages=recent, force=True)
        if schedule and not isinstance(schedule, Exception):
            return f"已重新生成【{current_persona}】的日程。\n{self._format_schedule(schedule)}"
        else:
            return f"重写【{current_persona}】日程失败，请稍后重试。"

    async def cmd_regenerate_all(self, message, event):
        """全量重写所有人格的日程"""
        current_persona = await self.get_persona_id(event)
        # 只重写当前仍然活跃的人格，别给已经关掉的人格白烧 token
        try:
            all_personas = await self.plugin._get_active_persona_ids()
        except Exception:
            all_personas = self.plugin.context_builder.get_known_persona_ids()
        if not all_personas:
            all_personas = [current_persona]

        recent = await self.get_recent_messages(event) if self.get_recent_messages else None

        # 缓存聊天记录，方便定时生成时参考
        if recent and self.plugin.chat_cache:
            self.plugin.chat_cache.update(current_persona, recent)
            await self.plugin.chat_cache.save()

        # 并发生成所有人格的日程
        tasks = []
        for pid in all_personas:
            tasks.append(self.generator.generate(pid, self.dynamic_styles, recent_messages=recent, force=True))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 优先获取当前人格的结果
        current_schedule = None
        for pid, result in zip(all_personas, results):
            if pid == current_persona and not isinstance(result, Exception) and result:
                current_schedule = result
                break

        # 如果当前人格（如 default）没生成，用第一个有效结果
        if not current_schedule:
            for result in results:
                if not isinstance(result, Exception) and result:
                    current_schedule = result
                    break

        if current_schedule:
            return f"已批量重新生成 {len(all_personas)} 个人格的日程。\n{self._format_schedule(current_schedule)}"
        else:
            return "日程重写失败：所有人格生成均未返回有效结果。"

    async def cmd_set_time(self, message, event):
        parts = message.split()
        if len(parts) < 2:
            return "请提供时间，例如 /日程时间 08:30"
        time_str = parts[1]
        try:
            datetime.datetime.strptime(time_str, "%H:%M")
            await self.plugin.set_config("schedule.schedule_time", time_str)
            return f"每日生成时间已设置为 {time_str}，已即时生效。"
        except ValueError:
            return "时间格式错误，请使用 HH:MM。"

    def _get_time_emoji(self, time_str: str) -> str:
        time_lower = time_str.lower()
        emoji_map = {
            "清晨": "🌄", "早晨": "🌅", "早上": "☀️",
            "上午": "📌", "午前": "📌",
            "中午": "🍽️", "午间": "🍽️", "午餐": "🍽️", "午后": "🍽️",
            "下午": "🌤️",
            "傍晚": "🌅", "黄昏": "🌇",
            "晚上": "🌙", "晚间": "🌙", "深夜": "🌃",
            "夜间": "🌃", "睡前": "🛌",
        }
        for keyword, emoji in emoji_map.items():
            if keyword in time_lower:
                return emoji
        return "⏰"

    @staticmethod
    def _place_text(item: dict) -> str:
        """这一段的地点文字（给 /查看日程 用）。

        优先取 `beats` 里的细地点（阳台 / 厨房 / 客厅这种，去重、按出现顺序），
        beats 里没有才退回 `location`（家 / 图书馆）。
        一个时段跑两个地方是常态（「上午：咖啡馆 → 图书馆」），所以用 `/` 串起来：
        `阳台/厨房`、`咖啡馆/街上`。词表外的地方原样保留英文，不乱翻。
        """
        places: list[str] = []
        for b in (item.get("beats") or []):
            if not isinstance(b, dict):
                continue
            at = str(b.get("at") or "").strip()
            if at and at not in places:
                places.append(at)
        if not places:
            loc = str(item.get("location") or "").strip()
            if loc:
                places.append(loc)
        # 「家 + 屋里的细地点」= 重复，省掉笼统的那个（「公园/家」这种照留）
        if len(places) > 1 and "home" in places and all(
            p in _HOME_SUB or p == "home" for p in places
        ):
            places.remove("home")
        return "/".join(location_zh(p) for p in places)

    def _format_schedule(self, data: dict) -> str:
        lines = []
        lines.append("🌅 今日日程")
        lines.append("")
        outfit = str(data.get("outfit") or "未知").strip()   # F5：键在、值为 null 时别崩
        lines.append(f"👗 OOTD：{outfit}")
        lines.append("")
        lines.append("📋 日程安排：")
        schedule = data.get("schedule", [])
        if isinstance(schedule, list):
            for item in schedule:
                if isinstance(item, dict):
                    time = item.get("time", "")
                    activity = item.get("activity", "")
                    emoji = self._get_time_emoji(time)
                    place = self._place_text(item)
                    head = f"{emoji} {time}" + (f" · {place}" if place else "")
                    lines.append(f"{head} - {activity}")
                else:
                    lines.append(f"⏰ {item}")
        elif isinstance(schedule, str):
            for line in schedule.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                emoji = self._get_time_emoji(line)
                lines.append(f"{emoji} {line}")
        note = str(data.get("note") or "").strip()   # F5：键在、值为 null 时别崩
        if note:
            lines.append("")
            lines.append(f"💬 今日寄语：{note}")
        return "\n".join(lines)


class PersonaCommands:
    def __init__(self, persona_map: dict, dynamic_styles: dict, plugin):
        self.persona_map = persona_map
        self.dynamic_styles = dynamic_styles
        self.plugin = plugin

    async def cmd_bind_style(self, message, event):
        cmd_text = message.strip()
        if not cmd_text.startswith("/"):
            cmd_text = "/" + cmd_text
        match = re.match(r"/风格\s+绑定\s+([\w\u4e00-\u9fff\-]+)\s+(.+)", cmd_text)
        if not match:
            return "格式：/风格 绑定 <人格名> <风格描述>"
        persona_name = match.group(1).strip()
        style_text = match.group(2).strip()
        if not persona_name or not style_text:
            return "人格名和风格描述都不能为空"

        self.dynamic_styles[persona_name] = style_text
        # F5(c)：只有真匹配到 1~3 号静态槽位、写进配置，才算「已绑定」。
        # 以前不管写没写进去都回「已绑定」——用户回设置页一看还是旧值，以为没生效。
        persisted = False
        for i in range(1, 4):
            pname_key = f"persona_styles.persona_name_{i}"
            style_key = f"persona_styles.style_control_{i}"
            existing_name = str(self.plugin.get_config(pname_key, "") or "").strip()
            if existing_name == persona_name:
                await self.plugin.set_config(style_key, style_text)
                persisted = True
                break

        if persisted:
            return f"已为【{persona_name}】绑定风格：{style_text}"
        return (f"已为【{persona_name}】设置风格：{style_text}"
                f"（本次运行内生效；没匹配到设置页的 1~3 号静态人格槽位，未写入配置）")
