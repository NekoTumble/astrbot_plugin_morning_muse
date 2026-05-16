import json
from datetime import date
import random
from typing import Dict, List, Any
from .storage import ScheduleStorage
from astrbot.api import logger

class ContextBuilder:
    def __init__(self, plugin, storage: ScheduleStorage, holidays_module):
        self.plugin = plugin
        self.storage = storage
        self.holidays = holidays_module

    def _get_holiday_info(self, today: date) -> str:
        cn_holidays = self.holidays.China()
        if today in cn_holidays:
            return f"今天是法定节假日：{cn_holidays.get(today)}"
        return "今天为工作日"

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

    async def build_context(self, persona_id: str, dynamic_styles: Dict[str, str],
                            recent_messages: List[str] = None) -> Dict[str, Any]:
        today = date.today()
        style = self.plugin.get_style_for_persona(persona_id)

        ref_days = self.plugin.get_config("context.reference_history_days", 3)
        history = await self.storage.get_history_schedules(persona_id, ref_days)
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
                "- 人设：{persona_desc}\n"
                "- 人格参考：{persona_prompt}\n\n"
                "## 🎲 今日创意约束（必须遵循）\n"
                "- 今日主题：【{daily_theme}】\n"
                "- 心情色彩：【{mood_color}】\n"
                "- 穿搭风格（建议融合方向）：【{outfit_style}】\n"
                "- 日程类型：【{schedule_type}】\n\n"
                "## 🚫 需要避免的重复内容\n"
                "{history_schedules}\n\n"
                "## 💡 参考信息\n"
                "{recent_chats}\n\n"
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
                "（不要改写），但 `outfit` 描述中的实际风格可以是在人格偏好基础上的合理融合。\n\n"
                "### 3. 日程类型与活动安排（第三优先级）\n"
                "- 日程类型：【{schedule_type}】  \n"
                "- 活动内容需符合日程类型，但人格的情感反应优先"
                "（例如：人格讨厌运动，即使日程类型是\"健身日\"也要写出抱怨和傲娇心理）。\n\n"
                "### 4. 避免重复内容\n"
                "{history_schedules}\n\n"
                "### 5. 参考信息\n"
                "{recent_chats}\n\n"
                "## 输出格式要求（严格遵循）\n"
                "- 你必须只输出 **JSON 对象本体**（不要 Markdown 代码块标记，不要额外解释）。\n"
                "- JSON 必须包含字段：\n"
                "  - `\"outfit_style\"`: 值必须严格等于给定的 {outfit_style}\n"
                "  - `\"outfit\"`: 今日穿搭描述（从里到外、从上到下、衣裤内衣裤鞋袜、饰品等），"
                "**第一行必须以\"风格：{outfit_style}\"开头**，"
                "但后续描述必须体现人格偏好与主题的融合。\n"
                "  - `\"schedule\"`: 数组，每个元素包含 `\"time\"` 和 `\"activity\"`。"
                "`time` 使用模糊时段（清晨/上午/午间/下午/傍晚/晚间），不要写具体时间点。\n"
                "  - `\"note\"`: 一段生活感悟或今日寄语，一两句中文，"
                "语气必须完全符合人格设定（从 `{persona_prompt}` 提取）。\n\n"
                "## 开始生成\n"
                "请按照以上约束，输出今日的 JSON 日程。"
            )

        replacements = {
            "{date_str}": date_str,
            "{weekday}": weekday,
            "{holiday}": holiday,
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
            prompt = prompt.replace(key, value)

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