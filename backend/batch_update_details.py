import sys
import os
import time
import asyncio
from datetime import datetime

# Ensure backend directory is in python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.models.database import SessionLocal
from app.models import models
from app.services.crawler_service import process_bidding
from playwright.sync_api import sync_playwright

def get_incomplete_records(db):
    """
    Find records that need updating:
    1. raw_html is None or empty
    2. raw_html length < 200
    3. meta_info is None
    4. meta_info indicates 'Unknown' for key fields (optional, but good for cleanup)
    """
    all_biddings = db.query(models.Bidding).all()
    incomplete = []
    
    for bidding in all_biddings:
        is_incomplete = False
        
        # Check HTML content
        if not bidding.raw_html or len(bidding.raw_html) < 200:
            is_incomplete = True
            
        # Check Meta Info
        elif not bidding.meta_info:
            is_incomplete = True
        
        # Check if AI analysis failed (budget/deadline unknown)
        # Note: We only check this if HTML is present, to avoid double counting
        elif isinstance(bidding.meta_info, dict):
             if bidding.meta_info.get('budget_amount') == '未知' or \
                bidding.meta_info.get('deadline') == '未知':
                 # If HTML is long enough but AI failed, we might want to re-run AI only?
                 # But re-crawling is safer to ensure we have the clean text.
                 is_incomplete = True

        if is_incomplete:
            incomplete.append(bidding)
            
    return incomplete

def batch_update():
    print("Starting Batch Update for Incomplete Records...")
    
    # Windows loop policy
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    db = SessionLocal()
    
    try:
        targets = get_incomplete_records(db)
        print(f"Found {len(targets)} incomplete records.")
        
        if not targets:
            print("No records to update.")
            return

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                print(f"Browser launch failed: {e}")
                return

            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            for i, bidding in enumerate(targets):
                print(f"[{i+1}/{len(targets)}] Processing: {bidding.title}")
                url = bidding.source_url
                
                try:
                    page = context.new_page()
                    
                    # --- Interception Logic (Copied from crawler_service.py) ---
                    detail_content = {"text": ""}
                    
                    def handle_detail_response(response):
                        if "application/json" in response.headers.get("content-type", ""):
                            try:
                                if any(key in response.url for key in ["selectInfoByOpenTenderCode", "getInfoById", "getNoticeDetail"]):
                                    data = response.json()
                                    content_found = None
                                    
                                    # Recursive search
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
                                        print(f"  -> Captured content from API: {len(content_found)} chars")
                                        detail_content["text"] = content_found
                                        
                            except Exception as e:
                                pass

                    page.on("response", handle_detail_response)
                    
                    # Go to page
                    try:
                        page.goto(url, timeout=30000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=10000)
                        except:
                            pass
                    except Exception as e:
                        print(f"  -> Navigation failed: {e}")
                        page.close()
                        continue

                    # Fallback check
                    content = detail_content["text"]
                    if not content:
                        print("  -> API interception failed, waiting/trying DOM fallback...")
                        page.wait_for_timeout(2000)
                        if detail_content["text"]:
                             content = detail_content["text"]
                        else:
                             # DOM Fallback
                             try:
                                 content = page.evaluate("""() => {
                                    const contentDiv = document.querySelector('.content') || 
                                                     document.querySelector('.article') || 
                                                     document.querySelector('.notice-content') ||
                                                     document.body;
                                    return contentDiv ? contentDiv.innerText : "";
                                }""")
                             except:
                                 content = ""
                    
                    if not content or len(content) < 100:
                        print("  -> Failed to retrieve valid content. Skipping update.")
                    else:
                        print(f"  -> Success! Content length: {len(content)}")
                        # Update record
                        process_bidding(db, bidding.title, content, url, bidding.publish_date)
                    
                    page.close()
                    
                except Exception as e:
                    print(f"  -> Error processing {bidding.title}: {e}")
                
                # Sleep briefly to be nice
                time.sleep(1)

            browser.close()

    finally:
        db.close()
        print("Batch update finished.")

if __name__ == "__main__":
    batch_update()
