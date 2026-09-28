"""
知识库元数据采集器
职责：
- 获取所有知识库列表（list_org_workspaces）
- 获取每个知识库详情（get_workspace）
- 递归遍历文档树（list_nodes）
- 将元数据写入 RDS ODS 层
SDK:
- alibabacloud_dingtalk.wiki_2_0 — 知识库管理
"""
import time
from datetime import datetime
from typing import Optional
from alibabacloud_dingtalk.wiki_2_0.client import Client as WikiClient
from alibabacloud_dingtalk.wiki_2_0.models import (
    ListOrgWorkspacesRequest,
    ListOrgWorkspacesHeaders,
    GetWorkspaceRequest,
    GetWorkspaceHeaders,
    ListNodesRequest,
    ListNodesHeaders,
)
from alibabacloud_tea_openapi.models import Config as OpenApiConfig
from alibabacloud_tea_util.models import RuntimeOptions
from sqlalchemy import text
from sqlalchemy.orm import Session
from tenacity import retry, stop_after_attempt, wait_exponential
from utils.token_manager import TokenManager
from utils.db import get_db_session
def parse_datetime(dt_str: Optional[str]) -> Optional[str]:
    """将 ISO 8601 日期转换为 MySQL DATETIME 格式"""
    if not dt_str:
        return None
    try:
        # 处理多种格式：2026-08-17T16:46Z, 2026-08-17T16:46:00Z, etc.
        dt_str = dt_str.replace('Z', '+00:00')
        if 'T' in dt_str:
            # ISO 8601 格式
            dt = datetime.fromisoformat(dt_str)
            return dt.strftime('%Y-%m-%d %H:%M:%S')
        else:
            # 已经是标准格式
            return dt_str
    except Exception:
        return None
