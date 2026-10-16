# -*- coding: utf-8 -*-
"""待办消息的手工入口：把「本该回、但当时没回上」的话排给机器人，让它自己补回去。

    python tools\\requeue.py                                  看现在欠着谁（机器人几分钟内自己补）
    python tools\\requeue.py --conv 某人 --text "他在不"       先预览，不动数据库
    python tools\\requeue.py --conv 某人 --text "他在不" --write   真排进去
    python tools\\requeue.py --conv 某人 --drop --write        把某人的待办撤掉

为什么需要它：程序升级成「失败自动补回」之前丢掉的那些消息，记忆库里没有记录，
没人会去补。用这个工具把它们排进待办队列，运行中的机器人就会当成新消息处理：
正常走人设、记忆和通知材料，回复后发给对方。

注意：`--write` 之后，机器人会在 2 分钟内自己把这条发出去（它并不知道这是补的
老消息，所以回复口吻就像刚收到一样）。写之前先看看预览对不对。
"""

from __future__ import annotations

import argparse
import json
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

from dsbot import Memory, load_config      # noqa: E402


def find_conv(name: str, names_path: Path) -> list:
    """按微信里显示的名字找 wxid（私聊优先）。找不到返回空列表。"""
    try:
        table = json.loads(names_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    hits = [wx for wx, shown in table.items() if str(shown) == name]
    private = [w for w in hits if "@chatroom" not in w]
    return private or hits


def show(mem: Memory) -> int:
    rows = mem.list_pending(limit=50)
    if not rows:
        print("现在没有欠着的消息。")
        return 0
    print("现在欠着 %d 条（机器人每 2 分钟扫一遍，会自己补）：" % len(rows))
    for row in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(float(row["created_at"])))
        print("  [%s] %s（失败 %s 次，%s）"
              % (when, row.get("conv_name") or row.get("conv_id"),
                 row.get("attempts"), str(row.get("reason") or "")[:40]))
        for line in str(row.get("body") or "").split("\n"):
            print("        %s" % line)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="把没回上的消息排进待办，让机器人自己补回",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="不带参数 = 看现在欠着谁。",
    )
    ap.add_argument("--conv", default="", help="对方在微信里显示的名字，或 wxid")
    ap.add_argument("--text", action="append", default=[],
                    help="要补的话，可以写多次（多条会合成一条回复）")
    ap.add_argument("--group", action="store_true", help="对方是群（默认按私聊处理）")
    ap.add_argument("--drop", action="store_true", help="撤掉这个会话的待办，不再补回")
    ap.add_argument("--write", action="store_true", help="真的写进数据库（不加只看预览）")
    ap.add_argument("--db", default="", help="指定的记忆库文件（默认按 config.json）")
    ap.add_argument("--names", default="", help="名称表 data/names.json（默认按 config.json）")
    args = ap.parse_args(argv)

    cfg = load_config(ROOT)
    db_path = Path(args.db) if args.db else cfg.abspath(cfg.memory.db_path)
    names_path = Path(args.names) if args.names else cfg.abspath("data/names.json")
    mem = Memory(db_path, max_turns=cfg.memory.max_turns, ttl_hours=cfg.memory.ttl_hours)
    try:
        if not args.conv:
            return show(mem)

        if "@chatroom" in args.conv or args.conv.startswith("wxid_"):
            conv_id, conv_name = args.conv, args.conv
            if args.conv.startswith("wxid_"):
                try:
                    table = json.loads(names_path.read_text(encoding="utf-8"))
                    conv_name = table.get(args.conv, args.conv)
                except Exception:
                    pass
        else:
            hits = find_conv(args.conv, names_path)
            if not hits:
                print("在 %s 里找不到显示名叫「%s」的会话。"
                      "名字要和微信里显示的完全一致（跑 run.bat --list-sessions 看真实名字）。"
                      % (names_path, args.conv))
                return 2
            if len(hits) > 1:
                print("「%s」对应多个会话：%s。请直接用 wxid。" % (args.conv, "、".join(hits)))
                return 2
            conv_id, conv_name = hits[0], args.conv

        now = time.time()
        if args.drop:
            rows = mem.list_pending(conv_id=conv_id, limit=50)
            if not rows:
                print("「%s」现在没有待办，不用撤。" % conv_name)
                return 0
            print("要撤掉「%s」的 %d 条待办：" % (conv_name, len(rows)))
            for row in rows:
                for line in str(row.get("body") or "").split("\n"):
                    print("    %s" % line)
            if not args.write:
                print("\n（预览。确认无误加 --write 真的撤掉）")
                return 0
            n = mem.drop_pending([int(r["id"]) for r in rows])
            print("\n已撤掉 %d 条。机器人不会再补回这些。" % n)
            return 0

        texts = [t.strip() for t in args.text if t and t.strip()]
        if not texts:
            print("要给 --conv 配至少一个 --text。想看现在欠着谁，不加参数直接跑。")
            return 2
        print("会话：%s（%s）%s" % (conv_name, conv_id, "【群】" if args.group else ""))
        print("要补的话：")
        for line in texts:
            print("    %s" % line)
        print("\n机器人会按正常流程回复这条（走人设、记忆和通知材料），发完这条待办就清了。")
        print("注意：它不知道这是几小时前的消息，口吻会像刚收到。所以别补已经过期的事"
              "（「10:05 提醒我」这种）。")
        if not args.write:
            print("\n（预览。确认无误加 --write 真的排进去；排进去后 2 分钟内会发出去）")
            return 0
        mem.add_pending(conv_id, "\n".join(texts), conv_name=conv_name,
                        sender_name=conv_name, is_group=bool(args.group),
                        reason="手工补排（当时生成失败，消息丢了）", now=now)
        print("\n已排入待办，机器人会在 2 分钟内自己补回。"
              "要看结果：data\\bot.log 里搜 [补回]。")
        return 0
    finally:
        mem.close()


if __name__ == "__main__":
    sys.exit(main())
