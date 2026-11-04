"""插件页面（`views/晨光心语/`）的取数层。

这一层的唯一目的：**页面和命令共用同一份数据**。
`/状态`、`/晨光报告` 里已经算好的那些东西（精力/心情/此刻在做/没做完的事、
开关一览/作息/模型/今天生成了没），原来只活在两条命令的拼字符串代码里；
页面要用同一套，就得把「取数」和「排版」拆开——

- 本模块只负责**取数**，返回纯 dict，不拼任何给人看的文字；
- 命令（`main.py` / `commands/debug.py`）拿这份 dict 拼文字；
- 页面拿同一份 dict 端点，交给 `views/晨光心语/index.html` 渲染。

这样以后改一处口径，两边一起变，不会出现「网页上是 62、命令里是 58」。

只读：本模块除了 `regenerate_today()`（等价 `/重写日程`，由页面显式点按钮触发）
之外，不写任何东西。
"""

import datetime

from astrbot.api import logger

from .life_bridge import activity_zh, energy_zh, location_zh, mood_zh
from .schedule_text import period_emoji, place_text, strip_legacy_period_prefix
from .state_store import StateStore

# 六个模糊时段 → 大约钟点（和天气、状态机用的是同一套）
PERIOD_ORDER = ["清晨", "上午", "午间", "下午", "傍晚", "晚间"]

# 页面「插件配置」块里的开关一览：配置键 → 中文名。
# 只放「看一眼就知道是什么」的开关 —— 像「按时段切分」那种要解释的，不上页面。
_SWITCHES = (
    ("life_engine.enabled", "内在状态", False),
    ("proactive.enabled", "主动消息", False),
    ("bridge.enabled", "记忆搭桥", False),
    ("causal_check.enabled", "因果自检", False),
    ("schedule_validation.enabled", "日程验收", True),
    ("event_ledger.enabled", "生活账本", False),
    ("weather.weather_enabled", "天气参考", False),
    # J1：只显示开关状态，页面上不提供编辑（避免多一个写入面）
    ("local_geo.enabled", "本地环境参考", False),
)


def period_index_of(hour: int) -> int:
    """把 0~23 点钟映射到六个时段的第几段。

    凌晨（<4 点）归到前一晚的「晚间」——深夜聊天时她该在收尾，不是新的一天刚开始。
    """
    try:
        hour = int(hour)
    except (TypeError, ValueError):
        hour = 12
    if hour < 4:
        return 5
    bounds = [4, 8, 11, 13, 17, 19]
    idx = 0
    for i, b in enumerate(bounds):
        if hour >= b:
            idx = i
    return idx


def _schedule_path(plugin, persona_id: str, day):
    """日程文件路径（用来读「生成于几点」）。

    走的是 storage 自己的路径规则 —— 它做过 persona_id 清洗，这里不重复实现。
    """
    try:
        return plugin.storage._get_path(persona_id, day)
    except Exception:
        return None


def schedule_time_of(plugin) -> datetime.time:
    """每日生成时间（配置里是 "08:00" 这种字符串）。"""
    raw = str(plugin.get_config("schedule.schedule_time", "08:00") or "08:00")
    raw = raw.strip().replace("：", ":")
    try:
        hh, mm = raw.split(":")[:2]
        return datetime.time(int(hh), int(mm))
    except Exception:
        return datetime.time(8, 0)


def before_generate_time(plugin, now: datetime.datetime) -> bool:
    """现在是否还没到今天的生成时间（= 今天这份日程本来就还不该存在）。"""
    st = schedule_time_of(plugin)
    return (now.hour, now.minute) < (st.hour, st.minute)


