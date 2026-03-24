# CodeBuddy 自定义技能集合

> 骆哥（创投骆哥）的 CodeBuddy / WorkBuddy 自定义技能库

## 技能列表

### 🔍 [bp-analyzer](./skills/bp-analyzer/SKILL.md)

**BP 深度分析技能 v2.0**

用于深度穿透分析商业计划书（BP）、行业研报，辅助 PE 基金经理进行投资决策。

**核心特性：**
- 自动识别项目赛道（AI/大模型、半导体、机器人、商业航天、低空经济等 10 个赛道）
- 动态生成行业精准的尖锐问题，禁止套用其他行业模板
- 四模块输出：费曼通俗翻译 → 第一性原理评估 → 30+ 非共识问题 → Jensen Huang 式倾向性判断
- 自动生成 Markdown 报告并同步至 Obsidian

**输出标准：** 3000-5000 字深度分析报告

---

### 📓 [weekly-notes-import](./skills/weekly-notes-import/SKILL.md)

**每周笔记智能导入技能**

将 Get笔记 App 的周记自动导入 Obsidian，完成主题聚类、价值分级、洞察卡片生成全流程。

**核心特性：**
- AI 动态主题聚类（语义分析，不限于预定义赛道）
- S/A/B/C 四级价值分类（四维评分体系）
- 自动生成 Zettelkasten 洞察卡片并与已有卡片合并
- 创建本周索引 + 主题索引 + 双向链接

**脚本文件：** `scripts/smart_import_to_obsidian.py`

---

## 使用方法

将对应技能目录复制到 `~/.workbuddy/skills/` 下即可在 WorkBuddy 中使用。

例如：
```bash
cp -r skills/bp-analyzer ~/.workbuddy/skills/
cp -r skills/weekly-notes-import ~/.workbuddy/skills/
```
