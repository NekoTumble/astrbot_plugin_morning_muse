#!/usr/bin/env python3
"""晨光心语 · 状态机本体 · 离线自检

    ⚠️ 分工说明：本脚本只管**状态机本体**——读档、精力落盘、关键词识别、
    活动真的落进 activity_logs。**日程回灌（铺格子）不在这里验**，
    那部分自 v1.5.11 起归 `docs/verify_schedule_align.py`。

用法（在插件根目录）：
    python3 docs/verify_life_replay.py

特点：
- **完全不碰线上数据**：全程在临时目录里造一个全新的 life.db。
- 只加载 `core/life_bridge.py`，不需要 AstrBot 在跑、不需要重载插件。
- 全绿退出码 0，有失败退出码 1。

为什么要有这个：2026-10-02 那次「精力不落盘」，代码看着没问题、日志也没报错，
但精力其实从来没写进过数据库。教训是——**日志全绿不等于生效，得落在数据上**。
这个脚本验的就是数据。
"""

import asyncio
import importlib
import pathlib
import sqlite3
import sys
import tempfile
import types

HERE = pathlib.Path(__file__).resolve().parent      # docs/
PLUGIN = HERE.parent                                 # 插件根
ASTRBOT = PLUGIN.parents[2]                          # AstrBot 主目录
if str(ASTRBOT) not in sys.path:
    sys.path.insert(0, str(ASTRBOT))

# 把插件当包挂进 sys.modules（插件根没有 __init__.py，不能直接 import）
for name, path in (("mmp", PLUGIN), ("mmp.core", PLUGIN / "core")):
    mod = types.ModuleType(name)
    mod.__path__ = [str(path)]
    sys.modules[name] = mod
lb = importlib.import_module("mmp.core.life_bridge")

CFG = {
    "schedule.timezone": "Asia/Shanghai",
    "life_engine.persona_type": "ai",
    "life_engine.wake_time": "07:00",
    "life_engine.schedule_replay": True,
}


class _StubPlugin:
    def get_config(self, key, default=None):
        return CFG.get(key, default)

    def get_style_for_persona(self, pid):
        return "soft"


_ok = True


def check(label, cond, extra=""):
    global _ok
    print(("  ✅ " if cond else "  ❌ ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        _ok = False


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mmp_verify_")
    mgr = lb.LifeBridgeManager(_StubPlugin(), pathlib.Path(tmp), max_personas=3)
    if not mgr.available():
        print("⚠️ 没装上 aura-life，跳过（这不代表失败）")
        return 0

    pid = "p_verify"
    svc = mgr._build_service(pid)
    db = str(mgr._db_path(pid))

    def energy_rows():
        c = sqlite3.connect(db)
        n = c.execute("SELECT COUNT(*) FROM life_energy_state").fetchone()[0]
        c.close()
        return n

    # ── Step 0.5：状态接续 ──
    print("\n=== Step 0.5 · 状态接续 ===")
    check("读档后 life_energy_state 有行（精力能落盘）", energy_rows() == 1, f"rows={energy_rows()}")
    check("_ensure_energy_row 幂等：已有行就不动", mgr._ensure_energy_row(svc) is False)

    from aura_life import LifeService
    old = LifeService(db_path=db, definition=mgr._load_definition(pid), persona_id=pid)
    check("对照：不读档直接重建 → 精力回到默认 0.7",
          abs(old._energy.state.level - 0.7) < 1e-6, f"{old._energy.state.level:.2f}")

    svc._energy._state.level = 0.35
    svc._save_state()
    svc2 = mgr._build_service(pid)
    check("重启读档后精力仍是 0.35（不再飘回 0.7）",
          abs(svc2._energy.state.level - 0.35) < 1e-6, f"{svc2._energy.state.level:.2f}")

    # ── Step 1：关键词识别 ──
    print("\n=== Step 1 · 关键词识别 ===")
    cases = [
        ("热了碗南瓜汤，然后午休半小时", "午间", "napping"),
        ("睡了个午觉", "午间", "napping"),
        ("在垫子上做了会儿瑜伽", "上午", "yoga"),
        ("拉伸一下僵掉的腰", "下午", "stretching"),
        ("下楼散步，看银杏", "下午", "going for a walk"),
        ("收拾一下，早点睡觉", "晚间", "sleeping"),
        ("本小姐宣布午睡时间开始，谁叫都不理", "午间", "napping"),
        ("爬起来也没去拿瑜伽垫，裹着开衫坐窗边", "清晨", ""),  # 否定 → 不认（今天就踩了这个）
        ("不睡了，起来看书", "晚间", ""),                      # 否定 → 不认
        ("早点睡觉", "上午", ""),        # 时段不对 → 不认
        ("去超市买了菜", "下午", ""),     # 没收录 → 不认
    ]
    for text, slot, want in cases:
        got = lb._match_replay_activity(text, slot)
        check(f"{slot}「{text}」→ {got or '(不认)'}", got == want, f"want={want or '(不认)'}")

    # ── Step 1：真的做一次 ──
    print("\n=== Step 1 · 活动真的落到状态机上 ===")
    before = svc2._energy.state.level
    check("napping 执行成功", mgr._perform_activity_sync(svc2, "napping") is True)
    check("精力真的动了", svc2._energy.state.level > before,
          f"{before:.2f} -> {svc2._energy.state.level:.2f}")
    c = sqlite3.connect(db)
    logs = c.execute("SELECT activity_name FROM activity_logs ORDER BY id DESC LIMIT 3").fetchall()
    c.close()
    check("activity_logs 里真的写进去了", any(r[0] == "napping" for r in logs), f"{logs}")
    check("不存在的活动会被拒", mgr._perform_activity_sync(svc2, "no_such_activity") is False)

    # 回灌（铺格子）的断言不在这里 —— 见 docs/verify_schedule_align.py
    items = [
        {"time": "午间", "activity": "热了碗南瓜汤，然后午休半小时"},
        {"time": "晚间", "activity": "收拾一下，早点睡觉"},
        {"time": "下午", "activity": "下楼散步，看银杏"},
    ]

    # ── 开关关闭 = 零影响 ──
    print("\n=== 关掉开关 = 回到从前 ===")
    CFG["life_engine.schedule_replay"] = False
    pid3 = "p_verify3"
    check("开关关掉后回灌为空", asyncio.run(mgr.replay_schedule(pid3, items, now_hour=23)) == [])
    check("且不会建库/建服务", pid3 not in mgr._services)
    CFG["life_engine.schedule_replay"] = True

    print("\n" + ("🎉 全部通过" if _ok else "⚠️ 有失败项，先别急着说好了"))
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