def _items_of(data: dict, cur: int, origin: dict = None) -> list[dict]:
    """把日程整理成页面用的一格一条。

    `origin` 是**原件**（补丁还没盖上去的那份）：被改过的格子除了正文，还会带上
    `activity_orig` / `place_orig`，页面据此在那个小标上点击切换「改后的 ⇄ 原本的」。
    """
    items = []
    src = data.get("schedule") or []
    orig_list = (origin or {}).get("schedule") or []
    for i, item in enumerate(src):
        if not isinstance(item, dict):
            continue
        label = str(item.get("time") or "")
        idx = PERIOD_ORDER.index(label) if label in PERIOD_ORDER else min(i, 5)
        overridden = bool(item.get("_ov"))
        entry = {
            "time": label,
            "emoji": period_emoji(label),
            "activity": strip_legacy_period_prefix(str(item.get("activity") or ""), label),
            "place": place_text(item, location_zh),
            "is_now": idx == cur,
            "is_past": idx < cur,
            # v1.5.31：聊天里临时改过的那几格给个标记（页面据此显示「XX 改的」并允许切换）
            "overridden": overridden,
        }
        if overridden:
            o = orig_list[i] if i < len(orig_list) and isinstance(orig_list[i], dict) else None
            entry["activity_orig"] = (
                strip_legacy_period_prefix(str(o.get("activity") or ""), label)
                if o else entry["activity"]
            )
            entry["place_orig"] = place_text(o, location_zh) if o else entry["place"]
            entry["activity_changed"] = entry["activity_orig"] != entry["activity"]
        items.append(entry)
    return items


def _mtime_of(plugin, persona_id: str, day) -> str:
    try:
        path = _schedule_path(plugin, persona_id, day)
        if path is not None and path.exists():
            return datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%H:%M")
    except OSError:
        pass
    return ""


async def build_schedule_view(plugin, persona_id: str, now: datetime.datetime = None,
                        allow_fallback: bool = True) -> dict:
    """今日日程（页面「📅 今日日程」那块 + 重写成功后刷新用）。

    `allow_fallback`：今天这份还没生成、而**现在也还没到生成时间**（比如凌晨 3 点）时，
    把**昨天那份**一并带回去（`fallback=True`），页面拿它灰着显示 ——
    否则每天 0 点到 08:00 页面都只会干巴巴一句「今天还没有日程」，其实她还在昨晚的夜里。
    """
    today = plugin._get_today()
    data = await plugin.storage.load_schedule(persona_id, today)
    now = now or plugin._get_now()
    cur = period_index_of(now.hour)
    gen_at = schedule_time_of(plugin).strftime("%H:%M")

    if not data:
        view = {
            "exists": False,
            "today": today.isoformat(),
            "current_index": cur,
            "items": [],
            "outfit": "",
            "outfit_style": "",
            "note": "",
            "intent": "",
            "generated_at": "",
            "next_generate_at": gen_at,
            "before_generate": before_generate_time(plugin, now),
            "fallback": False,
            "fallback_from": "",
        }
        if allow_fallback and view["before_generate"]:
            yesterday = today - datetime.timedelta(days=1)
            old = await plugin.storage.load_schedule(persona_id, yesterday)
            if old:
                # 回落显示的也是「那一天」，所以那天的补丁照样要盖上：
                # 不然昨晚被改过的格一到 0 点就变回原件，「XX 改了」也跟着不见了。
                origin_old = old
                try:
                    from .schedule_override import load as _load_override
                    from .schedule_override import merge_all as _merge_all

                    _ov_y = await _load_override(plugin, persona_id, yesterday)
                    old = _merge_all(old, _ov_y) or old
                except Exception:
                    pass
                view.update(
                    {
                        "items": _items_of(old, cur, origin=origin_old),
                        "outfit": str(old.get("outfit") or ""),
                        "outfit_style": str(old.get("outfit_style") or ""),
                        "note": str(old.get("note") or ""),
                        "intent": str(old.get("intent") or ""),
                        "generated_at": _mtime_of(plugin, persona_id, yesterday),
                        "fallback": True,
                        "fallback_from": yesterday.isoformat(),
                    }
                )
        return view

    # v1.5.31：聊天里临时改过的几段盖上来（只影响页面显示，原件不写）。
    # 用 merge_all（整天全量）而不是 apply_today：过去的格子也要显示「改后的样子」，
    # 否则到了晚上就只剩原件，看不出这一天动过。原件单独留着给页面做切换。
    # 局部 import —— schedule_override 反过来要用本模块的 PERIOD_ORDER/period_index_of。
    origin = data
    try:
        from .schedule_override import load as _load_override, merge_all as _merge_all

        _ov = await _load_override(plugin, persona_id, today)
        data = _merge_all(data, _ov) or data
    except Exception:
        pass

    return {
        "exists": True,
        "today": today.isoformat(),
        "current_index": cur,
        "items": _items_of(data, cur, origin=origin),
        "outfit": str(data.get("outfit") or ""),
        "outfit_style": str(data.get("outfit_style") or ""),
        "note": str(data.get("note") or ""),
        "intent": str(data.get("intent") or ""),
        "generated_at": _mtime_of(plugin, persona_id, today),
        "next_generate_at": gen_at,
        "before_generate": False,
        "fallback": False,
        "fallback_from": "",
    }


