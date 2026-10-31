"""配置默认值的**唯一来源**：`_conf_schema.json`。

为什么要有这个模块（v1.5.29）：

同一个默认值以前写了两遍 —— 一遍在 `_conf_schema.json`（设置页显示的），
一遍在每处 `get_config("键", 兜底)`（键缺失时真正用的）。两边不报错、不崩、
也看不出问题，所以已经漂了 12 处，最难看的一处是：

    schedule_validation.enabled  ← 设置页默认 true，代码兜底 false

结果就是：这个键一旦从配置里消失，日程验收会**静默关闭**，而设置页还显示「开启」。

现在改成：`get_config()` 在键缺失时，先来这里查 schema 登记的默认值，
查不到才用调用方自己传的兜底。这样「设置页写什么」和「缺键时用什么」只有一份。

注意：**只读、只做兜底**。配置里真有的值永远优先，本模块不参与写入。
"""

import json
from pathlib import Path
from typing import Any

from astrbot.api import logger

# 插件根目录下的 _conf_schema.json（本文件在 core/ 里，往上一层就是插件根）
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "_conf_schema.json"

_DEFAULTS: dict[str, Any] | None = None


def _flatten(node: dict, prefix: str = "", out: dict | None = None) -> dict:
    """把 schema 摊平成 {点号键: 默认值}。

    结构长这样：顶层键 → 要么是标量/列表（自己带 default），
    要么是 `{"type": "object", "items": {...}}`（要往 items 里钻）。
    """
    if out is None:
        out = {}
    for key, spec in (node or {}).items():
        if not isinstance(spec, dict):
            continue
        path = f"{prefix}{key}"
        items = spec.get("items")
        if spec.get("type") == "object" and isinstance(items, dict):
            _flatten(items, path + ".", out)
        else:
            out[path] = spec.get("default")
    return out


def _load() -> dict:
    global _DEFAULTS
    if _DEFAULTS is not None:
        return _DEFAULTS
    try:
        raw = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8-sig"))
        _DEFAULTS = _flatten(raw)
    except Exception as e:
        # 读不到就当没有：所有调用退回它们自己传的兜底（= 老行为），不让插件起不来
        logger.warning(f"[晨光心语] ⚠️ 读取配置默认值表失败，将退回各调用点的兜底: {e}")
        _DEFAULTS = {}
    return _DEFAULTS


def schema_default(key: str, fallback: Any = None) -> Any:
    """按点号键取 schema 里登记的默认值；没有这个键就返回 fallback。"""
    return _load().get(key, fallback)


def all_defaults() -> dict:
    """整张表（自检/排查用）。"""
    return dict(_load())
