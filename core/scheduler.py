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
        time_str = self.plugin.get_config("schedule.schedule_time", "08:00")
        tz_name = self.plugin.get_config("schedule.timezone", "Asia/Shanghai")
        try:
            hour, minute = map(int, time_str.split(":"))

            kwargs = {
                "func": self._scheduled_generate,
                "trigger": "cron",
                "hour": hour,
                "minute": minute,
                "id": "morning_muse_generate",
                "replace_existing": True
            }

            # V4 修复：非法时区抛的是 ZoneInfoNotFoundError（KeyError 子类），**不是**
            # ImportError —— 原来只抓 ImportError，会让整个 job 建不出来。这里放宽并回退 UTC，
            # 与 core/timezone_utils.py 的策略保持一致。
            try:
                from zoneinfo import ZoneInfo
                try:
                    kwargs["timezone"] = ZoneInfo(tz_name)
                except Exception:
                    logger.warning(f"[晨光心语] 无效时区 '{tz_name}'，回退到 UTC")
                    kwargs["timezone"] = ZoneInfo("UTC")
            except ImportError:
                try:
                    import pytz
                    try:
                        kwargs["timezone"] = pytz.timezone(tz_name)
                    except Exception:
                        logger.warning(f"[晨光心语] 无效时区 '{tz_name}'，回退到 UTC")
                        kwargs["timezone"] = pytz.UTC
                except Exception:
                    logger.warning(f"[晨光心语] 无法设置时区 '{tz_name}'，使用系统默认时区")

            # V4 修复：先建成功再替换。R1 修复：同 id 有 replace_existing=True 就够了 ——
            # 原来的 remove() 是按 **id** 删的，删掉的正是刚建好的新 job（还不抛异常、日志照打「已更新」）。
            new_job = self.scheduler.add_job(**kwargs)
            self._job = new_job
            logger.info(f"日程定时任务已更新至 {time_str} (时区: {tz_name})")
        except Exception as e:
            logger.error(f"定时任务设置失败: {e}")

    async def _scheduled_generate(self):
        all_personas = await self.plugin._get_active_persona_ids()
        if not all_personas:
            all_personas = ["default"]
        tasks = []
        for pid in all_personas:
            cached = self.plugin.chat_cache.get(pid) if self.plugin.chat_cache else []
            tasks.append(self.generator.generate(pid, dynamic_styles=self.plugin.dynamic_styles, recent_messages=cached, force=True))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success_count = sum(1 for r in results if r and not isinstance(r, Exception))
        logger.info(f"定时批量生成完成：{success_count}/{len(tasks)} 成功")

    def shutdown(self):
        self.scheduler.shutdown()
