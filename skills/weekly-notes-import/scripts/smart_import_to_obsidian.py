#!/usr/bin/env python3
"""
智能导入流程 v5.0 - 从Get笔记到Obsidian的知识加工SOP

从"内容搬运"到"知识加工"的七个步骤：
0. 链接预处理 (NEW) - summarize 拉取链接全文，提升评估质量
1. 主题聚类 - AI动态识别笔记主题
2. 价值筛选 - AI四维度评分(S/A/B/C)
3. 卡片去重 (NEW) - qmd搜索相似卡片，智能追加或新建
4. 主题索引 - 为每个主题创建索引页
5. 本周索引 - 生成本周总览页
6. 双向链接 (NEW) - qmd推荐相关笔记，建立知识网络

使用方法：
- 默认导入本周: python3 smart_import_to_obsidian.py
- 导入上一周: python3 smart_import_to_obsidian.py --week 1
- 导入两周前: python3 smart_import_to_obsidian.py --week 2
- 跳过链接预处理: python3 smart_import_to_obsidian.py --skip-summarize
- 跳过qmd去重: python3 smart_import_to_obsidian.py --skip-qmd
"""

import argparse
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

# 工作目录（当前脚本所在目录）
WORK_DIR = os.path.dirname(os.path.abspath(__file__))

# 导入整合模块
try:
    from summarize_integration import batch_preprocess_links, is_link_note
    SUMMARIZE_AVAILABLE = True
except ImportError:
    SUMMARIZE_AVAILABLE = False
    print("⚠️  summarize_integration 模块未找到，链接预处理功能不可用")

try:
    from qmd_integration import (
        find_matching_card, 
        update_index as qmd_update_index,
        find_related_notes,
        ensure_collection_exists
    )
    QMD_AVAILABLE = True
except ImportError:
    QMD_AVAILABLE = False
    print("⚠️  qmd_integration 模块未找到，卡片去重功能不可用")

# 详细日志记录（用于输出所有文件变更）
CHANGE_LOG = {
    'imported_notes': [],      # 导入的笔记
    'created_cards': [],       # 新创建的洞察卡片
    'updated_cards': [],       # 追加内容的洞察卡片
    'created_files': [],       # 创建的索引文件
    'updated_files': [],       # 更新的笔记文件（添加链接）
}

def reset_change_log():
    """重置变更日志"""
    global CHANGE_LOG
    CHANGE_LOG = {
        'imported_notes': [],
        'created_cards': [],
        'updated_cards': [],
        'created_files': [],
        'updated_files': [],
    }


def clean_wikilink(path: str) -> str:
    """
    清理 Obsidian wikilink 路径，确保格式正确
    
    处理以下问题：
    1. 确保有 .md 后缀（Obsidian 需要）
    2. 去掉多余的 [[ 和 ]]
    3. 确保以 [[ 开头，以 ]] 结尾
    
    示例：
    - [[00 📒 Inbox/xxx.md]] → [[00 📒 Inbox/xxx.md]]
    - 00 📒 Inbox/xxx]] → [[00 📒 Inbox/xxx.md]]
    - [[00 📒 Inbox/xxx]] → [[00 📒 Inbox/xxx.md]]
    """
    if not path:
        return path
    
    # 去掉多余的 [[
    while path.startswith('[['):
        path = path[2:]
    
    # 去掉多余的 ]]
    while path.endswith(']]'):
        path = path[:-2]
    
    # 确保有 .md 后缀
    if not path.endswith('.md'):
        path = path + '.md'
    
    # 确保有 [[ 和 ]]
    return f"[[{path}]]"


def extract_json_from_response(response_text: str) -> dict:
    """
    从 AI 响应中提取 JSON，具备强大的容错能力

    支持以下场景：
    1. 纯 JSON：{"key": "value"}
    2. JSON 在代码块中：
       ```json
       {"key": "value"}
       ```
    3. JSON 在 markdown 中，前后有其他文本
    4. JSON 被注释包裹
    5. JSON 被截断时，尝试修复不完整的字符串
    6. 多个 JSON 对象时，提取第一个

    Args:
        response_text: AI 返回的原始文本

    Returns:
        解析后的 JSON 字典，如果失败返回空字典
    """
    import json
    import re

    if not response_text or not response_text.strip():
        return {}

    response_text = response_text.strip()

    # 尝试 1: 直接解析整个文本
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        pass

    # 尝试 2: 提取 markdown 代码块中的 JSON
    # 支持 ```json 和 ``` 两种格式
    code_block_pattern = r'```(?:json)?\s*\n?([\s\S]*?)\n?```'
    code_blocks = re.findall(code_block_pattern, response_text, re.IGNORECASE)

    for block in code_blocks:
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue

    # 尝试 3: 查找第一个 { 和最后一个 }，提取中间内容
    start_idx = response_text.find('{')
    end_idx = response_text.rfind('}')

    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        json_str = response_text[start_idx:end_idx + 1]

        # 尝试修复被截断的字符串值
        # 如果最后一个双引号没有闭合，补上
        if json_str.count('"') % 2 != 0:
            # 找到最后一个未闭合的双引号
            last_unclosed_quote = json_str.rfind('"')
            # 检查这个引号后面是否还有引号（如果没有，说明未闭合）
            if last_unclosed_quote != -1 and '"' not in json_str[last_unclosed_quote + 1:]:
                # 尝试找到这个字段的值开始位置
                # 向前查找冒号
                colon_pos = json_str.rfind(':', 0, last_unclosed_quote)
                if colon_pos != -1:
                    # 截取到冒号后的内容
                    field_value = json_str[colon_pos + 1:].strip()
                    # 如果以双引号开头但没闭合，补上引号和可能的逗号/右括号
                    if field_value.startswith('"') and not field_value.endswith('"'):
                        # 修复未闭合的字符串
                        fixed_json_str = json_str + '"}'
                        try:
                            return json.loads(fixed_json_str)
                        except json.JSONDecodeError:
                            pass

        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass

    # 尝试 4: 使用正则表达式查找 JSON 对象（处理多行）
    json_pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
    matches = re.findall(json_pattern, response_text, re.DOTALL)

    for match in matches:
        try:
            return json.loads(match)
        except json.JSONDecodeError:
            continue

    # 尝试 5: 尝试解析部分 JSON（如果被截断，只提取完整的键值对）
    # 这是一个更激进的尝试，用于处理响应被截断的情况
    try:
        # 先尝试提取数组（因为数组可能包含多个元素，正则匹配会更复杂）
        # 提取 "topics": ["...", "..."] 这样的模式
        array_pattern = r'"([^"]+)"\s*:\s*(\[[^\]]*\])'
        array_matches = re.findall(array_pattern, response_text, re.DOTALL)

        # 提取简单的键值对（字符串、数字、布尔值）
        simple_kv_pattern = r'"([^"]+)"\s*:\s*(?:"([^"]*)"|([0-9.]+)|(true|false|null))'
        simple_matches = re.findall(simple_kv_pattern, response_text, re.DOTALL)

        result = {}

        # 处理数组字段
        for key, array_val in array_matches:
            try:
                result[key] = json.loads(array_val)
            except:
                # 如果解析失败，尝试提取数组中的字符串元素
                array_items = re.findall(r'"([^"]*)"', array_val)
                if array_items:
                    result[key] = array_items

        # 处理简单字段
        for key, str_val, num_val, bool_val in simple_matches:
            # 跳过已经处理的字段
            if key in result:
                continue

            if str_val is not None:
                result[key] = str_val
            elif num_val is not None:
                result[key] = float(num_val) if '.' in num_val else int(num_val)
            elif bool_val is not None:
                if bool_val == 'true':
                    result[key] = True
                elif bool_val == 'false':
                    result[key] = False
                else:
                    result[key] = None

        if result:  # 如果提取到至少一个键值对，返回部分结果
            return result
    except Exception:
        pass

    # 尝试 6: 移除注释后解析
    lines = response_text.split('\n')
    cleaned_lines = []
    for line in lines:
        # 移除 // 和 /* ... */ 风格的注释
        line = re.sub(r'//.*$', '', line)
        line = re.sub(r'/\*.*?\*/', '', line, flags=re.DOTALL)
        cleaned_lines.append(line)

    cleaned_text = '\n'.join(cleaned_lines)
    try:
        return json.loads(cleaned_text)
    except json.JSONDecodeError:
        pass

    # 所有尝试都失败，返回空字典
    print(f"      ⚠️ 无法从响应中提取有效 JSON")
    print(f"      📄 响应内容（前200字符）: {response_text[:200]}")
    return {}

def save_week_notes_to_json(notes: List[Dict], week_range: Tuple[str, str]):
    """
    保存本周笔记到JSON文件（供洞见雷达使用）

    Args:
        notes: 本周笔记列表
        week_range: 本周时间范围 (start_date, end_date)
    """
    json_path = f"{WORK_DIR}/this_week_all_notes.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({
            'week_range': week_range,
            'notes': notes,
            'total': len(notes)
        }, f, ensure_ascii=False, indent=2)
    print(f"  💾 保存本周笔记数据: {json_path}")

# 配置路径
GET_NOTES_API_URL = "https://openapi.biji.com/open/api/v1/"
AUTH_TOKEN = "gk_live_99631248162a50ee.0b4f45275c1d148098ab5d8b927ce2ea71f613cc553dc62c"
CLIENT_ID = "cli_6246e0e0f571f91309d3c383"

OBSIDIAN_VAULT = "/Users/luoyingchuansha/Library/Mobile Documents/iCloud~md~obsidian/Documents/创投骆哥"
INBOX_DIR = os.path.join(OBSIDIAN_VAULT, "00 📒 Inbox（From_Get笔记）")
INBOX_DIR_RELATIVE = "00 📒 Inbox（From_Get笔记）"  # 用于Obsidian内部链接的相对路径
ZETTELKASTEN_DIR = os.path.join(OBSIDIAN_VAULT, "06 🧠 Zettelkasten (卡片盒 - 核心洞察)")
TOPIC_INDEX_DIR = os.path.join(OBSIDIAN_VAULT, "02 🗂️ 主题索引", "主题")

# 主题关键词映射（8大赛道 + 4个辅助分类）
TOPIC_MAPPING = {
    # 核心投资赛道（8个）
    '低空经济': ['低空', '无人机', 'eVTOL', '飞行汽车', '空域管理', 'UAM', '空中出租车', '垂直起降', '飞行器'],
    '机器人': ['机器人', '人形', '具身智能', '工业机器人', '服务机器人', '灵巧手', '谐波减速器', 'RV减速器', '伺服', '运动控制'],
    '稳定币': ['稳定币', 'USDT', 'USDC', '数字货币', 'CBDC', 'DeFi', '跨境支付', '加密货币', '稳定价值'],
    '新材料': ['新材料', '碳纤维', '复合材料', '高温合金', '稀土', '材料科学', '超材料', '陶瓷基', '金属基'],
    '智能汽车': ['智能汽车', '自动驾驶', '激光雷达', '域控制器', '智能座舱', 'NOA', '辅助驾驶', 'ADAS', '车载芯片'],
    '人工智能AI': ['AI', '大模型', 'LLM', '算力', 'GPU', '训练', '推理', 'Agent', '智能体', 'AIGC', '生成式AI', 'Transformer', 'ChatGPT'],
    '具身智能': ['具身智能', 'Embodied AI', '机器人大脑', 'VLA', '端到端', '感知决策一体化', '物理世界'],
    '商业航天': ['航天', '卫星', '火箭', '发射', '遥感', '低轨星座', '星链', '激光通信', '运载', '空间站'],
    '半导体': ['半导体', '芯片', '晶圆', '光刻', 'EDA', 'IP', '封测', '先进封装', '晶圆厂', '代工', '集成电路'],
    
    # 辅助分类（4个）
    '投资工作': ['基金', '投资', 'VC', '尽调', '项目', '上市', '估值', '企业', '公司', '实控人', '专项资金', '会议', '募资', '退出'],
    '党建工作': ['党支部', '党建', '组织生活'],
    '发芽报告': ['发芽报告', '每日反思', '成长日志'],
    '创投骆哥': ['创投骆哥', '自媒体', '公众号', '视频脚本', '内容创作'],
}

# 主题-核心概念映射表（用于判断应该追加到哪张卡片）
# 格式：{主题: {类别: [关键词列表] → 目标卡片文件名}}
TOPIC_CONCEPT_MAPPING = {
    # ============ 低空经济 ============
    '低空经济': {
        '政策与空域': {
            'keywords': ['政策', '空域', '法规', '监管', '适航', '航线', '飞行审批', '空域管理'],
            'target_card': '低空经济-政策红利与空域开放的竞争逻辑.md'
        },
        '技术与成本': {
            'keywords': ['电池', '续航', '载荷', '安全性', '噪音', '技术成熟度', '成本曲线', '规模化'],
            'target_card': '低空经济-eVTOL技术成熟度与成本曲线.md'
        },
        '商业模式': {
            'keywords': ['商业模式', '运营', '基础设施', '起降场', '充电', '维护', '网络效应', '生态'],
            'target_card': '低空经济-基础设施先行与商业模式验证.md'
        },
    },
    
    # ============ 机器人 ============
    '机器人': {
        '核心零部件': {
            'keywords': ['减速器', '伺服', '电机', '控制器', '传感器', '执行器', '关节', '模组'],
            'target_card': '机器人-核心零部件的国产替代机会.md'
        },
        '技术路径': {
            'keywords': ['技术路径', '人形', '具身', '运动控制', '感知', '算法', '端到端', '数据驱动'],
            'target_card': '机器人-具身智能的技术路径与竞争格局.md'
        },
        '商业化与场景': {
            'keywords': ['应用场景', '商业化', '工厂', '物流', '服务', '家庭', '特种', '成本', 'ROI'],
            'target_card': '机器人-从工厂到商业场景的落地路径.md'
        },
    },
    
    # ============ 稳定币 ============
    '稳定币': {
        '机制与安全': {
            'keywords': ['机制', '抵押', '算法', '储备', '审计', '安全', '风险', '去中心化'],
            'target_card': '稳定币-机制设计与安全性分析.md'
        },
        '应用与监管': {
            'keywords': ['跨境支付', '合规', '监管', '牌照', '银行', 'DeFi', 'CeFi', '传统金融'],
            'target_card': '稳定币-跨境支付与监管博弈.md'
        },
    },
    
    # ============ 新材料 ============
    '新材料': {
        '技术壁垒': {
            'keywords': ['工艺', '配方', '研发', '专利', '壁垒', 'know-how', '技术门槛', '良率'],
            'target_card': '新材料-工艺壁垒与护城河.md'
        },
        '市场与成本': {
            'keywords': ['成本', '规模化', '应用', '市场', '替代', '国产化', '供应链', '价格'],
            'target_card': '新材料-规模化与成本下降曲线.md'
        },
    },
    
    # ============ 智能汽车 ============
    '智能汽车': {
        '感知与决策': {
            'keywords': ['激光雷达', '摄像头', '传感器', '感知', '算法', '模型', 'Transformer', 'BEV', '占用网络'],
            'target_card': '智能汽车-感知算法的技术演进.md'
        },
        '芯片与算力': {
            'keywords': ['芯片', 'SoC', '算力', 'GPU', 'NPU', '域控', '自动驾驶芯片', 'Orin', 'Thor'],
            'target_card': '智能汽车-车载算力与芯片竞争.md'
        },
        '商业模式': {
            'keywords': ['NOA', '订阅', 'OTA', '数据', '车队', '商业模式', '付费', '体验'],
            'target_card': '智能汽车-软件定义汽车的商业模式.md'
        },
    },
    
    # ============ 人工智能AI ============
    '人工智能AI': {
        '算力基础设施': {
            'keywords': ['算力', 'GPU', '集群', '通信', '光模块', '推理', '训练', 'H100', 'H200', 'B200', '成本'],
            'target_card': 'AI算力-算力基础设施的供需缺口与成本曲线.md'
        },
        '模型与应用': {
            'keywords': ['大模型', 'LLM', 'GPT', 'Claude', '开源', '闭源', 'Agent', '应用', '场景', '落地'],
            'target_card': 'AI模型与应用-从技术突破到商业落地.md'
        },
        '生态与格局': {
            'keywords': ['生态', '开源', '闭源', '护城河', '竞争', '格局', '基础设施', '应用层', '开发者'],
            'target_card': 'AI生态-开源与闭源的竞争格局.md'
        },
    },
    
    # ============ 具身智能 ============
    '具身智能': {
        '技术路径': {
            'keywords': ['VLA', '端到端', '感知决策', '物理世界', '交互', '操作', '泛化', '数据'],
            'target_card': '具身智能-VLA模型与端到端训练路径.md'
        },
        '数据与仿真': {
            'keywords': ['数据', '仿真', '合成数据', '训练', '泛化', '场景', '机器人', '数字孪生'],
            'target_card': '具身智能-数据飞轮与仿真训练.md'
        },
    },
    
    # ============ 商业航天 ============
    '商业航天': {
        '技术与市场': {
            'keywords': ['技术', '技术门槛', '技术加速', '技术壁垒', '技术路径', '鸿沟', '技术市场'],
            'target_card': '卫星商业化-技术与市场的鸿沟.md'
        },
        '商业化与规模': {
            'keywords': ['商业模式', '成本', '规模化', '现货模式', '规模效应', '商业化', '市场', '批量发射', '可回收'],
            'target_card': '商业航天-现货模式与军方驱动的规模化逻辑.md'
        },
        '数据与应用': {
            'keywords': ['数据', '遥感', '应用', 'DaaS', '数据服务', '卖答案', '照片'],
            'target_card': '卫星数据-从卖照片到卖答案的商业模式跃迁.md'
        },
        '卫星计算': {
            'keywords': ['计算', '星地', '6G', '边缘计算', '在轨', '算力', 'AI卫星'],
            'target_card': '卫星计算-星地协同与6G融合的基础设施逻辑.md'
        },
        '激光通信': {
            'keywords': ['激光通信', '星间', '星际', '通信', '链路', '光终端'],
            'target_card': '卫星激光通信-天地数据主权的基础设施竞争.md'
        },
    },
    
    # ============ 半导体 ============
    '半导体': {
        '设计与IP': {
            'keywords': ['设计', 'IP', 'EDA', '架构', '指令集', 'RISC-V', 'ARM', 'x86', 'GPU', 'CPU'],
            'target_card': '半导体-芯片设计与IP核的竞争.md'
        },
        '制造与设备': {
            'keywords': ['制造', '晶圆', '代工', '光刻', '设备', 'ASML', '工艺', '良率', '7nm', '5nm', '3nm'],
            'target_card': '半导体-晶圆制造与设备供应链.md'
        },
        '材料与封测': {
            'keywords': ['材料', '硅片', '封测', '先进封装', 'CoWoS', '2.5D', '3D', 'Chiplet'],
            'target_card': '半导体-材料与先进封装技术.md'
        },
        '国产替代': {
            'keywords': ['国产化', '替代', '自主可控', '供应链', '卡脖子', '信创', '政策'],
            'target_card': '半导体-国产替代的路径与挑战.md'
        },
    },
    
    # ============ 投资工作（辅助分类） ============
    '投资工作': {
        '投资逻辑': {
            'keywords': ['风险投资', '反直觉', '投资逻辑', 'VC逻辑', '投资策略', '赛道', '判断'],
            'target_card': '投资逻辑-风险投资的反直觉基因.md'
        },
        '认知策略': {
            'keywords': ['认知', '策略', 'B+', '帕累托', '决策', '判断', '思维模型', '第一性原理'],
            'target_card': '认知策略-B+主义的哲学与帕累托最优.md'
        },
        '估值与退出': {
            'keywords': ['估值', 'DCF', '可比公司', 'PS', 'PE', '退出', 'IPO', '并购', '流动性'],
            'target_card': '投资估值-估值方法与退出路径.md'
        },
    },
    
    # ============ 精英思维（辅助分类，保留） ============
    '精英思维': {
        '思维模式': {
            'keywords': ['精英思维', '复杂世界', '线性因果', '系统思维', '模型', '第一性原理'],
            'target_card': '精英思维-复杂世界模型vs线性因果思维.md'
        },
    },
}

