import json
import os
from datetime import date
from pathlib import Path
from typing import Optional, Dict, Any
import aiofiles
import asyncio

class ScheduleStorage:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.cache: Dict[str, Dict] = {}  # key: persona_id_date
        self._lock = asyncio.Lock()

    def _get_path(self, persona_id: str, d: date) -> Path:
        return self.data_dir / persona_id / f"schedule_{d.isoformat()}.json"

    async def load_schedule(self, persona_id: str, d: date) -> Optional[Dict[str, Any]]:
        cache_key = f"{persona_id}_{d.isoformat()}"
        if cache_key in self.cache:
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
        except OSError as e:
            raise RuntimeError(f"无法保存日程: {e}")

    async def clear_cache(self, persona_id: str, d: date):
        cache_key = f"{persona_id}_{d.isoformat()}"
        self.cache.pop(cache_key, None)

    async def get_history_schedules(self, persona_id: str, days: int) -> list:
        result = []
        today = date.today()
        for i in range(1, days + 1):
            d = today - date.resolution * i
            sched = await self.load_schedule(persona_id, d)
            if sched:
                result.append({"date": d.isoformat(), "schedule": sched})
        return result