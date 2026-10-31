"""J1 · 本地现实地址参考（带开关、默认关）。

一句话：让她排活动时知道「自己现实里在哪座城市、那边现在什么季节」，排出来贴合现实；
**但绝不把这些写进日程正文，也不写进任何日志。**

边界（照 NAS 清单 §J 写的）：

- 默认关。关着的时候**整条链路一行都不走**，生成结果与开功能前逐字节一致。
- 只描述能公开的环境层次：城市 + 季节气候感 + 可去的场所类型 + 距离感。
- 不许出现：具体街道、门牌、楼栋、小区名、精确坐标。
- 不调模型、不调任何外部接口，只往现有那次生成里多塞几十个字。
- 日志只记「开/关」「已配置/未配置」，**原文不进日志**。

三个配置项（就三个）：
    enable_local_geo   bool    总开关，默认 false
    local_geo_area     str     城市（必填）+ 区/片区（可选）+ 附近地标（可选）
                               留空则回退天气配置里的城市名
    local_geo_hint     str     可选，一句话说她和这片地方的关系
"""

import datetime

from astrbot.api import logger

# 占位符里那份画像的「标记句」：用来判断模板里是不是已经有这一段，避免补两次
MARK = "她现实里在"
BLOCK_TAG = "当前提示词模板里没有 {local_geo}"

# 月份 → 季节说法（北半球、中文习惯；不追求节气精度，够当氛围底色就行）
_MONTH_SEASON = {
    1: "隆冬", 2: "冬末", 3: "初春", 4: "仲春", 5: "暮春", 6: "初夏",
    7: "盛夏", 8: "夏末", 9: "初秋", 10: "深秋", 11: "深秋末", 12: "初冬",
}


def _season_of(month: int) -> str:
    return _MONTH_SEASON.get(int(month), "")


def build_text(plugin) -> str:
    """渲染那段「环境画像」。开关关着、或地址空着 → 返回空字符串（调用方什么都别做）。"""
    try:
        if not plugin.get_config("enable_local_geo", False):
            return ""
    except Exception:
        return ""

    area = str(plugin.get_config("local_geo_area", "") or "").strip()
    if not area:
        # 留空 → 回退天气配置里的城市名（现在那个字段填的是英文城市名，够用）
        area = str(plugin.get_config("weather.weather_location", "") or "").strip()
    if not area:
        logger.info("[晨光心语] 📍 本地环境参考开着，但没有城市（local_geo_area 与天气城市都空），本次不注入")
        return ""

    hint = str(plugin.get_config("local_geo_hint", "") or "").strip()

    try:
        today = plugin._get_today()
    except Exception:
        today = datetime.date.today()
    season = _season_of(today.month)

    lines = [
        "## 📍 她所在的地方（只作氛围参考，不是指令）",
        f"- {MARK}{area}。",
        f"- 现在是 {today.month} 月，{season}。那一带的气候按常理推断就好，别编出反季节的天气。",
        "- 附近能去的地方就写在上面那句里；距离感大概是步行十几分钟到半小时，要去远一点的得专门跑一趟。",
    ]
    if hint:
        lines.append(f"- 她跟这片地方的关系：{hint}")
    lines.append(
        "- **红线**：这些只当氛围底色。日程正文（activity）、location、beats 的 at、note 里"
        "**都不许**出现真实街道、门牌、楼栋、小区名或精确位置；"
        "location 仍然用那张英文地点表，表外的地方只能用泛化说法（「城东那家旧书店」这种）。"
    )
    return "\n".join(lines) + "\n"


def ensure_block(prompt: str, text: str) -> str:
    """模板里没有 {local_geo} 占位符时，把画像补进提示词。

    补的位置尽量靠前（「## 开始生成」之前）；没有这个标题就追加到末尾。
    已经有这段就一个字都不加。返回新 prompt。
    """
    if not text:
        return prompt
    if MARK in prompt:
        return prompt
    block = "\n" + text
    if "## 开始生成" in prompt:
        return prompt.replace("## 开始生成", block + "\n## 开始生成", 1)
    return prompt.rstrip() + "\n" + block
