import datetime
import re
import asyncio
from ..core.storage import ScheduleStorage
from ..core.generator import ScheduleGenerator

class ScheduleCommands:
    def __init__(self, storage: ScheduleStorage, generator: ScheduleGenerator, plugin,
                 dynamic_styles: dict, get_persona_id_func, get_recent_messages_func=None):
        self.storage = storage
        self.generator = generator
        self.plugin = plugin
        self.dynamic_styles = dynamic_styles
        self.get_persona_id = get_persona_id_func
        self.get_recent_messages = get_recent_messages_func

    async def cmd_view_schedule(self, message, event):
        persona_id = await self.get_persona_id(event)
        today = datetime.date.today()
        schedule = await self.storage.load_schedule(persona_id, today)
        if not schedule:
            # 检查昨天日程是否存在，区分新老用户
            yesterday = today - datetime.timedelta(days=1)
            yesterday_schedule = await self.storage.load_schedule(persona_id, yesterday)
            if yesterday_schedule:
                # 老用户：展示昨天的日程，提示今天日程将在08:00生成
                yesterday_text = self._format_schedule(yesterday_schedule)
                sched_time = self.plugin.get_config("schedule.schedule_time", "08:00")
                return (f"今天的日程将在{sched_time}生成，先看看昨天的日程吧喵～"
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
        all_personas = self.plugin.context_builder.get_known_persona_ids()
        if not all_personas:
            all_personas = [current_persona]

        recent = await self.get_recent_messages(event) if self.get_recent_messages else None

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
            return f"每日生成时间已设置为 {time_str}，将在下次调度生效。"
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

    def _format_schedule(self, data: dict) -> str:
        lines = []
        lines.append("🌅 今日日程")
        lines.append("")
        outfit = data.get('outfit', '未知')
        lines.append(f"👗 OOTD：{outfit}")
        lines.append("")
        lines.append("📋 日程安排：")
        schedule = data.get("schedule", [])
        if isinstance(schedule, list):
            for item in schedule:
                if isinstance(item, dict):
                    time = item.get('time', '')
                    activity = item.get('activity', '')
                    emoji = self._get_time_emoji(time)
                    lines.append(f"{emoji} {time} - {activity}")
                else:
                    lines.append(f"⏰ {item}")
        elif isinstance(schedule, str):
            for line in schedule.strip().split('\n'):
                line = line.strip()
                if not line:
                    continue
                emoji = self._get_time_emoji(line)
                lines.append(f"{emoji} {line}")
        note = data.get("note", "").strip()
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
        if not cmd_text.startswith('/'):
            cmd_text = '/' + cmd_text
        match = re.match(r'/风格\s+绑定\s+(\S+)\s+(.+)', cmd_text)
        if not match:
            return "格式：/风格 绑定 <人格名> <风格描述>"
        persona_name = match.group(1).strip()
        style_text = match.group(2).strip()
        if not persona_name or not style_text:
            return "人格名和风格描述都不能为空"

        self.dynamic_styles[persona_name] = style_text
        for i in range(1, 4):
            pname_key = f"persona_name_{i}"
            style_key = f"style_control_{i}"
            existing_name = self.plugin.get_config(pname_key, "").strip()
            if existing_name == persona_name:
                await self.plugin.set_config(style_key, style_text)
                break

        return f"已为【{persona_name}】绑定风格：{style_text}"