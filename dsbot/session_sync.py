# -*- coding: utf-8 -*-
"""班级记忆同步：让机器人和 Codex 的「班长助理」会话共用一份记忆。

为什么不是两个数据库对拷？两边的记忆结构根本不一样——机器人这边是「按微信
会话分开的问答对」，Codex 那边是「一份班级底稿 + 每次运行的汇报」。硬把表结构
对齐只会互相覆盖。所以做成一个共享目录，各自只写自己那半边：

    出去（机器人 -> 会话）：机器写，会话读
        dialog.jsonl   逐条追加：哪个会话、谁说的、什么角色、什么内容、什么时候
        dialog.md      同一批数据的摘要版，给人看（会话每次运行先读这个）
        bot-status.md  机器人活着吗、同步到哪儿了
    回来（会话 -> 机器人）：会话写，机器读
        knowledge.md   班级记忆正文（群清单、通知进展、口径……）
        inbox/*.md     会话随手追加的补充记忆，按文件名排序接在 knowledge.md 后面

「实时」到什么程度？机器人这边是真实时：每条消息回复前都会重新看一眼共享目录
（只比 mtime 和大小，很便宜），knowledge.md 一改，下一条回复立刻用上，不用重启。
会话那边做不到被叫醒，它每次运行（默认每天 4 次）先读 dialog.md，所以机器人收到
的东西最多滞后到下一次运行；想让会话马上看到，在会话里说一句「读一下
shared-memory」即可。

隐私闸门：从共享目录读进来的内容同样过一遍敏感信息检查（复用 knowledge.py 的
sensitive_reason）——身份证号、成规模的手机号/学号名单一律不进口，免得会话那边
一不小心把名册同步进来，机器人照着念出去。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .knowledge import sensitive_reason
from .session_memory import mask_personal

KNOWLEDGE_FILE = "knowledge.md"
INBOX_DIR = "inbox"
DIALOG_JSONL = "dialog.jsonl"
DIALOG_MD = "dialog.md"
STATUS_MD = "bot-status.md"
STATE_FILE = "state.json"

READ_EXTS = (".md", ".txt")


def _now_text(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts if ts else time.time()))


def _fingerprint(path: Path) -> Tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (-1, 0)
    # 用纳秒级的 mtime：同一秒里改两次也要认得出（毫秒精度会撞车）
    return (int(st.st_size), int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))))


def _from_tail(text: str, limit: int) -> str:
    """从末尾取不超过 limit 个字，并且只留完整的行（避免把半句话塞给模型）。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    newline = cut.find("\n")
    return cut[newline + 1:] if newline >= 0 else cut