class WikiCollector:
    """知识库元数据采集器"""
    PAGE_SIZE = 50
    API_RATE_LIMIT_SLEEP = 0.5
    def __init__(self, token_manager: TokenManager, db: Session, operator_id: str):
        self.token_manager = token_manager
        self.db = db
        self.operator_id = operator_id
        config = OpenApiConfig()
        config.protocol = "https"
        config.region_id = "central"
        self.client = WikiClient(config)
        self.stats = {"workspaces": 0, "nodes": 0, "errors": 0}
    def _get_token(self) -> str:
        return self.token_manager.get_access_token()
    def collect_all(self):
        """完整采集流程"""
        print("\n📚 开始知识库元数据采集")
        print("=" * 50)
        workspaces = self._list_all_workspaces()
        print(f"  发现 {len(workspaces)} 个知识库")
        for ws in workspaces:
            print(f"\n  📂 {ws['name']} (ID: {ws['workspace_id']})")
            # 获取知识库详情
            ws_detail = self._get_workspace_detail(ws["workspace_id"])
            if ws_detail:
                ws.update(ws_detail)
                print(f"    ✅ 详情获取成功: root_node={ws.get('root_node_id')}")
            else:
                if not ws.get("root_node_id") and ws.get("root_dentry_uuid"):
                    ws["root_node_id"] = ws["root_dentry_uuid"]
                print(f"    ℹ️ 详情获取失败，使用基础信息继续")
            # 写入数据库
            self._upsert_workspace(ws)
            self.stats["workspaces"] += 1
            # 递归遍历文档树
            root_node_id = ws.get("root_node_id") or ws.get("root_dentry_uuid")
            if root_node_id:
                self._collect_nodes_recursive(root_node_id, ws["workspace_id"])
            else:
                print(f"    ⚠ 未找到 root_node_id，跳过文档遍历")
        self._print_summary()
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    def _list_all_workspaces(self) -> list[dict]:
        """分页获取所有知识库列表"""
        all_workspaces = []
        page = 1
        while True:
            request = ListOrgWorkspacesRequest(
                operator_id=self.operator_id,
                page_number=page,
                page_size=self.PAGE_SIZE,
            )
            headers = ListOrgWorkspacesHeaders(
                x_acs_dingtalk_access_token=self._get_token()
            )
            response = self.client.list_org_workspaces_with_options(
                request, headers, RuntimeOptions()
            )
            workspaces = response.body.workspaces or []
            for ws in workspaces:
                all_workspaces.append({
                    "workspace_id": ws.workspace_id,
                    "name": ws.workspace_name,
                    "description": None,
                    "creator_id": None,
                    "create_time": parse_datetime(ws.create_time),
                    "modified_time": None,
                    "root_node_id": None,
                    "workspace_type": None,
                    "url": ws.url,
                    "team_id": None,
                    "status": str(ws.status) if ws.status is not None else None,
                    "root_dentry_uuid": ws.root_dentry_uuid,
                })
            print(f"    页 {page}: 获取 {len(workspaces)} 个知识库")
            if len(workspaces) < self.PAGE_SIZE:
                break
            page += 1
            time.sleep(self.API_RATE_LIMIT_SLEEP)
        return all_workspaces
    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _get_workspace_detail(self, workspace_id: str) -> Optional[dict]:
        """获取知识库详细信息"""
        try:
            request = GetWorkspaceRequest(
                operator_id=self.operator_id,
            )
            headers = GetWorkspaceHeaders(
                x_acs_dingtalk_access_token=self._get_token()
            )
            response = self.client.get_workspace_with_options(
                workspace_id, request, headers, RuntimeOptions()
            )
            ws = response.body.workspace
            if ws:
                return {
                    "creator_id": ws.creator_id,
                    "modified_time": parse_datetime(ws.modified_time),
                    "modifier_id": ws.modifier_id,
                    "description": ws.description,
                    "root_node_id": ws.root_node_id,
                    "workspace_type": ws.type,
                    "team_id": ws.team_id,
                }
        except Exception as e:
            print(f"    ⚠ 获取知识库详情失败: {e}")
        return None
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=15))
    def _list_nodes(self, parent_node_id: str, next_token: Optional[str] = None) -> tuple:
        """获取子节点列表（单页）"""
        request = ListNodesRequest(
            operator_id=self.operator_id,
            parent_node_id=parent_node_id,
            max_results=50,
            next_token=next_token,
        )
        headers = ListNodesHeaders(
            x_acs_dingtalk_access_token=self._get_token()
        )
        response = self.client.list_nodes_with_options(
            request, headers, RuntimeOptions()
        )
        nodes = response.body.nodes or []
        return nodes, response.body.next_token
    def _collect_nodes_recursive(self, parent_node_id: str, workspace_id: str, depth: int = 0):
        """递归遍历文档树"""
        indent = "    " + "  " * depth
        next_token = None
        while True:
            try:
                nodes, next_token = self._list_nodes(parent_node_id, next_token)
            except Exception as e:
                print(f"{indent}⚠ 遍历节点 {parent_node_id} 失败: {e}")
                self.stats["errors"] += 1
                return
            for node in nodes:
                node_data = {
                    "node_id": node.node_id,
                    "workspace_id": workspace_id,
                    "parent_node_id": parent_node_id,
                    "name": node.name,
                    "node_type": node.type,
                    "category": node.category,
                    "creator_id": node.creator_id,
                    "create_time": parse_datetime(node.create_time),
                    "create_timestamp": node.create_timestamp,
                    "modifier_id": node.modifier_id,
                    "modified_time": parse_datetime(node.modified_time),
                    "modified_timestamp": node.modified_timestamp,
                    "has_children": 1 if node.has_children else 0,
                    "file_size": node.size,
                    "extension": node.extension,
                    "url": node.url,
                }
                self._upsert_node(node_data)
                self.stats["nodes"] += 1
                type_icon = "📁" if node.has_children else "📄"
                print(f"{indent}{type_icon} {node.name} ({node.type})")
                if node.has_children:
                    time.sleep(self.API_RATE_LIMIT_SLEEP)
                    self._collect_nodes_recursive(
                        node.node_id, workspace_id, depth + 1
                    )
            if not next_token:
                break
            time.sleep(self.API_RATE_LIMIT_SLEEP)
    def _upsert_workspace(self, ws: dict):
        """写入/更新知识库记录"""
        sql = text("""
            INSERT INTO ods_wiki_workspace
                (workspace_id, workspace_name, description, creator_id,
                 create_time, modified_time, root_node_id, workspace_type,
                 url, team_id, status, sync_time)
            VALUES
                (:workspace_id, :name, :description, :creator_id,
                 :create_time, :modified_time, :root_node_id, :workspace_type,
                 :url, :team_id, :status, NOW())
            ON DUPLICATE KEY UPDATE
                workspace_name = VALUES(workspace_name),
                description = VALUES(description),
                creator_id = VALUES(creator_id),
                create_time = VALUES(create_time),
                modified_time = VALUES(modified_time),
                root_node_id = VALUES(root_node_id),
                workspace_type = VALUES(workspace_type),
                url = VALUES(url),
                team_id = VALUES(team_id),
                status = VALUES(status),
                sync_time = NOW()
        """)
        self.db.execute(sql, ws)
        self.db.commit()
    def _upsert_node(self, node: dict):
        """写入/更新文档节点记录"""
        sql = text("""
            INSERT INTO ods_wiki_node
                (node_id, workspace_id, parent_node_id, name, node_type,
                 category, creator_id, create_time, create_timestamp,
                 modifier_id, modified_time, modified_timestamp,
                 has_children, file_size, extension, url, sync_time)
            VALUES
                (:node_id, :workspace_id, :parent_node_id, :name, :node_type,
                 :category, :creator_id, :create_time, :create_timestamp,
                 :modifier_id, :modified_time, :modified_timestamp,
                 :has_children, :file_size, :extension, :url, NOW())
            ON DUPLICATE KEY UPDATE
                workspace_id = VALUES(workspace_id),
                parent_node_id = VALUES(parent_node_id),
                name = VALUES(name),
                node_type = VALUES(node_type),
                category = VALUES(category),
                creator_id = VALUES(creator_id),
                create_time = VALUES(create_time),
                create_timestamp = VALUES(create_timestamp),
                modifier_id = VALUES(modifier_id),
                modified_time = VALUES(modified_time),
                modified_timestamp = VALUES(modified_timestamp),
                has_children = VALUES(has_children),
                file_size = VALUES(file_size),
                extension = VALUES(extension),
                url = VALUES(url),
                sync_time = NOW()
        """)
        self.db.execute(sql, node)
        self.db.commit()
    def _print_summary(self):
        """打印采集统计"""
        print("\n" + "=" * 50)
        print("📊 知识库采集统计")
        print(f"  知识库数: {self.stats['workspaces']}")
        print(f"  文档节点数: {self.stats['nodes']}")
        print(f"  错误数: {self.stats['errors']}")
        print("=" * 50)
# ===== 测试入口 =====
if __name__ == "__main__":
    import os
    print("=" * 50)
    print("知识库元数据采集器测试")
    print("=" * 50)
    operator_id = os.getenv("DINGTALK_OPERATOR_USER_ID", "")
    if not operator_id:
        print("\n❌ 请在 config/.env 中设置 DINGTALK_OPERATOR_USER_ID")
        exit(1)
    print(f"  操作者 unionId: {operator_id}")
    tm = TokenManager()
    db = get_db_session()
    try:
        collector = WikiCollector(tm, db, operator_id)
        collector.collect_all()
    except Exception as e:
        print(f"\n❌ 采集失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()
