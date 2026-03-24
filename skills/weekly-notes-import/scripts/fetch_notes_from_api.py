#!/usr/bin/env python3
"""
从Get笔记API获取指定周次的笔记数据

支持获取本周或指定周次的笔记，并保存为JSON文件
"""

import argparse
import json
import os
import requests
import time
from datetime import datetime, timedelta
from typing import Dict, List

# Get笔记API配置
BASE_URL = "https://openapi.biji.com"
API_KEY = os.environ.get("GETNOTE_API_KEY", "")
CLIENT_ID = os.environ.get("GETNOTE_CLIENT_ID", "")

def load_config():
    """从 ~/.openclaw/openclaw.json 加载配置"""
    config_path = os.path.expanduser("~/.openclaw/openclaw.json")
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
            return config.get('skills', {}).get('entries', {}).get('getnote', {})
    return {}

def get_week_range(week_offset: int = 0):
    """
    计算指定周次的时间范围

    Args:
        week_offset: 周偏移量
            0 = 本周（默认）
            1 = 上一周
            2 = 两周前
            -1 = 下一周（未来）

    Returns:
        (start_date, end_date): 时间范围字符串

    规则：
    - 默认(week_offset=0):
      - 周一到周五: 本周一到今天
      - 周六: 本周一到今天
      - 周日: 上周日到今天
    - 指定周次(week_offset=1, 2, ...):
      - 完整一周（周一到周日）
    """
    today = datetime.now()
    weekday = today.weekday()  # 0=周一, 6=周日

    if week_offset == 0:
        # 本周：使用动态计算逻辑
        if weekday == 6:  # 周日
            start_date = today - timedelta(days=7)  # 上周日
        else:
            start_date = today - timedelta(days=weekday)  # 本周一
        end_date = today
    else:
        # 指定周次：完整一周（周一到周日）
        # 先找到本周一的日期
        this_monday = today - timedelta(days=weekday)
        # 计算目标周的开始日期
        target_monday = this_monday - timedelta(days=week_offset * 7)
        # 目标周的周日
        target_sunday = target_monday + timedelta(days=6)

        start_date = target_monday
        end_date = target_sunday

    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")

def fetch_notes_list(api_key: str, client_id: str, since_id: int = 0) -> List[Dict]:
    """
    获取笔记列表（支持分页和重试）

    Args:
        api_key: API密钥
        client_id: 客户端ID
        since_id: 游标，首次传0

    Returns:
        笔记列表
    """
    url = f"{BASE_URL}/open/api/v1/resource/note/list"
    headers = {
        "Authorization": api_key,
        "X-Client-ID": client_id
    }
    params = {"since_id": since_id}

    all_notes = []
    has_more = True
    retry_count = 0
    max_retries = 3

    while has_more:
        response = requests.get(url, headers=headers, params=params)
        data = response.json()

        if data.get("success"):
            notes = data.get("data", {}).get("notes", [])
            all_notes.extend(notes)
            has_more = data.get("data", {}).get("has_more", False)
            params["since_id"] = data.get("data", {}).get("next_cursor", 0)

            print(f"📥 已获取 {len(all_notes)} 条笔记...", end='\r')
            retry_count = 0  # 成功后重置重试计数
        else:
            error = data.get('error', {})
            error_code = error.get('code')
            error_reason = error.get('reason')

            # 检查是否是频率限制错误
            if error_code == 10202 and error_reason == 'qps_bucket_exceeded':
                retry_count += 1
                if retry_count <= max_retries:
                    wait_time = 5 * retry_count  # 递增等待时间：5秒、10秒、15秒
                    print(f"\n⚠️  API频率限制，等待 {wait_time} 秒后重试 ({retry_count}/{max_retries})...")
                    time.sleep(wait_time)
                    continue
                else:
                    print(f"\n❌ 已达到最大重试次数({max_retries})，停止获取")
                    break
            else:
                print(f"\n❌ 获取笔记列表失败: {error}")
                break

    print(f"\n✅ 总共获取 {len(all_notes)} 条笔记")
    return all_notes

