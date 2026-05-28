"""天气客户端模块 - 通过 wttr.in 获取全天天气预报（无需 API Key）"""
import asyncio
import logging
from datetime import date
from typing import Optional

logger = logging.getLogger("astrbot")

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False


class WeatherClient:
    """异步天气客户端，使用 wttr.in 免费 API 获取全天预报"""

    def __init__(self, enabled: bool = False, location: str = "Beijing"):
        self.enabled = enabled
        self.location = location
        self._cache = None       # 缓存预报文本
        self._cache_date = None  # 缓存日期

    async def get_weather(self) -> str:
        """获取全天天气描述文本，失败返回空字符串

        改用 JSON API 获取当天天气预报，包含：
        - 全天温度范围（最高/最低）
        - 降水概率
        - 清晨/上午/午间/下午/傍晚/晚间各时段天气
        同一天内仅请求一次 API，后续返回缓存。
        """
        if not self.enabled:
            return ""

        if not _HAS_HTTPX:
            logger.debug("[晨光心语] httpx 未安装，天气功能不可用")
            return ""

        # ── 日内缓存：同一天只请求一次 ──
        today = str(date.today())
        if self._cache is not None and self._cache_date == today:
            return self._cache

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # 用 JSON API 获取预报数据（我们只用第 0 天）
                url = f"https://wttr.in/{self.location}?format=j1&lang=zh"
                headers = {"User-Agent": "curl/7.68.0"}
                response = await client.get(url, headers=headers)

                if response.status_code != 200:
                    logger.debug(f"[晨光心语] 天气 API 返回状态码: {response.status_code}")
                    return ""

                data = response.json()
                weather_text = self._parse_daily_forecast(data)

                if weather_text:
                    self._cache = weather_text
                    self._cache_date = today
                    return weather_text
                return ""

        except asyncio.TimeoutError:
            logger.debug("[晨光心语] 天气查询超时")
            return ""
        except Exception as e:
            logger.debug(f"[晨光心语] 天气查询失败: {e}")
            return ""

    def _parse_daily_forecast(self, data: dict) -> str:
        """解析 wttr.in JSON 响应，提取全天天气摘要

        返回格式示例：
        今日天气：多云，气温 12~22°C，降水概率 10%；
          清晨：多云 12°C；上午：晴 18°C；午间：晴 22°C；下午：多云 20°C；傍晚：晴 19°C；晚间：少云 14°C
        """
        try:
            current = data.get("current_condition", [{}])[0]
            today = data.get("weather", [{}])[0]

            # ── 全天概览 ──
            overall = self._desc_zh(current.get("lang_zh", [{}])[0].get("value", ""))
            max_temp = today.get("maxtempC", "?")
            min_temp = today.get("mintempC", "?")

            # 降水概率取全天最大值
            hourly_list = today.get("hourly", [])
            max_rain = "0"
            for h in hourly_list:
                chance = h.get("chanceofrain", "0")
                if int(chance) > int(max_rain):
                    max_rain = chance

            overview = (
                f"今日天气：{overall}，"
                f"气温 {min_temp}~{max_temp}°C，"
                f"降水概率 {max_rain}%"
            )

            # ── 逐时段摘要 ──
            # 与提示词中日程时段对齐：清晨/上午/午间/下午/傍晚/晚间
            # hourly 数据每 3 小时一条，time 字段格式为 "0","300","600"...
            # 清晨→6:00  上午→9:00  午间→12:00  下午→15:00  傍晚→18:00  晚间→21:00
            period_map = {
                "清晨": 600,
                "上午": 900,
                "午间": 1200,
                "下午": 1500,
                "傍晚": 1800,
                "晚间": 2100,
            }

            # 建立 time -> hourly entry 映射
            hourly_by_time = {}
            for h in hourly_list:
                t = int(h.get("time", "0"))  # "600" -> 600, "1200" -> 1200
                hourly_by_time[t] = h

            parts = []
            for period_name, time_key in period_map.items():
                entry = hourly_by_time.get(time_key)
                if not entry:
                    continue

                desc = self._desc_zh(
                    entry.get("lang_zh", [{}])[0].get("value", "")
                )
                temp = entry.get("tempC", "?")
                rain = entry.get("chanceofrain", "0")

                part = f"{period_name}：{desc} {temp}°C"
                if int(rain) >= 30:
                    part += f"（降雨{rain}%）"
                parts.append(part)

            if parts:
                return overview + "；\n  " + "；".join(parts)
            return overview

        except Exception as e:
            logger.debug(f"[晨光心语] 天气数据解析失败: {e}")
            return ""

    @staticmethod
    def _desc_zh(desc: str) -> str:
        """确保中文描述非空"""
        return desc if desc and desc not in ("Unknown", "ERROR") else "天气未知"
