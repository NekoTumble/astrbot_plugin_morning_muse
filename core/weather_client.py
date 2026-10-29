"""天气客户端模块 - 通过 wttr.in 获取全天天气预报（无需 API Key）

另含官方预警客户端（中央气象台 findAlarm）—— v1.5.16 加的：
把「霾 / 大雾 / 高温」这类判断从「中文文本捞词」升级成「官方通报」。
"""
import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any

from .timezone_utils import get_today

logger = logging.getLogger("astrbot")

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False


# ── F4（保守版）：进程内复用同一条 AsyncClient ──
# 原来每次请求都 `async with httpx.AsyncClient(...)`，等于每次重做一次 TLS 握手。
# 保守版**只做连接复用**，刻意不动「失败后要不要重试」的逻辑 —— 失败照旧下一跳
# 心跳重来，所以天气接口一恢复就能立刻拿到。由 main.terminate() 调 aclose_client() 收尾。
_CLIENT: Any = None


def _get_client():
    global _CLIENT
    if _CLIENT is None or getattr(_CLIENT, "is_closed", False):
        _CLIENT = httpx.AsyncClient(timeout=15.0)
    return _CLIENT


async def aclose_client() -> None:
    """插件卸载 / 重载时关掉共用客户端，免得留一条没关的连接。"""
    global _CLIENT
    cli, _CLIENT = _CLIENT, None
    if cli is not None:
        try:
            await cli.aclose()
        except Exception:
            pass


def _as_int(value: Any, default: int = 0) -> int:
    """安全转 int —— wttr.in 偶发返回 "" / "-" / "N/A"，int() 会抛 ValueError。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    """安全转 float —— 跟 _as_int 一样，防 wttr.in 偶发的 "" / "-" / "N/A"。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first(seq: Any) -> dict:
    """安全取列表第一个元素：键缺失 / 为 None / 空列表都返回空 dict。"""
    if isinstance(seq, (list, tuple)) and seq:
        first = seq[0]
        return first if isinstance(first, dict) else {}
    return {}


