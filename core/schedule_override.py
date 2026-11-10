"""临时改日程的补丁层（overrides）。

**为什么不改日报原件**：原件是「早上那一次生成」的留档，第二天算「昨天做了什么」、
算因果延续、算历史重复度都靠它。一旦被就地改写，等于篡改历史——第二天的判断全歪。
所以这里只在旁边记一层补丁，读的时候「原件 + 补丁」合成：删掉补丁文件就回到早上那份。

落盘位置：`data/plugin_data/astrbot_plugin_morning_muse/overrides/<persona>/<YYYY-MM-DD>.json`

```json
{
  "date": "2026-11-04",
  "calls": 1,
  "calls_night": 0,
  "items": [
    {
      "from_hour": 15, "until_hour": 18,
      "from_period": "下午", "until_period": "傍晚",
      "activity": "跟主人一起去湖边散步",
      "location": "park",
      "reason": "聊天里答应了一起出门",
      "source": "tool",
      "ts": "2026-11-04T15:12:03+08:00"
    }
  ],
  "predawn": {
    "activity": "在客厅陪他看电影",
    "location": "living room",
    "reason": "聊到一半说不睡了",
    "ts": "2026-11-04T02:12:03+08:00"
  }
}
```

- `items`：白天用。一条补丁覆盖 `from_period … until_period` 这几格（含两端），
  **不动已经过去的格**（合成时按当前时段再夹一次）。
- `predawn`：凌晨用。今天这份还没生成时只有一个「眼下这一段」，
  生成点一到整段作废（连字段一起删），同时它会作为「今天凌晨那一段」喂进那次生成。
- `calls` / `calls_night` 是**两本账**：白天 3 次、夜里 2 次，分开算，互不占用。
  `/撤销调整` 只清内容、留着计数（否则「改-撤-改」能把上限刷没）。

本模块只做读写与合成，不碰状态机、不碰注入排版、不碰生成。
"""

import asyncio
import datetime
import json
import os
import re
from pathlib import Path

import aiofiles

from astrbot.api import logger

from .life_bridge import _LOCATION_ZH, _PERIOD_HOUR
from .storage import ScheduleStorage
from .view_data import PERIOD_ORDER, period_index_of, schedule_time_of


# 地点：中文「公园」→ 英文 `park`。词表在 life_bridge 里，那边是正向表，这里反着用。
_LOCATION_KEY = {}
for _k, _v in _LOCATION_ZH.items():
    _LOCATION_KEY.setdefault(str(_v).strip(), _k)

# 六个时段的起始钟点 —— **直接跟随** life_bridge._PERIOD_HOUR（唯一出处），不在这儿另写一份。
# 以前这里写 6/9/12/15/18/21、life_bridge 写另一套，来回换会串格（2026-11-10 统一）。
# 只用来把时段名换成钟点写进文件，供排查和状态机换算；**判断一律走时段名**。
PERIOD_START_HOUR = dict(_PERIOD_HOUR)

# 注入里给「被补丁覆盖的那几格」加的那行淡注。
OVERRIDE_NOTE = "（这一段是刚在聊天里说好的，不是早上就排好的）"

# 默认额度（配置项覆盖）
DEFAULT_MAX_DAY = 3
DEFAULT_MAX_NIGHT = 3


# ── 路径与读写 ──────────────────────────────────────────────────────


def _root(plugin) -> Path:
    """补丁根目录：`<数据目录>/overrides`。

    数据目录的权威来源是插件初始化时那份 `data_dir`；拿不到就退回
    「日程目录的上一层」（schedules/ 就在它下面），不去猜工作目录。
    """
    base = getattr(plugin, "data_dir", None)
    if base:
        return Path(base) / "overrides"
    storage = getattr(plugin, "storage", None)
    if storage is not None and getattr(storage, "data_dir", None):
        return Path(storage.data_dir).parent / "overrides"
    return Path("data/plugin_data/astrbot_plugin_morning_muse/overrides")


def _dir_of(plugin, persona_id: str) -> Path:
    safe = ScheduleStorage._sanitize_persona_id(persona_id)
    return _root(plugin) / safe


def path_of(plugin, persona_id: str, day) -> Path:
    return _dir_of(plugin, persona_id) / f"{day.isoformat()}.json"


