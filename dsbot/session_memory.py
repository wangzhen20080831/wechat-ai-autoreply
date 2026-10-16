# -*- coding: utf-8 -*-
"""直接读 Codex 会话的记忆：机器人自己去看「班长助理」那个会话说了什么。

上一版靠会话主动把知识写进 shared-memory\knowledge.md —— 那需要它动手，而且
只在它跑的时候才更新。这一版直接读那个会话自己的记录文件（rollout jsonl），
所以会话里聊了什么、它最后的结论是什么，机器人这边几秒内就能看到，不用等它
写文件，也不用等它的定时任务。

记录文件：%USERPROFILE%\.codex\sessions\<年>\<月>\<日>\rollout-<时间>-<thread_id>.jsonl
每一行一条事件。我们要的就两类：
    response_item / message      role=user    → 我（班长）对会话说的
    event_msg / task_complete    last_agent_message → 会话这一轮最后的结论
剩下的是工具调用、截图、token 统计，量很大但没用，直接跳过。

两个必须注意的点：
1. 定时任务（心跳）也会往会话里塞一条「用户消息」——那其实是自动注入的提示词，
   不是班长说的。用 task_started 里的 turn_trigger 区分：不是 composer 的轮次
   就不收用户消息，只收会话的结论。
2. 会话的汇报里可能出现同学的身份证号、手机号（真的出现过）。这些一律在进提示词
   之前脱敏，成规模的名册整条丢掉 —— 机器人是拿去替班长回同学的，绝不能漏出去。

读的时候是「按字节增量读」：记住上次读到哪儿，之后每次只读新增的那一小段，
所以 30 多 MB 的记录文件也能每 15 秒看一眼，几乎不花钱。
"""

from __future__ import annotations

import calendar
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .knowledge import sensitive_reason

DEFAULT_SESSIONS_ROOT = Path(os.path.expanduser("~")) / ".codex" / "sessions"

# 会话自己的提示词注入，不是班长说的话
# （«会话自己的固定开场白»要按你那边实际的提示词补，填在 config.json 的
#   sync.session_skip_user 里 —— 这里只放项目自带的、跟具体会话无关的那几条。）
USER_NOISE_MARKERS = (
    "<environment_context>",
    "# Files mentioned by the user:",
    "<app-context>",
    "<multi_agent_mode>",
    "本次是定时检查微信",
)

HEARTBEAT_RE = re.compile(r"<heartbeat>\s*(.*?)\s*</heartbeat>", re.S)
HEARTBEAT_MSG_RE = re.compile(r"<message>(.*?)</message>", re.S)
HEARTBEAT_DECISION_RE = re.compile(r"<decision>(.*?)</decision>", re.S)

MASK_RULES: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "（身份证号已隐去）"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "（手机号已隐去）"),
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "（长号码已隐去）"),
    (re.compile(r"(?<!\d)\d{6}\s?\d{8}(?!\d)"), "（证件号已隐去）"),
)

# [标题](C:\很长的\路径.md) -> 标题：路径对回答同学没用，白占字数
MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((?:[^)\n]+)\)")


def tidy_report(text: str) -> str:
    """把会话汇报里对同学没用的东西压掉（文件路径、空行、行尾空格）。"""
    out = MD_LINK_RE.sub(r"\1", text or "")
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def mask_personal(text: str) -> str:
    """把身份证号、手机号、银行卡这类号码从文本里抹掉（保留句子结构）。"""
    out = text or ""
    for pattern, replacement in MASK_RULES:
        out = pattern.sub(replacement, out)
    return out


def _strip_heartbeat(text: str) -> Tuple[str, str]:
    """会话的定时汇报是 <heartbeat> XML，取出人能看的那句和它的决定。"""
    m = HEARTBEAT_RE.search(text or "")
    if not m:
        return text.strip(), ""
    inner = m.group(1)
    msg = HEARTBEAT_MSG_RE.search(inner)
    decision = HEARTBEAT_DECISION_RE.search(inner)
    head = (text or "")[:m.start()].strip()
    body = (msg.group(1).strip() if msg else inner.strip())
    if head:
        body = head + "\n" + body
    return body.strip(), (decision.group(1).strip() if decision else "")


