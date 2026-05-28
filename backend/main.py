import os
import sys
import subprocess
from pathlib import Path

# 在打包环境下，确保获取正确的运行目录
if getattr(sys, 'frozen', False):
    # 如果是用 pyinstaller --onedir 打包的，sys._MEIPASS 或 sys.executable 所在的目录就是我们的根目录
    base_dir = os.path.dirname(sys.executable)
else:
    base_dir = os.getcwd()

# 强制将 Playwright 浏览器缓存设置到系统用户目录
# 这是因为在部分 Windows 系统下，如果把浏览器下到安装目录，会导致文件读写权限报错，从而执行失败
# 使用绝对的用户 AppData 路径是最稳妥的做法
browsers_path = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "AppData", "Local", "ms-playwright-business-radar")
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path

# 全局变量记录浏览器下载状态
browser_status = {
    "ready": False,
    "installing": False,
    "error": None
}

def ensure_playwright_browsers():
    global browser_status
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # 必须尝试 launch 才能触发找不到浏览器的错误
            browser = p.chromium.launch(headless=True)
            browser.close()
        browser_status["ready"] = True
    except Exception as e:
        print(f"Installing Playwright Chromium browser... Reason: {e}", flush=True)
        browser_status["installing"] = True
        try:
            from playwright._impl._driver import compute_driver_executable
            
            driver_executable, driver_cli = compute_driver_executable()
            # 必须传入修改后的环境变量给子进程
            env = os.environ.copy()
            # 注意，这里必须恢复为 *driver_cli，因为在某些环境下 driver_cli 返回的是列表，而之前我们的测试脚本里返回的是字符串
            # 为了兼容性，判断其类型再处理
            cmd = [driver_executable]
            if isinstance(driver_cli, list):
                cmd.extend(driver_cli)
            else:
                cmd.append(driver_cli)
            cmd.extend(["install", "chromium"])
            
            # 使用 shell=True 或者不在主线程阻塞，避免网络请求过慢导致后端 API 一直无法启动
            subprocess.run(cmd, env=env, check=True)
            browser_status["ready"] = True
            browser_status["installing"] = False
            print("Browser installation completed successfully.", flush=True)
        except Exception as ex:
            browser_status["installing"] = False
            browser_status["error"] = str(ex)
            print("Failed to install browser:", ex)

# 不要在这里阻塞启动，改为在后台线程执行
# ensure_playwright_browsers()

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional
from app.models.database import engine, Base, get_db
from app.models import models
from app.services.crawler_service import run_crawler_task, run_crawler_task_for_websites, log_queue
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import json
import time
import io
import urllib.parse

# 导入文档处理库
try:
    import pandas as pd
    from docx import Document
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH
except ImportError:
    pd = None
    Document = None

import threading

# 创建数据库表
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="商机雷达 API")

@app.on_event("startup")
def on_startup():
    # 启动时在后台线程检查并安装浏览器，不阻塞 FastAPI 的 8000 端口绑定
    threading.Thread(target=ensure_playwright_browsers, daemon=True).start()

# CORS 设置 (允许所有来源，生产环境需限制)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class EmailConfig(BaseModel):
    receiver: str
    sender: str
    password: str
    smtp_server: str
    smtp_port: int

class CrawlRequest(BaseModel):
    websites: Optional[List[str]] = None
    keywords: Optional[List[str]] = None  # 新增：关键词列表
    email_config: Optional[EmailConfig] = None  # 新增：邮箱配置

class ClearDatabaseRequest(BaseModel):
    confirm: bool = False  # 确认清除

class ScheduleRequest(BaseModel):
    enable: bool
    time1: str = ""
    time2: str = ""
    websites: list = []
    keywords: list = []
    email_config: Optional[EmailConfig] = None

@app.get("/")
def read_root():
    return {"message": "Welcome to Business Opportunity Radar API"}

@app.get("/biddings")
def get_biddings(skip: int = 0, limit: int = 20, category: str = None, notice_type: str = "采购公告", time_filter: int = 0, db: Session = Depends(get_db)):
    from datetime import datetime, timedelta
    query = db.query(models.Bidding).order_by(models.Bidding.created_at.desc())
    if notice_type:
         query = query.filter(models.Bidding.notice_type == notice_type)
    if category and category != "全部":
        query = query.filter(models.Bidding.category == category)
    
    if time_filter > 0:
        if time_filter == 1:
            # 今天凌晨 0 点
            start_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start_date = datetime.now() - timedelta(days=time_filter)
        query = query.filter(models.Bidding.created_at >= start_date)
        
    biddings = query.offset(skip).limit(limit).all()
    return biddings

@app.get("/biddings/{bid_id}")
def get_bidding_detail(bid_id: int, db: Session = Depends(get_db)):
    bidding = db.query(models.Bidding).filter(models.Bidding.bid_id == bid_id).first()
    if not bidding:
        raise HTTPException(status_code=404, detail="Bidding not found")
    return bidding