def in_sleep_window(plugin, persona_id: str) -> bool:
    """现在是否在她的睡眠窗口里（睡懒觉会顺延）。

    跟「睡眠硬接管」（life_bridge.enforce_sleep_slot）用的是同一个判断 ——
    所以不会出现「状态机钉成睡觉、页面说她还在放松」这种自相矛盾。
    """
    bridge = getattr(plugin, "life_bridge", None)
    if bridge is None:
        return False
    try:
        return bool(bridge.in_sleep_window_now(persona_id))
    except Exception:
        return False


def _display_of(live) -> dict:
    """状态机的原始码 → 页面直接能显示的中文。没有状态就返回空 dict。"""
    if not live:
        return {}
    energy = live.get("energy_effective")
    if energy is None:
        energy = live.get("energy")
    try:
        energy = float(energy or 0.0)
    except (TypeError, ValueError):
        energy = 0.0
    return {
        "energy": round(max(0.0, min(1.0, energy)) * 100),
        "energy_base": live.get("energy"),
        "energy_label": energy_zh(live.get("energy_label", "")),
        "location": location_zh(live.get("location", "")),
        "activity": activity_zh(live.get("current_activity", "")),
        "mood": mood_zh(live.get("mood", "")),
        "mood_intensity": live.get("mood_intensity"),
        "thought": live.get("thought", ""),
        "goals": live.get("goals") or [],
        "season": live.get("season", ""),
        "location_desc": live.get("location_desc", ""),
        "chaos_today": live.get("chaos_today", 0),
    }


async def collect_status(plugin, persona_id: str) -> dict:
    """「🫀 此刻状态」那块的原料 —— 就是 `/状态` 背后那一套。

    force=True：人刚打开页面 / 刚敲命令，要的是**此刻**，绕过状态机那 40 秒缓存。
    """
    life_on = bool(plugin.get_config("life_engine.enabled", False))
    out = {
        "persona_id": persona_id,
        "sleeping": False,
        "enabled": bool(life_on and plugin.life_bridge is not None),
        "live": None,
        "live_display": {},
        "shares": [],
        "wake_lag": 0,
        "night_interrupts": 0,
        "state": {},
        "state_summary": "",
        "threads_text": "",
        "open_threads": [],
        "carried_over": [],
        "ledger": None,
        "memory": None,
    }

    if out["enabled"]:
        try:
            out["live"] = await plugin.life_bridge.get_state(persona_id, force=True)
        except Exception as e:
            logger.warning(f"[晨光心语] {persona_id} 读取状态失败: {e}")
        # 给页面用的中文版：状态机吐的是 park / content / resting 这种码，
        # 命令侧一直靠 activity_zh() 这些翻译，页面也走同一套，别让两边口径跑偏。
        out["live_display"] = _display_of(out["live"])
        try:
            out["shares"] = await plugin.life_bridge.shareable(persona_id, limit=3)
        except Exception:
            out["shares"] = []
        try:
            out["wake_lag"] = plugin.life_bridge.wake_lag_minutes(persona_id)
            if out["wake_lag"] > 0:
                out["night_interrupts"] = plugin.life_bridge.night_interrupt_count(
                    persona_id
                )
        except Exception:
            out["wake_lag"] = 0

    # Phase 0 的 state 块（开不开状态机都有）
    try:
        sched = await plugin.storage.load_schedule(persona_id, plugin._get_today())
        state = (sched or {}).get("state") or {}
        if isinstance(state, dict) and state:
            out["state"] = state
            out["state_summary"] = StateStore.summarize(state)
            out["threads_text"] = StateStore.threads_text(state, limit=3)
            threads = state.get("open_threads")
            if isinstance(threads, list):
                out["open_threads"] = [str(x) for x in threads if str(x).strip()][:3]
            carried = state.get("carried_over")
            if isinstance(carried, list):
                out["carried_over"] = [str(x) for x in carried if str(x).strip()][:3]
    except Exception:
        pass

    try:
        if plugin.event_ledger is not None:
            out["ledger"] = await plugin.event_ledger.stats(persona_id)
    except Exception:
        out["ledger"] = None
    try:
        if plugin.memory_source is not None:
            out["memory"] = await plugin.memory_source.stats(persona_id)
    except Exception:
        out["memory"] = None

    out["sleeping"] = in_sleep_window(plugin, persona_id)
    return out


