from openai import OpenAI
from app.core.config import settings
import json

client = OpenAI(
    api_key=settings.ZHIPU_API_KEY,
    base_url=settings.ZHIPU_BASE_URL,
)

# 中国移动设计院能力说明
CMCC_DESIGN_INSTITUTE_CAPABILITIES = """
中国移动通信集团设计院有限公司核心能力：

一、CT 通信能力
- 无线：2/4/5G、WLAN、卫星通信、应急通信规划设计，国内领先
- 核心网：2/4/5G 核心网融合、To B 专网架构设计
- 有线：光传输、光缆、海缆、网管系统全流程咨询设计

二、IT 数智能力
- 云计算：移动 IT 云 / 公有云 / 网络云三朵云全量规划设计
- 大数据：数据中台、经营分析平台规划建设
- 数据网络：IP 网、云专网、云网协同设计
- 信息安全：反诈、骚扰电话、垃圾信息、安全态势感知系统设计
- 算力网络：算网顶层规划、数字设计、一体化集成、边缘云网、可视化运维、安全服务

三、全流程业务服务能力
- 咨询规划：数智化转型咨询、战略/网络/供应链规划、可研评估、专利支撑
- 工程建设：通信/建筑/机电工程设计、监理、总承包、系统集成
- 网络运维：运维工作台、网管系统、网信安全平台、深度覆盖提升
- 网络优化：传统网优、网优工具平台、新型网络研究
- 信息能源：绿色数据中心、信息能源解决方案

四、自研产品与硬件能力
- 边缘云网一体柜：排级/箱级，适配室内外多场景，即插即用，覆盖智慧矿山/园区/IDC扩容等
- 硬件工具：5G 小基站、光缆检测、信号测试、应急通信保障、软评防火墙日志采集
- 软件产品：网管、网信安全、网优平台、工程建设数字化产品

五、专业评估服务能力
- 软件开发工作量评估：基于国标功能点法，覆盖需求/立项/采购/实施/结算/审计全周期，用于预算、财评、造价管控

六、集成交付能力
- 算力网络集成：硬件/软件/安全/可视化运维全集成，服务网络云、智算中心、政务云
- 端到端交付：方案设计—施工督导—调测验收—售后支撑一站式服务
"""

def analyze_bidding(title: str, content: str) -> dict:
    """
    使用智谱 AI (GLM-4-Flash) 分析标讯内容
    """
    prompt = f"""请分析以下招投标信息，判断是否属于中国移动设计院可以参与的商机。

{CMCC_DESIGN_INSTITUTE_CAPABILITIES}

【招标信息】
标题: {title}
内容摘要: {content[:10000]}... (已截断)

【分析要求】
请基于中国移动设计院的核心能力，分析本公告中设计院可能参与的部分，返回 JSON 格式结果：

{{
    "score": 相关性评分 (0-100, 整数，越高表示越匹配设计院能力),
    "category": "业务分类 (如: 智算/算力网络/核心网/承载网/数据中心/云计算/5G/信息化/系统集成/咨询规划/其他)",
    "budget": "预算金额 (从文中提取具体金额，如 '5,460,000.00元'，没有则填 '未知')",
    "deadline": "截止日期 (从文中提取日期，如 '2026年03月03日'，没有则填 '未知')",
    "qualifications": "资质要求 (简要提取关键资质要求)",
    "summary": "项目简报 (一句话概括项目内容、预算、关键要求)",
    "opportunity_analysis": "商机分析 (基于设计院能力，分析本项目有哪些切入点，设计院可以参与哪些环节，建议的服务方案)"
}}

【输出要求】
1. 仅返回 JSON 对象，不要包含 markdown 代码块标记
2. opportunity_analysis 要具体，说明设计院哪些能力可以匹配本项目
3. 评分要客观，高评分项目应该是设计院明显有能力优势的项目"""

    try:
        response = client.chat.completions.create(
            model=settings.AI_MODEL_NAME, 
            messages=[
                {"role": "system", "content": "你是中国移动设计院的商机分析专家。请基于设计院的核心能力，精准分析招标公告中的商机，输出结构化的 JSON 数据。只返回 JSON，不要其他文字。"},
                {"role": "user", "content": prompt},
            ],
            stream=False
        )
        result_content = response.choices[0].message.content
        
        # 清理可能存在的 markdown 代码块标记
        result_content = result_content.replace("```json", "").replace("```", "").strip()
        
        # 尝试提取 JSON 部分
        import re
        json_match = re.search(r'\{.*\}', result_content, re.DOTALL)
        if json_match:
            result_content = json_match.group(0)
        
        result = json.loads(result_content)
        
        # 确保所有必要字段存在
        required_fields = ["score", "category", "budget", "deadline", "qualifications", "summary", "opportunity_analysis"]
        for field in required_fields:
            if field not in result:
                result[field] = "未知" if field != "score" else 0
        
        return result
        
    except Exception as e:
        print(f"AI Analysis Error: {e}")
        return {
            "score": 0,
            "category": "Error",
            "summary": "AI 分析失败",
            "budget": "未知",
            "deadline": "未知",
            "qualifications": "未知",
            "opportunity_analysis": "分析失败，请人工查看"
        }
