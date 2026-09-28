"""
会议记录采集器
职责：
- 获取历史会议列表（get_history_conf_data_list）
- 获取每个会议的详细信息（query_conference_info）
- 获取每个会议的参会成员（query_conference_members）
- 将元数据写入 RDS ODS 层
SDK:
- alibabacloud_dingtalk.conference_1_0 — 会议管理
"""
import time
from typing import Optional
from alibabacloud_dingtalk.conference_1_0.client import Client as ConferenceClient
from alibabacloud_dingtalk.conference_1_0.models import (
    GetHistoryConfDataListRequest,
    GetHistoryConfDataListHeaders,
    QueryConferenceInfoHeaders,
    QueryConferenceMembersRequest,
    QueryConferenceMembersHeaders,
)
from alibabacloud_tea_openapi.models import Config as OpenApiConfig
from alibabacloud_tea_util.models import RuntimeOptions
from sqlalchemy import text
from sqlalchemy.orm import Session
from tenacity import retry, stop_after_attempt, wait_exponential
from utils.token_manager import TokenManager
from utils.db import get_db_session
class ConferenceCollector:
    """会议记录采集器"""
    PAGE_SIZE = 50
    API_RATE_LIMIT_SLEEP = 0.5
    DEFAULT_DAYS_BACK = 30
    def __init__(self, token_manager: TokenManager, db: Session, operator_id: str):
        self.token_manager = token_manager
        self.db = db
        self.operator_id = operator_id
        config = OpenApiConfig()
        config.protocol = "https"
        config.region_id = "central"
        self.client = ConferenceClient(config)
        self.stats = {
            "conferences": 0,
            "members": 0,
            "errors": 0,
        }
    def _get_token(self) -> str:
        return self.token_manager.get_access_token()
    def collect_all(self, days_back: int = None):
        """
        完整采集流程
        :param days_back: 查询最近多少天的会议，默认 30 天
        """
        if days_back is None:
            days_back = self.DEFAULT_DAYS_BACK
        print(f"\n📅 开始会议记录采集（最近 {days_back} 天）")
        print("=" * 50)
        # Step 1: 获取历史会议列表
        conferences = self._list_history_conferences(days_back)
        print(f"  发现 {len(conferences)} 场会议")
        for conf in conferences:
            conf_id = conf["conference_id"]
            title = conf.get("title", "无标题")
            print(f"\n  📹 {title} (ID: {conf_id[:12]}...)")
            # Step 2: 获取会议详情
            detail = self._get_conference_detail(conf_id)
            if detail:
                conf.update(detail)
                print(f"    ✅ 详情获取成功")
            else:
                print(f"    ℹ️ 详情获取失败，使用列表信息")
            # Step 3: 写入会议记录
            self._upsert_conference(conf)
            self.stats["conferences"] += 1
            # Step 4: 获取参会成员
            members = self._get_conference_members(conf_id)
            if members:
                print(f"    👥 {len(members)} 位参会者")
                self._delete_conference_members(conf_id)
                for member in members:
                    member["conference_id"] = conf_id
                    self._insert_conference_member(member)
                    self.stats["members"] += 1
            else:
                print(f"    ⚠ 未获取到参会成员")
            time.sleep(self.API_RATE_LIMIT_SLEEP)
        self._print_summary()
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    def _list_history_conferences(self, days_back: int) -> list[dict]:
        """获取历史会议列表（分页）"""
        all_conferences = []
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - (days_back * 24 * 3600 * 1000)
        next_token = None
        page = 0
        while True:
            page += 1
            request = GetHistoryConfDataListRequest(
                start_time=start_ms,
                end_time=now_ms,
                max_results=self.PAGE_SIZE,
                next_token=next_token,
 		real_data=False,
            )
            headers = GetHistoryConfDataListHeaders(
                x_acs_dingtalk_access_token=self._get_token()
            )
            response = self.client.get_history_conf_data_list_with_options(
                request, headers, RuntimeOptions()
            )
            conf_list = response.body.list or []
            for conf in conf_list:
                all_conferences.append({
                    "conference_id": conf.conference_id,
                    "title": conf.title,
                    "creator_id": conf.creator_id,
                    "creator_nick": conf.creator_nick,
                    "dept_name": conf.dept_name,
                    "scene": conf.scene,
                    "start_time": conf.start_time,
                    "end_time": conf.end_time,
                    "time_length": conf.time_length,
                    "user_count": conf.user_count,
                    "free_type": conf.free_type,
                })
            print(f"    页 {page}: 获取 {len(conf_list)} 场会议")
            next_token = response.body.next_token
            if not next_token:
                break
            time.sleep(self.API_RATE_LIMIT_SLEEP)
        return all_conferences
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _get_conference_detail(self, conference_id: str) -> Optional[dict]:
        """获取会议详细信息"""
        try:
            headers = QueryConferenceInfoHeaders(
                x_acs_dingtalk_access_token=self._get_token()
            )
            response = self.client.query_conference_info_with_options(
                conference_id, headers, RuntimeOptions()
            )
            info = response.body.conf_info
            if info:
                return {
                    "conf_duration": info.conf_duration,
                    "status": info.status,
                    "room_code": info.room_code,
                    "minutes_status": info.minutes_status,
                    "minutes_owner_union_id": info.minutes_owner_union_id,
                    "cloud_record_status": info.cloud_record_status,
                    "cloud_record_owner_union_id": info.cloud_record_owner_union_id,
                    "external_link_url": info.external_link_url,
                }
        except Exception as e:
            print(f"    ⚠ 获取会议详情失败: {e}")
        return None
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    def _get_conference_members(self, conference_id: str) -> list[dict]:
        """获取会议参会成员（分页）"""
        all_members = []
        next_token = None
        while True:
            request = QueryConferenceMembersRequest(
                max_results=50,
                next_token=next_token,
            )
            headers = QueryConferenceMembersHeaders(
                x_acs_dingtalk_access_token=self._get_token()
            )
            response = self.client.query_conference_members_with_options(
                conference_id, request, headers, RuntimeOptions()
            )
            members = response.body.member_models or []
            for m in members:
                all_members.append({
                    "union_id": m.union_id,
                    "user_nick": m.user_nick,
                    "job_number": m.job_number,
                    "attend_status": m.attend_status,
                    "join_time": m.join_time,
                    "leave_time": m.leave_time,
                    "duration": m.duration,
                    "host": 1 if m.host else 0,
                    "co_host": 1 if m.co_host else 0,
                    "outer_org_member": 1 if m.outer_org_member else 0,
                    "pstn_join": 1 if m.pstn_join else 0,
                })
            next_token = response.body.next_token
            if not next_token:
                break
            time.sleep(self.API_RATE_LIMIT_SLEEP)
        return all_members
    def _upsert_conference(self, conf: dict):
        """写入/更新会议记录"""
        sql = text("""
            INSERT INTO ods_conference
                (conference_id, title, creator_id, creator_nick, dept_name,
                 scene, start_time, end_time, time_length, user_count,
                 free_type, conf_duration, status, room_code,
                 minutes_status, minutes_owner_union_id,
                 cloud_record_status, cloud_record_owner_union_id,
                 external_link_url, sync_time)
            VALUES
                (:conference_id, :title, :creator_id, :creator_nick, :dept_name,
                 :scene, :start_time, :end_time, :time_length, :user_count,
                 :free_type, :conf_duration, :status, :room_code,
                 :minutes_status, :minutes_owner_union_id,
                 :cloud_record_status, :cloud_record_owner_union_id,
                 :external_link_url, NOW())
            ON DUPLICATE KEY UPDATE
                title = VALUES(title),
                creator_id = VALUES(creator_id),
                creator_nick = VALUES(creator_nick),
                dept_name = VALUES(dept_name),
                scene = VALUES(scene),
                start_time = VALUES(start_time),
                end_time = VALUES(end_time),
                time_length = VALUES(time_length),
                user_count = VALUES(user_count),
                free_type = VALUES(free_type),
                conf_duration = VALUES(conf_duration),
                status = VALUES(status),
                room_code = VALUES(room_code),
                minutes_status = VALUES(minutes_status),
                minutes_owner_union_id = VALUES(minutes_owner_union_id),
                cloud_record_status = VALUES(cloud_record_status),
                cloud_record_owner_union_id = VALUES(cloud_record_owner_union_id),
                external_link_url = VALUES(external_link_url),
                sync_time = NOW()
        """)
        self.db.execute(sql, conf)
        self.db.commit()
    def _delete_conference_members(self, conference_id: str):
        """清除会议的旧成员记录"""
        sql = text("DELETE FROM ods_conference_member WHERE conference_id = :cid")
        self.db.execute(sql, {"cid": conference_id})
        self.db.commit()
    def _insert_conference_member(self, member: dict):
        """插入会议成员记录"""
        sql = text("""
            INSERT INTO ods_conference_member
                (conference_id, union_id, user_nick, job_number,
                 attend_status, join_time, leave_time, duration,
                 host, co_host, outer_org_member, pstn_join, sync_time)
            VALUES
                (:conference_id, :union_id, :user_nick, :job_number,
                 :attend_status, :join_time, :leave_time, :duration,
                 :host, :co_host, :outer_org_member, :pstn_join, NOW())
        """)
        self.db.execute(sql, member)
        self.db.commit()
    def _print_summary(self):
        """打印采集统计"""
        print("\n" + "=" * 50)
        print("📊 会议记录采集统计")
        print(f"  会议数: {self.stats['conferences']}")
        print(f"  参会成员记录数: {self.stats['members']}")
        print(f"  错误数: {self.stats['errors']}")
        print("=" * 50)
# ===== 测试入口 =====
if __name__ == "__main__":
    import os
    import sys
    print("=" * 50)
    print("会议记录采集器测试")
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
        collector = ConferenceCollector(tm, db, operator_id)
        collector.collect_all(days_back=days)
    except Exception as e:
        print(f"\n❌ 采集失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()
