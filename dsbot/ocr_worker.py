# -*- coding: utf-8 -*-
"""在独立子进程里跑 Windows OCR（由 dsbot/knowledge.py 调用，不给人直接用）。

为什么非得单开一个进程：winsdk 渲染完 PDF 之后，那些 COM 对象会在解释器退出
的时候把进程搞崩（退出码 0xC0000005 访问违例），有时候干脆卡着不做收尾。
把 OCR 关进子进程、写完结果直接 ``os._exit`` 跳过收尾，主程序就一点事没有
—— 子进程怎么崩都影响不到机器人本体。

约定：结果写进 ``--out`` 指定的 JSON 文件；stdout/stderr 只用来报错。

    python -m dsbot.ocr_worker --mode check
    python -m dsbot.ocr_worker --mode image --path a.png --out r.json
    python -m dsbot.ocr_worker --mode pdf --path a.pdf --out r.json --dpi 300 --max-pages 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dsbot.knowledge import (                    # noqa: E402
    OcrUnavailable,
    _ocr_languages,
    _ocr_pdf_task,
    _recognize_file,
    pick_ocr_language,
)


def _write(out: str, payload: Dict[str, Any]) -> None:
    if not out:
        return
    Path(out).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _finish(code: int) -> None:
    """写完结果就绕开解释器收尾 —— winsdk 的 COM 清理会崩，不如不走。"""
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(code)


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Windows OCR 子进程")
    parser.add_argument("--mode", required=True, choices=("check", "image", "pdf"))
    parser.add_argument("--path", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--lang", default="")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--max-chars", type=int, default=3000)
    args = parser.parse_args(argv)

    if args.mode == "check":
        try:
            langs = _ocr_languages()
            lang = pick_ocr_language(args.lang)
        except OcrUnavailable as exc:
            _write(args.out, {"ok": False, "unavailable": True, "why": str(exc)})
            _finish(1)
        except Exception as exc:                 # noqa: BLE001
            _write(args.out, {"ok": False, "unavailable": True,
                              "why": "读 OCR 引擎失败：%r" % (exc,)})
            _finish(1)
        _write(args.out, {"ok": True, "lang": lang, "langs": langs})
        _finish(0)

    path = Path(args.path)
    if not path.is_file():
        _write(args.out, {"ok": False, "why": "文件不存在：%s" % path})
        _finish(1)
    try:
        if args.mode == "image":
            text = asyncio.run(_recognize_file(path, args.lang))
            _write(args.out, {"ok": True, "text": text,
                              "pages": 1 if text else 0, "total": 1})
        else:
            text, done, total = asyncio.run(_ocr_pdf_task(
                path, args.dpi, args.max_pages, args.max_chars, args.lang))
            _write(args.out, {"ok": True, "text": text, "pages": done, "total": total})
    except OcrUnavailable as exc:
        _write(args.out, {"ok": False, "unavailable": True, "why": str(exc)})
        _finish(1)
    except Exception as exc:                     # noqa: BLE001
        _write(args.out, {"ok": False, "why": "OCR 失败：%r" % (exc,)})
        _finish(1)
    _finish(0)


if __name__ == "__main__":
    main()
