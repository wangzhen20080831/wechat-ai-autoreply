# -*- coding: utf-8 -*-
"""把「你自己在微信里回过的话」补进它的记忆（只读微信数据库，再写记忆库）。

    python tools\\backfill_self.py --conv 某人 --hours 8          先预览：今天我说了哪几句、哪些还没进记忆
    python tools\\backfill_self.py --conv 某人 --hours 8 --write   真的补进去
    python tools\\backfill_self.py --all-watch --hours 24            白名单里的私聊全扫一遍（慢一些）

为什么需要它：程序现在会把**运行期间**你手动回的话实时记进记忆，但升级之前你说过的话
它不知道。补一次之后，它就不会再把你已经回过的问题当"没人答过"重复问、重复答，
这些内容也会跟着同步进共享记忆，给「班长助理」会话看。

只读微信数据库，不改微信里的任何东西；写的是它自己的记忆库。重复运行不会重复插。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    os.system("chcp 65001 >nul 2>&1")
except Exception:
    pass
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except AttributeError:
    pass

from dsbot import Memory, Policy, load_config      # noqa: E402
from dsbot.wechat import WeChatIO, strip_sender_prefix   # noqa: E402

import re   # noqa: E402


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def already_known(mem: Memory, conv_id: str, text: str) -> bool:
    """这句是不是已经在记忆里了？连标点外的空白差异一起容忍，免得补出重复的一对。

    机器人自己发的话也在这条链路上（也是"我发的"），它们早就被 engine 记过一次了，
    这里要把它们认出来，不然补历史会补出两条一样的。
    """
    if mem.has_message(conv_id, "assistant", text):
        return True
    key = _squash(text)
    for row in mem.history(conv_id, limit_turns=200):
        if row.get("role") == "assistant" and _squash(str(row.get("content"))) == key:
            return True
    return False


def mine_in(io: WeChatIO, me: str, who: str, hours: float, limit: int = 300) -> list:
    """读这个会话里最近 messages：返回 [(时间戳, 正文), ...]，只留你自己发的文本。"""
    try:
        username = io.resolve_username(who)
    except Exception as exc:
        print("  读不到会话「%s」：%r" % (who, exc))
        return []
    if not username:
        print("  微信里找不到显示名叫「%s」的会话（跑 run.bat --list-sessions 看真实名字）" % who)
        return []
    try:
        rows = io._db.get_messages(username, limit=limit) or []
    except Exception as exc:
        print("  读「%s」的消息失败：%r" % (who, exc))
        return []
    cutoff = time.time() - float(hours) * 3600.0
    out = []
    for row in rows:
        try:
            ts = float(row.get("create_time") or 0)
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue
        if str(row.get("type") or "") != "文本":
            continue
        sender = str(row.get("sender_username") or "")
        if sender != me:
            continue
        text = strip_sender_prefix(str(row.get("content") or ""), sender).strip()
        if text:
            out.append((ts, text))
    out.sort(key=lambda item: item[0])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="把你自己在微信里回过的话补进它的记忆",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不加 --write 只预览，不动记忆库。",
    )
    ap.add_argument("--conv", action="append", default=[],
                    help="会话显示名或 wxid，可写多次")
    ap.add_argument("--all-watch", action="store_true",
                    help="白名单里的私聊全扫一遍（逐个查，慢）")
    ap.add_argument("--hours", type=float, default=8.0, help="往回看多少小时（默认 8）")
    ap.add_argument("--write", action="store_true", help="真的写进记忆库")
    ap.add_argument("--db", default="", help="指定的记忆库（默认按 config.json）")
    args = ap.parse_args(argv)

    cfg = load_config(ROOT)
    db_path = Path(args.db) if args.db else cfg.abspath(cfg.memory.db_path)
    mem = Memory(db_path, max_turns=cfg.memory.max_turns, ttl_hours=cfg.memory.ttl_hours)
    io = WeChatIO(account=cfg.wechat.account)
    try:
        info = io.connect()
        me = str(info.get("username") or "")
        print("当前账号：%s (%s)" % (info.get("nick_name") or "?", me or "?"))
        if not me:
            print("拿不到自己的 wxid，没法区分哪些是你发的。先让微信开着再跑一次。")
            return 2

        targets = list(args.conv)
        if args.all_watch:
            policy = Policy(cfg.trigger, self_names=[info.get("nick_name") or ""], self_wxid=me)
            for name in cfg.trigger.whitelist:
                if name not in targets and policy.watches(conv_id="", conv_name=name):
                    targets.append(name)
        if not targets:
            print("要指定 --conv 名字，或者加 --all-watch 扫白名单。")
            return 2

        total_new = total_all = 0
        for who in targets:
            rows = mine_in(io, me, who, args.hours)
            conv_id = ""
            try:
                conv_id = io.resolve_username(who) or ""
            except Exception:
                pass
            fresh_rows = []
            seen = set()
            for ts, text in rows:
                if text in seen or not conv_id:
                    continue
                seen.add(text)
                if not already_known(mem, conv_id, text):
                    fresh_rows.append((ts, text))
            fresh = {text for _ts, text in fresh_rows}
            total_all += len(rows)
            total_new += len(fresh_rows)
            print("\n「%s」%g 小时内你说过 %d 句，其中 %d 句还没进它的记忆："
                  % (who, args.hours, len(rows), len(fresh_rows)))
            for ts, text in rows:
                mark = "新" if text in fresh else "有"
                print("  [%s] %s  %s" % (time.strftime("%m-%d %H:%M", time.localtime(ts)),
                                         mark, text[:70]))
            if args.write and fresh_rows:
                for ts, text in fresh_rows:
                    mem.append(conv_id, "assistant", text, ts=ts, origin="human")
                print("  → 补进 %d 条" % len(fresh_rows))

        print("\n合计：看过 %d 句，其中 %d 句需要补。" % (total_all, total_new))
        if not args.write:
            print("（预览。确认无误加 --write 真的补进去）")
        else:
            print("已写进记忆库：%s" % db_path)
        return 0
    finally:
        mem.close()


if __name__ == "__main__":
    sys.exit(main())
