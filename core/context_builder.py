import json
from datetime import date, datetime, timedelta
import random
from typing import Dict, List, Any
from .storage import ScheduleStorage
from .timezone_utils import get_today
from astrbot.api import logger

try:
    from lunar_python import Lunar
    _HAS_LUNAR = True
except ImportError:
    _HAS_LUNAR = False

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

    def __init__(self, plugin, storage: ScheduleStorage, holidays_module):
        self.plugin = plugin
        self.storage = storage
        self.holidays = holidays_module

    def _get_today(self) -> date:
        """获取当前日期（使用配置的时区）"""
        tz_name = self.plugin.get_config("schedule.timezone", "Asia/Shanghai")
        return get_today(tz_name)

    # ── 三层节日检测 ───────────────────────────────────────
    def _get_holiday_info(self, today: date) -> str:
        """三层节日检测：法定节假日 → 公历节日 → 农历节日"""
        parts = []

        # 第一层：法定节假日（holidays）
        cn = self.holidays.China()
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

        return "，".join(parts) if parts else "今天为工作日"

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

    def get_known_persona_ids(self) -> List[str]:
        ids = []
        for i in range(1, 4):
            name = self.plugin.get_config(f"persona_styles.persona_name_{i}", "").strip()
            if name:
                ids.append(name)
        if hasattr(self.plugin, 'dynamic_styles'):
            for name in self.plugin.dynamic_styles.keys():
                if name.strip() and name.strip() not in ids:
                    ids.append(name.strip())
        if not ids:
            ids.append("default")
        return ids

    async def _get_persona_prompt(self, persona_id: str) -> str:
        """
        获取指定人格的完整提示词。
        只从 persona_manager 按 ID 精确查找，找不到返回空字符串，不猜测。
        """
        persona_mgr = self.plugin.context.persona_manager
        if not persona_mgr:
            return ""
        try:
            persona = await persona_mgr.get_persona(persona_id)
            if not persona:
                return ""
            # 按优先级尝试多种字段
            if hasattr(persona, 'prompt') and persona.prompt:
                prompt = persona.prompt
                logger.info(f"[晨光心语] ✅ 已加载 {persona_id} 的人格提示词 (长度: {len(prompt)})")
                return prompt
            if hasattr(persona, 'begin_dialogs') and persona.begin_dialogs:
                dialogs = persona.begin_dialogs
                prompt = "\n".join([str(d) for d in dialogs if d]) if isinstance(dialogs, list) else str(dialogs)
                if prompt.strip():
                    logger.info(f"[晨光心语] ✅ 从 begin_dialogs 加载 {persona_id} 的人格提示词 (长度: {len(prompt)})")
                    return prompt
            if hasattr(persona, 'system_prompt') and persona.system_prompt:
                prompt = persona.system_prompt
                logger.info(f"[晨光心语] ✅ 从 system_prompt 加载 {persona_id} 的人格提示词 (长度: {len(prompt)})")
                return prompt
            for attr in ['description', 'content', 'text']:
                val = getattr(persona, attr, None)
                if val and isinstance(val, str) and len(val.strip()) > 10:
                    logger.info(f"[晨光心语] ✅ 从 {attr} 加载 {persona_id} 的人格提示词 (长度: {len(val)})")
                    return val
            if hasattr(persona, 'dict'):
                try:
                    d = persona.dict()
                    for key in ['prompt', 'begin_dialogs', 'system_prompt', 'description', 'content']:
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
            logger.error(f"[晨光心语] 获取人格提示词异常: {e}")
            return ""

    async def build_context(self, persona_id: str,
                            recent_messages: List[str] = None) -> Dict[str, Any]:
        today = self._get_today()
        style = self.plugin.get_style_for_persona(persona_id)

        ref_days = self.plugin.get_config("context.reference_history_days", 3)
        history = await self.storage.get_history_schedules(persona_id, ref_days, today=today)
        history_text = ""
        for h in history:
            history_text += f"{h['date']}: {json.dumps(h['schedule'], ensure_ascii=False)}\n"

        ref_count = self.plugin.get_config("context.reference_recent_count", 5)
        recent_text = ""
        if recent_messages:
            recent_text = "\n".join(recent_messages[-ref_count:])

        daily_theme = self._random_pick(self.plugin.get_config("daily_themes", ""), "元气满满")
        mood_color = self._random_pick(self.plugin.get_config("mood_colors", ""), "暖橙")
        outfit_style = self._random_pick(self.plugin.get_config("outfit_styles", ""), "休闲")
        schedule_type = self._random_pick(self.plugin.get_config("schedule_types", ""), "学习")

        # 获取天气信息
        weather_text = ""
        if hasattr(self.plugin, 'weather_client') and self.plugin.weather_client:
            try:
                weather_text = await self.plugin.weather_client.get_weather()
            except Exception as e:
                logger.debug(f"[晨光心语] 天气获取失败: {e}")

        weekday_names = ['一', '二', '三', '四', '五', '六', '日']
        date_str = f"{today.year}年{today.month}月{today.day}日"
        weekday = "星期" + weekday_names[today.weekday()]
        holiday = self._get_holiday_info(today)

        if style:
            persona_desc = f"人格：{persona_id}，风格：{style}"
        else:
            persona_desc = f"人格：{persona_id}"

        persona_prompt = await self._get_persona_prompt(persona_id)

        prompt_template = self.plugin.get_config("prompt_template", "")
        if not prompt_template:
            prompt_template = (
                "# Role: Life Scheduler\n"
                "请根据以下信息，为自己规划一份今天的生活安排。\n\n"
                "## Context\n"
                "- 日期：{date_str} {weekday} {holiday}\n"
                "- 今日天气：{weather_info}\n"
                "- 人设：{persona_desc}\n"
                "- 人格参考：{persona_prompt}\n\n"
                "## 🎲 今日创意约束（必须遵循）\n"
                "- 今日主题：【{daily_theme}】\n"
                "- 心情色彩：【{mood_color}】\n"
                "- 穿搭风格（建议融合方向）：【{outfit_style}】\n"
                "- 日程类型（建议参考，非强制）：【{schedule_type}】\n\n"
                "## 🚫 需要避免的重复内容\n"
                "{history_schedules}\n\n"
                "## 💡 参考信息\n"
                "- 近期聊天：{recent_chats}\n\n"
                "## 强制约束（按优先级从高到低执行）\n\n"
                "### 1. 人格核心身份（最高优先级）\n"
                "你的人格设定由 `{persona_prompt}` 完整定义。  \n"
                "**该身份必须贯穿所有输出**：包括穿搭偏好、日常习惯、说话语气、情感反应、活动选择。  \n"
                "**当任何其他约束与此冲突时，优先保留人格身份特征**（而不是生硬地替换或忽略）。\n\n"
                "### 2. 当日主题与穿搭风格（第二优先级）\n"
                "- 今日主题：【{daily_theme}】  \n"
                "- 穿搭风格（建议方向）：【{outfit_style}】  \n\n"
                "**处理冲突的规则（模拟人类思考）**：\n"
                "- 如果 `{outfit_style}` 与人格偏好（来自 `{persona_prompt}`）不矛盾 → 直接融合"
                "（例如：人格喜欢复古风 + 运动主题 = 复古运动套装）。\n"
                "- 如果 `{outfit_style}` 与人格偏好明显冲突"
                "（例如人格喜欢洛丽塔，但主题要求\"商务正装\"）"
                " → **依然保留人格偏好的核心元素**（颜色/版型/标志性配饰等），"
                "仅在局部适应主题（换一件单品或加一件外套）。\n"
                "- 允许角色在内心吐槽\"为什么今天的主题是这个\"，"
                "但实际穿搭必须让读者能识别出人格偏好的延续。\n"
                "- **输出字段 `outfit_style` 的值必须等于原样给定的 {outfit_style}**"
                "（不要改写），但 `outfit` 描述中的实际风格可以是在人格偏好基础上的合理融合。\n"
                "- 穿搭同时需考虑今日天气 {weather_info}（如雨天需加雨具、降温需添衣等），"
                "在不破坏人格偏好的前提下做出适当调整。\n\n"
                "### 3. 日程类型与活动安排（建议参考，非强制）\n"
                "- 提供的日程类型：【{schedule_type}】仅作为灵感或方向建议，并非硬性要求。\n"
                "- **你可以完全根据自己的性格、当下心情以及今日天气 {weather_info} 来决定实际的活动安排。** "
                "如果性格强烈抵触该类型，或者天气完全不适宜，可以自由调整、替代甚至忽略该类型，"
                "用最符合人格的方式度过今天。\n"
                "- 在具体安排活动时，请综合考虑：角色性格的自然反应、当日的情绪基调（心情色彩）、"
                "以及天气带来的影响（如雨天更适合室内活动、酷暑避免户外暴晒等），"
                "让日程既忠于人格又贴合现实。\n\n"
                "### 4. 避免重复内容\n"
                "{history_schedules}\n\n"
                "### 5. 参考信息\n"
                "以上提供的近期聊天 {recent_chats} 可作为你今日心境或活动灵感的额外参考，但不强制使用。\n\n"
                "## 输出格式要求（严格遵循）\n"
                "- 你必须只输出 **JSON 对象本体**（不要 Markdown 代码块标记，不要额外解释）。\n"
                "- JSON 必须包含字段：\n"
                "  - `\"outfit_style\"`: 值必须严格等于给定的 {outfit_style}\n"
                "  - `\"outfit\"`: 今日穿搭描述（从里到外、从上到下、衣裤内衣裤鞋袜、饰品等），"
                "**第一行必须以\"风格：{outfit_style}\"开头**，"
                "但后续描述必须体现人格偏好与主题、天气的融合。\n"
                "  - `\"schedule\"`: 数组，每个元素包含 `\"time\"` 和 `\"activity\"`。"
                "`time` 使用模糊时段（清晨/上午/午间/下午/傍晚/晚间），不要写具体时间点。"
                "活动安排需自然融入角色的性格和今日天气。\n"
                "  - `\"note\"`: 一段生活感悟或今日寄语，一两句中文，"
                "语气必须完全符合人格设定（从 `{persona_prompt}` 提取），也可适当提及对天气或日程安排的感受。\n\n"
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
        }

        prompt = prompt_template
        for key, value in replacements.items():
            prompt = prompt.replace(key, str(value) if value else "")

        return {
            "prompt": prompt,
            "style": style,
            "random_elements": {
                "theme": daily_theme,
                "color": mood_color,
                "outfit": outfit_style,
                "schedule_type": schedule_type
            }
        }
