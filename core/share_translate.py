"""把底座写的英文「想跟你说的事」预译成中文（**按人格隔离** + 定时补新）。

**为什么要有这个模块**：底座（aura_life）给她生成的活动叙事是英文
（`Found some good songs and threw them in a playlist`），而插件要把它塞进中文提示词、
也得显示在中文页面上。实时翻译会让每轮对话慢一拍，所以**空闲时预译 + 落盘缓存**：
定时检查新增，有就批量翻一批；运行时只查缓存（命中用中文，没命中退回英文原文，绝不阻塞）。

**为什么按人格隔离**（2026-11-10 改）：同一句英文，两个人的语气本来不同。
缓存键从「句子哈希」改成「人格 + 句子哈希」，翻译时把**人格名和她说话的风格**一起给模型，
并要求用第一人称 / 省略主语（不用「她」）—— 这样小红书那 116 句共用句子也会各写一份。

缓存文件：`<plugin_data>/share_zh.json`
    { "<sha1(人格|句子) 前16位>": {"zh": "…", "ts": "…", "pid": "小夜"} }

旧版（不分人格）的键没有 `pid` 字段：仍然作为**兜底**使用（新译文没到之前先顶着），
一旦该人格自己的译文到位就会被优先取用。

对外接口：
- `zh_of(plugin, text, persona_id)` → 该人格的中文（没有则退回旧版共用译文，再没有返回 ""）
- `pending_items(plugin, pids)`    → [(人格, 原文)]，只列还没翻的
- `run_once(plugin, pids)`         → 跑一轮：查新增 → 按人格分组翻 → 存缓存
- `status(plugin, persona_id)`     → 给页面看的状态（该人格条数 / 今日新增 / 上次检查）
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
CACHE_VER = 3              # 翻译提示词变了就 +1，旧译文会被当成"待翻"重写
DEFAULT_BATCH = 40          # 一次发给模型多少条（别太大，回包好解析）
MAX_SCAN = 600              # 一轮最多扫多少条待翻（首次回填靠循环补完）
MAX_TEXT = 300              # 单条超长就截断（翻译用不着全文）


# ── 键与缓存 ────────────────────────────────────────────────────────


def key_of(persona_id: str, text: str) -> str:
    """人格 + 句子 → 缓存键（同句子在不同人格下是两条）。"""
    raw = f"{str(persona_id or '').strip()}\x00{str(text or '').strip()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def legacy_key_of(text: str) -> str:
    """旧版（不分人格）的键，只用来兜底。"""
    return hashlib.sha1(str(text or "").strip().encode("utf-8")).hexdigest()[:16]


def _cache_path(plugin) -> Path | None:
    bridge = getattr(plugin, "life_bridge", None)
    root = getattr(bridge, "data_dir", None)
    if not root:
        return None
    return Path(root) / CACHE_NAME


def load(plugin) -> dict:
    cached = getattr(plugin, "_share_zh", None)
    if isinstance(cached, dict):
        return cached
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
        plugin._share_zh = cache
        return True
    except Exception as e:
        logger.warning(f"[晨光心语] ⚠️ 写分享译名缓存失败: {e}")
        return False


def _zh_of_entry(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("zh") or "")
    return str(entry or "")          # 兼容旧格式（值直接是字符串）


def zh_of(plugin, text: str, persona_id: str = "") -> str:
    """该人格的中文译文；没有就退回旧版共用译文；再没有返回空串。"""
    if not text:
        return ""
    cache = load(plugin)
    if persona_id:
        got = _zh_of_entry(cache.get(key_of(persona_id, text)))
        if got:
            return got
    return _zh_of_entry(cache.get(legacy_key_of(text)))


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


def pending_items(plugin, persona_ids, limit: int = MAX_SCAN) -> list[tuple[str, str]]:
    """还没翻的 [(人格, 原文)]。旧版共用译文**不算**已翻 —— 要按人格重写一份。"""
    cache = load(plugin)
    out = []
    for pid in persona_ids:
        pid = str(pid or "").strip()
        if not pid:
            continue
        for t in raw_texts(plugin, pid, limit=limit):
            got = cache.get(key_of(pid, t))
            if isinstance(got, dict) and int(got.get("ver") or 1) >= CACHE_VER:
                continue
            if got and not isinstance(got, dict):
                continue        # 旧格式（值直接是字符串）也重写
            out.append((pid, t))
    return out


# ── 翻译 ────────────────────────────────────────────────────────────

_PROMPT = """你在替一个角色，把她今天记下的英文生活片段改写成中文。这些句子之后会给她自己看，
她可能顺着这个由头跟你聊起来。

角色：{name}{style_line}

要求：
- 一句对一句，**保留原来的意思和细节**，不扩写、不合并、不加解释
- 用她的口吻：第一人称或省略主语，**绝对不要用「她」**；口语、短句，不要翻译腔和书面语
- 上面那份设定只是让你抓语感：**不要出现设定里的任何专有名词、人名、地名、情节或描写**，
  也不要把设定里的句子搬进来；译文只讲下面这些英文句子本身说的事
- 只输出一个 JSON 数组，元素是字符串，顺序与输入完全一致
- 不要输出任何多余文字、不要 markdown 代码块

