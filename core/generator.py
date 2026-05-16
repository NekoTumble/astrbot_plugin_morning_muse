import json
import asyncio
from datetime import date, datetime
from typing import Optional, Dict, Any, List
from astrbot.api import logger

class ScheduleGenerator:
    def __init__(self, plugin, storage, context_builder, context=None, debug_logs: List[Dict] = None):
        self.plugin = plugin
        self.storage = storage
        self.context_builder = context_builder
        self.context = context
        self.debug_logs = debug_logs if debug_logs is not None else []

    def _add_log(self, level: str, msg: str):
        entry = {"time": datetime.now().isoformat(), "level": level, "msg": msg}
        self.debug_logs.append(entry)
        if level == "error":
            logger.error(msg)
        elif level == "warning":
            logger.warning(msg)
        else:
            logger.info(msg)

    async def _get_provider(self):
        if self.context is None:
            return None
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    async def generate(self, persona_id: str, dynamic_styles: Dict[str, str],
                       recent_messages: List[str] = None, force: bool = False) -> Optional[Dict]:
        provider = await self._get_provider()
        logger.warning(f"📅 生成日程: persona={persona_id}, force={force}, provider={provider is not None}")

        today = date.today()
        if not force:
            existing = await self.storage.load_schedule(persona_id, today)
            if existing:
                self._add_log("info", f"{persona_id} 日程已存在，跳过生成")
                return existing

        try:
            context = await self.context_builder.build_context(persona_id, dynamic_styles, recent_messages)
        except Exception as e:
            self._add_log("error", f"构建上下文失败: {e}")
            return None

        prompt = context["prompt"]
        model = self.plugin.get_config("schedule.schedule_model", "")
        schedule_data = None

        if provider is not None:
            for attempt in range(3):
                try:
                    resp = await self._call_llm(provider, prompt, model)
                    schedule_data = self._parse_llm_response(resp)
                    if schedule_data:
                        self._add_log("info", f"{persona_id} LLM生成成功 (attempt {attempt+1})")
                        break
                    else:
                        preview = str(resp)[:200] if resp else "(empty)"
                        self._add_log("warning", f"LLM返回解析失败 (attempt {attempt+1}): {preview}")
                except Exception as e:
                    self._add_log("warning", f"LLM调用失败 (attempt {attempt+1}): {e}")
                    await asyncio.sleep(1)

        if not schedule_data:
            schedule_data = self._fallback_generate(context["random_elements"])
            reason = "LLM Provider不可用" if provider is None else "所有尝试均失败"
            self._add_log("warning", f"{persona_id} 使用兜底生成 ({reason})")

        try:
            await self.storage.save_schedule(persona_id, today, schedule_data)
            self._add_log("info", f"{persona_id} 日程已保存")
        except Exception as e:
            self._add_log("error", f"保存日程失败: {e}")

        return schedule_data

    async def _call_llm(self, provider, prompt: str, model: str) -> str:
        if model:
            resp = await provider.text_chat(prompt=prompt, model=model)
        else:
            resp = await provider.text_chat(prompt=prompt)
        
        if hasattr(resp, 'completion_text'):
            return resp.completion_text
        try:
            return resp['choices'][0]['message']['content']
        except (TypeError, KeyError, IndexError):
            pass
        try:
            return resp.message.content
        except AttributeError:
            pass
        if isinstance(resp, str):
            return resp
        return str(resp)

    def _parse_llm_response(self, text: str) -> Optional[Dict]:
        if not text:
            return None
        try:
            text = text.strip()
            if "```" in text:
                parts = text.split("```")
                if len(parts) >= 2:
                    text = parts[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            data = json.loads(text)
            if isinstance(data, dict) and "outfit" in data and "schedule" in data:
                return data
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}, 原文前200字符: {text[:200] if isinstance(text, str) else str(text)[:200]}")
        except Exception as e:
            logger.warning(f"解析异常: {e}")
        return None

    def _fallback_generate(self, random_elements: Dict) -> Dict:
        themes = random_elements.get("theme", "日常生活")
        colors = random_elements.get("color", "舒适")
        outfits = random_elements.get("outfit", "休闲")
        types = random_elements.get("schedule_type", "事务")
        return {
            "outfit": f"今天选择了{outfits}风格的穿搭，主色调是{colors}。",
            "schedule": [
                {"time": "上午", "activity": f"专注于{themes}相关的{types}"},
                {"time": "下午", "activity": f"继续推进{types}计划"},
                {"time": "晚上", "activity": "放松身心，回顾一天"}
            ],
            "note": "（此为兜底日程，配置生成模型后可使用 AI 个性化）"
        }