class WeatherClient:
    """异步天气客户端，使用 wttr.in 免费 API 获取全天预报"""

    def __init__(self, enabled: bool = False, location: str = "Beijing",
                 tz_name: str = "Asia/Shanghai"):
        self.enabled = enabled
        self.location = location
        self.tz_name = tz_name
        self._cache = None       # 缓存预报文本
        self._cache_date = None  # 缓存日期
        # v1.5.16：结构化指标（降水概率 / 最低能见度 / 最大风速 / 各时段描述）。
        # 跟 _cache 同生共死 —— 上层改为看这些数字判天气，不再拿中文文本捞词。
        self._metrics: dict | None = None

    def get_metrics(self) -> dict:
        """取最近一次解析出来的结构化指标；没取到天气就返回空 dict。

        键：rain_prob（全天最高降水概率 %）、min_visibility（最低能见度 km）、
            max_wind_kmph（最大风速 km/h）、temp_min / temp_max、descs（全部中文描述）。
        任何一项拿不到都是 None，调用方自己判 —— 判定类的东西宁可不动，别乱猜。
        """
        return dict(self._metrics or {})

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
        # 注意用配置时区的“今天”，别用系统时区，否则跨时区时缓存会在错误的时刻失效
        today = str(get_today(self.tz_name))   # E15：tz_name 为空时 get_today 会用全局默认时区
        if self._cache is not None and self._cache_date == today:
            return self._cache

        try:
            # F4（保守版）：复用同一条客户端，只省掉每次的 TLS 握手
            client = _get_client()
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
            # 附修：httpx 超时抛的是 httpx.TimeoutException（不是 asyncio.TimeoutError），
            # 上面那个分支永远命中不了 —— 超时会被这里错记成「查询失败」。
            if _HAS_HTTPX and isinstance(e, httpx.TimeoutException):
                logger.debug("[晨光心语] 天气查询超时")
            else:
                logger.debug(f"[晨光心语] 天气查询失败: {e}")
            return ""

    def _parse_daily_forecast(self, data: dict) -> str:
        """解析 wttr.in JSON 响应，提取全天天气摘要

        返回格式示例：
        今日天气：多云，气温 12~22°C，降水概率 10%；
          清晨：多云 12°C；上午：晴 18°C；午间：晴 22°C；下午：多云 20°C；傍晚：晴 19°C；晚间：少云 14°C
        """
        try:
            # F3：`data.get(key, [{}])[0]` 的默认值只在**键缺失**时生效；键在、但值为
            # 空数组 `[]` 时 [0] 照样抛 IndexError（wttr.in 的 lang=zh 实测常失效）。
            # 统一走 _first()：键缺失 / None / 空列表都给安全默认。
            current = _first(data.get("current_condition"))
            today = _first(data.get("weather"))

            # ── 全天概览 ──
            overall = self._desc_zh(_first(current.get("lang_zh")).get("value", ""))
            max_temp = today.get("maxtempC", "?")
            min_temp = today.get("mintempC", "?")

            # 降水概率取全天最大值
            hourly_list = today.get("hourly", [])
            max_rain = "0"
            for h in hourly_list:
                chance = h.get("chanceofrain", "0")
                if _as_int(chance) > _as_int(max_rain):
                    max_rain = chance

            # ── 结构化指标（v1.5.16）──
            # 「霾 / 雾 / 雨」以前靠中文文本捞词：逐时段里只要有一格写着「烟霾」
            # 或「可能有零星雨」就点火 —— 结果天天点。这里把数字一并抠出来，
            # 上层改成看数字（能见度 / 降水概率 / 风速），文本只留给人看。
            _samples = [current] + [h for h in hourly_list if isinstance(h, dict)]
            _vis = [_as_float(s.get("visibility"), -1.0) for s in _samples]
            _vis = [v for v in _vis if v >= 0]
            _wind = [_as_float(s.get("windspeedKmph"), -1.0) for s in _samples]
            _wind = [w for w in _wind if w >= 0]
            descs = [overall]
            self._metrics = {
                "rain_prob": _as_int(max_rain, 0),
                "min_visibility": min(_vis) if _vis else None,
                "max_wind_kmph": max(_wind) if _wind else None,
                "descs": descs,
            }

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
                t = _as_int(h.get("time"), -1)  # "600" -> 600, "1200" -> 1200
                if t >= 0:
                    hourly_by_time[t] = h

            parts = []
            for period_name, time_key in period_map.items():
                entry = hourly_by_time.get(time_key)
                if not entry:
                    continue

                desc = self._desc_zh(_first(entry.get("lang_zh")).get("value", ""))
                temp = entry.get("tempC", "?")
                rain = entry.get("chanceofrain", "0")
                descs.append(desc)      # 给上层的「确定事件」判定用（雪 / 雷 / 冰雹……）

                part = f"{period_name}：{desc} {temp}°C"
                if _as_int(rain) >= 30:
                    part += f"（降雨{rain}%）"
                parts.append(part)

            if parts:
                return overview + "；\n  " + "；".join(parts)
            return overview

        except Exception as e:
            logger.debug(f"[晨光心语] 天气数据解析失败: {e}")
            self._metrics = None
            return ""

    # E4：wttr.in 的 lang=zh 实测常失效（返回英文），这里补一张英中对照小词典。
    # 不在表里的描述原样透传，维持现状、不乱猜。
    _DESC_ZH = {
        "sunny": "晴",
        "clear": "晴朗",
        "partly cloudy": "局部多云",
        "cloudy": "多云",
        "overcast": "阴",
        "mist": "薄雾",
        "fog": "雾",
        "freezing fog": "冻雾",
        "patchy rain possible": "可能有零星雨",
        "patchy rain nearby": "附近有零星雨",
        "patchy light drizzle": "零星毛毛雨",
        "light drizzle": "毛毛雨",
        "freezing drizzle": "冻毛毛雨",
        "heavy freezing drizzle": "强冻毛毛雨",
        "patchy light rain": "零星小雨",
        "light rain": "小雨",
        "moderate rain at times": "间歇中雨",
        "moderate rain": "中雨",
        "heavy rain": "大雨",
        "light freezing rain": "小冻雨",
        "moderate or heavy freezing rain": "中到大冻雨",
        "light rain shower": "小阵雨",
        "moderate or heavy rain shower": "中到大阵雨",
        "torrential rain shower": "暴雨",
        "patchy light rain with thunder": "零星雷阵雨",
        "moderate or heavy rain with thunder": "中到大雷阵雨",
        "thundery outbreaks possible": "可能有雷暴",
        "patchy light snow": "零星小雪",
        "light snow": "小雪",
        "moderate snow": "中雪",
        "heavy snow": "大雪",
        "patchy moderate snow": "零星中雪",
        "patchy heavy snow": "零星大雪",
        "blowing snow": "吹雪",
        "blizzard": "暴雪",
        "light snow showers": "小阵雪",
        "moderate or heavy snow showers": "中到大阵雪",
        "patchy light snow with thunder": "零星雷阵雪",
        "moderate or heavy snow with thunder": "中到大雷阵雪",
        "light sleet": "小雨夹雪",
        "moderate or heavy sleet": "中到大雨夹雪",
        "light sleet showers": "小阵雨夹雪",
        "moderate or heavy sleet showers": "中到大阵雨夹雪",
        "ice pellets": "冰粒",
        "light showers of ice pellets": "小冰粒阵",
        "moderate or heavy showers of ice pellets": "中到大冰粒阵",
        # ── 风 / 霾 / 尘 类 ──
        # 原来整类缺失。线上出现过 "Smoky haze"，查表查不到 → 描述被原样透传成英文
        # → 中文触发词一个都匹配不上 → 那天「天气变化」这根由头整体失效。
        "windy": "大风",
        "breezy": "微风",
        "squalls": "狂风",
        "smoke": "烟霾",
        "smoky haze": "烟霾",
        "haze": "霾",
        "blowing dust": "扬尘",
        "dust": "浮尘",
        "duststorm": "沙尘暴",
        "blowing sand": "扬沙",
        "sandstorm": "沙尘暴",
    }

    @classmethod
    def _desc_zh(cls, desc: str) -> str:
        """确保描述非空；英文描述翻成中文，认不出的原样返回。"""
        text = (desc or "").strip()
        if not text or text in ("Unknown", "ERROR"):
            return "天气未知"
        return cls._DESC_ZH.get(text.lower(), text)