# 笔记类型权重（用于价值筛选）
NOTE_TYPE_WEIGHTS = {
    '链接笔记': 10,      # 最高权重 - 通常是骆哥认为值得学习的优质内容
    '发芽报告': 8,       # 高权重 - 经过思考加工的洞见
    '尽调访谈': 7,       # 中高权重 - 投资相关的高价值内容
    '会议记录': 6,       # 中权重 - 重要会议记录
    '录音笔记': 5,       # 中低权重 - 选择性提取
    '文本笔记': 4,       # 低权重
    '图片笔记': 3,       # 低权重
}


def get_week_range(week_offset: int = 0) -> Tuple[str, str]:
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
    - 默认(week_offset=0): 本周一到今天（统一逻辑，周一到周日都是本周一到今天）
    - 指定周次(week_offset=1, 2, ...): 完整一周（周一到周日）
    """
    today = datetime.now()
    weekday = today.weekday()  # 0=周一, 6=周日

    if week_offset == 0:
        # 本周：统一为本周一到今天
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


def clean_markdown_format(text: str) -> str:
    """
    清理Markdown格式，移除表格、代码块等，仅保留纯文本
    
    Args:
        text: 原始文本
        
    Returns:
        清理后的纯文本
    """
    if not text:
        return ""
    
    import re
    
    # 1. 移除表格（|xxx|xxx|格式）
    # 匹配表格行：| 内容 | 内容 |
    text = re.sub(r'^\|[\s\-:|]+\|$', '', text, flags=re.MULTILINE)  # 移除分隔行 |---|---| 
    text = re.sub(r'^\|.*\|$', '', text, flags=re.MULTILINE)  # 移除表格内容行
    
    # 2. 移除代码块 ```xxx```
    text = re.sub(r'```[\s\S]*?```', '', text)
    
    # 3. 移除行内代码 `xxx`
    text = re.sub(r'`[^`]+`', '', text)
    
    # 4. 移除图片 ![xxx](xxx)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    
    # 5. 移除链接 [xxx](xxx)，保留文字
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    
    # 6. 移除HTML标签
    text = re.sub(r'<[^>]+>', '', text)
    
    # 7. 移除多余的空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    
    # 8. 清理行首的行号标记（如 "1. ", "   |" 等）
    lines = text.split('\n')
    cleaned_lines = []
    for line in lines:
        # 跳过纯表格分隔符行
        if re.match(r'^[\s\-:|]+$', line):
            continue
        # 跳过只包含数字和点的行（可能是列表）
        if re.match(r'^\s*\d+\.\s*$', line):
            continue
        cleaned_lines.append(line)
    
    text = '\n'.join(cleaned_lines)
    
    return text.strip()


def generate_filename(title: str, created_at: str) -> str:
    """
    生成带日期前缀的文件名（与import_to_obsidian.py保持一致）

    Args:
        title: 笔记标题
        created_at: 创建时间 (格式: "YYYY-MM-DD HH:MM:SS")

    Returns:
        文件名（不含.md后缀）
    """
    date_str = created_at.split(" ")[0].replace("-", "")
    safe_title = title.replace("/", "-").replace("\\", "-")[:50]
    return f"{date_str}-{safe_title}"


def import_notes_to_inbox(notes: List[Dict]) -> int:
    """
    将笔记导入到Obsidian Inbox（主题由AI在Step 1生成）

    Args:
        notes: 笔记数据列表

    Returns:
        成功导入的笔记数量
    """
    print(f"📌 步骤0.5: 将笔记导入到Obsidian Inbox（主题由AI生成）")

    # 重置变更日志
    reset_change_log()

    import_results = []
    skipped_count = 0

    for note in notes:
        note_id = note.get('id')
        title = note.get('title', '无标题')
        note_type = note.get('note_type', '未知')
        created_at = note.get('created_at', '')
        content = note.get('content', '')

        # 【问题2】过滤空白笔记
        # 过滤条件：
        # 1. 标题包含"无内容输入"
        # 2. 标题包含"空白语音"
        # 3. 内容包含"暂无可校正内容"
        # 4. 内容包含"无有效语音转写内容"
        skip_reason = None
        
        if '无内容输入' in title:
            skip_reason = "标题包含'无内容输入'"
        elif '空白语音' in title:
            skip_reason = "标题包含'空白语音'"
        elif content and ('暂无可校正内容' in content or '无有效语音转写内容' in content):
            skip_reason = "内容为空白或无有效语音"
        
        if skip_reason:
            skipped_count += 1
            print(f"  ⏭️  跳过 [{title}]: {skip_reason}")
            continue

        # 主题由AI在Step 1生成，这里只获取主主题
        topic = note.get('_topic', '其他')

        # 如果还没有主题，使用关键词匹配作为后备
        if not topic or topic == '其他':
            topic = classify_note(title, note_type)
            note['_topic'] = topic

        # 确定目标目录
        target_dir = os.path.join(INBOX_DIR, topic)
        os.makedirs(target_dir, exist_ok=True)

        # 生成文件名
        filename = generate_filename(title, created_at) + ".md"
        filepath = os.path.join(target_dir, filename)

        # 检查文件是否已存在
        if os.path.exists(filepath):
            skipped_count += 1
            # 即使跳过，也要生成relative_path和topic字段
            filename_only = os.path.basename(filename)
            relative_path = os.path.join("00 📒 Inbox（From_Get笔记）", topic, filename_only)
            note['relative_path'] = relative_path
            note['_topic'] = topic
            continue

        # 生成Markdown内容
        # 支持多主题（如果有）
        topics = note.get('_topics', [topic])
        topics_display = ', '.join(topics) if len(topics) > 1 else topic

        md_content = f"""---
笔记ID: {note_id}
来源: Get笔记
类型: {note_type}
创建时间: {created_at}
主题: {topics_display}
---

# {title}

## 笔记内容

{content}

---
*本文档由Get笔记导入, 导入时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""

        # 写入文件
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(md_content)

            # 生成相对路径（用于链接）
            relative_path = os.path.join("00 📒 Inbox（From_Get笔记）", topic, filename)

            # 保存相对路径到note对象
            note['relative_path'] = relative_path

            import_results.append({
                "title": title,
                "target_dir": topic,
                "filepath": filepath,
                "note_id": note_id
            })

            # 记录到变更日志
            CHANGE_LOG['imported_notes'].append({
                'title': title,
                'filepath': filepath,
                'topic': topic
            })
        except Exception as e:
            print(f"  ❌ 导入失败 [{title}]: {e}")

    print(f"  ✅ 成功导入 {len(import_results)} 条笔记")
    print(f"  ⏭️  跳过已存在的 {skipped_count} 条笔记")

    return len(import_results)


def ai_generate_topic(note: Dict) -> List[str]:
    """
    AI生成笔记主题

    Args:
        note: 笔记数据

    Returns:
        主题列表（1-2个主题）
    """
    from openai import OpenAI
    import os

    title = note.get('title', '无标题')
    note_type = note.get('note_type', 'unknown')
    content = note.get('content', '')[:2000]  # 限制长度

    prompt = f"""你是一个专业的知识管理专家，分析这条笔记的内容，给出最合适的主题。

## 主题要求

1. **主题粒度**：一级分类（不要太细）
2. **优先匹配已知赛道**：
   - 核心投资赛道：低空经济、机器人、半导体、商业航天、智能汽车、人工智能AI、具身智能、稳定币、新材料
   - 辅助分类：投资工作、党建工作、发芽报告、创投骆哥、精英思维
3. **如果都不匹配**：生成新主题（3-6个字）
4. **多主题归属**：如果笔记涉及多个主题，最多给出2个主题

## 输出格式（JSON）

{{
    "topics": ["主题1", "主题2"],
    "reason": "选择该主题的理由..."
}}

## 笔记信息

标题：{title}
类型：{note_type}
内容：{content}
"""

    # 检查是否使用 OpenRouter
    # 默认使用 OpenRouter（避免区域限制问题），除非显式设置为 false
    use_openrouter = os.getenv('USE_OPENROUTER', 'true').lower() != 'false'

    # 从环境变量读取 API Key
    # 优先使用 OPENAI_API_KEY（OpenRouter），其次使用 ANTHROPIC_API_KEY（Anthropic 官方）
    if use_openrouter:
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            raise ValueError("未找到 OPENAI_API_KEY 环境变量（OpenRouter 需要）")
    else:
        api_key = os.getenv('ANTHROPIC_API_KEY') or os.getenv('WORKBUDDY_ANTHROPIC_API_KEY')
        if not api_key:
            raise ValueError("未找到 ANTHROPIC_API_KEY 环境变量（Anthropic 官方需要）")

    if use_openrouter:
        # 使用 OpenRouter（通过 OpenAI SDK）
        base_url = os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1')
        client = OpenAI(api_key=api_key, base_url=base_url)
        # 使用 DeepSeek 模型（避免区域限制）
        model_name = "deepseek/deepseek-chat"  # DeepSeek V3
    else:
        # 使用 Anthropic 官方（通过 OpenAI SDK）
        client = OpenAI(api_key=api_key, base_url="https://api.anthropic.com")
        model_name = "claude-3-5-sonnet-20241022"

    try:
        response = client.chat.completions.create(
            model=model_name,
            max_tokens=1024,  # 增加到 1024，避免响应被截断
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}]
        )
        response_text = response.choices[0].message.content.strip()
    except Exception as e:
        # 打印详细错误信息
        error_msg = str(e)
        print(f"  ❌ API调用失败: {error_msg[:200]}")
        raise

    # 使用增强的 JSON 提取逻辑
    result = extract_json_from_response(response_text)

    if result:
        topics = result.get('topics', [])
        # 确保至少有一个主题
        if not topics:
            topics = ['其他']
        # 最多保留2个主题
        return topics[:2]
    else:
        print(f"      ⚠️ JSON提取失败，使用关键词匹配")
        # 失败时使用关键词匹配
        topic = classify_note(title, note_type)
        return [topic]


def classify_note(title: str, note_type: str = '') -> str:
    """
    根据笔记标题和类型自动分类主题（后备方法）

    Args:
        title: 笔记标题
        note_type: 笔记类型（链接笔记/发芽报告等）

    Returns:
        主题分类名称
    """
    # 先按优先级顺序匹配标题中的关键词
    for topic, keywords in TOPIC_MAPPING.items():
        for keyword in keywords:
            if keyword in title:
                return topic

    # 如果标题没匹配到,再看笔记类型
    if '尽调' in title or '访谈' in title:
        return '投资工作'
    if '党建' in title or '支部' in title:
        return '党建工作'

    return '其他'





def ai_evaluate_single_note(note: Dict) -> Dict:
    """
    单条笔记AI评估

    Args:
        note: 笔记数据（可能是嵌套结构或扁平结构）

    Returns:
        评估结果字典
    """
    from openai import OpenAI

    # 支持嵌套结构和扁平结构
    if 'note' in note:
        # 嵌套结构
        note_data = note.get('note', {})
        title = note_data.get('title', '无标题')
        note_type = note_data.get('note_type', 'unknown')
        created_at = note_data.get('created_at', 'unknown')
        content = note_data.get('content', '')[:3000]
    else:
        # 扁平结构
        title = note.get('title', '无标题')
        note_type = note.get('note_type', 'unknown')
        created_at = note.get('created_at', 'unknown')
        content = note.get('content', '')[:3000]

    prompt = f"""你是一个专业的知识管理专家，评估这条笔记的价值。

## 评分维度（0-1分）

1. **信息密度（0.2权重）**：
   - 核心信息的浓度
   - 是否包含干货、数据、具体案例
   - 低密度（0.0-0.3）：纯记录、流水账、碎片化信息
   - 中密度（0.4-0.7）：有核心信息，但夹杂大量废话
   - 高密度（0.8-1.0）：信息密集，每段都有价值

2. **洞察深度（0.35权重）**：
   - 是否有独立判断、深度分析
   - 是否从第一性原理拆解问题
   - 低深度（0.0-0.3）：纯记录、转述他人观点
   - 中深度（0.4-0.7）：有归纳总结，但缺乏独立判断
   - 高深度（0.8-1.0）：有独立判断、深度思考、第一性原理

3. **新颖性（0.25权重）**：
   - 是否有反共识、新视角、独特洞见
   - 是否打破认知惯性
   - 低新颖性（0.0-0.3）：陈述已知事实、常见观点
   - 中新颖性（0.4-0.7）：补充验证、细节完善
   - 高新颖性（0.8-1.0）：反共识、新视角、独特洞见

4. **实用性（0.2权重）**：
   - 是否可复用、可行动、可指导实践
   - 低实用性（0.0-0.3）：纯理论、无法落地
   - 中实用性（0.4-0.7）：有一定参考价值
   - 高实用性（0.8-1.0）：可直接用于决策、行动、创作

## 星级判定（统一标准）

综合四个维度，给出星级判定：
- **三星（0.85-1.0）**：高密度 + 高深度 + 高新颖性 + 高实用性 → 极高优先级，必须生成洞察卡片
- **二星（0.75-0.84）**：至少3个维度高分 → 中优先级，应该生成洞察卡片
- **一星（0.60-0.74）**：信息密度不错，但深度/新颖性一般 → 参考价值，不生成卡片
- **不推荐（0.0-0.59）**：纯记录、低价值 → 跳过

## 输出格式（JSON）

请严格按以下JSON格式输出：

{{
    "density": 0.8,
    "insight": 0.9,
    "novelty": 0.7,
    "practicality": 0.8,
    "total_score": 0.81,
    "star": 3,
    "reason": "信息密度高，包含3个核心数据点；洞察深度足，从第一性原理拆解；有反共识观点，打破惯性认知；可直接用于投资决策。",
    "should_generate_card": true,
    "card_keywords": ["护城河", "第一性原理", "投资逻辑"]
}}

## 笔记信息

标题：{title}
类型：{note_type}
创建时间：{created_at}
内容：{content}
"""

    # 检查是否使用 OpenRouter
    # 默认使用 OpenRouter（避免区域限制问题），除非显式设置为 false
    use_openrouter = os.getenv('USE_OPENROUTER', 'true').lower() != 'false'

    # 从环境变量读取 API Key
    # 优先使用 OPENAI_API_KEY（OpenRouter），其次使用 ANTHROPIC_API_KEY（Anthropic 官方）
    if use_openrouter:
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            raise ValueError("未找到 OPENAI_API_KEY 环境变量（OpenRouter 需要）")
    else:
        api_key = os.getenv('ANTHROPIC_API_KEY') or os.getenv('WORKBUDDY_ANTHROPIC_API_KEY')
        if not api_key:
            raise ValueError("未找到 ANTHROPIC_API_KEY 环境变量（Anthropic 官方需要）")

    if use_openrouter:
        # 使用 OpenRouter（通过 OpenAI SDK）
        base_url = os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1')
        client = OpenAI(api_key=api_key, base_url=base_url)
        model_name = "deepseek/deepseek-chat"  # 使用 OpenAI 模型
    else:
        # 使用 Anthropic 官方（通过 OpenAI SDK）
        client = OpenAI(api_key=api_key, base_url="https://api.anthropic.com")
        model_name = "claude-3-5-sonnet-20241022"

    try:
        response = client.chat.completions.create(
            model=model_name,
            max_tokens=1024,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}]
        )
        response_text = response.choices[0].message.content.strip()
    except Exception as e:
        # 打印详细错误信息
        error_msg = str(e)
        print(f"  ❌ API调用失败: {error_msg[:200]}")
        raise

    # 使用增强的 JSON 提取逻辑
    evaluation = extract_json_from_response(response_text)

    if not evaluation:
        # 如果JSON提取失败，返回默认评估
        print(f"      ⚠️ JSON提取失败，使用默认评估")
        evaluation = {
            "density": 0.5,
            "insight": 0.5,
            "novelty": 0.5,
            "practicality": 0.5,
            "total_score": 0.5,
            "star": 1,
            "reason": "JSON提取失败，使用默认评估",
            "should_generate_card": False,
            "card_keywords": []
        }
    else:
        # 计算总分（如果AI没有计算）
        if 'total_score' not in evaluation:
            evaluation['total_score'] = (
                evaluation['density'] * 0.2 +
                evaluation['insight'] * 0.35 +
                evaluation['novelty'] * 0.25 +
                evaluation['practicality'] * 0.2
            )

        # 判断星级（如果AI没有判断）- 统一三星体系
        if 'star' not in evaluation:
            total = evaluation['total_score']
            if total >= 0.85:
                evaluation['star'] = 3  # 三星
            elif total >= 0.75:
                evaluation['star'] = 2  # 二星
            elif total >= 0.60:
                evaluation['star'] = 1  # 一星
            else:
                evaluation['star'] = 0  # 不推荐

        # 判断是否应该生成卡片（三星或二星）
        if 'should_generate_card' not in evaluation:
            evaluation['should_generate_card'] = evaluation.get('star', 0) >= 2

    # 添加元数据
    evaluation['note_id'] = note.get('id')
    evaluation['note'] = note

    return evaluation


