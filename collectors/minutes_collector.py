"""
闪记采集器

职责：
- 获取闪记列表（query_create_minutes_list）
- 获取闪记转录文本（query_minutes_text）
- 将元数据写入 RDS ODS 层

SDK:
- alibabacloud_dingtalk.minutes_1_0 — 闪记管理
"""
import time

from alibabacloud_dingtalk.minutes_1_0.client import Client as MinutesClient
from alibabacloud_dingtalk.minutes_1_0.models import (
    QueryCreateMinutesListRequest,
    QueryCreateMinutesListHeaders,
    QueryMinutesTextRequest,
    QueryMinutesTextHeaders,
)
from alibabacloud_tea_openapi.models import Config as OpenApiConfig
from alibabacloud_tea_util.models import RuntimeOptions
from sqlalchemy import text
from sqlalchemy.orm import Session
from tenacity import retry, stop_after_attempt, wait_exponential

from utils.token_manager import TokenManager
from utils.db import get_db_session


class MinutesCollector:
    """闪记采集器"""

    PAGE_SIZE = 20
    API_RATE_LIMIT_SLEEP = 0.5
    DEFAULT_DAYS_BACK = 30

    def __init__(self, token_manager: TokenManager, db: Session, operator_id: str):
        self.token_manager = token_manager
        self.db = db
        self.operator_id = operator_id

        config = OpenApiConfig()
        config.protocol = "https"
        config.region_id = "central"
        self.client = MinutesClient(config)

        self.stats = {
            "minutes": 0,
            "texts": 0,
            "errors": 0,
        }

    def _get_token(self) -> str:
        return self.token_manager.get_access_token()

    def collect_all(self, days_back: int = None):
        """完整采集流程"""
        if days_back is None:
            days_back = self.DEFAULT_DAYS_BACK

        print(f"\n📝 开始闪记采集（最近 {days_back} 天）")
        print("=" * 50)

        # Step 1: 获取闪记列表
        minutes_list = self._list_minutes(days_back)
        print(f"  发现 {len(minutes_list)} 条闪记")

        for item in minutes_list:
            task_uuid = item["task_uuid"]
            title = item.get("title", "无标题")
            print(f"\n  📝 {title} (UUID: {task_uuid[:12]}...)")

            # Step 2: 写入闪记基础信息
            self._upsert_minutes(item)
            self.stats["minutes"] += 1

            # Step 3: 获取并写入转录文本
            text_count = self._collect_minutes_text(task_uuid)
            if text_count:
                print(f"    ✅ {text_count} 段转录文本")
            else:
                print(f"    ⚠ 无转录文本")

            time.sleep(self.API_RATE_LIMIT_SLEEP)

        self._print_summary()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    def _list_minutes(self, days_back: int) -> list[dict]:
        """获取闪记列表（分页）"""
        all_minutes = []

        # 计算时间范围（毫秒时间戳）
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (days_back * 24 * 3600 * 1000)

        next_token = None
        page = 0

        while True:
            page += 1
            request = QueryCreateMinutesListRequest(
                union_id=self.operator_id,
                max_results=self.PAGE_SIZE,
                next_token=next_token,
                gmt_create_start=start_ms,
                gmt_create_end=now_ms,
            )
            headers = QueryCreateMinutesListHeaders(
                x_acs_dingtalk_access_token=self._get_token()
            )
            response = self.client.query_create_minutes_list_with_options(
                request, headers, RuntimeOptions()
            )
            items = response.body.minutes_details or []

            for item in items:
                # 跳过已删除的闪记
                if getattr(item, 'is_deleted', False):
                    continue
                all_minutes.append({
                    "task_uuid": item.task_uuid,
                    "title": item.title,
                    "creator_union_id": item.creator_union_id,
                    "creator_nick": item.creator_nick,
                    "start_time": item.start_time,
                    "duration": item.duration_micros // 1000 if item.duration_micros else None,
                    "status": item.status,
                    "biz_type": item.biz_type,
                })

            print(f"    页 {page}: 获取 {len(items)} 条闪记")

            has_next = response.body.has_next
            next_token = response.body.next_token
            if not has_next or not next_token:
                break
            time.sleep(self.API_RATE_LIMIT_SLEEP)

        return all_minutes

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    def _collect_minutes_text(self, task_uuid: str) -> int:
        """获取并写入闪记转录文本"""
        # 先清除旧文本
        self._delete_minutes_text(task_uuid)

        all_paragraphs = []
        next_token = None

        while True:
            request = QueryMinutesTextRequest(
                union_id=self.operator_id,
		max_results=50,		
		next_token=next_token,
            )
            headers = QueryMinutesTextHeaders(
                x_acs_dingtalk_access_token=self._get_token()
            )
            response = self.client.query_minutes_text_with_options(
                task_uuid, request, headers, RuntimeOptions()
            )

            paragraphs = response.body.paragraph_list or []
            for idx, p in enumerate(paragraphs):
                all_paragraphs.append({
                    "task_uuid": task_uuid,
                    "speaker_id": p.union_id,
                    "speaker_nick": p.nick_name,
                    "start_time": p.start_time,
                    "end_time": p.end_time,
                    "text": p.paragraph,
                    "paragraph_index": len(all_paragraphs),
                })

            if not response.body.has_next or not response.body.next_token:
                break
            next_token = response.body.next_token
            time.sleep(self.API_RATE_LIMIT_SLEEP)

        for text_data in all_paragraphs:
            self._insert_minutes_text(text_data)
            self.stats["texts"] += 1

        return len(all_paragraphs)

    def _upsert_minutes(self, item: dict):
        """写入/更新闪记记录"""
        sql = text("""
            INSERT INTO ods_minutes
                (task_uuid, title, creator_id, creator_nick, start_time,
                 duration, status, biz_type, sync_time)
            VALUES
                (:task_uuid, :title, :creator_union_id, :creator_nick, :start_time,
                 :duration, :status, :biz_type, NOW())
            ON DUPLICATE KEY UPDATE
                title = VALUES(title),
                creator_id = VALUES(creator_id),
                creator_nick = VALUES(creator_nick),
                start_time = VALUES(start_time),
                duration = VALUES(duration),
                status = VALUES(status),
                biz_type = VALUES(biz_type),
                sync_time = NOW()
        """)
        self.db.execute(sql, item)
        self.db.commit()

    def _delete_minutes_text(self, task_uuid: str):
        """清除旧转录文本"""
        sql = text("DELETE FROM ods_minutes_text WHERE task_uuid = :uuid")
        self.db.execute(sql, {"uuid": task_uuid})
        self.db.commit()

    def _insert_minutes_text(self, text_data: dict):
        """插入转录文本"""
        sql = text("""
            INSERT INTO ods_minutes_text
                (task_uuid, speaker_id, speaker_nick, start_time,
                 end_time, text, paragraph_index, sync_time)
            VALUES
                (:task_uuid, :speaker_id, :speaker_nick, :start_time,
                 :end_time, :text, :paragraph_index, NOW())
        """)
        self.db.execute(sql, text_data)
        self.db.commit()

    def _print_summary(self):
        """打印采集统计"""
        print("\n" + "=" * 50)
        print("📊 闪记采集统计")
        print(f"  闪记数: {self.stats['minutes']}")
        print(f"  转录文本段数: {self.stats['texts']}")
        print(f"  错误数: {self.stats['errors']}")
        print("=" * 50)


# ===== 测试入口 =====
if __name__ == "__main__":
    import os
    import sys

    print("=" * 50)
    print("闪记采集器测试")
    print("=" * 50)

    operator_id = os.getenv("DINGTALK_OPERATOR_USER_ID", "")

    if not operator_id:
        print("\n❌ 请在 config/.env 中设置 DINGTALK_OPERATOR_USER_ID")
        exit(1)

    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(f"  操作者 unionId: {operator_id}")
    print(f"  查询范围: 最近 {days} 天")

    tm = TokenManager()
    db = get_db_session()

    try:
        collector = MinutesCollector(tm, db, operator_id)
        collector.collect_all(days_back=days)
    except Exception as e:
        print(f"\n❌ 采集失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()
