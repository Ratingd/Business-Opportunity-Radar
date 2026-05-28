import os
import sys

# 强制 Playwright 浏览器读取路径为用户 AppData 目录，避免权限和路径丢失问题
browsers_path = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), "AppData", "Local", "ms-playwright-business-radar")
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path

# 切换到脚本所在目录（即backend目录）
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.getcwd())

from app.models.database import SessionLocal
from app.services.crawler_service import run_crawler_task_for_websites
from main import ensure_playwright_browsers

def main():
    print("Started GitHub Actions scheduled task", flush=True)
    
    # 确保Playwright浏览器已安装
    print("Checking/Installing Playwright browsers...", flush=True)
    ensure_playwright_browsers()
    
    db = SessionLocal()
    try:
        # 这里你可以自定义你要爬取的网站列表和关键词。
        # 如果传 None 或者空列表，默认会爬取所有的网站并使用默认关键词。
        websites = [] 
        keywords = [] 
        
        print("Starting crawler task...", flush=True)
        run_crawler_task_for_websites(
            db=db,
            websites=websites,
            keywords=keywords,
            email_config=None  # 不使用邮件，将通过环境变量 FEISHU_WEBHOOK 触发飞书推送
        )
        print("Crawler task finished successfully.", flush=True)
    except Exception as e:
        import traceback
        print(f"Execution Error:\n{traceback.format_exc()}", flush=True)
        sys.exit(1)
    finally:
        db.close()

if __name__ == "__main__":
    main()