# ════════════════════════════════════════════════════════════════
# 官方预警（中央气象台）· v1.5.16
# ════════════════════════════════════════════════════════════════
# 为什么要有这个：wttr.in 只回答「现在什么天」，回答不了「官方有没有通报」——
# 它的 j1 里压根没有 alert 字段，唯一的严重度线索是 visibility（还是整数公里）。
# 于是「今天到底算不算大霾」只能猜，猜的结果就是逐时段里只要有一格写着
# 「烟霾」就点火 —— 天天点。
#
# 中央气象台这个接口免费、不用密钥，返回的正是手机推送那一套预警：
#   GET http://www.nmc.cn/rest/findAlarm?pageNo=1&pageSize=500
#   -> data.page.list[] = {title, issuetime, url, pic}
#   title 形如「江苏省镇江市气象台发布霾黄色预警信号」
#
# 三个已知的坑，都绕过去了：
#   1. 没有「按城市查」的参数（province / areaCode / stationid 实测全被忽略，
#      带非 ASCII 的参数还会直接吃 417）—— 只能整个拉回来，按标题里的地名自己筛；
#   2. 服务器不给 gzip，一次 120 KB 左右 —— 所以**必须缓存**，绝不跟着心跳裸查；
#   3. 它不管你在不在本地、也不管你嫌不嫌吵 —— 筛选和门槛全在我们这边。

_ALARM_URL = "http://www.nmc.cn/rest/findAlarm"
_ALARM_LEVELS = ("蓝色", "黄色", "橙色", "红色")
_ALARM_TITLE_RE = re.compile(
    r"^(?P<area>.+?)气象台发布(?P<type>.+?)(?P<level>蓝色|黄色|橙色|红色)预警"
)

# 设置页留空时回退到这几个 —— 刻意**不是**「全国都认」：
# 宁可安静，也不要半夜为另一个省的霾开口。
DEFAULT_ALARM_AREAS = ("镇江", "丹阳", "句容", "扬中", "丹徒")
DEFAULT_ALARM_TYPES = ("霾", "雾", "高温", "寒潮", "大风", "暴雨", "暴雪",
                       "冰雹", "雷电", "沙尘", "台风", "道路结冰")
DEFAULT_ALARM_LEVELS = ("黄色", "橙色", "红色")


