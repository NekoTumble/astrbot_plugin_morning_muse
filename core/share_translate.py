"""把底座写的英文「想跟你说的事」预译成中文（缓存 + 定时补新）。

**为什么要有这个模块**：底座（aura_life）给她生成的活动叙事是英文
（`Found some good songs and threw them in a playlist`），而插件要把它
塞进中文提示词、也得显示在中文页面上。实时翻译会让每轮对话都慢一拍，
所以在**空闲时预译 + 落盘缓存**：定时检查有没有新条目，有就批量翻一批，
运行时只查缓存（命中就用中文，没命中就退回英文原文，绝不阻塞）。

**翻译内容有质量**：这些句子不是随机模板，是模型为她那次具体活动写的，
还过了 `share_worthy` 与「她愿不愿意讲」两道判定 —— 所以值得保住细节。

缓存文件：`<plugin_data>/share_zh.json`
    { "<sha1 前16位>": {"zh": "淘到几首不错的歌，顺手扔进一个歌单", "ts": "2026-11-10T20:55:03"} }

对外接口：
- `zh_of(plugin, text)`     → 缓存里的中文；没有返回 ""
- `pending_texts(...)`      → 还没翻的原文（去重、按新到旧）
- `run_once(plugin, pids)`  → 跑一轮：查新增 → 翻 → 存缓存，返回统计
- `status(plugin)`          → 给页面/命令看的状态（总条数 / 今日新增 / 上次检查）
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from astrbot.api import logger

CACHE_NAME = "share_zh.json"
DEFAULT_BATCH = 40          # 一次发给模型多少条（别太大，回包好解析）
MAX_SCAN = 600              # 一轮最多扫多少条待翻（首次回填靠循环补完）
MAX_TEXT = 300              # 单条超长就截断（翻译用不着全文）


# ── 缓存读写 ────────────────────────────────────────────────────────


def key_of(text: str) -> str:
    return hashlib.sha1(str(text or "").strip().encode("utf-8")).hexdigest()[:16]


def _cache_path(plugin) -> Path | None:
    bridge = getattr(plugin, "life_bridge", None)
    root = getattr(bridge, "data_dir", None)
    if not root:
        return None
    return Path(root) / CACHE_NAME


def load(plugin) -> dict:
    path = _cache_path(plugin)
    if path is None or not path.exists():
        return {}
    try:
        data = json.load(io.open(path, encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"[晨光心语] ⚠️ 读分享译名缓存失败（当作空）: {e}")
        return {}


def save(plugin, cache: dict) -> bool:
    path = _cache_path(plugin)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        io.open(tmp, "w", encoding="utf-8").write(
            json.dumps(cache, ensure_ascii=False, indent=1)
        )
        tmp.replace(path)
        return True
    except Exception as e:
        logger.warning(f"[晨光心语] ⚠️ 写分享译名缓存失败: {e}")
        return False


def zh_of(plugin, text: str) -> str:
    """缓存里查中文；没有就空串（调用方自己决定退回原文还是占位）。"""
    if not text:
        return ""
    item = (load(plugin) if not isinstance(getattr(plugin, "_share_zh", None), dict)
            else plugin._share_zh).get(key_of(text))
    if isinstance(item, dict):
        return str(item.get("zh") or "")
    return str(item or "")


# ── 从底座库里取原文 ────────────────────────────────────────────────


def _db_path(plugin, persona_id: str) -> Path | None:
    bridge = getattr(plugin, "life_bridge", None)
    fn = getattr(bridge, "_db_path", None)
    if fn is None:
        return None
    try:
        return Path(fn(persona_id))
    except Exception:
        return None


def raw_texts(plugin, persona_id: str, limit: int = MAX_SCAN) -> list[str]:
    """该人格的全部「想跟你说的事」原文（新→旧，去重）。"""
    db = _db_path(plugin, persona_id)
    if db is None or not db.exists():
        return []
    out, seen = [], set()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "select content from shareable_experiences "
                "where content is not null and trim(content) <> '' "
                "order by id desc limit ?",
                (int(limit),),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        logger.debug(f"[晨光心语] 读分享原文失败（{persona_id}）: {e}")
        return []
    for (content,) in rows:
        t = str(content).strip()[:MAX_TEXT]
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def pending_texts(plugin, persona_ids, limit: int = MAX_SCAN) -> list[str]:
    cache = load(plugin)
    out, seen = [], set()
    for pid in persona_ids:
        for t in raw_texts(plugin, pid, limit=limit):
            k = key_of(t)
            if k in cache or t in seen:
                continue
            seen.add(t)
            out.append(t)
    return out


# ── 翻译 ────────────────────────────────────────────────────────────

_PROMPT = """把下面这些英文句子逐条翻译成中文。