输入：
{payload}"""


def _parse_translated(text: str, want: int) -> list[str]:
    """从模型回包里抠出字符串数组（容忍 ```json 包裹和多余说明）。"""
    s = str(text or "").strip()
    m = re.search(r"\[[\s\S]*\]", s)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                got = [str(x).strip() for x in arr if str(x).strip()]
                if got:
                    return got[:want]
        except Exception:
            pass
    lines = []
    for raw in s.splitlines():
        line = raw.strip().rstrip(",").strip()
        line = re.sub(r"^\d+[.、)]\s*", "", line)
        line = line.strip("\"“”'「」 ")
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


MAX_PERSONA = 0             # 0 = 不截断，人格提示词原样给（不同人格的设定差很多，截了会丢语气）


async def _voice_of(plugin, persona_id: str) -> str:
    """该人格的"语气参考"。

    优先用 AstrBot 里那份**完整人格提示词**（`context_builder._get_persona_prompt`）——
    它才真正写了这个人怎么说话；拿不到再退回配置里那段短的风格字段。
    结果按人格缓存，一次翻译只取一次。
    """
    cache = getattr(plugin, "_persona_prompt_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            plugin._persona_prompt_cache = cache
        except Exception:
            pass
    if persona_id in cache:
        return cache[persona_id]
    text = ""
    builder = getattr(plugin, "context_builder", None)
    fn = getattr(builder, "_get_persona_prompt", None)
    if fn is not None:
        try:
            text = str(await fn(persona_id) or "").strip()
        except Exception:
            text = ""
    if not text:
        try:
            text = str(plugin.get_style_for_persona(persona_id) or "").strip()
        except Exception:
            text = ""
    if MAX_PERSONA > 0:
        text = text[:MAX_PERSONA]
    cache[persona_id] = text
    return text


async def translate_items(plugin, items: list[tuple[str, str]],
                          batch: int = DEFAULT_BATCH) -> dict:
    """翻一批 [(人格, 原文)]，返回 {(人格, 原文): 中文}。失败返回已拿到的那部分。"""
    got: dict[tuple[str, str], str] = {}
    if not items:
        return got
    provider, model = await _provider(plugin)
    if provider is None:
        logger.warning("[晨光心语] ⚠️ 分享预翻译：没有可用模型，跳过（下轮再试）")
        return got

    # 按人格分组：每个 prompt 只带一个人格的风格，免得语气串味
    grouped: dict[str, list[str]] = {}
    for pid, text in items:
        grouped.setdefault(pid, []).append(text)

    for pid, texts in grouped.items():
        voice = await _voice_of(plugin, pid)
        style_line = (
            "\n她的角色设定（**只用来学语感、口癖、用词习惯**；不许把设定里的名词、情节、描写写进译文）：\n"
            + voice
        ) if voice else ""
        for i in range(0, len(texts), max(1, batch)):
            chunk = texts[i:i + batch]
            prompt = _PROMPT.format(
                name=pid, style_line=style_line,
                payload=json.dumps(chunk, ensure_ascii=False, indent=0),
            )
            try:
                raw = await _call(provider, model, prompt)
            except Exception as e:
                logger.warning(f"[晨光心语] ⚠️ 分享预翻译调用失败（{pid}）: {e}")
                await asyncio.sleep(1)
                continue
            arr = _parse_translated(raw, len(chunk))
            if len(arr) != len(chunk):
                logger.warning(
                    f"[晨光心语] ⚠️ 分享预翻译条数对不上（{pid}：要 {len(chunk)} 得 {len(arr)}），本批只收对得上的"
                )
            for src, zh in zip(chunk, arr):
                if zh and zh.strip():
                    got[(pid, src.strip())] = zh.strip()
    return got


# ── 一轮：查新增 → 翻 → 存 ──────────────────────────────────────────


async def run_once(plugin, persona_ids, batch: int = DEFAULT_BATCH) -> dict:
    """跑一轮预翻译。返回 {pending, translated, failed, total}。"""
    cache = load(plugin)
    items = pending_items(plugin, persona_ids)
    stats = {"pending": len(items), "translated": 0, "failed": False, "total": len(cache)}
    if not items:
        plugin._share_zh_stat = {"at": datetime.now().strftime("%H:%M"), "new": 0}
        return stats
    got = await translate_items(plugin, items, batch=batch)
    if not got:
        stats["failed"] = True
        plugin._share_zh_stat = {"at": datetime.now().strftime("%H:%M"), "new": 0, "failed": True}
        return stats
    now = datetime.now().isoformat(timespec="seconds")
    for (pid, src), zh in got.items():
        cache[key_of(pid, src)] = {"zh": zh, "ts": now, "pid": pid, "ver": CACHE_VER}
    if save(plugin, cache):
        plugin._share_zh = cache
    stats["translated"] = len(got)
    stats["total"] = len(cache)
    plugin._share_zh_stat = {"at": now, "new": len(got)}
    logger.info(f"[晨光心语] 🌐 分享预翻译：新增 {len(got)} 条（缓存共 {len(cache)} 条）")
    return stats


def status(plugin, persona_id: str = "") -> dict:
    """页面用：该人格的条数 / 今日新增 / 上次检查。"""
    cache = load(plugin)
    today = date.today().isoformat()
    pid = str(persona_id or "").strip()
    total = today_new = 0
    for v in cache.values():
        if not isinstance(v, dict):
            continue
        if pid and str(v.get("pid") or "") != pid:
            continue
        total += 1
        if str(v.get("ts") or "").startswith(today):
            today_new += 1
    last = getattr(plugin, "_share_zh_stat", None) or {}
    return {
        "total": total,
        "today_new": today_new,
        "last_at": str(last.get("at") or ""),
        "last_failed": bool(last.get("failed")),
    }
