"""晨光心语 · 中文对话关键词桥

底座 aura-life 的 `CONVERSATION_KEYWORD_MAP` 整张是**英文**的（39 条），
匹配方式是最朴素的 `if keyword in text.lower()`。于是中文环境下一句都命中不了 ——
夸她漂亮、逗她笑、说"好无聊"，全都白说。

这里在**插件侧**补一张中文对照表：主人说的话先过一遍，命中就把对应的
「英文信号」交给 `LifeBridge.apply_zh_triggers()`，由它手动喂给状态机的对应引擎。

设计上的几条硬规矩（踩过才知道的）：

1. **词条至少两个字。** 单字太毒 —— "热"会命中"热闹"、"累"会命中"积累"、
   "爱"会命中"可爱"。宁可漏检，不要误伤。
2. **不动底座。** 只在这一层做映射，底座升级不会把表冲掉。
3. **一条消息里同一个信号只算一次**，跨消息还有冷却（见 life_bridge 里的
   `zh_trigger_cooldown_minutes`），免得"哈哈"一晚上把灵感刷爆。
4. 只补**正向能吃到的那几档**和**负向扣的那几档**，跟英文表逐条对齐。
"""

from __future__ import annotations

# 每一条：(引擎, 底座 trigger_type) -> 中文词表
# 引擎名与底座 LifeService 的私有属性对应：
#   energy -> _energy / affect -> _affect / desire -> _desire_system
#   cognitive -> _cognitive / identity -> _identity / drive -> _drive
ZH_TRIGGER_MAP: dict[tuple[str, str], tuple[str, ...]] = {
    # ────────────────────── 能量（主人最直接能感觉到的） ──────────────────────
    # 正向加的是「灵感加成」（上限 0.25），负向才直接扣基础精力
    ("energy", "exciting_topic"): (
        "好期待", "太期待", "超期待", "等不及", "迫不及待", "好激动", "太激动",
        "兴奋", "好兴奋", "太棒了", "太好了",
    ),
    ("energy", "motivation"): (
        "加油", "继续加油", "别停", "接着说", "接着说下去", "干得好", "做得棒",
        "你可以的", "别放弃", "再加把劲", "再接再厉",
    ),
    ("energy", "boring_topic"): (
        "无聊", "好无聊", "没意思", "没劲", "提不起劲", "枯燥", "乏味", "老套",
    ),
    ("energy", "emotional_drain"): (
        "累死", "好累", "太累了", "累趴", "心累", "精疲力尽", "撑不住", "顶不住",
        "烦死", "好烦", "崩溃", "扛不住",
    ),
    ("energy", "laughter"): (
        "哈哈", "嘿嘿", "嘻嘻", "笑死", "好笑", "逗死", "乐死", "笑不活",
        "笑到", "太搞笑", "好搞笑",
    ),

    # ────────────────────── 情绪 ──────────────────────
    ("affect", "encouragement"): (
        "为你骄傲", "以你为荣", "你真棒", "做得很好", "干得漂亮", "做得好",
        "辛苦你了", "进步很大",
    ),
    ("affect", "emotional_support"): (
        "没事的", "不要紧", "别担心", "不用担心", "别怕", "会好的", "辛苦了",
        "我陪你", "有我在", "抱抱",
    ),
    ("affect", "humor"): (
        "哈哈", "嘿嘿", "笑死", "好笑", "逗死", "太搞笑", "好搞笑", "笑不活",
    ),
    ("affect", "deep_conversation"): (
        "你怎么想", "感觉怎么样", "心里怎么想", "心情如何", "你开心吗", "你在想什么",
    ),
    ("affect", "healthy_debate"): (
        "不同意", "我反对", "我不这么想", "你说错了", "不见得", "未必吧", "未必",
        "各有各的",
    ),

    # ────────────────────── 亲密 / 渴望 ──────────────────────
    ("desire", "compliment_physical"): (
        "好漂亮", "真漂亮", "太漂亮", "好美", "真美", "好好看", "真好看",
        "好可爱", "真可爱", "太可爱", "可爱死",
    ),
    ("desire", "compliment_intimate"): (
        "好性感", "真性感", "好迷人", "真迷人", "好撩人", "好诱人",
    ),
    ("desire", "being_desired"): (
        "想要你", "想要你陪", "需要你", "离不开你", "想有你在",
    ),
    ("desire", "fantasy_sharing"): (
        "幻想", "想象一下", "脑补", "假如有一天",
    ),
    ("desire", "flirting"): (
        "撩你", "撩一下", "逗你玩", "调戏", "逗逗你", "使坏",
    ),
    ("desire", "romantic_conversation"): (
        "想你", "好想你", "很想你", "喜欢你", "好喜欢", "爱你", "好爱你",
        "惦记你", "想见你", "想你了",
    ),

    # ────────────────────── 认知 ──────────────────────
    ("cognitive", "intellectual_topic"): (
        "你觉得", "你认为", "怎么想", "有意思", "很有趣", "还挺有趣", "有点意思",
    ),
    # R7：原先这里还收着「为什么」「万一」—— 两个都太泛（"为什么会这样""万一下雨"全是日常），
    # 群里任何人随口一句都会推着她的情绪走。挪出去，只留明确的假设式说法。
    ("cognitive", "philosophical_question"): (
        "如果说", "假如说", "要是有一天",
    ),
    ("cognitive", "disagreement"): (
        "不同意", "我反对", "不是这样", "不对吧", "我不认同",
    ),
    # R7：「不对」太泛（"不对，应该是……"日常就能说）→ 挪出去，留更明确的否定说法。
    ("cognitive", "defending_opinion"): (
        "才不是", "不是的", "你错了", "说错了",
    ),
    ("cognitive", "compliment_intelligence"): (
        "好聪明", "真聪明", "好机灵", "真机灵", "好厉害", "真厉害", "太厉害了",
    ),
    ("cognitive", "creative_prompt"): (
        "想象一下", "假如有一个", "如果有个", "脑洞",
    ),

    # ────────────────────── 身份 ──────────────────────
    ("identity", "affirmation"): (
        "爱你", "喜欢你", "最好了", "最喜欢你", "你是最好的",
    ),
    ("identity", "validation"): (
        "说得对", "有道理", "很对", "说得没错", "确实是这样",
    ),
    ("identity", "personal_question"): (
        "你想过我吗", "你想我了吗", "你在想我吗",
    ),
    ("identity", "shared_interest"): (
        "我也是", "我也一样", "同感", "我也是这么想的", "我也这么觉得",
    ),
    ("identity", "standing_ground"): (
        "求同存异", "各有各的", "各退一步",
    ),

    # ────────────────────── 驱动 ──────────────────────
    ("drive", "comfort_zone_push"): (
        "你该试试", "要不要试试", "试试看", "去试试", "换换口味",
    ),
    ("drive", "interesting_topic"): (
        "我好奇", "好好奇", "真好奇", "想知道", "挺好奇",
    ),
    ("drive", "accomplishment_shared"): (
        "做到了", "成功了", "搞定了", "弄好了", "完成了", "弄完了",
    ),
    ("drive", "new_idea"): (
        "新点子", "有个想法", "想到一个", "不如我们",
    ),
}


