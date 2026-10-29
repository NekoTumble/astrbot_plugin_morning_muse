"""晨光心语 · 记忆同步桥（Phase 2 · R9）

分工原则（方案 §12.5）：
    对话归记忆服务，生活归日程插件，中间一层同步桥。

这座桥**只读不写**——绝不往任何记忆服务里塞东西。

取法（方案 §12.3）：本插件只实现 `pool` —— 自建记忆池 + 主动检索，
不依赖对方暴露接口。（原先设想的 `snapshot` 只存不查、`direct` 直连对方
向量库，都因意义不大或通用性差而未做，故不再作为可选项暴露。）

自动识别规则（方案 §12.4）：先从 system_prompt 里找 `<XXX>...</XXX>` 段落，
优先挑名字里带 memory / mem / mnemo / rag / 记忆 的那些；
设置页填了「记忆搭桥目标」就只捞指定的那一个；留空 = 自动识别。
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiofiles

from astrbot.api import logger

from .event_ledger import _tokenize
from .state_store import safe_persona_id

# <Tag> ... </Tag>
_TAG_RE = re.compile(r"<([A-Za-z][A-Za-z0-9_\-]{1,31})>(.*?)</\1>", re.DOTALL)

# 名字里带这些字样的标记，优先当成记忆段落
# V8 修复：标签正则 _TAG_RE 只认 ASCII 起始标签名，中文词永远匹配不上，
# 留着只会误导（以为中文标签能被捞到）。删掉。
_MEMORY_HINTS = ("memory", "mem", "mnemo", "rag", "recall", "remember")

# 已知的记忆插件标记（内置规则，兜底用）
_KNOWN_TAGS = ("RAG-Faiss-Memory", "Mnemosyne")

# 单条记忆最长留这么多字符，避免池子被超大段落撑爆
_MAX_TEXT = 400

# 条目的编号形如 m_000123 —— 用来接着往下排号（裁剪后不会跟老条目撞号）
_ID_RE = re.compile(r"m_(\d+)$")

# 一个记忆块最多拆成几条 —— 防着那种「内部全是空行的长日记」一次灌爆池子
_MAX_SEGS_PER_TAG = 12

# 比这还短的「孤立小段」直接丢掉（像 Mnemosyne 单独占一行的「【全息记忆】」）
_MIN_SEG_CHARS = 30


def _looks_like_memory(tag: str) -> bool:
    low = tag.lower()
    return any(h in low for h in _MEMORY_HINTS)


def _as_limit(limit: Any, default: int = 3) -> int:
    """把 limit 归一成「非负整数」；0 / 负数 / 读不懂的一律当「不检索」。"""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else 0


def _parse_ts(value: Any) -> datetime | None:
    """把 ts 解析成 **naive** datetime；读不懂就返回 None。

    F11 修复：
      ① 带 tzinfo 的历史数据先转成 naive —— 原来 `ts < cutoff`（naive）会抛
         TypeError 直接逃出 retrieve()，把整次检索带崩；
      ② 坏时间戳返回 None，**不再假装「现在」** —— 原来那样会让这条永远排最前、
         且在 prune 里永远不算过期（清不掉）。
    """
    try:
        ts = datetime.fromisoformat(str(value))
    except Exception:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone().replace(tzinfo=None)
    return ts


def _locked(fn):
    """F10 修复：把一次「读 → 改 → 写盘 → 回填缓存」整段串起来。

    capture / prune 都是「load 之后横跨多个 await 才写盘」，并发时会各自基于
    同一份 pool 生成条目（id 撞号）+ 后写覆盖前写（丢写）。
    `load()` 故意不加锁（它被这两个方法在锁内调用），避免自死锁。
    """

    @functools.wraps(fn)
    async def _wrapper(self, *args, **kwargs):
        async with self._lock:
            return await fn(self, *args, **kwargs)

    return _wrapper


class MemoryBridge:
    """把「对话记忆」里跟生活有关的部分捞出来，攒成本地池子再自己检索。"""

    def __init__(self, data_dir: Path, source_tag: str = "", max_items: int = 2000):
        self.data_dir = Path(data_dir)
        self.source_tag = (source_tag or "").strip()
        try:
            self.max_items = int(max_items)
        except (TypeError, ValueError):
            self.max_items = 2000
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()          # F10：读-改-写串行化
        # F11：一条记忆的分词只算一次。**只活在内存里**，落盘时完全不碰它，
        # 所以不会往 jsonl 里多写字段（这是它跟「写进 item 对象」的关键区别）。
        self._token_cache: dict[str, set] = {}

    def _path(self, persona_id: str) -> Path:
        return self.data_dir / "life" / safe_persona_id(persona_id) / "memory_pool.jsonl"

    # ─────────────────────── 写入：从系统提示词里捞 ───────────────────────

    def extract(self, system_prompt: str) -> list[dict[str, str]]:
        """从一段 system_prompt 里抽出记忆段落。纯函数，好测。"""
        if not system_prompt or not isinstance(system_prompt, str):
            return []
        out: list[dict[str, str]] = []
        for m in _TAG_RE.finditer(system_prompt):
            tag, body = m.group(1), (m.group(2) or "").strip()
            if not body:
                continue
            if self.source_tag:
                if tag != self.source_tag:
                    continue
            elif not (_looks_like_memory(tag) or tag in _KNOWN_TAGS):
                continue
            # 剥掉记忆插件塞的包装：「--- BEGIN … ---」到「--- END … ---」之间那一整段
            # 是它写给模型看的说明（"以下是历史记忆、别当当前对话"之类），不是记忆本身。
            # 留着会白吃 `_MAX_TEXT` 的额度 —— RAG 那块就是这么把真记忆挤没、只剩英文说明的。
            text = re.sub(
                r"-{2,}\s*(?:BEGIN|END|开始|结束)[^-]*?-{2,}.*?"
                r"-{2,}\s*(?:BEGIN|END|开始|结束)[^-]*?-{2,}",
                " ", body, flags=re.IGNORECASE | re.DOTALL,
            )
            # 兜底：落单的标记行也清掉
            text = re.sub(r"-{2,}\s*(?:BEGIN|END|开始|结束)[^-]*?-{2,}", " ", text, flags=re.IGNORECASE)
            # N5：把一个块切成「尽量少、尽量满」的几条 —— 一个块里常塞了好几件事
            # （RAG 的「记忆 #1 / #2 / #3」之间、Mnemosyne 的「1. 时间地点 / 2. 在场的人」之间都是空行）。
            # 整块只当一条的话：第一件事之外的全都进不来，而且第一条自己还会被 _MAX_TEXT 砍掉大半。
            # 做法：先按【空行】切段 → 再把装不满的小段并进邻段（不然会拆出一堆
            # 「【全息记忆】」这样的碎片，条数也会暴涨）→ 超长的段自己截断。
            # 纯结构处理，不看具体文案 —— 免得跟人家的格式绑死（N1 的教训）。
            paras = [re.sub(r"\s+", " ", x).strip() for x in re.split(r"\n\s*\n", text)]
            segs: list[str] = []
            buf = ""
            for para in paras:
                if not para:
                    continue
                if len(para) < _MIN_SEG_CHARS and not buf:
                    continue  # 孤零零的小标题（如「【全息记忆】」）—— 不要
                if not buf:
                    buf = para
                elif len(buf) + 1 + len(para) <= _MAX_TEXT:
                    buf = f"{buf} {para}"
                else:
                    segs.append(buf)
                    buf = para
                if len(buf) >= _MAX_TEXT:
                    segs.append(buf[:_MAX_TEXT])
                    buf = ""
            if buf:
                segs.append(buf[:_MAX_TEXT])
            for seg in segs[:_MAX_SEGS_PER_TAG]:
                seg = seg.strip()
                if seg:
                    out.append({"tag": tag, "text": seg})
        return out

    def _tokens_of(self, item: dict[str, Any]) -> set:
        """F11：一条记忆的分词只算一次（`_tokenize` 是纯函数、正文不会变）。

        结果只存内存字典，绝不写回 item 对象 —— 落盘永远是原样那几行。
        """
        key = str(item.get("fingerprint") or item.get("id") or "")
        hit = self._token_cache.get(key)
        if hit is None:
            hit = set(_tokenize(item.get("text", "")))
            self._token_cache[key] = hit
        return hit

    @_locked
    async def capture(self, persona_id: str, system_prompt: str) -> int:
        """把这一轮捞到的记忆攒进池子。返回新增条数。"""
        items = self.extract(system_prompt)
        if not items:
            return 0
        pool = await self.load(persona_id)
        seen = {p.get("fingerprint") for p in pool}
        fresh: list[dict[str, Any]] = []
        now_iso = datetime.now().isoformat(timespec="seconds")
        nums = [
            int(mo.group(1))
            for mo in (_ID_RE.match(str(it.get("id") or "")) for it in pool)
            if mo
        ]
        nxt = (max(nums) + 1) if nums else 1
        for it in items:
            # 指纹：**去掉数字**再截前 120 字。
            # 记忆插件每次给的 Importance / 写入时间 / 条目编号都会抖，
            # 带进指纹的话，同一条记忆每轮都会被当成"新的"重写一遍
            #（实测第二轮就是这么白灌了 10 条）。
            # 先去数字、再截断（同 external_memory：先截后去数字会被位数抖动击穿）
            fp = f"{it['tag']}::{re.sub(r'[0-9]', '', it['text'])[:120]}"
            if fp in seen:
                continue
            seen.add(fp)
            fresh.append({
                "id": f"m_{nxt + len(fresh):06d}",
                "ts": now_iso,
                "tag": it["tag"],
                "text": it["text"],
                "fingerprint": fp,
            })
        if fresh:
            path = self._path(persona_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            merged = pool + fresh
            keep = self.max_items
            trim = bool(keep and keep > 0 and len(merged) > keep)
            if trim:
                # 超上限了：整本重写，把最旧的丢掉（只 append 是裁不掉的）
                merged = merged[-keep:]
            try:
                if trim:
                    tmp = path.with_suffix(".tmp")
                    async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
                        for it in merged:
                            await f.write(json.dumps(it, ensure_ascii=False) + "\n")
                    os.replace(tmp, path)
                else:
                    async with aiofiles.open(path, "a", encoding="utf-8") as f:
                        for it in fresh:
                            await f.write(json.dumps(it, ensure_ascii=False) + "\n")
            except OSError as e:
                logger.warning(f"[晨光心语] ⚠️ {persona_id} 写入记忆池失败: {e}")
                return 0
            self._cache[persona_id] = merged
            self._token_cache.clear()   # F11：池子变了，分词缓存跟着丢
            logger.debug(f"[晨光心语] 🌉 {persona_id} 记忆池 +{len(fresh)} 条（共 {len(merged)}）")
        return len(fresh)

    # ─────────────────────── 读取 ───────────────────────

    async def load(self, persona_id: str) -> list[dict[str, Any]]:
        if persona_id in self._cache:
            return self._cache[persona_id]
        path = self._path(persona_id)
        pool: list[dict[str, Any]] = []
        if path.exists():
            try:
                async with aiofiles.open(path, encoding="utf-8") as f:
                    async for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if isinstance(obj, dict) and obj.get("text"):
                                pool.append(obj)
                        except json.JSONDecodeError:
                            continue
            except OSError as e:
                logger.warning(f"[晨光心语] ⚠️ {persona_id} 读取记忆池失败: {e}")
        self._cache[persona_id] = pool
        return pool

    async def retrieve(self, persona_id: str, query: str, limit: int = 3,
                       days: int = 30) -> list[dict[str, Any]]:
        """按关键词 + 时间衰减，从池子里捞出跟当前话题最相关的几条。"""
        # F2：limit 先归一 —— 0 / 负数一律「不检索」，别让 [-0:] 把整池塞进提示词。
        # （原来只挡住了「空查询」那条分支；带查询的那条 `scored[:limit]` 漏了。）
        limit = _as_limit(limit, 3)
        if limit <= 0:
            return []
        pool = await self.load(persona_id)
        if not pool:
            return []
        q_tokens = set(_tokenize(query)) if query else set()
        if not q_tokens:
            return pool[-limit:]
        # F11：now 提到循环外（原来每条都要调一次 datetime.now()）；
        # cutoff 保持 naive，但下面把带 tz 的 ts 先归一成 naive 再比。
        now = datetime.now()
        cutoff = now - timedelta(days=days)
        scored = []
        for item in pool:
            ts = _parse_ts(item.get("ts"))
            if ts is not None and ts < cutoff:
                continue
            tokens = self._tokens_of(item)
            overlap = len(q_tokens & tokens) / max(1, len(q_tokens))
            if ts is None:
                # 时间戳读不懂：不参与时间衰减（不占「刚刚说过」的便宜）
                recency = 0.0
            else:
                age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
                recency = 0.5 ** (age_days / 14.0)
            scored.append((overlap + 0.3 * recency, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for _, it in scored[:limit]]

    async def stats(self, persona_id: str) -> dict[str, int]:
        pool = await self.load(persona_id)
        return {"total": len(pool)}

    @_locked
    async def prune(self, persona_id: str, retention_days: int = 30) -> int:
        """池子只留最近 N 天的记忆。"""
        if retention_days <= 0:
            return 0
        pool = await self.load(persona_id)
        cutoff = datetime.now() - timedelta(days=retention_days)
        kept, dropped = [], 0
        for item in pool:
            ts = _parse_ts(item.get("ts"))
            # F11：ts 读不懂的**留着** —— 原来把它当成「现在」，这类条目永远清不掉
            if ts is not None and ts < cutoff:
                dropped += 1
            else:
                kept.append(item)
        if dropped:
            path = self._path(persona_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept)
            try:
                async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
                    await f.write(body)
                import os
                os.replace(tmp, path)
                self._cache[persona_id] = kept
                self._token_cache.clear()   # F11：池子变了，分词缓存跟着丢
                logger.info(f"[晨光心语] 🧹 {persona_id} 记忆池清理 {dropped} 条")
            except OSError as e:
                logger.warning(f"[晨光心语] ⚠️ {persona_id} 清理记忆池失败: {e}")
                return 0
        return dropped

    def forget_cache(self, persona_id: str | None = None):
        if persona_id is None:
            self._cache.clear()
        else:
            self._cache.pop(persona_id, None)