def collect_switches(plugin) -> list[dict]:
    """开关一览（页面「⚙️ 插件配置」那些小圆点）。"""
    switches = []
    for path, label, default in _SWITCHES:
        switches.append(
            {
                "key": path,
                "label": label,
                "on": bool(plugin.get_config(path, default)),
                "extra": "",
            }
        )

    # 外部记忆库是「后端字符串」，不是布尔
    backend = str(plugin.get_config("external_memory.backend", "off") or "off").strip()
    switches.append(
        {
            "key": "external_memory.backend",
            "label": "外部记忆库",
            "on": backend not in ("", "off"),
            "extra": backend if backend not in ("", "off") else "",
        }
    )

    # 天气这一格把城市带上，省得再问「查的哪儿」
    city = str(plugin.get_config("weather.weather_location", "") or "").strip()
    for row in switches:
        if row["key"] == "weather.weather_enabled" and row["on"] and city:
            row["extra"] = city

    # 本地环境参考：开着的话把「附近的地方从哪查」也标出来（只显示，不在这里改）
    backend = str(plugin.get_config("local_geo.map", "off") or "off").strip().lower()
    label = {"amap": "高德", "osm": "OSM"}.get(backend, "")
    for row in switches:
        if row["key"] == "local_geo.enabled" and row["on"] and label:
            row["extra"] = label
    return switches


async def collect_report(plugin, include_prompts: bool = True) -> dict:
    """「⚙️ 插件配置」+ `/晨光报告` 背后那一套。

    注意：这里**不复制粘贴**命令里的拼串逻辑，命令改成拿这份 dict 再拼。
    """
    today = plugin._get_today()
    report = {
        "today": today.isoformat(),
        "schedule_time": str(plugin.get_config("schedule.schedule_time", "08:00")),
        "timezone": str(plugin.get_config("schedule.timezone", "Asia/Shanghai")),
        "model": str(plugin.get_config("schedule.schedule_model", "") or ""),
        "model_backup": str(
            plugin.get_config("schedule.schedule_model_backup", "") or ""
        ),
        "holiday": "",
        "static_personas": [],
        "dynamic_styles": dict(getattr(plugin, "dynamic_styles", {}) or {}),
        "persona_ids": [],
        "prompt_status": [],
        "generated": {},      # persona_id -> True / False / "error"
        "generated_at": {},   # persona_id -> "08:03"
        "logs": list(getattr(plugin, "debug_logs", []) or []),
        "switches": collect_switches(plugin),
        # 内存里最近 100 条生成日志里的告警/报错条数。
        # 说明：debug_logs 只在内存里、每行只有 HH:MM 没有日期，所以这里
        # 不做「今日」过滤 —— 就叫「近期告警」，别装成今日失败次数。
        "warn_count": sum(
            1
            for e in (getattr(plugin, "debug_logs", []) or [])
            if str(e.get("level", "")).lower() in ("warning", "error")
        ),
        "wake": "",
        "bedtime": "",
        "life_enabled": bool(plugin.get_config("life_engine.enabled", False)),
    }

    try:
        report["holiday"] = plugin.context_builder._get_holiday_info(today) or ""
    except Exception as e:
        report["holiday"] = f"(获取失败: {e})"

    for i in range(1, 4):
        name = str(plugin.get_config(f"persona_styles.persona_name_{i}", "") or "").strip()
        style = str(
            plugin.get_config(f"persona_styles.style_control_{i}", "") or ""
        ).strip()
        if name:
            report["static_personas"].append({"name": name, "style": style})

    try:
        all_ids = plugin.context_builder.get_known_persona_ids() or []
    except Exception:
        all_ids = []
    report["persona_ids"] = [str(x) for x in all_ids]

    for pid in (report["persona_ids"] if include_prompts else []):
        try:
            prompt = await plugin.context_builder._get_persona_prompt(pid)
            report["prompt_status"].append(
                {"id": pid, "ok": bool(prompt), "length": len(prompt or "")}
            )
        except Exception as e:
            report["prompt_status"].append(
                {"id": pid, "ok": False, "error": str(e), "length": 0}
            )

    for pid in report["persona_ids"]:
        try:
            schedule = await plugin.storage.load_schedule(pid, today)
        except Exception:
            report["generated"][pid] = "error"
            continue
        report["generated"][pid] = bool(schedule)
        if schedule:
            path = _schedule_path(plugin, pid, today)
            try:
                if path is not None and path.exists():
                    report["generated_at"][pid] = datetime.datetime.fromtimestamp(
                        path.stat().st_mtime
                    ).strftime("%H:%M")
            except OSError:
                pass

    if report["life_enabled"]:
        report["wake"] = str(plugin.get_config("life_engine.wake_time", "07:00") or "")
        report["bedtime"] = str(
            plugin.get_config("life_engine.bedtime_time", "23:00") or ""
        )

    return report


