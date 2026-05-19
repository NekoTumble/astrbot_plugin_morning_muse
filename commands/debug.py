import datetime

class DebugCommands:
    def __init__(self, plugin):
        self.plugin = plugin

    async def cmd_show_log(self, message, event):
        logs = self.plugin.debug_logs if hasattr(self.plugin, 'debug_logs') else []
        if not logs:
            return "暂无生成日志。"
        recent = logs[-10:]
        output = "📜 最近10条生成日志：\n"
        for entry in recent:
            output += f"[{entry['time']}] {entry['level'].upper()}: {entry['msg']}\n"
        return output.strip()

    async def cmd_report(self, message, event):
        p = self.plugin
        lines = []
        lines.append("📊 晨光心语 · 运行报告")
        lines.append("━━━━━━━━━━━━━━━━━━")

        # 基本设置
        lines.append(f"⏰ 定时生成：{p.get_config('schedule.schedule_time', '08:00')}")
        lines.append(f"🤖 生成模型：{p.get_config('schedule.schedule_model', '') or '(使用默认)'}")

        # 今日节日（从 context_builder 的三层检测获取）
        today = datetime.date.today()
        holiday_info = p.context_builder._get_holiday_info(today)
        lines.append(f"🎉 今日节日：{holiday_info}")

        # 静态人格配置 - 修复：使用嵌套路径读取
        lines.append("\n👤 已配置人格（静态）：")
        has_static = False
        for i in range(1, 4):
            name = p.get_config(f"persona_styles.persona_name_{i}", "").strip()
            style = p.get_config(f"persona_styles.style_control_{i}", "").strip()
            if name:
                has_static = True
                lines.append(f"  {i}️⃣ {name} → {style or '(无风格)'}")
        if not has_static:
            lines.append("  (未配置)")

        # 动态绑定
        if hasattr(p, 'dynamic_styles') and p.dynamic_styles:
            lines.append("\n🔧 动态绑定人格：")
            for name, style in p.dynamic_styles.items():
                lines.append(f"  {name} → {style or '(无风格)'}")
        else:
            lines.append("\n🔧 动态绑定人格：(无)")

        # 已知人格列表
        all_ids = p.context_builder.get_known_persona_ids()
        lines.append(f"\n📋 所有人格标识：{', '.join(all_ids)}")

        # 人格提示词加载情况
        lines.append("\n📚 人格提示词加载状态：")
        for pid in all_ids:
            prompt = await p.context_builder._get_persona_prompt(pid)
            if prompt:
                lines.append(f"  ✅ {pid} (长度: {len(prompt)})")
            else:
                lines.append(f"  ⚠️ {pid} (未找到)")

        # 今日日程生成状态
        today = datetime.date.today()
        lines.append(f"\n📅 今日日程状态 ({today.isoformat()})：")
        for pid in all_ids:
            schedule = await p.storage.load_schedule(pid, today)
            if schedule:
                lines.append(f"  ✅ {pid}：已生成")
            else:
                lines.append(f"  ❌ {pid}：未生成")

        # 近期日志
        logs = p.debug_logs if hasattr(p, 'debug_logs') else []
        if logs:
            lines.append("\n💬 近期生成日志（最近5条）：")
            for entry in logs[-5:]:
                lines.append(f"  [{entry['time']}] {entry['level'].upper()}: {entry['msg']}")
        else:
            lines.append("\n💬 近期生成日志：(暂无)")

        return "\n".join(lines)