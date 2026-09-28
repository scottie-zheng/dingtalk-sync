"""数据库会话管理"""
import pymysql
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from utils.config import load_config


def get_db_session() -> Session:
    """创建数据库会话"""
    config = load_config()
    rds = config["storage"]["rds"]

    engine = create_engine(
        f"mysql+pymysql://{rds['user']}:{rds['password']}"
        f"@{rds['host']}:{rds['port']}/{rds['database']}"
        f"?charset=utf8mb4",
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    return SessionLocal()
