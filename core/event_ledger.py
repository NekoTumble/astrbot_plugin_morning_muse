"""晨光心语 · 轻量事件账本（Phase 2）

不引 Docker、不引向量库——就是一个 `events.jsonl` 记事本：
一行一条 JSON，只记「发生过什么、了没了的」。

它是 EMG（事件感知记忆图）的下位替代：
- 真 EMG 用模型把文本转成图、在图上走多跳因果；
- 我们用「关键词 + 时间衰减 + LLM 挑选」把其中七八成做到位。

一条事件长这样：
    {"id": "e_20260930_01", "date": "2026-09-30",
     "summary": "把九月的工作收尾了", "keywords": ["工作", "九月", "收尾"],
     "status": "open", "related_goal": "收尾九月",
     "created": "2026-09-30T11:02:00"}
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import aiofiles

try:
    from astrbot.api import logger
except ImportError:  # 独立自测
    import logging

    logger = logging.getLogger(__name__)

from .state_store import safe_persona_id
from .timezone_utils import get_today

# 关键词重合的权重 > 时间新近度的权重（想让"相关"压过"新鲜"）
_W_KEYWORD = 1.0
_W_RECENCY = 0.4
# 超过这么多天没动过的事件，逐渐淡出
_RECENCY_HALFLIFE_DAYS = 7.0

_STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "把", "被", "给", "对", "从", "到",
    "今天", "昨天", "明天", "一下", "一点", "自己", "还有", "然后", "因为",
    "所以", "但是", "就是", "这个", "那个", "什么", "怎么", "已经", "还是",
}


def _tokenize(text: str) -> list[str]:
    """把中文短句切成粗糙的「词」——按标点断句 + 二字滑窗，够检索用就行。"""
    if not text:
        return []
    parts = re.split(r"[\s,，、。；;：:！!？?（）()\[\]【】\"'“”‘’\-—…·]+", str(text))
    tokens: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) <= 4:
            tokens.append(p)
            continue
        # 长词切成二字滑窗，让「便签纸」和「便签」能撞上
        for i in range(len(p) - 1):
            tokens.append(p[i:i + 2])
    return [t for t in tokens if t not in _STOPWORDS]


def _as_limit(limit: Any, default: int = 3) -> int:
    """把 limit 归一成「非负整数」；0 / 负数 / 读不懂的一律当「不检索」。

    F2 修复：以前 limit=-1 会走 `scored[:-1]`,那是「除最后一条外的全部」——
    跟「取前 N 条」正好反着来，会把大半本账灌进提示词。这里统一钳到 0。
    """
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else 0


def _norm_id(raw: Any) -> str:
    """把 LLM 报回来的编号洗成可比对的形状（2026-10-15 新增）。

    LLM 抄 id 时会手抖：加方括号、引号、把 `e_20261013_02` 写成
    `e_2026-10-13_02`。所以统一「只留字母数字、转小写」再比：
        e_20261013_02 → e2026101302
        e_2026-10-13_02 → e2026101302   ✓ 撞上了
        [e_20261013_02] → e2026101302   ✓
    编出来的号（在账本里根本不存在）洗完之后自然谁也对不上，会被安静忽略。
    """
    return re.sub(r"[^0-9a-zA-Z]", "", str(raw or "")).lower()


def _locked(fn):
    """F10 修复：把一次「读 → 改 → 写盘 → 回填缓存」整段串起来。

    record / close_by_summary / prune 都是「load 之后横跨多个 await 才写盘」，
    心跳、日程生成、命令三条路并发时会基于同一份 events 各自生成事件
    （id 撞号），后写者还会覆盖前写者（丢写）。
    `load()` 故意**不加**这把锁 —— 它被上面几个方法在锁内调用，加了会自死锁。
    """

    @functools.wraps(fn)
    async def _wrapper(self, *args, **kwargs):
        async with self._lock:
            return await fn(self, *args, **kwargs)

    return _wrapper


class EventLedger:
    """按 persona 隔离的事件账本。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._cache: dict[str, list[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()   # F10：读-改-写串行化（见 _locked 注释）

    # ─────────────────────── 路径与读写 ───────────────────────

    def _path(self, persona_id: str) -> Path:
        return self.data_dir / "life" / safe_persona_id(persona_id) / "events.jsonl"

    async def load(self, persona_id: str) -> list[dict[str, Any]]:
        if persona_id in self._cache:
            return self._cache[persona_id]
        path = self._path(persona_id)
        events: list[dict[str, Any]] = []
        if path.exists():
            try:
                async with aiofiles.open(path, encoding="utf-8") as f:
                    async for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if isinstance(obj, dict) and obj.get("summary"):
                                events.append(obj)
                        except json.JSONDecodeError:
                            continue  # 坏行跳过，别让一行毁掉整个账本
            except OSError as e:
                logger.warning(f"[晨光心语] ⚠️ {persona_id} 读取事件账本失败 {path.name}: {e}")
        self._cache[persona_id] = events
        return events

    async def _append(self, persona_id: str, events: list[dict[str, Any]]) -> bool:
        path = self._path(persona_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with aiofiles.open(path, "a", encoding="utf-8") as f:
                for e in events:
                    await f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 写入事件账本失败: {e}")
            return False   # V11 修复：写盘失败就如实返回，别谎报「记上了」
        return True

    async def _rewrite(self, persona_id: str, events: list[dict[str, Any]]) -> bool:
        """改写整个账本（关闭事件、清理旧事件时用）。返回是否落盘成功。"""
        path = self._path(persona_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
            async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
                await f.write(body)
            import os
            os.replace(tmp, path)
        except OSError as e:
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 重写事件账本失败: {e}")
            return False   # V11 修复：写盘失败就别更新内存缓存，免得内存与磁盘对不上
        self._cache[persona_id] = events
        return True

    # ─────────────────────── 写 ───────────────────────

    @staticmethod
    def _new_id(d: date, seq: int) -> str:
        return f"e_{d.strftime('%Y%m%d')}_{seq:02d}"

    @_locked
    async def record(self, persona_id: str, raw_events: Any, d: date | None = None) -> int:
        """把 LLM 顺手产出的 events 落账。返回新写入的条数。"""
        if not raw_events:
            return 0
        if isinstance(raw_events, (str, dict)):
            raw_events = [raw_events]
        if not isinstance(raw_events, (list, tuple)):
            return 0

        d = d or get_today()
        events = await self.load(persona_id)
        existing_summaries = {e.get("summary", "").strip() for e in events}
        seq = sum(1 for e in events if str(e.get("date")) == d.isoformat())
        now_iso = datetime.now().isoformat(timespec="seconds")

        fresh: list[dict[str, Any]] = []
        for item in raw_events:
            if isinstance(item, str):
                summary, keywords, status = item.strip(), [], "open"
            elif isinstance(item, dict):
                summary = str(item.get("summary") or item.get("event") or "").strip()
                keywords = item.get("keywords") or []
                status = str(item.get("status") or "open").strip().lower()
            else:
                continue

            if not summary or summary in existing_summaries:
                continue  # 去重：同一件事别记两遍
            if isinstance(keywords, str):
                keywords = [k.strip() for k in re.split(r"[;；,，\s]+", keywords) if k.strip()]
            keywords = [str(k).strip() for k in keywords if str(k).strip()][:6]

            seq += 1
            fresh.append({
                "id": self._new_id(d, seq),
                "date": d.isoformat(),
                "summary": summary,
                "keywords": keywords or _tokenize(summary)[:6],
                "status": "closed" if status == "closed" else "open",
                "related_goal": str(item.get("related_goal") or "").strip() if isinstance(item, dict) else "",
                "created": now_iso,
            })
            existing_summaries.add(summary)

        if fresh:
            ok = await self._append(persona_id, fresh)
            if not ok:
                return 0   # V11 修复：没落盘就等于没记上，别再返回条数假装成功
            events.extend(fresh)
            self._cache[persona_id] = events
            logger.info(f"[晨光心语] 📒 {persona_id} 事件账本 +{len(fresh)} 条")
        return len(fresh)

    @_locked
    async def close_by_summary(self, persona_id: str, summaries: list[str]) -> int:
        """按内容关闭——LLM 说不出 id，只能告诉我们「这件事做完了」。

        F1 修复：原来直接在 load() 返回的**缓存对象**上改 status，而且
        `_rewrite` 的布尔返回值被丢掉 —— 写盘失败也照样打 ✅ 并返回条数。
        后果：内存说「已了结」、磁盘还是 open，重启全回滚；而 prune 只清 open，
        会把本该永久保留的 closed 当过期 open 清掉（「去年的今天」的原料就没了）。
        现在：先在**副本**上改，写盘成功才回填缓存；失败返回 0 且不打 ✅。
        """
        if not summaries:
            return 0
        wants = [str(s).strip() for s in summaries if str(s).strip()]
        if not wants:
            return 0
        events = await self.load(persona_id)
        snapshot = [dict(e) for e in events]      # 先在副本上改，不碰缓存里那批对象
        n = 0
        for e in snapshot:
            if e.get("status") == "closed":
                continue
            text = e.get("summary", "")
            for s in wants:
                if s == text or (len(s) >= 4 and (s in text or text in s)):
                    e["status"] = "closed"
                    e["closed_at"] = datetime.now().isoformat(timespec="seconds")
                    e["closed_by"] = "summary"   # 2026-10-15：跟 close_by_ids 统一口径，事后能分清是哪条路关的
                    n += 1
                    break
        if not n:
            return 0
        if not await self._rewrite(persona_id, snapshot):   # ← 检查返回值
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 事件了结写盘失败，本次不改动内存")
            return 0
        logger.info(f"[晨光心语] ✅ {persona_id} 事件按内容了结 {n} 条")
        return n

    @_locked
    async def close_by_ids(self, persona_id: str, ids: Any, source: str = "id") -> int:
        """按编号了结（2026-10-15 新增，主路）。

        为什么要有这条路：原来的 close_by_summary 要求「整句一模一样」或
        「一句完整包含另一句」，换一个说法就撞不上——实测建账 15 天，
        closed_at 一条都没有，等于这条口子从来没通过。
        现在清单里每条都带上账本 id，让她直接点名，措辞随便写都能关掉。
        close_by_summary 留着当兜底（万一她还按老习惯报描述）。
        """
        if not ids:
            return 0
        if isinstance(ids, (str, bytes)):
            ids = [ids]
        wants = {_norm_id(x) for x in ids}
        wants.discard("")
        if not wants:
            return 0
        events = await self.load(persona_id)
        snapshot = [dict(e) for e in events]      # 同 close_by_summary：先在副本上改
        hit: list[str] = []
        for e in snapshot:
            if e.get("status") == "closed":
                continue
            if _norm_id(e.get("id")) in wants:
                e["status"] = "closed"
                e["closed_at"] = datetime.now().isoformat(timespec="seconds")
                e["closed_by"] = source
                hit.append(str(e.get("id")))
        if not hit:
            return 0
        if not await self._rewrite(persona_id, snapshot):
            logger.warning(f"[晨光心语] ⚠️ {persona_id} 事件按编号了结写盘失败，本次不改动内存")
            return 0
        logger.info(f"[晨光心语] ✅ {persona_id} 事件按编号了结 {len(hit)} 条：{', '.join(hit)}")
        return len(hit)

    # ─────────────────────── 读 ───────────────────────

    @staticmethod
    def _score(event: dict[str, Any], query_tokens: set, today: date) -> float:
        ev_tokens = set(_tokenize(event.get("summary", ""))) | set(event.get("keywords") or [])
        overlap = len(query_tokens & ev_tokens)
        kw_score = overlap / max(1, len(query_tokens)) if query_tokens else 0.0

        try:
            created = date.fromisoformat(str(event.get("date")))
            age_days = max(0.0, (today - created).days)
        except Exception:
            age_days = 30.0
        recency = 0.5 ** (age_days / _RECENCY_HALFLIFE_DAYS)

        return _W_KEYWORD * kw_score + _W_RECENCY * recency

    async def retrieve(self, persona_id: str, query: str, limit: int = 3,
                       today: date | None = None) -> list[dict[str, Any]]:
        """按「关键词重合 + 时间衰减」捞出还没了结的事件。"""
        # F2：limit 先归一；0 / 负数 = 不检索（绝不能变成「除尾巴外的全部」）
        limit = _as_limit(limit, 3)
        if limit <= 0:
            return []
        today = today or get_today()
        q = _tokenize(query) if isinstance(query, str) else list(query or [])
        q_tokens = set(q)
        events = await self.load(persona_id)
        open_events = [e for e in events if e.get("status") != "closed"]
        if not open_events:
            return []
        scored = sorted(open_events, key=lambda e: self._score(e, q_tokens, today), reverse=True)
        return scored[:limit]

    async def candidates(self, persona_id: str, days: int = 30, cap: int = 30) -> list[dict[str, Any]]:
        """给 LLM 挑选用的候选池（最近 N 天还没了结的事件）。"""
        today = get_today()
        events = await self.load(persona_id)
        out = []
        for e in events:
            if e.get("status") == "closed":
                continue
            try:
                if (today - date.fromisoformat(str(e.get("date")))).days > days:
                    continue
            except Exception:
                pass
            out.append(e)
        out.sort(key=lambda e: str(e.get("date")), reverse=True)
        return out[:cap]

    async def stats(self, persona_id: str) -> dict[str, int]:
        events = await self.load(persona_id)
        return {
            "total": len(events),
            "open": sum(1 for e in events if e.get("status") != "closed"),
            "closed": sum(1 for e in events if e.get("status") == "closed"),
        }

    # ─────────────────────── 维护 ───────────────────────

    @_locked
    async def prune(self, persona_id: str, retention_days: int = 30) -> int:
        """清掉「很久以前、又一直没了结」的旧事 —— **只清 open**。

        已经了结的（closed）**永久保留**：它们是旧账的原料 ——
          * 「去年的今天」要翻 365 天前的那一条；
          * 「旧事想起」（⑧）也只从 closed 里捞。
        这两条都靠旧账活着，清了就没得讲了。

        真正该清的是「一直挂着的未了结事件」：超过保留窗口就清掉，
        免得越堆越多、把账本和被带进日程的「线头」一起污染。
        """
        if retention_days <= 0:
            return 0
        cutoff = get_today() - timedelta(days=retention_days)
        events = await self.load(persona_id)
        kept, dropped = [], 0
        for e in events:
            keep = True
            # 只对「未了结」的套用保留窗口；已了结的一律留着
            if str(e.get("status") or "").lower() != "closed":
                try:
                    if date.fromisoformat(str(e.get("date"))) < cutoff:
                        keep = False
                except Exception:
                    pass
            if keep:
                kept.append(e)
            else:
                dropped += 1
        if dropped:
            ok = await self._rewrite(persona_id, kept)
            if ok:
                logger.info(f"[晨光心语] 🧹 {persona_id} 事件账本清理 {dropped} 条")
            else:
                logger.warning(f"[晨光心语] ⚠️ {persona_id} 账本清理写盘失败，本次不改动")
        return dropped

    def forget_cache(self, persona_id: str | None = None):
        if persona_id is None:
            self._cache.clear()
        else:
            self._cache.pop(persona_id, None)
