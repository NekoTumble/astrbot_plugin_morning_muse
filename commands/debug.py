import datetime


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
        p = self.plugin
        lines = []
        lines.append("📊 晨光心语 · 运行报告")
        lines.append("━━━━━━━━━━━━━━━━━━")

        # 基本设置
        lines.append(f"⏰ 定时生成：{p.get_config('schedule.schedule_time', '08:00')}")
        lines.append(f"🤖 生成模型：{p.get_config('schedule.schedule_model', '') or '(使用默认)'}")
        lines.append(f"🛟 备用兜底：{p.get_config('schedule.schedule_model_backup', '') or '(回落到对话默认)'}")

        # 今日节日（从 context_builder 的三层检测获取）
        try:
            today = p._get_today()  # 按插件配置的时区取“今天”
        except Exception:
            today = datetime.date.today()
        # N1：`_get_holiday_info` 现在只报节日，没有节日就是空串（工作日/休息日不再混进来）
        # F6：单独兜一层 —— 以前它裸奔，这里一抛错整个 /晨光报告 就断了
        try:
            holiday_info = p.context_builder._get_holiday_info(today)
        except Exception as e:
            holiday_info = f"(获取失败: {e})"
        lines.append(f"🎉 今日节日：{holiday_info or '(无节日)'}")

        # 静态人格配置 - 修复：使用嵌套路径读取
        lines.append("\n👤 已配置人格（静态）：")
        has_static = False
        for i in range(1, 4):
            name = str(p.get_config(f"persona_styles.persona_name_{i}", "") or "").strip()
            style = str(p.get_config(f"persona_styles.style_control_{i}", "") or "").strip()
            if name:
                has_static = True
                lines.append(f"  {i}️⃣ {name} → {style or '(无风格)'}")
        if not has_static:
            lines.append("  (未配置)")

        # 动态绑定
        if hasattr(p, "dynamic_styles") and p.dynamic_styles:
            lines.append("\n🔧 动态绑定人格：")
            for name, style in p.dynamic_styles.items():
                lines.append(f"  {name} → {style or '(无风格)'}")
        else:
            lines.append("\n🔧 动态绑定人格：(无)")

        # 已知人格列表
        try:
            all_ids = p.context_builder.get_known_persona_ids() or []
        except Exception as e:
            all_ids = []
            lines.append(f"\n📋 所有人格标识：(获取失败: {e})")
        else:
            lines.append(f"\n📋 所有人格标识：{', '.join(str(x) for x in all_ids)}")

        # 人格提示词加载情况
        lines.append("\n📚 人格提示词加载状态：")
        for pid in all_ids:
            try:
                prompt = await p.context_builder._get_persona_prompt(pid)
            except Exception as e:
                lines.append(f"  ❌ {pid} (获取失败: {e})")
                continue
            if prompt:
                lines.append(f"  ✅ {pid} (长度: {len(prompt)})")
            else:
                lines.append(f"  ⚠️ {pid} (未找到)")

        # 今日日程生成状态
        lines.append(f"\n📅 今日日程状态 ({today.isoformat()})：")
        for pid in all_ids:
            try:
                schedule = await p.storage.load_schedule(pid, today)
            except Exception as e:
                lines.append(f"  ❌ {pid}：读取失败 ({e})")
                continue
            if schedule:
                lines.append(f"  ✅ {pid}：已生成")
            else:
                lines.append(f"  ❌ {pid}：未生成")

        # 近期日志
        logs = p.debug_logs if hasattr(p, "debug_logs") else []
        if logs:
            lines.append("\n💬 近期生成日志（最近5条）：")
            for entry in logs[-5:]:
                lines.append(f"  [{entry.get('time', '')}] {str(entry.get('level', '')).upper()}: {entry.get('msg', '')}")
        else:
            lines.append("\n💬 近期生成日志：(暂无)")

        return "\n".join(lines)
