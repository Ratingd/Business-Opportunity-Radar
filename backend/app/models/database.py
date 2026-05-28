from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
from app.core.config import settings

# 兼容 Linux: 确保路径分隔符正确，sqlite URL 在 Windows/Linux 通用
SQLALCHEMY_DATABASE_URL = settings.DATABASE_URL

# 对于 SQLite，connect_args={"check_same_thread": False} 是必须的
# 对于 PostgreSQL，可以移除 connect_args
connect_args = {"check_same_thread": False} if "sqlite" in SQLALCHEMY_DATABASE_URL else {}

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args=connect_args
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