def ai_evaluate_radar_only(note: Dict) -> Dict:
    """
    录音笔记AI评估（仅用于洞见雷达，不生成卡片）

    Args:
        note: 笔记数据（可能是嵌套结构或扁平结构）

    Returns:
        评估结果字典（不包含should_generate_card和card_keywords）
    """
    from openai import OpenAI

    # 支持嵌套结构和扁平结构
    if 'note' in note:
        # 嵌套结构
        note_data = note.get('note', {})
        title = note_data.get('title', '无标题')
        note_type = note_data.get('note_type', 'unknown')
        created_at = note_data.get('created_at', 'unknown')
        content = note_data.get('content', '')[:3000]
    else:
        # 扁平结构
        title = note.get('title', '无标题')
        note_type = note.get('note_type', 'unknown')
        created_at = note.get('created_at', 'unknown')
        content = note.get('content', '')[:3000]

    prompt = f"""你是一个专业的知识管理专家，评估这条录音笔记的洞见价值。

## 评分维度（0-1分）

1. **信息密度（0.2权重）**：
   - 核心信息的浓度
   - 是否包含干货、数据、具体案例
   - 低密度（0.0-0.3）：纯记录、流水账、碎片化信息
   - 中密度（0.4-0.7）：有核心信息，但夹杂大量废话
   - 高密度（0.8-1.0）：信息密集，每段都有价值

2. **洞察深度（0.35权重）**：
   - 是否有独立判断、深度分析
   - 是否从第一性原理拆解问题
   - 低深度（0.0-0.3）：纯记录、转述他人观点
   - 中深度（0.4-0.7）：有归纳总结，但缺乏独立判断
   - 高深度（0.8-1.0）：有独立判断、深度思考、第一性原理

3. **新颖性（0.25权重）**：
   - 是否有反共识、新视角、独特洞见
   - 是否打破认知惯性
   - 低新颖性（0.0-0.3）：陈述已知事实、常见观点
   - 中新颖性（0.4-0.7）：补充验证、细节完善
   - 高新颖性（0.8-1.0）：反共识、新视角、独特洞见

4. **实用性（0.2权重）**：
   - 是否可复用、可行动、可指导实践
   - 低实用性（0.0-0.3）：纯理论、无法落地
   - 中实用性（0.4-0.7）：有一定参考价值
   - 高实用性（0.8-1.0）：可直接用于决策、行动、创作

## 星级判定（统一标准）

综合四个维度，给出星级判定：
- **三星（0.85-1.0）**：高密度 + 高深度 + 高新颖性 + 高实用性
- **二星（0.75-0.84）**：至少3个维度高分
- **一星（0.60-0.74）**：信息密度不错，但深度/新颖性一般
- **不推荐（0.0-0.59）**：纯记录、低价值

## 输出格式（JSON）

请严格按以下JSON格式输出：

{{
    "density": 0.8,
    "insight": 0.9,
    "novelty": 0.7,
    "practicality": 0.8,
    "total_score": 0.81,
    "star": 3,
    "reason": "信息密度高，包含3个核心数据点；洞察深度足，从第一性原理拆解；有反共识观点，打破惯性认知；可直接用于投资决策。"
}}

注意：录音笔记不生成洞察卡片，所以不需要返回should_generate_card和card_keywords字段。

## 笔记信息

标题：{title}
类型：{note_type}
创建时间：{created_at}
内容：{content}
"""

    # 检查是否使用 OpenRouter
    # 默认使用 OpenRouter（避免区域限制问题），除非显式设置为 false
    use_openrouter = os.getenv('USE_OPENROUTER', 'true').lower() != 'false'

    # 从环境变量读取 API Key
    # 优先使用 OPENAI_API_KEY（OpenRouter），其次使用 ANTHROPIC_API_KEY（Anthropic 官方）
    if use_openrouter:
        api_key = os.getenv('OPENAI_API_KEY')
        if not api_key:
            raise ValueError("未找到 OPENAI_API_KEY 环境变量（OpenRouter 需要）")
    else:
        api_key = os.getenv('ANTHROPIC_API_KEY') or os.getenv('WORKBUDDY_ANTHROPIC_API_KEY')
        if not api_key:
            raise ValueError("未找到 ANTHROPIC_API_KEY 环境变量（Anthropic 官方需要）")

    if use_openrouter:
        # 使用 OpenRouter（通过 OpenAI SDK）
        base_url = os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1')
        client = OpenAI(api_key=api_key, base_url=base_url)
        model_name = "deepseek/deepseek-chat"  # 使用 OpenAI 模型
    else:
        # 使用 Anthropic 官方（通过 OpenAI SDK）
        client = OpenAI(api_key=api_key, base_url="https://api.anthropic.com")
        model_name = "claude-3-5-sonnet-20241022"

    try:
        response = client.chat.completions.create(
            model=model_name,
            max_tokens=1024,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}]
        )
        response_text = response.choices[0].message.content.strip()
    except Exception as e:
        # 打印详细错误信息
        error_msg = str(e)
        print(f"  ❌ API调用失败: {error_msg[:200]}")
        raise

    # 使用增强的 JSON 提取逻辑
    evaluation = extract_json_from_response(response_text)

    if not evaluation:
        # 如果JSON提取失败，返回默认评估
        print(f"      ⚠️ JSON提取失败，使用默认评估")
        evaluation = {
            "density": 0.5,
            "insight": 0.5,
            "novelty": 0.5,
            "practicality": 0.5,
            "total_score": 0.5,
            "star": 1,
            "reason": "JSON提取失败，使用默认评估"
        }
    else:
        # 计算总分（如果AI没有计算）
        if 'total_score' not in evaluation:
            evaluation['total_score'] = (
                evaluation['density'] * 0.2 +
                evaluation['insight'] * 0.35 +
                evaluation['novelty'] * 0.25 +
                evaluation['practicality'] * 0.2
            )

        # 判断星级（如果AI没有判断）- 统一三星体系
        if 'star' not in evaluation:
            total = evaluation['total_score']
            if total >= 0.85:
                evaluation['star'] = 3  # 三星
            elif total >= 0.75:
                evaluation['star'] = 2  # 二星
            elif total >= 0.60:
                evaluation['star'] = 1  # 一星
            else:
                evaluation['star'] = 0  # 不推荐

        # 录音笔记不生成卡片
        evaluation['should_generate_card'] = False
        evaluation['card_keywords'] = []

    # 添加元数据
    evaluation['note_id'] = note.get('id')
    evaluation['note'] = note

    return evaluation


def save_all_evaluations(evaluations: List[Dict]):
    """
    保存所有评估结果到JSON文件

    Args:
        evaluations: 评估结果列表
    """
    json_path = f"{WORK_DIR}/all_evaluations.json"

    # 统计星级分布
    star_stats = {}
    for e in evaluations:
        star = e.get('star', 0)
        star_stats[star] = star_stats.get(star, 0) + 1

    data = {
        'evaluated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'total_notes': len(evaluations),
        'star_distribution': star_stats,
        'evaluations': []
    }

    for e in evaluations:
        # 只保留必要字段
        eval_summary = {
            'note_id': e.get('note_id', e.get('note', {}).get('id', '')),
            'title': e.get('title', e.get('note', {}).get('title', '无标题'))[:50],
            'type': e.get('type', e.get('note', {}).get('note_type', 'unknown')),
            'density': e.get('density', 0.5),
            'insight': e.get('insight', 0.5),
            'novelty': e.get('novelty', 0.5),
            'practicality': e.get('practicality', 0.5),
            'total_score': e.get('total_score', 0.5),
            'star': e.get('star', 0),
            'reason': e.get('reason', ''),
            'should_generate_card': e.get('should_generate_card', False),
            'card_keywords': e.get('card_keywords', [])
        }
        data['evaluations'].append(eval_summary)

    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"  💾 保存所有评估结果: {json_path}")


def find_target_card(note: Dict) -> str:
    """
    根据主题和核心概念查找应该追加的卡片（方案C：关键词映射表 + AI兜底）

    Args:
        note: 笔记数据

    Returns:
        应该追加的卡片文件名，如果没有匹配则返回None
    """
    topic = note.get('_topic', '')
    title = note.get('title', '')
    content = note.get('content', '')

    # 如果主题不在映射表中，直接返回None（新建）
    if topic not in TOPIC_CONCEPT_MAPPING:
        return None

    # 获取该主题的映射表
    topic_mappings = TOPIC_CONCEPT_MAPPING[topic]

    # 合并标题和内容，用于关键词匹配
    text = title + ' ' + content

    # 遍历该主题的所有类别，查找匹配的卡片
    for category_name, category_info in topic_mappings.items():
        keywords = category_info['keywords']
        target_card = category_info['target_card']

        # 检查是否包含任意关键词
        for keyword in keywords:
            if keyword in text:
                print(f"  🎯 匹配到卡片: {target_card} (关键词: {keyword})")
                return target_card

    # 如果没有匹配到，返回None（AI兜底：问AI或者新建）
    print(f"  ⚠️  未匹配到现有卡片，将新建")
    return None


def extract_insights(note: Dict) -> Dict:
    """
    从笔记中提取洞察（核心段落和金句）

    Args:
        note: 笔记数据

    Returns:
        包含核心段落和金句的字典
    """
    content = note.get('content', '')
    title = note.get('title', '')
    note_type = note.get('note_type', '')

    insights = {
        'core_paragraphs': [],
        'golden_sentences': []
    }

    # 洞察关键词库（扩展版）
    insight_keywords = [
        # 洞察类
        '洞察', '观点', '思考', '理解', '本质', '核心', '关键',
        # 逻辑类
        '逻辑', '规律', '原因', '因此', '所以', '因为', '导致', '造成',
        # 判断类
        '判断', '认为', '结论', '发现', '意识到', '认识到',
        # 价值类
        '价值', '意义', '重要性', '关键因素', '决定性',
        # 对比类
        '对比', '差异', '区别', '不同', '相反', '而', '但',
        # 模式类
        '模式', '框架', '结构', '系统', '机制',
        # 趋势类
        '趋势', '未来', '方向', '路径', '机会', '风险',
        # 原则类
        '原则', '经验', '教训', '方法', '策略',
    ]

    # 强语气词（用于金句提取）
    strong_tone_words = [
        '本质', '核心', '关键', '最重要', '真正的', '才是', '必须', '不能', '一定', '必然',
        '从来', '始终', '永远', '唯一', '只有', '除非', '无论如何', '根本上',
        '其实', '本质上', '事实上', '实际上', '根本上',
    ]

    # 提取核心段落（使用启发式规则）
    lines = content.split('\n')
    current_paragraph = []

    for line in lines:
        line = line.strip()
        if not line:
            if current_paragraph:
                paragraph = ' '.join(current_paragraph)
                # 过滤条件：
                # 1. 长度在100-600字之间
                # 2. 包含至少1个洞察关键词
                # 3. 不是纯列表或纯数据
                if 100 < len(paragraph) < 600:
                    # 检查段落是否包含洞察关键词
                    keyword_count = sum(1 for kw in insight_keywords if kw in paragraph)
                    if keyword_count >= 1:
                        # 评分：关键词密度 + 段落长度合理性
                        score = keyword_count * 10
                        if 150 < len(paragraph) < 400:  # 黄金长度区间
                            score += 5

                        # 只保留高分段落（>=15分）
                        if score >= 15:
                            insights['core_paragraphs'].append(paragraph)
                current_paragraph = []
        else:
            current_paragraph.append(line)

    # 如果还有段落没处理
    if current_paragraph:
        paragraph = ' '.join(current_paragraph)
        if 100 < len(paragraph) < 600:
            insights['core_paragraphs'].append(paragraph)

    # 限制核心段落数量（最多5个，按长度排序，优先取中等长度的）
    if len(insights['core_paragraphs']) > 5:
        # 按长度评分：150-300字最佳
        insights['core_paragraphs'].sort(
            key=lambda p: -abs(len(p) - 225)  # 225是(150+300)/2
        )
        insights['core_paragraphs'] = insights['core_paragraphs'][:5]

    # 提取金句（短而有力的句子）
    for line in lines:
        line = line.strip()
        # 金句特征: 短 (30-100字), 有标点, 不是列表项, 不是标题
        is_list_item = line.startswith('-') or line.startswith('*') or line.startswith('•')
        is_heading = line.startswith('#') or re.match(r'^[一二三四五六七八九十]+[、.]', line)

        if (30 < len(line) < 100 and
            not is_list_item and not is_heading and
            (line.endswith('。') or line.endswith('！') or line.endswith('？') or
             line.endswith('.') or line.endswith('!') or line.endswith('?'))):

            # 包含强语气词
            strong_word_count = sum(1 for kw in strong_tone_words if kw in line)

            # 评分：强语气词数量 + 句子长度合理性
            score = strong_word_count * 10
            if 40 < len(line) < 80:  # 黄金长度区间
                score += 5

            # 只保留高分金句（>=10分）
            if score >= 10 and line not in insights['golden_sentences']:
                insights['golden_sentences'].append(line)

    # 限制金句数量（最多3个）
    insights['golden_sentences'] = insights['golden_sentences'][:3]

    return insights