要求：
- 一句对一句，**保持原来的意思和细节**，不要扩写、不要合并、不要加解释
- 口语化，像一个人随口讲自己刚做过什么；不要翻译腔、不要书面语、不要敬语
- 只输出一个 JSON 数组，元素是字符串，顺序与输入完全一致
- 不要输出任何多余文字、不要 markdown 代码块

输入：
{payload}"""


def _parse_translated(text: str, want: int) -> list[str]:
    """从模型回包里抠出字符串数组（容忍 ```json 包裹和多余说明）。"""
    s = str(text or "").strip()
    m = re.search(r"\[[\[\s\S]*\]", s)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                got = [str(x).strip() for x in arr if str(x).strip()]
                if got:
                    return got[:want]
        except Exception:
            pass
    # 退路：按行拆（每行去掉序号/引号/逗号）
    lines = []
    for raw in s.splitlines():
        line = raw.strip().rstrip(",").strip()
        line = re.sub(r'^[\d]+[.、)]\s*', "", line)
        line = line.strip('"“”\'「」 ')
        if line and not line.startswith("[") and not line.startswith("]"):
            lines.append(line)
    return lines[:want]


async def _provider(plugin):
    """借日程那边的主模型；没有就用备用。"""
    gen = getattr(plugin, "generator", None)
    if gen is None:
        return None, None
    try:
        p, m = await gen._resolve_provider_for_schedule()
        if p is not None:
            return p, m
    except Exception:
        pass
    try:
        return await gen._resolve_backup_provider()
    except Exception:
        return None, None


async def _call(provider, model: str, prompt: str) -> str:
    if model:
        resp = await provider.text_chat(prompt=prompt, model=model)
    else:
        resp = await provider.text_chat(prompt=prompt)
    if hasattr(resp, "completion_text"):
        return str(resp.completion_text or "")
    try:
        return str(resp["choices"][0]["message"]["content"])
    except Exception:
        return ""


async def translate_texts(plugin, texts: list[str], batch: int = DEFAULT_BATCH) -> dict:
    """翻一批，返回 {原文: 中文}。失败返回空 dict（调用方按失败处理）。"""
    got: dict[str, str] = {}
    if not texts:
        return got
    provider, model = await _provider(plugin)
    if provider is None:
        logger.warning("[晨光心语] ⚠️ 分享预翻译：没有可用模型，跳过（下轮再试）")
        return got
    for i in range(0, len(texts), max(1, batch)):
        chunk = texts[i:i + batch]
        payload = json.dumps(chunk, ensure_ascii=False, indent=0)
        try:
            raw = await _call(provider, model, _PROMPT.format(payload=payload))
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ 分享预翻译调用失败: {e}")
            await asyncio.sleep(1)
            continue
        arr = _parse_translated(raw, len(chunk))
        if len(arr) != len(chunk):
            logger.warning(
                f"[晨光心语] ⚠️ 分享预翻译条数对不上（要 {len(chunk)} 得 {len(arr)}），本批只收对得上的"
            )
        for src, zh in zip(chunk, arr):
            if zh and zh.strip():
                got[src.strip()] = zh.strip()
    return got


# ── 一轮：查新增 → 翻 → 存 ──────────────────────────────────────────


async def run_once(plugin, persona_ids, batch: int = DEFAULT_BATCH) -> dict:
    """跑一轮预翻译。返回 {pending, translated, failed, total}。"""
    cache = load(plugin)
    pend = pending_texts(plugin, persona_ids)
    stats = {"pending": len(pend), "translated": 0, "failed": False, "total": len(cache)}
    if not pend:
        plugin._share_zh_stat = {"at": datetime.now().strftime("%H:%M"), "new": 0}
        return stats
    got = await translate_texts(plugin, pend, batch=batch)
    if not got:
        stats["failed"] = True
        plugin._share_zh_stat = {"at": datetime.now().strftime("%H:%M"), "new": 0, "failed": True}
        return stats
    now = datetime.now().isoformat(timespec="seconds")
    for src, zh in got.items():
        cache[key_of(src)] = {"zh": zh, "ts": now}
    if save(plugin, cache):
        plugin._share_zh = cache
    stats["translated"] = len(got)
    stats["total"] = len(cache)
    plugin._share_zh_stat = {"at": now, "new": len(got)}
    logger.info(f"[晨光心语] 🌐 分享预翻译：新增 {len(got)} 条（缓存共 {len(cache)} 条）")
    return stats


def status(plugin) -> dict:
    cache = load(plugin)
    today = date.today().isoformat()
    today_new = 0
    for v in cache.values():
        ts = str((v or {}).get("ts") if isinstance(v, dict) else "")
        if ts.startswith(today):
            today_new += 1
    last = getattr(plugin, "_share_zh_stat", None) or {}
    return {
        "total": len(cache),
        "today_new": today_new,
        "last_at": str(last.get("at") or ""),
        "last_failed": bool(last.get("failed")),
    }
