"""时区工具模块 - 提供统一的时区感知日期获取"""
import datetime
import logging
from typing import Optional

logger = logging.getLogger("astrbot")

# Try zoneinfo (Python 3.9+) first, fallback to pytz
try:
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:
    _HAS_ZONEINFO = False
    try:
        import pytz
        _HAS_PYTZ = True
    except ImportError:
        _HAS_PYTZ = False


def _get_tz(tz_name: str):
    """获取时区对象"""
    if not tz_name:
        tz_name = "Asia/Shanghai"
    
    if _HAS_ZONEINFO:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            logger.warning(f"[晨光心语] 无效时区 '{tz_name}'，回退到 UTC")
            return ZoneInfo("UTC")
    
    if _HAS_PYTZ:
        try:
            return pytz.timezone(tz_name)
        except pytz.exceptions.UnknownTimeZoneError:
            logger.warning(f"[晨光心语] 无效时区 '{tz_name}'，回退到 UTC")
            return pytz.UTC
    
    # No timezone library available, use naive datetime
    logger.warning("[晨光心语] 无时区库可用（需要 zoneinfo 或 pytz），使用系统本地时间")
    return None


def get_now(tz_name: str = "Asia/Shanghai") -> datetime.datetime:
    """获取指定时区的当前时间"""
    tz = _get_tz(tz_name)
    if tz is None:
        return datetime.datetime.now()
    return datetime.datetime.now(tz)


def get_today(tz_name: str = "Asia/Shanghai") -> datetime.date:
    """获取指定时区的当前日期"""
    return get_now(tz_name).date()
