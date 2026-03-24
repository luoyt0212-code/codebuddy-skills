#!/usr/bin/env python3
"""
summarize 整合模块 - 为链接类笔记提供内容预处理

功能：
1. 识别链接类型笔记
2. 调用 summarize CLI 获取内容摘要
3. 将摘要整合到笔记内容中，提升 AI 评估准确性

依赖：summarize CLI (brew install steipete/tap/summarize)
环境变量：GEMINI_API_KEY 或 OPENAI_API_KEY
"""

import subprocess
import re
import os
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse


def run_summarize(url: str, length: str = "medium") -> Tuple[bool, str]:
    """
    调用 summarize CLI 获取 URL 内容摘要
    
    Args:
        url: 目标 URL
        length: 摘要长度 (short/medium/long/xl/xxl)
        
    Returns:
        (成功标志, 摘要内容或错误信息)
    """
    try:
        cmd = [
            "summarize", url,
            "--length", length,
            "--model", "google/gemini-3-flash-preview"
        ]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60  # summarize 可能需要时间拉取页面
        )
        
        if result.returncode == 0:
            return True, result.stdout.strip()
        else:
            error_msg = result.stderr.strip() if result.stderr else "未知错误"
            return False, f"summarize 失败: {error_msg}"
            
    except subprocess.TimeoutExpired:
        return False, "summarize 超时（页面加载太慢）"
    except FileNotFoundError:
        return False, "summarize 未安装，请先运行: brew install steipete/tap/summarize"
    except Exception as e:
        return False, f"summarize 执行错误: {str(e)}"


def extract_url_from_note(note: Dict) -> Optional[str]:
    """
    从笔记内容中提取 URL
    
    Args:
        note: 笔记数据
        
    Returns:
        提取到的 URL，无则返回 None
    """
    content = note.get("content", "")
    
    # 匹配 http/https URL
    url_pattern = r'https?://[^\s<>"\')\]]+'
    urls = re.findall(url_pattern, content)
    
    if urls:
        # 返回第一个找到的 URL
        return urls[0]
    
    return None


def is_link_note(note: Dict) -> bool:
    """
    判断是否为链接类型笔记
    
    Args:
        note: 笔记数据
        
    Returns:
        是否是链接笔记
    """
    # 检查 note_type
    note_type = note.get("note_type", "")
    if note_type == "link":
        return True
    
    # 检查内容是否以 URL 为主
    content = note.get("content", "")
    url_pattern = r'https?://[^\s<>"\')\]]+'
    urls = re.findall(url_pattern, content)
    
    # 如果内容主要是 URL（URL 占内容长度 50% 以上）
    if urls:
        total_url_length = sum(len(url) for url in urls)
        if total_url_length / len(content) > 0.5:
            return True
    
    return False


def preprocess_link_note(note: Dict, force_refresh: bool = False) -> Dict:
    """
    预处理链接笔记，获取内容摘要
    
    Args:
        note: 原始笔记数据
        force_refresh: 是否强制重新获取（忽略已有摘要）
        
    Returns:
        处理后的笔记数据（添加了摘要）
    """
    # 检查是否已处理过
    content = note.get("content", "")
    if "[summarized]" in content and not force_refresh:
        return note
    
    # 提取 URL
    url = extract_url_from_note(note)
    if not url:
        return note
    
    print(f"  🔗 发现链接笔记: {note.get('title', '无标题')[:40]}...")
    print(f"     URL: {url[:60]}...")
    
    # 获取摘要
    success, summary = run_summarize(url, length="medium")
    
    if success:
        # 将摘要追加到笔记内容
        summary_section = f"\n\n---\n📄 **内容摘要** (via summarize)\n\n{summary}\n\n[summarized]"
        note["content"] = content + summary_section
        
        # 标记已处理
        note["_summarized"] = True
        note["_summary_length"] = len(summary)
        
        print(f"     ✅ 摘要已添加 ({len(summary)} 字符)")
    else:
        # 记录失败
        note["_summarized"] = False
        note["_summary_error"] = summary
        print(f"     ⚠️  摘要获取失败: {summary}")
    
    return note


def batch_preprocess_links(notes: list, skip_on_error: bool = True) -> list:
    """
    批量预处理链接笔记
    
    Args:
        notes: 笔记列表
        skip_on_error: 出错时是否跳过（继续处理其他）
        
    Returns:
        处理后的笔记列表
    """
    processed = []
    link_count = 0
    success_count = 0
    
    for note in notes:
        if is_link_note(note):
            link_count += 1
            try:
                processed_note = preprocess_link_note(note)
                if processed_note.get("_summarized"):
                    success_count += 1
                processed.append(processed_note)
            except Exception as e:
                print(f"  ⚠️  处理链接笔记失败: {e}")
                if skip_on_error:
                    processed.append(note)
                else:
                    raise
        else:
            processed.append(note)
    
    if link_count > 0:
        print(f"\n📊 链接预处理完成: {success_count}/{link_count} 成功")
    
    return processed


def get_url_domain(url: str) -> str:
    """
    获取 URL 的域名
    
    Args:
        url: URL 字符串
        
    Returns:
        域名
    """
    try:
        parsed = urlparse(url)
        return parsed.netloc.replace("www.", "")
    except:
        return "unknown"


def estimate_content_quality(url: str) -> str:
    """
    根据 URL 来源估计内容质量
    
    Args:
        url: URL 字符串
        
    Returns:
        质量等级 (high/medium/low)
    """
    domain = get_url_domain(url)
    
    # 高质量来源
    high_quality_sources = [
        "xiaoyuzhoufm.com",  # 小宇宙
        "mp.weixin.qq.com",  # 微信公众号
        "igetget.com",       # 得到
        "zhihu.com",         # 知乎
        "sspai.com",         # 少数派
        "36kr.com",          # 36氪
        "pingwest.com",      # PingWest
        "techcrunch.com",
        "theinformation.com",
    ]
    
    # 中质量来源
    medium_quality_sources = [
        "bilibili.com",
        "youtube.com",
        "douyin.com",
    ]
    
    if any(src in domain for src in high_quality_sources):
        return "high"
    elif any(src in domain for src in medium_quality_sources):
        return "medium"
    else:
        return "low"


# 向后兼容接口
def enrich_note_content(note: Dict) -> Dict:
    """
    丰富笔记内容（如果笔记是链接类型，获取摘要）
    
    这是给主脚本调用的简化接口
    """
    if is_link_note(note):
        return preprocess_link_note(note)
    return note


if __name__ == "__main__":
    # 测试代码
    print("测试 summarize 整合模块")
    
    # 测试链接识别
    test_notes = [
        {
            "title": "测试文章",
            "content": "https://www.xiaoyuzhoufm.com/episode/123456",
            "note_type": "link"
        },
        {
            "title": "普通笔记",
            "content": "这是普通笔记内容，没有链接",
            "note_type": "text"
        }
    ]
    
    for note in test_notes:
        print(f"\n笔记: {note['title']}")
        print(f"  是链接笔记: {is_link_note(note)}")
        if is_link_note(note):
            enriched = enrich_note_content(note)
            print(f"  处理后内容长度: {len(enriched['content'])}")
