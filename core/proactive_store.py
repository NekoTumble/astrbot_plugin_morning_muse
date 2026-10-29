"""晨光心语 · 主动消息的落盘层（订阅白名单 + 发送账本）

两个文件，都放在 `data/plugin_data/astrbot_plugin_morning_muse/` 下：

1. `subscriptions.json` —— 「我能主动去找谁」的白名单。
   按 unified_msg_origin（UMO）存，每条记着绑定的人格、私聊/群聊、订阅时间。
   **没订阅过的会话永远不会收到主动消息**——这是硬门槛，不是开关。

2. `proactive_state.json` —— 主动消息的账本：
   今天发了几条、每根导火索上次是什么时候用的、连续几条没人理、
   有没有一条被刹车拦下、正延后等着发……重启不丢。

设计原则（跟插件其他地方保持一致）：
- 写盘一律「临时文件 + os.replace」原子替换，写坏了也不至于把 JSON 撕成两半；
- **所有失败都只记日志、返回降级结果**，绝不把异常抛给主流程。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiofiles

try:
    from astrbot.api import logger
except ImportError:  # 独立自测
    import logging

    logger = logging.getLogger(__name__)

# 账本里最多留几条「最近发生」的记录（给 /晨光 主动状态 看个大概就够）
_MAX_HISTORY = 30
_MAX_SKIPPED = 5      # 「这次不说」的记录只留最近几条
# 已提过的旧事最多记多少个指纹，防止账本无限膨胀
_MAX_MENTIONED = 60
# F7：「每个会话最后说话的时间」这张表原来只增不减（别的表都有 _MAX_*，唯独漏了它）。
# 会话（UMO）是会越攒越多的，所以按时间戳只留最近 N 个。
_MAX_UMO_ACTIVITY = 64
# 延后队列最多留这么长（小时），过期就丢掉，别把陈年旧事翻出来说
_DEFAULT_DEFER_MAX_HOURS = 10
# 延后队列最多攒几件 —— 封顶，绝不无限涨（满了丢最老的）
_DEFAULT_DEFER_MAX_ITEMS = 3


def _skipped_fresh(at_iso: Any, max_age_hours: int) -> bool:
    """这条「没说出口」还新鲜吗？

    时间戳读不懂就当它新鲜 —— 宁可多给她一条上下文，也不误删。
    """
    try:
        t = datetime.fromisoformat(str(at_iso))
    except (TypeError, ValueError):
        return True
    try:
        now = datetime.now(t.tzinfo) if t.tzinfo is not None else datetime.now()
        return (now - t) <= timedelta(hours=max_age_hours)
    except Exception:
        return True


def format_skipped_line(item: dict[str, Any]) -> str:
    """把一条「想说、但没说出口」摆成一行给人看。

    形如 `10-04 13:49 想说：……（当时没说：……）`。

    N2 修复：条目存的是 `at / trigger / detail / why`，早先两处 UI 读的却是
    `text / note`（那是「已发出历史」的字段），所以「💭 最近想说没说出口的」
    永远是空的。顺带把**日期**带上 —— 只显示 HH:MM 会让人以为是今早的事。
    """
    it = item or {}
    at = str(it.get("at") or "")
    when = at[5:16].replace("T", " ") if len(at) >= 16 else ""
    detail = str(it.get("detail") or "").strip()
    why = str(it.get("why") or "").strip()
    if not detail:
        return ""
    line = f"{when} 想说：{detail}" if when else f"想说：{detail}"
    if why:
        line += f"（当时没说：{why}）"
    return line


def detect_chat_type(umo: str) -> str:
    """从 UMO 里认出这是私聊还是群聊。

    UMO 形如 `platform_name:message_type:session_id`，message_type 一般是
    `friend_message` / `group_message`。认不出来的按私聊算（少打扰群）。
    """
    parts = str(umo or "").split(":", 2)
    if len(parts) >= 2 and "group" in parts[1].lower():
        return "group"
    return "private"


class SubscriptionStore:
    """「她可以主动去找谁」的白名单。私聊 / 群聊分开索引。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._file = self.data_dir / "subscriptions.json"
        self._lock = asyncio.Lock()
        # {umo: {"persona_id": str, "chat_type": "private|group", "added_at": iso}}
        self._subs: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._load_failed = False

    # ─────────────────────── 读写 ───────────────────────

    async def load(self):
        if self._loaded:
            return
        if self._file.exists():
            try:
                async with aiofiles.open(self._file, encoding="utf-8") as f:
                    content = await f.read()
                data = json.loads(content)
                subs = data.get("subscriptions") if isinstance(data, dict) else None
                if isinstance(subs, dict):
                    self._subs = {
                        str(k): v for k, v in subs.items() if isinstance(v, dict)
                    }
                logger.info(
                    f"[晨光心语] 🔔 主动消息订阅表已加载：{len(self._subs)} 个会话"
                )
                self._load_failed = False
            except Exception as e:
                # ★ 读失败**不能**当成「空表」就这么走下去：之后任何一次订阅/退订
                #   触发的 save()，都会把「只有新条目」的这份表写回去，
                #   把主人攒下的白名单整个抹掉。这里标记住，交给 save() 先备份。
                logger.warning(
                    f"[晨光心语] ⚠️ 读取订阅表失败（本次按空表跑，下次会重试；"
                    f"保存时会先把原文件备份成 .corrupt_*）: {e}"
                )
                self._subs = {}
                self._load_failed = True
                return  # 不置 _loaded：留着下次重试
        self._loaded = True

    async def save(self):
        async with self._lock:
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                if self._load_failed and self._file.exists():
                    # 原表读不出来（坏了 / 权限），先留个带时间戳的副本再写，
                    # 别直接盖掉 —— 那份白名单可能是唯一的一份。
                    backup = self._file.with_name(
                        self._file.name + ".corrupt_"
                        + datetime.now().strftime("%Y%m%d_%H%M%S")
                    )
                    try:
                        os.replace(self._file, backup)
                        logger.warning(
                            f"[晨光心语] ⚠️ 原订阅表读取失败，已备份为 {backup.name} 再写新表"
                        )
                    except Exception as e:
                        logger.warning(f"[晨光心语] ⚠️ 备份坏订阅表失败（继续写）: {e}")
                self._load_failed = False
                payload = json.dumps(
                    {
                        "version": 1,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                        "subscriptions": self._subs,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                tmp = self._file.with_suffix(".json.tmp")
                async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
                    await f.write(payload)
                os.replace(tmp, self._file)
                self._loaded = True
            except Exception as e:
                logger.warning(f"[晨光心语] ⚠️ 保存订阅表失败: {e}")

    # ─────────────────────── 查询 ───────────────────────

    def all(self) -> dict[str, dict[str, Any]]:
        return self._subs

    def list_all(self) -> list[dict[str, Any]]:
        """按订阅时间排好序的全部条目（给 /晨光 订阅列表 用）。"""
        rows = []
        for umo, rec in self._subs.items():
            rows.append({
                "umo": umo,
                "persona_id": str(rec.get("persona_id") or ""),
                "chat_type": str(rec.get("chat_type") or detect_chat_type(umo)),
                "added_at": str(rec.get("added_at") or ""),
            })
        rows.sort(key=lambda r: r.get("added_at") or "")
        return rows

    def is_subscribed(self, umo: str) -> bool:
        return str(umo or "") in self._subs

    def targets_for_persona(self, persona_id: str,
                            chat_types: tuple[str, ...] | None = None) -> list[str]:
        """某人格可以主动发消息的会话列表。私聊排在前面（群聊打扰成本高）。"""
        pid = str(persona_id or "")
        out: list[str] = []
        for umo, rec in self._subs.items():
            if str(rec.get("persona_id") or "") != pid:
                continue
            ctype = str(rec.get("chat_type") or detect_chat_type(umo))
            if chat_types is not None and ctype not in chat_types:
                continue
            out.append(umo)
        out.sort(key=lambda u: 0 if detect_chat_type(u) == "private" else 1)
        return out

    def count(self) -> int:
        return len(self._subs)

    # ─────────────────────── 改写 ───────────────────────

    async def subscribe(self, umo: str, persona_id: str) -> tuple[bool, str]:
        """把会话加进白名单。返回 (是否新加, 给人看的一句话)。"""
        umo = str(umo or "")
        if not umo:
            return False, "⚠️ 拿不到这个会话的标识，订阅失败喵。"
        await self.load()
        chat_type = detect_chat_type(umo)
        now_iso = datetime.now().isoformat(timespec="seconds")
        old = self._subs.get(umo)
        self._subs[umo] = {
            "persona_id": str(persona_id or ""),
            "chat_type": chat_type,
            "added_at": (old or {}).get("added_at") or now_iso,
            "updated_at": now_iso,
        }
        await self.save()
        kind = "群聊" if chat_type == "group" else "私聊"
        if old:
            if str(old.get("persona_id") or "") != str(persona_id or ""):
                return True, (
                    f"🔔 已把这个{kind}会话的订阅人格改成「{persona_id}」喵。"
                )
            return False, f"🔔 这个{kind}会话本来就在订阅里，小白记着呐。"
        return True, f"🔔 已订阅：以后到点、或者她很想说话的时候，会来这个{kind}找你喵。"

    async def unsubscribe(self, umo: str) -> tuple[bool, str]:
        umo = str(umo or "")
        await self.load()
        if umo not in self._subs:
            return False, "🔕 这个会话本来就没订阅喵，不用退。"
        self._subs.pop(umo, None)
        await self.save()
        return True, "🔕 已退订：以后不会主动来这个会话打扰了喵。"

    def forget(self, umo: str):
        self._subs.pop(str(umo or ""), None)


class ProactiveState:
    """主动消息的账本（预算 / 冷却 / 未回应 / 延后队列），跨重启不丢。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._file = self.data_dir / "proactive_state.json"
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {"version": 1, "personas": {}}
        self._loaded = False

    # ─────────────────────── 读写 ───────────────────────

    async def load(self):
        if self._loaded:
            return
        if self._file.exists():
            try:
                async with aiofiles.open(self._file, encoding="utf-8") as f:
                    content = await f.read()
                data = json.loads(content)
                if isinstance(data, dict) and isinstance(data.get("personas"), dict):
                    self._data = data
                    self._data.setdefault("version", 1)
                    logger.info(
                        f"[晨光心语] 📔 主动消息账本已加载："
                        f"{len(self._data['personas'])} 个人格"
                    )
            except Exception as e:
                logger.warning(f"[晨光心语] ⚠️ 读取主动消息账本失败（按空账跑）: {e}")
                self._data = {"version": 1, "personas": {}}
        self._loaded = True

    async def save(self):
        async with self._lock:
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                payload = json.dumps(self._data, ensure_ascii=False, indent=2)
                tmp = self._file.with_suffix(".json.tmp")
                async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
                    await f.write(payload)
                os.replace(tmp, self._file)
                self._loaded = True
            except Exception as e:
                logger.warning(f"[晨光心语] ⚠️ 保存主动消息账本失败: {e}")

    # ─────────────────────── 人格账本 ───────────────────────

    @staticmethod
    def _blank() -> dict[str, Any]:
        """一个人格的空白账本。"""
        return {
            "daily_date": "",
            "daily_count_private": 0,
            "daily_count_group": 0,
            "trigger_used": {},        # 导火索 key -> 上次使用的 ISO 时间
            "trigger_daily_count": {},  # 导火索 key -> 今天已发出的次数（「每天上限」用）
            "last_sent_at": "",
            "last_sent_umo": "",
            "consecutive_unanswered": 0,
            "paused": False,
            "unanswered_reset_date": "",  # 「睡醒重置连败账」最近一次生效的日期（YYYY-MM-DD）
            "last_user_at": "",
            "last_user_by_umo": {},    # 每个会话各自的「主人最后开口」（聊天时别插嘴用）
            "last_ledger_check": "",
            "mentioned_events": {},    # 已提过的账本事件 id -> ISO
            "mentioned_memories": [],  # 已提过的记忆指纹（只留最近 N 个）
            "weather_snapshot": {},    # {"date":..., "min":..., "max":..., "desc":...}
            "deferred": None,          # 被刹车拦下的那几件（封顶队列），等下一个可发时段
            "history": [],             # 最近发过什么的摘要（最多 _MAX_HISTORY 条）
            "skipped": [],             # 她自己决定「这次不说」的记录（最多 _MAX_SKIPPED 条）
        }

    async def ensure(self, persona_id: str, today_iso: str = "") -> dict[str, Any]:
        """取某人格的账本；顺手做一次「跨天重置」。"""
        await self.load()
        pid = str(persona_id or "default")
        personas = self._data.setdefault("personas", {})
        st = personas.get(pid)
        if not isinstance(st, dict):
            st = self._blank()
            personas[pid] = st
        # 补齐后来新增的字段（老账本也能平滑升上来）
        for k, v in self._blank().items():
            st.setdefault(k, v)
        if today_iso and st.get("daily_date") != today_iso:
            # 新的一天：预算和导火索的「当天一次」清空，别的（未回应 / 延后）接着走
            st["daily_date"] = today_iso
            st["daily_count_private"] = 0
            st["daily_count_group"] = 0
            st["trigger_used"] = {}
            st["trigger_daily_count"] = {}
        return st

    def raw(self, persona_id: str) -> dict[str, Any] | None:
        return self._data.get("personas", {}).get(str(persona_id or ""))

    def all_personas(self) -> dict[str, Any]:
        return self._data.get("personas", {}) or {}

    # ─────────────────────── 记账动作 ───────────────────────

    async def mark_sent(self, persona_id: str, trigger_key: str,
                        now_iso: str, umo: str, chat_type: str,
                        note: str = "", text: str = "") -> dict[str, Any]:
        """记一笔「发出去了」。text = 发出去的原话（用来做「别重复说过的话」）。"""
        st = await self.ensure(persona_id)
        st["last_sent_at"] = now_iso
        st["last_sent_umo"] = umo
        st["trigger_used"][trigger_key] = now_iso
        # 每天上限的计数：**真发出去了才 +1**（被刹车拦下的不算）
        _tdc = st.setdefault("trigger_daily_count", {})
        _key = str(trigger_key)
        _tdc[_key] = int(_tdc.get(_key) or 0) + 1
        if chat_type == "group":
            st["daily_count_group"] = int(st.get("daily_count_group") or 0) + 1
        else:
            st["daily_count_private"] = int(st.get("daily_count_private") or 0) + 1
        st["consecutive_unanswered"] = int(st.get("consecutive_unanswered") or 0) + 1
        # 延后队列里那件要真补发出去了，就只撤这一件；别的还留着
        _raw = st.get("deferred")
        if _raw:
            _rest = [
                it for it in self.defer_items(_raw)
                if str((it.get("payload") or {}).get("key") or "") != str(trigger_key)
            ]
            st["deferred"] = self._defer_pack(_rest, self.defer_hours(_raw))
        hist = list(st.get("history") or [])
        hist.append({
            "at": now_iso,
            "trigger": trigger_key,
            "umo": umo,
            "note": str(note or "")[:60],
            "text": str(text or "")[:160],   # 原话；老数据没这个字段，读的时候要兜底
        })
        st["history"] = hist[-_MAX_HISTORY:]
        await self.save()
        return st

    async def note_user_activity(self, persona_id: str, umo: str = "",
                                 now_iso: str = "") -> None:
        """主人说话了：解除「连续未回应暂停」，并把计数归零。"""
        st = await self.ensure(persona_id)
        changed = False
        if st.get("consecutive_unanswered"):
            st["consecutive_unanswered"] = 0
            changed = True
        if st.get("paused"):
            st["paused"] = False
            changed = True
            logger.info(f"[晨光心语] 💬 {persona_id} 主人开口了，主动消息恢复")
        now_iso = now_iso or datetime.now().isoformat(timespec="seconds")
        st["last_user_at"] = now_iso
        # 会话级也记一笔 —— 这是「主人正在聊天」的判据（聊天时不插嘴）。
        # 跟 last_user_at 一样走内存、不强制落盘：每条消息都写盘太吵，
        # 重载后靠下一跳心跳自然补齐。
        if umo:
            per = st.get("last_user_by_umo")
            if not isinstance(per, dict):
                per = st["last_user_by_umo"] = {}
            per[str(umo)] = now_iso
            # F7：按时间戳留最近 N 条。刚写进去的这个 UMO 一定是「最新」的，
            # 所以「主人正在聊天」用的那条永远不会被裁掉。
            if len(per) > _MAX_UMO_ACTIVITY:
                for _k in sorted(per, key=lambda x: str(per.get(x) or ""))[
                    : len(per) - _MAX_UMO_ACTIVITY
                ]:
                    per.pop(_k, None)
        if changed:
            await self.save()

    def last_user_at_umo(self, persona_id: str, umo: str) -> str:
        """某个会话里「主人最后开口」的时间（ISO）。没有就返回空串。

        读的是 note_user_activity 顺手写的**会话级**那份；拿不到时调用方
        会退回人格级的 last_user_at。
        """
        st = self.raw(persona_id) or {}
        per = st.get("last_user_by_umo")
        if isinstance(per, dict):
            return str(per.get(str(umo or "")) or "")
        return ""

    async def set_paused(self, persona_id: str, paused: bool) -> None:
        st = await self.ensure(persona_id)
        if bool(st.get("paused")) != bool(paused):
            st["paused"] = bool(paused)
            await self.save()

    async def reset_unanswered(self, persona_id: str, today_iso: str = "",
                               why: str = "睡醒") -> bool:
        """睡醒 → 把「连续未回应」这本账清零（设置页开关控制，默认不开）。

        只动两个「连败」记号：`consecutive_unanswered` 和 `paused`。
        **每日额度 / 延后队列 / 说过的由头一概不碰** —— 那几本是另外的账，
        睡醒该重算的只有「她昨天说了两条没人理，就别再开口了」这一条。

        `unanswered_reset_date` 记下「今天已经重置过」，重载插件、重启都不会重复刷；
        跨天时它会自己变成「不是今天」，于是下一个早晨又会生效一次。
        返回：这次真的清到了什么没有（给日志用；已经清零过就返回 False）。
        """
        st = await self.ensure(persona_id)
        today_iso = today_iso or datetime.now().date().isoformat()
        changed = False
        if int(st.get("consecutive_unanswered") or 0):
            st["consecutive_unanswered"] = 0
            changed = True
        if st.get("paused"):
            st["paused"] = False
            changed = True
        if str(st.get("unanswered_reset_date") or "") != today_iso:
            st["unanswered_reset_date"] = today_iso
            changed = True
        if changed:
            logger.info(
                f"[晨光心语] 🌅 {persona_id} {why}，连续未回应清零（今天可以重新主动开口）"
            )
            await self.save()
        return changed

    # ───────── 延后队列（封顶的，绝不让账本膨胀） ─────────

    @staticmethod
    def defer_items(raw: Any) -> list[dict[str, Any]]:
        """把延后队列统一成列表。兼容最早的「单条」老格式。"""
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        if isinstance(raw, dict):
            if isinstance(raw.get("items"), list):
                return [x for x in raw["items"] if isinstance(x, dict)]
            if raw.get("payload"):
                return [{"payload": raw.get("payload"), "queued_at": raw.get("queued_at")}]
        return []

    @staticmethod
    def defer_hours(raw: Any) -> int:
        """队列整体的保质期（小时）。"""
        if isinstance(raw, dict):
            try:
                v = int(raw.get("expire_at_hours") or 0)
                if v >= 1:
                    return v
            except (TypeError, ValueError):
                pass
        return _DEFAULT_DEFER_MAX_HOURS

    @staticmethod
    def _defer_pack(items: list[dict[str, Any]], hours: int) -> dict[str, Any] | None:
        """装回账本。空队列就写 None，不留空壳。"""
        if not items:
            return None
        return {"items": list(items), "expire_at_hours": max(1, int(hours))}

    async def defer(self, persona_id: str, payload: dict[str, Any],
                    now_iso: str, max_hours: int = _DEFAULT_DEFER_MAX_HOURS,
                    max_items: int = _DEFAULT_DEFER_MAX_ITEMS,
                    reason: str = "") -> None:
        """把「被刹车拦下的那一条」记进延后队列，等下一个可发时段。

        队列是**封顶**的（默认 3 件）：
        - 同一个由头只占一格 —— 再被拦一次只刷新排队时间，不会堆成一串；
        - 超出上限就丢掉最老的那件，绝不无限涨。

        `reason` = 为什么被拦的（如「主人正在跟你说话」）。它会被写进 payload，
        补发时提示词里会告诉她 —— 让她自己判断这句话现在还值不值得说。
        """
        st = await self.ensure(persona_id)
        raw = st.get("deferred")
        payload = dict(payload or {})
        if reason:
            # ⚠️ 要写进**内层**的 payload —— to_pending() 是 {key,..,payload:{}} 两层，
            #    补发时 TriggerHit.from_pending() 只认里面那层。写外层等于白写。
            inner = payload.get("payload")
            if not isinstance(inner, dict):
                inner = payload["payload"] = {}
            inner["blocked_reason"] = str(reason)[:120]
        key = str((payload or {}).get("key") or "")
        old_items = [
            it for it in self.defer_items(raw)
            if str((it.get("payload") or {}).get("key") or "") == key
        ]
        items = [
            it for it in self.defer_items(raw)
            if str((it.get("payload") or {}).get("key") or "") != key
        ]
        # R2-1 修复：同一个由头再次被拦，**不刷新排队时间** ——
        # 保质期度量的是「第一次被拦」，否则一个念头能整夜续命到次日照发。
        queued_at = (old_items[0].get("queued_at") if old_items else None) or now_iso
        items.append({"payload": dict(payload or {}), "queued_at": queued_at})
        keep = max(1, int(max_items or _DEFAULT_DEFER_MAX_ITEMS))
        # 保质期一律按**当前配置**走：主人把默认从 6 改成 10 之后，
        # 老队列里残留的旧值不该把新写的也一起拖住
        new_raw = self._defer_pack(
            items[-keep:], max(1, int(max_hours or _DEFAULT_DEFER_MAX_HOURS))
        )
        # 反复被拦（典型场景：主人一直在聊，闹钟每分钟醒一次）——
        # 队列一个字都没变的时候别再重写账本，省掉一堆无意义的写盘。
        if new_raw == raw:
            return
        st["deferred"] = new_raw
        await self.save()

    async def clear_deferred(self, persona_id: str, key: str = "") -> None:
        """清延后队列。给了 key 就只撤那一件，不给就整个清空。"""
        st = await self.ensure(persona_id)
        raw = st.get("deferred")
        if not raw:
            return
        if key:
            rest = [
                it for it in self.defer_items(raw)
                if str((it.get("payload") or {}).get("key") or "") != str(key)
            ]
            st["deferred"] = self._defer_pack(rest, self.defer_hours(raw))
        else:
            st["deferred"] = None
        await self.save()

    # ───────── 自由裁量：她说过的 / 她想说但没说的 ─────────

    async def record_skipped(self, persona_id: str, trigger_key: str,
                             detail: str, why: str, now_iso: str) -> None:
        """她这次自己决定不说：记下来。

        下次生成主动消息时会把这些摆回她眼前（「你最近想说、但最后没说出口的」），
        免得「拿不准就别发」一路憋成哑巴。
        ⚠️ **不占额度、也不把由头标记成用过** —— 这两件事由调用方保证。
        """
        st = await self.ensure(persona_id)
        sk = list(st.get("skipped") or [])
        sk.append({
            "at": now_iso,
            "trigger": trigger_key,
            "detail": str(detail or "")[:80],
            "why": str(why or "")[:80],
        })
        st["skipped"] = sk[-_MAX_SKIPPED:]
        await self.save()

    def recent_said(self, persona_id: str, limit: int = 2) -> list[str]:
        """最近发出去的几句话（做「别重复说过的话」用）。"""
        st = self.raw(persona_id) or {}
        out: list[str] = []
        for h in (st.get("history") or [])[-max(1, limit):]:
            if not isinstance(h, dict):
                continue
            t = str(h.get("text") or "").strip() or str(h.get("note") or "").strip()
            if t:
                out.append(t)
        return out

    def recent_skipped(self, persona_id: str, limit: int = 3,
                       max_age_hours: int | None = None) -> list[dict[str, Any]]:
        """最近「想说、但最后没说出口」的几件事。

        `max_age_hours`：只要这个时长以内的（None = 不过滤）。
        N2 配套修复 —— 不设窗口的话，三天前憋下的话会被摆到提示词里、
        看着像今早刚发生的。
        """
        st = self.raw(persona_id) or {}
        items = [h for h in (st.get("skipped") or []) if isinstance(h, dict)]
        if max_age_hours and max_age_hours > 0:
            items = [h for h in items if _skipped_fresh(h.get("at"), max_age_hours)]
        return items[-max(1, limit):]

    async def mark_mentioned_event(self, persona_id: str, event_id: str,
                                   now_iso: str) -> None:
        st = await self.ensure(persona_id)
        if not event_id:
            return
        events = st.setdefault("mentioned_events", {})
        events[str(event_id)] = now_iso
        # A8 修复：按时间留最近 N 条，别让它无限长（跟 mentioned_memories 一个规矩）。
        if len(events) > _MAX_MENTIONED:
            keep = sorted(events.items(), key=lambda kv: str(kv[1]))[-_MAX_MENTIONED:]
            st["mentioned_events"] = dict(keep)
        await self.save()

    async def mark_mentioned_memory(self, persona_id: str, fingerprint: str) -> None:
        st = await self.ensure(persona_id)
        if not fingerprint:
            return
        items = list(st.get("mentioned_memories") or [])
        if fingerprint in items:
            items.remove(fingerprint)
        items.append(str(fingerprint))
        st["mentioned_memories"] = items[-_MAX_MENTIONED:]
        await self.save()

    async def set_ledger_check(self, persona_id: str, now_iso: str) -> None:
        st = await self.ensure(persona_id)
        st["last_ledger_check"] = now_iso
        await self.save()

    async def set_weather_snapshot(self, persona_id: str, snapshot: dict[str, Any]) -> None:
        st = await self.ensure(persona_id)
        st["weather_snapshot"] = dict(snapshot or {})
        await self.save()
