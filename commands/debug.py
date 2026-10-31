from ..core.view_data import collect_report, render_report_text


class DebugCommands:
    def __init__(self, plugin):
        self.plugin = plugin

    async def cmd_show_log(self, message, event):
        logs = self.plugin.debug_logs if hasattr(self.plugin, "debug_logs") else []
        if not logs:
            return "暂无生成日志。"
        recent = logs[-10:]
        output = "📜 最近10条生成日志：\n"
        for entry in recent:
            output += f"[{entry.get('time', '')}] {str(entry.get('level', '')).upper()}: {entry.get('msg', '')}\n"
        return output.strip()

    async def cmd_report(self, message, event):
        """运行报告。

        v1.5.29 起「取数」挪进 core/view_data.py 了 —— 插件页面那块「插件配置」
        读的是同一份 collect_report()。这里只管把它排成给人看的文字，
        输出内容与 v1.5.28 保持一致。
        """
        report = await collect_report(self.plugin)
        return render_report_text(report)