def generate_zettelkasten_card(note: Dict, insights: Dict, topic: str, card_keywords: List[str] = None, use_qmd: bool = True) -> str:
    """
    生成Zettelkasten洞察卡片（支持增量追加，方案C：关键词映射表 + AI兜底 + qmd智能搜索）

    Args:
        note: 原始笔记数据
        insights: 提取的洞察
        topic: 主题分类
        card_keywords: AI评估时提取的关键词（用于生成卡片）
        use_qmd: 是否使用qmd进行相似卡片搜索

    Returns:
        卡片文件名
    """
    title = note.get('title', '')
    note_id = note.get('id', '')
    created_at = note.get('created_at', '')[:10]  # 只取日期部分

    target_card_filename = None
    
    # 优先使用 qmd 搜索相似卡片（如果可用且启用）
    if use_qmd and QMD_AVAILABLE:
        try:
            target_card_filename = find_matching_card(note)
        except Exception as e:
            print(f"      ⚠️ qmd搜索失败，回退到关键词映射表: {e}")
    
    # 如果 qmd 没找到或不可用，使用关键词映射表
    if not target_card_filename:
        target_card_filename = find_target_card(note)

    if target_card_filename:
        # 找到了匹配的卡片，使用该卡片文件名
        card_filename = target_card_filename
        # 追加模式不更新卡片结构和命名
    else:
        # 没有匹配到，创建新卡片，使用声明式命名
        # 获取评估信息
        evaluation = note.get('_evaluation', {})
        reason = evaluation.get('reason', '')

        # 生成声明式卡片名称
        card_name = ""

        if reason:
            # 清理reason，提取一个完整的观点句作为声明
            reason_cleaned = clean_markdown_format(reason)
            sentences = reason_cleaned.replace('！', '。').replace('？', '。').split('。')
            for sent in sentences[:3]:  # 看前3句话
                sent = sent.strip()
                # 理想长度：10-30字，包含"是/就是/意味着/说明"等断言词
                if len(sent) >= 10 and len(sent) <= 30:
                    skip_patterns = ["这条笔记", "本文", "这篇文章", "这个内容", "这段内容", "值得注意", "值得关注", "提供了", "包含了"]
                    if not any(p in sent for p in skip_patterns):
                        # 这是一个声明式句子
                        card_name = sent
                        break

        # 降级：从关键词生成声明
        if not card_name and card_keywords and len(card_keywords) >= 2:
            # 用两个关键词生成一个简单的声明
            card_name = f"{card_keywords[0]}：{card_keywords[1]}"

        # 最后降级：从标题生成
        if not card_name:
            # 尝试从标题提取声明
            if '——' in title:
                card_name = title.split('——')[0].strip()
            elif '：' in title:
                card_name = title.split('：')[0].strip()
            else:
                card_name = title[:25]

        # 清理特殊字符，限制长度
        card_name = re.sub(r'[<>:"/\|?*]', '', card_name)
        card_name = card_name.strip()[:30]

        # 直接使用声明式命名（不加主题前缀）
        card_filename = f"{card_name}.md"

    card_path = os.path.join(ZETTELKASTEN_DIR, card_filename)

    # 写入卡片文件
    os.makedirs(ZETTELKASTEN_DIR, exist_ok=True)

    # 检查是否已存在
    is_update = os.path.exists(card_path)

    if is_update:
        # 增量追加模式：读取现有卡片，追加到"📌 后续补充"区块末尾
        with open(card_path, 'r', encoding='utf-8') as f:
            existing_content = f.read()

        # 构建本次追加的标记行（用于去重判断）
        # 注意：relative_path已经是完整格式 "00 📒 Inbox（From_Get笔记）/主题/文件名.md"
        note_relative_path = note.get('relative_path', title)
        # 清理路径：去除 .md 后缀
        if note_relative_path.endswith('.md'):
            note_relative_path = note_relative_path[:-3]
        # 直接使用relative_path（已包含完整路径），不再额外拼接
        source_marker = f"### {created_at} 来自 [[{note_relative_path}]]"

        # 防重复：如果相同来源已经追加过，跳过
        if source_marker in existing_content:
            print(f"      ⏭️  已存在相同来源追加，跳过")
            return card_filename

        # 构建追加内容（追加到文件末尾，而不是 replace）
        append_content = f"\n{source_marker}\n\n"

        # 添加"与原核心洞见的关联"说明
        relation_note = generate_relation_note(note, existing_content)
        if relation_note:
            append_content += f"**🔗 与原核心洞见的关联**: {relation_note}\n\n"

        # 添加核心段落
        for i, para in enumerate(insights['core_paragraphs'][:3], 1):
            append_content += f"{para}\n\n"

        # 添加金句
        if insights.get('golden_sentences'):
            append_content += "**金句**:\n"
            for sentence in insights['golden_sentences']:
                append_content += f"- {sentence}\n"
            append_content += "\n"

        # 确保文件末尾有"## 📌 后续补充"区块，然后追加到末尾
        if "## 📌 后续补充" in existing_content:
            # 直接追加到文件末尾（不 replace，避免重复）
            updated_content = existing_content.rstrip() + "\n" + append_content
        else:
            # 如果没有后续补充区块，先加区块再追加
            updated_content = existing_content.rstrip() + "\n\n## 📌 后续补充\n" + append_content

        # 写入更新后的卡片
        with open(card_path, 'w', encoding='utf-8') as f:
            f.write(updated_content)

        # 记录到变更日志
        CHANGE_LOG['updated_cards'].append({
            'filename': card_filename,
            'note_title': title
        })

    else:
        # 新建卡片模式：创建完整卡片（按用户要求的原子化三部曲格式）
        
        # 获取评估信息
        evaluation = note.get('_evaluation', {})
        reason = evaluation.get('reason', '')
        note_content = note.get('content', '')
        
        # ========== 改进1: 核心洞察从笔记原文提取 ==========
        # 策略：优先从原文提取有信息量的段落，而不是用评估语言
        core_insight = ""
        
        # 1. 优先从原文content中提取核心段落
        if note_content:
            # 找包含关键信息的段落（跳过开头/结尾的客套话）
            content_cleaned = clean_markdown_format(note_content)
            paragraphs = content_cleaned.split('\n\n')
            
            for para in paragraphs:
                para = para.strip()
                # 选择有实质内容的段落（50-300字）
                if 50 < len(para) < 400:
                    # 跳过过于通用的开头
                    skip_patterns = ["本文", "这篇文章", "以下", "首先", "下面", "今天", "首先"]
                    if not any(p in para[:10] for p in skip_patterns):
                        core_insight = para[:200]  # 200字以内
                        break
        
        # 2. 降级：使用insights中的core_paragraphs
        if not core_insight and insights.get('core_paragraphs'):
            core_insight = insights['core_paragraphs'][0][:200]
        
        # 3. 最后降级：使用reason（评估语言）
        if not core_insight and reason:
            core_insight = reason[:200]
        
        # 清理
        core_insight = clean_markdown_format(core_insight)
        
        # ========== 改进2: 金句从原文提取 ==========
        golden_sentence = ""
        
        # 1. 优先从原文找金句
        if note_content:
            lines = note_content.split('\n')
            for line in lines:
                line = line.strip()
                line_clean = clean_markdown_format(line)
                # 金句特征：有一定长度，有结尾标点，不是标题/列表
                if (20 < len(line_clean) < 150 and 
                    not line_clean.startswith('#') and 
                    not line_clean.startswith('-') and
                    (line_clean.endswith('。') or line_clean.endswith('！') or 
                     line_clean.endswith('？') or line_clean.endswith('"') or
                     line_clean.endswith('」'))):
                    golden_sentence = line_clean
                    break
        
        # 2. 降级：使用insights中的golden_sentences
        if not golden_sentence and insights.get('golden_sentences'):
            golden_sentence = insights['golden_sentences'][0]
        
        golden_sentence = clean_markdown_format(golden_sentence)
        
        # ========== 改进3: 行动建议 ==========
        action_connection = ""
        
        # 1. 从reason中提取（如果有的话）
        if reason:
            action_keywords = ["建议", "应该", "可以", "需要", "值得", "关键在于", "方向是", "策略是", "行动", "把握", "关注", "思考"]
            for keyword in action_keywords:
                if keyword in reason:
                    idx = reason.find(keyword)
                    action_connection = reason[idx:idx+100]
                    for sep in ["。", "；", "！", "？"]:
                        if sep in action_connection:
                            action_connection = action_connection.split(sep)[0] + sep
                            break
                    if len(action_connection) >= 10:
                        break
        
        # 2. 如果reason中没有，从原文content中提取
        if not action_connection and note_content:
            for keyword in ["建议", "应该", "可以", "需要", "关键", "方向"]:
                if keyword in note_content:
                    idx = note_content.find(keyword)
                    action_connection = note_content[idx:idx+80]
                    for sep in ["。", "；", "！", "？"]:
                        if sep in action_connection:
                            action_connection = action_connection.split(sep)[0] + sep
                            break
                    if len(action_connection) >= 10:
                        break
        
        # 3. 最后降级：使用主题相关的默认建议
        if not action_connection or len(action_connection) < 10:
            topic_action_map = {
                "人工智能AI": "关注AI技术落地场景，思考对硬科技投资的影响",
                "机器人": "关注核心零部件和商业化落地能力",
                "商业航天": "关注火箭成本下降曲线和卫星应用场景",
                "半导体": "关注国产替代进度和先进制程突破",
                "智能汽车": "关注智能驾驶渗透率和产业链机会",
                "低空经济": "关注政策开放节奏和适航认证进展",
                "创投观察": "思考内容转化路径和受众定位"
            }
            action_connection = topic_action_map.get(topic, "深入理解此洞察，思考在硬科技投资/自媒体创作中的应用场景")

        action_connection = clean_markdown_format(action_connection)

        # ========== C-I-C 结构生成 ==========

        # Context（脱水背景）：笔记的背景信息/问题提出
        # 从笔记标题或内容中提取背景信息
        context = ""
        if '——' in title:
            context = title.split('——')[1].strip()
        elif '：' in title:
            context = title.split('：')[1].strip()
        else:
            # 从内容第一段提取背景
            if note_content:
                content_cleaned = clean_markdown_format(note_content)
                paragraphs = content_cleaned.split('\n\n')
                if paragraphs:
                    context = paragraphs[0][:150]

        # Insight（核心洞察）：刚才生成的核心洞察
        insight = core_insight

        # Connection（关联钩子）：行动建议
        connection = action_connection

        # 清理
        context = clean_markdown_format(context)
        insight = clean_markdown_format(insight)

        # C-I-C 结构：Context + Insight + Connection
        card_content = f"""---
卡片类型: 洞见
主题标签:
  - {topic}
来源笔记: "{note.get('title', '无标题')}"
创建: {created_at}
更新: {created_at}
---

## Context（脱水背景）

{context}

---

## Insight（核心洞察）

{insight}

---

## Connection（关联钩子）

{connection}

---

*来源: [[{note.get('relative_path', title).replace('.md', '')}]] | 更新于 {created_at}*
"""

        with open(card_path, 'w', encoding='utf-8') as f:
            f.write(card_content)

        CHANGE_LOG['created_cards'].append({
            'filename': card_filename,
            'note_title': title
        })

    return card_filename