def _override_card_row(cell: dict, orig: dict = None) -> dict:
    """「动过的地方」里的一行：改后的正文 + 原件（页面点标签就地切换用）。"""
    label = str((cell or {}).get("time") or "")
    act = strip_legacy_period_prefix(str((cell or {}).get("activity") or ""), label)
    o = orig if isinstance(orig, dict) else {}
    act_orig = strip_legacy_period_prefix(str(o.get("activity") or ""), label) if o else act
    return {
        "slot": label,
        "emoji": period_emoji(label),
        "activity": act,
        "activity_orig": act_orig,
        "place": place_text(cell or {}, location_zh),
        "place_orig": place_text(o, location_zh) if o else place_text(cell or {}, location_zh),
    }


async def build_override_card(plugin, persona_id: str, now: datetime.datetime = None) -> dict:
    """页面「✏️ 动过的地方」那张卡的内容（2026-11-05 加）。

    按「**今天这份有没有生成**」切换，切换时刻就是生成时间，不写死某个钟点：

    - 有今天日程 → `mode="day"`：只列被改过的那几段（页面上带「小夜改的」，可点切原件）；
    - 没有今天日程 → `mode="dawn"`：列凌晨临时排的那几段（带「夜晚活动」）；
      只在「还没到今天的生成时间」时算数 —— 过了生成点它本来就作废了。
    """
    today = plugin._get_today()
    now = now or plugin._get_now()
    try:
        from .schedule_override import load as _load_ov
        from .schedule_override import merge_all as _merge_all
        from .schedule_override import predawn_segments, predawn_window_open

        ov = await _load_ov(plugin, persona_id, today)
    except Exception:
        return {"mode": "dawn", "items": []}

    data = await plugin.storage.load_schedule(persona_id, today)
    if data:
        merged = _merge_all(data, ov) or data
        base = data.get("schedule") or []
        items = []
        for i, cell in enumerate(merged.get("schedule") or []):
            if not isinstance(cell, dict) or not cell.get("_ov"):
                continue
            orig = base[i] if i < len(base) and isinstance(base[i], dict) else {}
            items.append(_override_card_row(cell, orig))
        return {"mode": "day", "items": items}

    items = []
    try:
        window_open = predawn_window_open(plugin, now)
    except Exception:
        window_open = False
    if window_open:
        for seg in predawn_segments(ov):
            til = seg.get("until_hour")
            items.append({
                "slot": "",
                "emoji": "🌙",
                "range": (f"{seg['from_hour']:02d}:00–{til:02d}:00"
                          if til is not None else f"{seg['from_hour']:02d}:00 之后"),
                "activity": str(seg.get("activity") or ""),
                "place": place_text({"location": seg.get("location")}, location_zh),
            })
    return {"mode": "dawn", "items": items}


