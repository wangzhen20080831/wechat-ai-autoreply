# -*- coding: utf-8 -*-
"""对话记忆：每个会话一条独立的上下文，存在 SQLite 里。

要点：
  * 按会话隔离——A 的上下文绝对不会漏到 B 那里去。
  * 重启不丢——Bot 崩了/关机了，重新起来还记得之前聊过什么。
  * 会过期——超过 ttl_hours 没有互动的会话，下次开口前记忆自动清空，
    免得半个月后突然接着旧话题聊，露出马脚。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class Memory:
    def __init__(self, db_path: Path, max_turns: int = 12, ttl_hours: int = 72) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_turns = max(1, int(max_turns))
        self.ttl_seconds = max(0, int(ttl_hours)) * 3600
        self._lock = threading.RLock()
        # Listener 的回调在后台线程里跑，所以关掉同线程检查，自己在外面加锁
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS messages (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    conv_id  TEXT    NOT NULL,
                    role     TEXT    NOT NULL,
                    content  TEXT    NOT NULL,
                    ts       REAL    NOT NULL,
                    -- origin: bot=机器人自己回的；human=班长本人在微信里手动回的
                    origin   TEXT    NOT NULL DEFAULT 'bot'
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, id);
                CREATE TABLE IF NOT EXISTS conv_state (
                    conv_id       TEXT PRIMARY KEY,
                    last_reply_at REAL NOT NULL DEFAULT 0,
                    last_user_at  REAL NOT NULL DEFAULT 0
                );
                -- 生成失败、还没发出去的消息。一个会话只留一行，再失败就往里并，
                -- 这样补回来的时候是一条完整的话，而不是攒成一串碎回复。
                CREATE TABLE IF NOT EXISTS pending (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    conv_id     TEXT    NOT NULL,
                    conv_name   TEXT    NOT NULL DEFAULT '',
                    sender_name TEXT    NOT NULL DEFAULT '',
                    is_group    INTEGER NOT NULL DEFAULT 0,
                    body        TEXT    NOT NULL,
                    created_at  REAL    NOT NULL,
                    attempts    INTEGER NOT NULL DEFAULT 0,
                    last_try_at REAL    NOT NULL DEFAULT 0,
                    reason      TEXT    NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_pending_conv ON pending(conv_id, id);
                """
            )
            self._conn.commit()
            self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """老记忆库补新列（SQLite 没有 ADD COLUMN IF NOT EXISTS，只能自己看）。"""
        try:
            cols = {str(r["name"]) for r in self._conn.execute("PRAGMA table_info(messages)")}
            if "origin" not in cols:
                self._conn.execute(
                    "ALTER TABLE messages ADD COLUMN origin TEXT NOT NULL DEFAULT 'bot'")
                self._conn.commit()
        except Exception:
            pass

    # ------------------------------------------------------------ 上下文
    def history(self, conv_id: str, limit_turns: Optional[int] = None) -> List[Dict[str, str]]:
        """返回最近 limit_turns 轮（一轮 = 一问一答）的上下文，老的在前。"""
        turns = max(1, int(limit_turns or self.max_turns))
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, ts FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT ?",
                (conv_id, turns * 2),
            ).fetchall()
        if not rows:
            return []
        if self.ttl_seconds and time.time() - float(rows[0]["ts"]) > self.ttl_seconds:
            self.clear(conv_id)          # 太久没说话，当作新对话
            return []
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def append(self, conv_id: str, role: str, content: str,
               ts: Optional[float] = None, origin: str = "bot") -> None:
        """记一条。

        ``ts`` 只在补历史时用（把当时真发生的时间写对），平时不传。
        ``origin`` = bot（机器人回的）/ human（班长本人在微信里手动回的），
        同步给「班长助理」会话时靠它把"谁说的"分开写。
        """
        now = float(ts if ts is not None else time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages(conv_id, role, content, ts, origin) VALUES(?,?,?,?,?)",
                (conv_id, role, content, now, origin or "bot"),
            )
            self._conn.execute(
                """
                INSERT INTO conv_state(conv_id, last_reply_at, last_user_at) VALUES(?,?,?)
                ON CONFLICT(conv_id) DO UPDATE SET
                    last_reply_at = CASE WHEN ?='assistant' THEN ? ELSE last_reply_at END,
                    last_user_at  = CASE WHEN ?='user'      THEN ? ELSE last_user_at  END
                """,
                (conv_id, 0.0, 0.0, role, now, role, now),
            )
            self._conn.commit()
        self.prune(conv_id)

    def prune(self, conv_id: str, keep_messages: Optional[int] = None) -> None:
        keep = max(2, int(keep_messages or self.max_turns) * 2)
        with self._lock:
            self._conn.execute(
                """
                DELETE FROM messages
                WHERE conv_id=? AND id NOT IN (
                    SELECT id FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT ?
                )
                """,
                (conv_id, conv_id, keep),
            )
            self._conn.commit()

    def clear(self, conv_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE conv_id=?", (conv_id,))
            self._conn.commit()

    def clear_all(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages")
            self._conn.execute("DELETE FROM conv_state")
            self._conn.commit()

    def drop_message(self, conv_id: str, role: str, content: str) -> int:
        """删掉某会话里**最新的一条**指定角色、正文完全一致的记录，返回删了几条。

        用途：AI 已经生成好、但最后没发出去的那句（例如你抢在它前面自己回了），
        留在记忆里会让下次对话以为"我说过这句话"。按正文精确匹配删除，不会误伤。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM messages WHERE conv_id=? AND role=? AND content=? "
                "ORDER BY id DESC LIMIT 1",
                (conv_id, role, content),
            ).fetchone()
            if not row:
                return 0
            self._conn.execute("DELETE FROM messages WHERE id=?", (row["id"],))
            self._conn.commit()
            return 1

    def has_message(self, conv_id: str, role: str, content: str) -> bool:
        """这个会话里有没有一模一样的这一条（补历史时用来防重复）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM messages WHERE conv_id=? AND role=? AND content=? LIMIT 1",
                (conv_id, role, content),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------ 冷却
    def last_reply_at(self, conv_id: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_reply_at FROM conv_state WHERE conv_id=?", (conv_id,)
            ).fetchone()
        return float(row["last_reply_at"]) if row else 0.0

    def last_user_at(self, conv_id: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_user_at FROM conv_state WHERE conv_id=?", (conv_id,)
            ).fetchone()
        return float(row["last_user_at"]) if row else 0.0

    def mark_reply(self, conv_id: str, ts: Optional[float] = None) -> None:
        now = float(ts if ts is not None else time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conv_state(conv_id, last_reply_at, last_user_at) VALUES(?,?,0)
                ON CONFLICT(conv_id) DO UPDATE SET last_reply_at=excluded.last_reply_at
                """,
                (conv_id, now),
            )
            self._conn.commit()

    # ------------------------------------------------------------ 运维
    def stats(self) -> Dict[str, int]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
            convs = self._conn.execute(
                "SELECT COUNT(DISTINCT conv_id) AS c FROM messages"
            ).fetchone()["c"]
            pending = self._conn.execute("SELECT COUNT(*) AS c FROM pending").fetchone()["c"]
        return {"messages": int(total), "conversations": int(convs), "pending": int(pending)}

    # -------------------------------------------------- 没发出去的（待补回）
    def add_pending(self, conv_id: str, body: str, *, conv_name: str = "",
                    sender_name: str = "", is_group: bool = False,
                    reason: str = "", now: Optional[float] = None) -> int:
        """记一条「生成失败、欠着对方」的消息，返回这一行的 id。

        同一个会话只保留一行：已经有待办就把新内容并进去（逐行去重，免得重试时
        把同一句话叠好几遍）。attempts 记累计失败次数。
        """
        body = (body or "").strip()
        if not body:
            return 0
        now = float(now if now is not None else time.time())
        with self._lock:
            row = self._conn.execute(
                "SELECT id, body FROM pending WHERE conv_id=? ORDER BY id LIMIT 1", (conv_id,)
            ).fetchone()
            if row:
                kept = [ln for ln in str(row["body"]).split("\n") if ln.strip()]
                new = [ln for ln in body.split("\n") if ln.strip() and ln not in kept]
                merged = "\n".join(kept + new) if new else str(row["body"])
                self._conn.execute(
                    """
                    UPDATE pending SET body=?, attempts=attempts+1, last_try_at=?, reason=?
                    WHERE id=?
                    """,
                    (merged, now, str(reason)[:300], row["id"]),
                )
                self._conn.commit()
                return int(row["id"])
            cur = self._conn.execute(
                """
                INSERT INTO pending(conv_id, conv_name, sender_name, is_group, body,
                                    created_at, attempts, last_try_at, reason)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (conv_id, conv_name, sender_name, 1 if is_group else 0, body,
                 now, 1, now, str(reason)[:300]),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def list_pending(self, conv_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        sql = ("SELECT id, conv_id, conv_name, sender_name, is_group, body, created_at, "
               "attempts, last_try_at, reason FROM pending")
        args: List[Any] = []
        if conv_id:
            sql += " WHERE conv_id=?"
            args.append(conv_id)
        sql += " ORDER BY id ASC LIMIT ?"
        args.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def drop_pending(self, ids: Any) -> int:
        """删掉（已经补回来了 / 太旧不要了的）待办。"""
        if isinstance(ids, int):
            ids = [ids]
        ids = [int(i) for i in (ids or []) if i]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        with self._lock:
            cur = self._conn.execute("DELETE FROM pending WHERE id IN (%s)" % marks, ids)
            self._conn.commit()
        return int(cur.rowcount or 0)

    def touch_pending(self, pid: int, reason: str = "", now: Optional[float] = None) -> None:
        """重投一次（还没成功），更新尝试次数和时间。"""
        with self._lock:
            self._conn.execute(
                "UPDATE pending SET attempts=attempts+1, last_try_at=?, reason=? WHERE id=?",
                (float(now if now is not None else time.time()), str(reason)[:300], int(pid)),
            )
            self._conn.commit()

    def since(self, last_id: int = 0, limit: int = 500) -> List[Dict[str, Any]]:
        """按 id 递增取消息（给同步/导出用：只看比 last_id 新的那些）。

        注意 id 是 AUTOINCREMENT，但 prune() 会删旧行 —— 所以这个水位是「导出过
        的最大 id」，删掉的那部分本来也已经导出去了。
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, conv_id, role, content, ts, origin FROM messages
                WHERE id > ? ORDER BY id ASC LIMIT ?
                """,
                (int(last_id), max(1, int(limit))),
            ).fetchall()
        return [
            {"id": int(r["id"]), "conv_id": r["conv_id"], "role": r["role"],
             "content": r["content"], "ts": float(r["ts"]),
             "origin": (r["origin"] if "origin" in r.keys() else "bot")}
            for r in rows
        ]
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()
