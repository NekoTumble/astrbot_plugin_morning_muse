"""晨光心语 · 记忆来源门面（主路 + 兜底）

上层（日程生成 / 主动消息）只认这一个入口：``retrieve(persona_id, query, limit)``。
内部按顺序找：

  ① **主路** 外部记忆库（`external_memory.py`，只读）
  ② **兜底** 本地池（`memory_bridge.py`，接 `astrbot_plugin_livingmemory` 那种短记忆，
     默认关；主路不可达、或主路空手时才顶上）

这样 context_builder / proactive 里的调用点一个字都不用改——
它们原来用的就是 ``memory_bridge.retrieve(...)``，现在换成这个门面而已。

换句话说：**池子不扫了，但没被删** —— 它从"唯一的路"退成"万一的那一层"。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger


class MemorySource:
    """外部库（主路）→ 本地池（兜底）的统一检索入口。"""

    def __init__(self, plugin=None, external=None, pool=None):
        self.plugin = plugin
        self.external = external
        self.pool = pool

    # ─────────────────────── 状态 ───────────────────────

    def pool_enabled(self) -> bool:
        if self.pool is None or self.plugin is None:
            return False
        try:
            return bool(self.plugin.get_config("bridge.enabled", False))
        except Exception:
            return False

    def external_enabled(self) -> bool:
        return self.external is not None and self.external.available()

    def ready(self) -> bool:
        """有任意一条路能走，就算可用。上层拿这个决定要不要去检索。"""
        return self.external_enabled() or self.pool_enabled()

    def describe(self) -> str:
        parts = []
        if self.external is not None:
            parts.append(f"主路={self.external.describe()}" if self.external_enabled() else "主路=off")
        else:
            parts.append("主路=未装")
        parts.append(f"兜底池={'开' if self.pool_enabled() else '关'}")
        return " ｜ ".join(parts)

    # ─────────────────────── 检索 ───────────────────────

    async def retrieve(self, persona_id: str, query: str, limit: int = 3,
                       days: int = 30) -> list[dict[str, Any]]:
        """先问外部库；空手或不可达，再看兜底池。"""
        if self.external_enabled():
            try:
                # 透传调用方的 limit。以前硬传 None，外部库回落到自己的 top_k(=3)，
                # 于是「旧事想起」设计上的 top6 和日程侧的 bridge.max_events 全部静默失效。
                hits = await self.external.search(persona_id, query, limit=limit)
            except Exception as e:  # 双保险：外部库自己已经吞了，这里再兜一层
                hits = []
                logger.warning(f"[晨光心语] ⚠️ 外部记忆库检索异常（回落兜底）: {e}")
            if hits:
                return hits
            logger.debug("[晨光心语] 主路空手，看看兜底池有没有")

        if self.pool_enabled():
            try:
                return await self.pool.retrieve(persona_id, query, limit=limit, days=days)
            except Exception as e:
                logger.warning(f"[晨光心语] ⚠️ 兜底池检索异常: {e}")
        return []

    # ─────────────────────── 统计 / 维护 ───────────────────────

    async def stats(self, persona_id: str) -> dict[str, Any]:
        out: dict[str, Any] = {"external": "off", "external_mode": "", "pool": 0}
        if self.external is not None:
            if self.external_enabled():
                out["external"] = self.external.describe()
                try:
                    out["external_mode"] = self.external.mode_text()
                except Exception:
                    out["external_mode"] = ""
            else:
                out["external"] = "off"
        if self.pool is not None:
            try:
                out["pool"] = (await self.pool.stats(persona_id)).get("total", 0)
            except Exception:
                out["pool"] = 0
        return out

    def forget_cache(self, persona_id: str | None = None):
        if self.pool is not None:
            self.pool.forget_cache(persona_id)