async def build_overview(plugin, persona_id: str = "") -> dict:
    """页面一打开要的全部东西：三块 + 人格清单 + 服务端时间。"""
    now = plugin._get_now()
    today = plugin._get_today()

    try:
        active = [str(x) for x in (await plugin._get_active_persona_ids() or [])]
    except Exception:
        active = []
    if not active:
        active = [str(plugin._get_default_persona_id())]

    pid = str(persona_id or "").strip()
    if pid not in active:
        pid = active[0]

    personas = []
    for item in active:
        try:
            style = plugin.get_style_for_persona(item)
        except Exception:
            style = ""
        personas.append({"id": item, "style": style})

    # 页面只用到「开关 / 作息 / 模型 / 今日生成状态」，其余字段（日志、人格提示词状态、
    # 静态人格表…）是给 /晨光报告 用的，别塞进页面响应：包更小，也少一层暴露面。
    config = await collect_report(plugin, include_prompts=False)
    for drop in ("logs", "prompt_status", "persona_ids", "static_personas", "dynamic_styles"):
        config.pop(drop, None)

    return {
        "ok": True,
        "today": today.isoformat(),
        "weekday": "星期" + "一二三四五六日"[today.weekday()],
        "server_time": now.strftime("%H:%M"),
        "server_date": now.strftime("%Y年%m月%d日"),
        "persona": pid,
        "persona_style": next(
            (p["style"] for p in personas if p["id"] == pid), ""
        ),
        "personas": personas,
        "schedule": await build_schedule_view(plugin, pid, now=now),
        "override": await build_override_card(plugin, pid, now=now),
        "status": await collect_status(plugin, pid),
        "config": config,
    }


def _failure_hint(plugin) -> str:
    """失败时把最近一条告警捞出来当附件理由 —— 别让人只看到「失败了」。"""
    logs = list(getattr(plugin, "debug_logs", []) or [])
    for entry in reversed(logs[-6:]):
        if str(entry.get("level", "")).lower() in ("warning", "error", "warning:", "error:"):
            msg = str(entry.get("msg", "")).strip()
            if msg:
                return msg
    return ""


async def regenerate_today(plugin, persona_id: str, recent_messages=None) -> tuple:
    """等价 `/重写日程` 的那一跳：**只重写这个人格的今天**。

    返回 (成功?, 说明文字, 新鲜日程 dict 或 None)。
    - 显式覆盖，不走「今天只生成一次」的缓存短路（force=True）；
    - 生成完照常写缓存、照常记日志 —— 跟命令走的是同一条路；
    - 失败把原因照实说出来（主模型失败 / 备用也失败 / 解析不出来），绝不静默当成功。
    """
    if plugin.generator is None or plugin.storage is None:
        return False, "插件还在初始化，稍等几秒再试", None

    if recent_messages is None:
        try:
            recent_messages = (
                plugin.chat_cache.get(persona_id) if plugin.chat_cache else []
            )
        except Exception:
            recent_messages = []

    try:
        data = await plugin.generator.generate(
            persona_id,
            getattr(plugin, "dynamic_styles", {}) or {},
            recent_messages=list(recent_messages or []),
            force=True,
        )
    except Exception as e:
        return False, f"生成时出错：{type(e).__name__}: {e}", None

    if isinstance(data, Exception):
        return False, f"生成失败：{type(data).__name__}: {data}", None
    if not data:
        hint = _failure_hint(plugin)
        tail = f"（最近一条：{hint}）" if hint else ""
        return False, f"生成失败：模型没返回能解析的日程{tail}", None

    return True, "已重写", await build_schedule_view(plugin, persona_id)


# ── 命令用的排版（页面不调这里，页面自己渲染） ──────────────────────────