class AlarmClient:
    """官方预警客户端。**只留一份内存缓存**，跨天自动作废。

    缓存口径（跟主人的问题一一对应）：
      - 只存一份：预警是「此刻有效」的东西，攒历史没有意义；
      - 不落盘：重载插件多拉一次也就 120 KB、0.2 秒，不值得为它添一个文件和清理逻辑；
      - 跨天作废：日期对不上就当没有（= 主人说的「睡醒清掉前一天」，只是不用手动清）；
      - 睡觉时段（23:00~07:00）不刷新：有旧缓存就吃旧的，没有就返回空；
      - 取数失败：静默吞掉，把空结果缓起来，一小时后再说 —— 绝不拖垮天气那根由头。
    """

    def __init__(self, enabled: bool = False, areas=None, types=None, levels=None,
                 ttl_seconds: int = 3600, day_start: int = 7, day_end: int = 23,
                 tz_name: str = "Asia/Shanghai"):
        self.enabled = enabled
        self.areas = [str(a).strip() for a in (areas or DEFAULT_ALARM_AREAS) if str(a).strip()]
        self.types = [str(t).strip() for t in (types or DEFAULT_ALARM_TYPES) if str(t).strip()]
        self.levels = [str(l).strip() for l in (levels or DEFAULT_ALARM_LEVELS) if str(l).strip()]
        self.ttl_seconds = max(60, int(ttl_seconds or 3600))
        self.day_start = int(day_start)
        self.day_end = int(day_end)
        self.tz_name = tz_name or "Asia/Shanghai"
        self._cache: list[dict] | None = None
        self._cache_date: str | None = None
        self._cache_ts: float = 0.0

    def _now_hour(self) -> int:
        """配置时区下的当前钟点（用来判要不要往外跑）。"""
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(self.tz_name)).hour
        except Exception:
            return datetime.now().hour

    def _remember(self, alerts: list[dict]) -> None:
        """写缓存。失败路径也走这里 —— 免得每分钟都去撞一次墙。"""
        self._cache = alerts
        self._cache_date = str(get_today(self.tz_name))
        self._cache_ts = time.monotonic()

    async def get_alerts(self) -> list[dict]:
        """本地此刻生效的官方预警。拿不到就返回空列表 —— 永远不抛。"""
        if not self.enabled or not _HAS_HTTPX:
            return []

        today = str(get_today(self.tz_name))
        if self._cache_date != today:       # 跨天：昨天那份自动作废
            self._cache = None
            self._cache_ts = 0.0

        now_ts = time.monotonic()
        if self._cache is not None and (now_ts - self._cache_ts) < self.ttl_seconds:
            return self._cache

        hour = self._now_hour()
        if not (self.day_start <= hour < self.day_end):
            return self._cache or []

        try:
            client = _get_client()
            resp = await client.get(
                _ALARM_URL,
                params={"pageNo": 1, "pageSize": 500},
                headers={"User-Agent": "curl/7.68.0"},
            )
            if resp.status_code != 200:
                logger.debug(f"[晨光心语] 预警接口返回状态码: {resp.status_code}")
                self._remember([])
                return self._cache or []
            data = resp.json() or {}
            items = ((data.get("data") or {}).get("page") or {}).get("list") or []
        except Exception as e:
            if _HAS_HTTPX and isinstance(e, httpx.TimeoutException):
                logger.debug("[晨光心语] 预警查询超时")
            else:
                logger.debug(f"[晨光心语] 预警查询失败: {e}")
            self._remember([])
            return self._cache or []

        alerts = self.filter_alerts(items)
        self._remember(alerts)
        if alerts:
            logger.info(
                f"[晨光心语] ⚠️ 本地官方预警 {len(alerts)} 条：{alerts[0].get('title')}"
            )
        return alerts

    def filter_alerts(self, items: list) -> list[dict]:
        """三张筛子：地名 / 预警类型 / 等级。重的排前面（红 > 橙 > 黄 > 蓝）。"""
        out: list[dict] = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "")
            m = _ALARM_TITLE_RE.match(title)
            if not m:                       # 「森林火险」「地质灾害」这种照样能进，靠类型筛
                continue
            area, atype, level = m.group("area"), m.group("type"), m.group("level")
            if self.areas and not any(a in area for a in self.areas):
                continue
            if self.types and not any(t in atype for t in self.types):
                continue
            if self.levels and level not in self.levels:
                continue
            out.append({
                "title": title,
                "area": area,
                "type": atype,
                "level": level,
                "issuetime": str(it.get("issuetime") or ""),
                "url": str(it.get("url") or ""),
            })
        rank = {lv: i for i, lv in enumerate(reversed(_ALARM_LEVELS))}
        out.sort(key=lambda a: a.get("issuetime") or "", reverse=True)   # 新的在前
        out.sort(key=lambda a: rank.get(a.get("level"), 9))              # 再按等级稳定排序
        return out
