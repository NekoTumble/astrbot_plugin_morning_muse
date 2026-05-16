import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from astrbot.api import logger

class MorningScheduler:
    def __init__(self, generator, plugin):
        self.scheduler = AsyncIOScheduler()
        self.generator = generator
        self.plugin = plugin
        self._job = None

    def start(self):
        self.scheduler.start()
        self.update_job()

    def update_job(self):
        if self._job:
            self._job.remove()
        time_str = self.plugin.get_config("schedule.schedule_time", "08:00")
        try:
            hour, minute = map(int, time_str.split(":"))
            self._job = self.scheduler.add_job(
                self._scheduled_generate,
                'cron',
                hour=hour,
                minute=minute,
                id='morning_muse_generate',
                replace_existing=True
            )
            logger.info(f"日程定时任务已更新至 {time_str}")
        except Exception as e:
            logger.error(f"定时任务设置失败: {e}")

    async def _scheduled_generate(self):
        all_personas = await self.plugin._get_active_persona_ids()
        if not all_personas:
            all_personas = ["default"]
        tasks = []
        for pid in all_personas:
            cached = self.plugin.chat_cache.get(pid) if self.plugin.chat_cache else []
            tasks.append(self.generator.generate(pid, dynamic_styles={}, recent_messages=cached, force=False))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success_count = sum(1 for r in results if r and not isinstance(r, Exception))
        logger.info(f"定时批量生成完成：{success_count}/{len(tasks)} 成功")

    def shutdown(self):
        self.scheduler.shutdown()