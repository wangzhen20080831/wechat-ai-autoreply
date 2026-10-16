# -*- coding: utf-8 -*-
"""通知材料库的命令行工具：建索引、看清单、试检索。

    python tools\index_notices.py                 建/更新索引（只处理新增和改过的）
    python tools\index_notices.py --rebuild       全部重读一遍
    python tools\index_notices.py --list          看库里有什么
    python tools\index_notices.py --search 体测    只用本地检索，看能不能搜到（不联网）
    python tools\index_notices.py --no-wechat     只索引 knowledge/ 目录，不碰微信文件

为什么要有 --search：同学问的问题五花八门，先自己搜一下，看通知里到底有没有
能答的东西。搜不到就说明该把哪个文件补进 knowledge\ 目录，或者干脆别让 AI 硬答。
"""

from __future__ import annotations

import argparse
import os
import sys
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

from dsbot import Knowledge, load_config      # noqa: E402
from dsbot.knowledge import ocr_available     # noqa: E402


def build(cfg, no_wechat: bool, ocr: bool = True) -> Knowledge:
    kcfg = cfg.knowledge
    return Knowledge(
        cfg.abspath(kcfg.db_path),
        dirs=kcfg.dirs,
        wechat_files=kcfg.wechat_files and not no_wechat,
        exclude_keywords=kcfg.exclude_keywords,
        min_chars=kcfg.min_chars,
        max_file_mb=kcfg.max_file_mb,
        max_docs=kcfg.max_docs,
        root=cfg.root,
        ocr=dict(enabled=bool(kcfg.ocr.enabled and ocr), lang=kcfg.ocr.lang,
                 dpi=kcfg.ocr.dpi, max_pages=kcfg.ocr.max_pages,
                 max_chars=kcfg.ocr.max_chars, trigger_chars=kcfg.ocr.trigger_chars),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="通知材料库：建索引 / 查库 / 试检索")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--rebuild", action="store_true", help="忽略缓存，全部重读")
    parser.add_argument("--list", action="store_true", help="列出库里已有的材料")
    parser.add_argument("--search", default="", help="拿这句话试一次本地检索")
    parser.add_argument("--top", type=int, default=4, help="检索返回几块（默认 4）")
    parser.add_argument("--no-wechat", action="store_true", help="不索引微信里的文件")
    parser.add_argument("--no-ocr", action="store_true", help="不 OCR 扫描件（快一些）")
    parser.add_argument("--verbose", action="store_true", help="逐文件打印新增/更新")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    cfg = load_config(ROOT, config_path=cfg_path if cfg_path.is_file() else None)
    if not cfg.knowledge.enabled:
        print("配置里 knowledge.enabled = false，材料库没启用。")
        return 1
    know = build(cfg, args.no_wechat, ocr=not args.no_ocr)
    print("材料库：%s" % know.db_path)
    print("扫描范围：%s%s" % (
        "、".join(know.dirs) or "（没配本地目录）",
        " ＋ 微信收到的文件" if know.wechat_files else ""))
    if know.ocr_enabled:
        usable, why = ocr_available(cfg.knowledge.ocr.lang)
        print("扫描件 OCR：%s" % (why if usable else "不可用 —— %s" % why))
    else:
        print("扫描件 OCR：已关闭（扫描件会被跳过）")
    print()

    if args.search:
        hits = know.search(args.search, k=args.top)
        if not hits:
            print("没搜到相关内容。可能是库里还没有这份通知，或者说法对不上。")
            know.close()
            return 2
        for i, hit in enumerate(hits, 1):
            print("=" * 74)
            print("%d) 《%s》（%s）  匹配度 %.2f" % (i, hit.title, hit.date or "?", hit.score))
            print("-" * 74)
            print(hit.text[:700])
        print("=" * 74)
        know.close()
        return 0

    if args.list and not args.rebuild:
        docs = know.list_docs(limit=500)
        print("库里共 %d 份材料、约 %d 字" % (len(docs), know.stats()["chars"]))
        for doc in docs:
            print("  %-10s %-8s %-52s %6d 字" % (
                doc["date"] or "?", doc["kind"], doc["title"][:50], doc["chars"]))
        know.close()
        return 0

    report = know.index(force=args.rebuild, verbose=args.verbose)
    print("结果：%s" % report.summary())
    if report.skipped:
        print("\n跳过 %d 个（这些不会被 AI 引用）：" % len(report.skipped))
        for name, why in report.skipped:
            print("   %-54s %s" % (name[:52], why))
    docs = know.list_docs(limit=20)
    if docs:
        print("\n库里最新的 20 份：")
        for doc in docs:
            print("   %-10s %-50s %6d 字" % (doc["date"] or "?", doc["title"][:48], doc["chars"]))
    print("\n分词方式：%s" % know.stats()["tokenizer"])
    if not know.stats()["docs"]:
        print("\n提示：把通知文件（docx / pdf / xlsx）丢进项目里的 knowledge\\ 目录，")
        print("      再跑一次本脚本；或者别加 --no-wechat，让它读微信收到的文件。")
    know.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
