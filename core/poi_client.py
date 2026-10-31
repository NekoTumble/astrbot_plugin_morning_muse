"""J1 · 本地地点参考：周边 POI 查询（可选，默认不查）。

设置页有个下拉 `local_geo_map`，三选一：

    off   不查（默认）—— 只靠你手填的清单 + 模型自己的城市常识
    amap  高德 Web 服务 —— 国内店名最全，需要你自己申请 Key（有免费额度）
    osm   OpenStreetMap —— 免费、不用 Key，大城市的公园/地标还行，小店铺偏少

三条纪律（跟上一版一样，只是多了一个数据源）：

1. **只有「本地环境参考」总开关开着、且地图源不是 off 时才发请求**；关着一行都不走。
2. **一天最多查一次，结果落盘缓存**（生成与重写共用；换地址/换源才重查）。
3. **不落盘坐标**：缓存只存「名字 + 粗粒度距离桶（步行可达 / 要出门一趟）」。

失败一律安静退化（没网 / 配额用尽 / 地址解析不出 → 当作没有 POI），
退回「模型常识 + 手填清单」，不影响日程生成。
"""

import json
import math
import time
from datetime import date
from pathlib import Path

from astrbot.api import logger

from .weather_client import _get_client  # 复用天气那条 httpx 连接，由 aclose_client() 统一收尾

# ── 高德 ────────────────────────────────────────────────
AMAP_GEO = "https://restapi.amap.com/v3/geocode/geo"
AMAP_AROUND = "https://restapi.amap.com/v3/place/around"
# 餐饮服务 / 购物服务 / 风景名胜 / 文化场馆 / 体育休闲
AMAP_TYPES = "050000|060000|110000|140000|080000"

# ── OpenStreetMap ──────────────────────────────────────
NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"
# Nominatim 的使用政策要求带一个能辨认来源的 UA
USER_AGENT = "AstrBot-MorningMuse/1.5.29 (AstrBot plugin: local geo reference)"

RADIUS_M = 3000          # 周边 3 公里
MAX_POIS = 8             # 最多带几个地方给她
WALK_M = 1500            # ≤1.5 公里算「步行可达」
TIMEOUT = 12.0

# 名字里带这些词的不要（不是「可去的地方」，是行政/住宅/公司一类）
_SKIP_WORDS = ("小区", "社区", "村委会", "居委会", "有限公司", "售后", "维修", "充电站")

_OSM_QUERY = """[out:json][timeout:25];
(
 nwr["name"]["amenity"~"^(cafe|restaurant|fast_food|food_court|library|cinema|theatre|marketplace)$"](around:{r},{lat},{lon});
 nwr["name"]["leisure"~"^(park|garden|playground|fitness_centre|sports_centre)$"](around:{r},{lat},{lon});
 nwr["name"]["shop"~"^(books|supermarket|convenience|bakery|coffee|tea|florist)$"](around:{r},{lat},{lon});
 nwr["name"]["tourism"~"^(museum|attraction|gallery|viewpoint)$"](around:{r},{lat},{lon});
);
out center 80;"""


def resolve_area(plugin) -> str:
    """查询中心用哪个地址：优先 `local_geo_area`，空则回退天气配置里的城市。"""
    area = str(plugin.get_config("local_geo_area", "") or "").strip()
    if not area:
        area = str(plugin.get_config("weather.weather_location", "") or "").strip()
    return area


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _bucket(distance_m: float) -> str:
    return "步行可达" if 0 < distance_m <= WALK_M else "要出门一趟"


def _acceptable(name: str) -> bool:
    return bool(name) and len(name) <= 24 and not any(w in name for w in _SKIP_WORDS)