def fetch_note_details(api_key: str, client_id: str, note_id: int) -> Dict:
    """
    获取单条笔记的详情

    Args:
        api_key: API密钥
        client_id: 客户端ID
        note_id: 笔记ID

    Returns:
        笔记详情
    """
    url = f"{BASE_URL}/open/api/v1/resource/note/detail"
    headers = {
        "Authorization": api_key,
        "X-Client-ID": client_id
    }
    params = {"id": note_id}

    response = requests.get(url, headers=headers, params=params)
    data = response.json()

    if data.get("success"):
        return data.get("data", {}).get("note", {})
    else:
        print(f"❌ 获取笔记详情失败 (ID: {note_id}): {data.get('error', {})}")
        return None

def filter_notes_by_week(notes: List[Dict], start_date: str, end_date: str) -> List[Dict]:
    """
    根据周次范围筛选笔记

    Args:
        notes: 所有笔记列表
        start_date: 开始日期 (YYYY-MM-DD)
        end_date: 结束日期 (YYYY-MM-DD)

    Returns:
        符合时间范围的笔记列表
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)  # 包含结束日当天

    filtered = []
    for note in notes:
        created_at = note.get("created_at", "")
        if created_at:
            note_dt = datetime.strptime(created_at.split()[0], "%Y-%m-%d")
            if start_dt <= note_dt < end_dt:
                filtered.append(note)

    return filtered

def main():
    parser = argparse.ArgumentParser(description='从Get笔记API获取指定周次的笔记数据')
    parser.add_argument('--week', type=int, default=0,
                        help='周偏移量 (0=本周, 1=上一周, 2=两周前)')
    parser.add_argument('--output', type=str, default="notes_with_details.json",
                        help='输出JSON文件路径')

    args = parser.parse_args()

    # 加载配置
    getnote_config = load_config()
    api_key = getnote_config.get("apiKey", API_KEY)
    client_id = getnote_config.get("env", {}).get("GETNOTE_CLIENT_ID", CLIENT_ID)

    if not api_key or not client_id:
        print("❌ 未找到Get笔记API配置")
        print("请在 ~/.openclaw/openclaw.json 中配置 Get笔记的apiKey和client_id")
        return

    # 计算周次范围
    week_range = get_week_range(args.week)
    week_label = "本周" if args.week == 0 else f"{args.week}周前"
    print(f"\n{'='*60}")
    print(f"📅 获取{week_label}笔记: {week_range[0]} 至 {week_range[1]}")
    print(f"{'='*60}\n")

    # 步骤1：获取所有笔记列表
    print("步骤1: 获取笔记列表...")
    all_notes = fetch_notes_list(api_key, client_id, since_id=0)

    if not all_notes:
        print("❌ 没有获取到任何笔记")
        return

    # 步骤2：根据周次筛选笔记
    print(f"\n步骤2: 筛选符合时间范围的笔记...")
    filtered_notes = filter_notes_by_week(all_notes, week_range[0], week_range[1])
    print(f"✅ 从 {len(all_notes)} 条笔记中筛选出 {len(filtered_notes)} 条符合时间范围的笔记")

    if not filtered_notes:
        print(f"\n⚠️ {week_range[0]} 至 {week_range[1]} 期间没有笔记")
        print("可能原因：")
        print("  1. 这段时间你没有用Get笔记记录")
        print("  2. 笔记可能已被删除或移入回收站")
        return

    # 步骤3：获取每条笔记的详情
    print(f"\n步骤3: 获取笔记详情...")
    notes_with_details = []
    for i, note in enumerate(filtered_notes, 1):
        note_id = note.get("id")
        if note_id:
            print(f"  获取详情 [{i}/{len(filtered_notes)}] ID: {note_id}...", end='\r')
            detail = fetch_note_details(api_key, client_id, note_id)
            if detail:
                notes_with_details.append({"note": detail})
            
            # 避免API限流：每获取5条笔记后sleep 1秒
            if i % 5 == 0:
                time.sleep(1)

    print(f"\n✅ 成功获取 {len(notes_with_details)} 条笔记详情")

    # 步骤4：保存到JSON文件
    output_file = os.path.join("/Users/luoyingchuansha/WorkBuddy/20260320162931", args.output)
    print(f"\n步骤4: 保存到JSON文件: {output_file}")

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(notes_with_details, f, ensure_ascii=False, indent=2)

    print(f"✅ 完成！笔记数据已保存到: {output_file}")
    print(f"\n📊 统计信息:")
    print(f"  - 总笔记数: {len(notes_with_details)}")
    print(f"  - 时间范围: {week_range[0]} 至 {week_range[1]}")

if __name__ == '__main__':
    main()
