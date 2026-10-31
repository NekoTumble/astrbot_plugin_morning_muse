"""J1 · 本地现实地址参考（带开关、默认关）。

口径（2026-10-31 按主人要求改过一次，跟第一版相反）：

> **要「像活在现实里」**：日程里可以有真实店名、公园名、地标名，会去真的地方；
> **但不许到住址级**：门牌号、楼栋单元、小区名、楼层、精确距离一律不出现 ——
> 免得群友顺着日程问出他住哪、跑来"现实 PK"。

所以红线分两半：**名字可以真，地址必须虚**。

数据从哪来（三层，越往下越具体）：

1. 手填清单 `local_geo_places`（最可靠，零外部请求）
2. 地图周边搜索（可选，高德要 Key / OSM 免费；一天最多一次，见 core/poi_client.py）
3. 都为空时，交给模型的城市常识 —— 同时要求「拿不准就说得模糊些」

另外带一道**代码侧过滤** `scrub_*`：生成完扫一遍正文，命中门牌号一类就抹掉。
这道过滤有自己的开关 `local_geo_scrub`（默认开），且**总开关关着时它也不跑**。
"""

import datetime
import re

from astrbot.api import logger

# 占位符里那份画像的「标记句」：用来判断模板里是不是已经有这一段，避免补两次
MARK = "她现实里在"

# 月份 → 季节说法（北半球、中文习惯；不追求节气精度，够当氛围底色就行）
_MONTH_SEASON = {
    1: "隆冬", 2: "冬末", 3: "初春", 4: "仲春", 5: "暮春", 6: "初夏",
    7: "盛夏", 8: "夏末", 9: "初秋", 10: "深秋", 11: "深秋末", 12: "初冬",
}

