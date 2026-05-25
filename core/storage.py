import json
import os
import re
from collections import OrderedDict
from datetime import date
from pathlib import Path
from typing import Optional, Dict, Any
import aiofiles
import asyncio

class ScheduleStorage:
    def __init__(self, data_dir: Path, max_cache_size: int = 200):
        self.data_dir = data_dir
        self.cache: OrderedDict[str, Dict] = OrderedDict()  # LRU cache: key -> value
        self._max_cache_size = max_cache_size
        self._lock = asyncio.Lock()

    def set_max_cache_size(self, size: int):
        """动态调整缓存上限，并淘汰超出的条目"""
        self._max_cache_size = max(10, size)
        self._evict_cache()

    def _evict_cache(self):
        """淘汰超出上限的最旧缓存条目"""
        while len(self.cache) > self._max_cache_size:
            self.cache.popitem(last=False)  # FIFO: 移除最早插入的

    @staticmethod
    def _sanitize_persona_id(persona_id: str) -> str:
        """过滤 persona_id 中的路径遍历字符，防止写入到数据目录外"""
        # 移除所有路径分隔符和 . 组合
        clean = persona_id.replace("/", "").replace("\\", "").replace("..", "")
        # 只保留安全字符（字母、数字、中文、下划线、横线、空格）
        clean = re.sub(r'[^\w\u4e00-\u9fff\- ]', '', clean)
        return clean.strip() or "default"

    def _get_path(self, persona_id: str, d: date) -> Path:
        safe_id = self._sanitize_persona_id(persona_id)
        return self.data_dir / safe_id / f"schedule_{d.isoformat()}.json"

    async def load_schedule(self, persona_id: str, d: date) -> Optional[Dict[str, Any]]:
        cache_key = f"{persona_id}_{d.isoformat()}"
        if cache_key in self.cache:
            # LRU: 移到末尾（最近访问）
            self.cache.move_to_end(cache_key)
            return self.cache[cache_key]
        file_path = self._get_path(persona_id, d)
        if not file_path.exists():
            return None
        try:
            async with self._lock:
                async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                data = json.loads(content)
                self.cache[cache_key] = data
                self._evict_cache()
                return data
        except (json.JSONDecodeError, OSError) as e:
            return None

    async def save_schedule(self, persona_id: str, d: date, schedule_data: Dict):
        file_path = self._get_path(persona_id, d)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = file_path.with_suffix('.tmp')
        try:
            async with self._lock:
                async with aiofiles.open(tmp_path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(schedule_data, ensure_ascii=False, indent=2))
                os.replace(tmp_path, file_path)
            cache_key = f"{persona_id}_{d.isoformat()}"
            self.cache[cache_key] = schedule_data
            self._evict_cache()
        except OSError as e:
            raise RuntimeError(f"无法保存日程: {e}")

    async def clear_cache(self, persona_id: str, d: date):
        cache_key = f"{persona_id}_{d.isoformat()}"
        self.cache.pop(cache_key, None)

    async def get_history_schedules(self, persona_id: str, days: int, today: date = None) -> list:
        result = []
        if today is None:
            today = date.today()
        for i in range(1, days + 1):
            d = today - date.resolution * i
            sched = await self.load_schedule(persona_id, d)
            if sched:
                result.append({"date": d.isoformat(), "schedule": sched})
        return result
