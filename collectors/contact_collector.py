"""
组织通讯录采集器
职责：
- 递归获取组织部门树（topapi/v2/department/listsub）
- 获取每个部门的用户列表（topapi/v2/user/list）
- 写入部门表、用户表、用户-部门关联表
API:
- 企业内部旧版 API（oapi.dingtalk.com），需要 AccessToken
- 需要在钉钉开放平台申请「通讯录管理」权限
"""
import time
import json
import requests
from typing import Optional
from sqlalchemy import text
from sqlalchemy.orm import Session
from tenacity import retry, stop_after_attempt, wait_exponential
from utils.token_manager import TokenManager
from utils.db import get_db_session
class ContactCollector:
    """组织通讯录采集器"""
    BASE_URL = "https://oapi.dingtalk.com"
    PAGE_SIZE = 100
    API_RATE_LIMIT_SLEEP = 0.5
    def __init__(self, token_manager: TokenManager, db: Session):
        self.token_manager = token_manager
        self.db = db
        self.stats = {
            "departments": 0,
            "users": 0,
            "user_dept_links": 0,
            "errors": 0,
        }
    def _get_token(self) -> str:
        return self.token_manager.get_access_token()
    def collect_all(self, root_dept_id: int = 1):
        """
        完整采集流程
        :param root_dept_id: 根部门 ID，默认为 1（企业根部门）
        """
        print("\n🏢 开始组织通讯录采集")
        print("=" * 50)
        # Step 1: 递归采集部门树
        print("  📂 采集部门树 ...")
        self._collect_departments_recursive(root_dept_id)
        print(f"  共采集 {self.stats['departments']} 个部门")
        # Step 2: 对每个部门采集用户
        print("\n  👤 采集部门用户 ...")
        dept_ids = self._get_all_dept_ids()
        print(f"  需要处理 {len(dept_ids)} 个部门的用户")
        for dept_id in dept_ids:
            users = self._list_dept_users(dept_id)
            if users:
                for user in users:
                    self._upsert_user(user)
                    self.stats["users"] += 1
                    # 写入用户-部门关联
                    self._upsert_user_dept(user["userid"], dept_id)
                    self.stats["user_dept_links"] += 1
            time.sleep(self.API_RATE_LIMIT_SLEEP)
        self._print_summary()
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    def _list_sub_departments(self, dept_id: int) -> list[dict]:
        """获取子部门列表"""
        url = f"{self.BASE_URL}/topapi/v2/department/listsub?access_token={self._get_token()}"
        payload = {"dept_id": dept_id}
        response = requests.post(url, json=payload, timeout=30)
        data = response.json()
        if data.get("errcode") != 0:
            raise Exception(f"获取子部门失败: {data.get('errmsg', '未知错误')} (dept_id={dept_id})")
        return data.get("result", [])
    def _collect_departments_recursive(self, dept_id: int, depth: int = 0):
        """递归采集部门树"""
        indent = "    " + "  " * depth
        try:
            sub_depts = self._list_sub_departments(dept_id)
        except Exception as e:
            print(f"{indent}⚠ 获取子部门失败: {e}")
            self.stats["errors"] += 1
            return
        for dept in sub_depts:
            did = dept.get("dept_id")
            name = dept.get("name", "未知")
            parent_id = dept.get("parent_id", 0)
            dept_data = {
                "dept_id": did,
                "dept_name": name,
                "parent_id": parent_id,
                "dept_group": 1 if dept.get("dept_group") else 0,
                "auto_add_user": 1 if dept.get("auto_add_user") else 0,
                "auto_approve_apply": 1 if dept.get("auto_approve_apply") else 0,
                "from_union_org": 1 if dept.get("from_union_org") else 0,
                "resource_permission": 1 if dept.get("resource_permission") else 0,
            }
            self._upsert_department(dept_data)
            self.stats["departments"] += 1
            print(f"{indent}📁 {name} (ID: {did})")
            # 递归子部门
            time.sleep(self.API_RATE_LIMIT_SLEEP)
            self._collect_departments_recursive(did, depth + 1)
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    def _list_dept_users(self, dept_id: int) -> list[dict]:
        """获取部门下的用户列表（分页）"""
        all_users = []
        cursor = 0
        while True:
            url = f"{self.BASE_URL}/topapi/v2/user/list?access_token={self._get_token()}"
            payload = {
                "dept_id": dept_id,
                "cursor": cursor,
                "size": self.PAGE_SIZE,
            }
            response = requests.post(url, json=payload, timeout=30)
            data = response.json()
            if data.get("errcode") != 0:
                raise Exception(f"获取部门用户失败: {data.get('errmsg')} (dept_id={dept_id})")
            result = data.get("result", {})
            users = result.get("list", [])
            for u in users:
                all_users.append({
                    "userid": u.get("userid"),
                    "unionid": u.get("unionid"),
                    "name": u.get("name"),
                    "avatar": u.get("avatar"),
                    "mobile": u.get("mobile"),
                    "hide_mobile": 1 if u.get("hide_mobile") else 0,
                    "telephone": u.get("telephone"),
                    "job_number": u.get("job_number"),
                    "title": u.get("title"),
                    "email": u.get("email"),
                    "org_email": u.get("org_email"),
                    "work_place": u.get("work_place"),
                    "active": 1 if u.get("active") else 0,
                    "admin": 1 if u.get("admin") else 0,
                    "boss": 1 if u.get("boss") else 0,
                    "leader": 1 if u.get("leader") else 0,
                    "state_code": u.get("state_code"),
                    "manager_userid": u.get("manager_userid"),
                    "dept_id_list": json.dumps(u.get("dept_id_list", [])),
                })
            if not result.get("has_more", False):
                break
            cursor = result.get("next_cursor", 0)
            time.sleep(self.API_RATE_LIMIT_SLEEP)
        return all_users
    def _get_all_dept_ids(self) -> list[int]:
        """从数据库获取所有已采集的部门 ID"""
        result = self.db.execute(text("SELECT dept_id FROM ods_org_department"))
        return [row[0] for row in result]
    def _upsert_department(self, dept: dict):
        """写入/更新部门记录"""
        sql = text("""
            INSERT INTO ods_org_department
                (dept_id, dept_name, parent_id, dept_group, auto_add_user,
                 auto_approve_apply, from_union_org, resource_permission, sync_time)
            VALUES
                (:dept_id, :dept_name, :parent_id, :dept_group, :auto_add_user,
                 :auto_approve_apply, :from_union_org, :resource_permission, NOW())
            ON DUPLICATE KEY UPDATE
                dept_name = VALUES(dept_name),
                parent_id = VALUES(parent_id),
                dept_group = VALUES(dept_group),
                auto_add_user = VALUES(auto_add_user),
                auto_approve_apply = VALUES(auto_approve_apply),
                from_union_org = VALUES(from_union_org),
                resource_permission = VALUES(resource_permission),
                sync_time = NOW()
        """)
        self.db.execute(sql, dept)
        self.db.commit()
    def _upsert_user(self, user: dict):
        """写入/更新用户记录"""
        sql = text("""
            INSERT INTO ods_contact_user
                (userid, unionid, name, avatar, mobile, hide_mobile,
                 telephone, job_number, title, email, org_email,
                 work_place, active, admin, boss, leader,
                 state_code, manager_userid, dept_id_list, sync_time)
            VALUES
                (:userid, :unionid, :name, :avatar, :mobile, :hide_mobile,
                 :telephone, :job_number, :title, :email, :org_email,
                 :work_place, :active, :admin, :boss, :leader,
                 :state_code, :manager_userid, :dept_id_list, NOW())
            ON DUPLICATE KEY UPDATE
                unionid = VALUES(unionid),
                name = VALUES(name),
                avatar = VALUES(avatar),
                mobile = VALUES(mobile),
                hide_mobile = VALUES(hide_mobile),
                telephone = VALUES(telephone),
                job_number = VALUES(job_number),
                title = VALUES(title),
                email = VALUES(email),
                org_email = VALUES(org_email),
                work_place = VALUES(work_place),
                active = VALUES(active),
                admin = VALUES(admin),
                boss = VALUES(boss),
                leader = VALUES(leader),
                state_code = VALUES(state_code),
                manager_userid = VALUES(manager_userid),
                dept_id_list = VALUES(dept_id_list),
                sync_time = NOW()
        """)
        self.db.execute(sql, user)
        self.db.commit()
    def _upsert_user_dept(self, userid: str, dept_id: int):
        """写入/更新用户-部门关联"""
        sql = text("""
            INSERT IGNORE INTO ods_contact_user_dept
                (userid, dept_id, sync_time)
            VALUES (:userid, :dept_id, NOW())
        """)
        self.db.execute(sql, {"userid": userid, "dept_id": dept_id})
        self.db.commit()
    def _print_summary(self):
        """打印采集统计"""
        print("\n" + "=" * 50)
        print("📊 组织通讯录采集统计")
        print(f"  部门数: {self.stats['departments']}")
        print(f"  用户记录数: {self.stats['users']}")
        print(f"  用户-部门关联数: {self.stats['user_dept_links']}")
        print(f"  错误数: {self.stats['errors']}")
        print("=" * 50)
# ===== 测试入口 =====
if __name__ == "__main__":
    import os
    print("=" * 50)
    print("组织通讯录采集器测试")
    print("=" * 50)
    tm = TokenManager()
    db = get_db_session()
    try:
        collector = ContactCollector(tm, db)
        collector.collect_all()
    except Exception as e:
        print(f"\n❌ 采集失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()