def render_status_text(data: dict) -> str:
    """`/状态` 的正文。逐行保持 v1.5.28 的老样子，只是数据换成 collect_status() 的。"""
    pid = data.get("persona_id", "")
    live = data.get("live")
    lines = []

    if live:
        lines.append(f"🫀 {pid} 的此刻状态")
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append(
            f"精力：{live.get('energy')}"
            f"（实际 {live.get('energy_effective', live.get('energy'))}"
            f" · {energy_zh(live.get('energy_label', ''))}）"
        )
        lines.append(
            f"心情：{mood_zh(live.get('mood', ''))}（强度 {live.get('mood_intensity')}）"
        )
        if live.get("current_activity"):
            lines.append(f"此刻在做：{activity_zh(live['current_activity'])}")
        if live.get("location"):
            lines.append(f"所在：{location_zh(live['location'])}")
        if live.get("goals"):
            lines.append("惦记的目标：" + "；".join(live["goals"]))
        if live.get("thought"):
            lines.append(f"脑子里在想：{live['thought']}")
        shares = data.get("shares") or []
        if shares:
            lines.append("最近想说的：")
            for s in shares:
                lines.append(f"  · {s.get('content', '')}")
        lag = data.get("wake_lag") or 0
        if lag > 0:
            lines.append(
                f"🛌 昨晚被吵醒 {data.get('night_interrupts', 0)} 次，今早多睡了 {lag} 分钟"
            )
    else:
        lines.append(f"🫀 {pid} 的内在状态未开启")
        lines.append("（可以在设置页打开「内在状态」，下面是日程里记着的状态）")

    if data.get("state_summary"):
        lines.append("")
        lines.append("📌 今天记下的状态")
        lines.append(data["state_summary"])
        if data.get("threads_text"):
            lines.append("还没了结的事：")
            lines.append(data["threads_text"])

    ledger = data.get("ledger")
    if ledger:
        lines.append(f"📒 生活账本：共 {ledger['total']} 条，还没了结 {ledger['open']} 条")
    memory = data.get("memory") or {}
    if memory.get("external") and memory.get("external") != "off":
        mode = f"（{memory['external_mode']}）" if memory.get("external_mode") else ""
        lines.append(f"🌐 外部记忆库：{memory['external']}{mode}")
    if memory.get("pool"):
        lines.append(f"🌉 记忆池：{memory['pool']} 条（兜底）")

    return "\n".join(lines)


def render_report_text(report: dict) -> str:
    """`/晨光报告` 的正文。同样逐行保持老样子。"""
    lines = []
    lines.append("📊 晨光心语 · 运行报告")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"⏰ 定时生成：{report.get('schedule_time', '08:00')}")
    lines.append(
        f"🤖 生成模型：{report.get('model') or '(使用默认)'}"
    )
    lines.append(
        f"🛟 备用兜底：{report.get('model_backup') or '(回落到对话默认)'}"
    )
    lines.append(f"🎉 今日节日：{report.get('holiday') or '(无节日)'}")

    lines.append("\n👤 已配置人格（静态）：")
    statics = report.get("static_personas") or []
    if statics:
        for i, item in enumerate(statics, start=1):
            lines.append(f"  {i}️⃣ {item['name']} → {item['style'] or '(无风格)'}")
    else:
        lines.append("  (未配置)")

    dynamics = report.get("dynamic_styles") or {}
    if dynamics:
        lines.append("\n🔧 动态绑定人格：")
        for name, style in dynamics.items():
            lines.append(f"  {name} → {style or '(无风格)'}")
    else:
        lines.append("\n🔧 动态绑定人格：(无)")

    ids = report.get("persona_ids") or []
    lines.append(f"\n📋 所有人格标识：{', '.join(ids)}")

    lines.append("\n📚 人格提示词加载状态：")
    for row in report.get("prompt_status") or []:
        if row.get("error"):
            lines.append(f"  ❌ {row['id']} (获取失败: {row['error']})")
        elif row.get("ok"):
            lines.append(f"  ✅ {row['id']} (长度: {row['length']})")
        else:
            lines.append(f"  ⚠️ {row['id']} (未找到)")

    lines.append(f"\n📅 今日日程状态 ({report.get('today', '')})：")
    for pid in ids:
        state = (report.get("generated") or {}).get(pid)
        if state == "error":
            lines.append(f"  ❌ {pid}：读取失败")
        elif state:
            lines.append(f"  ✅ {pid}：已生成")
        else:
            lines.append(f"  ❌ {pid}：未生成")

    logs = report.get("logs") or []
    if logs:
        lines.append("\n💬 近期生成日志（最近5条）：")
        for entry in logs[-5:]:
            lines.append(
                f"  [{entry.get('time', '')}] "
                f"{str(entry.get('level', '')).upper()}: {entry.get('msg', '')}"
            )
    else:
        lines.append("\n💬 近期生成日志：(暂无)")

    return "\n".join(lines)
