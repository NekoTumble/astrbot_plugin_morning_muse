#!/usr/bin/env python3
"""日程 → 状态机 · 接管自检（离线，副本上跑，**不碰线上库**）

用法：
    cd <插件目录> && python3 docs/verify_schedule_align.py

    也可用环境变量指定（不设则自动探测）：
    MUSE_PID=p_xxxxxxxxxxxx python3 docs/verify_schedule_align.py
    MUSE_DATA_DIR=/path/to/plugin_data python3 docs/verify_schedule_align.py

检查内容（对应 CHANGELOG v1.5.11）：
    ① 兜底归一：地点/活动的中文、英文变体、缺字段、超 3 个节拍
    ② 节拍分摊：1 个铺满 3 格 / 2 个 2+1 / 3 个一格一个
    ③ 铺格子：到点的时段真的写进了 planner 的活动表
    ④ 提前铺未来时段、跳过已经过完的时段
    ⑤ 睡眠让路：睡眠窗口里的小时不铺（归「睡眠硬接管」管）
    ⑥ 自动注册：词表外的地名写进地点名册

> 教训（2026-10-02）：**日志全绿 ≠ 生效**，验收必须落在数据上。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

# 离线自检引导：没有 AstrBot 时给插件模块一个最小 logger 替身
# （插件本体统一 `from astrbot.api import logger`，不写回退分支；这里补上现场）
try:
    import astrbot.api  # noqa: F401
except ImportError:  # pragma: no cover - 只在脱离 AstrBot 的环境里走到
    import logging as _logging
    import types as _types

    _logging.basicConfig(level=_logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    _api = _types.ModuleType("astrbot.api")
    _api.logger = _logging.getLogger("muse-selfcheck")
    _pkg = _types.ModuleType("astrbot")
    _pkg.api = _api
    sys.modules["astrbot"] = _pkg
    sys.modules["astrbot.api"] = _api

from core.life_bridge import LifeBridgeManager  # noqa: E402
from core.generator import ScheduleGenerator    # noqa: E402

# 数据目录默认按插件位置推：<主目录>/data/plugin_data/<插件名>，也可用 MUSE_DATA_DIR 覆盖
_DATA_DEFAULT = PLUGIN_DIR.parents[1] / "plugin_data" / PLUGIN_DIR.name
DATA_SRC = Path(os.environ.get("MUSE_DATA_DIR", str(_DATA_DEFAULT)))
PID = os.environ.get("MUSE_PID", "").strip()

if not PID:
    # 没指定就自动挑一个现存人格（p_ 开头），挑不到就留空
    # 先看数据目录下的 p_* 目录，再退一步看 persona_defs/p_*.json（插件实际落盘的样子）
    cand = []
    if DATA_SRC.exists():
        cand = sorted(p.name for p in DATA_SRC.glob("p_*") if p.is_dir())
        if not cand:
            defs = DATA_SRC / "persona_defs"
            if defs.is_dir():
                cand = sorted(p.stem for p in defs.glob("p_*.json"))
    PID = cand[0] if cand else ""

PASS, FAIL = [], []


def check(name: str, ok: bool, extra: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}{(' — ' + extra) if extra else ''}")


class DummyPlugin:
    def get_config(self, key, default=None):
        return default

    def get_style_for_persona(self, pid):
        return ""

    def __getattr__(self, name):
        def _f(*a, **k):
            return None
        return _f


CASES = [
    {"time": "清晨", "activity": "阴，出门时飘着小雨", "location": "家里",
     "beats": [{"do": "冲个澡", "at": "卧室"}]},
    {"time": "上午", "activity": "图书馆靠窗，画草图、翻书、记笔记", "location": "library",
     "beats": [{"do": "sketching ideas", "at": "library"},
               {"do": "reading", "at": "library"},
               {"do": "journaling", "at": "library"}]},
    {"time": "午间", "activity": "图书馆旁边的咖啡角", "beats": [{"do": "喝咖啡", "at": "咖啡角"}]},
    {"time": "下午", "activity": "继续在图书馆整理，翻了会儿书"},
    {"time": "傍晚", "activity": "从图书馆出来，天还没黑"},
    {"time": "晚间", "activity": "回到家，洗澡换睡裙", "location": "home",
     "beats": [{"do": "taking a bath", "at": "家"}, {"do": "reading", "at": "bedroom"},
               {"do": "sleeping", "at": "home"}, {"do": "x", "at": "home"},
               {"do": "y", "at": "home"}]},
]


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="muse_align_check_"))
    if DATA_SRC.exists():
        shutil.copytree(DATA_SRC, work / "data")
        root = work / "data"
    else:
        root = work
    print(f"工作副本：{root}（生产库没动）\n")

    print("① 兜底归一（generator._normalize_schedule_tags）")
    gen = ScheduleGenerator.__new__(ScheduleGenerator)
    out = gen._normalize_schedule_tags(json.loads(json.dumps({"schedule": CASES})))
    items = out["schedule"]
    check("中文地点「家里」→ home", items[0]["location"] == "home", items[0]["location"])
    check("中文活动「冲个澡」→ morning shower",
          items[0]["beats"] and items[0]["beats"][0]["do"] == "morning shower")
    check("beats 的 at 优先于散文（咖啡角→cafe）", items[2]["location"] == "cafe", items[2]["location"])
    check("缺 location 时从散文找线索（图书馆）", items[3]["location"] == "library", items[3]["location"])
    check("缺 beats 时从散文推活动（整理→tidying up）",
          items[3]["beats"] and items[3]["beats"][0]["do"] == "tidying up")
    check("英文变体 taking a bath → morning shower",
          items[5]["beats"] and items[5]["beats"][0]["do"] == "morning shower")
    check("超过 3 个节拍被采样成 3 个", len(items[5]["beats"]) <= 3, str(len(items[5]["beats"])))

    print("\n② 节拍分摊（_spread_beats）")
    s1 = LifeBridgeManager._spread_beats([{"do": "a"}], 3)
    s2 = LifeBridgeManager._spread_beats([{"do": "a"}, {"do": "b"}], 3)
    s3 = LifeBridgeManager._spread_beats([{"do": "a"}, {"do": "b"}, {"do": "c"}], 3)
    check("1 个 → 铺满 3 格", len(s1) == 3 and s1[0] == s1[2])
    check("2 个 → 2+1", [x["do"] for x in s2] == ["a", "a", "b"])
    check("3 个 → 一格一个", [x["do"] for x in s3] == ["a", "b", "c"])

    async def run():
        if not PID:
            print("  ⚠️ 没有可用人格（未设 MUSE_PID，也没在数据目录里找到 p_* 人格），跳过 ③~⑥")
            return

        m = LifeBridgeManager(DummyPlugin(), root)
        svc = await m._get_service(PID)

        real_hour = datetime.datetime.now().hour
        sleep_hours = {h for h in range(24) if m._hour_in_sleep_window(PID, h)}
        print(f"  （真实时间 {real_hour} 点；睡眠窗口内的小时：{sorted(sleep_hours)}）")

        PERIOD = [("清晨", 6), ("上午", 9), ("午间", 12), ("下午", 15), ("傍晚", 18), ("晚间", 21)]
        target = None
        for nm, st in PERIOD:              # 优先当前时段
            if st <= real_hour < st + 3:
                target = (nm, st); break
        if target is None:                 # 否则第一个未来的
            for nm, st in PERIOD:
                if st > real_hour:
                    target = (nm, st); break
        name, st = target
        expect = [h for h in (st, st + 1, st + 2) if h >= real_hour and h not in sleep_hours]

        print(f"\n③ 铺格子（目标时段「{name}」，预计可铺 {expect}）")
        items = [{"time": name, "activity": "…", "location": "library",
                  "beats": [{"do": "sketching ideas", "at": "library"},
                            {"do": "reading", "at": "library"},
                            {"do": "journaling", "at": "library"}]}]
        touched = LifeBridgeManager._align_sync(svc, items, real_hour, sleep_hours)
        print("   实际铺了:", touched)
        got = sorted(int(t.split(":")[0]) for t in touched)
        check("该铺的格都铺了", got == expect, f"期望 {expect}，得到 {got}")
        check("没有假账（没碰过去了的小时）", all(h >= real_hour for h in got))

        plan = svc._daily_planner.current_plan
        if not expect:
            # 目标时段的格子全都不可铺（已过完 / 撞睡眠窗口），内容断言无从检查
            print("  ⚠️ 目标时段当格不可铺，跳过「写进活动表」的内容断言")
        else:
            s0 = plan.get_slot_for_hour(expect[0])
            # 节拍是摊在 start/+1/+2 三格上的，当前时刻可能已经吃掉前几格，
            # 所以要按偏移量取「这一格本来该铺哪一拍」，不能写死第一拍
            spread = LifeBridgeManager._spread_beats(items[0]["beats"], 3)
            want = str(spread[min(expect[0] - st, len(spread) - 1)].get("do") or "").strip()
            check("真的写进了活动表", s0 is not None and s0.activity_name == want,
                  f"{getattr(s0, 'activity_name', '?')} @ {getattr(s0, 'location', '?')}"
                  f"（该格应铺「{want}」）")

        print("\n④ 已过完的时段不铺")
        t_passed = LifeBridgeManager._align_sync(
            svc, [{"time": "清晨", "activity": "…", "location": "home",
                   "beats": [{"do": "reading", "at": "home"}]}], real_hour, set())
        check("清晨（已过完）没被铺", (real_hour < 6) or not t_passed, str(t_passed))

        print("\n⑤ 睡眠让路（把目标时段的 3 格全标成睡眠小时 → 应该一格都不铺）")
        t_sleep = LifeBridgeManager._align_sync(svc, items, real_hour, {st, st + 1, st + 2})
        check("睡眠小时的格被让开", not t_sleep, str(t_sleep))

        print("\n⑥ 自动注册（词表外的地名）")
        before = set(svc._location_registry.keys())
        LifeBridgeManager._ensure_location(svc, "植物园")
        check("「植物园」写进了地点名册", "植物园" in set(svc._location_registry.keys()) - before)

    asyncio.run(run())

    print("\n" + "=" * 46)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败：", "、".join(FAIL))
    shutil.rmtree(work, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