class SessionSync:
    """把机器人记忆和「班长助理」会话的记忆连起来（详见模块开头）。"""

    def __init__(
        self,
        dir_path: Path,
        memory: Any,
        *,
        name_of: Optional[Callable[[str], str]] = None,
        max_chars: int = 4000,
        dialog_keep: int = 200,
        log: Any = None,
        session: Any = None,
    ) -> None:
        self.dir = Path(dir_path)
        self.memory = memory
        self.name_of = name_of or (lambda conv_id: conv_id)
        self.max_chars = max(0, int(max_chars))
        self.dialog_keep = max(1, int(dialog_keep))
        self.log = log
        self.session = session          # 可选：直接读那个会话的记忆（session_memory.py）
        self._lock = threading.RLock()
        self._state: Dict[str, Any] = {}
        self._cache_text = ""
        self._files_text = ""
        self._cache_key: Tuple = ()
        self._last_pull_at = 0.0
        self._last_export_at = 0.0
        self._pulled_files: List[str] = []
        self._refused: List[str] = []

    # ---------------------------------------------------------------- 基础
    @property
    def jsonl_path(self) -> Path:
        return self.dir / DIALOG_JSONL

    @property
    def dialog_path(self) -> Path:
        return self.dir / DIALOG_MD

    @property
    def knowledge_path(self) -> Path:
        return self.dir / KNOWLEDGE_FILE

    @property
    def inbox_path(self) -> Path:
        return self.dir / INBOX_DIR

    def _say(self, level: str, msg: str, *args: Any) -> None:
        if self.log is None:
            return
        try:
            getattr(self.log, level)("[共享记忆] " + msg, *args)
        except Exception:
            pass

    def prepare(self) -> None:
        """建目录、读水位。目录有问题也不能抛——同步坏了不许拖垮机器人。"""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.inbox_path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self._say("warning", "共享目录建不出来（%s）：%r", self.dir, exc)
            return
        self._state = self._read_state()

    def _read_state(self) -> Dict[str, Any]:
        try:
            data = json.loads((self.dir / STATE_FILE).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_state(self) -> None:
        data = dict(self._state)
        data["updated_at"] = time.time()
        data["updated_text"] = _now_text()
        self._state = data
        self._write_text(self.dir / STATE_FILE,
                         json.dumps(data, ensure_ascii=False, indent=2))

    def _write_text(self, path: Path, text: str) -> None:
        try:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(str(tmp), str(path))
        except Exception as exc:
            self._say("debug", "写 %s 失败：%r", path.name, exc)

    # ------------------------------------------------------- 出去：对话记录
    def export(self, force: bool = False) -> int:
        """把新出现的问答追加到共享目录，返回这次导出的条数。"""
        with self._lock:
            if not self.dir.is_dir():
                self.prepare()
            if not self.dir.is_dir():
                return 0
            last_id = int(self._state.get("last_message_id") or 0)
            try:
                rows = self.memory.since(last_id, limit=400)
            except Exception as exc:
                self._say("warning", "读机器人记忆失败：%r", exc)
                return 0
            records: List[Dict[str, Any]] = []
            newest_id = last_id
            for row in rows:
                conv_id = str(row.get("conv_id") or "")
                newest_id = max(newest_id, int(row["id"]))
                # __once__ / __mock__ 这类是自测留下的假对话，别混进给会话看的流水
                if conv_id.startswith("__"):
                    continue
                records.append({
                    "id": int(row["id"]),
                    "ts": float(row["ts"]),
                    "time": _now_text(float(row["ts"])),
                    "conv_id": conv_id,
                    "conv_name": self._safe_name(conv_id),
                    "role": str(row.get("role") or ""),
                    "origin": str(row.get("origin") or "bot"),
                    "text": str(row.get("content") or ""),
                    "group": conv_id.endswith("@chatroom"),
                })
            if not records:
                if newest_id > last_id:
                    self._state["last_message_id"] = newest_id   # 自测那些行也算看过了
                if not self.dialog_path.exists():
                    self._write_dialog_md()
                    self._write_status_md()
                    self._write_state()
                return 0
            try:
                with self.jsonl_path.open("a", encoding="utf-8") as fh:
                    for rec in records:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception as exc:
                self._say("warning", "写 dialog.jsonl 失败：%r", exc)
                return 0
            self._state["last_message_id"] = max(newest_id, records[-1]["id"])
            self._state["exported_total"] = int(self._state.get("exported_total") or 0) + len(records)
            self._last_export_at = time.time()
            self._write_dialog_md()
            self._write_status_md()
            self._write_state()
            self._say("info", "同学侧对话已同步 %d 条（给「班长助理」会话看）", len(records))
            return len(records)

    def _safe_name(self, conv_id: str) -> str:
        try:
            return str(self.name_of(conv_id) or conv_id)
        except Exception:
            return conv_id

    def _read_records(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            with self.jsonl_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
        except FileNotFoundError:
            return []
        except Exception as exc:
            self._say("debug", "读 dialog.jsonl 失败：%r", exc)
        return out

    def _write_dialog_md(self) -> None:
        """把 JSONL 尾部渲染成人看得懂的对话流水（会话运行第一步就读这个）。"""
        records = self._read_records()
        kept = records[-self.dialog_keep:]
        lines = [
            "# 机器人（同学侧）对话同步",
            "",
            "> 本文件由「DeepSeek 微信自动回复」机器人写入；同目录 %s 是同一批数据的机器可读版。" % DIALOG_JSONL,
            "> 机器人替班长回复同学时的「谁 + 问了什么 + 答了什么」都在这里。",
            "> 最后更新 %s，累计 %d 条，下面显示最近 %d 条。" % (
                _now_text(), len(records), len(kept)),
            "",
        ]
        current = ""
        for rec in kept:
            conv = str(rec.get("conv_name") or rec.get("conv_id") or "?")
            if conv != current:
                current = conv
                lines.append("")
                lines.append("## %s" % conv)
                lines.append("")
            role = str(rec.get("role"))
            if role == "assistant" and str(rec.get("origin") or "bot") == "human":
                who = "班长本人回的"        # 自己本人在微信里敲的，不是机器人
            else:
                who = {"user": "对方", "assistant": "机器人（以班长身份）"}.get(role, role)
            text = str(rec.get("text") or "").replace("\r\n", "\n").strip()
            text = "\n".join("    " + seg.strip() for seg in text.split("\n") if seg.strip())
            lines.append("- **%s** %s：%s" % (rec.get("time") or "?", who, text))
        if not kept:
            lines.append("（还没有同步到任何对话。）")
        lines.append("")
        self._write_text(self.dialog_path, "\n".join(lines))

    def _write_status_md(self) -> None:
        stats: Dict[str, Any] = {}
        try:
            stats = self.memory.stats()
        except Exception:
            pass
        lines = [
            "# 机器人同步状态",
            "",
            "| 项目 | 值 |",
            "| --- | --- |",
            "| 最后同步 | %s |" % _now_text(),
            "| 累计同步对话条数 | %s |" % self._state.get("exported_total", 0),
            "| 机器人记忆库 | %s 条、%s 个会话 |" % (
                stats.get("messages", "?"), stats.get("conversations", "?")),
            "| 已读回的班级记忆 | %s |" % ("、".join(self._state.get("pulled_names") or []) or "（还没有）"),
            "| 机器人进程 | PID %s |" % os.getpid(),
            "",
            "机器人把同学侧问答写进本目录；班长助理会话把班级知识写回 %s 和 %s/。" % (
                KNOWLEDGE_FILE, INBOX_DIR),
            "",
        ]
        self._write_text(self.dir / STATUS_MD, "\n".join(lines))

    # --------------------------------------------------- 回来：班级知识记忆
    def pull(self, force: bool = False) -> str:
        """读回班级记忆：会话写下的文件 + 会话自己的对话记录（实时）。

        两部分都做了缓存，每条消息都能放心调：文件只比 mtime/大小，
        会话记录是按字节增量读，都没变时纯内存返回。
        """
        with self._lock:
            if not self.dir.is_dir():
                self.prepare()
            files_text = self._files_text
            if self.dir.is_dir():
                files = self._readable_files()
                key = tuple([str(p)] + list(_fingerprint(p)) for p in files)
                if force or key != self._cache_key:
                    self._cache_key = key
                    files_text = self._read_files(files)
                    self._files_text = files_text
            session_text = ""
            if self.session is not None:
                try:
                    session_text = self.session.poll(force=force)
                except Exception as exc:
                    self._say("warning", "读会话记忆失败：%r", exc)
            parts = [p for p in (files_text, self._session_block(session_text)) if p]
            text = self._budget(files_text, session_text)
            self._cache_text = text
            self._last_pull_at = time.time()
            if self._pulled_files or self._refused:
                self._state["pulled_names"] = self._pulled_files
                self._state["pulled_at"] = self._last_pull_at
                self._write_state()
            if session_text:
                self._state["session_chars"] = len(session_text)
                self._write_state()
            return text

    def _read_files(self, files: List[Path]) -> str:
        blocks: List[str] = []
        names: List[str] = []
        refused: List[str] = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except Exception as exc:
                self._say("debug", "读共享记忆 %s 失败：%r", path.name, exc)
                continue
            if not text:
                continue
            why = sensitive_reason(text)
            if why:
                refused.append("%s（%s）" % (path.name, why))
                continue
            names.append(path.name)
            blocks.append(mask_personal(text))     # 会话写的东西也照样脱敏
        self._refused = refused
        self._pulled_files = names
        if refused:
            self._say("warning", "共享记忆里有敏感内容，已拦截不进口：%s", "；".join(refused))
        return "\n\n---\n\n".join(blocks)

    @staticmethod
    def _session_block(session_text: str) -> str:
        if not session_text:
            return ""
        return ("【班长助理会话最近的记忆】（机器人直接从那个会话的记录里读的，实时）\n"
                + session_text)

    def _budget(self, files_text: str, session_text: str) -> str:
        """把两份记忆拼起来，超预算时先砍文件那半截（它更新慢），会话那半留最新的。

        会话记忆是「越靠后越新」，所以截断要砍前面、留后面 —— 直接切尾巴会把
        最新聊的东西切掉，那正是最要紧的。
        """
        limit = self.max_chars
        if not limit:
            return "\n\n---\n\n".join(p for p in (files_text, self._session_block(session_text)) if p)
        block = self._session_block(session_text)
        if files_text and block:
            files_share = int(limit * 0.45)
            if len(files_text) > files_share:
                files_text = files_text[:files_share].rstrip() + "\n…（班级记忆较长，已截断）"
            room = max(600, limit - len(files_text))
            if len(block) > room:
                head, _, body = block.partition("\n")
                body = _from_tail(body, room - len(head) - 20)
                block = head + "\n…（更早的略）\n" + body
        elif block and len(block) > limit:
            head, _, body = block.partition("\n")
            block = head + "\n…（更早的略）\n" + _from_tail(body, limit - len(head) - 20)
        elif files_text and len(files_text) > limit:
            files_text = files_text[:limit].rstrip() + "\n…（班级记忆较长，已截断）"
        return "\n\n---\n\n".join(p for p in (files_text, block) if p)

    def _readable_files(self) -> List[Path]:
        """knowledge.md 在前，inbox/ 里的按文件名排序在后。"""
        files: List[Path] = []
        if self.knowledge_path.is_file():
            files.append(self.knowledge_path)
        try:
            for path in sorted(self.inbox_path.iterdir()):
                if (path.is_file() and path.suffix.lower() in READ_EXTS
                        and not path.name.startswith(".")):
                    files.append(path)
        except FileNotFoundError:
            pass
        except Exception as exc:
            self._say("debug", "列 inbox 失败：%r", exc)
        return files

    # ---------------------------------------------------------------- 运维
    def rebuild(self) -> int:
        """清掉已同步的对话记录，从头再推一遍（水位也重置）。"""
        with self._lock:
            for path in (self.jsonl_path, self.dir / STATE_FILE):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    self._say("warning", "删不掉 %s：%r", path.name, exc)
            self._state = {}
            return self.export(force=True)

    def status(self) -> Dict[str, Any]:
        size = _fingerprint(self.knowledge_path)[0] if self.knowledge_path.is_file() else -1
        st: Dict[str, Any] = {
            "dir": str(self.dir),
            "exists": self.dir.is_dir(),
            "last_message_id": int(self._state.get("last_message_id") or 0),
            "exported_total": int(self._state.get("exported_total") or 0),
            "last_export": self._last_export_at or float(self._state.get("updated_at") or 0),
            "knowledge": ("knowledge.md %d 字节" % size) if size >= 0 else "（还没有 knowledge.md）",
            "pulled": self._pulled_files,
            "refused": self._refused,
        }
        if self.session is not None:
            st["session"] = self.session.status()
        return st