def parse_extra_keywords(raw: str) -> dict[tuple[str, str], tuple[str, ...]]:
    """解析主人自己加的中文信号词（E5）。

    格式：一行一条 `引擎|信号|词1,词2,词3`；`#` 开头是注释，空行忽略。
    看不懂的行**直接跳过**，绝不因为一行写错就让整张表失效。
    """
    out: dict[tuple[str, str], tuple[str, ...]] = {}
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        engine = parts[0].strip()
        trigger = parts[1].strip()
        words = tuple(w.strip() for w in parts[2].split(",") if w.strip())
        if engine and trigger and words:
            key = (engine, trigger)
            out[key] = out.get(key, ()) + words
    return out


def match_zh_triggers(text: str,
                      extra: dict[tuple[str, str], tuple[str, ...]] | None = None
                      ) -> list[tuple[str, str]]:
    """扫一遍中文，返回命中的 `(引擎, trigger_type)` 列表（已去重，保序）。

    纯函数、不碰状态机 —— 命中了还得由 LifeBridge 决定要不要真喂下去。
    `extra` 是主人自定义的追加词表（E5），与内置表**合并**后一起匹配。
    """
    raw = str(text or "")
    if not raw.strip():
        return []
    lowered = raw.lower()
    merged: dict[tuple[str, str], tuple[str, ...]] = dict(ZH_TRIGGER_MAP)
    for key, words in (extra or {}).items():
        merged[key] = tuple(merged.get(key, ())) + tuple(words)
    hits: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for key, words in merged.items():
        for word in words:
            if word and word.lower() in lowered:
                if key not in seen:
                    seen.add(key)
                    hits.append(key)
                break          # 同一个信号命中一个词就够了
    return hits