@app.post("/crawl")
def trigger_crawl(request: CrawlRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    手动触发爬虫任务 (异步后台运行)
    支持指定网站列表: {"websites": ["guangdong", "guangxi"], "keywords": ["智算", "5G"]}
    """
    global browser_status
    if not browser_status["ready"]:
        if browser_status["installing"]:
            return {"status": "error", "message": "首次运行正在下载内置浏览器组件，预计需要 1-3 分钟，请耐心等待下载完成后重试..."}
        else:
            err_msg = browser_status.get("error", "未知错误")
            return {"status": "error", "message": f"内置浏览器初始化失败，请重启软件重试。({err_msg})"}

    print(f"Received crawl request, websites: {request.websites}, keywords: {request.keywords}, starting background task...", flush=True)
    
    # 使用 FastAPI 的 BackgroundTasks 在独立线程池中运行同步爬虫
    # 支持指定网站列表和关键词列表及邮箱配置
    background_tasks.add_task(run_crawler_task_for_websites, db, request.websites, request.keywords, request.email_config)
    
    return {"success": True, "message": "Crawler task started in background", "websites": request.websites, "keywords": request.keywords}

@app.post("/clear-database")
def clear_database(request: ClearDatabaseRequest, db: Session = Depends(get_db)):
    """
    清空数据库中的所有招标数据
    需要确认: {"confirm": true}
    """
    if not request.confirm:
        raise HTTPException(status_code=400, detail="请确认清除操作: {'confirm': true}")
    
    try:
        # 删除所有招标数据
        count = db.query(models.Bidding).delete()
        db.commit()
        print(f"Database cleared, deleted {count} records", flush=True)
        return {"success": True, "message": f"数据库已清空，共删除 {count} 条记录", "deleted_count": count}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"清除数据库失败: {str(e)}")

@app.post("/schedule")
def update_schedule(req: ScheduleRequest):
    """设置 Windows 定时任务"""
    import os
    import subprocess
    import sys
    
    cwd = os.getcwd()
    config_path = os.path.join(cwd, "schedule_config.json")
    bat_path = os.path.join(cwd, "run_task.bat")
    vbs_path = os.path.join(cwd, "run_hidden.vbs")
    
    is_packaged = getattr(sys, 'frozen', False)
    exe_path = sys.executable if is_packaged else "py"
    
    # 1. 无论是否开启，先保存最新的配置供可能存在的定时任务读取
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(req.dict(), f, ensure_ascii=False, indent=2)
        
    # 2. 生成唤起脚本 (由于 schtasks 需要绝对路径，且最好不弹黑框)
    with open(bat_path, "w", encoding="gbk") as f:
        f.write(f'@echo off\ncd /d "{cwd}"\n')
        if is_packaged:
            f.write(f'"{exe_path}" --run-scheduled\n')
        else:
            f.write(f'{exe_path} run_scheduled.py\n')
        
    with open(vbs_path, "w", encoding="gbk") as f:
        f.write('Set WshShell = CreateObject("WScript.Shell")\n')
        f.write(f'WshShell.Run chr(34) & "{bat_path}" & Chr(34), 0\n')
        f.write('Set WshShell = Nothing\n')

    # 3. 删除旧任务
    subprocess.run('schtasks /Delete /TN "BusinessRadar_Task1" /F', shell=True, capture_output=True)
    subprocess.run('schtasks /Delete /TN "BusinessRadar_Task2" /F', shell=True, capture_output=True)
    
    # 4. 创建新任务
    if req.enable:
        if req.time1:
            cmd1 = f'schtasks /Create /TN "BusinessRadar_Task1" /TR "wscript.exe \\"{vbs_path}\\"" /SC DAILY /ST {req.time1} /F'
            subprocess.run(cmd1, shell=True)
        if req.time2:
            cmd2 = f'schtasks /Create /TN "BusinessRadar_Task2" /TR "wscript.exe \\"{vbs_path}\\"" /SC DAILY /ST {req.time2} /F'
            subprocess.run(cmd2, shell=True)
            
    return {"success": True, "message": "定时任务已同步"}

@app.get("/logs/stream")
async def stream_logs():
    """
    SSE 实时日志流接口
    前端使用 EventSource 订阅实时日志
    """
    async def event_generator():
        while True:
            try:
                # 非阻塞方式检查日志队列
                if not log_queue.empty():
                    msg = log_queue.get_nowait()
                    data = json.dumps(msg, ensure_ascii=False)
                    yield f"data: {data}\n\n"
                else:
                    # 发送心跳
                    yield f"data: {{\"type\": \"heartbeat\", \"ts\": {int(time.time())}}}\n\n"
                    await asyncio.sleep(1)
            except Exception:
                await asyncio.sleep(1)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        }
    )

@app.get("/export/excel")
def export_excel(notice_type: str = None, db: Session = Depends(get_db)):
    """导出商机清单 Excel"""
    try:
        from app.services.report_service import generate_excel_bytes
        
        query = db.query(models.Bidding).order_by(models.Bidding.created_at.desc())
        if notice_type:
            query = query.filter(models.Bidding.notice_type == notice_type)
        biddings = query.all()
        
        excel_bytes = generate_excel_bytes(biddings)
        
        filename = urllib.parse.quote("商机清单.xlsx")
        return StreamingResponse(
            io.BytesIO(excel_bytes), 
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出Excel失败: {str(e)}")

@app.get("/export/word")
def export_word(notice_type: str = None, db: Session = Depends(get_db)):
    """导出商机分析报告 Word"""
    try:
        from app.services.report_service import generate_word_bytes
        
        query = db.query(models.Bidding).order_by(models.Bidding.created_at.desc())
        if notice_type:
            query = query.filter(models.Bidding.notice_type == notice_type)
        # Word报告只导出得分 >= 60 的重点商机，最多100条防止文件过大
        biddings = query.filter(models.Bidding.ai_score >= 60).limit(100).all()
        
        word_bytes = generate_word_bytes(biddings)
        
        filename = urllib.parse.quote("商机分析报告.docx")
        return StreamingResponse(
            io.BytesIO(word_bytes), 
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"导出Word失败: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    import sys
    
    # 允许通过命令行参数启动定时任务
    if len(sys.argv) > 1 and sys.argv[1] == "--run-scheduled":
        from run_scheduled import main as scheduled_main
        scheduled_main()
        sys.exit(0)
        
    # 启动 Web 服务器
    uvicorn.run(app, host="0.0.0.0", port=8000)