def parse_existing_topic_index(topic_path: str) -> Dict:
    """
    解析现有的主题索引文件，提取覆盖周数

    Args:
        topic_path: 主题索引文件路径

    Returns:
        包含覆盖周数的字典
    """
    existing_data = {
        'covered_weeks': set(),  # 已覆盖的周数
    }

    if not os.path.exists(topic_path):
        return existing_data

    with open(topic_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 提取覆盖周数
    week_match = re.search(r'覆盖周数:\s*(.+)', content)
    if week_match:
        weeks_str = week_match.group(1).strip()
        if weeks_str and weeks_str != '未知':
            existing_data['covered_weeks'] = set(weeks_str.split(', '))

    return existing_data


def get_week_label_from_date(date_str: str) -> str:
    """
    从日期字符串获取周标签（格式: YYYY-Wxx）

    Args:
        date_str: 日期字符串 (YYYY-MM-DD)

    Returns:
        周标签
    """
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        week_number = date_obj.isocalendar()[1]
        return f"{date_obj.year}-W{week_number:02d}"
    except:
        return "未知"


def get_month_label_from_date(date_str: str) -> str:
    """
    从日期字符串获取月标签（格式: YYYY-MM）

    Args:
        date_str: 日期字符串 (YYYY-MM-DD)

    Returns:
        月标签
    """
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{date_obj.year}-{date_obj.month:02d}"
    except:
        return "未知"


def extract_action_suggestion(reason: str) -> str:
    """
    从reason中提取行动线索（简短可执行的建议）

    Args:
        reason: AI评估的理由

    Returns:
        行动建议文本（简短，20-50字）
    """
    if not reason:
        return "深入理解并思考此洞察的应用场景"

    # 尝试提取"建议"相关的内容
    action_keywords = ["建议", "应该", "可以", "需要", "值得", "关键在于", "重点是", "行动", "方向是", "策略是"]
    for keyword in action_keywords:
        idx = reason.find(keyword)
        if idx != -1:
            # 提取关键词后的句子（最多50字，确保简短）
            sentence = reason[idx:idx+60]
            # 在句号、分号、逗号处截断
            for sep in ["。", "；", "，", "！", "？"]:
                if sep in sentence:
                    sentence = sentence.split(sep)[0] + sep
                    break
            # 如果太长，精简到50字以内
            if len(sentence) > 50:
                sentence = sentence[:50] + "..."
            return sentence

    # 如果没找到，返回简短的默认建议
    return "结合自身实践深入思考此洞察的落地方式"


def extract_full_insight(note: Dict, evaluation: Dict) -> str:
    """
    提取完整洞见段落（原文核心段落 + AI点评）

    Args:
        note: 笔记数据
        evaluation: AI评估结果

    Returns:
        完整洞见文本（最多300字）
    """
    # 优先使用AI评估的reason
    reason = evaluation.get('reason', '')
    if reason:
        return reason[:300]  # 限制在300字以内

    # 降级：提取笔记内容
    content = note.get('note_content', '') or note.get('content', '')
    if content:
        return content[:200] + "..."

    return "暂无详细洞见内容"


def generate_relation_note(note: Dict, existing_content: str) -> str:
    """
    生成"与原核心洞见的关联"说明

    Args:
        note: 新笔记数据
        existing_content: 现有卡片内容

    Returns:
        关联说明文本（1-2句话）
    """
    # 提取现有卡片的核心洞见（从"## 核心洞见"区块）
    existing_core = ""
    if "## 核心洞见" in existing_content:
        idx = existing_content.find("## 核心洞见")
        end_idx = existing_content.find("\n##", idx + 1)
        if end_idx == -1:
            end_idx = len(existing_content)
        existing_core = existing_content[idx:end_idx].replace("## 核心洞见", "").strip()[:200]

    # 提取新笔记的核心洞察
    evaluation = note.get('_evaluation', {})
    new_insight = evaluation.get('reason', '')
    if new_insight:
        new_insight = new_insight[:150]
    else:
        insights = extract_insights(note)
        if insights['core_paragraphs']:
            new_insight = insights['core_paragraphs'][0][:150]

    # 基于对比生成关联说明
    if existing_core and new_insight:
        # 如果两份洞察有关键词重叠，说明是深化
        common_words = set(existing_core.split()) & set(new_insight.split())
        if len(common_words) >= 3:
            return f"这条新洞察与原核心洞见在{', '.join(list(common_words)[:3])}等概念上相关，提供了更深入或更具体的应用场景。"
        else:
            return f"这条新洞察从不同角度补充了原核心洞见，拓展了{note.get('_topic', '该主题')}的应用边界。"
    elif new_insight:
        return f"这条新洞察补充了原核心洞见的实践案例，提供了更具体的行动指引。"
    else:
        return "新增同主题洞察，提供了更多维度的视角。"



def extract_note_highlight(note: Dict) -> str:
    """
    从笔记中提取摘要/亮点（精炼版，最多80字符）

    Args:
        note: 笔记数据

    Returns:
        摘要文本
    """
    content = note.get('content', '')

    # 如果是空内容，返回空
    if not content:
        return ""

    # 移除markdown标记
    # 移除标题标记（#、##、###等）
    content = re.sub(r'^#+\s*', '', content, flags=re.MULTILINE)
    # 移除加粗标记
    content = re.sub(r'\*\*(.+?)\*\*', r'\1', content)
    # 移除代码块
    content = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
    # 移除链接
    content = re.sub(r'\[.+?\]\(.+?\)', '', content)

    # 清理多余空白
    content = re.sub(r'\n+', '\n', content)
    content = content.strip()

    # 如果内容包含"录音信息"、"智能总结"等Get笔记的模板字段
    # 尝试跳过这些结构化字段,提取真正的核心内容
    if "录音信息" in content:
        # 尝试提取"录音总结"后的第一段
        summary_match = re.search(r'录音总结\n*(.+?)(?:\n|$)', content, re.DOTALL)
        if summary_match:
            content = summary_match.group(1).strip()

    # 移除常见的Get笔记模板内容
    content = re.sub(r'录音信息.*?内容类型.*?\n*', '', content, flags=re.DOTALL)
    content = re.sub(r'-\s*录音时间：.+?\n*', '', content)
    content = re.sub(r'-\s*时长：.+?\n*', '', content)
    content = re.sub(r'-\s*参与人数：.+?\n*', '', content)

    content = content.strip()

    # 提取前80字符（更精炼）
    if len(content) > 80:
        highlight = content[:80] + "..."
    else:
        highlight = content

    return highlight


def generate_ai_summary(note: Dict) -> str:
    """
    生成 AI 智能总结（核心观点，150-200字）

    格式要求：
    ① 主要论点/框架（30-40%篇幅）
    ② 关键洞察/反共识观点（40-50%篇幅）
    ③ actionable结论（10-30%篇幅）

    Args:
        note: 笔记数据

    Returns:
        AI 智能总结文本（150-200字）
    """
    content = note.get('content', '')
    title = note.get('title', '')
    topic = note.get('_topic', '')

    # 首先检查 AI 评估结果中是否已有核心观点
    evaluation = note.get('_evaluation', {})
    ai_reason = evaluation.get('reason', '')

    if ai_reason and len(ai_reason) >= 100:
        # 清理 AI 评估结果，提取核心观点
        # 移除常见的评估框架性文字
        summary = re.sub(r'^这条.*?(提供了|包含|展现了|具有)', '', ai_reason)
        summary = re.sub(r'^该.*?(主要|核心|关键|突出)', '', summary)

        # 清理标点和多余空白
        summary = re.sub(r'[。,，;；\s]+', '。', summary).strip()

        # 截取到合适长度（150-200字）
        if len(summary) > 200:
            # 尝试按句子截断
            sentences = re.split(r'[。!！?？]', summary)
            result = ''
            for sent in sentences:
                if len(result + sent) <= 200:
                    result += sent + '。'
                else:
                    break
            summary = result.strip()

        if len(summary) >= 100:
            return summary

    # 如果没有 AI 评估结果，从笔记内容中提取
    # 移除 Get 笔记的模板内容
    cleaned_content = re.sub(r'录音信息.*?内容类型.*?\n*', '', content, flags=re.DOTALL)
    cleaned_content = re.sub(r'-\s*录音时间：.+?\n*', '', cleaned_content)
    cleaned_content = re.sub(r'-\s*时长：.+?\n*', '', cleaned_content)
    cleaned_content = re.sub(r'-\s*参与人数：.+?\n*', '', cleaned_content)
    cleaned_content = re.sub(r'录音总结.*?\n*', '', cleaned_content)
    cleaned_content = re.sub(r'智能总结.*?\n*', '', cleaned_content)

    # 移除 Markdown 标记
    cleaned_content = re.sub(r'^#+\s*', '', cleaned_content, flags=re.MULTILINE)
    cleaned_content = re.sub(r'\*\*(.+?)\*\*', r'\1', cleaned_content)
    cleaned_content = re.sub(r'```.*?```', '', cleaned_content, flags=re.DOTALL)

    # 清理多余空白
    cleaned_content = re.sub(r'\n+', '\n', cleaned_content).strip()

    # 提取前 200 字作为核心观点
    if len(cleaned_content) > 200:
        # 尝试按句子截断
        sentences = re.split(r'[。!！?？\n]', cleaned_content)
        result = ''
        for sent in sentences:
            if len(result + sent) <= 200:
                result += sent + '。'
            else:
                break
        summary = result.strip()
    else:
        summary = cleaned_content

    # 如果总结太短，补充上下文
    if len(summary) < 100:
        summary = f"{title}。{summary}"

    return summary


def generate_topic_index_by_week(topic: str, week_notes: List[Dict], week_range: Tuple[str, str], week_label: str, top_notes: List[Dict] = None) -> str:
    """
    生成按周的主题索引页（每周每个主题一个独立文件）

    Args:
        topic: 主题名称
        week_notes: 本周该主题下的所有笔记
        week_range: 本周时间范围 (start_date, end_date)
        week_label: 周标签（如 "2026-W12"）
        top_notes: 本周高价值笔记列表

    Returns:
        主题索引文件路径
    """
    # 文件命名：{主题}-{年份}-W{周数}.md
    topic_path = os.path.join(TOPIC_INDEX_DIR, f"{topic}-{week_label}.md")

    # 提取高价值笔记的id集合
    top_note_ids = set()
    if top_notes:
        top_note_ids = {note['id'] for note in top_notes if note.get('_topic') == topic}

    # 计算该主题下的高价值笔记数
    top_notes_count = sum(1 for note in week_notes if note['id'] in top_note_ids)

    # 生成内容
    content = f"""---
索引类型: 主题索引
主题: {topic}
周次: {week_label}
时间范围: {week_range[0]} 至 {week_range[1]}
笔记数: {len(week_notes)}
高价值笔记: {top_notes_count}
最后更新: {datetime.now().strftime("%Y-%m-%d %H:%M")}
---

# {topic} - {week_label}

> {week_range[0]} 至 {week_range[1]} 共 {len(week_notes)} 条笔记
> ⭐ {top_notes_count} 条高价值笔记

---

## 📅 按日期查看

"""

    # 按日期分组笔记
    dates_in_week = {}
    for note in week_notes:
        created_at = note.get('created_at', '')[:10]
        if created_at not in dates_in_week:
            dates_in_week[created_at] = []
        dates_in_week[created_at].append(note)

    for date in sorted(dates_in_week.keys(), reverse=True):
        date_notes = dates_in_week[date]
        content += f"\n### {date} ({len(date_notes)}条)\n\n"

        for note in date_notes:
            title = note.get('title', '')
            note_type = note.get('note_type', '')
            note_id = note.get('id', '')

            # 判断是否是高价值笔记
            is_top_note = note_id in top_note_ids
            star_prefix = "⭐ " if is_top_note else ""

            # 提取摘要/亮点
            highlight = extract_note_highlight(note)
            highlight_text = f"\n  > {highlight}" if highlight else ""

            # 只显示非空的笔记类型
            type_display = f" ({note_type})" if note_type else ""
            content += f"- {star_prefix}**{title}**{type_display}{highlight_text}\n"
            # 使用带日期前缀的完整文件名
            filename = generate_filename(title, note.get('created_at', ''))
            content += f"  - 链接: [[{filename}]]\n\n"

    # 添加"相关链接"区块
    content += "---\n\n## 🔗 相关链接\n\n"
    content += f"- [[本周索引|返回本周总览]]\n"
    content += f"- [[{topic}|查看主题累积索引]]（如存在）\n"

    # 写入文件
    os.makedirs(TOPIC_INDEX_DIR, exist_ok=True)
    with open(topic_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return topic_path


def generate_topic_index(topic: str, all_notes: List[Dict], week_range: Tuple[str, str], top_notes: List[Dict] = None) -> str:
    """
    生成主题索引页（支持累积式增长）

    Args:
        topic: 主题名称
        all_notes: 该主题下的所有笔记（包括历史记录）
        week_range: 本周时间范围 (start_date, end_date)
        top_notes: 本周高价值笔记列表

    Returns:
        主题索引文件路径
    """
    topic_path = os.path.join(TOPIC_INDEX_DIR, f"{topic}.md")

    # 计算本周的周数
    start_date = datetime.strptime(week_range[0], "%Y-%m-%d")
    week_label = get_week_label_from_date(week_range[0])

    # 解析现有索引（获取覆盖周数）
    existing_data = parse_existing_topic_index(topic_path)

    # 更新覆盖周数
    existing_data['covered_weeks'].add(week_label)
    covered_weeks_list = sorted(list(existing_data['covered_weeks']))

    # 提取高价值笔记的id集合
    top_note_ids = set()
    if top_notes:
        top_note_ids = {note['id'] for note in top_notes if note.get('_topic') == topic}

    # 按日期和周分组笔记
    notes_by_week = {}

    for note in all_notes:
        created_at = note.get('created_at', '')[:10]

        # 按周分组
        week_label_note = get_week_label_from_date(created_at)
        if week_label_note not in notes_by_week:
            notes_by_week[week_label_note] = []
        notes_by_week[week_label_note].append(note)

    # 生成索引内容
    covered_weeks_str = ', '.join(covered_weeks_list)
    total_notes = len(all_notes)

    # 计算本周新增笔记数
    week_start = datetime.strptime(week_range[0], "%Y-%m-%d")
    week_end = datetime.strptime(week_range[1], "%Y-%m-%d")
    new_notes_count = sum(1 for note in all_notes
                         if note.get('created_at') and
                            week_start <= datetime.strptime(note.get('created_at', '')[:10], "%Y-%m-%d") <= week_end)

    # 计算该主题下的高价值笔记数
    top_notes_count = sum(1 for note in all_notes if note['id'] in top_note_ids)

    content = f"""---
索引类型: 主题索引
主题: {topic}
最后更新: {datetime.now().strftime("%Y-%m-%d %H:%M")}
覆盖周数: {covered_weeks_str}
总笔记数: {total_notes}
高价值笔记: {top_notes_count}
---

# {topic} - 累积索引

> 本周（{week_range[0]} 至 {week_range[1]}）新增 {new_notes_count} 条笔记
> 总计 {total_notes} 条笔记，覆盖 {len(covered_weeks_list)} 周
> ⭐ {top_notes_count} 条高价值笔记

---

## 📅 按周查看

"""

    for week_label_sorted in sorted(notes_by_week.keys(), reverse=True):
        week_notes = notes_by_week[week_label_sorted]
        content += f"\n### {week_label_sorted} ({len(week_notes)}条)\n\n"

        # 该周内按日期排序
        dates_in_week = {}
        for note in week_notes:
            created_at = note.get('created_at', '')[:10]
            if created_at not in dates_in_week:
                dates_in_week[created_at] = []
            dates_in_week[created_at].append(note)

        for date in sorted(dates_in_week.keys(), reverse=True):
            for note in dates_in_week[date]:
                title = note.get('title', '')
                note_type = note.get('note_type', '')
                note_id = note.get('id', '')

                # 判断是否是高价值笔记
                is_top_note = note_id in top_note_ids
                star_prefix = "⭐ " if is_top_note else ""

                # 提取摘要/亮点
                highlight = extract_note_highlight(note)
                highlight_text = f"\n  > {highlight}" if highlight else ""

                # 只显示非空的笔记类型
                type_display = f" ({note_type})" if note_type else ""
                content += f"- {star_prefix}**{title}**{type_display}{highlight_text}\n"
                # 使用带日期前缀的完整文件名
                filename = generate_filename(title, note.get('created_at', ''))
                content += f"  - 链接: [[{filename}]]\n\n"

    # 添加"相关洞察"区块
    content += "---\n\n## 🧠 相关洞察\n\n"
    content += "> （从Zettelkasten中提取相关卡片）\n\n"

    # 写入文件
    os.makedirs(TOPIC_INDEX_DIR, exist_ok=True)
    with open(topic_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return topic_path




def generate_weekly_index_new(all_notes: List[Dict], week_range: Tuple[str, str], topic_stats: Dict, top_notes: List[Dict], OBSIDIAN_VAULT: str) -> str:
    """
    生成本周索引页（深化推荐理由 + 按主题分类显示所有笔记）

    Args:
        all_notes: 本周所有笔记
        week_range: 本周时间范围
        topic_stats: 各主题的统计信息
        top_notes: Top 5高价值笔记
        OBSIDIAN_VAULT: Obsidian Vault 路径

    Returns:
        本周索引文件路径
    """
    start_date, end_date = week_range
    # 计算周数
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    week_number = start_dt.isocalendar()[1]
    year = start_dt.year
    week_label = f"{year}-W{week_number}"

    # 生成到本周索引/目录
    week_index_dir = os.path.join(OBSIDIAN_VAULT, "02 🗂️ 主题索引", "本周索引")
    os.makedirs(week_index_dir, exist_ok=True)
    week_index_path = os.path.join(week_index_dir, f"{year}年第{week_number}周索引.md")

    # 生成内容
    content = f"""---
索引类型: 本周索引
时间范围: {week_range[0]} 至 {week_range[1]}
创建时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}
总笔记数: {len(all_notes)}
---

# 本周笔记索引 ({week_label})

## 📊 概览

- **总笔记数**: {len(all_notes)} 条
- **覆盖主题**: {len(topic_stats)} 个
- **时间范围**: {week_range[0]} 至 {week_range[1]}

---

## 💎 本周重要笔记 (Top 5)

> 基于多维度加权评分模型筛选：洞察深度(40%) + 信息密度(20%) + 行动相关性(20%) + 新颖性(10%) + 情感强度(10%)

"""

    # 添加Top 5高价值笔记（改进版：AI智能总结 + 入选理由）
    for i, note in enumerate(top_notes, 1):
        title = note.get('title', '')
        note_type = note.get('note_type', '')
        topic = note.get('_topic', '其他')
        score = note.get('_value_score', 0)
        star = note.get('_star', 0)
        evaluation = note.get('_evaluation', {})
        content_path = note.get('relative_path', '')

        # 生成 AI 智能总结（核心观点）
        ai_summary = generate_ai_summary(note)

        # 入选理由（多维度评分）
        selection_reason = generate_selection_reason(note)

        content += f"""### {i}. {title}

**主题**: {topic} | **类型**: {note_type} | **星级**: {'⭐' * star}

**入选理由**: {selection_reason}

**核心观点**:
> {ai_summary if ai_summary else "（见原文）"}

[[{content_path}]]

---
"""

    content += "\n---\n\n## 📅 按日期查看\n\n"

    # 按日期分组
    notes_by_date = {}
    for note in all_notes:
        created_at = note.get('created_at', '')[:10]
        if created_at not in notes_by_date:
            notes_by_date[created_at] = []
        notes_by_date[created_at].append(note)

    for date in sorted(notes_by_date.keys(), reverse=True):
        content += f"### {date}\n\n"
        for note in notes_by_date[date]:
            title = note.get('title', '')
            note_type = note.get('note_type', '')
            topic = note.get('_topic', '其他')
            relative_path = note.get('relative_path', '')
            content += f"- **{title}** ({topic} / {note_type})\n"
            wikilink = clean_wikilink(relative_path)
            content += f"  - 链接: {wikilink}\n\n"

    # 写入文件
    os.makedirs(os.path.dirname(week_index_path), exist_ok=True)
    with open(week_index_path, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f"  ✅ 生成本周索引: {week_index_path}")

    return week_index_path


def generate_deep_recommendation(note: Dict, evaluation: Dict) -> Dict[str, str]:
    """
    生成深度推荐理由（核心洞见 + 为什么重要 + 行动建议）

    Args:
        note: 笔记数据
        evaluation: AI评估结果

    Returns:
        深度推荐理由字典
    """
    reason = evaluation.get('reason', '')
    insight_score = evaluation.get('insight', 0)
    density_score = evaluation.get('density', 0)

    # 1. 提取核心洞见（从reason中提取第一句话或核心观点）
    core_insight = extract_core_insight(reason, note)

    # 2. 为什么重要（基于AI评估的insight深度和density密度）
    if insight_score >= 0.8:
        why_important = f"这条笔记的洞察深度极高（{insight_score:.1f}/10），提供了独特的视角，可能改变你对{note.get('_topic', '相关领域')}的认知。"
    elif density_score >= 0.8:
        why_important = f"这条笔记的信息密度很高（{density_score:.1f}/10），包含大量有价值的信息，值得反复研读和提取洞见。"
    elif insight_score >= 0.6:
        why_important = f"这条笔记提供了有价值的洞见（{insight_score:.1f}/10），对理解{note.get('_topic', '相关领域')}有重要参考意义。"
    else:
        why_important = "这条笔记包含有用的信息和观点，值得保存并后续回顾。"

    # 3. 行动建议（从reason中提取"建议"相关内容）
    action_suggestion = extract_action_suggestion(reason)

    return {
        'core_insight': core_insight,
        'why_important': why_important,
        'action_suggestion': action_suggestion
    }


def select_top5_notes_improved(notes: List[Dict], three_star_notes: List[Dict]) -> List[Dict]:
    """
    改进的Top 5筛选函数：从三星笔记中挑选评分最高的5条

    评分维度：
    - 洞察深度 (40%): 笔记提供的独特视角和认知价值
    - 信息密度 (20%): 单位字数包含的信息量
    - 行动相关性 (20%): 对投资/工作实践的指导意义
    - 新颖性 (10%): 观点的新鲜度和独特性
    - 情感强度 (10%): 是否能引发强烈共鸣

    Args:
        notes: 所有笔记
        three_star_notes: 三星笔记列表（有AI评估）

    Returns:
        Top 5笔记列表
    """
    # 权重配置
    WEIGHTS = {
        'insight': 0.40,      # 洞察深度
        'density': 0.20,      # 信息密度
        'practicality': 0.20, # 行动相关性
        'novelty': 0.10,      # 新颖性
        'emotion': 0.10       # 情感强度
    }

    # 候选笔记池：三星笔记（统一三星体系）
    if three_star_notes:
        candidates = three_star_notes

        if candidates:
            # 为每条笔记计算综合评分
            scored_notes = []
            for note in candidates:
                evaluation = note.get('_evaluation', {})

                # 提取各维度评分
                insight = evaluation.get('insight', 0.5)
                density = evaluation.get('density', 0.5)
                novelty = evaluation.get('novelty', 0.5)
                practicality = evaluation.get('practicality', 0.5)

                # 情感强度：通过内容长度×新颖性估算
                content = note.get('content', '') or note.get('note', {}).get('content', '')
                content_length = len(content)
                if content_length < 500 and novelty >= 0.7:
                    emotion = 0.9
                elif content_length < 1000 and novelty >= 0.6:
                    emotion = 0.7
                elif content_length > 3000:
                    emotion = 0.5
                else:
                    emotion = 0.6

                # 计算加权总分
                total_score = (
                    insight * WEIGHTS['insight'] +
                    density * WEIGHTS['density'] +
                    practicality * WEIGHTS['practicality'] +
                    novelty * WEIGHTS['novelty'] +
                    emotion * WEIGHTS['emotion']
                )

                # 记录各维度评分
                note['_top5_scores'] = {
                    'total': total_score,
                    'insight': insight,
                    'density': density,
                    'practicality': practicality,
                    'novelty': novelty,
                    'emotion': emotion
                }

                scored_notes.append((total_score, note))

            # 按总分排序，取前5
            scored_notes.sort(key=lambda x: x[0], reverse=True)
            top5 = [note for _, note in scored_notes[:5]]

            return top5

    # 【降级方案】如果没有AI评估数据，使用启发式评分
    # 基于：内容长度 + 主题类型 + 标题关键词
    print("  ⚠️ 没有AI评估数据，使用启发式评分筛选Top 5...")

    scored_notes = []
    for note in notes:
        content = note.get('content', '') or ''
        content_length = len(content)
        title = note.get('title', '')
        topic = note.get('_topic', '其他')

        # 启发式评分
        heuristic_score = 0.5

        # 内容长度评分（太长或太短都不好）
        if 500 <= content_length <= 2000:
            heuristic_score += 0.2  # 适中长度
        elif 2000 < content_length <= 5000:
            heuristic_score += 0.1  # 偏长但可读
        elif content_length < 200:
            heuristic_score -= 0.2  # 太短

        # 主题加权（投资/商业相关更重要）
        important_topics = ['投资工作', '商业航天', '人工智能AI', '机器人', '半导体']
        if topic in important_topics:
            heuristic_score += 0.15

        # 标题关键词加权
        important_keywords = ['深度', '分析', '洞察', '思考', '投资', '尽调', '战略', '趋势']
        for kw in important_keywords:
            if kw in title:
                heuristic_score += 0.05
                break

        # 发芽报告加权（个人反思也有价值）
        if '发芽报告' in title or topic == '发芽报告':
            heuristic_score += 0.1

        # 确保分数在0-1范围内
        heuristic_score = max(0, min(1, heuristic_score))

        note['_top5_scores'] = {
            'total': heuristic_score,
            'insight': heuristic_score * 0.9,
            'density': heuristic_score * 0.8,
            'practicality': heuristic_score * 0.7,
            'novelty': heuristic_score * 0.6,
            'emotion': heuristic_score * 0.5,
            'is_heuristic': True
        }

        scored_notes.append((heuristic_score, note))

    # 按总分排序，取前5
    scored_notes.sort(key=lambda x: x[0], reverse=True)
    top5 = [note for _, note in scored_notes[:5]]

    return top5


def generate_selection_reason(note: Dict) -> str:
    """
    生成Top 5入选理由

    Args:
        note: 笔记数据

    Returns:
        入选理由文本
    """
    scores = note.get('_top5_scores', {})
    total = scores.get('total', 0)
    is_heuristic = scores.get('is_heuristic', False)

    # 如果是启发式评分，生成简化理由
    if is_heuristic:
        topic = note.get('_topic', '其他')
        title = note.get('title', '')
        content_length = len(note.get('content', '') or '')

        reasons = [f"综合评分 {total:.2f}（启发式评估）"]

        # 基于主题
        if topic in ['投资工作', '商业航天', '人工智能AI', '机器人', '半导体']:
            reasons.append(f"主题\"{topic}\"具有投资研究价值")
        elif topic == '发芽报告':
            reasons.append("个人成长反思，记录认知迭代")
        elif topic == '其他':
            reasons.append("内容有一定参考价值")

        # 基于内容长度
        if 500 <= content_length <= 2000:
            reasons.append(f"内容长度适中（{content_length}字），可读性强")

        return "；".join(reasons)

    # AI评估分数理由
    insight = scores.get('insight', 0)
    density = scores.get('density', 0)
    practicality = scores.get('practicality', 0)
    novelty = scores.get('novelty', 0)
    emotion = scores.get('emotion', 0)

    reasons = []

    # 基于最高维度生成理由
    max_dim = max([(insight, '洞察深度极高，提供独特认知视角'),
                   (density, '信息密度高，内容精炼有价值'),
                   (practicality, '实践指导性强，可直接落地'),
                   (novelty, '观点新颖独特，突破常规认知'),
                   (emotion, '引发强烈共鸣，情感冲击力强')],
                  key=lambda x: x[0])

    reasons.append(f"综合评分 {total:.2f}，主要亮点：{max_dim[1]}")

    # 如果某个维度特别高，补充说明
    if insight >= 0.85:
        reasons.append(f"洞察深度 {insight:.1f}/10，对{note.get('_topic', '相关领域')}有独到见解")
    if practicality >= 0.8:
        reasons.append(f"实践价值 {practicality:.1f}/10，可直接指导行动")
    if density >= 0.8:
        reasons.append(f"信息密度 {density:.1f}/10，字字千金")

    return "；".join(reasons)


def add_related_links(all_notes: List[Dict], topic_mapping: Dict[str, List[str]], force_update: bool = False, current_week: int = None) -> int:
    """
    为所有笔记添加"相关链接"区块

    Args:
        all_notes: 所有笔记数据
        topic_mapping: 主题到笔记列表的映射
        force_update: 是否强制更新已存在的"相关链接"区块
        current_week: 当前周数（用于生成本周索引链接）

    Returns:
        更新的笔记数量
    """
    updated_count = 0

    for note in all_notes:
        note_topic = note.get('_topic', '')
        title = note.get('title', '')
        created_at = note.get('created_at', '')
        # 使用带日期前缀的完整文件名
        filename = generate_filename(title, created_at) + ".md"
        # 笔记放在子目录中（投资工作/、AI学习/等）
        note_path = os.path.join(INBOX_DIR, note_topic, filename)

        if not os.path.exists(note_path):
            continue

        # 读取现有内容
        with open(note_path, 'r', encoding='utf-8') as f:
            existing_content = f.read()

        # 检查是否已经有"相关链接"区块（如果强制更新，则忽略此检查）
        if '## 相关链接' in existing_content and not force_update:
            continue

        # 收集相关链接
        related_links = []

        # 同主题的其他笔记（最多5条，按时间倒序取最新的）
        if note_topic in topic_mapping:
            # 按创建时间倒序排序
            sorted_notes = sorted(topic_mapping[note_topic], key=lambda x: x.get('created_at', ''), reverse=True)
            # 取前5条（排除自己）
            for other_note in sorted_notes[:5]:
                # 使用历史笔记保存的文件名（如果有的话）
                if 'filename' in other_note:
                    other_filename = other_note['filename']
                else:
                    # 否则重新生成
                    other_title = other_note.get('title', '')
                    other_created_at = other_note.get('created_at', '')
                    other_filename = generate_filename(other_title, other_created_at) + ".md"

                # 排除自己（使用文件名比较）
                if other_filename != filename:
                    # 提取文件名（不含.md）
                    other_filename_without_ext = other_filename.replace('.md', '')
                    other_title = other_note.get('title', '')
                    related_links.append(f"- [[{other_filename_without_ext}|{other_title}]] (同主题)")

        # 主题索引链接
        topic_index_path = f"02 🗂️ 主题索引/主题/{note_topic}.md"
        if os.path.exists(os.path.join(OBSIDIAN_VAULT, topic_index_path)):
            related_links.append(f"- [{note_topic}主题]({topic_index_path})")

        # 本周索引链接
        week_index_path = f"02 🗂️ 主题索引/本周索引/2026年第{current_week}周索引.md"
        if os.path.exists(os.path.join(OBSIDIAN_VAULT, week_index_path)):
            related_links.append(f"- [本周索引]({week_index_path})")

        # 同主题的洞察卡片
        zettelkasten_dir = ZETTELKASTEN_DIR
        if os.path.exists(zettelkasten_dir):
            for card_file in os.listdir(zettelkasten_dir):
                if card_file.startswith(note_topic):
                    card_title = card_file.replace('.md', '')
                    related_links.append(f"- [[/{zettelkasten_dir}/{card_file}|{card_title}]] (洞察卡片)")

        # 添加"相关链接"区块
        if related_links:
            links_block = "\n\n## 相关链接\n\n" + '\n'.join(related_links) + "\n"

            # 追加到文件末尾
            with open(note_path, 'a', encoding='utf-8') as f:
                f.write(links_block)

            # 记录到变更日志
            CHANGE_LOG['updated_files'].append(note_path)

            updated_count += 1

    return updated_count


def step1_topic_clustering(notes: List[Dict]) -> List[Dict]:
    """
    步骤1: 主题聚类（使用关键词匹配，暂时禁用AI）

    逻辑：
    1. 使用关键词匹配快速分类
    2. 支持多主题归属
    """
    print("📌 步骤1: 主题聚类（使用关键词匹配）")

    for note in notes:
        # 获取笔记标题和类型（兼容两种数据格式）
        # 格式1: note.note.title (嵌套结构)
        # 格式2: note.title (扁平结构，Get笔记API直接返回)
        if 'note' in note and isinstance(note['note'], dict):
            note_data = note['note']
        else:
            note_data = note
        
        title = note_data.get('title', '')
        note_type = note_data.get('note_type', 'unknown')

        # 使用关键词匹配分类
        topic = classify_note(title, note_type)
        note['_topics'] = [topic]
        note['_topic'] = topic

    # 统计各主题数量
    topic_counts = {}
    for note in notes:
        topic = note['_topic']
        topic_counts[topic] = topic_counts.get(topic, 0) + 1

    print(f"  主题分布: {topic_counts}")
    return notes


def step2_value_filtering(notes: List[Dict], top_n: int = 5) -> Tuple[List[Dict], Dict]:
    """
    步骤2: AI分层价值评估（三层策略）- 统一三星体系

    第1层：价值类型判断（基于语义，不看形式）
    - 高优先级（完整AI评估）：
      * 发芽报告（标题标记）
      * 个人思考（含原创思考关键词：思考/我认为/为什么/反思等）
      * 项目决策（涉及具体项目+投资/决策/尽调等关键词）
    - 中优先级（AI提取洞见）：
      * 优质长内容链接（来自小宇宙/公众号/得到等）
      * 较长录音（>1000字，无深度思考信号）
    - 低优先级（跳过AI）：
      * 碎片化记录、纯转载、重复性内容

    第2层：AI智能调用（30-60秒）
    - 高优先级：完整AI分析（价值评估 + 卡片关键词）
    - 中优先级：AI提取洞见（不生成卡片）

    第3层：分层输出（统一三星体系）
    - 三星（3星）：总分>=0.85 → 生成卡片 + 洞见雷达
    - 二星（2星）：总分>=0.75 → 生成卡片 + 洞见雷达
    - 一星（1星）：总分>=0.60 → 仅洞见雷达
    - 不推荐（0星）：<0.60 → 跳过
    """
    print(f"📌 步骤2: AI分层价值评估 - 共 {len(notes)} 条")

    all_evaluations = []

    # 第1层：价值类型判断（基于语义，不看形式）
    high_priority_notes = []
    medium_priority_notes = []
    low_priority_notes = []

    # 关键词库
    ORIGINALITY_KEYWORDS = [
        '思考', '我认为', '为什么', '反思', '我觉得', '发现', '体会', '感悟',
        '洞察', '意识到', '突然想到', '新认知', '打破', '颠覆', '逆向思维',
        '对比', '论证', '推演', '验证', '复盘'
    ]
    DECISION_KEYWORDS = ['投资', '决策', '项目', '尽调', '估值', '退出', 'LP', 'GP']
    HIGH_QUALITY_SOURCES = ['小宇宙', '公众号', '得到', '36氪', '财新', '虎嗅', '晚点']

    for note in notes:
        # 获取笔记数据（兼容两种格式）
        note_data = note.get('note', note)
        note_type = note_data.get('note_type', 'unknown')
        title = note_data.get('title', '')
        content = note_data.get('content', '')
        content_lower = content.lower()

        # 判断逻辑（按优先级顺序）
        is_sprout_report = '发芽报告' in title
        has_original_thinking = any(kw in content for kw in ORIGINALITY_KEYWORDS)
        has_project_decision = any(kw in content for kw in DECISION_KEYWORDS) and ('项目' in content or '基金' in content)
        is_high_quality_link = note_type == 'link' and any(src in content for src in HIGH_QUALITY_SOURCES)
        is_long_recording = note_type.startswith('recorder') and len(content) > 1000

        # 高优先级：发芽报告 / 个人思考 / 项目决策
        if is_sprout_report or has_original_thinking or has_project_decision:
            note['_analysis_type'] = 'card_and_radar'
            note['_priority'] = 'high'
            note['_should_generate_card'] = True
            note['_priority_reason'] = '发芽报告' if is_sprout_report else ('个人思考' if has_original_thinking else '项目决策')
            high_priority_notes.append(note)
        # 中优先级：优质长内容链接 / 较长录音（无深度思考信号）
        elif is_high_quality_link or is_long_recording:
            note['_analysis_type'] = 'radar_only'
            note['_priority'] = 'medium'
            note['_should_generate_card'] = False
            note['_priority_reason'] = '优质链接' if is_high_quality_link else '录音记录'
            medium_priority_notes.append(note)
        # 低优先级：其他
        else:
            note['_analysis_type'] = 'skip'
            note['_priority'] = 'low'
            note['_should_generate_card'] = False
            note['_priority_reason'] = '碎片化/低价值'
            low_priority_notes.append(note)

    print(f"  优先级分布: 高={len(high_priority_notes)} 中={len(medium_priority_notes)} 低={len(low_priority_notes)}")
    if high_priority_notes:
        reasons = {}
        for n in high_priority_notes:
            r = n.get('_priority_reason', '其他')
            reasons[r] = reasons.get(r, 0) + 1
        print(f"    高优先级构成: {reasons}")

    # 第2层：AI智能调用

    # 2.1 先分析高优先级笔记（链接笔记、发芽报告）
    if high_priority_notes:
        print(f"\n  🔄 分析高优先级笔记（{len(high_priority_notes)}条）...")
        for i, note in enumerate(high_priority_notes, 1):
            title_short = note.get('title', '无标题')[:35]
            print(f"    [{i}/{len(high_priority_notes)}] {title_short}...", end='', flush=True)

            try:
                # 调用AI进行完整价值评估
                evaluation = ai_evaluate_single_note(note)

                # 保存评估结果（统一三星体系）
                note['_evaluation'] = evaluation
                note['_star'] = evaluation.get('star', 1)
                note['_value_score'] = evaluation.get('total_score', 0.0)

                star = evaluation.get('star', 1)
                score = evaluation.get('total_score', 0.0)
                star_label = '⭐' * star if star > 0 else '❌'
                print(f" → [{star_label}] 评分:{score:.2f}", flush=True)

                all_evaluations.append(evaluation)

            except Exception as e:
                print(f" ⚠️ 失败: {str(e)[:30]}", flush=True)
                # 降级方案：使用默认二星
                fallback_eval = {
                    'note_id': note.get('id', ''),
                    'note': note,
                    'density': 0.75,
                    'insight': 0.75,
                    'novelty': 0.75,
                    'practicality': 0.75,
                    'total_score': 0.75,
                    'star': 2,
                    'reason': f'AI评估异常: {str(e)[:100]}',
                    'should_generate_card': True,
                    'card_keywords': [note.get('_topic', '其他')]
                }
                note['_evaluation'] = fallback_eval
                note['_star'] = 2
                note['_value_score'] = 0.75
                all_evaluations.append(fallback_eval)

    # 2.2 再分析中等优先级笔记（录音笔记）
    if medium_priority_notes:
        print(f"\n  🔄 分析中等优先级笔记（{len(medium_priority_notes)}条）...")
        for i, note in enumerate(medium_priority_notes, 1):
            title_short = note.get('title', '无标题')[:35]
            print(f"    [{i}/{len(medium_priority_notes)}] {title_short}...", end='', flush=True)

            try:
                # 调用AI提取洞见（不生成卡片）
                evaluation = ai_evaluate_radar_only(note)

                # 保存评估结果（统一三星体系）
                note['_evaluation'] = evaluation
                note['_star'] = evaluation.get('star', 1)
                note['_value_score'] = evaluation.get('total_score', 0.0)

                star = evaluation.get('star', 1)
                score = evaluation.get('total_score', 0.0)
                star_label = '⭐' * star if star > 0 else '❌'
                print(f" → [{star_label}] 评分:{score:.2f}", flush=True)

                all_evaluations.append(evaluation)

            except Exception as e:
                print(f" ⚠️ 失败: {str(e)[:30]}", flush=True)
                # 降级方案：使用默认一星
                fallback_eval = {
                    'note_id': note.get('id', ''),
                    'note': note,
                    'density': 0.60,
                    'insight': 0.60,
                    'novelty': 0.60,
                    'practicality': 0.60,
                    'total_score': 0.60,
                    'star': 1,
                    'reason': f'AI评估异常: {str(e)[:100]}',
                    'should_generate_card': False,
                    'card_keywords': [note.get('_topic', '其他')]
                }
                note['_evaluation'] = fallback_eval
                note['_star'] = 1
                note['_value_score'] = 0.60
                all_evaluations.append(fallback_eval)

    # 2.3 低优先级笔记：直接标记0星（不推荐）
    if low_priority_notes:
        print(f"\n  📝 低优先级笔记（{len(low_priority_notes)}条）: 跳过AI，标记0星")
        for note in low_priority_notes:
            fallback_eval = {
                'note_id': note.get('id', ''),
                'note': note,
                'density': 0.30,
                'insight': 0.30,
                'novelty': 0.30,
                'practicality': 0.30,
                'total_score': 0.30,
                'star': 0,
                'reason': '低优先级: 内容长度短或类型不重要',
                'should_generate_card': False,
                'card_keywords': [note.get('_topic', '其他')]
            }
            note['_evaluation'] = fallback_eval
            note['_star'] = 0
            note['_value_score'] = 0.30
            all_evaluations.append(fallback_eval)

    # 保存所有评估结果到JSON
    save_all_evaluations(all_evaluations)

    # 统计各星级数量
    star_counts = {}
    for note in notes:
        star = note.get('_star', 0)
        star_counts[star] = star_counts.get(star, 0) + 1

    print(f"\n  星级分布: {star_counts}")

    # 第3层：分层输出（统一三星体系）
    three_star_notes = [n for n in notes if n.get('_star') == 3]
    two_star_notes = [n for n in notes if n.get('_star') == 2]
    one_star_notes = [n for n in notes if n.get('_star') == 1]
    zero_star_notes = [n for n in notes if n.get('_star', 0) == 0]

    # 按评分排序
    three_star_notes.sort(key=lambda n: n.get('_value_score', 0), reverse=True)
    two_star_notes.sort(key=lambda n: n.get('_value_score', 0), reverse=True)

    # 返回所有笔记 + 分类结果（用数字key兼容旧代码）
    return notes, {
        '3': three_star_notes,
        '2': two_star_notes,
        '1': one_star_notes,
        '0': zero_star_notes,
        'evaluations': all_evaluations
    }


def step3_insight_cards(notes: List[Dict]) -> List[str]:
    """
    步骤5.5: 生成洞察卡片（基于三星/二星笔记，统一使用三星体系）

    逻辑：
    1. 只处理星级为三星（_star=3）或二星（_star=2）的笔记
    2. 提取AI评估时的关键词（card_keywords）
    3. 生成/更新卡片（使用关键词映射表 + AI兜底）

    Returns:
        本次运行新建的卡片文件名列表（用于MOC索引更新）
    """
    print("📌 步骤5.5: 生成洞察卡片（基于三星/二星笔记）")

    card_files = []
    new_card_files = []  # 只包含本次新建的卡片
    processed_count = 0
    skipped_count = 0

    for note in notes:
        # 获取星级（由generate_insight_radar_new赋值）
        star = note.get('_star', 0)
        evaluation = note.get('_evaluation', {})

        # 只处理三星和二星
        if star < 2:
            print(f"  ⏭️  跳过 [{star}星]: {note.get('title', '无标题')[:40]}...")
            skipped_count += 1
            continue

        # 不再跳过"其他"主题，改为标记
        if note.get('_topic', '') == '其他':
            print(f"  ⚠️  处理 [其他主题]: {note.get('title', '无标题')[:40]}...")

        processed_count += 1

        # 使用AI评估时提取的关键词
        card_keywords = evaluation.get('card_keywords', [])

        # 提取洞察
        insights = extract_insights(note)

        # 如果没有核心段落，尝试从笔记内容中提取
        if not insights['core_paragraphs'] and not insights['golden_sentences']:
            # 降级：从笔记内容中提取前200字作为核心洞察
            note_content = note.get('content', '')
            if note_content and len(note_content) > 50:
                insights['core_paragraphs'] = [note_content[:200]]
                print(f"  ⚠️  降级提取 [无洞察]: {note.get('title', '无标题')[:40]}...")
            else:
                print(f"  ⚠️  跳过 [无洞察]: {note.get('title', '无标题')[:40]}...")
                skipped_count += 1
                continue

        # 获取主题
        topic = note['_topic']

        # 判断是新建还是追加（在生成卡片之前检查）
        card_file = generate_zettelkasten_card(note, insights, topic, card_keywords)
        card_path = os.path.join(ZETTELKASTEN_DIR, card_file)

        # 检查是否是新建（通过检查卡片创建时间）
        is_new_card = True
        if os.path.exists(card_path):
            # 文件已存在，读取并检查创建时间
            try:
                import stat
                stat_info = os.stat(card_path)
                # 获取文件修改时间
                mtime = stat_info.st_mtime
                from datetime import datetime
                file_time = datetime.fromtimestamp(mtime)
                now = datetime.now()
                # 如果文件在最近1分钟内创建，认为是本次运行新建的
                if (now - file_time).total_seconds() < 60:
                    is_new_card = True
                else:
                    is_new_card = False
            except:
                # 如果获取时间失败，默认认为是追加
                is_new_card = False
        else:
            # 文件不存在（理论上不会发生，因为generate_zettelkasten_card应该已创建）
            is_new_card = True

        # 添加到对应的列表
        card_files.append(card_file)

        star_label = '⭐' * star if star > 0 else '❌'
        if is_new_card:
            print(f"  ✨ 新建卡片 [{star_label}]: {card_file}")
            new_card_files.append(card_file)
            CHANGE_LOG['created_cards'].append({
                'filename': card_file,
                'note_title': note.get('title', '无标题')
            })
        else:
            print(f"  ✏️  追加到卡片 [{star_label}]: {card_file}")
            CHANGE_LOG['updated_cards'].append({
                'filename': card_file,
                'note_title': note.get('title', '无标题')
            })

    print(f"  共处理 {processed_count} 张洞察卡片（新建{len(new_card_files)}张，追加{len(card_files) - len(new_card_files)}张）")
    print(f"  跳过 {skipped_count} 条笔记")

    # 返回所有处理的卡片（用于MOC索引更新），而不是只返回新建的
    return card_files


def load_all_historical_notes() -> List[Dict]:
    """
    加载所有历史笔记（从Obsidian Inbox扫描）

    Returns:
        所有历史笔记列表
    """
    print(f"  📂 从Obsidian Inbox加载历史笔记...")

    all_notes = []

    # 遍历Inbox下的所有子目录
    for root, dirs, files in os.walk(INBOX_DIR):
        # 跳过隐藏目录
        dirs[:] = [d for d in dirs if not d.startswith('.')]

        for filename in files:
            if not filename.endswith('.md'):
                continue

            filepath = os.path.join(root, filename)

            # 读取文件
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()

                # 提取笔记ID（从frontmatter中）
                note_id = None
                filename_without_ext = filename.replace('.md', '')
                # 从文件名中提取真实标题（去掉日期前缀）
                # 格式：YYYYMMDD-标题 或 YYYYMMDD-标题.md
                if '-' in filename_without_ext:
                    parts = filename_without_ext.split('-', 1)
                    if len(parts[0]) == 8 and parts[0].isdigit():
                        # 有日期前缀，去掉它
                        title = parts[1]
                    else:
                        title = filename_without_ext
                else:
                    title = filename_without_ext
                created_at = ''

                # 获取子目录名（可能是主题，也可能是其他）
                subfolder = os.path.basename(os.path.dirname(filepath))

                # 将子目录名映射到主题（支持新增的文件夹）
                topic_mapping = {
                    'AI学习': 'AI学习',
                    '人工智能AI': '人工智能AI',
                    '投资工作': '投资工作',
                    '商业航天': '商业航天',
                    '党建工作': '党建工作',
                    '发芽报告': '发芽报告',       # 新增
                    '创投骆哥': '创投骆哥',     # 新增
                    '其他': '其他',
                    '链接笔记': '其他',
                    '录音笔记': '其他',
                }
                topic = topic_mapping.get(subfolder, '其他')

                # 解析frontmatter
                lines = content.split('\n')
                in_frontmatter = False
                for line in lines:
                    if line.strip() == '---':
                        in_frontmatter = not in_frontmatter
                        continue
                    if in_frontmatter:
                        if line.startswith('笔记ID:'):
                            note_id = line.split('笔记ID:')[1].strip()
                        elif line.startswith('创建时间:'):
                            created_at = line.split('创建时间:')[1].strip()

                # 如果没有找到笔记ID，使用文件名作为fallback
                if not note_id:
                    # 尝试从文件名中提取日期（格式：YYYYMMDD-标题）
                    if '-' in filename:
                        date_part = filename.split('-')[0]
                        if len(date_part) == 8 and date_part.isdigit():
                            # 格式化日期
                            try:
                                year = date_part[0:4]
                                month = date_part[4:6]
                                day = date_part[6:8]
                                created_at = f"{year}-{month}-{day} 00:00:00"
                            except:
                                pass

                all_notes.append({
                    'id': note_id or filename,
                    'title': title,
                    'created_at': created_at,
                    '_topic': topic,
                    'content': content,
                    'filepath': filepath,
                    'filename': filename  # 直接保存文件名
                })

            except Exception as e:
                print(f"  ⚠️  无法读取文件 {filename}: {e}")
                continue

    print(f"  ✅ 加载了 {len(all_notes)} 条历史笔记")
    return all_notes


def step4_topic_indexes(current_week_notes: List[Dict], week_range: Tuple[str, str], top_notes: List[Dict] = None) -> Dict[str, str]:
    """
    步骤4: 生成动态主题索引（按周拆分，每个主题每周一个独立文件）

    Args:
        current_week_notes: 本周笔记（用于统计，主题从Inbox历史笔记获取）
        week_range: 本周时间范围
        top_notes: 本周高价值笔记列表
    """
    print("📌 步骤4: 生成动态主题索引（按周拆分）")

    # 从Inbox加载历史笔记，获取正确的主题信息（基于文件夹结构）
    historical_notes = load_all_historical_notes()

    if not historical_notes:
        print("  ⚠️  无法加载历史笔记，跳过主题索引生成")
        return {}

    # 计算本周的周标签
    start_date = datetime.strptime(week_range[0], "%Y-%m-%d")
    week_number = start_date.isocalendar()[1]
    year = start_date.year
    current_week_label = f"{year}-W{week_number}"

    # 只保留本周的笔记（基于created_at筛选）
    week_start = datetime.strptime(week_range[0], "%Y-%m-%d")
    week_end = datetime.strptime(week_range[1], "%Y-%m-%d")
    
    this_week_notes = []
    for note in historical_notes:
        created_at_str = note.get('created_at', '')
        if created_at_str:
            try:
                created_date = datetime.strptime(created_at_str[:10], "%Y-%m-%d")
                if week_start <= created_date <= week_end:
                    this_week_notes.append(note)
            except:
                pass

    if not this_week_notes:
        print("  ⚠️  本周没有笔记，跳过主题索引生成")
        return {}

    print(f"  📂 本周有 {len(this_week_notes)} 条笔记，生成主题索引...")

    # 按主题分组（使用历史笔记中的主题信息）
    topic_notes = {}
    for note in this_week_notes:
        topic = note.get('_topic', '其他')
        if topic not in topic_notes:
            topic_notes[topic] = []
        topic_notes[topic].append(note)

    # 打印主题分布（用于调试）
    for t, count in sorted(topic_notes.items(), key=lambda x: -len(x[1])):
        print(f"    {t}: {count}条")

    # 为每个主题生成索引（过滤掉不应该生成索引的主题）
    excluded_topics = {'00 📒 Inbox（From_Get笔记）', '其他'}
    topic_files = {}
    for topic, notes_list in topic_notes.items():
        if topic in excluded_topics:
            continue  # 跳过特殊主题

        # 确保主题索引目录存在
        os.makedirs(TOPIC_INDEX_DIR, exist_ok=True)

        # 生成按周的主题索引文件
        topic_path = generate_topic_index_by_week(topic, notes_list, week_range, current_week_label, top_notes)
        topic_files[topic] = topic_path
        print(f"  ✅ 生成主题索引: {topic}-{current_week_label} ({len(notes_list)}条笔记)")

        # 记录到变更日志
        CHANGE_LOG['created_files'].append(topic_path)

    print(f"  📊 共生成 {len(topic_files)} 个主题索引（{current_week_label}）")

    return topic_files


def step5_weekly_index(notes: List[Dict], week_range: Tuple[str, str], top_notes: List[Dict] = None) -> str:
    """
    步骤5: 生成本周索引
    """
    print("📌 步骤5: 生成本周索引")

    # 统计主题分布
    topic_stats = {}
    for note in notes:
        topic = note.get('_topic', '其他')
        topic_stats[topic] = topic_stats.get(topic, 0) + 1

    # 生成本周索引
    week_index_path = generate_weekly_index_new(notes, week_range, topic_stats, top_notes or [], OBSIDIAN_VAULT)
    print(f"  生成本周索引: {week_index_path}")

    # 记录到变更日志
    CHANGE_LOG['created_files'].append(week_index_path)

    return week_index_path


def generate_insight_radar_new(notes: List[Dict], star_notes: Dict[str, List[Dict]], week_range: Tuple[str, str], OBSIDIAN_VAULT: str) -> str:
    """
    生成洞见雷达（使用已计算的星级）

    Args:
        notes: 本周所有笔记
        star_notes: 分类后的笔记 {'3': [...], '2': [...], '1': [...], '0': [...]}
        week_range: 本周时间范围
        OBSIDIAN_VAULT: Obsidian Vault 路径

    Returns:
        洞见雷达文件路径
    """
    import os
    import re

    start_date, end_date = week_range
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    week_number = start_dt.isocalendar()[1]
    year = start_dt.year
    week_label = f"{year}-W{week_number}"

    # 生成到本周洞见/目录
    radar_dir = os.path.join(OBSIDIAN_VAULT, "02 🗂️ 主题索引", "本周洞见")
    os.makedirs(radar_dir, exist_ok=True)
    radar_path = os.path.join(radar_dir, f"{year}-W{week_number}-洞见雷达.md")

    # 直接使用已计算的星级（由step2_value_filtering赋值）
    three_star_notes = star_notes.get('3', [])
    two_star_notes = star_notes.get('2', [])
    one_star_notes = star_notes.get('1', [])

    # 按评分排序
    three_star_notes.sort(key=lambda n: n.get('_value_score', 0), reverse=True)
    two_star_notes.sort(key=lambda n: n.get('_value_score', 0), reverse=True)
    one_star_notes.sort(key=lambda n: n.get('_value_score', 0), reverse=True)

    # 生成内容
    content = f"""---
索引类型: 洞见雷达
时间范围: {week_range[0]} 至 {week_range[1]}
创建时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}
---

# {week_label} - 洞见雷达

> 本周共提取 {len(three_star_notes) + len(two_star_notes) + len(one_star_notes)} 个高密度洞见段落
> 三星推荐 {len(three_star_notes)} 个, 二星精选 {len(two_star_notes)} 个, 一星亮点 {len(one_star_notes)} 个

---

## ⭐ 三星推荐（最高优先级）

> 必读！本周最有价值的洞察

"""

    # 添加三星推荐
    for i, note in enumerate(three_star_notes, 1):
        title = note.get('title', '')
        evaluation = note.get('_evaluation', {})
        density = evaluation.get('density', 0) * 10
        insight_score = evaluation.get('insight', 0) * 10
        practicality = evaluation.get('practicality', 0) * 10
        score = note.get('_star_score', 0)
        reason = evaluation.get('reason', '')
        relative_path = note.get('relative_path', '')

        # 提取行动线索
        action_suggestion = extract_action_suggestion(reason)

        # 提取完整段落（核心内容）
        full_insight = extract_full_insight(note, evaluation)

        content += f"""### {i}. {title} ⭐⭐⭐
- **来源**: {clean_wikilink(relative_path)}
- **评分**: 信息密度 {density:.1f}/10 | 洞察深度 {insight_score:.1f}/10 | 行动线索 {practicality:.1f}/10
- **行动线索**: {action_suggestion}
- **完整洞见**: {full_insight}

---
"""

    # 添加二星精选
    if two_star_notes:
        content += """## ⭐⭐ 二星精选

> 高价值内容，值得精读

"""
        for i, note in enumerate(two_star_notes, 1):
            title = note.get('title', '')
            evaluation = note.get('_evaluation', {})
            density = evaluation.get('density', 0) * 10
            insight_score = evaluation.get('insight', 0) * 10
            practicality = evaluation.get('practicality', 0) * 10
            score = note.get('_star_score', 0)
            reason = evaluation.get('reason', '')
            relative_path = note.get('relative_path', '')

            action_suggestion = extract_action_suggestion(reason)
            full_insight = extract_full_insight(note, evaluation)

            content += f"""### {i}. {title} ⭐⭐
- **来源**: {clean_wikilink(relative_path)}
- **评分**: 信息密度 {density:.1f}/10 | 洞察深度 {insight_score:.1f}/10 | 行动线索 {practicality:.1f}/10
- **行动线索**: {action_suggestion}
- **完整洞见**: {full_insight}

---
"""

    # 添加一星亮点
    if one_star_notes:
        content += """## ⭐ 一星亮点

> 有启发的内容，值得快速浏览

"""
        for i, note in enumerate(one_star_notes, 1):
            title = note.get('title', '')
            evaluation = note.get('_evaluation', {})
            density = evaluation.get('density', 0) * 10
            insight_score = evaluation.get('insight', 0) * 10
            practicality = evaluation.get('practicality', 0) * 10
            score = note.get('_star_score', 0)
            reason = evaluation.get('reason', '')
            relative_path = note.get('relative_path', '')

            action_suggestion = extract_action_suggestion(reason)
            full_insight = extract_full_insight(note, evaluation)

            content += f"""### {i}. {title} ⭐
- **来源**: {clean_wikilink(relative_path)}
- **评分**: 信息密度 {density:.1f}/10 | 洞察深度 {insight_score:.1f}/10 | 行动线索 {practicality:.1f}/10
- **行动线索**: {action_suggestion}
- **完整洞见**: {full_insight}

---
"""

    # 写入文件
    with open(radar_path, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f"  ✅ 生成洞见雷达: {radar_path}")
    print(f"     三星推荐: {len(three_star_notes)} 个 | 二星精选: {len(two_star_notes)} 个 | 一星亮点: {len(one_star_notes)} 个")

    return radar_path

def print_change_report(week_label: str):
    """
    打印完整的变更报告

    Args:
        week_label: 周标签（如"本周"、"2周前"）
    """
    print("\n" + "="*60)
    print(f"📋 完整变更报告（{week_label}）")
    print("="*60 + "\n")

    # 1. 导入的笔记
    if CHANGE_LOG['imported_notes']:
        print("## 📥 导入的笔记")
        for i, note in enumerate(CHANGE_LOG['imported_notes'], 1):
            print(f"{i}. {note['title']}")
            print(f"   - 主题: {note['topic']}")
            print(f"   - 路径: {note['filepath']}")
        print()

    # 2. 创建的洞察卡片
    if CHANGE_LOG['created_cards']:
        print("## 💎 新创建的洞察卡片")
        for i, card in enumerate(CHANGE_LOG['created_cards'], 1):
            print(f"{i}. {card['filename']}")
            print(f"   - 来源笔记: {card['note_title']}")
            print(f"   - 路径: 06 🧠 Zettelkasten (卡片盒 - 核心洞察)/{card['filename']}")
        print()

    # 3. 更新的洞察卡片
    if CHANGE_LOG['updated_cards']:
        print("## ✏️  追加内容的洞察卡片")
        for i, card in enumerate(CHANGE_LOG['updated_cards'], 1):
            print(f"{i}. {card['filename']}")
            print(f"   - 追加内容: 来自 {card['note_title']}")
            print(f"   - 路径: 06 🧠 Zettelkasten (卡片盒 - 核心洞察)/{card['filename']}")
        print()

    # 4. 创建的索引文件
    if CHANGE_LOG['created_files']:
        print("## 📄 创建的索引文件")
        for i, filepath in enumerate(CHANGE_LOG['created_files'], 1):
            print(f"{i}. {filepath}")
        print()

    # 5. 更新的笔记文件（添加链接）
    if CHANGE_LOG['updated_files']:
        print("## 🔗 添加了双向链接的笔记")
        print(f"总计: {len(CHANGE_LOG['updated_files'])} 条笔记\n")
        for i, filepath in enumerate(CHANGE_LOG['updated_files'][:20], 1):  # 只显示前20条
            print(f"{i}. {filepath}")
        if len(CHANGE_LOG['updated_files']) > 20:
            print(f"... 还有 {len(CHANGE_LOG['updated_files']) - 20} 条笔记")
        print()

    print("="*60)
    print(f"✅ 变更报告完成")
    print("="*60 + "\n")


def step6_related_links(current_week_notes: List[Dict], all_notes: List[Dict], week_offset: int = 0, use_qmd: bool = True) -> int:
    """
    步骤6: 添加双向链接（支持 qmd 智能关联推荐）

    Args:
        current_week_notes: 本周的笔记（需要更新它们的链接）
        all_notes: 所有历史笔记（用于生成链接）
        week_offset: 周偏移量（用于计算当前周数）
        use_qmd: 是否使用qmd进行关联推荐
    """
    print("📌 步骤6: 添加双向链接")

    # 计算当前周数
    today = datetime.now()
    first_day = datetime(today.year, 1, 1)
    days = (today - timedelta(days=week_offset * 7) - first_day).days
    current_week = (days // 7) + 1 - week_offset

    # 按主题分组（使用所有历史笔记）
    topic_mapping = {}
    for note in all_notes:
        topic = note.get('_topic', '其他')
        if topic not in topic_mapping:
            topic_mapping[topic] = []
        topic_mapping[topic].append(note)

    # 添加相关链接（只更新本周的笔记）
    updated_count = add_related_links(current_week_notes, topic_mapping, current_week=current_week)
    
    # 使用 qmd 进行智能关联推荐（如果启用）
    if use_qmd and QMD_AVAILABLE and not args.skip_qmd:
        print("  🔍 使用 qmd 进行智能关联推荐...")
        try:
            # 更新 qmd 索引（确保包含新导入的笔记）
            qmd_update_index()
            
            # 为每张卡片添加 qmd 推荐的相关笔记
            qmd_enhanced_count = add_qmd_related_links(current_week_notes)
            print(f"  qmd 增强: 为 {qmd_enhanced_count} 条笔记添加了智能关联")
        except Exception as e:
            print(f"  ⚠️ qmd 关联推荐失败: {e}")

    print(f"  为 {updated_count} 条笔记添加了相关链接")

    return updated_count


def add_qmd_related_links(notes: List[Dict], top_k: int = 3) -> int:
    """
    使用 qmd 为笔记添加智能关联链接
    
    Args:
        notes: 笔记列表
        top_k: 每个笔记推荐的相关笔记数量
        
    Returns:
        成功添加关联的笔记数量
    """
    enhanced_count = 0
    
    for note in notes:
        try:
            # 查找相关笔记
            related = find_related_notes(note, top_k=top_k)
            
            if not related:
                continue
            
            # 获取笔记文件路径
            note_path = note.get('_file_path', '')
            if not note_path or not os.path.exists(note_path):
                continue
            
            # 读取现有内容
            with open(note_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 构建 qmd 推荐链接区块
            qmd_links_section = "\n\n## 🔗 qmd 智能推荐\n\n基于内容相似度推荐:\n"
            for r in related:
                file_path = r.get('file', '')
                score = r.get('score', 0)
                snippet = r.get('snippet', '')[:50]
                
                # 提取文件名作为链接文本
                file_name = os.path.basename(file_path).replace('.md', '')
                qmd_links_section += f"- [[{file_name}]] (相似度: {score:.2f})\n"
            
            # 检查是否已有 qmd 推荐区块
            if "## 🔗 qmd 智能推荐" in content:
                # 替换现有区块
                content = re.sub(
                    r'## 🔗 qmd 智能推荐.*?\n(?=## |$)',
                    qmd_links_section + "\n",
                    content,
                    flags=re.DOTALL
                )
            else:
                # 在文件末尾添加
                content = content.rstrip() + qmd_links_section
            
            # 写回文件
            with open(note_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            enhanced_count += 1
            
        except Exception as e:
            print(f"    ⚠️ 处理笔记失败: {e}")
            continue
    
    return enhanced_count


def run_full_sop(notes: List[Dict], top_n: int = 5, week_offset: int = 0, custom_week_range: Tuple = None) -> Dict:
    """
    执行完整的6步SOP流程

    Args:
        notes: 所有笔记数据
        top_n: 提取前N条精华
        week_offset: 周偏移量 (0=本周, 1=上一周, 2=两周前)
        custom_week_range: 自定义日期范围元组 (start_date, end_date)，优先于week_offset

    Returns:
        执行结果汇总
    """
    print(f"\n{'='*60}")
    print(f"🚀 开始执行智能导入SOP")
    print(f"{'='*60}\n")

    # 优先使用自定义日期范围
    if custom_week_range:
        week_range = custom_week_range
        week_label = "自定义范围"
    else:
        week_range = get_week_range(week_offset)
        week_label = "本周" if week_offset == 0 else f"{week_offset}周前"
    print(f"📅 {week_label}时间范围: {week_range[0]} 至 {week_range[1]}\n")

    result = {
        'week_range': week_range,
        'week_offset': week_offset,
        'total_notes': len(notes),
        'steps': {}
    }

    # 根据周次范围筛选笔记
    from datetime import datetime
    start_dt = datetime.strptime(week_range[0], "%Y-%m-%d")
    end_dt = datetime.strptime(week_range[1], "%Y-%m-%d") + timedelta(days=1)  # 包含结束日当天

    filtered_notes = []
    for note in notes:
        created_at = note.get('created_at', '')
        if created_at:
            note_dt = datetime.strptime(created_at.split()[0], "%Y-%m-%d")
            if start_dt <= note_dt < end_dt:
                filtered_notes.append(note)

    print(f"📂 筛选结果: 从 {len(notes)} 条笔记中筛选出 {len(filtered_notes)} 条符合时间范围的笔记\n")

    # 使用筛选后的笔记执行后续步骤
    notes = filtered_notes

    # 保存本周笔记数据到JSON（供洞见雷达使用）
    save_week_notes_to_json(notes, week_range)

    # 步骤0.5: 将笔记导入到Obsidian Inbox
    imported_count = import_notes_to_inbox(notes)
    result['steps']['import_to_inbox'] = f'完成，导入{imported_count}条笔记'

    # 步骤0.6: 链接预处理 (NEW - summarize)
    if SUMMARIZE_AVAILABLE and not args.skip_summarize:
        print(f"\n📎 步骤0.6: 链接预处理 (summarize)...")
        notes = batch_preprocess_links(notes)
        result['steps']['link_preprocessing'] = '完成'
    else:
        if not SUMMARIZE_AVAILABLE:
            result['steps']['link_preprocessing'] = '跳过 (模块未安装)'
        else:
            result['steps']['link_preprocessing'] = '跳过 (用户指定)'

    # 步骤1: 主题聚类
    notes = step1_topic_clustering(notes)
    result['steps']['topic_clustering'] = '完成'

    # 步骤2: 价值筛选（统一三星体系）
    notes, star_notes = step2_value_filtering(notes)
    three_star_notes = star_notes['3']
    two_star_notes = star_notes['2']
    one_star_notes = star_notes['1']
    zero_star_notes = star_notes['0']

    # Top 5：从三星笔记中挑选评分最高的5条
    top_notes = select_top5_notes_improved(notes, three_star_notes)

    result['steps']['value_filtering'] = f'完成，三星{len(three_star_notes)}条，二星{len(two_star_notes)}条，一星{len(one_star_notes)}条，Top 5{len(top_notes)}条'

    # 步骤3: 本周索引（提前：用户优先看本周概览）
    week_index = step5_weekly_index(notes, week_range, top_notes)
    result['steps']['weekly_index'] = f'完成，{week_index}'

    # [已删除] 步骤4: 主题索引 - 用户要求删除，维护成本高，已有本周索引
    # topic_files = step4_topic_indexes(notes, week_range, top_notes)
    # result['steps']['topic_indexes'] = f'完成，生成{len(topic_files)}个主题索引'

    # 步骤5: 洞见雷达（使用已计算的星级）
    radar_path = generate_insight_radar_new(notes, star_notes, week_range, OBSIDIAN_VAULT)
    result['steps']['insight_radar'] = f'完成，{radar_path}'

    # 步骤5.5: 洞察卡片（基于三星/二星笔记，雷达计算后才有_star字段）
    # 从所有笔记中筛选三星和二星（_star字段由generate_insight_radar_new赋值）
    three_and_two_star_notes = [n for n in notes if n.get('_star', 0) >= 2]
    card_files = step3_insight_cards(three_and_two_star_notes)
    result['steps']['insight_cards'] = f'完成，生成{len(card_files)}张卡片（三星+二星笔记）'

    # 步骤3.5: 更新MOC索引（知识卡片主索引）
    update_moc_index(card_files, week_range, week_label)

    # 步骤6: 双向链接（链接到索引和卡片）
    all_notes = load_all_historical_notes()
    updated_count = step6_related_links(notes, all_notes, week_offset, use_qmd=not args.skip_qmd)
    result['steps']['related_links'] = f'完成，更新{updated_count}条笔记'

    print(f"\n{'='*60}")
    print(f"✅ SOP执行完成")
    print(f"{'='*60}\n")

    # 打印完整的变更报告
    print_change_report(week_label)

    return result


def update_moc_index(card_files: List[str], week_range: Tuple[str, str], week_label: str):
    """
    更新知识卡片主索引（MOC）和各个主题MOC文件

    Args:
        card_files: 本周处理的卡片文件名列表（包括新建和追加的）
        week_range: 本周时间范围
        week_label: 周标签
    """
    if not card_files:
        print("  ⏭️  没有生成新卡片，跳过MOC索引更新")
        return

    # MOC 索引目录
    moc_dir = "/Users/luoyingchuansha/Library/Mobile Documents/iCloud~md~obsidian/Documents/创投骆哥/02 🗂️ 主题索引/知识卡片索引"
    moc_main_file = os.path.join(moc_dir, "知识卡片索引.md")

    if not os.path.exists(moc_main_file):
        print(f"  ⚠️  MOC主索引文件不存在: {moc_main_file}")
        return

    # 读取现有 MOC 索引
    with open(moc_main_file, 'r', encoding='utf-8') as f:
        moc_content = f.read()

    # 提取本周生成的新卡片信息
    new_cards = []
    for card_filename in card_files:
        card_path = os.path.join(ZETTELKASTEN_DIR, card_filename)
        if os.path.exists(card_path):
            with open(card_path, 'r', encoding='utf-8') as f:
                card_content = f.read()

            # 提取主题标签
            topic = "其他"
            topic_match = re.search(r'主题标签:\s*\n\s*-\s*(.+)', card_content)
            if topic_match:
                topic = topic_match.group(1).strip()

            new_cards.append({
                'filename': card_filename,
                'topic': topic
            })

    if not new_cards:
        print("  ⏭️  没有有效的新卡片，跳过MOC索引更新")
        return

    # 按主题分组
    cards_by_topic = {}
    for card in new_cards:
        topic = card['topic']
        if topic not in cards_by_topic:
            cards_by_topic[topic] = []
        cards_by_topic[topic].append(card)

    # 主题标签到MOC文件的映射
    topic_to_moc_file = {
        '人工智能AI': 'MOC - 人工智能AI与人机协作.md',
        '商业航天': 'MOC - 商业航天.md',
        '人生哲学与投资智慧': 'MOC - 人生哲学与投资智慧.md',
    }

    # 更新各个主题的MOC文件
    updated_topic_mocs = 0
    for topic, cards in cards_by_topic.items():
        # 查找对应的MOC文件
        moc_filename = topic_to_moc_file.get(topic)
        if not moc_filename:
            print(f"  ⚠️  主题【{topic}】没有对应的MOC文件，跳过")
            continue

        moc_file_path = os.path.join(moc_dir, moc_filename)
        if not os.path.exists(moc_file_path):
            print(f"  ⚠️  MOC文件不存在: {moc_file_path}")
            continue

        # 读取MOC文件
        with open(moc_file_path, 'r', encoding='utf-8') as f:
            moc_file_content = f.read()

        # 查找"## 卡片持仓列表"部分
        cards_section_start = moc_file_content.find('## 卡片持仓列表')
        if cards_section_start == -1:
            print(f"  ⚠️  MOC文件【{moc_filename}】中没有找到'卡片持仓列表'部分")
            continue

        # 查找下一个"##"的位置（卡片持仓列表的结束位置）
        next_section_start = moc_file_content.find('\n## ', cards_section_start + 1)
        if next_section_start == -1:
            # 没有下一个"##"，插入到文件末尾
            insert_pos = len(moc_file_content)
        else:
            # 在下一个"##"之前插入
            insert_pos = next_section_start

        # 提取卡片持仓列表部分的内容
        cards_section = moc_file_content[cards_section_start:insert_pos]

        # 追加新卡片链接
        updated = False
        for card in cards:
            # 检查卡片是否已在列表中
            if card['filename'] not in cards_section:
                # 提取卡片的核心洞察
                card_path = os.path.join(ZETTELKASTEN_DIR, card['filename'])
                if os.path.exists(card_path):
                    with open(card_path, 'r', encoding='utf-8') as f:
                        card_content = f.read()

                    # 提取核心洞察（【核心洞察】或【金句/证据】部分）
                    core_insight = ""
                    insight_match = re.search(r'## 【核心洞察】\n(.*?)(?=##|$)', card_content, re.DOTALL)
                    if insight_match:
                        core_insight = insight_match.group(1).strip()
                        # 取前200字
                        if len(core_insight) > 200:
                            core_insight = core_insight[:200] + "..."

                    if not core_insight:
                        # 尝试提取金句
                        golden_match = re.search(r'## 【金句/证据】\n(.*?)(?=##|$)', card_content, re.DOTALL)
                        if golden_match:
                            core_insight = golden_match.group(1).strip()
                            # 取前200字
                            if len(core_insight) > 200:
                                core_insight = core_insight[:200] + "..."

                # 计算卡片编号
                existing_cards = re.findall(r'^\d+\.\s+\[\[', cards_section, re.MULTILINE)
                new_card_num = len(existing_cards) + 1

                # 构建新卡片条目
                new_card_entry = f"\n{new_card_num}. [[{card['filename']}]]\n"
                if core_insight:
                    new_card_entry += f"   - 核心：{core_insight}\n"
                new_card_entry += f"   - 来源：{week_label}\n"

                # 追加到卡片持仓列表
                moc_file_content = moc_file_content[:insert_pos] + new_card_entry + moc_file_content[insert_pos:]
                updated = True
                # 更新insert_pos，因为已经追加了内容
                insert_pos += len(new_card_entry)

        if updated:
            # 更新frontmatter中的卡片数量
            frontmatter_end = moc_file_content.find('---', 1)
            if frontmatter_end != -1:
                frontmatter = moc_file_content[:frontmatter_end]
                # 提取现有卡片数量
                count_match = re.search(r'卡片数量:\s*(\d+)', frontmatter)
                if count_match:
                    old_count = int(count_match.group(1))
                    new_count = old_count + len(cards)
                    frontmatter = re.sub(
                        r'卡片数量:\s*\d+',
                        f'卡片数量: {new_count}',
                        frontmatter
                    )
                    moc_file_content = frontmatter + moc_file_content[frontmatter_end:]

            # 写回文件
            with open(moc_file_path, 'w', encoding='utf-8') as f:
                f.write(moc_file_content)
            updated_topic_mocs += 1
            print(f"  ✅ 已更新MOC文件: {moc_filename}，添加{len(cards)}张卡片")

    # 更新 MOC 主索引
    updated = False

    for topic, cards in cards_by_topic.items():
        # 检查该主题是否在主索引中
        topic_section_pattern = rf'### {re.escape(topic)}'
        if re.search(topic_section_pattern, moc_content):
            # 找到主题区块，追加新卡片
            # 查找该主题区块的结束位置
            pattern = rf'(### {re.escape(topic)}.*?)(\n### [^#]|\n## [^#])'
            match = re.search(pattern, moc_content, re.DOTALL)

            if match:
                topic_content = match.group(1)
                next_section = match.group(2)

                # 添加新卡片链接
                for card in cards:
                    card_link = f"- [[{card['filename']}]] （{week_label}）\n"

                    # 检查是否已存在
                    if card['filename'] not in topic_content:
                        topic_content += card_link
                        updated = True

                # 替换主题区块
                moc_content = moc_content.replace(match.group(1), topic_content)
        else:
            # 主题不存在，需要创建新主题区块
            # 找到"卡片持仓列表"部分的末尾（在"关联索引"之前）
            # 查找"## 关联索引"的位置
            related_index_pos = moc_content.find('\n## 关联索引')
            if related_index_pos == -1:
                # 如果没有"关联索引"，查找"---"分隔线
                related_index_pos = moc_content.find('\n---\n\n## 关联索引')
                if related_index_pos == -1:
                    # 都找不到，插入到文档中最后一个"###"主题之后、第一个"## "之前
                    insert_pos = moc_content.rfind('\n### ')
                    if insert_pos != -1:
                        # 找到下一个"## "的位置
                        next_section_pos = moc_content.find('\n## ', insert_pos + 1)
                        related_index_pos = next_section_pos
                    else:
                        # 找不到合适位置，插入到文档末尾
                        related_index_pos = len(moc_content)

            if related_index_pos != -1:
                # 构建"### {topic}\n\n"格式的新主题区块
                new_section = f"### {topic}\n\n"

                for card in cards:
                    new_section += f"- [[{card['filename']}]] （{week_label}）\n"

                new_section += "\n"

                # 插入到"关联索引"之前
                moc_content = moc_content[:related_index_pos] + new_section + moc_content[related_index_pos:]
                updated = True
            else:
                print(f"  ⚠️  无法为新主题找到合适的插入位置: {topic}")

    # 更新文件
    if updated:
        with open(moc_main_file, 'w', encoding='utf-8') as f:
            f.write(moc_content)
        print(f"  ✅ 已更新MOC主索引，添加{len(new_cards)}张卡片")
    elif updated_topic_mocs > 0:
        print(f"  ✅ 已更新{updated_topic_mocs}个主题MOC文件")
    else:
        print(f"  ⏭️  所有卡片已存在于MOC索引中，无需更新")


if __name__ == '__main__':
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='Get笔记到Obsidian智能导入SOP v5.0')
    parser.add_argument('--week', type=int, default=0,
                        help='周偏移量 (0=本周, 1=上一周, 2=两周前)')
    parser.add_argument('--notes-file', type=str, default="notes_with_details.json",
                        help='笔记JSON文件路径')
    parser.add_argument('--skip-fetch', action='store_true',
                        help='跳过从API获取数据，直接使用本地文件')
    parser.add_argument('--skip-summarize', action='store_true',
                        help='跳过链接预处理（summarize）')
    parser.add_argument('--skip-qmd', action='store_true',
                        help='跳过qmd卡片去重')

    args = parser.parse_args()

    # 显示导入的周次信息
    week_range = get_week_range(args.week)
    week_label = "本周" if args.week == 0 else f"{args.week}周前"
    print(f"\n{'='*60}")
    print(f"📅 导入{week_label}笔记: {week_range[0]} 至 {week_range[1]}")
    print(f"{'='*60}\n")

    # 步骤0：从Get笔记API获取数据（除非指定--skip-fetch）
    notes_file = os.path.join("/Users/luoyingchuansha/WorkBuddy/20260320162931", args.notes_file)

    if not args.skip_fetch:
        print(f"📡 步骤0: 从Get笔记API获取数据...")
        import subprocess
        fetch_script = os.path.join("/Users/luoyingchuansha/WorkBuddy/20260320162931", "fetch_notes_from_api.py")
        result = subprocess.run(
            ["python3", fetch_script, "--week", str(args.week), "--output", args.notes_file],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print(f"❌ 从API获取数据失败:")
            print(result.stderr)
            import sys
            sys.exit(1)
        else:
            print(f"✅ API数据获取成功\n")
            print(result.stdout)

    # 从JSON文件加载笔记数据
    if os.path.exists(notes_file):
        print(f"📂 从本地文件加载笔记: {notes_file}")
        with open(notes_file, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)

        # 提取notes字段（get_week_notes.py生成的JSON结构是 {week_range, notes, total}）
        if isinstance(raw_data, dict) and 'notes' in raw_data:
            notes = raw_data['notes']
            # 如果JSON文件包含week_range，使用它而不是计算
            if 'week_range' in raw_data and raw_data['week_range']:
                week_range = raw_data['week_range']
                args.week = -1  # 标记为使用自定义日期范围
                print(f"  📅 检测到笔记日期范围: {week_range[0]} 至 {week_range[1]}")
        else:
            # 兼容旧格式：列表格式，每个元素有note字段
            notes = [item['note'] for item in raw_data if 'note' in item]

        # 【修复】如果JSON文件为空，回退到从Inbox文件夹加载
        if len(notes) == 0:
            print(f"  ⚠️ JSON文件为空，回退到从Obsidian Inbox加载...")
            notes = load_all_historical_notes()
            print(f"  ✅ 从Inbox加载了 {len(notes)} 条历史笔记")

        # 执行完整SOP（传入周偏移量和自定义日期范围）
        custom_week_range = None
        if 'week_range' in raw_data and raw_data['week_range']:
            custom_week_range = tuple(raw_data['week_range'])
        result = run_full_sop(notes, top_n=5, week_offset=args.week, custom_week_range=custom_week_range)

        print("\n📊 执行结果:")
        for step, status in result['steps'].items():
            print(f"  - {step}: {status}")
    else:
        print(f"❌ 文件不存在: {notes_file}")
        print("请先运行 fetch_notes_from_api.py 获取笔记数据")
