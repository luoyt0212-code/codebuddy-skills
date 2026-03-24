#!/usr/bin/env python3
"""
qmd 整合模块 - 为 weekly-notes-import 提供本地笔记搜索能力

功能：
1. 卡片去重 - 查找是否已有相似主题的卡片
2. 关联推荐 - 为笔记/卡片推荐相关内容
3. 索引维护 - 确保 qmd 索引及时更新

依赖：qmd CLI (bun install -g https://github.com/tobi/qmd)
"""

import subprocess
import json
import os
from typing import List, Dict, Optional, Tuple
from pathlib import Path

# Obsidian 知识库路径
VAULT_PATH = "/Users/luoyingchuansha/Library/Mobile Documents/iCloud~md~obsidian/Documents/创投骆哥"
CARDS_DIR = os.path.join(VAULT_PATH, "06 🧠 Zettelkasten (卡片盒 - 核心洞察)")
INBOX_DIR = os.path.join(VAULT_PATH, "00 📒 Inbox（From_Get笔记）")

# qmd collection 名称
QMD_COLLECTION = "创投骆哥"

# 相似度阈值
SIMILARITY_THRESHOLD = 0.6  # BM25 分数阈值，高于此值认为是相似内容


def run_qmd_command(args: List[str]) -> Tuple[bool, str]:
    """
    执行 qmd 命令
    
    Args:
        args: qmd 命令参数列表
        
    Returns:
        (成功标志, 输出内容)
    """
    try:
        cmd = ["qmd"] + args
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30  # qmd search 通常很快
        )
        if result.returncode == 0:
            return True, result.stdout
        else:
            return False, result.stderr
    except subprocess.TimeoutExpired:
        return False, "qmd 命令超时"
    except FileNotFoundError:
        return False, "qmd 未安装，请先运行: bun install -g https://github.com/tobi/qmd"
    except Exception as e:
        return False, f"qmd 执行错误: {str(e)}"


def ensure_collection_exists() -> bool:
    """
    确保 qmd collection 已创建
    
    Returns:
        是否成功
    """
    # 检查 collection 是否存在
    success, output = run_qmd_command(["status"])
    if not success:
        # 尝试创建 collection
        success, output = run_qmd_command([
            "collection", "add", VAULT_PATH,
            "--name", QMD_COLLECTION,
            "--mask", "**/*.md"
        ])
        if success:
            print(f"  ✅ 创建 qmd collection: {QMD_COLLECTION}")
            # 初始化 embedding（用于语义搜索）
            run_qmd_command(["embed"])
        else:
            print(f"  ⚠️  创建 qmd collection 失败: {output}")
            return False
    return True


def update_index() -> bool:
    """
    更新 qmd 索引（在导入新笔记后调用）
    
    Returns:
        是否成功
    """
    success, output = run_qmd_command(["update"])
    if success:
        print("  ✅ qmd 索引已更新")
        return True
    else:
        print(f"  ⚠️  qmd 索引更新失败: {output}")
        return False


def search_similar_cards(query: str, top_k: int = 5) -> List[Dict]:
    """
    搜索相似的知识卡片
    
    Args:
        query: 搜索查询（笔记标题+核心内容）
        top_k: 返回结果数量
        
    Returns:
        相似卡片列表，每个包含 file, score, snippet
    """
    # 使用 BM25 关键词搜索（最快）
    success, output = run_qmd_command([
        "search", query,
        "-c", QMD_COLLECTION,
        "-n", str(top_k),
        "--json"
    ])
    
    if not success:
        print(f"  ⚠️  qmd 搜索失败: {output}")
        return []
    
    try:
        results = json.loads(output)
        # 过滤出卡片目录下的结果
        cards_results = [
            r for r in results 
            if "06 🧠 Zettelkasten" in r.get("file", "")
        ]
        return cards_results
    except json.JSONDecodeError:
        print(f"  ⚠️  qmd 返回解析失败")
        return []