def empty(day) -> dict:
    return {
        "date": day.isoformat() if hasattr(day, "isoformat") else str(day),
        "calls": 0,
        "calls_night": 0,
        "items": [],
        # v1.5.31：凌晨那段是**一串**（她能分阶段排：先接着睡到 6 点、6 点起做早饭…）。
        # 早先存成单条的旧文件，load() 会把它包成只有一条的一串。
        "predawn": [],
    }


def _norm_predawn(raw) -> list:
    """把 predawn 归一成一串 dict（兼容早期「单条 dict」的老文件）。"""
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out = []
    for seg in raw:
        if not isinstance(seg, dict):
            continue
        act = str(seg.get("activity") or "").strip()
        if not act:
            continue
        try:
            frm = int(seg.get("from_hour"))
        except (TypeError, ValueError):
            continue
        try:
            til = int(seg.get("until_hour"))
        except (TypeError, ValueError):
            til = None
        out.append({
            "from_hour": max(0, min(23, frm)),
            "until_hour": (max(0, min(24, til)) if til is not None else None),
            "activity": act,
            "location": str(seg.get("location") or "").strip(),
            "reason": str(seg.get("reason") or "").strip()[:40],
            "ts": str(seg.get("ts") or ""),
        })
    # 同一个起点只留最后一条（她改口了），再按时间排
    by_from = {}
    for seg in out:
        by_from[seg["from_hour"]] = seg
    return [by_from[h] for h in sorted(by_from)]


async def load(plugin, persona_id: str, day) -> dict:
    """读当天补丁。文件不在、坏掉、形状不对 —— 一律当成「没补丁」，绝不抛。"""
    p = path_of(plugin, persona_id, day)
    if not p.exists():
        return empty(day)
    try:
        async with aiofiles.open(p, encoding="utf-8") as f:
            data = json.loads(await f.read())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"[晨光心语] 补丁读取失败（按无补丁处理）: {e}")
        return empty(day)
    if not isinstance(data, dict):
        return empty(day)
    out = empty(day)
    out.update({k: data.get(k) for k in ("calls", "calls_night", "items", "predawn")
                if k in data})
    if not isinstance(out.get("items"), list):
        out["items"] = []
    try:
        out["calls"] = max(0, int(out.get("calls") or 0))
    except (TypeError, ValueError):
        out["calls"] = 0
    try:
        out["calls_night"] = max(0, int(out.get("calls_night") or 0))
    except (TypeError, ValueError):
        out["calls_night"] = 0
    out["predawn"] = _norm_predawn(out.get("predawn"))
    return out


async def save(plugin, persona_id: str, day, data: dict) -> None:
    """原子写：先写 .tmp 再 os.replace（跟 storage 同一个套路，断电不会留半截文件）。"""
    p = path_of(plugin, persona_id, day)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, p)
    except OSError as e:
        raise RuntimeError(f"补丁写入失败: {e}")


def delete(plugin, persona_id: str, day) -> bool:
    """整份补丁删掉（回到早上那份）。返回是否真的删了。"""
    p = path_of(plugin, persona_id, day)
    try:
        if p.exists():
            p.unlink()
            return True
    except OSError as e:
        logger.warning(f"[晨光心语] 补丁删除失败: {e}")
    return False


# ── 时段与地点的小工具 ──────────────────────────────────────────────


def slot_of_hour(hour: int) -> str:
    """钟点 → 时段名（跟注入的切分同一个口径：<4 点算前一晚的晚间）。"""
    try:
        h = int(hour)
    except (TypeError, ValueError):
        h = 12
    return PERIOD_ORDER[period_index_of(h)]


def slot_index(label: str):
    """时段名 → 序号；不认识返回 None。"""
    s = str(label or "").strip()
    return PERIOD_ORDER.index(s) if s in PERIOD_ORDER else None


def slot_hour(label: str):
    return PERIOD_START_HOUR.get(str(label or "").strip())


def current_slot(now: datetime.datetime = None) -> tuple:
    """(时段名, 序号)：此刻落在哪一段。"""
    hour = (now or datetime.datetime.now()).hour
    label = slot_of_hour(hour)
    return label, slot_index(label)


