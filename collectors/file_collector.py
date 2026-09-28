#!/usr/bin/env python3
"""
云文件采集器 (DingTalk Cloud File Collector)

通过 search_dentries API 采集钉钉云盘中的文件列表。
由于 Storage.OperationLog.Read.Me 权限无法申请，
改用 search_dentries 按关键词搜索文件，追踪文件创建者/修改者变化。

API:
  - POST /v2.0/storage/dentries/search (Storage.Dentry.Search)
    operatorId 在 query, keyword+option 在 body

用法:
  python collectors/file_collector.py
"""

import os
import sys
import json
import time
from datetime import datetime
from pathlib import Path

# 添加项目根目录到 path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / "config" / ".env")

import mysql.connector
import requests

# ============================================================
# 配置
# ============================================================
APP_KEY = os.getenv("DINGTALK_APP_KEY")
APP_SECRET = os.getenv("DINGTALK_APP_SECRET")
UNION_ID = os.getenv("DINGTALK_UNION_ID")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 3306)),
    "user": os.getenv("DB_USER", "sync_user"),
    "password": os.getenv("DB_PASSWORD", "sync_pass123"),
    "database": os.getenv("DB_NAME", "dingtalk_sync"),
    "charset": "utf8mb4",
}

# 搜索关键词列表（用于覆盖不同类型的文件）
SEARCH_KEYWORDS = [
    "文件", "文档", "会议", "报告", "计划",
    "项目", "纪要", "方案", "设计", "测试",
    "培训", "资源", "验收", "调研", "进度",
    "a", "e", "o", "1", "2",
]

API_BASE = "https://api.dingtalk.com/v2.0/storage"


# ============================================================
# AccessToken
# ============================================================
def get_access_token() -> str:
    """获取 AccessToken（先查 Redis 缓存，再调 API）"""
    import redis
    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True,
    )
    cached = r.get("dingtalk:access_token")
    if cached:
        return cached

    resp = requests.get(
        "https://oapi.dingtalk.com/gettoken",
        params={"appkey": APP_KEY, "appsecret": APP_SECRET},
    )
    data = resp.json()
    if data.get("errcode") != 0:
        raise RuntimeError(f"获取 token 失败: {data}")

    token = data["access_token"]
    r.set("dingtalk:access_token", token, ex=data.get("expires_in", 7200) - 300)
    return token


# ============================================================
# API 调用
# ============================================================
def search_dentries(token: str, keyword: str, next_token: str = None) -> dict:
    """调用 search_dentries API"""
    headers = {
        "x-acs-dingtalk-access-token": token,
        "Content-Type": "application/json",
    }

    body = {
        "keyword": keyword,
        "option": {"maxResults": 50},
    }
    if next_token:
        body["option"]["nextToken"] = next_token

    resp = requests.post(
        f"{API_BASE}/dentries/search?operatorId={UNION_ID}",
        headers=headers,
        json=body,
    )

    if resp.status_code != 200:
        raise RuntimeError(
            f"search_dentries 失败 [{resp.status_code}]: {resp.text[:200]}"
        )

    return resp.json()


# ============================================================
# 采集主逻辑
# ============================================================
def collect_files(token: str, db_cursor) -> dict:
    """采集所有文件"""
    print("\n📁 采集云文件列表...")

    seen_uuids = set()
    all_files = []
    keyword_stats = {}

    for kw in SEARCH_KEYWORDS:
        nt = None
        kw_count = 0
        while True:
            try:
                data = search_dentries(token, kw, nt)
            except RuntimeError as e:
                print(f"  ⚠️  关键词 '{kw}' 出错: {e}")
                break

            items = data.get("items", [])
            for item in items:
                uuid = item.get("dentryUuid", "")
                if uuid and uuid not in seen_uuids:
                    seen_uuids.add(uuid)
                    all_files.append(item)
                    kw_count += 1

            nt = data.get("nextToken")
            if not nt or not items:
                break
            time.sleep(0.3)

        if kw_count > 0:
            keyword_stats[kw] = kw_count
            print(f"  关键词 '{kw}': +{kw_count} 新文件")

        time.sleep(0.2)

    print(f"\n  ✅ 共发现 {len(all_files)} 个文件（去重后）")

    # 写入数据库
    new_count = 0
    updated_count = 0
    modified_count = 0

    for f in all_files:
        uuid = f.get("dentryUuid", "")
        name = f.get("name", "")
        creator = f.get("creator", {})
        modifier = f.get("modifier", {})
        creator_name = creator.get("name", "")
        creator_user_id = creator.get("userId", "")
        modifier_name = modifier.get("name", "")
        modifier_user_id = modifier.get("userId", "")

        # 判断是否被他人修改
        modified_flag = 0
        if creator_user_id and modifier_user_id and creator_user_id != modifier_user_id:
            modified_flag = 1
            modified_count += 1

        # UPSERT
        db_cursor.execute("""
            INSERT INTO ods_file_dentry (
                dentry_uuid, name, creator_name, creator_user_id,
                modifier_name, modifier_user_id, modified_flag,
                first_seen, last_sync
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, NOW(), NOW()
            )
            ON DUPLICATE KEY UPDATE
                name = VALUES(name),
                creator_name = VALUES(creator_name),
                creator_user_id = VALUES(creator_user_id),
                modifier_name = VALUES(modifier_name),
                modifier_user_id = VALUES(modifier_user_id),
                modified_flag = VALUES(modified_flag),
                last_sync = NOW()
        """, (
            uuid, name, creator_name, creator_user_id,
            modifier_name, modifier_user_id, modified_flag,
        ))

        if db_cursor.rowcount == 1:
            new_count += 1
        elif db_cursor.rowcount == 2:
            updated_count += 1

    print(f"  新增: {new_count}, 更新: {updated_count}, 被他人修改: {modified_count}")

    return {
        "total": len(all_files),
        "new": new_count,
        "updated": updated_count,
        "modified": modified_count,
        "keywords": keyword_stats,
    }


