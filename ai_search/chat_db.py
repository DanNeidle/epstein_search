# © Dan Neidle and Tax Policy Associates 2026
import json
from typing import Any

import streamlit as st

from ai_search.auth_db import get_db_connection
from ai_search.config import MAX_TITLE_LEN


def _safe_json_loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def init_chat_db() -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_calls_json TEXT NOT NULL DEFAULT '[]',
                downloads_json TEXT NOT NULL DEFAULT '[]',
                cost_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id)
            )
            """
        )


def create_conversation(user_id: int, title: str = "New chat") -> int:
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO conversations (user_id, title)
            VALUES (?, ?)
            """,
            (user_id, title[:MAX_TITLE_LEN]),
        )
        last_id = cursor.lastrowid
        if last_id is None:
            row = conn.execute("SELECT last_insert_rowid() AS id").fetchone()
            if row is None:
                raise RuntimeError("Failed to create conversation.")
            last_id = int(row["id"])
    return int(last_id)


def list_conversations(user_id: int) -> list[dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, title, created_at, updated_at
            FROM conversations
            WHERE user_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (user_id,),
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def conversation_belongs_to_user(conversation_id: int, user_id: int) -> bool:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
    return row is not None


def load_conversation_messages(conversation_id: int) -> list[dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT role, content, tool_calls_json, downloads_json, cost_json
            FROM conversation_messages
            WHERE conversation_id = ?
            ORDER BY id ASC
            """,
            (conversation_id,),
        ).fetchall()
    messages = []
    for row in rows:
        entry: dict[str, Any] = {"role": row["role"], "content": row["content"]}
        tool_calls_raw = _safe_json_loads(row["tool_calls_json"], [])
        downloads_raw = _safe_json_loads(row["downloads_json"], [])
        cost_raw = _safe_json_loads(row["cost_json"], {})
        tool_calls = tool_calls_raw if isinstance(tool_calls_raw, list) else []
        downloads = downloads_raw if isinstance(downloads_raw, list) else []
        cost = cost_raw if isinstance(cost_raw, dict) else {}
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if downloads:
            entry["downloads"] = downloads
        if cost:
            entry["cost"] = cost
        messages.append(entry)
    return messages


def update_conversation_title_if_default(conversation_id: int, prompt: str) -> None:
    title = prompt.strip().replace("\n", " ")
    if not title:
        return
    new_title = title[:MAX_TITLE_LEN]
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT title FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row and row["title"] == "New chat":
            conn.execute(
                "UPDATE conversations SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_title, conversation_id),
            )


def save_conversation_message(
    conversation_id: int,
    role: str,
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    downloads: list[dict[str, Any]] | None = None,
    cost: dict[str, Any] | None = None,
) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO conversation_messages (
                conversation_id, role, content, tool_calls_json, downloads_json, cost_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                role,
                content,
                json.dumps(tool_calls or []),
                json.dumps(downloads or []),
                json.dumps(cost or {}),
            ),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conversation_id,),
        )


def delete_conversation(conversation_id: int, user_id: int) -> bool:
    if not conversation_belongs_to_user(conversation_id, user_id):
        return False
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM conversation_messages WHERE conversation_id = ?",
            (conversation_id,),
        )
        cursor = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )
    return cursor.rowcount > 0


def reset_chat_state() -> None:
    st.session_state.messages = []
    st.session_state.chat_session = None
    st.session_state.chat_client = None
    st.session_state.chat_api_key = None
    st.session_state.chat_max_loops = None
    st.session_state.doc_id_to_source_path = {}
    st.session_state.current_conversation_id = None
