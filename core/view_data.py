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


async def build_schedule_view(plugin, persona_id: str, now: datetime.datetime = None) -> dict:
    """今日日程（页面「📅 今日日程」那块 + 重写成功后刷新用）。"""
    today = plugin._get_today()
    data = await plugin.storage.load_schedule(persona_id, today)
    now = now or plugin._get_now()
    cur = period_index_of(now.hour)

    if not data:
        return {
            "exists": False,
            "today": today.isoformat(),
            "current_index": cur,
            "items": [],
            "outfit": "",
            "outfit_style": "",
            "note": "",
            "intent": "",
            "generated_at": "",
        }

    items = []
    for i, item in enumerate(data.get("schedule") or []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("time") or "")
        idx = PERIOD_ORDER.index(label) if label in PERIOD_ORDER else min(i, 5)
        items.append(
            {
                "time": label,
                "emoji": period_emoji(label),
                "activity": strip_legacy_period_prefix(
                    str(item.get("activity") or ""), label
                ),
                "place": place_text(item, location_zh),
                "is_now": idx == cur,
                "is_past": idx < cur,
            }
        )

    generated_at = ""
    path = _schedule_path(plugin, persona_id, today)
    try:
        if path is not None and path.exists():
            generated_at = datetime.datetime.fromtimestamp(
                path.stat().st_mtime
            ).strftime("%H:%M")
    except OSError:
        generated_at = ""

    return {
        "exists": True,
        "today": today.isoformat(),
        "current_index": cur,
        "items": items,
        "outfit": str(data.get("outfit") or ""),
        "outfit_style": str(data.get("outfit_style") or ""),
        "note": str(data.get("note") or ""),
        "intent": str(data.get("intent") or ""),
        "generated_at": generated_at,
    }


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
