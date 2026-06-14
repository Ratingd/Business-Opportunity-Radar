import asyncio
from sqlalchemy.orm import Session
from app.models import models
from app.services.ai_service import analyze_bidding
from datetime import datetime
import time
from playwright.sync_api import sync_playwright
import re
import queue

# 全局日志队列（线程安全，供 SSE 实时推送到前端）
log_queue = queue.Queue(maxsize=2000)

def push_log(text: str, level: str = 'info'):
    """将日志写入队列并同时输出到 stdout"""
    import time as _time
    import builtins
    msg = {
        'type': 'log',
        'level': level,   # info / success / warning / error / crawl
        'text': text,
        'ts': _time.strftime('%H:%M:%S')
    }
    builtins.print(text, flush=True)
    try:
        log_queue.put_nowait(msg)
    except queue.Full:
        try:
            log_queue.get_nowait() # 丢弃最旧的
            log_queue.put_nowait(msg)
        except queue.Empty:
            pass

# 重写 print，使当前文件的所有 print 自动进入 push_log
import builtins
def print(*args, **kwargs):
    # 如果 kwargs 里有 flush 等，我们只取内容
    text = " ".join(str(arg) for arg in args)
    # 简单的分级逻辑
    level = 'info'
    if 'Error' in text or 'error' in text or 'Failed' in text or 'failed' in text:
        level = 'error'
    elif 'Success' in text or 'success' in text:
        level = 'success'
    push_log(text, level)


# 默认关键词（当未传入关键词时使用）
DEFAULT_KEYWORDS = ["智算", "算力网络", "核心网", "业务网", "数据网", "承载网", "骨干网", "IP网", "5G", "服务器", "集成", "算力", "IDC", "通信工程", "勘察设计", "工程监理", "DICT", "数智化", "信息化", "智慧校园", "智慧医疗", "公安视频", "数字政府", "云影像", "云平台", "网络安全", "绿色能源", "信息能源", "规划", "项目咨询", "运维服务", "专网", "造价评估", "方案审核", "通信网络", "机房建设", "数据中心", "弱电工程", "安防监控", "智慧园区", "智能化改造", "系统运维", "通信设计", "可行性研究"]

# 当前使用的关键词（会被传入的关键词覆盖）
KEYWORDS = DEFAULT_KEYWORDS.copy()

# 中国移动招标网目标单位筛选
CMCC_TARGET_COMPANIES = ["广东", "广西", "海南", "设计院", "互联网公司"]

# 中国电信阳光采购网目标省份
CHINATELECOM_TARGET_PROVINCE = "广东"

# 中国联通招标网目标省份
UNICOM_TARGET_PROVINCE = "广东"

# 广东省公共资源交易平台 - 无需额外筛选，本身就是广东省数据
GDZY_TARGET_PROVINCE = "广东"

# 海南省公共资源交易服务平台 - 无需额外筛选，本身就是海南省数据
HAINAN_TARGET_PROVINCE = "海南"

