# -*- coding: utf-8 -*-
"""按一份「人名清单」批量生成 config.json 的白名单。

为什么需要它：白名单匹配的是**微信里实际的显示名**（备注优先），
而名册上的人名经常和微信里对不上——异体字、昵称没改、备注写错字、
重名。直接把人名抄进白名单，结果是"加了 31 个人，实际只回得动 28 个"，
而且不会报错，只是静默不回。

这个脚本会：
  1. 拉取你微信里的联系人显示名；
  2. 逐个人名做三级匹配：精确显示名 → 曾在指定群里出现过的近似名 → 放弃；
  3. 遇到重名（比如通讯录里 3 个"刘洋"）时，用群成员身份挑出对的那个，
     并改用 wxid 写进白名单，避免误回别人；
  4. 打印逐条对照表，让你肉眼复核。

用法：
    python tools/sync_whitelist.py 名册.json               只看结果，不改文件
    python tools/sync_whitelist.py 名册.json --write       写回 config.json
    python tools/sync_whitelist.py 名字.txt --group 班级群名 --write

人名清单支持两种格式：
    *.json   取 rows[].name（兼容手机号/身份证等字段，本脚本只读 name）
    *.txt    一行一个名字，# 开头为注释
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

from dsbot.wechat import WeChatIO, is_group_chat      # noqa: E402


def load_names(path: Path) -> List[str]:
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        rows = data.get("rows") if isinstance(data, dict) else data
        return [str(r["name"]).strip() for r in (rows or []) if str(r.get("name", "")).strip()]
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def display_of(record: Dict) -> str:
    return str(record.get("remark") or record.get("nick_name") or record.get("username") or "")


class Matcher:
    def __init__(self, io: WeChatIO) -> None:
        self.io = io
        self.display_to_users: Dict[str, List[str]] = {}
        for username, disp in io.name_map().items():
            self.display_to_users.setdefault(disp, []).append(username)
        self.group: Dict[str, str] = {}       # username -> 群里的显示名
        self.group_name = ""

    def load_group(self, keyword: str) -> bool:
        if not keyword:
            return False
        target = None
        for s in self.io.list_sessions(limit=500):
            username = str(s.get("username") or "")
            if not is_group_chat(username):
                continue
            name = self.io.display_name(username)
            if keyword == name or keyword == username:
                target = username
                break
        if target is None:
            target = self.io._db.group_name_to_id(keyword)     # 名字不完全一致时的兜底
        if not target:
            print("⚠️  找不到群「%s」，将只在全部联系人里匹配" % keyword)
            return False
        self.group_name = self.io.display_name(target)
        for m in self.io._db.get_group_members(target) or []:
            uid = str(m.get("username") or "")
            if uid:
                self.group[uid] = display_of(m) or self.io.display_name(uid)
        print("已载入群「%s」的 %d 名成员" % (self.group_name, len(self.group)))
        return True

    def resolve(self, name: str) -> Tuple[Optional[str], str]:
        """返回 (白名单里该写的字符串, 说明)。None 表示没匹配上。"""
        exact = self.display_to_users.get(name, [])
        in_group = [u for u in exact if u in self.group]

        if len(exact) == 1:
            return name, "微信显示名一致"
        if len(exact) > 1:
            if len(in_group) == 1:
                return in_group[0], "微信里有 %d 个人叫这个，用 wxid 锁定群里那一个" % len(exact)
            return None, "重名且无法确定是哪一个（通讯录里 %d 个同名的）" % len(exact)

        # 名字对不上：只在班群成员里找近似名，避免在几千个联系人里瞎猜
        if not self.group:
            return None, "微信里找不到这个名字"
        pool = {uid: disp for uid, disp in self.group.items()}
        cands = difflib.get_close_matches(name, list(pool.values()), n=3, cutoff=0.4)
        for cand in cands:
            if self.display_to_users.get(cand):
                return cand, "名册写「%s」，微信里显示「%s」" % (name, cand)
        for uid, disp in pool.items():
            if name and any(ch in disp for ch in name):
                return disp, "名册写「%s」，微信里显示「%s」" % (name, disp)
        return None, "微信里找不到这个名字"


def main() -> int:
    parser = argparse.ArgumentParser(description="按人名清单生成微信白名单")
    parser.add_argument("names", help="人名清单（.json 或 .txt）")
    parser.add_argument("--group", default="", help="用哪个群的成员来消歧（推荐填班级群名）")
    parser.add_argument("--config", default=str(ROOT / "config.json"), help="要写入的 config.json")
    parser.add_argument("--write", action="store_true", help="真的写回配置（默认只预览）")
    parser.add_argument("--keep", default="", help="额外保留在白名单里的名字/群名，逗号分隔")
    args = parser.parse_args()

    names_path = Path(args.names)
    if not names_path.is_file():
        print("❌ 找不到人名清单：%s" % names_path)
        return 1
    names = load_names(names_path)
    if not names:
        print("❌ 清单里没读到人名")
        return 1
    print("名册共 %d 人" % len(names))

    io = WeChatIO()
    info = io.connect()
    print("微信账号：%s\n" % (info.get("nick_name") or "?"))
    matcher = Matcher(io)
    matcher.load_group(args.group)
    print()

    entries: List[str] = []
    notes: Dict[str, str] = {}
    unmatched: List[Tuple[str, str]] = []
    for name in names:
        got, why = matcher.resolve(name)
        if not got:
            unmatched.append((name, why))
            print("❌ %-6s %s" % (name, why))
            continue
        if got not in entries:
            entries.append(got)
        if got != name:
            notes[got] = why
            print("⚠️  %-6s -> %-12s %s" % (name, got, why))
        else:
            print("✅ %s" % name)

    for extra in [x.strip() for x in args.keep.split(",") if x.strip()]:
        if extra not in entries:
            entries.append(extra)

    print()
    print("=" * 70)
    print("名册 %d 人，白名单最终 %d 条" % (len(names), len(entries)))
    if unmatched:
        print("没匹配上的 %d 个：" % len(unmatched))
        for n, why in unmatched:
            print("   %s —— %s" % (n, why))
        print("   （这些人不会被自动回复。可以手动在微信里给他们加个备注、")
        print("     或者直接把人名/微信号补进 config.json 的 trigger.whitelist）")

    if not args.write:
        print("\n（预览模式，没有改动任何文件。确认无误后加 --write）")
        return 0 if not unmatched else 2

    cfg_path = Path(args.config)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    trigger = cfg.setdefault("trigger", {})
    old = trigger.get("whitelist") or []
    trigger["whitelist"] = entries
    if notes:
        trigger["_whitelist_notes"] = notes
    trigger.setdefault(
        "_whitelist_说明",
        "白名单同时支持「微信显示名」和「wxid」；本文件里凡是 wxid 都是为了避开重名。"
        "白名单匹配的是微信当前显示名（备注优先），改备注后需要重跑 tools/sync_whitelist.py。",
    )
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n✅ 已写入 %s（原白名单 %d 条 → 现在 %d 条）" % (cfg_path, len(old), len(entries)))
    print("   记得跑一次 doctor.py 复核。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
