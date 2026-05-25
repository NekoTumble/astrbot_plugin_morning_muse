"""天气客户端模块 - 通过 wttr.in 获取天气信息（无需 API Key）"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger("astrbot")

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False


class WeatherClient:
    """异步天气客户端，使用 wttr.in 免费 API"""
    
    def __init__(self, enabled: bool = False, location: str = "Beijing"):
        self.enabled = enabled
        self.location = location
        self._cache = None
        self._cache_date = None
    
    async def get_weather(self) -> str:
        """获取天气描述文本，失败返回空字符串"""
        if not self.enabled:
            return ""
        
        if not _HAS_HTTPX:
            logger.debug("[晨光心语] httpx 未安装，天气功能不可用")
            return ""
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # wttr.in format: ?format=3 gives compact one-line output
                url = f"https://wttr.in/{self.location}?format=%C+%t+%h+%w&lang=zh"
                headers = {"User-Agent": "curl/7.68.0"}  # wttr.in needs user-agent
                response = await client.get(url, headers=headers)
                if response.status_code == 200:
                    text = response.text.strip()
                    if text and "Unknown" not in text and "ERROR" not in text:
                        return f"今日天气：{text}"
                return ""
        except asyncio.TimeoutError:
            logger.debug("[晨光心语] 天气查询超时")
            return ""
        except Exception as e:
            logger.debug(f"[晨光心语] 天气查询失败: {e}")
            return ""
