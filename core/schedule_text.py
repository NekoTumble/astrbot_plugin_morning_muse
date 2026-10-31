"""日程正文的小工具：时段表情、地点串、以及「老日程里的时段重复」清理。

这三个都是「渲染之前」要用的纯字符串处理，命令（`/查看日程`）和使用者
（`/重写日程`、插件页面）共用同一份，免得两边各写一遍、以后又对不上。
"""

# 六个基准时段 + 几个常见同义词。只用于识别「开头是不是在报时段」。
PERIOD_LABELS = ("清晨", "上午", "午间", "下午", "傍晚", "晚间")
PERIOD_ALIASES = PERIOD_LABELS + ("早晨", "早上", "中午", "晚上", "深夜")

# 时段和正文之间常见的分隔符
_DUP_SEPARATORS = ("·", "・", "：", ":", "，", ",", "、", "-", "—", " ", "\u3000")

# 紧贴时段时，这几个字开头的多半是「下午茶」「晚间新闻」这类词，
# 摘掉时段反而会把词拆坏，所以留着不动。
_COMPOUND_TAIL = ("的", "地", "得", "茶", "饭", "餐", "新闻", "时", "间")

# 「家 + 屋里的细地点」= 重复，省掉笼统的那个（「公园/家」这种照留）
_HOME_SUB = {"balcony", "kitchen", "bedroom", "living room", "study nook", "garden"}


def period_emoji(time_str: str) -> str:
    """给一个时段名配个表情。认不出来的给 ⏰。"""
    text = str(time_str or "").lower()
    emoji_map = {
        "清晨": "🌄", "早晨": "🌅", "早上": "☀️",
        "上午": "📌", "午前": "📌",
        "中午": "🍽️", "午间": "🍽️", "午餐": "🍽️", "午后": "🍽️",
        "下午": "🌤️",
        "傍晚": "🌅", "黄昏": "🌇",
        "晚上": "🌙", "晚间": "🌙", "深夜": "🌃",
        "夜间": "🌃", "睡前": "🛌",
    }
    for keyword, emoji in emoji_map.items():
        if keyword in text:
            return emoji
    return "⏰"


def place_text(item: dict, location_zh) -> str:
    """这一段的地点文字。

    优先取 `beats` 里的细地点（阳台 / 厨房 / 客厅这种，去重、按出现顺序），
    beats 里没有才退回 `location`（家 / 图书馆）。
    一个时段跑两个地方是常态（「上午：咖啡馆 → 图书馆」），所以用 `/` 串起来：
    `阳台/厨房`、`咖啡馆/街上`。词表外的地方原样保留英文，不乱翻。

    `location_zh` 由调用方传进来（core.life_bridge 里那张对照表），
    这样这个模块不用反向依赖 life_bridge。
    """
    if not isinstance(item, dict):
        return ""
    places: list[str] = []
    for b in (item.get("beats") or []):
        if not isinstance(b, dict):
            continue
        at = str(b.get("at") or "").strip()
        if at and at not in places:
            places.append(at)
    if not places:
        loc = str(item.get("location") or "").strip()
        if loc:
            places.append(loc)
    if len(places) > 1 and "home" in places and all(
        p in _HOME_SUB or p == "home" for p in places
    ):
        places.remove("home")
    return "/".join(location_zh(p) for p in places)


def strip_legacy_period_prefix(activity: str, time_label: str = "") -> str:
    """把老日程正文开头多出来的「时段」摘掉（v1.5.29 的 H1 兼容）。

    为什么要这一步：`_format_for_injection()` 和 `/查看日程` 渲染时都会写
    「时段 · 正文」，而 v1.5.29 之前生成器写 `activity` 时**没人拦着**它
    开头再报一遍时段，于是出现 `晚间 · 晚间烟霾16°C，天黑了…` 这种重复。

    **只摘时段，不摘天气**：天气那半句是这一格的场景铺垫（实测六格里有五格
    是「阴，十几度…」开头的），摘掉反而把气氛削没了。

    v1.5.29 起提示词里已经写明「开头别报时段」，新生成的日程不会再带，
    所以这里只对**升级前已经落盘的老日程**生效，属于窄兼容，不是靠猜：

    1. 开头是「时段 + 分隔符」→ 摘掉（这是渲染层重复，最明确）；
    2. 开头紧贴着本格的时段、后面又不是「的/地/得/茶/饭…」这类字 →
       摘掉（`晚间烟霾16°C`、`上午做正事` 这种）。第二条只在时段与这一格
       自己的 `time` 对得上时才动手，别的时段词一律不碰。
    """
    text = (activity or "").lstrip()
    if not text:
        return text

    for _ in range(3):  # 「晚间 · 晚间…」这种套两层的，来回摘几遍
        changed = False

        # ① 时段 + 分隔符（分隔符可能连着好几个：「晚间 · 」「上午，」「中午: 」）
        for label in PERIOD_ALIASES:
            if not text.startswith(label):
                continue
            rest = text[len(label):]
            trimmed = rest.lstrip()
            for _ in range(3):
                if trimmed and trimmed[0] in _DUP_SEPARATORS:
                    trimmed = trimmed[1:].lstrip()
                else:
                    break
            if trimmed != rest:
                text = trimmed
                changed = True
            break
        if changed:
            continue

        # ② 本格时段紧贴正文
        own = (time_label or "").strip()
        if not own or not text.startswith(own):
            break
        rest = text[len(own):]
        if not rest or rest.startswith(_COMPOUND_TAIL):
            break
        text = rest.lstrip()
        if not text:
            break

    return text
