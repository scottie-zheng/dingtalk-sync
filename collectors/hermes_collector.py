"""
Hermes Agent 数据提取器
职责：
- 从 Hermes 的 state.db (SQLite) 提取会话、消息、工具调用记录
- 解析 assistant 消息中的 tool_calls JSON
- 写入 MySQL ODS 层
- 支持增量同步（基于 session.started_at）
数据源：~/.hermes/state.db
"""
import json
import os
import sqlite3
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.orm import Session as DBSession
from utils.db import get_db_session
# Hermes state.db 路径
HERMES_DB_PATH = os.path.expanduser("~/.hermes/state.db")
class HermesCollector:
    """Hermes 数据提取器"""
    def __init__(self, db: DBSession, hermes_db_path: str = None):
        self.db = db
        self.hermes_db_path = hermes_db_path or HERMES_DB_PATH
        self.stats = {
            "sessions": 0,
            "messages": 0,
            "tool_calls": 0,
            "errors": 0,
        }
    def collect_all(self, source_filter: str = None, since: str = None):
        """
        完整采集流程
        :param source_filter: 只采集指定平台，如 'dingtalk', 'cli'，None=全部
        :param since: 只采集此时间之后的会话，格式 'YYYY-MM-DD'，None=全部
        """
        print(f"\n🤖 开始 Hermes 数据提取")
        print("=" * 50)
        print(f"  数据源: {self.hermes_db_path}")
        if not os.path.exists(self.hermes_db_path):
            print(f"  ❌ 文件不存在: {self.hermes_db_path}")
            return
        # 连接 Hermes SQLite
        hermes_conn = sqlite3.connect(self.hermes_db_path)
        hermes_conn.row_factory = sqlite3.Row
        try:
            # Step 1: 提取会话
            sessions = self._extract_sessions(hermes_conn, source_filter, since)
            print(f"  发现 {len(sessions)} 个会话")
            for session in sessions:
                session_id = session["session_id"]
                title = session.get("title", "无标题")[:40]
                source = session.get("source", "unknown")
                print(f"\n  💬 [{source}] {title} ({session_id[:20]}...)")
                # 写入会话
                self._upsert_session(session)
                self.stats["sessions"] += 1
                # Step 2: 提取消息
                messages = self._extract_messages(hermes_conn, session_id)
                print(f"    📝 {len(messages)} 条消息")
                # 先清除旧消息和工具调用
                self._delete_messages(session_id)
                self._delete_tool_calls(session_id)
                for msg in messages:
                    self._insert_message(msg)
                    self.stats["messages"] += 1
                    # Step 3: 从 assistant 消息解析工具调用
                    if msg["msg_role"] == "assistant" and msg.get("tool_calls_raw"):
                        tool_calls = self._parse_tool_calls(
                            msg["tool_calls_raw"],
                            session_id,
                            msg["timestamp"]
                        )
                        if tool_calls:
                            for tc in tool_calls:
                                self._insert_tool_call(tc)
                                self.stats["tool_calls"] += 1
                            print(f"    🔧 {len(tool_calls)} 次工具调用")
        finally:
            hermes_conn.close()
        self._print_summary()
    def _extract_sessions(self, conn, source_filter, since) -> list[dict]:
        """从 SQLite 提取会话列表"""
        query = "SELECT * FROM sessions WHERE 1=1"
        params = []
        if source_filter:
            query += " AND source = ?"
            params.append(source_filter)
        if since:
            # 转换为 Unix timestamp
            since_ts = datetime.strptime(since, "%Y-%m-%d").timestamp()
            query += " AND started_at >= ?"
            params.append(since_ts)
        query += " ORDER BY started_at DESC"
        rows = conn.execute(query, params).fetchall()
        sessions = []
        for row in rows:
            started_at = datetime.fromtimestamp(row["started_at"]) if row["started_at"] else None
            ended_at = datetime.fromtimestamp(row["ended_at"]) if row["ended_at"] else None
            sessions.append({
                "session_id": row["id"],
                "source": row["source"],
                "user_id": row["user_id"],
                "title": row["title"],
                "model": row["model"],
                "parent_session_id": row["parent_session_id"],
                "started_at": started_at,
                "ended_at": ended_at,
                "end_reason": row["end_reason"],
                "message_count": row["message_count"],
                "tool_call_count": row["tool_call_count"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_read_tokens": row["cache_read_tokens"],
                "cache_write_tokens": row["cache_write_tokens"],
                "reasoning_tokens": row["reasoning_tokens"],
                "cwd": row["cwd"],
                "billing_provider": row["billing_provider"],
                "billing_mode": row["billing_mode"],
                "estimated_cost_usd": row["estimated_cost_usd"],
                "actual_cost_usd": row["actual_cost_usd"],
                "api_call_count": row["api_call_count"],
                "rewind_count": row["rewind_count"],
                "archived": row["archived"],
            })
        return sessions
    def _extract_messages(self, conn, session_id: str) -> list[dict]:
        """从 SQLite 提取某会话的所有消息"""
        query = """
            SELECT * FROM messages
            WHERE session_id = ?
            ORDER BY timestamp ASC
        """
        rows = conn.execute(query, (session_id,)).fetchall()
        messages = []
        for row in rows:
            ts = datetime.fromtimestamp(row["timestamp"]) if row["timestamp"] else None
            messages.append({
                "session_id": session_id,
                "msg_role": row["role"],
                "content": row["content"],
                "tool_name": row["tool_name"],
                "tool_call_id": row["tool_call_id"],
                "tool_calls_raw": row["tool_calls"],  # JSON string
                "timestamp": ts,
                "token_count": row["token_count"],
                "finish_reason": row["finish_reason"],
                "platform_message_id": row["platform_message_id"],
                "observed": 1 if row["observed"] else 0,
                "active": 1 if row["active"] else 0,
            })
        return messages
    def _parse_tool_calls(self, tool_calls_json: str, session_id: str, ts) -> list[dict]:
        """解析 assistant 消息中的 tool_calls JSON"""
        if not tool_calls_json:
            return []
        try:
            calls = json.loads(tool_calls_json)
        except json.JSONDecodeError:
            self.stats["errors"] += 1
            return []
        result = []
        for call in calls:
            func = call.get("function", {})
            result.append({
                "session_id": session_id,
                "call_id": call.get("call_id") or call.get("id", ""),
                "tool_type": call.get("type", "function"),
                "function_name": func.get("name", ""),
                "arguments": func.get("arguments", ""),
                "timestamp": ts,
            })
        return result
    def _upsert_session(self, session: dict):
        """写入/更新会话"""
        sql = text("""
            INSERT INTO ods_hermes_session
                (session_id, source, user_id, title, model, parent_session_id,
                 started_at, ended_at, end_reason, message_count, tool_call_count,
                 input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                 reasoning_tokens, cwd, billing_provider, billing_mode,
                 estimated_cost_usd, actual_cost_usd, api_call_count,
                 rewind_count, archived, sync_time)
            VALUES
                (:session_id, :source, :user_id, :title, :model, :parent_session_id,
                 :started_at, :ended_at, :end_reason, :message_count, :tool_call_count,
                 :input_tokens, :output_tokens, :cache_read_tokens, :cache_write_tokens,
                 :reasoning_tokens, :cwd, :billing_provider, :billing_mode,
                 :estimated_cost_usd, :actual_cost_usd, :api_call_count,
                 :rewind_count, :archived, NOW())
            ON DUPLICATE KEY UPDATE
                source = VALUES(source),
                user_id = VALUES(user_id),
                title = VALUES(title),
                model = VALUES(model),
                parent_session_id = VALUES(parent_session_id),
                started_at = VALUES(started_at),
                ended_at = VALUES(ended_at),
                end_reason = VALUES(end_reason),
                message_count = VALUES(message_count),
                tool_call_count = VALUES(tool_call_count),
                input_tokens = VALUES(input_tokens),
                output_tokens = VALUES(output_tokens),
                cache_read_tokens = VALUES(cache_read_tokens),
                cache_write_tokens = VALUES(cache_write_tokens),
                reasoning_tokens = VALUES(reasoning_tokens),
                cwd = VALUES(cwd),
                billing_provider = VALUES(billing_provider),
                billing_mode = VALUES(billing_mode),
                estimated_cost_usd = VALUES(estimated_cost_usd),
                actual_cost_usd = VALUES(actual_cost_usd),
                api_call_count = VALUES(api_call_count),
                rewind_count = VALUES(rewind_count),
                archived = VALUES(archived),
                sync_time = NOW()
        """)
        self.db.execute(sql, session)
        self.db.commit()
    def _delete_messages(self, session_id: str):
        """清除会话的旧消息"""
        sql = text("DELETE FROM ods_hermes_message WHERE session_id = :sid")
        self.db.execute(sql, {"sid": session_id})
        self.db.commit()
    def _insert_message(self, msg: dict):
        """插入消息"""
        sql = text("""
            INSERT INTO ods_hermes_message
                (session_id, msg_role, content, tool_name, tool_call_id,
                 timestamp, token_count, finish_reason,
                 platform_message_id, observed, active, sync_time)
            VALUES
                (:session_id, :msg_role, :content, :tool_name, :tool_call_id,
                 :timestamp, :token_count, :finish_reason,
                 :platform_message_id, :observed, :active, NOW())
        """)
        self.db.execute(sql, msg)
        self.db.commit()
    def _delete_tool_calls(self, session_id: str):
        """清除会话的旧工具调用记录"""
        sql = text("DELETE FROM ods_hermes_tool_call WHERE session_id = :sid")
        self.db.execute(sql, {"sid": session_id})
        self.db.commit()
    def _insert_tool_call(self, tc: dict):
        """插入工具调用记录"""
        sql = text("""
            INSERT INTO ods_hermes_tool_call
                (session_id, call_id, tool_type, function_name,
                 arguments, timestamp, sync_time)
            VALUES
                (:session_id, :call_id, :tool_type, :function_name,
                 :arguments, :timestamp, NOW())
        """)
        self.db.execute(sql, tc)
        self.db.commit()
    def _print_summary(self):
        """打印采集统计"""
        print("\n" + "=" * 50)
        print("📊 Hermes 数据提取统计")
        print(f"  会话数: {self.stats['sessions']}")
        print(f"  消息数: {self.stats['messages']}")
        print(f"  工具调用数: {self.stats['tool_calls']}")
        print(f"  错误数: {self.stats['errors']}")
        print("=" * 50)
# ===== 测试入口 =====
if __name__ == "__main__":
    import sys
    print("=" * 50)
    print("Hermes 数据提取器测试")
    print("=" * 50)
    # 参数解析
    source_filter = None
    since = None
    for arg in sys.argv[1:]:
        if arg.startswith("source="):
            source_filter = arg.split("=", 1)[1]
        elif arg.startswith("since="):
            since = arg.split("=", 1)[1]
    if source_filter:
        print(f"  平台过滤: {source_filter}")
    if since:
        print(f"  时间过滤: {since} 之后")
    db = get_db_session()
    try:
        collector = HermesCollector(db)
        collector.collect_all(source_filter=source_filter, since=since)
    except Exception as e:
        print(f"\n❌ 提取失败: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()
