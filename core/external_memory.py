"""晨光心语 · 外部记忆库直连（只读）

把「她的生活线索」的来源，从**被动白嫖注入块**（本地池）升级成
**主动只读查询外部记忆库**——拿到的是她自己的原始记忆，
不是别人嚼过一通、再被我们切片的四百字。

设计铁律（施工计划 §2 / §3.4）：
  1. **只读** —— 只管「查」，绝不碰写入类操作（增 / 改 / 删 / 建表 / 删表）。
     验收 §5.1 会 grep 代码确认这件事，所以这里连注释都避开那几个英文词。
  2. **默认关** —— backend=off 时完全不碰外部库，行为与老版本一模一样。
  3. **失败只降级** —— 任何异常都吞掉、只记 warning、返回 []，
     绝不把错误抛给日程 / 主动消息的主流程。
  4. **通用** —— 地址 / 集合 / 口令 / 人格映射全可配；
     embedding 用 AstrBot 现成的 provider；向量距离度量自适应探测。
     不是"只有这一台 NAS 能用"。

返回格式统一成 ``[{"text", "fingerprint", "ts"}]``，跟本地池（memory_bridge）
一致，上层不用为两个来源写两套代码。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # 离线自测 / 单测时没有 astrbot
    import logging

    logger = logging.getLogger(__name__)

from .state_store import safe_persona_id

# 单条取回来最多留多少字 —— 这是**安全阀**，防止某条特别长的把提示词撑爆。
# 「记得住多少」由设置页的 `sections` 决定：留空 = 整条原样给。
#   实测 500 条外部记忆：只取摘要      中位 176 字
#                        摘要+记忆内核  中位 632 字（P99 = 1140）
#                        整篇原文      中位 1508 字（已剥掉元数据尾巴）
# 默认 1200：够「摘要 + 记忆内核」用；填 0 = 不限制。
_DEFAULT_MAX_CHARS = 1200

# 设置页「记忆要哪几部分」：留空 = 整条原样给（短记忆正合适）；
# 填了名字就只取那几段，一行一个。可用名字见 _SECTION_ALIAS。
_DEFAULT_SECTIONS: tuple[str, ...] = ()

# 记忆晶体里常见的段落小标题 → 归一化段名。
# 不同人格 / 不同版本写出来的标题不一样（比如「时空锚点」=「时间与地点」），
# 这里归一到同一段；设置页勾一个名字就能覆盖各种写法。
_SECTION_ALIAS = {
    "时间与地点": "时间与地点", "时空锚点": "时间与地点",
    "在场的人": "在场的人", "主体镜像": "在场的人",
    "记忆内核": "记忆内核", "语义晶体层": "记忆内核",
    "情绪痕迹": "情绪痕迹", "情感动力场": "情绪痕迹",
    "决定与判断": "决定与判断", "决策轨迹": "决定与判断",
    "记忆关联": "记忆关联", "记忆联网": "记忆关联",
}

# 段标题行：`1. 时间与地点：内容`（内联带冒号）
#   或 `3. 记忆内核` / `4. 情感动力场`（独占一行、可无冒号，有的还带 ** 加粗）
_SECTION_RE = re.compile(
    r"^[ 	]*(\d{1,2})\s*[.、．]\s*\**\s*([^\n：:]{2,16}?)\s*\**\s*(?:[：:][ 	]*(.*)|)[ 	]*$",
    re.MULTILINE,
)

# 元数据尾巴（实体 / 关系索引）—— 永远不进提示词
_META_RE = re.compile(r"<MNEMO_META>.*", re.DOTALL)

# 【快照摘要】段 = 这条记忆的一句话概括，比整篇【全息记忆】更适合进提示词
_SNAP_RE = re.compile(r"【快照摘要】[^\S\n]*\s*(.*?)(?=\n\s*\n|\n?【|\n\s*\d{1,2}\s*[.、．]|\Z)", re.DOTALL)

# 距离度量探测顺序：先不传（用集合自己的默认），不行再逐个试。
# 实测 Mnemosyne 的 `ai` 集合建的是 L2 —— 写死 COSINE 会直接报
# "metric type not match"。自适应才能给别人用。
_METRIC_CANDIDATES: tuple[str | None, ...] = (None, "L2", "COSINE", "IP")

# 认识的后端。mcp / http 留到 Step 4，本轮先只放 milvus。
_SUPPORTED_BACKENDS = ("milvus",)


def _is_awaitable(obj: Any) -> bool:
    return inspect.isawaitable(obj)


class ExternalMemory:
    """外部记忆库的只读客户端（当前实现：直连 Milvus）。"""

    def __init__(self, plugin, data_dir: Path | None = None):
        self.plugin = plugin
        self.data_dir = Path(data_dir) if data_dir else None

        def cfg(key: str, default: Any) -> Any:
            try:
                return plugin.get_config(f"external_memory.{key}", default)
            except Exception:
                return default

        self.backend = str(cfg("backend", "off") or "off").strip().lower()
        self.address = str(cfg("address", "") or "").strip()
        self.collection = str(cfg("collection", "ai") or "ai").strip()
        self.token = str(cfg("token", "") or "").strip()
        self.embedding_provider_id = str(cfg("embedding_provider_id", "") or "").strip()
        self.persona_map = self._parse_map(str(cfg("persona_map", "") or ""))
        try:
            self.top_k = max(1, int(cfg("top_k", 3) or 3))
        except (TypeError, ValueError):
            self.top_k = 3
        try:
            self.timeout = max(1.0, float(cfg("timeout", 5) or 5))
        except (TypeError, ValueError):
            self.timeout = 5.0
        try:
            raw_max = cfg("max_chars", _DEFAULT_MAX_CHARS)
            self.max_chars = int(raw_max if raw_max is not None else _DEFAULT_MAX_CHARS)
        except (TypeError, ValueError):
            self.max_chars = _DEFAULT_MAX_CHARS
        if self.max_chars < 0:  # 负数当没填；0 = 明确「不限制」
            self.max_chars = _DEFAULT_MAX_CHARS

        # 记忆要哪几部分（留空 = 整条原样给）
        self.sections = self._parse_sections(cfg("sections", None), default=_DEFAULT_SECTIONS)

        self._client = None
        # 建连锁：_milvus() 在工作线程里被调用（_search_sync 经 asyncio.to_thread），
        # 所以必须是 threading.Lock —— asyncio.Lock 在线程里压根不生效。
        self._conn_lock = threading.Lock()
        self._metric: str | None = None
        self._embedding = None
        self._warned = False

    # ─────────────────────── 配置 / 状态 ───────────────────────

    @staticmethod
    def _parse_map(raw: str) -> dict[str, str]:
        """`亚纪=alice, 白羽=shiro` → dict。"""
        out: dict[str, str] = {}
        for chunk in re.split(r"[,，;；\n]", raw or ""):
            if "=" not in chunk and "：" not in chunk and ":" not in chunk:
                continue
            parts = re.split(r"[=：:]", chunk, maxsplit=1)
            if len(parts) != 2:
                continue
            k, v = parts[0].strip(), parts[1].strip()
            if k and v:
                out[k] = v
        return out

    def available(self) -> bool:
        """配置上是否启用了外部记忆库。"""
        return self.backend in _SUPPORTED_BACKENDS and bool(self.address)

    def describe(self) -> str:
        if self.backend == "off":
            return "off"
        if not self.address:
            return f"{self.backend}(未填地址)"
        return f"{self.backend}@{self.address}/{self.collection}"

    # ─────────────────────── 人格映射（§3.2） ───────────────────────

    def resolve_persona(self, persona_id: str) -> str:
        """把本插件的 persona_id（形如 `p_0123456789ab`）翻成外部库里的人格键。

        ① 设置页映射优先（加新人格不用改代码）
        ② 退回 `persona_defs/<id>.json` 的 `name`（例如："亚纪" / "白羽"）
        ③ 都没有 → 原样用（人格名本身就是键时天然正确）
        """
        pid = str(persona_id or "").strip()
        if not pid:
            return ""
        if pid in self.persona_map:
            return self.persona_map[pid]
        name = self._def_name(pid)
        if name and name != "default":
            return name
        return pid

    def _def_name(self, persona_id: str) -> str:
        if self.data_dir is None:
            return ""
        path = self.data_dir / "persona_defs" / f"{safe_persona_id(persona_id)}.json"
        try:
            if not path.exists():
                return ""
            data = json.loads(path.read_text(encoding="utf-8"))
            return str((data or {}).get("name") or "").strip()
        except Exception as e:
            logger.debug(f"[晨光心语] 读取人格映射 {path.name} 失败（忽略）: {e}")
            return ""

    # ─────────────────────── 连接（惰性） ───────────────────────

    def _milvus(self):
        """惰性建连：插件启动时不连，第一次真要查了才连 —— 配错地址也不会拖慢启动。

        并发保护：本函数在工作线程里被调用（_search_sync 经 asyncio.to_thread），
        08:00 双人格并发首次检索时会同时看到 _client is None —— 没有锁的话
        两边各建一个，后写覆盖前写，先建的那个泄漏（gRPC channel 只能等 GC）。
        """
        if self._client is not None:
            return self._client
        with self._conn_lock:
            # 双检：等锁的这个进来时，前一个可能已经把连接建好了
            if self._client is not None:
                return self._client
            from pymilvus import MilvusClient  # 没装就是 ImportError，被上层吞掉

            uri = self.address
            if not uri.startswith("http"):
                uri = f"http://{uri}"
            kwargs: dict[str, Any] = {"uri": uri, "timeout": self.timeout}
            if self.token:
                kwargs["token"] = self.token
            self._client = MilvusClient(**kwargs)
            logger.info(f"[晨光心语] 🔌 外部记忆库已连接: {self.describe()}")
            return self._client

    # ─────────────────────── embedding ───────────────────────

    async def _get_embedding_provider(self):
        """拿 AstrBot 现成的 embedding provider —— 跟记忆插件同源，向量空间天然对齐。"""
        if self._embedding is not None:
            return self._embedding
        ctx = getattr(self.plugin, "context", None)
        if ctx is None:
            return None

        prov = None
        if self.embedding_provider_id:
            getter = getattr(ctx, "get_provider_by_id", None)
            if getter:
                try:
                    prov = getter(self.embedding_provider_id)
                    if _is_awaitable(prov):
                        prov = await prov
                except Exception as e:
                    logger.warning(f"[晨光心语] ⚠️ 指定的 embedding provider 取不到: {e}")
                    prov = None

        if prov is None:
            getter = getattr(ctx, "get_all_embedding_providers", None)
            if getter:
                try:
                    lst = getter()
                    if _is_awaitable(lst):
                        lst = await lst
                    if isinstance(lst, tuple):
                        # 有的版本返回 (providers, err)
                        lst = lst[0]
                    lst = list(lst or [])
                    if lst:
                        prov = lst[0]
                except Exception as e:
                    logger.warning(f"[晨光心语] ⚠️ embedding provider 列表取不到: {e}")
                    prov = None

        if prov is None:
            if not self._warned:
                self._warned = True
                logger.warning("[晨光心语] ⚠️ 没找到 embedding provider，外部记忆库这轮跳过")
            return None
        self._embedding = prov
        return prov

    async def _embed(self, text: str) -> list[float] | None:
        prov = await self._get_embedding_provider()
        if prov is None:
            return None
        fn = getattr(prov, "get_embedding", None) or getattr(prov, "get_embeddings", None)
        if fn is None:
            return None
        try:
            res = fn(text)
            if _is_awaitable(res):
                res = await asyncio.wait_for(res, timeout=self.timeout * 2)
            if res and isinstance(res[0], (list, tuple)):
                res = res[0]  # get_embeddings 返回 [[...]]
            vec = list(res or [])
            return vec or None
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ 外部记忆库：文本向量化失败（跳过）: {e}")
            return None

    # ─────────────────────── 查询（只读） ───────────────────────

    def _search_sync(self, vec: list[float], expr: str, limit: int):
        """同步的向量检索。距离度量自适应：记住了就不再探。"""
        client = self._milvus()
        order = []
        if self._metric:
            order.append(self._metric)
        order.extend(m for m in _METRIC_CANDIDATES if m != self._metric)

        last_err: Exception | None = None
        for metric in order:
            kwargs: dict[str, Any] = {
                "collection_name": self.collection,
                "data": [vec],
                "limit": limit,
                "filter": expr,
                "output_fields": ["content", "create_time"],
            }
            if metric:
                kwargs["search_params"] = {"metric_type": metric}
            try:
                res = client.search(**kwargs)
                if metric != self._metric:
                    self._metric = metric
                    logger.info(f"[晨光心语] 🧭 外部记忆库距离度量探测为 {metric or '默认'}")
                return res
            except Exception as e:
                last_err = e
                if self._metric:
                    # 已知的度量突然失效（集合被重建？）→ 清掉重探一轮
                    self._metric = None
        raise last_err if last_err else RuntimeError("external search failed")

    async def search(self, persona_id: str, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        """按人格 + 语义，从外部库只读捞几条原始记忆。任何问题都返回 []。"""
        if not self.available():
            return []
        persona = self.resolve_persona(persona_id)
        if not persona or persona == "default":
            logger.debug(f"[晨光心语] 外部记忆库：人格 {persona_id} 没有可用键，跳过")
            return []
        query = (query or "").strip()
        if not query:
            return []

        n = int(limit or self.top_k or 3)
        vec = await self._embed(query)
        if not vec:
            return []

        expr = f"personality_id == {json.dumps(persona, ensure_ascii=False)}"
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(self._search_sync, vec, expr, n),
                timeout=self.timeout + 2,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[晨光心语] ⚠️ 外部记忆库超时（{self.timeout}s），本轮降级")
            return []
        except Exception as e:
            logger.warning(f"[晨光心语] ⚠️ 外部记忆库查询失败（已降级）: {e}")
            return []

        hits: list[dict[str, Any]] = []
        for group in res or []:
            for h in group or []:
                ent = h.get("entity") if isinstance(h, dict) else None
                content = ""
                if isinstance(ent, dict):
                    content = str(ent.get("content") or "")
                elif isinstance(h, dict):
                    content = str(h.get("content") or "")
                text = self._slim(content)
                if not text:
                    continue
                hits.append({
                    "text": text,
                    # 先去数字、再截断。反过来（先截 120 再去数字）的话，
                    # 文本里数字位数一变（"下午 3 点" → "下午 15 点"），
                    # 120 字的窗口就被挤歪，指纹跟着变 → 同一件事被当成两件、去重失效。
                    "fingerprint": f"ext::{re.sub(r'[0-9]', '', text)[:120]}",
                    "ts": self._iso(h.get("create_time")),
                    "source": "external",
                })
        if hits:
            logger.debug(f"[晨光心语] 🌐 {persona} 外部记忆库命中 {len(hits)} 条")
        return hits

    # ─────────────────────── 小工具 ───────────────────────

    def mode_text(self) -> str:
        """给人看的一行说明（/状态 里用）。"""
        label = "、".join(self.sections) if self.sections else "整条原样"
        cap = "不限" if not self.max_chars else f"{self.max_chars}字"
        return f"{label}｜单条上限 {cap}"

    @staticmethod
    def _parse_sections(raw: Any, default: Any = _DEFAULT_SECTIONS) -> list[str]:
        """设置页填的段落名 → 归一化列表。兼容 list / 逗号分隔字符串 / 空。"""
        fallback = list(default)
        if raw is None or raw == "":
            items: list[Any] = list(fallback)
        elif isinstance(raw, (list, tuple, set)):
            items = list(raw) or list(fallback)
        else:
            items = [x for x in re.split(r"[,，;；\n]", str(raw)) if x.strip()]
        out: list[str] = []
        for it in items:
            name = str(it).strip().strip("*【】[] ").strip()
            if not name:
                continue
            name = _SECTION_ALIAS.get(name, name)
            if name not in out:
                out.append(name)
        return out or fallback

    @staticmethod
    def _norm(t: str) -> str:
        """段内压缩空白，段落之间保留换行。"""
        t = re.sub(r"[ 	\r\n]+", " ", t or "")
        return re.sub(r"\n{2,}", "\n", t).strip()

    @staticmethod
    def _snapshot(t: str) -> str:
        """取【快照摘要】那一句。"""
        m = _SNAP_RE.search(t or "")
        if m and m.group(1).strip():
            return re.sub(r"[ 	\r\n]+", " ", m.group(1).strip())
        return ""

    @staticmethod
    def _split_sections(t: str, allowed: set | None = None) -> dict[str, str]:
        """按 `N. 小标题：` 把记忆晶体切成段。标题名先做别名归并。

        分段点 = 别名表里的标准名字 ∪ `allowed` 里用户自选的自由标题名。
        标准名字永远算分段点（否则「记忆内核」会一路吃到文末）；
        别名表外的名字只有 `allowed` 里点名了才算 —— 正文里偶尔也会出现
        `1. 提比说` 这种编号列表项，不点名就不认，免得把上一段的尾巴截断。
        """
        marks = []
        for m in _SECTION_RE.finditer(t or ""):
            raw = m.group(2).strip()
            name = _SECTION_ALIAS.get(raw)
            if name is None:
                # 别名表外的名字：只有用户自己填了，才认它是分段点
                if not allowed or raw not in allowed:
                    continue
                name = raw
            marks.append((m, name))
        out: dict[str, str] = {}
        for i, (m, name) in enumerate(marks):
            end = marks[i + 1][0].start() if i + 1 < len(marks) else len(t)
            body = ((m.group(3) or "") + "\n" + t[m.end():end]).strip()
            if not name or not body:
                continue
            out[name] = f"{out[name]}\n{body}".strip() if name in out else body
        return out

    def _compose(self, t: str, want: list[str]) -> str:
        """按要的名字，把对应的段落拼起来。"""
        snap = self._snapshot(t)
        segs = self._split_sections(t, allowed=set(want))
        parts: list[str] = []
        for name in want:
            if name == "快照摘要":
                if snap:
                    parts.append(snap)
                continue
            body = segs.get(name)
            if body:
                parts.append(f"{name}：{self._norm(body)}")
        if not parts:
            # 认不出的格式（别人的库 / 没写小标题）→ 退回摘要，再退回全文开头
            parts.append(snap or self._norm(t))
        return "\n".join(p for p in parts if p)

    def _slim(self, text: str) -> str:
        """按设置页填的「要哪几部分」取内容（留空 = 整条原样），再套安全阀截断。"""
        t = _META_RE.sub("", text or "").strip()  # 元数据尾巴永远剥掉
        if not t:
            return ""
        want = self.sections
        body = self._compose(t, want) if want else self._norm(t)
        if not body:
            return ""
        if self.max_chars and self.max_chars > 0 and len(body) > self.max_chars:
            body = body[: self.max_chars].rstrip() + "…"
        return body

    @staticmethod
    def _iso(value: Any) -> str:
        try:
            ts = int(value)
            if ts > 10_000_000_000:  # 毫秒
                ts //= 1000
            return datetime.fromtimestamp(ts).isoformat(timespec="seconds")
        except Exception:
            return datetime.now().isoformat(timespec="seconds")