def find_matching_card(note: Dict, min_score: float = SIMILARITY_THRESHOLD) -> Optional[str]:
    """
    查找应该追加的现有卡片（替代原有关键词映射表方案）
    
    Args:
        note: 笔记数据，包含 title, content, _topic
        min_score: 最小相似度阈值
        
    Returns:
        匹配的卡片文件名（相对路径），无匹配返回 None
    """
    title = note.get("title", "")
    content = note.get("content", "")
    topic = note.get("_topic", "")
    
    # 构建搜索查询：主题 + 标题 + 内容前 200 字
    query = f"{topic} {title} {content[:200]}"
    
    print(f"  🔍 qmd 搜索相似卡片: {title[:40]}...")
    
    # 搜索相似卡片
    results = search_similar_cards(query, top_k=3)
    
    if not results:
        print(f"  ⚠️  未找到相似卡片，将新建")
        return None
    
    # 检查最高分的匹配
    best_match = results[0]
    score = best_match.get("score", 0)
    file_path = best_match.get("file", "")
    
    # 提取文件名
    if score >= min_score:
        # 从完整路径中提取文件名
        card_name = os.path.basename(file_path)
        print(f"  🎯 匹配到卡片: {card_name} (相似度: {score:.2f})")
        return card_name
    else:
        print(f"  ⚠️  最佳匹配相似度 {score:.2f} < 阈值 {min_score}，将新建")
        return None


def find_related_notes(note: Dict, top_k: int = 5) -> List[Dict]:
    """
    查找与笔记相关的其他笔记（用于双向链接推荐）
    
    Args:
        note: 笔记数据
        top_k: 返回结果数量
        
    Returns:
        相关笔记列表
    """
    title = note.get("title", "")
    content = note.get("content", "")
    
    # 构建查询
    query = f"{title} {content[:300]}"
    
    success, output = run_qmd_command([
        "search", query,
        "-c", QMD_COLLECTION,
        "-n", str(top_k + 5),  # 多取几个，过滤后返回
        "--json"
    ])
    
    if not success:
        return []
    
    try:
        results = json.loads(output)
        # 排除当前笔记本身
        note_path = note.get("_file_path", "")
        filtered = [
            r for r in results 
            if note_path not in r.get("file", "")
        ]
        return filtered[:top_k]
    except json.JSONDecodeError:
        return []


def get_card_content(card_filename: str) -> Optional[str]:
    """
    获取卡片完整内容
    
    Args:
        card_filename: 卡片文件名（不含路径）
        
    Returns:
        卡片内容，不存在返回 None
    """
    card_path = os.path.join(CARDS_DIR, card_filename)
    if not os.path.exists(card_path):
        return None
    
    try:
        with open(card_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"  ⚠️  读取卡片失败: {e}")
        return None


def semantic_search_cards(query: str, top_k: int = 3) -> List[Dict]:
    """
    语义搜索（用于更精准的关联发现）
    
    注意：vsearch 较慢，仅在必要时使用
    
    Args:
        query: 搜索查询
        top_k: 返回结果数量
        
    Returns:
        语义相似卡片列表
    """
    print(f"  🔍 qmd 语义搜索（较慢）...")
    
    success, output = run_qmd_command([
        "vsearch", query,
        "-c", QMD_COLLECTION,
        "-n", str(top_k),
        "--json"
    ])
    
    if not success:
        print(f"  ⚠️  语义搜索失败: {output}")
        return []
    
    try:
        results = json.loads(output)
        return [
            r for r in results 
            if "06 🧠 Zettelkasten" in r.get("file", "")
        ]
    except json.JSONDecodeError:
        return []


# 向后兼容：提供与原脚本相同的接口
def find_target_card(note: Dict) -> Optional[str]:
    """
    与原脚本兼容的接口
    
    优先使用 qmd 搜索，失败则返回 None（让调用方新建）
    """
    # 确保 collection 存在
    if not ensure_collection_exists():
        return None
    
    return find_matching_card(note)


if __name__ == "__main__":
    # 测试代码
    print("测试 qmd 整合模块")
    
    # 测试索引更新
    update_index()
    
    # 测试搜索
    test_note = {
        "title": "商业航天发射成本分析",
        "content": "火箭回收技术将发射成本降低了 80%，这是商业航天的关键转折点...",
        "_topic": "商业航天"
    }
    
    result = find_matching_card(test_note)
    print(f"匹配结果: {result}")