def clean_html(html_content):
    """
    简单清理 HTML 标签，保留纯文本，用于 AI 分析
    """
    if not html_content:
        return ""
    cleaned = re.sub(r'<(style|script)[^>]*>.*?</\1>', '', html_content, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', '\n', cleaned)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def process_bidding(db: Session, title: str, content: str, url: str, publish_date: datetime = None, notice_type: str = "采购公告", source_website: str = "广东省政府采购网"):
    """
    处理抓取到的数据：AI分析 -> 过滤 -> 存库
    """
    # 1. 查重
    existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
    if existing:
        if not existing.raw_html or not existing.meta_info or len(existing.raw_html or "") < 200:
             print(f"Updating existing record details (Missing or Incomplete): {title}", flush=True)
             pass 
        else:
            print(f"Skipping existing: {title}", flush=True)
            return existing

    # 2. AI 分析
    print(f"Analyzing: {title}...", flush=True)
    text_content = clean_html(content)
    analysis = analyze_bidding(title, text_content[:10000])
    
    # 记录命中的关键词
    analysis['matched_keywords'] = [kw for kw in KEYWORDS if kw in title]
    
    score = analysis.get('score', 0)
    print(f"Score: {score}", flush=True)

    if score < 0: 
        print("Filtered out (Low score)", flush=True)
        return None 

    if existing:
        existing.raw_html = content
        existing.meta_info = analysis
        existing.ai_score = score
        existing.content_abstract = analysis.get('summary', '')[:500]
        existing.category = analysis.get('category', '未分类')
        existing.notice_type = notice_type 
        existing.source_website = source_website
        existing.opportunity_analysis = analysis.get('opportunity_analysis', '')[:2000]
        if publish_date:
             existing.publish_date = publish_date
             
        db.commit()
        db.refresh(existing)
        print(f"Updated: {existing.title}", flush=True)
        return existing
    else:
        new_bidding = models.Bidding(
            title=title,
            source_url=url,
            publish_date=publish_date or datetime.now(),
            content_abstract=analysis.get('summary', '')[:500],
            category=analysis.get('category', '未分类'),
            notice_type=notice_type,
            source_website=source_website,
            ai_score=score,
            raw_html=content,
            meta_info=analysis,
            opportunity_analysis=analysis.get('opportunity_analysis', '')[:2000]
        )
        db.add(new_bidding)
        db.commit()
        db.refresh(new_bidding)
        print(f"Saved: {new_bidding.title}", flush=True)
        return new_bidding

def crawl_guangdong(db: Session, context):
    """
    广东省政府采购网爬虫
    """
    print("\n=== Starting Guangdong Crawler ===", flush=True)
    base_url = "https://gdgpo.czt.gd.gov.cn/maincms-web/noticeInformationGd"
    page = context.new_page()
    
    try:
        print(f"Fetching list page: {base_url}...", flush=True)
        captured_items = []

        def handle_response(response):
            if "application/json" in response.headers.get("content-type", "") and "selectInfoForIndex" in response.url:
                try:
                    json_data = response.json()
                    data_list = []
                    if isinstance(json_data.get('data'), dict):
                            data_list = json_data.get('data').get('rows', [])
                    elif isinstance(json_data.get('data'), list):
                            data_list = json_data.get('data')
                    
                    if data_list and isinstance(data_list, list):
                        print(f"DEBUG: Found {len(data_list)} items in API: {response.url}", flush=True)
                        for item in data_list:
                            title = item.get('title') or item.get('noticeTitle') or item.get('subject') or item.get('name')
                            link = item.get('url') or item.get('link') or item.get('pageurl')
                            pub_time_str = item.get('publishTime') or item.get('noticeTime') or item.get('addtime')
                            pub_date = None
                            if pub_time_str:
                                try:
                                    if isinstance(pub_time_str, int): 
                                        pub_date = datetime.fromtimestamp(pub_time_str / 1000)
                                    else:
                                        if len(pub_time_str) >= 19:
                                                pub_date = datetime.strptime(pub_time_str[:19], "%Y-%m-%d %H:%M:%S")
                                        elif len(pub_time_str) >= 10:
                                                pub_date = datetime.strptime(pub_time_str[:10], "%Y-%m-%d")
                                except:
                                    pass
                            
                            if not link and item.get('id'):
                                if item.get('pageurl'):
                                    link = f"https://gdgpo.czt.gd.gov.cn{item.get('pageurl')}"
                                else:
                                    link = f"https://gdgpo.czt.gd.gov.cn/maincms-web/noticeGd?id={item.get('id')}"
                            
                            if title:
                                captured_items.append({
                                    "title": title, 
                                    "href": link or "https://gdgpo.czt.gd.gov.cn",
                                    "publish_date": pub_date
                                })
                except Exception as e:
                    if "Target page, context or browser has been closed" not in str(e):
                        print(f"Error parsing API response: {e}", flush=True)

        page.on("response", handle_response)
        page.goto(base_url, timeout=60000)
        
        notice_types = [
            {"name": "采购公告", "type": "采购公告"},
            {"name": "采购意向公开", "type": "采购意向公开"}
        ]
        
        for n_type in notice_types:
            type_name = n_type["name"]
            db_type = n_type["type"]
            push_log(f"\n--- 开始爬取类型: {type_name} ---", 'crawl')
            
            captured_items.clear()
            
            push_log(f"选择筛选条件: {type_name}", 'info')
            try:
                page.wait_for_selector(f"text={type_name}", timeout=10000)
                page.click(f"text={type_name}")
                time.sleep(1) 
            except Exception as e:
                push_log(f"选择筛选失败 '{type_name}': {e}", 'error')
                continue

            push_log("点击查询按钮...", 'info')
            try:
                page.wait_for_selector("text=查询", timeout=10000)
                with page.expect_response(lambda response: "selectInfoForIndex" in response.url and response.status == 200, timeout=15000) as response_info:
                    page.click("text=查询")
                push_log("第1页 API响应成功", 'info')
            except Exception as e:
                push_log(f"单击查询失败: {e}", 'error')

            time.sleep(1)
            push_log(f"第1页获取到 {len(captured_items)} 条公告", 'info')
            
            # 翻页获取更多数据
            for page_num in range(2, 4):  # 再爬2页
                try:
                    # 点击下一页
                    next_btn = page.query_selector('.btn-next:not(.is-disabled), .el-pagination .btn-next:not(.is-disabled)')
                    if next_btn:
                        next_btn.click()
                        time.sleep(3)
                        push_log(f"第{page_num}页获取到 {len(captured_items)} 条（累计）", 'info')
                    else:
                        push_log(f"没有更多分页，共{page_num-1}页", 'info')
                        break
                except Exception as e:
                    push_log(f"翻页失败: {e}", 'warning')
                    break
            
            push_log(f"翻页完成，{type_name} 共获取 {len(captured_items)} 条公告", 'info')
            
            if len(captured_items) == 0:
                    push_log("API截取无数据，尝试DOM解析...", 'warning')
                    time.sleep(3) 
                    try:
                        rows = page.locator("tr").all()
                        push_log(f"DOM中发现 {len(rows)} 行", 'info')
                        for row in rows:
                            row_text = row.inner_text()
                            if "标题" in row_text and "发布时间" in row_text:
                                continue
                            links = row.locator("a").all()
                            for link in links:
                                title = link.inner_text().strip()
                                href = link.get_attribute("href")
                                if title and href:
                                    if not href.startswith("http"):
                                        if href.startswith("/"):
                                            href = f"https://gdgpo.czt.gd.gov.cn{href}"
                                        else:
                                            href = f"https://gdgpo.czt.gd.gov.cn/maincms-web/{href}"
                                    if len(title) > 5:
                                        captured_items.append({"title": title, "href": href})
                                        break
                    except Exception as e:
                        push_log(f"DOM解析失败: {e}", 'error')

            # 统计匹配数
            matched_titles = [item['title'] for item in captured_items if any(kw in item['title'] for kw in KEYWORDS)]
            push_log(f"广东省采购 - {type_name}: 共{len(captured_items)}条公告，匹配关键词 {len(matched_titles)} 条", 'success' if matched_titles else 'warning')
            for mt in matched_titles:
                push_log(f"  命中: {mt[:60]}", 'success')

            for item in captured_items:
                title = item['title']
                url = item['href']
                pub_date = item.get('publish_date')
                
                if title and ("下载" in title or "指南" in title or "登录" in title):
                    continue

                if any(kw in title for kw in KEYWORDS):
                    existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                    if existing:
                        if not existing.raw_html or not existing.meta_info or len(existing.raw_html or "") < 200:
                                push_log(f"重新爬取不完整记录: {title[:50]}", 'info')
                        else:
                                if existing.notice_type != db_type:
                                    existing.notice_type = db_type
                                    db.commit()
                                    push_log(f"更新公告类型: {title[:50]}", 'info')
                                push_log(f"已存在，跳过: {title[:50]}", 'info')
                                continue

                    try:
                        detail_page = context.new_page()
                        detail_content = {"text": ""}
                        def handle_detail_response(response):
                            if "application/json" in response.headers.get("content-type", ""):
                                try:
                                    if any(key in response.url for key in ["selectInfoByOpenTenderCode", "getInfoById", "getNoticeDetail"]):
                                        data = response.json()
                                        content_found = None
                                        stack = [data]
                                        while stack:
                                            current = stack.pop()
                                            if isinstance(current, dict):
                                                if 'content' in current and isinstance(current['content'], str) and len(current['content']) > 100:
                                                    content_found = current['content']
                                                    break 
                                                for k, v in current.items():
                                                    if isinstance(v, (dict, list)):
                                                        stack.append(v)
                                            elif isinstance(current, list):
                                                for item in current:
                                                    if isinstance(item, (dict, list)):
                                                        stack.append(item)
                                        if content_found:
                                            detail_content["text"] = content_found
                                except:
                                    pass

                        detail_page.on("response", handle_detail_response)
                        detail_page.goto(url, timeout=30000)
                        try:
                                detail_page.wait_for_load_state("networkidle", timeout=15000)
                        except:
                                pass
                        
                        content = detail_content["text"]
                        if not content:
                            print("API interception failed, trying DOM fallback...", flush=True)
                            detail_page.wait_for_timeout(3000) 
                            if detail_content["text"]:
                                    content = detail_content["text"]
                            else:
                                    content = detail_page.evaluate("""() => {
                                    const table = document.querySelector('table');
                                    if (table) return table.outerHTML;
                                    const contentDiv = document.querySelector('.content') || 
                                                        document.querySelector('.article') || 
                                                        document.querySelector('.notice-content') ||
                                                        document.querySelector('.noticeDetail') || 
                                                        document.querySelector('#app') || 
                                                        document.body;
                                    return contentDiv.innerHTML; 
                                }""")
                        
                        process_bidding(db, title, content, url, pub_date, notice_type=db_type, source_website="广东省政府采购网") 
                        detail_page.close()
                    except Exception as e:
                        print(f"Error processing detail {url}: {e}", flush=True)
                        try:
                            if 'detail_page' in locals():
                                detail_page.close()
                        except:
                            pass
                else:
                    title_safe = title.encode('gbk', 'ignore').decode('gbk') if title else ""
                    print(f"Skipped (No keyword match): {title_safe}", flush=True)
    except Exception as e:
        print(f"Guangdong Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_guangxi(db: Session, context):
    """
    广西政府采购网爬虫 - 通过页面访问获取数据
    """
    print("\n=== Starting Guangxi Crawler ===", flush=True)
    
    # 广西政府采购网主页
    base_url = "https://zfcg.gxzf.gov.cn/"
    
    # 定义需要抓取的类型和对应的URL参数
    notice_types = [
        {"name": "采购公告", "type": "采购公告", "url": "https://zfcg.gxzf.gov.cn/luban/category?parentId=66485&childrenCode=ZcyAnnouncement10016&pageNo=1&pageSize=20"},
        {"name": "采购意向公开", "type": "采购意向公开", "url": "https://zfcg.gxzf.gov.cn/luban/category?parentId=66485&childrenCode=ZcyAnnouncement3007&pageNo=1&pageSize=20"}
    ]
    
    page = context.new_page()
    
    try:
        for n_type in notice_types:
            type_name = n_type["name"]
            db_type = n_type["type"]
            list_url = n_type["url"]
            
            print(f"\n--- Starting crawl for type: {type_name} ---", flush=True)
            print(f"Fetching list page: {list_url}", flush=True)
            
            captured_items = []
            
            # API 拦截数据
            def handle_list_response(response):
                if "application/json" in response.headers.get("content-type", "") and "portal/category" in response.url:
                    try:
                        json_data = response.json()
                        data_list = []
                        if isinstance(json_data.get('result'), dict) and isinstance(json_data['result'].get('data'), dict):
                            data_list = json_data['result']['data'].get('data', [])
                        
                        if data_list and isinstance(data_list, list):
                            print(f"DEBUG: Found {len(data_list)} items in API: {response.url}", flush=True)
                            for item in data_list:
                                title = item.get('title')
                                article_id = item.get('articleId')
                                pub_time_str = item.get('publishDate')
                                pub_date = None
                                
                                if pub_time_str:
                                    try:
                                        if len(str(pub_time_str)) >= 10:
                                            pub_date = datetime.strptime(str(pub_time_str)[:10], "%Y-%m-%d")
                                    except:
                                        pass
                                
                                link = f"https://zfcg.gxzf.gov.cn/portal/detail?articleId={article_id}&parentId=66485" if article_id else ""
                                
                                if title and link:
                                    captured_items.append({
                                        "title": title,
                                        "href": link,
                                        "publish_date": pub_date
                                    })
                    except Exception as e:
                        if "Target page, context or browser has been closed" not in str(e):
                            print(f"Error parsing Guangxi API response: {e}", flush=True)
            
            page.on("response", handle_list_response)
            
            try:
                # 访问列表页面
                page.goto(list_url, timeout=60000)
                try:
                    page.wait_for_load_state('networkidle', timeout=15000)
                except Exception as e:
                    print(f"  networkidle timeout, continuing: {e}", flush=True)
                time.sleep(3)  # 等待 API 响应和处理
                
                # 移除 list_response 监听器，防止影响详情页
                page.remove_listener("response", handle_list_response)
                
                # 如果 API 拦截失败，尝试从页面中提取列表数据
                if len(captured_items) == 0:
                    items = page.evaluate("""() => {
                        const results = [];
                        // 尝试多种可能的选择器
                        const rows = document.querySelectorAll('.list-item, .notice-item, .article-item, tr');
                        rows.forEach(row => {
                            const link = row.querySelector('a');
                            if (link) {
                                const title = link.textContent?.trim();
                                let href = link.getAttribute('href');
                                if (title && href) {
                                    // 处理相对链接
                                    if (href.startsWith('/')) {
                                        href = 'https://zfcg.gxzf.gov.cn' + href;
                                    } else if (!href.startsWith('http')) {
                                        href = 'https://zfcg.gxzf.gov.cn/' + href;
                                    }
                                    // 尝试获取日期
                                    const dateEl = row.querySelector('.date, .time, .publish-date');
                                    const date = dateEl ? dateEl.textContent.trim() : null;
                                    results.push({title, href, date});
                                }
                            }
                        });
                        return results;
                    }""")
                    
                    if items and len(items) > 0:
                        print(f"Found {len(items)} items from page for {type_name}", flush=True)
                        for item in items:
                            pub_date = None
                            if item.get('date'):
                                try:
                                    pub_date = datetime.strptime(str(item['date'])[:10], "%Y-%m-%d")
                                except:
                                    pass
                            captured_items.append({
                                "title": item['title'],
                                "href": item['href'],
                                "publish_date": pub_date
                            })
                    else:
                        print(f"No items found on page for {type_name}", flush=True)
                
                print(f"Total items to process: {len(captured_items)}", flush=True)
                
                # 处理抓取到的项目
                for item in captured_items:
                    title = item['title']
                    url = item['href']
                    pub_date = item.get('publish_date')
                    
                    if any(kw in title for kw in KEYWORDS):
                        print(f"Target found: {title}", flush=True)
                        existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                        if existing:
                            if not existing.raw_html or not existing.meta_info or len(existing.raw_html or "") < 200:
                                pass
                            else:
                                if existing.notice_type != db_type:
                                    existing.notice_type = db_type
                                    existing.source_website = "广西政府采购网"
                                    db.commit()
                                print(f"Skipping existing: {title}", flush=True)
                                continue
                        
                        # 抓取详情
                        try:
                            detail_page = context.new_page()
                            # 准备拦截 API 响应
                            detail_content = {"text": ""}
                            def handle_detail_response(response):
                                # ZCY 详情页数据 API 通常包含 detail 或 getInfo
                                if "application/json" in response.headers.get("content-type", ""):
                                    try:
                                        # 针对广西政府采购网: https://zfcg.gxzf.gov.cn/portal/detail
                                        if any(key in response.url for key in ["portal/detail", "front/notice/detail", "getNoticeDetail", "getInfoById"]):
                                            data = response.json()
                                            # ZCY 通用结构提取逻辑
                                            content_found = None
                                            stack = [data]
                                            while stack:
                                                current = stack.pop()
                                                if isinstance(current, dict):
                                                    # 查找可能的正文字段
                                                    # 广西网特定: result.data.content
                                                    if 'content' in current and isinstance(current['content'], str) and len(current['content']) > 100:
                                                        content_found = current['content']
                                                        break
                                                    elif 'htmlContent' in current and isinstance(current['htmlContent'], str):
                                                        content_found = current['htmlContent']
                                                        break
                                                    
                                                    for k, v in current.items():
                                                        if isinstance(v, (dict, list)):
                                                            stack.append(v)
                                                elif isinstance(current, list):
                                                    for item in current:
                                                        if isinstance(item, (dict, list)):
                                                            stack.append(item)
                                            if content_found:
                                                print(f"API interception successful for {url}", flush=True)
                                                detail_content["text"] = content_found
                                    except:
                                        pass

                            detail_page.on("response", handle_detail_response)
                            detail_page.goto(url, timeout=30000)
                            try:
                                # 显式等待关键内容出现，避免抓到空壳
                                detail_page.wait_for_selector('table, .notice-area, .detail-content, .notice-detail', timeout=15000)
                            except:
                                print("Wait for content selector timeout, trying raw extraction...", flush=True)

                            # 优先使用 API 拦截到的内容
                            content = detail_content["text"]
                            if not content:
                                print("API interception failed/empty, using DOM extraction...", flush=True)
                                content = detail_page.evaluate("""() => {
                                // 1. 优先查找表格 (通常是意向公开的核心)
                                const table = document.querySelector('.notice-area table') || document.querySelector('table');
                                if (table) return table.outerHTML;
                                
                                // 2. 查找正文容器 (排除 header/footer)
                                const contentDiv = document.querySelector('.notice-area') || 
                                                 document.querySelector('.detail-content') || 
                                                 document.querySelector('.notice-detail');
                                                 
                                if (contentDiv) return contentDiv.innerHTML;
                                
                                // 3. 兜底：如果实在找不到，尝试从 #app 中提取，但尽量移除干扰元素
                                const app = document.querySelector('#app') || document.body;
                                if (app) {
                                    // 克隆节点以免影响页面
                                    const clone = app.cloneNode(true);
                                    // 移除头部、底部、侧边栏等常见干扰项
                                    // 增加移除包含特定文本的元素 (如"下午好", "欢迎来到")
                                    const toRemove = clone.querySelectorAll('.header, .footer, .sidebar, .top-bar, .bottom-bar, .breadcrumb, .nav, .menu, .logo-area, .search-area');
                                    toRemove.forEach(el => el.remove());
                                    
                                    // 移除脚本和样式
                                    clone.querySelectorAll('script, style').forEach(el => el.remove());
                                    
                                    // 尝试移除包含无关文本的顶部元素
                                    // 这里简单遍历前几个子元素，如果是包含"下午好"的就移除
                                    const children = Array.from(clone.children);
                                    for (let i = 0; i < Math.min(children.length, 5); i++) {
                                        if (children[i].innerText && (children[i].innerText.includes('下午好') || children[i].innerText.includes('欢迎来到') || children[i].innerText.includes('登录'))) {
                                            children[i].remove();
                                        }
                                    }
                                    
                                    return clone.innerHTML;
                                }
                                
                                return document.body.innerHTML;
                            }""")
                            
                            process_bidding(db, title, content, url, pub_date, notice_type=db_type, source_website="广西政府采购网")
                            detail_page.close()
                        except Exception as e:
                            print(f"Error processing detail {url}: {e}", flush=True)
                            try:
                                detail_page.close()
                            except:
                                pass
                    else:
                        title_safe = title.encode('gbk', 'ignore').decode('gbk') if title else ""
                        print(f"Skipped (No keyword match): {title_safe}", flush=True)
                        
            except Exception as e:
                print(f"Error processing category {type_name}: {e}", flush=True)
                
    except Exception as e:
        print(f"Guangxi Crawler error: {e}", flush=True)
    finally:
        page.close()

def run_crawler_task(db: Session):
    """运行所有网站的爬虫任务（向后兼容）"""
    run_crawler_task_for_websites(db, None, None)

def run_crawler_task_for_websites(db: Session, websites: list = None, keywords: list = None, email_config = None):
    """
    运行指定网站的爬虫任务
    
    Args:
        db: 数据库会话
        websites: 要爬取的网站ID列表，如 ["guangdong", "guangxi"]，None表示爬取所有
        keywords: 关键词列表，如 ["智算", "5G"]，None表示使用默认关键词
    """
    # 设置关键词（如果传入了关键词则使用传入的，否则使用默认）
    global KEYWORDS
    if keywords and len(keywords) > 0:
        KEYWORDS = keywords
        push_log(f"使用自定义关键词（{len(KEYWORDS)}个）: {'、'.join(KEYWORDS)}", 'info')
    else:
        KEYWORDS = DEFAULT_KEYWORDS.copy()
        push_log(f"使用默认关键词（{len(KEYWORDS)}个）", 'info')
    
    push_log(f"开始爬虫任务，目标网站: {websites}", 'crawl')
    import asyncio
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    from sqlalchemy import func
    from app.models import models
    # 记录爬虫开始前的最大商机 ID，用于极其精准地过滤出本次新抓取的商机
    # 彻底避开 SQLite 时间精度丢失和时区差异带来的 Bug
    max_id_record = db.query(func.max(models.Bidding.bid_id)).first()
    max_bid_id_before_task = max_id_record[0] if max_id_record and max_id_record[0] else 0
    
    # 默认爬取所有网站
    if websites is None or len(websites) == 0:
        websites = ['guangdong', 'guangxi', 'cmcc', 'chinatelecom', 'shenzhen', 'guangzhou', 'unicom', 'gdzy', 'hainan', 'chinatower', 'miit', 'gdzjcs', 'zycg', 'ccgp', 'dfmc', 'travelsky', 'powerchina', 'ceec']
    
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            # 根据选择的网站运行对应的爬虫
            if 'guangdong' in websites:
                push_log('=== 开始爬取: 广东省政府采购网 ===', 'crawl')
                crawl_guangdong(db, context)
            
            if 'guangxi' in websites:
                push_log('=== 开始爬取: 广西政府采购网 ===', 'crawl')
                crawl_guangxi(db, context)
            
            if 'cmcc' in websites:
                push_log('=== 开始爬取: 中国移动招标与采购网 ===', 'crawl')
                crawl_cmcc(db, context)
            
            if 'chinatelecom' in websites:
                push_log('=== 开始爬取: 中国电信阳光采购网 ===', 'crawl')
                crawl_chinatelecom(db, context)
            
            if 'shenzhen' in websites:
                push_log('=== 开始爬取: 深圳市政府采购网 ===', 'crawl')
                crawl_shenzhen(db, context)
            
            if 'guangzhou' in websites:
                push_log('=== 开始爬取: 广州市政府采购中心 ===', 'crawl')
                crawl_guangzhou(db, context)
            
            if 'unicom' in websites:
                push_log('=== 开始爬取: 中国联通招标与采购网 ===', 'crawl')
                crawl_unicom(db, context)
            
            if 'gdzy' in websites:
                push_log('=== 开始爬取: 广东省公共资源交易平台 ===', 'crawl')
                crawl_gdzy(db, context)
            
            if 'hainan' in websites:
                push_log('=== 开始爬取: 海南省公共资源交易服务平台 ===', 'crawl')
                crawl_hainan(db, context)
                
            if 'chinatower' in websites:
                push_log('=== 开始爬取: 中国铁塔电子采购平台 ===', 'crawl')
                crawl_chinatower(db, context)
            
            if 'miit' in websites:
                push_log('=== 开始爬取: 通信工程建设项目招标投标管理信息平台 ===', 'crawl')
                crawl_miit(db, context)
            
            if 'gdzjcs' in websites:
                push_log('=== 开始爬取: 广东省网上中介服务超市 ===', 'crawl')
                crawl_gdzjcs(db, context)
            
            if 'zycg' in websites:
                push_log('=== 开始爬取: 中央政府采购网 ===', 'crawl')
                crawl_zycg(db, context)
            
            if 'ccgp' in websites:
                push_log('=== 开始爬取: 中国政府采购网 ===', 'crawl')
                crawl_ccgp(db, context)
            
            if 'dfmc' in websites:
                push_log('=== 开始爬取: 东风公司采购招投标平台 ===', 'crawl')
                crawl_dfmc(db, context)
            
            if 'travelsky' in websites:
                push_log('=== 开始爬取: 中国航信采购与招标网 ===', 'crawl')
                crawl_travelsky(db, context)
            
            if 'powerchina' in websites:
                push_log('=== 开始爬取: 中国电建阳光采购网 ===', 'crawl')
                crawl_powerchina(db, context)
            
            if 'ceec' in websites:
                push_log('=== 开始爬取: 中国能建电子采购平台 ===', 'crawl')
                crawl_ceec(db, context)
            
        except Exception as e:
            push_log(f"浏览器启动失败: {e}", 'error')
        finally:
            if 'browser' in locals():
                browser.close()

    push_log('所有网站抓取完成', 'success')

    # 从环境变量获取飞书Webhook
    import os
    feishu_webhook = os.environ.get("FEISHU_WEBHOOK")

    if feishu_webhook or email_config:
        push_log("正在整理商机数据准备推送...", "info")
        try:
            from app.models import models
            from app.services.report_service import generate_excel_bytes, generate_word_bytes, generate_email_html, generate_feishu_markdown, send_feishu_message
            from app.services.email_service import send_report_email
            
            # 获取本次爬取到的数据
            biddings_for_push = db.query(models.Bidding)\
                .filter(models.Bidding.bid_id > max_bid_id_before_task)\
                .filter(models.Bidding.ai_score >= 80)\
                .order_by(models.Bidding.ai_score.desc())\
                .limit(100).all()
            
            # 1. 飞书推送
            if feishu_webhook:
                push_log("正在发送飞书群机器人消息...", "info")
                md_content = generate_feishu_markdown(biddings_for_push)
                is_feishu_success = send_feishu_message(feishu_webhook, md_content)
                if is_feishu_success:
                    if biddings_for_push:
                        push_log(f"本次新增 {len(biddings_for_push)} 条重点商机，已成功推送至飞书！", "success")
                    else:
                        push_log("本次抓取无新增重点商机，已发送飞书零商机通知。", "info")
                else:
                    push_log("飞书消息推送失败，请检查 Webhook URL", "error")

            # 2. 邮件推送
            if email_config:
                push_log("正在整理商机数据并发送邮件...", "info")
                excel_bytes = generate_excel_bytes(biddings_for_push) if biddings_for_push else None
                word_bytes = generate_word_bytes(biddings_for_push) if biddings_for_push else None
                html_content = generate_email_html(biddings_for_push)
                
                success = send_report_email(email_config, excel_bytes, word_bytes, html_content)
                if success:
                    if biddings_for_push:
                        push_log(f"本次新增 {len(biddings_for_push)} 条重点商机，已成功发送至邮箱！", "success")
                    else:
                        push_log("本次抓取无新增重点商机，已发送零商机通知邮件。", "info")
        except Exception as e:
            push_log(f"生成报告或发送推送过程出错: {e}", "error")

    push_log('整个爬虫任务链已全部结束', 'success')


def crawl_chinatower(db: Session, context):
    """
    中国铁塔电子采购平台爬虫
    按照用户要求，省份选择广东省，行业分别选择通信及计算机类
    """
    print("\n=== Starting ChinaTower Crawler ===", flush=True)
    base_url = "https://ebid.chinatowercom.cn/zgtt/gggs/003001/detailpage.html"
    industries = ["电信、广播电视和卫星传输服务", "计算机、通信和其他电子设备制造业"]
    
    for industry in industries:
        push_log(f"--- 正在抓取中国铁塔行业: {industry} ---", 'info')
        page = context.new_page()
        try:
            page.goto(base_url, timeout=60000)
            page.wait_for_load_state('networkidle', timeout=30000)
            time.sleep(2)
            
            # 选择省份和行业
            try:
                # 强制通过 select 标签选值
                page.locator('#codearea').select_option(label="广东省", force=True)
                page.locator('#hangye').select_option(label=industry, force=True)
                
                # 触发 change 事件更新插件视图
                page.evaluate('''() => {
                    const event = new Event('change', { bubbles: true });
                    document.getElementById('codearea').dispatchEvent(event);
                    document.getElementById('hangye').dispatchEvent(event);
                    if (window.jQuery) {
                        $('#codearea').trigger('chosen:updated');
                        $('#hangye').trigger('chosen:updated');
                    }
                }''')
                time.sleep(1)
            except Exception as e:
                print(f"选择省份或行业失败: {e}", flush=True)

            # 点击查询
            try:
                page.locator(".chose-btn").first.click(timeout=5000)
                time.sleep(3)
            except Exception as e:
                print(f"点击查询失败: {e}", flush=True)
                
            captured_items = []
            
            for page_num in range(1, 4):
                items = page.evaluate("""() => {
                    const results = [];
                    const links = document.querySelectorAll('a');
                    for (const link of links) {
                        const title = link.innerText?.trim() || link.getAttribute('title');
                        let href = link.getAttribute('href');
                        if (title && title.length > 8 && href && href.includes('.html') && !href.includes('detailpage.html')) {
                            results.push({title: title, href: href});
                        }
                    }
                    return results;
                }""")
                
                import urllib.parse
                for item in items:
                    href = item['href']
                    if not href.startswith('http'):
                        href = urllib.parse.urljoin("https://ebid.chinatowercom.cn/zgtt/gggs/003001/", href)
                    if not any(x['href'] == href for x in captured_items):
                        captured_items.append({"title": item['title'], "href": href})
                
                # 翻页
                try:
                    next_btn = page.query_selector('.btn-next:not(.disabled), .next:not(.disabled), text=下一页')
                    if next_btn:
                        next_btn.click()
                        time.sleep(3)
                    else:
                        break
                except:
                    break
            
            matched_titles = [item['title'] for item in captured_items if any(kw in item['title'] for kw in KEYWORDS)]
            push_log(f"中国铁塔 - {industry[:6]}..: 共{len(captured_items)}条公告，匹配关键词 {len(matched_titles)} 条", 'success' if matched_titles else 'warning')
            
            for item in captured_items:
                title = item['title']
                url = item['href']
                if any(kw in title for kw in KEYWORDS):
                    existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                    if existing:
                        continue
                        
                    try:
                        detail_page = context.new_page()
                        detail_page.goto(url, timeout=30000)
                        detail_page.wait_for_load_state('networkidle', timeout=15000)
                        
                        content = detail_page.evaluate("() => document.body.innerText")
                        process_bidding(db, title, content, url, notice_type="采购公告", source_website="中国铁塔采购平台")
                        detail_page.close()
                    except Exception as e:
                        print(f"Detail error {url}: {e}", flush=True)
                        try:
                            detail_page.close()
                        except:
                            pass
        except Exception as e:
            push_log(f"中国铁塔抓取异常: {e}", 'error')
        finally:
            page.close()


def crawl_cmcc(db: Session, context):
    """
    中国移动招标与采购网爬虫
    只抓取指定单位（广东、广西、海南、设计院、互联网公司）的公告
    详情页为扫描件，不抓取正文内容
    """
    print("\n=== Starting CMCC Crawler ===", flush=True)
    print(f"目标单位: {', '.join(CMCC_TARGET_COMPANIES)}", flush=True)
    
    base_url = "https://b2b.10086.cn/#/biddingProcurementBulletin"
    page = context.new_page()
    
    try:
        # 拦截API响应
        captured_items = []
        
        def handle_response(response):
            url = response.url
            if 'queryList' in url or 'publish/query' in url:
                try:
                    if 'json' in response.headers.get('content-type', ''):
                        data = response.json()
                        if isinstance(data, dict) and 'data' in data:
                            page_data = data['data']
                            if isinstance(page_data, dict) and 'content' in page_data:
                                records = page_data['content']
                                if isinstance(records, list):
                                    print(f"[API] Captured {len(records)} items", flush=True)
                                    for record in records:
                                        title = record.get('name')
                                        publish_id = record.get('id')
                                        uuid = record.get('uuid')
                                        notice_type = record.get('publishOneType_dictText') or record.get('publishType_dictText') or '采购公告'
                                        company = record.get('companyTypeName', '')
                                        
                                        # 单位筛选
                                        is_target = any(target in company for target in CMCC_TARGET_COMPANIES)
                                        if not is_target:
                                            continue
                                        
                                        if title and publish_id:
                                            detail_url = f"https://b2b.10086.cn/#/noticeDetail?publishId={publish_id}&publishUuid={uuid or ''}"
                                            
                                            pub_date = None
                                            pub_time = record.get('publishDate') or record.get('backDate')
                                            if pub_time:
                                                try:
                                                    pub_date = datetime.strptime(str(pub_time)[:19], "%Y-%m-%d %H:%M:%S")
                                                except:
                                                    pass
                                            
                                            captured_items.append({
                                                'title': title,
                                                'url': detail_url,
                                                'publish_date': pub_date,
                                                'notice_type': notice_type,
                                                'company': company
                                            })
                except Exception as e:
                    if "Target page, context or browser has been closed" not in str(e):
                        print(f"[API Error] {e}", flush=True)
        
        page.on('response', handle_response)
        
        # 访问列表页
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        page.wait_for_load_state('networkidle', timeout=30000)
        time.sleep(8)
        
        # 翻页获取更多数据
        print("Getting more pages...", flush=True)
        for page_num in range(2, 4):  # 再爬2页
            try:
                next_btn = page.query_selector('.btn-next:not(.is-disabled), .el-pagination .btn-next:not(.is-disabled)')
                if next_btn:
                    print(f"  Page {page_num}...", flush=True)
                    next_btn.click()
                    time.sleep(5)
                else:
                    break
            except Exception as e:
                print(f"  Pagination stopped: {e}", flush=True)
                break
        
        print(f"Captured {len(captured_items)} target items", flush=True)
        
        # 处理每条公告
        for item in captured_items:
            title = item['title']
            url = item['url']
            company = item.get('company', '')
            
            print(f"Processing: {title[:60]}... [Company: {company}]", flush=True)
            
            # 关键词匹配
            if any(kw in title for kw in KEYWORDS):
                print(f"  Keyword matched", flush=True)
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    print(f"  Skipping existing", flush=True)
                    continue
                
                # 保存公告（不抓取详情正文，标记为扫描件）
                new_bidding = models.Bidding(
                    title=title,
                    source_url=url,
                    publish_date=item.get('publish_date') or datetime.now(),
                    content_abstract=f"【中国移动招标与采购网 - {item['notice_type']}】采购单位：{company}。详情页为扫描件，请点击查看原文。",
                    category='未分类',
                    notice_type=item['notice_type'],
                    source_website=f"中国移动招标与采购网-{company}",
                    ai_score=50,  # 基础分数，等待AI分析
                    raw_html='<div style="color:#999;padding:20px;text-align:center;">此公告详情页为扫描件图片格式，无法直接提取文字内容。<br>请点击下方"在浏览器打开"按钮查看原文。</div>',
                    meta_info={'is_scan_image': True, 'company': company, 'notice_type': item['notice_type']}
                )
                db.add(new_bidding)
                db.commit()
                db.refresh(new_bidding)
                print(f"  Saved: {new_bidding.title}", flush=True)
                
                # AI分析
                try:
                    from app.services.ai_service import analyze_bidding
                    analysis = analyze_bidding(title, title)  # 使用标题作为内容进行分析
                    if analysis:
                        analysis['matched_keywords'] = [kw for kw in KEYWORDS if kw in title]
                        new_bidding.ai_score = analysis.get('score', 50)
                        new_bidding.category = analysis.get('category', '未分类')
                        new_bidding.content_abstract = analysis.get('summary', new_bidding.content_abstract)[:500]
                        new_bidding.meta_info = {**new_bidding.meta_info, **analysis}
                        db.commit()
                        print(f"  AI analyzed, score: {new_bidding.ai_score}", flush=True)
                except Exception as e:
                    print(f"  AI analysis failed: {e}", flush=True)
            else:
                print(f"  No keyword match, skipped", flush=True)
                
    except Exception as e:
        print(f"CMCC Crawler error: {e}", flush=True)
    finally:
        page.close()


def crawl_chinatelecom(db: Session, context):
    """
    中国电信阳光采购网爬虫
    只抓取广东省份的公告
    详情页为扫描件，不抓取正文内容
    """
    print("\n=== Starting ChinaTelecom Crawler ===", flush=True)
    print(f"目标省份: {CHINATELECOM_TARGET_PROVINCE}", flush=True)
    
    base_url = "https://caigou.chinatelecom.com.cn/search"
    page = context.new_page()
    
    try:
        # 拦截API响应
        captured_items = []
        
        def handle_response(response):
            url = response.url
            if 'queryListNew' in url:
                try:
                    if 'json' in response.headers.get('content-type', ''):
                        data = response.json()
                        if isinstance(data, dict) and data.get('code') == 200:
                            inner_data = data.get('data', {})
                            page_info = inner_data.get('pageInfo', {})
                            records = page_info.get('list', [])
                            if isinstance(records, list):
                                print(f"[API] Captured {len(records)} items", flush=True)
                                for record in records:
                                    title = record.get('docTitle')
                                    province = record.get('provinceName', '')
                                    notice_type = record.get('docType', '采购公告')
                                    
                                    # 省份筛选 - 只保留广东
                                    if province != CHINATELECOM_TARGET_PROVINCE:
                                        continue
                                    
                                    # 构造详情URL
                                    id_encry = record.get('idEncryStr', '')
                                    doc_code = record.get('docCode', '')
                                    encry_code = record.get('encryCode', '')
                                    
                                    if title and id_encry:
                                        detail_url = f"https://caigou.chinatelecom.com.cn/noticeDetail?noticeId={id_encry}&docCode={doc_code}&encryCode={encry_code}"
                                        
                                        pub_date = None
                                        pub_time = record.get('createDate')
                                        if pub_time:
                                            try:
                                                pub_date = datetime.strptime(str(pub_time)[:10], "%Y-%m-%d")
                                            except:
                                                pass
                                        
                                        captured_items.append({
                                            'title': title,
                                            'url': detail_url,
                                            'publish_date': pub_date,
                                            'notice_type': notice_type,
                                            'province': province
                                        })
                except Exception as e:
                    if "Target page, context or browser has been closed" not in str(e):
                        print(f"[API Error] {e}", flush=True)
        
        page.on('response', handle_response)
        
        # 访问列表页
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        page.wait_for_load_state('networkidle', timeout=30000)
        time.sleep(5)
        
        # 尝试翻页获取更多数据
        print("Trying to get more pages...", flush=True)
        for page_num in range(2, 4):  # 翻3页
            try:
                next_btn = page.query_selector('.btn-next:not(.is-disabled), .el-pagination .btn-next:not(.is-disabled)')
                if next_btn:
                    print(f"  Page {page_num}...", flush=True)
                    next_btn.click()
                    time.sleep(3)
                else:
                    page_btn = page.query_selector(f'.el-pager li:nth-child({page_num})')
                    if page_btn:
                        print(f"  Page {page_num}...", flush=True)
                        page_btn.click()
                        time.sleep(3)
                    else:
                        break
            except Exception as e:
                print(f"  Pagination stopped: {e}", flush=True)
                break
        
        print(f"Captured {len(captured_items)} target items from Guangdong", flush=True)
        
        # 处理每条公告
        for item in captured_items:
            title = item['title']
            url = item['url']
            province = item.get('province', '')
            
            print(f"Processing: {title[:60]}... [Province: {province}]", flush=True)
            
            # 关键词匹配
            if any(kw in title for kw in KEYWORDS):
                print(f"  Keyword matched", flush=True)
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    print(f"  Skipping existing", flush=True)
                    continue
                
                # 保存公告（不抓取详情正文，标记为扫描件）
                new_bidding = models.Bidding(
                    title=title,
                    source_url=url,
                    publish_date=item.get('publish_date') or datetime.now(),
                    content_abstract=f"【中国电信阳光采购网 - {item['notice_type']}】省份：{province}。详情页为扫描件，请点击查看原文。",
                    category='未分类',
                    notice_type=item['notice_type'],
                    source_website=f"中国电信阳光采购网-{province}",
                    ai_score=50,
                    raw_html='<div style="color:#999;padding:20px;text-align:center;">此公告详情页为扫描件图片格式，无法直接提取文字内容。<br>请点击下方"在浏览器打开"按钮查看原文。</div>',
                    meta_info={'is_scan_image': True, 'province': province, 'notice_type': item['notice_type']}
                )
                db.add(new_bidding)
                db.commit()
                db.refresh(new_bidding)
                print(f"  Saved: {new_bidding.title}", flush=True)
                
                # AI分析
                try:
                    from app.services.ai_service import analyze_bidding
                    analysis = analyze_bidding(title, title)
                    if analysis:
                        analysis['matched_keywords'] = [kw for kw in KEYWORDS if kw in title]
                        new_bidding.ai_score = analysis.get('score', 50)
                        new_bidding.category = analysis.get('category', '未分类')
                        new_bidding.content_abstract = analysis.get('summary', new_bidding.content_abstract)[:500]
                        new_bidding.meta_info = {**new_bidding.meta_info, **analysis}
                        db.commit()
                        print(f"  AI analyzed, score: {new_bidding.ai_score}", flush=True)
                except Exception as e:
                    print(f"  AI analysis failed: {e}", flush=True)
            else:
                print(f"  No keyword match, skipped", flush=True)
                
    except Exception as e:
        print(f"ChinaTelecom Crawler error: {e}", flush=True)
    finally:
        page.close()


def crawl_shenzhen(db: Session, context):
    """
    深圳市政府采购网爬虫
    无需筛选省份（深圳市本身就是目标区域）
    详情页可以正常抓取正文
    """
    print("\n=== Starting Shenzhen Crawler ===", flush=True)
    
    base_url = "http://zfcg.szggzy.com:8081/gsgg/002001/002001002/002001002001/list.html"
    page = context.new_page()
    
    try:
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        page.wait_for_load_state('networkidle', timeout=30000)
        time.sleep(3)
        
        # 提取公告列表
        items = page.evaluate("""() => {
            const results = [];
            const listItems = document.querySelectorAll('ul.news-items li');
            for (const item of listItems) {
                const link = item.querySelector('a.text-overflow');
                if (link) {
                    const title = link.getAttribute('title') || link.innerText?.trim();
                    let href = link.getAttribute('href');
                    if (title && title.length > 5 && href) {
                        if (href.startsWith('/')) {
                            href = 'http://zfcg.szggzy.com:8081' + href;
                        }
                        const dateSpan = item.querySelector('span');
                        const date = dateSpan ? dateSpan.innerText?.trim() : '';
                        results.push({
                            title: title,
                            href: href,
                            date: date
                        });
                    }
                }
            }
            return results;
        }""")
        
        print(f"Found {len(items)} items", flush=True)
        
        # 处理每条公告
        for item in items:
            title = item['title']
            url = item['href']
            
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 关键词匹配
            if any(kw in title for kw in KEYWORDS):
                print(f"  Keyword matched", flush=True)
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    print(f"  Skipping existing", flush=True)
                    continue
                
                # 解析日期
                pub_date = None
                if item.get('date'):
                    try:
                        pub_date = datetime.strptime(str(item['date'])[:10], "%Y-%m-%d")
                    except:
                        pass
                
                # 抓取详情页
                try:
                    detail_page = context.new_page()
                    detail_page.goto(url, timeout=30000)
                    detail_page.wait_for_load_state('networkidle', timeout=20000)
                    time.sleep(2)
                    
                    # 提取正文
                    content = detail_page.evaluate("""() => {
                        // 查找正文区域
                        const selectors = [
                            '.article-content', '.detail-content', '.news-content',
                            '.content-box', '.view-content', '.info-content',
                            '.article', '.content', '#content'
                        ];
                        
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 100) {
                                return el.innerHTML;
                            }
                        }
                        
                        // 查找包含大量文本的区域
                        const divs = document.querySelectorAll('div');
                        let bestMatch = null;
                        let maxTextLength = 0;
                        
                        for (const div of divs) {
                            const text = div.innerText;
                            if (text && text.length > maxTextLength && text.length > 300) {
                                if (div.id !== 'header' && !div.className.includes('nav') && !div.className.includes('footer')) {
                                    maxTextLength = text.length;
                                    bestMatch = div;
                                }
                            }
                        }
                        
                        return bestMatch ? bestMatch.innerHTML : document.body.innerHTML;
                    }""")
                    
                    detail_page.close()
                    
                    # 处理并保存
                    process_bidding(db, title, content, url, pub_date, notice_type="采购公告", source_website="深圳市政府采购网")
                    
                except Exception as e:
                    print(f"  Detail page error: {e}", flush=True)
                    try:
                        detail_page.close()
                    except:
                        pass
            else:
                print(f"  No keyword match, skipped", flush=True)
        
        # 尝试翻页获取更多数据
        print("Trying pagination...", flush=True)
        for page_num in range(2, 4):  # 再爬2页
            try:
                # 点击下一页
                next_btn = page.query_selector('.m-pagination-page a[data-page-index]:not(.active)')
                if next_btn:
                    print(f"  Page {page_num}...", flush=True)
                    next_btn.click()
                    time.sleep(3)
                    
                    # 提取当前页公告
                    page_items = page.evaluate("""() => {
                        const results = [];
                        const listItems = document.querySelectorAll('ul.news-items li');
                        for (const item of listItems) {
                            const link = item.querySelector('a.text-overflow');
                            if (link) {
                                const title = link.getAttribute('title') || link.innerText?.trim();
                                let href = link.getAttribute('href');
                                if (title && title.length > 5 && href) {
                                    if (href.startsWith('/')) {
                                        href = 'http://zfcg.szggzy.com:8081' + href;
                                    }
                                    const dateSpan = item.querySelector('span');
                                    const date = dateSpan ? dateSpan.innerText?.trim() : '';
                                    results.push({ title, href, date });
                                }
                            }
                        }
                        return results;
                    }""")
                    
                    # 处理当前页公告
                    for item in page_items:
                        title = item['title']
                        url = item['href']
                        
                        if any(kw in title for kw in KEYWORDS):
                            print(f"  Found: {title[:50]}...", flush=True)
                            
                            existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                            if existing:
                                continue
                            
                            pub_date = None
                            if item.get('date'):
                                try:
                                    pub_date = datetime.strptime(str(item['date'])[:10], "%Y-%m-%d")
                                except:
                                    pass
                            
                            try:
                                detail_page = context.new_page()
                                detail_page.goto(url, timeout=30000)
                                detail_page.wait_for_load_state('networkidle', timeout=20000)
                                time.sleep(2)
                                
                                content = detail_page.evaluate("""() => {
                                    const selectors = ['.article-content', '.detail-content', '.news-content', '.content-box', '.content', '#content'];
                                    for (const sel of selectors) {
                                        const el = document.querySelector(sel);
                                        if (el && el.innerText && el.innerText.length > 100) return el.innerHTML;
                                    }
                                    return document.body.innerHTML;
                                }""")
                                
                                detail_page.close()
                                process_bidding(db, title, content, url, pub_date, notice_type="采购公告", source_website="深圳市政府采购网")
                            except Exception as e:
                                print(f"    Error: {e}", flush=True)
                                try:
                                    detail_page.close()
                                except:
                                    pass
                else:
                    break
            except Exception as e:
                print(f"  Pagination error: {e}", flush=True)
                break
                
    except Exception as e:
        print(f"Shenzhen Crawler error: {e}", flush=True)
    finally:
        page.close()


def crawl_guangzhou(db: Session, context):
    """
    广州市政府采购中心爬虫
    无需筛选省份（广州市本身就是目标区域）
    详情页正文在iframe中，需要特殊处理
    """
    print("\n=== Starting Guangzhou Crawler ===", flush=True)
    
    base_url = "https://gzzfcg.gcycloud.cn/freecms/site/gzsaas/cggg/index.html"
    page = context.new_page()
    
    try:
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        page.wait_for_load_state('networkidle', timeout=30000)
        time.sleep(3)
        
        # 提取公告列表
        items = page.evaluate("""() => {
            const results = [];
            const listItems = document.querySelectorAll('.noticeShowList li, .noticeListUl li, .procurementAnnouncementShowList li');
            for (const item of listItems) {
                const link = item.querySelector('a');
                if (link) {
                    const title = link.getAttribute('title') || link.innerText?.trim();
                    let href = link.getAttribute('href');
                    if (title && title.length > 5 && href && !href.includes('javascript')) {
                        if (href.startsWith('/')) {
                            href = 'https://gzzfcg.gcycloud.cn' + href;
                        }
                        results.push({
                            title: title,
                            href: href
                        });
                    }
                }
            }
            return results;
        }""")
        
        print(f"Found {len(items)} items", flush=True)
        
        # 翻页获取更多数据
        print("Getting more pages...", flush=True)
        for page_num in range(2, 4):  # 再爬2页
            try:
                next_btn = page.query_selector('.pagination-next:not(.disabled), .next:not(.disabled), .btn-next:not(.is-disabled)')
                if next_btn:
                    print(f"  Page {page_num}...", flush=True)
                    next_btn.click()
                    time.sleep(3)
                    
                    # 提取当前页公告
                    page_items = page.evaluate("""() => {
                        const results = [];
                        const listItems = document.querySelectorAll('.noticeShowList li, .noticeListUl li, .procurementAnnouncementShowList li');
                        for (const item of listItems) {
                            const link = item.querySelector('a');
                            if (link) {
                                const title = link.getAttribute('title') || link.innerText?.trim();
                                let href = link.getAttribute('href');
                                if (title && title.length > 5 && href && !href.includes('javascript')) {
                                    if (href.startsWith('/')) {
                                        href = 'https://gzzfcg.gcycloud.cn' + href;
                                    }
                                    results.push({ title, href });
                                }
                            }
                        }
                        return results;
                    }""")
                    
                    items = items.concat(page_items)
                    print(f"  Page {page_num}: {page_items.length} items", flush=True)
                else:
                    break
            except Exception as e:
                print(f"  Pagination error: {e}", flush=True)
                break
        
        print(f"Total after pagination: {len(items)} items", flush=True)
        
        # 处理每条公告
        for item in items:
            title = item['title']
            url = item['href']
            
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 关键词匹配
            if any(kw in title for kw in KEYWORDS):
                print(f"  Keyword matched", flush=True)
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    print(f"  Skipping existing", flush=True)
                    continue
                
                # 抓取详情页
                try:
                    detail_page = context.new_page()
                    detail_page.goto(url, timeout=30000)
                    detail_page.wait_for_load_state('networkidle', timeout=20000)
                    time.sleep(2)
                    
                    # 提取正文（可能在iframe中）
                    content = detail_page.evaluate("""() => {
                        // 检查是否有iframe
                        const iframe = document.querySelector('iframe');
                        if (iframe) {
                            try {
                                const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                                if (iframeDoc && iframeDoc.body) {
                                    return iframeDoc.body.innerHTML;
                                }
                            } catch(e) {}
                        }
                        
                        // 尝试多种选择器
                        const selectors = ['.ggxx-con', '.ggxxCon', '.infoCon', '.content-box', '.detail-content', '.content'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 100) {
                                return el.innerHTML;
                            }
                        }
                        
                        // 查找包含大量文本的区域
                        const divs = document.querySelectorAll('div');
                        let bestMatch = null;
                        let maxTextLength = 0;
                        for (const div of divs) {
                            const text = div.innerText;
                            if (text && text.length > maxTextLength && text.length > 300) {
                                const className = (div.className || '').toString();
                                if (!className.includes('nav') && !className.includes('footer') && !className.includes('header')) {
                                    maxTextLength = text.length;
                                    bestMatch = div;
                                }
                            }
                        }
                        return bestMatch ? bestMatch.innerHTML : document.body.innerHTML;
                    }""")
                    
                    detail_page.close()
                    
                    # 处理并保存
                    process_bidding(db, title, content, url, notice_type="采购公告", source_website="广州市政府采购中心")
                    
                except Exception as e:
                    print(f"  Detail page error: {e}", flush=True)
                    try:
                        detail_page.close()
                    except:
                        pass
            else:
                print(f"  No keyword match, skipped", flush=True)
                
    except Exception as e:
        print(f"Guangzhou Crawler error: {e}", flush=True)
    finally:
        page.close()


def crawl_unicom(db: Session, context):
    """
    中国联通招标与采购网爬虫
    筛选广东省份的公告
    详情页正文可以正常抓取
    """
    print("\n=== Starting ChinaUnicom Crawler ===", flush=True)
    print(f"目标省份: {UNICOM_TARGET_PROVINCE}", flush=True)
    
    base_url = "https://www.chinaunicombidding.cn/bidInformation"
    page = context.new_page()
    
    try:
        # 拦截API响应
        captured_data = []
        
        def handle_response(response):
            url = response.url
            if 'getAnnoList' in url:
                try:
                    if 'json' in response.headers.get('content-type', ''):
                        data = response.json()
                        records = data.get('data', {}).get('records', [])
                        if isinstance(records, list):
                            print(f"[API] Captured {len(records)} items", flush=True)
                            for r in records:
                                captured_data.append(r)
                except:
                    pass
        
        page.on('response', handle_response)
        
        # 访问列表页
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        page.wait_for_load_state('networkidle', timeout=30000)
        time.sleep(3)
        
        # 设置筛选条件
        print("Setting filters...", flush=True)
        try:
            # 选择"采购公告"
            page.click('text=采购公告', timeout=5000)
            time.sleep(1)
        except:
            pass
        
        try:
            # 选择"分公司"
            page.click('text=分公司', timeout=5000)
            time.sleep(2)
        except:
            pass
        
        # 翻页获取更多数据
        print("Getting more pages...", flush=True)
        for page_num in range(2, 4):  # 共3页
            try:
                # 查找下一页按钮
                next_btn = page.query_selector('.ant-pagination-next:not(.ant-pagination-disabled), .el-pagination .btn-next:not(.is-disabled)')
                if next_btn:
                    print(f"  Page {page_num}...", flush=True)
                    next_btn.click()
                    time.sleep(3)
                else:
                    break
            except:
                break
        
        print(f"Total captured: {len(captured_data)} items", flush=True)
        
        # 筛选广东省份的公告
        guangdong_items = []
        for item in captured_data:
            province = item.get('provinceName', '')
            company = item.get('bidCompany', '')
            title = item.get('annoName', '')
            item_id = item.get('id')
            
            if UNICOM_TARGET_PROVINCE in province or UNICOM_TARGET_PROVINCE in company:
                guangdong_items.append({
                    'id': item_id,
                    'title': title,
                    'company': company,
                    'province': province,
                    'notice_type': item.get('annoType', '采购公告'),
                    'create_date': item.get('createDate')
                })
        
        print(f"Found {len(guangdong_items)} items from Guangdong", flush=True)
        
        # 处理每条公告
        for item in guangdong_items:
            title = item['title']
            item_id = item['id']
            url = f"https://www.chinaunicombidding.cn/bidInformation/detail?id={item_id}"
            
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 关键词匹配
            if any(kw in title for kw in KEYWORDS):
                print(f"  Keyword matched", flush=True)
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    print(f"  Skipping existing", flush=True)
                    continue
                
                # 抓取详情页
                try:
                    detail_page = context.new_page()
                    detail_page.goto(url, timeout=30000)
                    detail_page.wait_for_load_state('networkidle', timeout=20000)
                    time.sleep(2)
                    
                    # 提取正文
                    content = detail_page.evaluate("""() => {
                        const selectors = ['.content', '.detail-content', '.content-box', '.article-content'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 100) {
                                return el.innerHTML;
                            }
                        }
                        return document.body.innerHTML;
                    }""")
                    
                    detail_page.close()
                    
                    # 解析发布日期
                    pub_date = None
                    if item.get('create_date'):
                        try:
                            pub_date = datetime.strptime(str(item['create_date'])[:19], "%Y-%m-%d %H:%M:%S")
                        except:
                            pass
                    
                    # 处理并保存
                    process_bidding(db, title, content, url, pub_date, notice_type=item['notice_type'], source_website=f"中国联通招标网-{item['province']}")
                    
                except Exception as e:
                    print(f"  Detail page error: {e}", flush=True)
                    try:
                        detail_page.close()
                    except:
                        pass
            else:
                print(f"  No keyword match, skipped", flush=True)
                
    except Exception as e:
        print(f"ChinaUnicom Crawler error: {e}", flush=True)
    finally:
        page.close()


def crawl_gdzy(db: Session, context):
    """
    广东省公共资源交易平台爬虫
    交易公开选择"政府采购"，分别爬取"采购意向公开"和"采购项目（公告）"
    详情页正文可以正常抓取
    """
    print("\n=== Starting GDZY Crawler ===", flush=True)
    
    base_url = "https://ygp.gdzwfw.gov.cn/#/44/jygg"
    
    # 定义要爬取的两种类型
    notice_types = [
        {"name": "采购意向公开", "type": "采购意向公开"},
        {"name": "采购项目（公告）", "type": "采购公告"}
    ]
    
    for n_type in notice_types:
        type_name = n_type["name"]
        db_type = n_type["type"]
        
        print(f"\n--- Processing type: {type_name} ---", flush=True)
        
        page = context.new_page()
        
        try:
            # 拦截API响应
            captured_items = []
            
            def handle_response(response):
                url = response.url
                if 'search/v2/items' in url:
                    try:
                        if 'json' in response.headers.get('content-type', ''):
                            data = response.json()
                            search_data = data.get('data', {})
                            page_data = search_data.get('pageData', [])
                            if isinstance(page_data, list):
                                print(f"[API] Captured {len(page_data)} items", flush=True)
                                for item in page_data:
                                    captured_items.append(item)
                    except:
                        pass
            
            page.on('response', handle_response)
            
            # 访问列表页
            print(f"Fetching list page...", flush=True)
            page.goto(base_url, timeout=60000)
            page.wait_for_load_state('networkidle', timeout=30000)
            time.sleep(3)
            
            # 设置筛选条件
            print("Setting filters...", flush=True)
            try:
                # 点击"政府采购"
                page.click('text=政府采购', timeout=5000)
                time.sleep(2)
            except:
                pass
            
            try:
                # 点击对应的交易环节
                page.click(f'text={type_name}', timeout=5000)
                time.sleep(3)
            except:
                pass
            
            # 翻页获取更多数据
            print("Getting more pages...", flush=True)
            for page_num in range(2, 4):  # 共3页
                try:
                    next_btn = page.query_selector('.ant-pagination-next:not(.ant-pagination-disabled), .pagination-next:not(.disabled)')
                    if next_btn:
                        print(f"  Page {page_num}...", flush=True)
                        next_btn.click()
                        time.sleep(3)
                    else:
                        break
                except:
                    break
            
            print(f"Total captured for {type_name}: {len(captured_items)} items", flush=True)
            
            # 处理每条公告
            for item in captured_items:
                title = item.get('noticeTitle', '')
                notice_id = item.get('noticeId')
                
                if not title or not notice_id:
                    continue
                
                print(f"Processing: {title[:60]}...", flush=True)
                
                # 关键词匹配
                if any(kw in title for kw in KEYWORDS):
                    print(f"  Keyword matched", flush=True)
                    
                    # 构造详情URL
                    url = f"https://ygp.gdzwfw.gov.cn/#/44/new/jygg/v3/D?noticeId={notice_id}"
                    
                    # 检查是否已存在
                    existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                    if existing:
                        print(f"  Skipping existing", flush=True)
                        continue
                    
                    # 抓取详情页
                    try:
                        detail_page = context.new_page()
                        detail_page.goto(url, timeout=30000)
                        detail_page.wait_for_load_state('networkidle', timeout=20000)
                        time.sleep(2)
                        
                        # 提取正文
                        content = detail_page.evaluate("""() => {
                            const selectors = ['.content', '.detail-content', '.info-content', '.article-content'];
                            for (const sel of selectors) {
                                const el = document.querySelector(sel);
                                if (el && el.innerText && el.innerText.length > 100) {
                                    return el.innerHTML;
                                }
                            }
                            return document.body.innerHTML;
                        }""")
                        
                        detail_page.close()
                        
                        # 解析发布日期
                        pub_date = None
                        pub_time = item.get('publishDate')
                        if pub_time:
                            try:
                                pub_date = datetime.strptime(str(pub_time)[:19], "%Y-%m-%d %H:%M:%S")
                            except:
                                pass
                        
                        # 处理并保存
                        source = item.get('source', '广东省公共资源交易平台')
                        process_bidding(db, title, content, url, pub_date, notice_type=db_type, source_website=source)
                        
                    except Exception as e:
                        print(f"  Detail page error: {e}", flush=True)
                        try:
                            detail_page.close()
                        except:
                            pass
                else:
                    print(f"  No keyword match, skipped", flush=True)
                    
        except Exception as e:
            print(f"GDZY Crawler error for {type_name}: {e}", flush=True)
        finally:
            page.close()


def crawl_hainan(db: Session, context):
    """
    海南省公共资源交易服务平台爬虫
    交易信息选择"政府采购"下的"采购公告"
    详情页正文可以正常抓取
    """
    print("\n=== Starting Hainan Crawler ===", flush=True)
    
    base_url = "https://ggzy.hainan.gov.cn/ggzyjy/jyxx/003002/003002002/jyxx_list.html"
    page = context.new_page()
    
    try:
        # 拦截API响应
        captured_items = []
        
        def handle_response(response):
            url = response.url
            if 'getFullTextDataNew' in url:
                try:
                    if 'json' in response.headers.get('content-type', ''):
                        data = response.json()
                        records = data.get('result', {}).get('records', [])
                        if isinstance(records, list):
                            print(f"[API] Captured {len(records)} items", flush=True)
                            for item in records:
                                captured_items.append(item)
                except:
                    pass
        
        page.on('response', handle_response)
        
        # 访问列表页
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        page.wait_for_load_state('networkidle', timeout=30000)
        time.sleep(3)
        
        # 翻页获取更多数据
        print("Getting more pages...", flush=True)
        for page_num in range(2, 4):  # 共3页
            try:
                next_btn = page.query_selector('.pagination-next:not(.disabled), .next:not(.disabled)')
                if next_btn:
                    print(f"  Page {page_num}...", flush=True)
                    next_btn.click()
                    time.sleep(3)
                else:
                    break
            except:
                break
        
        print(f"Total captured: {len(captured_items)} items", flush=True)
        
        # 处理每条公告
        for item in captured_items:
            title = item.get('title', '')
            info_id = item.get('infoid')
            link_url = item.get('linkurl')
            
            if not title or not info_id:
                continue
            
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 关键词匹配
            if any(kw in title for kw in KEYWORDS):
                print(f"  Keyword matched", flush=True)
                
                # 构造详情URL
                if link_url:
                    url = f"https://ggzy.hainan.gov.cn{link_url}" if link_url.startswith('/') else link_url
                else:
                    url = f"https://ggzy.hainan.gov.cn/ggzyjy/jyxx/003002/003002002/{info_id[:8]}/{info_id}.html"
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    print(f"  Skipping existing", flush=True)
                    continue
                
                # 抓取详情页
                try:
                    detail_page = context.new_page()
                    detail_page.goto(url, timeout=30000)
                    detail_page.wait_for_load_state('networkidle', timeout=20000)
                    time.sleep(2)
                    
                    # 提取正文
                    content = detail_page.evaluate("""() => {
                        const selectors = ['.article', '.detail-content', '.content-box', '.info-content'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 100) {
                                return el.innerHTML;
                            }
                        }
                        return document.body.innerHTML;
                    }""")
                    
                    detail_page.close()
                    
                    # 解析发布日期
                    pub_date = None
                    pub_time = item.get('webdate') or item.get('infodate')
                    if pub_time:
                        try:
                            pub_date = datetime.strptime(str(pub_time)[:19], "%Y-%m-%d %H:%M:%S")
                        except:
                            pass
                    
                    # 处理并保存
                    notice_type = item.get('categoryname', '采购公告')
                    source = item.get('xiaquname', '海南省公共资源交易服务中心')
                    process_bidding(db, title, content, url, pub_date, notice_type=notice_type, source_website=source)
                    
                except Exception as e:
                    print(f"  Detail page error: {e}", flush=True)
                    try:
                        detail_page.close()
                    except:
                        pass
            else:
                print(f"  No keyword match, skipped", flush=True)
                
    except Exception as e:
        print(f"Hainan Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_gdzjcs(db: Session, context):
    """
    广东省网上中介服务超市爬虫
    """
    print("\n=== Starting GDZJCS Crawler ===", flush=True)
    base_url = "https://ygp.gdzwfw.gov.cn/zjfwcs/gd-zjcs-pub/purchaseNotice"
    page = context.new_page()
    
    try:
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        page.wait_for_load_state('networkidle', timeout=30000)
        time.sleep(3)
        
        captured_items = []
        
        # 翻页获取数据
        for page_num in range(1, 4):  # 抓取3页
            print(f"  Page {page_num}...", flush=True)
            
            items = page.evaluate("""() => {
                const results = [];
                const listItems = document.querySelectorAll('.purchaseNotice-list-item');
                if (listItems.length === 0) {
                    const links = document.querySelectorAll('a[href*="/view/"]');
                    for (const link of links) {
                        const title = link.getAttribute('title') || link.innerText?.trim();
                        let href = link.getAttribute('href');
                        if (title && href && title.length > 5) {
                            results.push({ title, href });
                        }
                    }
                } else {
                    for (const item of listItems) {
                        const link = item.querySelector('a');
                        if (link) {
                            const title = link.getAttribute('title') || link.innerText?.trim();
                            let href = link.getAttribute('href');
                            if (title && href) {
                                results.push({ title, href });
                            }
                        }
                    }
                }
                return results;
            }""")
            
            print(f"  Found {len(items)} items on page {page_num}", flush=True)
            if not items:
                break
                
            for item in items:
                if not any(x['href'] == item['href'] for x in captured_items):
                    captured_items.append(item)
            
            # 点击下一页
            try:
                next_btn = page.query_selector('a.layui-laypage-next:not(.layui-disabled)')
                if next_btn:
                    next_btn.click()
                    time.sleep(3)
                else:
                    break
            except:
                break
                
        print(f"Total captured: {len(captured_items)} items", flush=True)
        
        # 处理每条公告
        import urllib.parse
        for item in captured_items:
            title = item['title']
            url = item['href']
            
            if not url.startswith('http'):
                url = urllib.parse.urljoin('https://ygp.gdzwfw.gov.cn/zjfwcs/gd-zjcs-pub/', url)
            
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 关键词匹配
            if any(kw in title for kw in KEYWORDS):
                print(f"  Keyword matched", flush=True)
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    print(f"  Skipping existing", flush=True)
                    continue
                
                # 抓取详情页
                try:
                    detail_page = context.new_page()
                    detail_page.goto(url, timeout=30000)
                    detail_page.wait_for_load_state('networkidle', timeout=20000)
                    time.sleep(2)
                    
                    # 提取正文
                    content_html = detail_page.evaluate("""() => {
                        const selectors = ['.detail__main', '.content-wrap', '.notice-detail-wrap', '.detail-content', '.content-box', 'body'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 100) {
                                return el.innerHTML;
                            }
                        }
                        return document.body.innerHTML;
                    }""")
                    
                    # 尝试从详情页提取日期
                    pub_date_str = detail_page.evaluate("""() => {
                        const timeEl = document.querySelector('.time, .date, .publish-time');
                        return timeEl ? timeEl.innerText : null;
                    }""")
                    
                    detail_page.close()
                    
                    pub_date = None
                    if pub_date_str:
                        import re
                        date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                        if date_match:
                            try:
                                pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                            except:
                                pass
                                
                    if not pub_date:
                        pub_date = datetime.now()
                    
                    # 处理并保存
                    process_bidding(db, title, content_html, url, pub_date, notice_type="采购公告", source_website="广东省中介服务超市")
                    
                except Exception as e:
                    print(f"  Detail page error: {e}", flush=True)
                    try:
                        detail_page.close()
                    except:
                        pass
            else:
                print(f"  No keyword match, skipped", flush=True)
                
    except Exception as e:
        print(f"GDZJCS Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_zycg(db: Session, context):
    """
    中央政府采购网爬虫
    """
    print("\n=== Starting ZYCG Crawler ===", flush=True)
    base_url = "https://www.zycg.gov.cn/freecms/site/zygjjgzfcgzx/ddwtxm/index.html?choosedli=1&choosedname=%E6%8B%9B%E6%A0%87%E9%87%87%E8%B4%AD%E5%85%AC%E5%91%8A"
    page = context.new_page()
    
    try:
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        try:
            page.wait_for_load_state('networkidle', timeout=15000)
        except:
            pass
        time.sleep(3)
        
        captured_items = []
        
        # 翻页获取数据
        for page_num in range(1, 4):  # 抓取3页
            print(f"  Page {page_num}...", flush=True)
            
            items = page.evaluate("""() => {
                const results = [];
                const links = document.querySelectorAll('.list_content li a, .news_list li a, .list_box li a');
                for (const link of links) {
                    const title = link.getAttribute('title') || link.innerText?.trim();
                    let href = link.getAttribute('href');
                    if (title && href && title.length > 5) {
                        results.push({ title, href });
                    }
                }
                
                if (results.length === 0) {
                    const links2 = document.querySelectorAll('a');
                    for (const link of links2) {
                        const title = link.getAttribute('title') || link.innerText?.trim();
                        let href = link.getAttribute('href');
                        if (title && href && href.includes('/ggxx/') && title.length > 5) {
                            results.push({ title, href });
                        }
                    }
                }
                return results;
            }""")
            
            print(f"  Found {len(items)} items on page {page_num}", flush=True)
            if not items:
                break
                
            for item in items:
                if not any(x['href'] == item['href'] for x in captured_items):
                    captured_items.append(item)
            
            # 点击下一页
            try:
                next_btn = page.query_selector('a.next:not(.disabled), li.next a, a:has-text("下一页")')
                if next_btn:
                    next_btn.click()
                    time.sleep(3)
                else:
                    break
            except:
                break
                
        print(f"Total captured: {len(captured_items)} items", flush=True)
        
        # 处理每条公告
        import urllib.parse
        for item in captured_items:
            title = item['title']
            url = item['href']
            
            if not url.startswith('http'):
                url = urllib.parse.urljoin('https://www.zycg.gov.cn', url)
            
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 关键词匹配
            if any(kw in title for kw in KEYWORDS):
                print(f"  Keyword matched", flush=True)
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    print(f"  Skipping existing", flush=True)
                    continue
                
                # 抓取详情页
                try:
                    detail_page = context.new_page()
                    detail_page.goto(url, timeout=30000)
                    detail_page.wait_for_load_state('networkidle', timeout=20000)
                    time.sleep(2)
                    
                    # 提取正文
                    content_html = detail_page.evaluate("""() => {
                        const selectors = ['.detail_content', '.article-content', '.content-box', 'body'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 100) {
                                return el.innerHTML;
                            }
                        }
                        return document.body.innerHTML;
                    }""")
                    
                    # 尝试从详情页提取日期
                    pub_date_str = detail_page.evaluate("""() => {
                        const timeEl = document.querySelector('.time, .date, .publish-time, .info-date');
                        if (timeEl) return timeEl.innerText;
                        
                        // 查找包含日期的文本
                        const allDivs = document.querySelectorAll('div, span, p');
                        for (const div of allDivs) {
                            const text = div.innerText;
                            if (text && text.match(/20\\d{2}-\\d{2}-\\d{2}/) && text.length < 50) {
                                return text;
                            }
                        }
                        return null;
                    }""")
                    
                    detail_page.close()
                    
                    pub_date = None
                    if pub_date_str:
                        import re
                        date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                        if date_match:
                            try:
                                pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                            except:
                                pass
                                
                    if not pub_date:
                        pub_date = datetime.now()
                    
                    # 处理并保存
                    process_bidding(db, title, content_html, url, pub_date, notice_type="招标公告", source_website="中央政府采购网")
                    
                except Exception as e:
                    print(f"  Detail page error: {e}", flush=True)
                    try:
                        detail_page.close()
                    except:
                        pass
            else:
                print(f"  No keyword match, skipped", flush=True)
                
    except Exception as e:
        print(f"ZYCG Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_ccgp(db: Session, context):
    """
    中国政府采购网爬虫 (包含中央公告和地方公告)
    """
    print("\n=== Starting CCGP Crawler ===", flush=True)
    urls = [
        {"url": "https://www.ccgp.gov.cn/cggg/zygg/gkzb/", "type": "中央公告"},
        {"url": "https://www.ccgp.gov.cn/cggg/dfgg/gkzb/", "type": "地方公告"}
    ]
    
    page = context.new_page()
    
    try:
        import urllib.parse
        for u_info in urls:
            base_url = u_info["url"]
            cg_type = u_info["type"]
            print(f"Fetching {cg_type} list page...", flush=True)
            
            page.goto(base_url, timeout=60000)
            time.sleep(5)
            
            captured_items = []
            
            # 翻页获取数据
            for page_num in range(1, 4):  # 抓取3页
                print(f"  Page {page_num}...", flush=True)
                
                items = page.evaluate("""() => {
                    const results = [];
                    const links = document.querySelectorAll('ul.c_list_bid li a, .vT-srch-result-list-bid li a');
                    for (const link of links) {
                        const title = link.getAttribute('title') || link.innerText?.trim();
                        let href = link.getAttribute('href');
                        
                        if (title && href && title.length > 5) {
                            results.push({ title, href });
                        }
                    }
                    
                    if (results.length === 0) {
                        const links2 = document.querySelectorAll('a');
                        for (const link of links2) {
                            const title = link.getAttribute('title') || link.innerText?.trim();
                            let href = link.getAttribute('href');
                            if (title && href && href.includes('htm') && title.length > 5) {
                                results.push({ title, href });
                            }
                        }
                    }
                    return results;
                }""")
                
                print(f"  Found {len(items)} items on page {page_num}", flush=True)
                if not items:
                    break
                    
                for item in items:
                    if not any(x['href'] == item['href'] for x in captured_items):
                        captured_items.append(item)
                
                # 点击下一页
                try:
                    next_btn = page.query_selector('a.next:not(.disabled), a:has-text("下一页")')
                    if next_btn:
                        next_btn.click()
                        time.sleep(3)
                    else:
                        break
                except:
                    break
                    
            print(f"Total captured for {cg_type}: {len(captured_items)} items", flush=True)
            
            # 处理每条公告
            for item in captured_items:
                title = item['title']
                url = item['href']
                
                if not url.startswith('http'):
                    url = urllib.parse.urljoin(base_url, url)
                
                print(f"Processing: {title[:60]}...", flush=True)
                
                # 关键词匹配
                if any(kw in title for kw in KEYWORDS):
                    print(f"  Keyword matched", flush=True)
                    
                    # 检查是否已存在
                    existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                    if existing:
                        print(f"  Skipping existing", flush=True)
                        continue
                    
                    # 抓取详情页
                    try:
                        detail_page = context.new_page()
                        detail_page.goto(url, timeout=60000, referer=base_url)
                        time.sleep(2)
                        
                        # 提取正文
                        content_html = detail_page.evaluate("""() => {
                            const selectors = ['.vF_detail_content', '.vT_detail_content', '.vF_deatil_main', '.vT_deatil_main', '.table', 'body'];
                            for (const sel of selectors) {
                                const el = document.querySelector(sel);
                                if (el && el.innerText && el.innerText.length > 100) {
                                    return el.innerHTML;
                                }
                            }
                            return document.body.innerHTML;
                        }""")
                        
                        # 尝试从详情页提取日期
                        pub_date_str = detail_page.evaluate("""() => {
                            const timeEl = document.querySelector('.time, .date, .publish-time, .vT_detail_content_content_text_t');
                            if (timeEl) return timeEl.innerText;
                            
                            const allDivs = document.querySelectorAll('div, span, p');
                            for (const div of allDivs) {
                                const text = div.innerText;
                                if (text && text.match(/20\\d{2}-\\d{2}-\\d{2}/) && text.length < 50) {
                                    return text;
                                }
                            }
                            return null;
                        }""")
                        
                        detail_page.close()
                        
                        pub_date = None
                        if pub_date_str:
                            import re
                            date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                            if date_match:
                                try:
                                    pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                                except:
                                    pass
                                    
                        if not pub_date:
                            pub_date = datetime.now()
                        
                        # 处理并保存
                        process_bidding(db, title, content_html, url, pub_date, notice_type="公开招标公告", source_website=f"中国政府采购网({cg_type})")
                        
                    except Exception as e:
                        print(f"  Detail page error: {e}", flush=True)
                        try:
                            detail_page.close()
                        except:
                            pass
                else:
                    print(f"  No keyword match, skipped", flush=True)
                    
    except Exception as e:
        print(f"CCGP Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_dfmc(db: Session, context):
    """
    东风公司采购招投标平台爬虫
    使用 etp.dfmc.com.cn 绕过 dfmjyzx.com 的长亭雷池 WAF 防护
    """
    print("\n=== Starting DFMC Crawler ===", flush=True)
    base_url = "https://etp.dfmc.com.cn/jyxx/004001/trade_info_new.html"
    page = context.new_page()
    
    # 注入绕过 webdriver 检测
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
    """)
    
    try:
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        time.sleep(3)
        
        try:
            # 尝试点击“招标公告”分类以过滤无关公告
            zb_btn = page.query_selector('text="招标公告"')
            if zb_btn:
                zb_btn.click()
                time.sleep(3)
        except Exception as e:
            print("Click 招标公告 failed:", e)
            
        captured_items = []
        
        # 翻页获取数据
        for page_num in range(1, 4):  # 抓取3页
            print(f"  Page {page_num}...", flush=True)
            
            items = page.evaluate("""() => {
                const results = [];
                const links = document.querySelectorAll('table tbody tr td a, .info_list li a, .list_ul li a, ul.ewb-right-item li a');
                for (const link of links) {
                    const title = link.getAttribute('title') || link.innerText?.trim();
                    let href = link.getAttribute('href');
                    if (title && href && href.includes('html') && title.length > 5) {
                        results.push({ title, href });
                    }
                }
                return results;
            }""")
            
            print(f"  Found {len(items)} items on page {page_num}", flush=True)
            if not items:
                break
                
            for item in items:
                if not any(x['href'] == item['href'] for x in captured_items):
                    captured_items.append(item)
            
            # 点击下一页
            try:
                next_btn = page.query_selector('.next, li.next a, a:has-text("下一页"), a:has-text(">")')
                if next_btn:
                    next_btn.click()
                    time.sleep(3)
                else:
                    break
            except:
                break
                
        print(f"Total captured: {len(captured_items)} items", flush=True)
        
        # 处理每条公告
        import urllib.parse
        for item in captured_items:
            title = item['title']
            url = item['href']
            
            if not url.startswith('http'):
                url = urllib.parse.urljoin("https://etp.dfmc.com.cn", url)
            
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 关键词匹配
            if any(kw in title for kw in KEYWORDS):
                print(f"  Keyword matched", flush=True)
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    print(f"  Skipping existing", flush=True)
                    continue
                
                # 抓取详情页
                try:
                    detail_page = context.new_page()
                    detail_page.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', {
                            get: () => undefined
                        });
                    """)
                    detail_page.goto(url, timeout=60000, referer=base_url)
                    time.sleep(2)
                    
                    # 提取正文
                    content_html = detail_page.evaluate("""() => {
                        const selectors = ['.public-content', '.article-info', '.notice-detail', '.article_con', '.content_box', '.detail_main', 'body'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 100) {
                                return el.innerHTML;
                            }
                        }
                        return document.body.innerHTML;
                    }""")
                    
                    # 尝试从详情页提取日期
                    pub_date_str = detail_page.evaluate("""() => {
                        const timeEl = document.querySelector('.time, .date, .publish-time, .vT_detail_content_content_text_t');
                        if (timeEl) return timeEl.innerText;
                        
                        const allDivs = document.querySelectorAll('div, span, p');
                        for (const div of allDivs) {
                            const text = div.innerText;
                            if (text && text.match(/20\\d{2}-\\d{2}-\\d{2}/) && text.length < 50 && text.includes('发布时间')) {
                                return text;
                            }
                        }
                        return null;
                    }""")
                    
                    detail_page.close()
                    
                    pub_date = None
                    if pub_date_str:
                        import re
                        date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                        if date_match:
                            try:
                                pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                            except:
                                pass
                                
                    if not pub_date:
                        pub_date = datetime.now()
                    
                    # 处理并保存
                    process_bidding(db, title, content_html, url, pub_date, notice_type="招标公告", source_website="东风公司采购招投标平台")
                    
                except Exception as e:
                    print(f"  Detail page error: {e}", flush=True)
                    try:
                        detail_page.close()
                    except:
                        pass
            else:
                print(f"  No keyword match, skipped", flush=True)
                
    except Exception as e:
        print(f"DFMC Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_travelsky(db: Session, context):
    """
    中国航信采购与招标网爬虫
    注意处理正文在独立 iframe 中的情况
    """
    print("\n=== Starting Travelsky Crawler ===", flush=True)
    base_url = "http://gys.travelsky.com.cn/travelsky/noticeTendering/moreNoticeTenderingPresent"
    page = context.new_page()
    
    try:
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        time.sleep(3)
        
        captured_items = []
        
        # 翻页获取数据
        for page_num in range(1, 4):  # 抓取3页
            print(f"  Page {page_num}...", flush=True)
            
            items = page.evaluate("""() => {
                const results = [];
                const as = document.querySelectorAll('a[href*="zhaobiaocaigou"]');
                for (const a of as) {
                    const title = a.innerText?.trim();
                    const href = a.getAttribute('href');
                    if (title && href && href.includes('zhaobiaocaigou')) {
                        const match = href.match(/zhaobiaocaigou\\((\\d+)\\)/);
                        if (match) {
                            results.push({
                                title: title.replace(/\\s+/g, ' '),
                                href: '/travelsky/noticeTendering/selectHtml/' + match[1]
                            });
                        }
                    }
                }
                return results;
            }""")
            
            print(f"  Found {len(items)} items on page {page_num}", flush=True)
            if not items:
                break
                
            for item in items:
                if not any(x['href'] == item['href'] for x in captured_items):
                    captured_items.append(item)
            
            # 点击下一页
            try:
                next_btn = page.query_selector('a:has-text("下一页")')
                if next_btn:
                    next_btn.click()
                    time.sleep(3)
                else:
                    break
            except:
                break
                
        print(f"Total captured: {len(captured_items)} items", flush=True)
        
        # 处理每条公告
        import urllib.parse
        for item in captured_items:
            title = item['title']
            url = item['href']
            
            if not url.startswith('http'):
                url = urllib.parse.urljoin("http://gys.travelsky.com.cn", url)
            
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 关键词匹配
            if any(kw in title for kw in KEYWORDS):
                print(f"  Keyword matched", flush=True)
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    print(f"  Skipping existing", flush=True)
                    continue
                
                # 抓取详情页
                try:
                    detail_page = context.new_page()
                    detail_page.goto(url, timeout=60000, referer=base_url)
                    time.sleep(3)
                    
                    # 尝试从 iframe 中提取正文
                    content_html = ""
                    try:
                        frames = detail_page.frames
                        for f in frames:
                            if f.url == 'about:blank' or 'travelsky' not in f.url: # Usually the content is in an about:blank iframe
                                text = f.evaluate("() => document.body.innerText")
                                if text and len(text) > 50:
                                    content_html = f.evaluate("() => document.body.innerHTML")
                                    break
                    except Exception as e:
                        print(f"  Iframe extract error: {e}", flush=True)
                    
                    if not content_html:
                        content_html = detail_page.evaluate("""() => {
                            const selectors = ['.notice_body', '.content', 'body'];
                            for (const sel of selectors) {
                                const el = document.querySelector(sel);
                                if (el && el.innerText && el.innerText.length > 50) {
                                    return el.innerHTML;
                                }
                            }
                            return document.body.innerHTML;
                        }""")
                    
                    # 提取日期
                    pub_date_str = detail_page.evaluate("""() => {
                        const timeEl = document.querySelector('.time, .date');
                        if (timeEl) return timeEl.innerText;
                        
                        const allDivs = document.querySelectorAll('div, span, p');
                        for (const div of allDivs) {
                            const text = div.innerText;
                            if (text && text.match(/20\\d{2}-\\d{2}-\\d{2}/) && text.length < 50 && text.includes('发布时间')) {
                                return text;
                            }
                        }
                        return null;
                    }""")
                    
                    detail_page.close()
                    
                    pub_date = None
                    if pub_date_str:
                        import re
                        date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                        if date_match:
                            try:
                                pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                            except:
                                pass
                                
                    if not pub_date:
                        pub_date = datetime.now()
                    
                    # 处理并保存
                    process_bidding(db, title, content_html, url, pub_date, notice_type="招标公告", source_website="中国航信采购与招标网")
                    
                except Exception as e:
                    print(f"  Detail page error: {e}", flush=True)
                    try:
                        detail_page.close()
                    except:
                        pass
            else:
                print(f"  No keyword match, skipped", flush=True)
                
    except Exception as e:
        print(f"Travelsky Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_powerchina(db: Session, context):
    """
    中国电建阳光采购网爬虫
    使用模拟点击进入详情页，并处理 iframe 中的正文
    """
    print("\n=== Starting PowerChina Crawler ===", flush=True)
    base_url = "https://bid.powerchina.cn/consult/notice?type=%E6%8B%9B%E9%87%87%E5%85%AC%E5%91%8A&typeName=%E6%8B%9B%E9%87%87%E5%85%AC%E5%91%8A&bidType=1"
    page = context.new_page()
    
    try:
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        try:
            page.wait_for_load_state('networkidle', timeout=30000)
        except Exception as e:
            print(f"  networkidle timeout, continuing: {e}", flush=True)
        time.sleep(5)
        
        captured_items = []
        
        # 翻页获取数据
        for page_num in range(1, 4):  # 抓取3页
            print(f"  Page {page_num}...", flush=True)
            
            # 记录当前页面上的所有行，稍后通过模拟点击进入
            row_count = page.evaluate("() => document.querySelectorAll('.el-table__row').length")
            print(f"  Found {row_count} rows on page {page_num}", flush=True)
            
            if row_count == 0:
                break
                
            for i in range(row_count):
                try:
                    # 提取标题
                    title = page.evaluate(f"() => {{ const el = document.querySelectorAll('.el-table__row')[{i}].querySelector('.title, .name, a, .cell span'); return el ? el.innerText.trim() : ''; }}")
                    
                    if not title or len(title) < 5:
                        continue
                        
                    print(f"Processing: {title[:60]}...", flush=True)
                    
                    # 关键词匹配
                    if any(kw in title for kw in KEYWORDS):
                        print(f"  Keyword matched", flush=True)
                        
                        # 点击该行
                        with context.expect_page(timeout=15000) as new_page_info:
                            page.evaluate(f"""() => {{
                                const row = document.querySelectorAll('.el-table__row')[{i}];
                                const link = row.querySelector('.title, a');
                                if (link) link.click();
                                else row.click();
                            }}""")
                        
                        detail_page = new_page_info.value
                        url = detail_page.url
                        
                        # 检查是否已存在
                        existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                        if existing:
                            print(f"  Skipping existing", flush=True)
                            detail_page.close()
                            continue
                            
                        detail_page.wait_for_load_state('networkidle', timeout=15000)
                        time.sleep(3)
                        
                        # 提取正文 (可能在 iframe 中)
                        content_html = detail_page.evaluate("""() => {
                            const iframe = document.querySelector('iframe');
                            if (iframe) {
                                try {
                                    return iframe.contentDocument.body.innerHTML;
                                } catch(e) {
                                    // ignore CORS error
                                }
                            }
                            const el = document.querySelector('.content-box, .notice-content, .detail-content, .ql-editor, .content');
                            if (el) return el.innerHTML;
                            return document.body.innerHTML;
                        }""")
                        
                        # 提取日期
                        pub_date_str = detail_page.evaluate("""() => {
                            const timeEl = document.querySelector('.time, .date');
                            if (timeEl) return timeEl.innerText;
                            
                            const allDivs = document.querySelectorAll('div, span, p');
                            for (const div of allDivs) {
                                const text = div.innerText;
                                if (text && text.match(/20\\d{2}-\\d{2}-\\d{2}/) && text.length < 50 && text.includes('发布时间')) {
                                    return text;
                                }
                            }
                            return null;
                        }""")
                        
                        detail_page.close()
                        
                        pub_date = None
                        if pub_date_str:
                            import re
                            date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                            if date_match:
                                try:
                                    pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                                except:
                                    pass
                                    
                        if not pub_date:
                            pub_date = datetime.now()
                        
                        # 处理并保存
                        process_bidding(db, title, content_html, url, pub_date, notice_type="招标公告", source_website="中国电建阳光采购网")
                        captured_items.append({"title": title, "url": url})
                    else:
                        print(f"  No keyword match, skipped", flush=True)
                except Exception as e:
                    print(f"  Error processing row {i}: {e}", flush=True)
            
            # 点击下一页
            try:
                next_btn = page.query_selector('.btn-next')
                if next_btn and not next_btn.get_attribute('disabled'):
                    next_btn.click()
                    page.wait_for_load_state('networkidle', timeout=15000)
                    time.sleep(3)
                else:
                    break
            except:
                break
                
        print(f"Total captured: {len(captured_items)} items", flush=True)
                
    except Exception as e:
        print(f"PowerChina Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_ceec(db: Session, context):
    """
    中国能建电子采购平台爬虫
    """
    print("\n=== Starting CEEC Crawler ===", flush=True)
    base_url = "https://ec.ceec.net.cn/HomeInfo/ProjectList.aspx?InfoLevel=MQA=&bigType=WgBCAEcARwA="
    page = context.new_page()
    
    try:
        print(f"Fetching list page...", flush=True)
        page.goto(base_url, timeout=60000)
        time.sleep(3)
        
        captured_items = []
        
        # 翻页获取数据
        for page_num in range(1, 4):  # 抓取3页
            print(f"  Page {page_num}...", flush=True)
            
            items = page.evaluate("""() => {
                const results = [];
                const links = document.querySelectorAll('a');
                for (const link of links) {
                    const title = link.getAttribute('title') || link.innerText?.trim();
                    let href = link.getAttribute('href');
                    if (title && href && href.includes('Details.aspx') && title.length > 5) {
                        results.push({ title: title.replace(/\\n/g, '').replace(/\\r/g, ''), href });
                    }
                }
                return results;
            }""")
            
            print(f"  Found {len(items)} items on page {page_num}", flush=True)
            if not items:
                break
                
            for item in items:
                if not any(x['href'] == item['href'] for x in captured_items):
                    captured_items.append(item)
            
            # 点击下一页
            try:
                next_btn = page.query_selector('#lbtnNext, a:has-text("下一页"), a:has-text(">>")')
                if next_btn:
                    next_btn.click()
                    time.sleep(3)
                else:
                    # try evaluating click
                    clicked = page.evaluate("""() => {
                        const links = document.querySelectorAll('a');
                        for(let a of links) {
                            if(a.innerText.includes('下一页')) {
                                a.click();
                                return true;
                            }
                        }
                        return false;
                    }""")
                    if clicked:
                        time.sleep(3)
                    else:
                        break
            except:
                break
                
        print(f"Total captured: {len(captured_items)} items", flush=True)
        
        # 处理每条公告
        import urllib.parse
        for item in captured_items:
            title = item['title']
            url = item['href']
            
            if not url.startswith('http'):
                url = urllib.parse.urljoin("https://ec.ceec.net.cn/HomeInfo/", url)
            
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 关键词匹配
            if any(kw in title for kw in KEYWORDS):
                print(f"  Keyword matched", flush=True)
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    print(f"  Skipping existing", flush=True)
                    continue
                
                # 抓取详情页
                try:
                    detail_page = context.new_page()
                    detail_page.goto(url, timeout=60000, referer=base_url)
                    time.sleep(2)
                    
                    # 提取正文
                    content_html = detail_page.evaluate("""() => {
                        const selectors = ['.page_wrap', '.article', '.content', '.news_content', '#Div2', '.infoCon', '.article_box', '#printArea', '.NoticeDetail'];
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el && el.innerText && el.innerText.length > 100) {
                                return el.innerHTML;
                            }
                        }
                        // Fallback to max div
                        const els = document.querySelectorAll('div');
                        let maxLen = 0;
                        let maxEl = null;
                        for (const el of els) {
                            if (el.innerText && el.innerText.length > maxLen) {
                                maxLen = el.innerText.length;
                                maxEl = el;
                            }
                        }
                        if (maxEl) return maxEl.innerHTML;
                        
                        return document.body.innerHTML;
                    }""")
                    
                    # 尝试从详情页提取日期
                    pub_date_str = detail_page.evaluate("""() => {
                        const timeEl = document.querySelector('.time, .date, .publish-time, .vT_detail_content_content_text_t');
                        if (timeEl) return timeEl.innerText;
                        
                        const allDivs = document.querySelectorAll('div, span, p');
                        for (const div of allDivs) {
                            const text = div.innerText;
                            if (text && text.match(/20\\d{2}-\\d{2}-\\d{2}/) && text.length < 50 && text.includes('时间')) {
                                return text;
                            }
                        }
                        return null;
                    }""")
                    
                    detail_page.close()
                    
                    pub_date = None
                    if pub_date_str:
                        import re
                        date_match = re.search(r'20\d{2}-\d{2}-\d{2}', pub_date_str)
                        if date_match:
                            try:
                                pub_date = datetime.strptime(date_match.group(0), "%Y-%m-%d")
                            except:
                                pass
                                
                    if not pub_date:
                        pub_date = datetime.now()
                    
                    # 处理并保存
                    process_bidding(db, title, content_html, url, pub_date, notice_type="招标公告", source_website="中国能建电子采购平台")
                    
                except Exception as e:
                    print(f"  Detail page error: {e}", flush=True)
                    try:
                        detail_page.close()
                    except:
                        pass
            else:
                print(f"  No keyword match, skipped", flush=True)
                
    except Exception as e:
        print(f"CEEC Crawler error: {e}", flush=True)
    finally:
        page.close()

def crawl_miit(db: Session, context):
    """
    通信工程建设项目招标投标管理信息平台爬虫
    API直接返回数据，无需处理前端滑块验证
    """
    print("\n=== Starting MIIT Crawler ===", flush=True)
    
    base_url = "https://txzbgl.miit.gov.cn/zbtb/gateway/gatewayPublicity/bidBulletinListDoor"
    captured_items = []
    
    try:
        print(f"Fetching MIIT API directly...", flush=True)
        # 翻页获取数据
        for page_num in range(1, 4):  # 抓取3页
            try:
                print(f"  Page {page_num}...", flush=True)
                response = context.request.post(
                    base_url,
                    data={"page": page_num, "limit": 10},
                    headers={"Content-Type": "application/json"}
                )
                
                if response.status == 200:
                    data = response.json()
                    if data and "page" in data and "list" in data["page"]:
                        items = data["page"]["list"]
                        print(f"  Found {len(items)} items on page {page_num}", flush=True)
                        if not items:
                            break
                        captured_items.extend(items)
                    else:
                        print(f"  No valid data on page {page_num}", flush=True)
                        break
                else:
                    print(f"  API returned status {response.status}", flush=True)
                    break
                
                time.sleep(2)
            except Exception as e:
                print(f"  Pagination stopped: {e}", flush=True)
                break
        
        print(f"Total captured: {len(captured_items)} items", flush=True)
        
        # 处理每条公告
        for item in captured_items:
            title = item.get('bulletinTitle', '')
            uuid = item.get('uuid')
            content_html = item.get('bulletinComment', '')
            issue_date_str = item.get('issueDate')
            
            if not title or not uuid:
                continue
                
            print(f"Processing: {title[:60]}...", flush=True)
            
            # 关键词匹配
            if any(kw in title for kw in KEYWORDS):
                print(f"  Keyword matched", flush=True)
                
                url = f"https://txzbgl.miit.gov.cn/#/gateway/inviteDetail?uuid={uuid}"
                
                # 检查是否已存在
                existing = db.query(models.Bidding).filter(models.Bidding.source_url == url).first()
                if existing:
                    print(f"  Skipping existing", flush=True)
                    continue
                
                # 解析发布日期
                pub_date = None
                if issue_date_str:
                    try:
                        pub_date = datetime.strptime(str(issue_date_str)[:10], "%Y-%m-%d")
                    except:
                        pass
                
                if not pub_date:
                    pub_date = datetime.now()
                    
                if not content_html:
                    content_html = "<p>无正文内容</p>"
                    
                try:
                    process_bidding(db, title, content_html, url, pub_date, notice_type="招标公告", source_website="通信工程平台")
                except Exception as e:
                    print(f"  Error processing item: {e}", flush=True)
            else:
                print(f"  No keyword match, skipped", flush=True)
                
    except Exception as e:
        print(f"MIIT Crawler error: {e}", flush=True)

