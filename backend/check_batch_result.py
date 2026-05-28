import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.models.database import SessionLocal
from app.models import models

def check_record():
    db = SessionLocal()
    title_part = "广州市中心城区非机动车道骨干网络优化提升方案"
    bidding = db.query(models.Bidding).filter(models.Bidding.title.contains(title_part)).first()
    
    if bidding:
        print(f"Title: {bidding.title}")
        print(f"Raw HTML Length: {len(bidding.raw_html) if bidding.raw_html else 0}")
        print(f"Meta Info: {bidding.meta_info}")
        print(f"AI Score: {bidding.ai_score}")
    else:
        print("Record not found")
    
    db.close()

if __name__ == "__main__":
    check_record()