# ── 门牌号一类的过滤规则 ─────────────────────────────────────────────
# 只认阿拉伯数字（「三层楼」「一层薄雾」这种中文数字是正常描写，别误伤）
# 「3 号桌」「16°C」「21:30」这类不是住址，也别动 —— 所以不单认「号」，
# 要么是「号楼/号院/号房」，要么是「路/街/巷 … 12 号」这种带路名的。
_SCRUB_PATTERNS = (
    (re.compile(r"[0-9０-９]+\s*(?:号楼|号院|号房|栋|幢|单元|室|楼|层)"), "附近"),
    (re.compile(r"(?:路|街|道|巷|村|里|弄)\s*[0-9０-９]+\s*号"), "附近"),
    (re.compile(r"[0-9０-９]+\s*(?:弄|胡同)"), "附近"),
    # 「XX小区」整段换掉：从「小区」往前吃到标点/空格为止
    (re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{1,12}\s*小区"), "住处附近"),
    (re.compile(r"门牌(?:号|号码)?"), ""),
    (re.compile(r"距(?:离)?\s*(?:我)?家?\s*[0-9０-９]+\s*(?:米|公里|千米|km|m)"), "走几步就到"),
)

# 连着抹两处会留下「附近 附近」，收一下尾
_COLLAPSE = (
    re.compile(r"(?:附近|住处附近)[ \u3000]*(?:附近|住处附近)"),
    re.compile(r"[ \u3000]{2,}"),
)

# 要过一遍过滤的文本字段（顶层）
_TEXT_FIELDS = ("intent", "note")
# state 块里也要过（这些同样会被注入到对话里）
_STATE_FIELDS = ("focus", "tomorrow_hint")
_STATE_LISTS = ("open_threads", "carried_over")


def _season_of(month: int) -> str:
    return _MONTH_SEASON.get(int(month), "")


def _manual_places(plugin, key: str = "local_geo_places") -> list[str]:
    """手填的清单。一行一个，也认中英文逗号/顿号分隔。"""
    raw = str(plugin.get_config(key, "") or "").strip()
    if not raw:
        return []
    parts = re.split(r"[,，、;；\n]+", raw)
    out: list[str] = []
    for p in parts:
        name = p.strip().strip("-· ")
        if name and name not in out:
            out.append(name)
    return out[:10]


def build_text(plugin, pois: list[dict] | None = None) -> str:
    """渲染那段「环境画像」。总开关关着 → 返回空串（调用方什么都别做）。"""
    try:
        if not plugin.get_config("enable_local_geo", False):
            return ""
    except Exception:
        return ""

    area = str(plugin.get_config("local_geo_area", "") or "").strip()
    if not area:
        # 留空 → 回退天气配置里的城市名
        area = str(plugin.get_config("weather.weather_location", "") or "").strip()
    if not area:
        logger.info("[晨光心语] 📍 本地环境参考开着，但没有地址（local_geo_area 与天气城市都空），本次不注入")
        return ""

    hint = str(plugin.get_config("local_geo_hint", "") or "").strip()

    try:
        today = plugin._get_today()
    except Exception:
        today = datetime.date.today()
    season = _season_of(today.month)

    lines = [
        "## 📍 她所在的地方（氛围参考：名字可以真，住址必须虚）",
        f"- {MARK}{area}。现在是 {today.month} 月，{season}。那一带的气候按常理推断就好，别编出反季节的天气。",
    ]

    # 具体能去的地方分两组（手填 + 地图查询各归各的组）：
    #   生活圈 → 平时常去；同城景点 → 要费点工夫跑一趟
    # 注意：这里只给「参考与建议」，不设频率/距离的硬限制 —— 她天天去也行。
    manual = _manual_places(plugin, "local_geo_places")
    manual_scenic = _manual_places(plugin, "local_geo_scenic_places")
    nearby = [p for p in (pois or []) if isinstance(p, dict) and p.get("group") != "scenic"]
    scenic = [p for p in (pois or []) if isinstance(p, dict) and p.get("group") == "scenic"]

    lines.append("- 这一带真实存在、她**平时常去**的地方（可以写它们的名字，但不要加门牌号/详细地址）：")
    shown: set[str] = set()
    for name in manual:
        lines.append(f"  · {name}")
        shown.add(name)
    for p in nearby:
        name = str(p.get("name") or "").strip()
        if not name or name in shown:
            continue
        shown.add(name)
        bucket = str(p.get("bucket") or "").strip()
        lines.append(f"  · {name}" + (f"（{bucket}）" if bucket else ""))
    if not shown:
        lines.append(
            "  · （没有具体清单）你知道这座城市的真实地标就写；拿不准的别硬编，说成「城东那家开了很多年的旧书店」这种也像真的。"
        )

    scenic_shown: set[str] = set()
    scenic_lines: list[str] = []
    for name in manual_scenic:
        if name not in shown:
            scenic_shown.add(name)
            scenic_lines.append(f"  · {name}")
    for p in scenic:
        name = str(p.get("name") or "").strip()
        if name and name not in shown and name not in scenic_shown:
            scenic_shown.add(name)
            scenic_lines.append(f"  · {name}")
    if scenic_lines:
        lines.append("- 同城还可以去这些地方（跑一趟要费点工夫，去一趟是件事）：")
        lines.extend(scenic_lines)

    # 距离与节奏：**只给建议**，不设硬限制。想天天去也行，只要写出来可信。
    lines.append(
        "- 远近和频率都随她：近的地方可以天天去，远的偶尔专门跑一趟，真惦记着想连着去几天也没什么不可以。"
        "唯一要顾的是「读起来可信」——去得远、去得勤，就在正文里交代一句为什么（想去的念头、正好顺路、约了人）。"
        "出城（去别的城市）写成计划好的一趟，别让人以为她天天在外面跑。"
    )

    if hint:
        lines.append(f"- 她跟这片地方的关系：{hint}")

    lines.append(
        "- **红线（必须遵守）**：店名、公园名、地标名可以是真的；但**不许**出现门牌号、楼栋单元、"
        "小区名、楼层、精确距离（「距我家 300 米」「3 号楼」都不行），也不许说「我家在 XX」。"
        "location 字段仍用那张英文地点表，表外的具体地点只写名字、不写地址；"
        "真被问到住哪，只说大概的区/片，别报能反查的门牌。"
    )
    return "\n".join(lines) + "\n"


def scrub_address(text: str) -> tuple[str, int]:
    """把一段文字里的住址级信息抹掉。返回 (新文本, 命中次数)。"""
    if not text or not isinstance(text, str):
        return text, 0
    hits = 0
    out = text
    for pattern, replacement in _SCRUB_PATTERNS:
        out, n = pattern.subn(replacement, out)
        hits += n
    if hits:
        out = _COLLAPSE[0].sub("附近", out)
        out = _COLLAPSE[1].sub(" ", out).strip()
    return out, hits


def scrub_schedule(plugin, data: dict) -> int:
    """生成完之后过一遍：正文 / 寄语 / 主题 / state 里的住址级信息抹掉。

    - 总开关关着 → 直接返回 0（与没做这功能时一模一样）
    - `local_geo_scrub` 关着 → 也返回 0
    返回命中次数（不记录原文，只记次数）。
    """
    if not isinstance(data, dict):
        return 0
    try:
        if not plugin.get_config("enable_local_geo", False):
            return 0
        if not plugin.get_config("local_geo_scrub", True):
            return 0
    except Exception:
        return 0

    total = 0

    for item in data.get("schedule") or []:
        if not isinstance(item, dict):
            continue
        item["activity"], n = scrub_address(str(item.get("activity") or ""))
        total += n
        for beat in item.get("beats") or []:
            if isinstance(beat, dict):
                beat["do"], n = scrub_address(str(beat.get("do") or ""))
                total += n

    for field in _TEXT_FIELDS:
        if field in data:
            data[field], n = scrub_address(str(data.get(field) or ""))
            total += n

    state = data.get("state")
    if isinstance(state, dict):
        for field in _STATE_FIELDS:
            if field in state:
                state[field], n = scrub_address(str(state.get(field) or ""))
                total += n
        for field in _STATE_LISTS:
            if isinstance(state.get(field), list):
                cleaned = []
                for line in state[field]:
                    new_line, n = scrub_address(str(line))
                    total += n
                    cleaned.append(new_line)
                state[field] = cleaned

    if total:
        logger.info(f"[晨光心语] 📍 本地环境参考：抹掉了 {total} 处住址级信息（门牌/小区一类）")
    return total


def ensure_block(prompt: str, text: str) -> str:
    """模板里没有 {local_geo} 占位符时，把画像补进提示词（补在「## 开始生成」之前）。"""
    if not text:
        return prompt
    if MARK in prompt:
        return prompt
    block = "\n" + text
    if "## 开始生成" in prompt:
        return prompt.replace("## 开始生成", block + "\n## 开始生成", 1)
    return prompt.rstrip() + "\n" + block
