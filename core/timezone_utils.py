"""时区工具模块 - 提供统一的时区感知日期获取"""
import datetime
import logging

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


# E15：全局默认时区。插件加载时按「配置时区」设一次，
# 让账本 / 状态种子 / 历史日程这些拿不到 plugin 引用的模块也能用同一个「今天」。
_DEFAULT_TZ = "Asia/Shanghai"


def set_default_tz(tz_name: str = "") -> None:
    """插件加载时调用一次：把配置时区定成全局默认。"""
    global _DEFAULT_TZ
    if tz_name:
        _DEFAULT_TZ = str(tz_name)


def get_default_tz() -> str:
    return _DEFAULT_TZ


def get_now(tz_name: str = "") -> datetime.datetime:
    """获取指定时区的当前时间（不传就用全局默认时区）"""
    tz = _get_tz(tz_name or _DEFAULT_TZ)
    if tz is None:
        return datetime.datetime.now()
    return datetime.datetime.now(tz)


def get_today(tz_name: str = "") -> datetime.date:
    """获取指定时区的当前日期（不传就用全局默认时区）"""
    return get_now(tz_name).date()
