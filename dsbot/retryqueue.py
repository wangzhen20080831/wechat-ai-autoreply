# -*- coding: utf-8 -*-
"""欠着没回的消息：生成失败时先记下来，稍后自己补回，补不上也不能装作没看见。

为什么需要这一层：DeepSeek 在高峰时段会超时或返回 503。老做法是「失败就丢掉」，
同学那边看到的是一句不回 —— 他以为班长不理他。新做法是：

    生成失败 → 记成「待办」（写进记忆库，重启也不丢，一个会话只留一行）
             → 20 秒后自动重来一次（最多 max_retries 次）
             → 还是不行，就交给后台每 2 分钟扫一遍，到点接着试
             → 期间同学又说新话，两条会并成一条回复，一次说清
             → 超过 max_age_hours 还没补上，才彻底放弃（日志里写明）

重试节奏是「越失败越慢」，免得服务商挂了的时候疯狂打接口。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional


class RetryQueue:
    def __init__(
        self,
        memory: Any,
        *,
        retry_delay: float = 20.0,
        max_retries: int = 2,
        flush_seconds: float = 120.0,
        max_age_hours: float = 2.0,
        max_rows: int = 40,
        deliver: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
        log: Any = None,
        timer: Optional[Callable[..., Any]] = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.memory = memory
        self.retry_delay = max(1.0, float(retry_delay))
        self.max_retries = max(0, int(max_retries))
        self.flush_seconds = max(10.0, float(flush_seconds))
        self.max_age_seconds = max(0.0, float(max_age_hours) * 3600.0)
        self.max_rows = max(1, int(max_rows))
        self.deliver = deliver
        self.log = log
        self._timer = timer or (lambda sec, fn, args=(): threading.Timer(sec, fn, args=args))
        self._now = now_fn
        self._lock = threading.RLock()
        self._delivered = 0
        self._given_up = 0

    # ---------------------------------------------------------------- 日志
    def _say(self, level: str, msg: str, *args: Any) -> None:
        if self.log is None:
            return
        try:
            getattr(self.log, level)("[补回] " + msg, *args)
        except Exception:
            pass

    # ------------------------------------------------------------ 记一笔
    def note_failure(self, conv_id: str, body: str, *, conv_name: str = "",
                     sender_name: str = "", is_group: bool = False,
                     reason: str = "") -> int:
        """生成失败时调用：记下来，并决定要不要马上再试一次。"""
        with self._lock:
            self.memory.add_pending(conv_id, body, conv_name=conv_name,
                                    sender_name=sender_name, is_group=is_group,
                                    reason=reason, now=self._now())
            rows = self.memory.list_pending(conv_id=conv_id, limit=1)
            attempts = int(rows[0]["attempts"]) if rows else 1
            if attempts <= self.max_retries:
                self._say("warning", "「%s」这条先欠着，%.0f 秒后自动重试（第 %d 次）",
                          conv_name or conv_id, self.retry_delay, attempts)
                self._schedule_retry(conv_id)
            else:
                self._say("error",
                          "「%s」已经失败 %d 次，挂进待办，之后每 %.0f 秒自己试；"
                          "超过 %.0f 小时还没成功才放弃",
                          conv_name or conv_id, attempts, self.flush_seconds,
                          self.max_age_seconds / 3600.0)
            return attempts

    def _schedule_retry(self, conv_id: str) -> None:
        try:
            timer = self._timer(self.retry_delay, self._retry_once, (conv_id,))
            if hasattr(timer, "daemon"):
                timer.daemon = True
            timer.start()
        except Exception as exc:
            self._say("warning", "排重试失败（不影响其它逻辑）：%r", exc)

    def _retry_once(self, conv_id: str) -> None:
        """单会话重投（note_failure 排的定时器触发）。"""
        with self._lock:
            rows = self.memory.list_pending(conv_id=conv_id, limit=1)
            if not rows:
                return
            row = rows[0]
            if self._expired(row):
                self._give_up(row)
                return
            self._deliver(row)

    # ------------------------------------------------------------ 后台补回
    def flush(self, force: bool = False) -> int:
        """把到点的待办重新投一遍，返回这一轮投出去几条。"""
        with self._lock:
            if self.deliver is None:
                return 0
            now = self._now()
            sent = 0
            for row in self.memory.list_pending(limit=self.max_rows):
                if self._expired(row, now):
                    self._give_up(row)
                    continue
                if not force and (now - float(row["last_try_at"] or 0)) < self._gap(int(row["attempts"])):
                    continue
                self._deliver(row)
                sent += 1
            return sent

    def _deliver(self, row: Dict[str, Any]) -> None:
        assert self.deliver is not None
        self.memory.touch_pending(int(row["id"]), reason="到点重投", now=self._now())
        meta = {
            "conv_name": row.get("conv_name") or "",
            "sender_name": row.get("sender_name") or "",
            "is_group": bool(row.get("is_group")),
            "attempts": int(row["attempts"]),
            "age": max(0.0, self._now() - float(row["created_at"] or 0)),
        }
        try:
            self.deliver(str(row["conv_id"]), str(row["body"]), meta)
            self._delivered += 1
        except Exception as exc:
            self._say("warning", "重投失败：%r", exc)

    def _gap(self, attempts: int) -> float:
        """越失败越慢：20/40/80… 秒，最低不小于后台扫描间隔。"""
        base = self.retry_delay * (2 ** min(max(0, int(attempts)), 4))
        return max(base, self.flush_seconds)

    def _expired(self, row: Dict[str, Any], now: Optional[float] = None) -> bool:
        if not self.max_age_seconds:
            return False
        moment = float(now if now is not None else self._now())
        return moment - float(row["created_at"] or 0) > self.max_age_seconds

    def _give_up(self, row: Dict[str, Any]) -> None:
        self.memory.drop_pending(int(row["id"]))
        self._given_up += 1
        hours = (self._now() - float(row["created_at"] or 0)) / 3600.0
        self._say("error", "「%s」拖了 %.1f 小时还没补回来，放弃这条（对方一直没收到回复）",
                  row.get("conv_name") or row.get("conv_id"), hours)

    # ------------------------------------------------------------ 补回来之后
    def pending_for(self, conv_id: str) -> List[Dict[str, Any]]:
        return self.memory.list_pending(conv_id=conv_id, limit=5)

    def clear(self, conv_id: str) -> int:
        rows = self.memory.list_pending(conv_id=conv_id, limit=50)
        return self.memory.drop_pending([int(r["id"]) for r in rows])

    # ---------------------------------------------------------------- 线程
    def start(self) -> threading.Thread:
        """后台线程：定期扫一遍待办。"""
        interval = max(10.0, min(self.flush_seconds, 60.0))

        def loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    self.flush()
                except Exception as exc:
                    self._say("warning", "后台补回出错（不影响回复）：%r", exc)

        thread = threading.Thread(target=loop, name="dsbot-retry", daemon=True)
        thread.start()
        return thread

    def stats(self) -> Dict[str, int]:
        return {
            "pending": len(self.memory.list_pending(limit=self.max_rows)),
            "redelivered": self._delivered,
            "given_up": self._given_up,
        }