class SessionMemoryReader:
    """盯着某个 Codex 会话的记录文件，增量读出「会话最近的记忆」。"""

    def __init__(
        self,
        thread_id: str,
        *,
        sessions_root: Optional[Path] = None,
        max_chars: int = 3000,
        items: int = 12,
        tail_mb: float = 3.0,
        skip_user: Tuple[str, ...] = (),
        log: Any = None,
    ) -> None:
        self.thread_id = str(thread_id or "").strip()
        self.sessions_root = Path(sessions_root or DEFAULT_SESSIONS_ROOT)
        self.max_chars = max(0, int(max_chars))
        self.items = max(1, int(items))
        self.tail_bytes = max(1, int(float(tail_mb) * 1024 * 1024))
        self.skip_user = tuple(skip_user) + USER_NOISE_MARKERS
        self.log = log
        self._lock = threading.RLock()
        self._path: Optional[Path] = None
        self._offset = 0
        self._carry = ""
        self._entries: List[Dict[str, Any]] = []
        self._turn_trigger: Dict[str, str] = {}
        self._turn_user: Dict[str, str] = {}
        self._cache_key: Tuple = ()
        self._cache_text = ""
        self._stats = {"lines": 0, "dropped": 0, "masked": 0}

    # ---------------------------------------------------------------- 文件
    @property
    def enabled(self) -> bool:
        return bool(self.thread_id)

    def _say(self, level: str, msg: str, *args: Any) -> None:
        if self.log is None:
            return
        try:
            getattr(self.log, level)("[会话记忆] " + msg, *args)
        except Exception:
            pass

    def find_rollout(self, refresh: bool = False) -> Optional[Path]:
        """那个会话的记录文件在哪（按 thread_id 找，取最新的一个）。"""
        if self._path is not None and not refresh and self._path.is_file():
            return self._path
        best: Optional[Path] = None
        try:
            for path in self.sessions_root.glob("*/*/*/rollout-*.jsonl"):
                if self.thread_id in path.name:
                    if best is None or path.stat().st_mtime > best.stat().st_mtime:
                        best = path
        except Exception as exc:
            self._say("debug", "扫 sessions 目录失败：%r", exc)
        if best is not None:
            if self._path is None or best != self._path:
                self._say("info", "读到会话记录：%s", best)
            self._path = best
        return best

    # ---------------------------------------------------------------- 读取
    def poll(self, force: bool = False) -> str:
        """看一眼有没有新内容，返回给提示词用的那段「会话最近的记忆」。"""
        with self._lock:
            if not self.enabled:
                return ""
            path = self.find_rollout()
            if path is None:
                return self._cache_text
            try:
                size = path.stat().st_size
            except OSError:
                return self._cache_text
            if size < self._offset:              # 文件被换掉/截断了，从头尾部重来
                self._say("info", "会话记录文件变了（%d -> %d 字节），重新读尾段", self._offset, size)
                self._offset = 0
                self._carry = ""
                self._entries.clear()
            start = self._offset
            if start == 0:
                start = max(0, size - self.tail_bytes)
            if size <= start and self._entries:
                return self._rendered()
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(start)
                    chunk = fh.read()
            except Exception as exc:
                self._say("warning", "读会话记录失败：%r", exc)
                return self._rendered()
            self._offset = size
            if start:                            # 从头截断的位置可能落在半行上
                newline = chunk.find("\n")
                chunk = chunk[newline + 1:] if newline >= 0 else ""
            self._consume(self._carry + chunk)
            return self._rendered()

    def _consume(self, blob: str) -> None:
        lines = blob.split("\n")
        self._carry = lines.pop()                # 最后一段可能还没写完，留到下次
        added = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            self._stats["lines"] += 1
            try:
                rec = json.loads(line)
            except Exception:
                continue
            added += self._one(rec)
        if added:
            self._cache_key = ()
            self._say("info", "会话记忆 +%d 条（共 %d 条）", added, len(self._entries))

    def _one(self, rec: Dict[str, Any]) -> int:
        kind = str(rec.get("type") or "")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        ptype = str(payload.get("type") or "")
        ts = self._ts(rec.get("timestamp"))

        if kind == "event_msg" and ptype == "task_started":
            turn = str(payload.get("turn_id") or "")
            trigger = ""
            attribution = payload.get("turn_attribution")
            if isinstance(attribution, dict):
                trigger = str(attribution.get("turn_trigger") or "")
            trigger = trigger or str(payload.get("turn_trigger") or "")
            if turn:
                self._turn_trigger[turn] = trigger
            return 0

        if kind == "response_item" and ptype == "message" and payload.get("role") == "user":
            text = "".join(str(c.get("text") or "") for c in (payload.get("content") or [])
                           if isinstance(c, dict) and str(c.get("type") or "").endswith("text"))
            text = text.strip()
            if not text:
                return 0
            meta = payload.get("internal_chat_message_metadata_passthrough")
            turn = ""
            if isinstance(meta, dict):
                turn = str(meta.get("turn_id") or "")
            trigger = self._turn_trigger.get(turn, "")
            if trigger and trigger != "composer":
                return 0                         # 定时任务自动塞进来的提示词，不是班长说的
            if any(marker in text for marker in self.skip_user):
                return 0
            if turn:
                self._turn_user[turn] = text
            self._add("user", text, ts)
            return 1

        if kind == "event_msg" and ptype == "task_complete":
            text = str(payload.get("last_agent_message") or "").strip()
            if not text:
                return 0
            body, decision = _strip_heartbeat(text)
            meta: Dict[str, Any] = {}
            if decision:
                meta["decision"] = decision
            self._add("agent", body, ts, meta)
            return 1
        return 0

    def _add(self, role: str, text: str, ts: float, meta: Optional[Dict[str, Any]] = None) -> None:
        # 先抹号码、再判整条要不要丢：一条汇报里顺带提了某个同学的身份证号，抹掉号码
        # 之后剩下的内容（谁干了什么）还是有用的；只有抹完还像一整份名册的才整条不要。
        masked = tidy_report(mask_personal(text))
        if masked != text:
            self._stats["masked"] += 1
        why = sensitive_reason(masked)
        if why:
            self._stats["dropped"] += 1
            self._say("warning", "会话记忆里有成规模的隐私内容（%s），这条整条不收", why)
            return
        entry = {"role": role, "text": masked, "ts": ts}
        if meta:
            entry.update(meta)
        self._entries.append(entry)
        if len(self._entries) > 400:
            del self._entries[:-200]

    @staticmethod
    def _ts(raw: Any) -> float:
        if isinstance(raw, (int, float)):
            return float(raw)
        text = str(raw or "")
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                # 记录里的时间戳是 UTC（带 Z），按 UTC 解出 epoch 才是对的
                return float(calendar.timegm(time.strptime(text, fmt)))
            except Exception:
                continue
        return time.time()

    # ---------------------------------------------------------------- 渲染
    def _rendered(self) -> str:
        key = (len(self._entries), self._entries[-1]["ts"] if self._entries else 0,
               self._entries[-1]["text"][:40] if self._entries else "")
        if key == self._cache_key:
            return self._cache_text
        self._cache_key = key
        recent = self._entries[-self.items:]
        lines: List[str] = []
        for entry in recent:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(entry["ts"]))
            who = "我（班长）" if entry["role"] == "user" else "班长助理"
            text = entry["text"].strip().replace("\r\n", "\n")
            if len(text) > 500:
                text = text[:500].rstrip() + "…"
            extra = ""
            if entry.get("decision"):
                extra = "（本轮判定：%s）" % entry["decision"]
            text = "\n   ".join(text.split("\n"))
            lines.append("- [%s] %s%s：%s" % (stamp, who, extra, text))
        text = "\n".join(lines)
        if self.max_chars and len(text) > self.max_chars:
            text = text[-self.max_chars:]           # 留最近的
            text = text[text.find("\n") + 1:] if "\n" in text else text
        self._cache_text = text
        return text

    def status(self) -> Dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "file": str(self._path or ""),
            "offset": self._offset,
            "entries": len(self._entries),
            "lines": self._stats["lines"],
            "masked": self._stats["masked"],
            "dropped": self._stats["dropped"],
        }