def location_key(hint: str) -> str:
    """把「公园 / 客厅 / park」这类说法换成词表里的英文键；认不出返回空串。

    认不出的就**不填**（保持原件的地点），别自造一个词表外的键 —— 状态机那边
    认不得的词会让整格作废。
    """
    s = str(hint or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low in _LOCATION_ZH:
        return low
    if s in _LOCATION_KEY:
        return _LOCATION_KEY[s]
    # 「家里的客厅」「去公园走走」这种带几个字的，按包含关系兜一层；
    # 取最长的那个匹配，免得「家」把「客厅」吃掉。
    best, best_len = "", 0
    for zh, key in _LOCATION_KEY.items():
        if zh and zh in s and len(zh) > best_len:
            best, best_len = key, len(zh)
    return best


def max_day(plugin) -> int:
    try:
        v = int(plugin.get_config("schedule_override.max_per_day", DEFAULT_MAX_DAY))
    except (TypeError, ValueError):
        v = DEFAULT_MAX_DAY
    return max(1, v)


def max_night(plugin) -> int:
    try:
        v = int(plugin.get_config("schedule_override.max_predawn", DEFAULT_MAX_NIGHT))
    except (TypeError, ValueError):
        v = DEFAULT_MAX_NIGHT
    return max(1, v)


def enabled(plugin) -> bool:
    try:
        return bool(plugin.get_config("schedule_override.enabled", True))
    except Exception:
        return True


# ── 合成 ────────────────────────────────────────────────────────────


def apply(data: dict, ov: dict, cur_idx: int) -> dict:
    """把白天补丁铺到日程上，返回**新的** dict（原件那份对象不动）。

    规则（三条一起用，缺一条就会出乱子）：

    1. 只铺 `[from_period, until_period]` 这个窗口，窗口外一个字节都不碰；
    2. `cur_idx` 之前的格不铺 —— **写入侧**靠这一条把「不许把过去改掉」落死
       （工具里还会把起点夹到此刻）。**显示侧传 0**（见 `merge_all`）：
       过去那几格如果被改过，也应该显示改后的样子，否则用户事后看不出改没改。
    3. 被铺过的格打 `_ov=True`：注入据此加那行淡注，页面据此显示小标并允许切换看原件。
       它不是「活动名」，渲染时别把它当正文。
    """
    if not isinstance(data, dict):
        return data
    items = (ov or {}).get("items") or []
    if not isinstance(items, list) or not items:
        return data
    base = data.get("schedule") or []
    if not isinstance(base, list) or not base:
        return data
    out = [dict(it) if isinstance(it, dict) else it for it in base]
    try:
        cur = int(cur_idx)
    except (TypeError, ValueError):
        cur = 0

    for patch in items:
        if not isinstance(patch, dict):
            continue
        act = str(patch.get("activity") or "").strip()
        if not act:
            continue
        a = slot_index(patch.get("from_period"))
        if a is None:
            a = slot_index(slot_of_hour(patch.get("from_hour")))
        if a is None:
            continue
        b = slot_index(patch.get("until_period"))
        if b is None or b < a:
            b = a
        loc = str(patch.get("location") or "").strip()
        for i in range(max(a, cur), min(b, len(out) - 1) + 1):
            cell = out[i]
            if not isinstance(cell, dict):
                continue
            cell["activity"] = act
            if loc:
                cell["location"] = loc
            cell["_ov"] = True
    return {**data, "schedule": out}


def apply_today(plugin, schedule: dict | None, ov: dict,
                now: datetime.datetime = None, include_now: bool = True) -> dict | None:
    """合成入口（**按此刻截断**）：日程 + 当天补丁 → 合成结果。没有补丁就原样返回。

    `include_now=False` 时连「此刻」那一格也不改，只动「接下来」。
    注意：这是「写作/回灌」用的口径 —— 只关心从现在往后。
    要看「整天的真实样子」（含被改过的历史格），用 `merge_all`。
    """
    if not isinstance(schedule, dict) or not schedule:
        return schedule
    if not isinstance(ov, dict) or not (ov.get("items") or []):
        return schedule
    hour = (now or datetime.datetime.now()).hour
    cur = period_index_of(hour)
    return apply(schedule, ov, cur if include_now else cur + 1)


def merge_all(schedule: dict | None, ov: dict) -> dict | None:
    """合成入口（**整天全量**）：不管现在几点，补丁盖过哪几格就显示哪几格。

    给「展示」用：页面、注入。这样 15:00 改过的下午，到了晚上再看还是「改后的样子」，
    并且带着 `_ov` 标记，能看出这一天动过。没有补丁就原样返回（连拷贝都不做）。
    """
    if not isinstance(schedule, dict) or not schedule:
        return schedule
    if not isinstance(ov, dict) or not (ov.get("items") or []):
        return schedule
    return apply(schedule, ov, 0)


# ── 凌晨临时格 ──────────────────────────────────────────────────────


def predawn_window_open(plugin, now: datetime.datetime = None) -> bool:
    """凌晨那段「今天这份还没生成」的窗口还开着吗（= 还没到今天的生成点）。"""
    now = now or datetime.datetime.now()
    gen = schedule_time_of(plugin)
    return (now.hour, now.minute) < (gen.hour, gen.minute)


def parse_hour(text) -> int | None:
    """把「07:00」「7点」「7：30」「7」这类说法解析成一个整点；认不出返回 None。

    凌晨那段用的是**钟点**（她说的通常是"再睡到七点"），跟白天那六个模糊时段是两套。
    """
    s = str(text or "").strip()
    if not s:
        return None
    s = s.replace("：", ":").replace("．", ".").replace(".", ":")
    m = re.match(r"^(\d{1,2})(?::(\d{1,2}))?$", s)
    if not m:
        m = re.match(r"^(\d{1,2})\s*[点时]", s)
    if not m:
        return None
    try:
        h = int(m.group(1))
    except (TypeError, ValueError):
        return None
    return h if 0 <= h <= 23 else None


def parse_plan(raw) -> list:
    """把「一次排好几段」解析成 [{from_hour, activity}, …]。

    接受几种写法（模型给 array 最好，给成字符串也认）：
    - `["03:00 在客厅陪他坐着", "06:30 回卧室睡觉"]`
    - `"06:30 回卧室睡觉；07:30 起来做早饭"`（换行、分号、竖线都当分隔）
    - 每段前面必须有时刻（`06:30` / `6点` / `7`），后面那句就是这一段做什么。
    认不出时刻的那几条直接跳过 —— 宁可少排，也别把话排错。
    """
    items = []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if s.lstrip().startswith("["):
            try:
                raw = json.loads(s)
            except Exception:
                raw = re.split(r"[\n;；|]+", s)
        else:
            raw = re.split(r"[\n;；|]+", s)
    if not isinstance(raw, (list, tuple)):
        return []
    pat = re.compile(
        r"^(\d{1,2})(?:\s*[:：]\s*(\d{1,2}))?(?:\s*[点时](?:\s*(\d{1,2})\s*分?)?)?"
        r"\s*[，,：:]?\s*(.+)$"
    )
    for it in raw:
        text = str(it or "").strip()
        if not text:
            continue
        m = pat.match(text)
        if not m:
            continue
        try:
            hour = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if not (0 <= hour <= 23):
            continue
        body = str(m.group(4) or "").strip(" 　，,：:。")
        if not body:
            continue
        items.append({"from_hour": hour, "activity": body})
    return items


def segments_from_plan(plan_items: list, now_hour: int) -> list:
    """把 parse_plan 的结果排成顺序的几段：起点、终点（下一段的起点 / 空=到今天生成）。

    起点早于此刻的，一律夹到此刻（已经过去的排不了）。
    """
    clean = []
    for it in plan_items or []:
        if not isinstance(it, dict):
            continue
        try:
            h = int(it.get("from_hour"))
        except (TypeError, ValueError):
            continue
        act = str(it.get("activity") or "").strip()
        if not act:
            continue
        clean.append({"from_hour": max(h, int(now_hour)), "activity": act})
    clean.sort(key=lambda x: x["from_hour"])
    # 同一个起点只留最后一条
    dedup = {}
    for seg in clean:
        dedup[seg["from_hour"]] = seg
    ordered = [dedup[h] for h in sorted(dedup)]
    out = []
    for i, seg in enumerate(ordered):
        nxt = ordered[i + 1]["from_hour"] if i + 1 < len(ordered) else None
        out.append({**seg, "until_hour": nxt})
    return out


def predawn_segments(ov: dict) -> list:
    """她排的那几段，算好**实际生效区间**再返回。

    规则：按起点排序；某一行的终点如果越过下一行的起点，就截到下一行起点为止
    —— 于是「先接着睡到 7 点」＋「7 点起做早饭」会呈现成两段，而不是后一段把前一段吞掉。
    最后一段没写终点就一路铺到今天生成时间（由调用方给上限）。
    """
    segs = _norm_predawn((ov or {}).get("predawn"))
    out = []
    for i, seg in enumerate(segs):
        frm = seg["from_hour"]
        til = seg.get("until_hour")
        nxt = segs[i + 1]["from_hour"] if i + 1 < len(segs) else None
        if nxt is not None and (til is None or til > nxt):
            til = nxt
        if til is not None and til <= frm:
            continue          # 被后面那段完全盖掉了
        out.append({**seg, "until_hour": til})
    return out


def predawn_active(plugin, ov: dict, has_schedule: bool,
                   now: datetime.datetime = None) -> bool:
    """凌晨临时格此刻算不算数。

    三个条件缺一不可：① 今天这份还没生成；② 排了至少一段；③ 还没到今天的生成点。
    过了生成点就作废 —— 不管那次生成成没成功（生成失败等于今天没排，也就没得改）。
    """
    if has_schedule:
        return False
    if not isinstance(ov, dict) or not _norm_predawn(ov.get("predawn")):
        return False
    return predawn_window_open(plugin, now)


async def clear_predawn(plugin, persona_id: str, day) -> None:
    """把凌晨临时格抹掉（到生成点 / 已经喂过生成就删，不留残渣）。"""
    try:
        ov = await load(plugin, persona_id, day)
        if not ov.get("predawn"):
            return
        ov["predawn"] = []
        await save(plugin, persona_id, day, ov)
    except Exception as e:
        logger.debug(f"[晨光心语] 凌晨临时格清理失败（不影响运行）: {e}")


async def push_predawn(plugin, persona_id: str, activity: str,
                       location: str = "", until_hour: int = None,
                       from_hour: int = None,
                       now: datetime.datetime = None) -> list:
    """把「这一段起点 → 这一段终点」这几个整点铺成她说的那件事。

    `from_hour` 不给就从此刻这个整点起；`until_hour` 不给就铺到今天的生成点。
    一次排了好几段时，每段各调一次 —— 后一段从更晚的整点起铺，自然把前一段的后半截盖掉。

    跟 `life_bridge.align_schedule_slots` 用的是同一套私有 API（`_daily_planner
    .set_planned_activity`），区别只在按**小时区间**铺而不是按「时段三格」铺。
    睡眠窗口里的整点让路（那几格归「睡眠硬接管」）；任何失败都只降级，不改任何状态。
    """
    bridge = getattr(plugin, "life_bridge", None)
    if bridge is None:
        return []
    try:
        if not bridge.available():
            return []
        act = str(activity or "").strip()
        if not act:
            return []
        now = now or datetime.datetime.now()
        gen = schedule_time_of(plugin)
        if gen.hour <= now.hour:
            return []            # 窗口已经关了，别去铺白天
        start = now.hour
        if isinstance(from_hour, int) and now.hour <= from_hour <= 23:
            start = from_hour
        end = gen.hour
        if isinstance(until_hour, int) and now.hour < until_hour < end:
            end = until_hour
        if end <= start:
            return []

        svc = await bridge._get_service(persona_id)
        if svc is None:
            return []
        planner = getattr(svc, "_daily_planner", None)
        if planner is None:
            return []
        loc = str(location or "").strip() or "home"
        try:
            bridge._ensure_location(svc, loc)
        except Exception:
            pass
        touched = []

        def _do():
            for h in range(start, min(end, 24)):
                try:
                    if bridge._hour_in_sleep_window(persona_id, h, now):
                        continue
                    planner.set_planned_activity(h, act, location=loc, reason="chat")
                    touched.append(f"{h:02d}:{act}@{loc}")
                except Exception as e:
                    logger.debug(f"[晨光心语] 凌晨铺 {h:02d} 点失败: {e}")

        await asyncio.to_thread(_do)
        return touched
    except Exception as e:
        logger.warning(f"[晨光心语] ⚠️ 凌晨临时格铺进状态机失败（已降级）: {e}")
        return []


async def push_window(plugin, persona_id: str, from_period: str, until_period: str,
                      activity: str, location: str = "",
                      now: datetime.datetime = None) -> list:
    """把白天补丁覆盖的那几段铺进状态机的计划表（「页面改了、人还在图书馆」的解药）。

    直接复用 `life_bridge.align_schedule_slots` —— 「只铺当前及以后」「睡眠窗口让路」
    「词表外的地点自动注册」这三条都由它把关，这里只负责把窗口翻译成它认得的 items。
    """
    bridge = getattr(plugin, "life_bridge", None)
    act = str(activity or "").strip()
    if bridge is None or not act:
        return []
    a = slot_index(from_period)
    b = slot_index(until_period)
    if a is None:
        return []
    if b is None or b < a:
        b = a
    loc = str(location or "").strip() or "home"
    items = [
        {"time": PERIOD_ORDER[i], "location": loc,
         "beats": [{"do": act, "at": loc}]}
        for i in range(a, b + 1)
    ]
    hour = (now or datetime.datetime.now()).hour
    try:
        return await bridge.align_schedule_slots(persona_id, items, now_hour=hour)
    except Exception as e:
        logger.warning(f"[晨光心语] ⚠️ 临时调整铺进状态机失败（已降级）: {e}")
        return []


def predawn_note(ov: dict) -> str:
    """凌晨注入里追加的一行（她自己的口吻，不出现「日程/补丁」这类词）。

    排了一段就一句话；排了好几段就按「到几点 / 几点起」串起来 —— 别让她以为
    这一段到天亮都是同一件事。
    """
    segs = predawn_segments(ov)
    if not segs:
        return ""
    parts = []
    for i, seg in enumerate(segs):
        act = seg["activity"]
        where = _LOCATION_ZH.get(seg.get("location") or "", "")
        # 她自己写的那句话里要是已经带了地方（「在客厅陪他看电影」），别再叠一次
        text = act if (not where or where in act) else f"在{where}{act}"
        frm, til = seg["from_hour"], seg.get("until_hour")
        if len(segs) == 1:
            parts.append(text)
            break
        if i == len(segs) - 1 and til is None:
            parts.append(f"{frm:02d}:00 之后{text}")
        elif i == len(segs) - 1:
            parts.append(f"{frm:02d}:00–{til:02d}:00 {text}")
        else:
            parts.append(f"到 {til:02d}:00 {text}")
    return "（刚在聊天里说好的：" + "；".join(parts) + "）"


def predawn_prompt_block(ov: dict) -> str:
    """喂给当天那次生成的一段参考（模板里没有占位符，靠「开始生成」之前补进去）。"""
    segs = predawn_segments(ov)
    if not segs:
        return ""
    lines = ["## 今天凌晨那一段（参考）"]
    if len(segs) == 1:
        seg = segs[0]
        where = _LOCATION_ZH.get(seg.get("location") or "", "")
        act = seg["activity"]
        # 活动正文里已经有这个地点时，别再补一遍括号
        tail = f"（{where}）" if where and where not in act else ""
        lines.append(f"今天凌晨你还醒着，跟他说好的是：{act}{tail}。")
    else:
        lines.append("今天凌晨你还醒着，跟他说好的是这么排的：")
        for i, seg in enumerate(segs):
            where = _LOCATION_ZH.get(seg.get("location") or "", "")
            frm, til = seg["from_hour"], seg.get("until_hour")
            place = f"（{where}）" if where and where not in seg["activity"] else ""
            if i == len(segs) - 1 and til is None:
                lines.append(f"- {frm:02d}:00 起：{seg['activity']}{place}")
            elif i == len(segs) - 1:
                lines.append(f"- {frm:02d}:00–{til:02d}:00：{seg['activity']}{place}")
            else:
                lines.append(f"- 到 {til:02d}:00：{seg['activity']}{place}")
    lines.append("排今天的日程时把这段接上，别再排出跟它打架的安排。")
    return "\n".join(lines)
