#!/usr/bin/env python3
"""体检工具 · 状态机活动名的中文覆盖度（离线，只读）

底座（aura_life）每造一个新活动名，页面「此刻 · 正在」就会显示它。
译名表 `life_bridge._ACTIVITY_ZH` 没收录的会**原样显示英文** ——
2026-11-10 体检就抓到 10 个（含 6 个偏私密的），所以留这个工具定期扫一遍。

用法（在 NAS 上跑，读的是真实库，只 SELECT 不写）：

    cd /AstrBot/data/plugins/astrbot_plugin_morning_muse
    MUSE_DATA_DIR=/AstrBot/data/plugin_data/astrbot_plugin_morning_muse python docs/audit_activity_zh.py

缺哪些就照这个格式补进 `_ACTIVITY_ZH`：

    "making tea": "泡茶",

退出码：0 = 全覆盖；1 = 有未收录（打印清单）。
"""
from __future__ import annotations

import glob
import os
import sqlite3
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

try:
    from core.life_bridge import _ACTIVITY_ZH
except Exception as e:  # pragma: no cover
    print(f"⚠️ 导入 life_bridge 失败：{e}")
    raise SystemExit(2)


def data_root() -> Path | None:
    env = os.environ.get("MUSE_DATA_DIR", "").strip()
    if env:
        return Path(env)
    # 常见位置：插件目录同级的 plugin_data
    for cand in (PLUGIN_DIR.parent / "plugin_data", Path("/AstrBot/data/plugin_data")):
        if cand.exists():
            return cand
    return None


def main() -> int:
    root = data_root()
    if root is None:
        print("⚠️ 找不到数据目录，用 MUSE_DATA_DIR 指定")
        return 2
    dbs = sorted(glob.glob(str(Path(root) / "**" / "life.db"), recursive=True))
    if not dbs:
        print(f"⚠️ {root} 下没有 life/life.db")
        return 2

    names: dict[str, int] = {}
    for db in dbs:
        try:
            con = sqlite3.connect(db)
            for (n,) in con.execute("select activity_name from activity_logs"):
                if n:
                    k = str(n).strip()
                    names[k] = names.get(k, 0) + 1
            con.close()
        except sqlite3.Error as e:
            print(f"  （跳过 {db}: {e}）")

    known = {k.lower() for k in _ACTIVITY_ZH}
    missing = {n: c for n, c in names.items() if n.lower() not in known}

    print(f"历史里出现过的活动名：{len(names)} 个 ｜ 译名表收录：{len(known)} 条")
    if not missing:
        print("✅ 全部有中文译名")
        return 0

    print(f"❌ 有 {len(missing)} 个没收录（页面会原样显示英文）：")
    for name, cnt in sorted(missing.items(), key=lambda x: -x[1]):
        print(f'   "{name}": "…",        # 出现 {cnt} 次')
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
