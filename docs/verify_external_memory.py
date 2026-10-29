"""离线自检 · 外部记忆库直连（只读）

**开发者自检脚本**：需要一个可达的 Milvus（里面得有人格数据）才跑得完。

跑法::

    MM_MILVUS=127.0.0.1:19530 python3 docs/verify_external_memory.py

- 地址从环境变量 `MM_MILVUS` 读，不填就用默认 `127.0.0.1:19530`
- 集合同样可用 `MM_MILVUS_COLLECTION` 覆盖，默认 `ai`
- 人格 id / 名字 **从 `persona_defs/` 动态读**，不写死

验五件事：
  1. 人格映射：persona_id → 库里的人格名
  2. 链路能通：embedding → 向量检索 → 拿到原始记忆（且被截过）
  3. **人格隔离**：同一个查询向量喂不同人格，回来的必须是各自的人
  4. 降级不炸：地址填错 / `backend=off`，都只返回 []，绝不抛异常
  5. 门面：主路通走主路；全关时空手

不碰任何写入接口。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
CORE = PLUGIN / "core"
# 数据目录默认按插件位置推：<主目录>/data/plugin_data/<插件名>，也可用 MM_PLUGIN_DATA 覆盖
_DATA_DEFAULT = PLUGIN.parents[1] / "plugin_data" / PLUGIN.name
DATA_DIR = Path(os.environ.get("MM_PLUGIN_DATA", str(_DATA_DEFAULT)))
ADDRESS = os.environ.get("MM_MILVUS", "127.0.0.1:19530")
COLLECTION = os.environ.get("MM_MILVUS_COLLECTION", "ai")


def discover_personas() -> list[tuple[str, str]]:
    """从 persona_defs/*.json 读出 (persona_id, 人格名)，不写死。"""
    rows: list[tuple[str, str]] = []
    for f in sorted((DATA_DIR / "persona_defs").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        pid = str(d.get("id") or "").strip()
        name = str(d.get("name") or "").strip()
        if pid and name:
            rows.append((pid, name))
    return rows


# ── 造一个包壳，让插件的相对导入（from .state_store import ...）能解析 ──
_pkg = types.ModuleType("mmtest")
_pkg.__path__ = [str(CORE)]
sys.modules["mmtest"] = _pkg


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"mmtest.{name}", CORE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"mmtest.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


_load("state_store")
_ext = _load("external_memory")
_memsrc = _load("memory_source")
ExternalMemory = _ext.ExternalMemory
MemorySource = _memsrc.MemorySource


class FakeProvider:
    """假 embedding：按 query 文本查表给向量，省掉真调模型。"""

    def __init__(self, table):
        self.table = table

    async def get_embedding(self, text):
        return self.table.get(text)


class FakeContext:
    def __init__(self, provider):
        self._p = provider

    def get_all_embedding_providers(self):
        return [self._p]

    def get_provider_by_id(self, _pid):
        return None


class FakePlugin:
    def __init__(self, cfg, provider):
        self._cfg = cfg
        self.context = FakeContext(provider)

    def get_config(self, key, default=None):
        cur = self._cfg
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return default
        return cur


def cfg(**over):
    base = {
        "external_memory": {
            "backend": "milvus",
            "address": ADDRESS,
            "collection": COLLECTION,
            "token": "",
            "embedding_provider_id": "",
            "persona_map": "",
            "top_k": 3,
            "timeout": 5,
            "sections": ["快照摘要", "记忆内核"],
            "max_chars": 1200,
        },
        "bridge": {"enabled": False},
    }
    base["external_memory"].update(over)
    return base


async def main() -> int:
    try:
        from pymilvus import MilvusClient
    except ImportError:
        print("⚠️ 没装 pymilvus，跳过（这个脚本只在装了它的环境里跑）")
        return 0

    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  ✅ " if cond else "  ❌ ") + msg)
        if not cond:
            ok = False

    personas = discover_personas()
    if not personas:
        print(f"⏭️ {DATA_DIR}/persona_defs 里没找到人格定义 —— 本次自检跳过，未验证任何项")
        return 0
    print(f"靶子: {ADDRESS}/{COLLECTION}")
    print(f"人格: " + "、".join(f"{p}→{n}" for p, n in personas) + "\n")

    try:
        cli = MilvusClient(f"http://{ADDRESS}")
    except Exception as e:
        print(f"⏭️ 连不上 Milvus（{ADDRESS}）：{e}")
        print("   本次自检跳过 —— 这是没验，不是通过。查一下 MM_MILVUS 地址，或先起 Milvus 再跑。")
        return 0

    # 拿各人格的真向量当"查询向量"
    vecs: dict[str, list] = {}
    for pid, name in personas[:2]:
        rows = cli.query(collection_name=COLLECTION,
                         filter=f'personality_id == {json.dumps(name, ensure_ascii=False)}',
                         output_fields=["embedding"], limit=1)
        if rows:
            vecs[name] = rows[0]["embedding"]
    if not vecs:
        print("⚠️ 库里没查到这个人的数据，跳过链路/隔离测试")
        return 0
    print(f"查询向量: " + "、".join(f"{k}({len(v)}维)" for k, v in vecs.items()) + "\n")

    plug = FakePlugin(cfg(), FakeProvider({f"{n}查询": v for n, v in vecs.items()}))
    mem = ExternalMemory(plug, DATA_DIR)

    print("① 状态与映射")
    check(mem.available(), f"available() = {mem.available()}  ({mem.describe()})")
    for pid, name in personas:
        got = mem.resolve_persona(pid)
        check(got == name, f"{pid} → {got}（期望 {name}）")

    print("\n② 链路（主路检索）")
    first_pid, first_name = personas[0]
    query = f"{first_name}查询"
    hits = await mem.search(first_pid, query)
    check(len(hits) > 0, f"{first_name}命中 {len(hits)} 条")
    if hits:
        check(all(len(h["text"]) <= mem.max_chars + 1 for h in hits),
              f"每条都没超安全阀：字数 {[len(h['text']) for h in hits]}")
        print(f"     样例: {hits[0]['text'][:90]}…")
    check(mem._metric is not None, f"距离度量自动探测 = {mem._metric}")

    print("\n②b 记忆要哪几部分（留空 = 整条原样，安全阀关掉看真实长度）")
    lengths: dict[str, int] = {}
    prov = FakeProvider({f"{n}查询": v for n, v in vecs.items()})
    cases = [("整条原样", []),
             ("只要摘要", ["快照摘要"]),
             ("摘要+记忆内核", ["快照摘要", "记忆内核"]),
             ("自选自由名", ["快照摘要", "在场的人"])]
    for label, secs in cases:
        m = ExternalMemory(FakePlugin(cfg(sections=secs, max_chars=0), prov), DATA_DIR)
        got = await m.search(first_pid, query)
        lens = [len(h["text"]) for h in got]
        check(len(got) > 0, f"{label:12s} {m.mode_text():26s} 字数 {lens}")
        if got:
            lengths[label] = len(got[0]["text"])
    if {"只要摘要", "摘要+记忆内核", "整条原样"} <= set(lengths):
        check(lengths["只要摘要"] < lengths["摘要+记忆内核"] < lengths["整条原样"],
              "层层加量：摘要 {0} < 摘要+内核 {1} < 整条 {2}".format(
                  lengths["只要摘要"], lengths["摘要+记忆内核"], lengths["整条原样"]))

    if len(personas) >= 2:
        print("\n③ 人格隔离（同一个向量，喂不同人格）")
        p2_pid, p2_name = personas[1]
        hits2 = await mem.search(p2_pid, f"{p2_name}查询")
        check(len(hits2) > 0, f"{p2_name}命中 {len(hits2)} 条")
        same = {h["text"] for h in hits} & {h["text"] for h in hits2}
        check(not same, f"两边结果无交集（交集 {len(same)} 条）")

    print("\n④ 降级不炸")
    bad = ExternalMemory(FakePlugin(cfg(address="127.0.0.1:1"), FakeProvider({})), DATA_DIR)
    r = await bad.search(first_pid, "随便")
    check(r == [], f"地址不通 → 返回 {r}（不抛异常）")
    off = ExternalMemory(FakePlugin(cfg(backend="off"), FakeProvider({})), DATA_DIR)
    check(await off.search(first_pid, "随便") == [] and not off.available(),
          "backend=off → 完全不碰外部库")

    print("\n⑤ 门面（主路 → 兜底）")
    src = MemorySource(plugin=plug, external=mem, pool=None)
    check(src.ready(), f"ready() = {src.ready()}  ({src.describe()})")
    check(len(await src.retrieve(first_pid, f"{first_name}查询", limit=3)) > 0, "门面拿到结果")
    src_off = MemorySource(plugin=FakePlugin(cfg(backend="off"), FakeProvider({})),
                           external=off, pool=None)
    check(await src_off.retrieve(first_pid, "x", limit=3) == [] and not src_off.ready(),
          "全关 → 空手，不报错")

    print("\n" + ("🎉 全绿" if ok else "⚠️ 有红项"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