# ============================================================
# 统计汇总
# ============================================================
def print_summary(db_cursor):
    """打印采集统计"""
    print("\n" + "=" * 50)
    print("📊 采集统计")
    print("=" * 50)

    # 文件总数
    db_cursor.execute("SELECT COUNT(*) FROM ods_file_dentry")
    total = db_cursor.fetchone()[0]
    print(f"  文件总数: {total}")

    # 被修改的文件
    db_cursor.execute("SELECT COUNT(*) FROM ods_file_dentry WHERE modified_flag = 1")
    modified = db_cursor.fetchone()[0]
    print(f"  被他人修改: {modified}")

    # 创建者分布
    db_cursor.execute("""
        SELECT creator_name, COUNT(*) as cnt
        FROM ods_file_dentry
        GROUP BY creator_name
        ORDER BY cnt DESC
        LIMIT 10
    """)
    rows = db_cursor.fetchall()
    if rows:
        print("\n  文件创建者 Top 10:")
        for name, cnt in rows:
            print(f"    {name}: {cnt}")

    # 修改者分布
    db_cursor.execute("""
        SELECT modifier_name, COUNT(*) as cnt
        FROM ods_file_dentry
        WHERE modifier_user_id != creator_user_id
        GROUP BY modifier_name
        ORDER BY cnt DESC
        LIMIT 10
    """)
    rows = db_cursor.fetchall()
    if rows:
        print("\n  文件修改者（非创建者）:")
        for name, cnt in rows:
            print(f"    {name}: {cnt}")

    # 最近发现的文件
    db_cursor.execute("""
        SELECT name, creator_name, modifier_name, modified_flag, last_sync
        FROM ods_file_dentry
        ORDER BY last_sync DESC
        LIMIT 5
    """)
    rows = db_cursor.fetchall()
    if rows:
        print("\n  最近同步的文件:")
        for name, creator, modifier, flag, sync in rows:
            mod_mark = " ✏️" if flag else ""
            display = (name or "")[:35]
            print(f"    [{creator}/{modifier}]{mod_mark} {display}")


# ============================================================
# 主函数
# ============================================================
def main():
    print("🚀 云文件采集器")
    print(f"   时间: {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"   搜索关键词: {len(SEARCH_KEYWORDS)} 个")

    # 检查必要配置
    if not UNION_ID:
        print("   ❌ 缺少 DINGTALK_UNION_ID 配置！")
        print("   请在 config/.env 中添加: DINGTALK_UNION_ID=你的unionid")
        sys.exit(1)
    print(f"   UNION_ID: {UNION_ID[:10]}...")

    # 获取 token
    token = get_access_token()
    print(f"   AccessToken: {token[:20]}...")

    # 连接数据库
    conn = mysql.connector.connect(**DB_CONFIG)
    cursor = conn.cursor()

    try:
        # 建表（如果不存在）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ods_file_dentry (
                id              BIGINT AUTO_INCREMENT PRIMARY KEY,
                dentry_uuid     VARCHAR(128) NOT NULL COMMENT '文件唯一ID',
                name            VARCHAR(512) COMMENT '文件名称',
                creator_name    VARCHAR(128) COMMENT '创建者姓名',
                creator_user_id VARCHAR(128) COMMENT '创建者UserID',
                modifier_name   VARCHAR(128) COMMENT '最近修改者姓名',
                modifier_user_id VARCHAR(128) COMMENT '最近修改者UserID',
                modified_flag   TINYINT DEFAULT 0 COMMENT '是否被他人修改(creator≠modifier)',
                first_seen      DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '首次发现时间',
                last_sync       DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '最后同步时间',
                UNIQUE KEY uk_dentry_uuid (dentry_uuid),
                INDEX idx_creator (creator_user_id),
                INDEX idx_modifier (modifier_user_id),
                INDEX idx_modified_flag (modified_flag)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # 采集文件
        stats = collect_files(token, cursor)

        # 提交
        conn.commit()

        # 统计
        print_summary(cursor)

        print("\n✅ 采集完成！")

    except Exception as e:
        conn.rollback()
        print(f"\n❌ 采集失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()
