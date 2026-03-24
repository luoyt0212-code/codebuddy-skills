#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

# Obsidian路径
OBSIDIAN_VAULT = "/Users/luoyingchuansha/Library/Mobile Documents/iCloud~md~obsidian/Documents/创投骆哥"
INBOX_DIR = os.path.join(OBSIDIAN_VAULT, "00 📒 Inbox（From_Get笔记）")

# 读取本周所有笔记(3月16日-3月20日)
with open('last_week_notes.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

# 筛选本周一到周五的笔记
notes = data['notes']
week_notes = []

for note in notes:
    created_at = datetime.strptime(note['created_at'], "%Y-%m-%d %H:%M:%S")
    # 本周一(3月16日)到周五(3月20日)
    start_date = datetime(2026, 3, 16)
    end_date = datetime(2026, 3, 20, 23, 59, 59)
    
    if start_date <= created_at <= end_date:
        week_notes.append(note)

print(f"找到本周一到周五(3月16日-3月20日)的笔记: {len(week_notes)}条")

# 按类型统计
type_count = {}
for note in week_notes:
    note_type = note['note_type']
    type_count[note_type] = type_count.get(note_type, 0) + 1

print("\n笔记类型分布:")
for note_type, count in sorted(type_count.items()):
    print(f"  {note_type}: {count}条")

# 输出笔记列表
print(f"\n本周笔记列表:")
for i, note in enumerate(week_notes, 1):
    created_date = note['created_at'].split()[0]
    note_type = note['note_type']
    title = note['title'][:50] + "..." if len(note['title']) > 50 else note['title']
    print(f"{i}. [{created_date}] [{note_type}] {title}")

# 保存到文件
with open('this_week_all_notes.json', 'w', encoding='utf-8') as f:
    json.dump(week_notes, f, ensure_ascii=False, indent=2)

print(f"\n已保存到: this_week_all_notes.json")