class PoiClient:
    def __init__(self, plugin, data_dir: Path):
        self.plugin = plugin
        self.cache_path = Path(data_dir) / "local_geo_poi_cache.json"
        self._mem: dict | None = None

    # ── 对外 ────────────────────────────────────────────────
    async def day_pois(self) -> list[dict]:
        """返回 [{"name": ..., "bucket": ...}]；没开/没源/查不到都是空列表。"""
        if not self.plugin.get_config("enable_local_geo", False):
            return []
        backend = str(self.plugin.get_config("local_geo_map", "off") or "off").strip().lower()
        if backend not in ("amap", "osm"):
            return []
        area = resolve_area(self.plugin)
        if not area:
            return []
        if backend == "amap" and not str(
            self.plugin.get_config("local_geo_amap_key", "") or ""
        ).strip():
            return []          # 选了高德却没填 Key = 当没选（不报错、不打扰）

        today = date.today().isoformat()
        cached = self._load_cache()
        if (
            cached
            and cached.get("date") == today
            and cached.get("area") == area
            and cached.get("backend") == backend
        ):
            return list(cached.get("items") or [])

        try:
            if backend == "amap":
                items = await self._query_amap(area)
            else:
                items = await self._query_osm(area)
        except Exception as e:
            # 不把地址原文写进日志
            logger.info(f"[晨光心语] 📍 本地地点参考：查询失败已跳过（{type(e).__name__}）")
            items = []

        # 查不到也缓存「今天这个地址没有结果」，免得每次生成都去打接口（配额很贵）
        self._save_cache(
            {"date": today, "area": area, "backend": backend, "items": items,
             "ts": int(time.time())}
        )
        return items

    # ── 缓存 ────────────────────────────────────────────────
    def _load_cache(self) -> dict | None:
        if self._mem is not None:
            return self._mem
        try:
            if self.cache_path.exists():
                self._mem = json.loads(self.cache_path.read_text(encoding="utf-8"))
                return self._mem
        except Exception as e:
            logger.debug(f"[晨光心语] 本地地点缓存读取失败（忽略）: {e}")
        self._mem = None
        return None

    def _save_cache(self, data: dict) -> None:
        self._mem = data
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.debug(f"[晨光心语] 本地地点缓存写入失败（忽略）: {e}")

    def forget(self) -> None:
        """改了地址/换了源之后想立刻重查就调它。"""
        self._mem = None
        try:
            self.cache_path.unlink(missing_ok=True)
        except Exception:
            pass

    # ── 高德 ────────────────────────────────────────────────
    async def _query_amap(self, area: str) -> list[dict]:
        key = str(self.plugin.get_config("local_geo_amap_key", "") or "").strip()
        client = _get_client()
        lng, lat = await self._amap_geocode(client, key, area)
        if not lng:
            logger.info("[晨光心语] 📍 本地地点参考：地址解析不出来，本次不查周边（不影响日程）")
            return []
        return await self._amap_around(client, key, lng, lat)

    async def _amap_geocode(self, client, key: str, area: str):
        resp = await client.get(
            AMAP_GEO, params={"key": key, "address": area, "output": "JSON"}, timeout=TIMEOUT
        )
        data = resp.json()
        if str(data.get("status")) != "1":
            logger.info(f"[晨光心语] 📍 高德地理编码失败（info={data.get('info')}），本次跳过")
            return "", ""
        geos = data.get("geocodes") or []
        loc = str((geos[0] or {}).get("location") or "") if geos else ""
        if "," not in loc:
            return "", ""
        lng, lat = loc.split(",", 1)
        return lng.strip(), lat.strip()

    async def _amap_around(self, client, key: str, lng: str, lat: str) -> list[dict]:
        resp = await client.get(
            AMAP_AROUND,
            params={
                "key": key,
                "location": f"{lng},{lat}",
                "radius": RADIUS_M,
                "types": AMAP_TYPES,
                "sortrule": "distance",
                "offset": 25,
                "page": 1,
                "extensions": "base",
                "output": "JSON",
            },
            timeout=TIMEOUT,
        )
        data = resp.json()
        if str(data.get("status")) != "1":
            logger.info(f"[晨光心语] 📍 高德周边搜索失败（info={data.get('info')}），本次跳过")
            return []

        items: list[dict] = []
        seen: set[str] = set()
        for poi in data.get("pois") or []:
            if not isinstance(poi, dict):
                continue
            name = str(poi.get("name") or "").strip()
            if not _acceptable(name) or name in seen:
                continue
            try:
                dist = int(str(poi.get("distance") or "0"))
            except (TypeError, ValueError):
                dist = 0
            seen.add(name)
            items.append({"name": name, "bucket": _bucket(dist)})
            if len(items) >= MAX_POIS:
                break
        if items:
            logger.info(f"[晨光心语] 📍 高德：查到 {len(items)} 个周边地点（今天不再重复查）")
        return items

    # ── OpenStreetMap ───────────────────────────────────────
    async def _query_osm(self, area: str) -> list[dict]:
        client = _get_client()
        lat, lon = await self._osm_geocode(client, area)
        if lat is None:
            logger.info("[晨光心语] 📍 本地地点参考：OSM 解析不出这个地址，本次不查周边（不影响日程）")
            return []
        return await self._osm_around(client, lat, lon)

    async def _osm_geocode(self, client, area: str):
        resp = await client.get(
            NOMINATIM,
            params={"q": area, "format": "json", "limit": 1, "addressdetails": 0},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        data = resp.json()
        if not isinstance(data, list) or not data:
            return None, None
        try:
            return float(data[0]["lat"]), float(data[0]["lon"])
        except (KeyError, TypeError, ValueError):
            return None, None

    async def _osm_around(self, client, lat: float, lon: float) -> list[dict]:
        query = _OSM_QUERY.format(r=RADIUS_M, lat=lat, lon=lon)
        resp = await client.post(
            OVERPASS,
            data={"data": query},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        data = resp.json()
        rows: list[tuple[float, str]] = []
        for el in data.get("elements") or []:
            if not isinstance(el, dict):
                continue
            tags = el.get("tags") or {}
            name = str(tags.get("name:zh") or tags.get("name") or "").strip()
            if not _acceptable(name):
                continue
            center = el.get("center") or {}
            el_lat = el.get("lat", center.get("lat"))
            el_lon = el.get("lon", center.get("lon"))
            try:
                dist = _haversine_m(lat, lon, float(el_lat), float(el_lon))
            except (TypeError, ValueError):
                dist = 0.0
            rows.append((dist, name))

        rows.sort(key=lambda x: x[0])
        items: list[dict] = []
        seen: set[str] = set()
        for dist, name in rows:
            if name in seen:
                continue
            seen.add(name)
            items.append({"name": name, "bucket": _bucket(dist)})
            if len(items) >= MAX_POIS:
                break
        if items:
            logger.info(f"[晨光心语] 📍 OSM：查到 {len(items)} 个周边地点（今天不再重复查）")
        return items
