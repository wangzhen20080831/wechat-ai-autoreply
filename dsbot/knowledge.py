# -*- coding: utf-8 -*-
"""通知材料库：让 AI 照着通知原文回答，而不是凭感觉编。

为什么需要它：同学问「体测几点在哪集合」「医保什么时候截止」，光靠模型自己
发挥，迟早给你编出一个错误的时间。这里把通知文件读成文本、切块存起来，
有人提问时按关键词取出最相关的几块塞进提示词，模型就有据可依。

支持的文件（其余格式会明确跳过，不静默忽略）：
    .docx   直接读 zip 里的 XML，不依赖第三方库
    .xlsx   优先 openpyxl，没装就用 zip 解析兜底
    .pdf    需要 pypdf 或 pdfminer.six；没有就说清楚读不了
    .txt / .md / .csv   直接读，自动猜编码
    .jpg / .png 等图片，以及「打印出来拍照/扫描」的那种 PDF：
            正文抽不出文字时自动用 Windows 自带 OCR 认成文字（见下面 OCR 一节）

隐私闸门（很重要）：抽完文本先扫一遍（OCR 认出来的文字也一样过闸门），出现
身份证号、或一堆手机号的文件 **直接不入库**。学生名册那种表一旦进了库，同学问
「某某的身份证号」模型就会照答，那是要出事的。被拦下的文件会连同原因列在索引报告里。

具体四条（改一次就把 _GATE_VERSION +1，老库会自动全量重判）：
    身份证号 ≥1 处            → 拦
    手机号 ≥5 处              → 拦
    学号一类的长编号 ≥20 处    → 拦（学费账单库、考生名单这种）
    文件名像花名册的          → 拦（只对微信自动收集的文件，knowledge\\ 里你自己放的不管）
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import asyncio
import subprocess
import sys
import tempfile
import time
import warnings
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------- 文本抽取

DOC_EXTS = (".docx", ".pdf", ".xlsx", ".txt", ".md", ".csv")
_TEXT_EXTS = (".txt", ".md", ".csv")
IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")     # 只有开着 OCR 才收

_LOUD_LOGGERS = ("pypdf", "pypdf._reader", "pypdf._utils",
                 "pdfminer", "pdfminer.pdfpage", "pdfminer.pdfinterp")


def _silence_pdf_logs() -> None:
    """pypdf 碰到 PDF 里的小毛病会刷一屏 WARNING，对用户没意义，压掉。"""
    for name in _LOUD_LOGGERS:
        try:
            logging.getLogger(name).setLevel(logging.ERROR)
        except Exception:
            pass


class Unsupported(Exception):
    """这个文件读不了。原因会写进索引报告，让人知道少了什么。"""


_TAG_RE = re.compile(r"<[^>]+>")


def _tidy(text: str) -> str:
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in (text or "").splitlines()]
    out: List[str] = []
    for ln in lines:
        if not ln:
            if out and out[-1] != "":
                out.append("")
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _extract_docx(path: Path) -> str:
    """docx = zip + XML。把段落/表格标记换成换行和制表符，再去掉所有标签。"""
    try:
        zf = zipfile.ZipFile(str(path))
    except zipfile.BadZipFile:
        raise Unsupported("不是有效的 docx（压缩包打不开，可能是 .doc 改的名字）")
    with zf:
        if "word/document.xml" not in zf.namelist():
            raise Unsupported("docx 里没有 word/document.xml（可能不是标准 docx）")
        raw = zf.read("word/document.xml")
        for extra in ("word/header1.xml", "word/footer1.xml"):
            if extra in zf.namelist():
                raw += b"\n" + zf.read(extra)
    text = raw.decode("utf-8", errors="replace")
    for old, new in (
        ("</w:p>", "\n"),
        ("</w:tr>", "\n"),
        ("</w:tc>", "\t"),
        ("<w:tab", "\t<w:tab"),
        ("<w:br", "\n<w:br"),
    ):
        text = text.replace(old, new)
    return _tidy(html.unescape(_TAG_RE.sub("", text)))


def _extract_xlsx_openpyxl(path: Path) -> str:
    import openpyxl

    # openpyxl 对「不支持的数据验证扩展」之类的警告没意义，压掉，别刷进日志
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        parts: List[str] = []
        try:
            for ws in wb.worksheets:
                rows: List[str] = []
                for row in ws.iter_rows(values_only=True):
                    cells = ["" if c is None else str(c).strip() for c in row]
                    while cells and not cells[-1]:
                        cells.pop()
                    if cells:
                        rows.append(" | ".join(cells))
                if rows:
                    parts.append("【%s】\n%s" % (ws.title, "\n".join(rows)))
        finally:
            wb.close()
    return _tidy("\n".join(parts))


_SST_RE = re.compile(r"<si>(.*?)</si>", re.S)
_T_RE = re.compile(r"<t[^>]*>(.*?)</t>", re.S)
_ROW_RE = re.compile(r"<row[^>]*>(.*?)</row>", re.S)
_CELL_RE = re.compile(r"<c([^>]*)>(.*?)</c>", re.S)
_V_RE = re.compile(r"<v>(.*?)</v>", re.S)
_S_ATTR_RE = re.compile(r't="(\w+)"')


def _extract_xlsx_raw(path: Path) -> str:
    """没有 openpyxl 时的兜底：xlsx 也是 zip，读共享字符串表 + 各工作表。"""
    try:
        zf = zipfile.ZipFile(str(path))
    except zipfile.BadZipFile:
        raise Unsupported("不是有效的 xlsx（压缩包打不开）")
    with zf:
        names = zf.namelist()
        shared: List[str] = []
        if "xl/sharedStrings.xml" in names:
            sst = zf.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
            shared = [html.unescape("".join(_T_RE.findall(item))) for item in _SST_RE.findall(sst)]
        parts: List[str] = []
        for name in sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)):
            xml = zf.read(name).decode("utf-8", errors="replace")
            lines: List[str] = []
            for row in _ROW_RE.findall(xml):
                cells: List[str] = []
                for attrs, body in _CELL_RE.findall(row):
                    value = ""
                    found = _V_RE.search(body)
                    if found:
                        value = found.group(1)
                        if _S_ATTR_RE.search(attrs or "") and _S_ATTR_RE.search(attrs).group(1) == "s":
                            try:
                                value = shared[int(value)]
                            except (ValueError, IndexError):
                                value = ""
                    if value:
                        cells.append(html.unescape(_TAG_RE.sub("", value)).strip())
                if cells:
                    lines.append(" | ".join(cells))
            if lines:
                parts.append("【%s】\n%s" % (Path(name).stem, "\n".join(lines)))
    return _tidy("\n".join(parts))


def _extract_xlsx(path: Path) -> str:
    try:
        return _extract_xlsx_openpyxl(path)
    except ImportError:
        return _extract_xlsx_raw(path)
    except zipfile.BadZipFile as exc:
        raise Unsupported("不是有效的 xlsx（压缩包打不开，可能是 .xls 改的名字）：%s" % exc)


def _extract_pdf(path: Path) -> str:
    _silence_pdf_logs()
    errors: List[str] = []
    try:
        import pypdf

        reader = pypdf.PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        if text.strip():
            return _tidy(text)
        errors.append("pypdf 没读出文字")
    except ImportError:
        errors.append("没装 pypdf")
    except Exception as exc:
        errors.append("pypdf 出错：%s" % exc)
    try:
        from pdfminer.high_level import extract_text as _pm_extract

        text = _pm_extract(str(path)) or ""
        if text.strip():
            return _tidy(text)
        errors.append("pdfminer 没读出文字")
    except ImportError:
        errors.append("没装 pdfminer.six")
    except Exception as exc:
        errors.append("pdfminer 出错：%s" % exc)
    raise Unsupported("PDF 读不出文字（可能是扫描件）；%s" % "；".join(errors))


def _extract_text_like(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "utf-16"):
        try:
            return _tidy(data.decode(enc))
        except UnicodeDecodeError:
            continue
    return _tidy(data.decode("utf-8", errors="replace"))


def extract_text(path: Path) -> Tuple[str, str]:
    """返回 (正文, 类型)。读不了就抛 Unsupported。"""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _extract_docx(path), "docx"
    if suffix == ".xlsx":
        return _extract_xlsx(path), "xlsx"
    if suffix == ".pdf":
        return _extract_pdf(path), "pdf"
    if suffix in _TEXT_EXTS:
        return _extract_text_like(path), "text"
    raise Unsupported("不支持的格式 %s" % (suffix or "（没有扩展名）"))


# ---------------------------------------------------------------- OCR（扫描件）
#
# 打印出来再拍照/扫描的 PDF 里没有文字层，pypdf 只能认出空气。这里用
# Windows 自带的 OCR（Windows.Media.Ocr）+ 自带的 PDF 渲染器
# （Windows.Data.Pdf）把它读成文字 —— 不用装 Tesseract，不用联网，不花钱。
# winsdk 本来就是微信自动化的依赖，所以这条链路不引入任何新东西。
#
# 但真正干活的在「子进程」里（dsbot/ocr_worker.py），不在主程序里：
# winsdk 渲染完 PDF，COM 对象在解释器退出时会让进程崩（0xC0000005 访问违例，
# 偶尔还会卡死不做收尾）。OCR 全在子进程里跑、子进程写完结果 os._exit 走人，
# 主程序这边就干净了 —— 它怎么崩都影响不到机器人本体。

_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
# 中文标点。OCR 在这些标点前后也会塞空格。英文的半角标点（, . %）一律不碰 ——
# 碰了会把 "Hello. World" 挤成 "Hello.World"。
_PUNCT_L = "（【《「『，。、；：！？…．"          # 后面不该有空格
_PUNCT_R = "，。、；：？！）】》」』…．"          # 前面不该有空格
_HSPACE = r"[^\S\n]"                            # 横向空白（不含换行，行结构要留住）
_PREFER_LANGS = ("zh-Hans-CN", "zh-Hans", "zh-Hant-TW", "zh-Hant", "ja", "en-US")


class OcrUnavailable(Exception):
    """这台机器上没有可用的 OCR（需要 Windows 10/11 自带的中文 OCR）。"""


def join_ocr_spaces(text: str) -> str:
    """Windows OCR 会在每个汉字之间塞一个空格，会把中文分词全打碎，合回去。

    「以 升 大 学 生」→「以升大学生」；英文单词之间的空格和换行都留着。
    """
    out = text or ""
    out = re.sub(r"(?<=[%s0-9])%s+(?=[%s0-9])" % (_CJK, _HSPACE, _CJK), "", out)
    out = re.sub(r"%s+(?=[%s])" % (_HSPACE, _PUNCT_R), "", out)
    out = re.sub(r"(?<=[%s])%s+" % (_PUNCT_L, _HSPACE), "", out)
    return _tidy(out)


def _ocr_languages() -> List[str]:
    try:
        from winsdk.windows.media.ocr import OcrEngine
    except Exception as exc:
        raise OcrUnavailable("没装 winsdk（pip install winsdk）：%s" % exc)
    try:
        return [lang.language_tag for lang in OcrEngine.available_recognizer_languages]
    except Exception as exc:
        raise OcrUnavailable("读不到 Windows OCR 语言列表：%s" % exc)


def pick_ocr_language(prefer: str = "") -> str:
    tags = _ocr_languages()
    if not tags:
        raise OcrUnavailable(
            "Windows 里没装任何 OCR 语言包。去「设置 → 时间和语言 → 语言」给中文"
            "添加『光学字符识别』，或装一下简体中文语言包。")
    if prefer:
        for tag in tags:
            if tag.lower() == prefer.lower():
                return tag
        for tag in tags:
            if tag.lower().startswith(prefer.lower()[:5]):
                return tag
    for want in _PREFER_LANGS:
        for tag in tags:
            if tag.lower().startswith(want.lower()):
                return tag
    return tags[0]


def ocr_available(prefer: str = "") -> Tuple[bool, str]:
    """返回 (能不能用, 说明)。自检脚本用它报个明白话。

    问的是子进程（真正干活的那个），所以主程序自始至终不碰 winsdk。
    """
    try:
        payload = _run_worker(["--mode", "check"] + (["--lang", prefer] if prefer else []),
                              timeout=90)
    except OcrUnavailable as exc:
        return False, str(exc)
    if not payload.get("ok"):
        return False, str(payload.get("why") or "Windows OCR 用不了")
    return True, "Windows 自带 OCR，用 %s" % payload.get("lang")


def _result_text(result: Any) -> str:
    """把 OCR 结果按「行」拼回文本。

    Windows 给的 ``.text`` 是整页一行长字符串（换行都换成空格了），表格会挤成
    一坨、检索切块也切不准。它其实按行给了结果，这里逐行去汉字间的空格，行与行
    之间留换行 —— 表格的一行就是一行。
    """
    lines = [join_ocr_spaces(getattr(line, "text", "") or "")
             for line in (getattr(result, "lines", None) or [])]
    lines = [line for line in lines if line]
    if lines:
        return "\n".join(lines)
    return join_ocr_spaces(getattr(result, "text", "") or "")


_WORKER = Path(__file__).resolve().parent / "ocr_worker.py"


def _python_exe() -> str:
    """启动 OCR 子进程用的解释器。venv 里优先用它自己的 python.exe，
    这样子进程的 site-packages（winsdk 在这儿）和主程序完全一致。"""
    if os.name == "nt":
        launcher = Path(sys.prefix) / "Scripts" / "python.exe"
        if launcher.is_file():
            return str(launcher)
    return sys.executable


def _run_worker(args: Sequence[str], timeout: float) -> Dict[str, Any]:
    """让子进程去 OCR，把 JSON 结果读回来。失败一律抛 OcrUnavailable。"""
    tmp = tempfile.mkdtemp(prefix="dsbot-ocr-")
    out = Path(tmp) / "result.json"
    cmd = [_python_exe(), "-X", "utf8", str(_WORKER), "--out", str(out)] + list(args)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
    try:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env,
                                  cwd=str(_WORKER.parent.parent))
        except subprocess.TimeoutExpired:
            raise OcrUnavailable("OCR 算了超过 %d 秒还没完，这份先跳过" % int(timeout))
        except OSError as exc:
            raise OcrUnavailable("调不起 OCR 子进程：%s" % exc)
        # 子进程是「写完结果再退出」，所以哪怕它退出码不好看，结果照样算数
        payload: Any = None
        try:
            payload = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            payload = None
        if isinstance(payload, dict):
            return payload
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        err = "；".join(line for line in err.splitlines()[-3:])
        raise OcrUnavailable("OCR 子进程没给出结果（退出码 %s）%s"
                             % (proc.returncode, "：" + err if err else ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _engine(prefer: str = "") -> Any:
    from winsdk.windows.globalization import Language
    from winsdk.windows.media.ocr import OcrEngine

    lang = pick_ocr_language(prefer)
    engine = OcrEngine.try_create_from_language(Language(lang))
    if engine is None:
        raise OcrUnavailable("Windows OCR 建不出 %s 的识别器" % lang)
    return engine


async def _recognize_file(path: Path, prefer: str) -> str:
    """图片（jpg/png）直接 OCR。跑在 OCR 子进程里（见 ocr_worker.py）。"""
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage import FileAccessMode, StorageFile

    engine = _engine(prefer)
    file = await StorageFile.get_file_from_path_async(str(path))
    stream = await file.open_async(FileAccessMode.READ)
    try:
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        result = await engine.recognize_async(bitmap)
        return _result_text(result)
    finally:
        stream.close()


async def _ocr_pdf_task(path: Path, dpi: int, max_pages: int, max_chars: int,
                       prefer: str) -> Tuple[str, int, int]:
    from winsdk.windows.data.pdf import PdfDocument, PdfPageRenderOptions
    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.storage import StorageFile
    from winsdk.windows.storage.streams import InMemoryRandomAccessStream

    engine = _engine(prefer)
    file = await StorageFile.get_file_from_path_async(str(path))
    doc = await PdfDocument.load_from_file_async(file)
    total = int(doc.page_count)
    parts: List[str] = []
    done = 0
    blank_in_a_row = 0
    for index in range(min(total, max(1, max_pages))):
        page = doc.get_page(index)
        options = PdfPageRenderOptions()
        width = page.size.width / 72.0 * max(72, dpi)
        options.destination_width = max(1, min(4000, int(round(width))))
        options.destination_height = 0            # 0 = 高度按比例算
        stream = InMemoryRandomAccessStream()
        try:
            await page.render_to_stream_async(stream, options)
            stream.seek(0)
            decoder = await BitmapDecoder.create_async(stream)
            bitmap = await decoder.get_software_bitmap_async()
            result = await engine.recognize_async(bitmap)
        finally:
            stream.close()
            try:
                page.close()
            except Exception:
                pass
        done += 1
        page_text = _result_text(result)
        if page_text:
            blank_in_a_row = 0
            parts.append(page_text)
            if sum(len(p) for p in parts) >= max_chars:
                break
        else:
            blank_in_a_row += 1
            if blank_in_a_row >= 3 and not parts:
                break                             # 连着三页一个字没有，别耗着了
    return "\n".join(parts), done, total


def ocr_pdf(path: Path, *, dpi: int = 300, max_pages: int = 10,
            max_chars: int = 3000, lang: str = "") -> Tuple[str, int, int]:
    """把扫描件 PDF 认成文字。返回 (正文, 识别了几页, 共几页)。"""
    pages = max(1, int(max_pages))
    payload = _run_worker(
        ["--mode", "pdf", "--path", str(path), "--lang", lang or "",
         "--dpi", str(int(dpi)), "--max-pages", str(pages), "--max-chars", str(int(max_chars))],
        timeout=min(600.0, 90.0 + 25.0 * pages))
    if not payload.get("ok"):
        raise OcrUnavailable(str(payload.get("why") or "OCR 没认出东西"))
    return (str(payload.get("text") or ""), int(payload.get("pages") or 0),
            int(payload.get("total") or 0))


def ocr_image(path: Path, *, lang: str = "") -> str:
    """把一张图片（拍下来的通知）认成文字。"""
    payload = _run_worker(["--mode", "image", "--path", str(path), "--lang", lang or ""],
                          timeout=120.0)
    if not payload.get("ok"):
        raise OcrUnavailable(str(payload.get("why") or "OCR 没认出东西"))
    return str(payload.get("text") or "")


# ---------------------------------------------------------------- 隐私闸门

_ID_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
# 学号 / 账号 / 账单号一类的长数字。单个不算隐私，成规模就是名单了。
_LONGNUM_RE = re.compile(r"(?<!\d)(\d{10,14})(?!\d)")

_ID_MIN = 1          # 身份证号出现 1 处就拦
_PHONE_MIN = 5       # 手机号要 5 处以上才拦（一两个是通知里的咨询电话）
_LONGNUM_MIN = 20    # 长编号 20 处以上 = 名单表

# 文件名里带这些词的，多半是花名册而不是通知。只对「微信自动收集」的文件生效 ——
# 你自己往 knowledge\ 里丢的，默认你要用它，就不替你做主了。
_ROSTER_NAME_HINTS = ("名单", "名册", "花名册", "汇总表", "统计表", "信息表", "情况统计",
                      "摸底", "档案", "台账", "资格库", "座位表", "座位图")

# 隐私规则每改一次就 +1：老库下次索引会自动全量重判，不会被"文件没变过"缓存糊弄过去。
# 3：加了扫描件 OCR —— 以前被当"文字太少"跳过的 PDF/图片，这轮要重新走一遍。
# 4：OCR 改成一页一行地拼（表格不再挤成一坨），已经通过 OCR 入过库的也得重跑。
# 5：OCR 文本里的全角标点空格也合掉，文字更干净。
_GATE_VERSION = 5


def sensitive_reason(text: str) -> str:
    """这份文本该不该入库。返回空串 = 可以入库。"""
    ids = _ID_RE.findall(text)
    if ids:
        return "疑似含身份证号（%d 处），已拦下不入库" % len(ids)
    phones = _PHONE_RE.findall(text)
    if len(phones) >= _PHONE_MIN:
        return "疑似含手机号（%d 处），已拦下不入库" % len(phones)
    numbers = _LONGNUM_RE.findall(text)
    if len(numbers) >= _LONGNUM_MIN:
        return "疑似学生名单/花名册（含 %d 处学号一类的长编号），已拦下不入库" % len(numbers)
    return ""


def roster_like_name(name: str) -> str:
    """文件名像不像花名册。像的话返回命中的那个词，否则空串。"""
    for hint in _ROSTER_NAME_HINTS:
        if hint in (name or ""):
            return hint
    return ""


# ---------------------------------------------------------------- 切块


def chunk_text(text: str, size: int = 400, overlap: int = 80) -> List[str]:
    """把正文聚成不超过 size 的块。块间留一点重叠，答案跨界时不会被切没。"""
    paras = [p for p in (text or "").split("\n") if p.strip()]
    chunks: List[str] = []
    cur = ""
    for para in paras:
        if len(cur) + len(para) + 1 <= size:
            cur = (cur + "\n" + para) if cur else para
            continue
        if cur:
            chunks.append(cur)
            cur = (cur[-overlap:] + "\n" + para) if overlap > 0 else para
        else:
            cur = para
        while len(cur) > size:                      # 单段就超长时按 size 硬切
            chunks.append(cur[:size])
            cur = cur[max(0, size - overlap):]
    if cur.strip():
        chunks.append(cur)
    return [c.strip() for c in chunks if c.strip()]


# ---------------------------------------------------------------- 分词

_STOP = set("的了吗呢吧啊和与及或在是有个我你他她它这那之其也就都还只把被为对能给让"
            "通知关于各位同学大家以及进行需要要求请务必如下相关事宜")

# 疑问词/口头语。这些词在「通知」语料里很罕见，BM25 会给它们虚高的权重，
# 于是问「体测什么时候」反而把含「什么时候」的科普文排到了前面。检索前先丢掉。
_QUERY_STOP = set(
    "什么 什么时候 啥 啥时候 怎么 怎样 怎么样 咋样 如何 为啥 为什么 哪 哪里 哪儿 哪个 哪些 "
    "多少 几点 几号 几天 时候 时间点 是不是 是否 能否 能不能 可不可以 有没有 有木有 请问 "
    "一下 告诉 帮我 想问 问问 我该 我要 我想 我们 咱们 你们 这个 那个 这些 那些 还有 就是 "
    "主要 大概 现在 然后 顺便 麻烦 谢谢 辛苦 通知 消息 最近 呗 呀 哦 呃 嗯 嘛 哈哈 呵呵".split())


class Tokenizer:
    """有 jieba 就用 jieba，没有就用「单字 + 双字」兜底。

    中文没空格，双字切分（bigram）虽然糙，做关键词召回够用；
    装上 jieba 之后效果明显更好，所以它是可选的而不是硬依赖。
    """

    def __init__(self) -> None:
        try:
            import jieba

            jieba.setLogLevel(60)
            self._jieba = jieba
        except Exception:
            self._jieba = None
        self._cache: Dict[str, List[str]] = {}

    @property
    def name(self) -> str:
        return "jieba" if self._jieba is not None else "bigram"

    def tokens(self, text: str) -> List[str]:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        out = self._cut(text)
        if len(self._cache) > 4000:
            self._cache.clear()
        self._cache[text] = out
        return out

    def _cut(self, text: str) -> List[str]:
        text = (text or "").lower()
        words: List[str] = []
        if self._jieba is not None:
            for word in self._jieba.lcut(text):
                word = word.strip()
                if word and word not in _STOP and (len(word) >= 2 or word.isdigit()):
                    words.append(word)
            return words
        for chunk in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", text):
            if chunk.isascii():
                if len(chunk) >= 2:
                    words.append(chunk)
            elif len(chunk) == 1:
                words.append(chunk)
            else:
                words.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
        return [w for w in words if w not in _STOP]


# ---------------------------------------------------------------- 报告


@dataclass
class Chunk:
    doc_id: int
    title: str
    date: str
    text: str
    score: float = 0.0


@dataclass
class IndexReport:
    scanned: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped_unchanged: int = 0      # 上次就跳过了、这次连读都不用读的
    skipped: List[Tuple[str, str]] = field(default_factory=list)
    docs: int = 0
    chars: int = 0
    seconds: float = 0.0

    def summary(self) -> str:
        bits = ["扫到 %d 个文件" % self.scanned, "新增 %d" % self.added,
                "更新 %d" % self.updated, "未变 %d" % self.unchanged]
        if self.removed:
            bits.append("移除 %d" % self.removed)
        if self.skipped:
            bits.append("跳过 %d" % len(self.skipped))
        if self.skipped_unchanged:
            bits.append("另有 %d 个上次已跳过（不再重读）" % self.skipped_unchanged)
        return "、".join(bits) + "；库里现有 %d 份材料、约 %d 字（%.1fs）" % (
            self.docs, self.chars, self.seconds)


# ---------------------------------------------------------------- 主类


class Knowledge:
    """通知材料库：建索引（读文件 → 切块 → 存库）+ 检索（提问 → 最相关的块）。"""

    def __init__(
        self,
        db_path: Path,
        dirs: Sequence[str] = (),
        *,
        wechat_files: bool = False,
        exclude_keywords: Sequence[str] = (),
        min_chars: int = 40,
        max_file_mb: float = 12.0,
        max_docs: int = 400,
        root: Optional[Path] = None,
        ocr: Any = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(root) if root else self.db_path.parent.parent
        self.dirs = [d for d in dirs if d]
        self.wechat_files = bool(wechat_files)
        self.exclude_keywords = [k for k in exclude_keywords if k]
        self.min_chars = int(min_chars)
        self.max_file_mb = float(max_file_mb)
        self.max_docs = int(max_docs)

        def setting(name: str, default: Any) -> Any:
            if ocr is None:
                return default
            if isinstance(ocr, dict):
                return ocr.get(name, default)
            return getattr(ocr, name, default)

        # OCR：扫描件（没文字层的 PDF）和拍下来的照片，用 Windows 自带 OCR 认成文字
        self.ocr_enabled = bool(setting("enabled", False))
        self.ocr_lang = str(setting("lang", "") or "")
        self.ocr_dpi = int(setting("dpi", 300))
        self.ocr_max_pages = int(setting("max_pages", 10))
        self.ocr_max_chars = int(setting("max_chars", 3000))
        self.ocr_trigger_chars = int(setting("trigger_chars", 60))
        self.tokenizer = Tokenizer()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._postings: Optional[Dict[str, List[Tuple[int, int]]]] = None
        self._rows: List[Tuple[str, str, str]] = []
        self._lengths: List[int] = []
        self._avg_len = 0.0

    # ------------------------------------------------------------ 建表
    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS docs (
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE,
                title TEXT,
                date TEXT,
                kind TEXT,
                mtime REAL,
                size INTEGER,
                sha1 TEXT,
                chars INTEGER,
                added_at REAL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                doc_id INTEGER,
                ord INTEGER,
                text TEXT
            );
            CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
            CREATE TABLE IF NOT EXISTS skips (
                path TEXT PRIMARY KEY,
                mtime REAL,
                size INTEGER,
                reason TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 找文件
    @property
    def _exts(self) -> Tuple[str, ...]:
        """收哪些后缀。图片只在开着 OCR 时才收，不然读进来也是白读。"""
        return DOC_EXTS + (IMG_EXTS if self.ocr_enabled else ())

    def _local_files(self) -> List[Path]:
        out: List[Path] = []
        for one in self.dirs:
            base = Path(one)
            if not base.is_absolute():
                base = self.root / base
            if base.is_dir():
                out.extend(sorted(p for p in base.rglob("*")
                                  if p.is_file() and p.suffix.lower() in self._exts))
        return out

    def _wechat_file_roots(self) -> List[Path]:
        """微信收到/发出的文件都躺在 <账号目录>/msg/file/<年-月>/ 下面。"""
        db_dir = ""
        try:
            from wechatauto.db import auto_detect_db_dir

            db_dir = auto_detect_db_dir() or ""
        except Exception:
            db_dir = ""
        home = Path.home()
        bases = [p for p in (db_dir,
                             str(home / "xwechat_files"),
                             str(home / "Documents" / "xwechat_files"),
                             str(home / "Documents" / "WeChat Files"),
                             str(home / "Documents" / "xwechat_files")) if p]
        roots: List[Path] = []
        for base in bases:
            if not os.path.isdir(base):
                continue
            for name in os.listdir(base):
                for sub in ("msg/file", "msg\\file", "FileStorage/File"):
                    path = Path(base) / name / sub
                    if path.is_dir():
                        roots.append(path)
        return roots

    def _iter_files(self) -> Iterator[Tuple[Path, str]]:
        """产出 (文件, 来源)。来源是 "local"（knowledge 目录）或 "wechat"（微信缓存）。"""
        seen = set()
        for path in self._local_files():
            key = str(path.resolve()).lower()
            if key not in seen:
                seen.add(key)
                yield path, "local"
        if not self.wechat_files:
            return
        for root in self._wechat_file_roots():
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in self._exts:
                    continue
                key = str(path.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                yield path, "wechat"

    @staticmethod
    def _doc_date(path: Path) -> str:
        """优先用微信那种 <年-月> 目录名，退而求其次用文件修改时间。"""
        for part in path.parts:
            if re.fullmatch(r"\d{4}-\d{2}", part):
                return part
        try:
            return time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime))
        except OSError:
            return ""

    @staticmethod
    def _doc_title(path: Path) -> str:
        title = re.sub(r"\(\d+\)$", "", path.stem.strip()).strip()
        return title or path.name

    # ------------------------------------------------------------ 索引
    def index(self, force: bool = False, verbose: bool = False) -> IndexReport:
        started = time.time()
        report = IndexReport()
        gate_row = self._conn.execute("SELECT value FROM meta WHERE key='gate'").fetchone()
        if gate_row is None or gate_row["value"] != str(_GATE_VERSION):
            force = True          # 隐私规则升级过，全部重判一遍
        known = {row["path"]: row for row in self._conn.execute("SELECT * FROM docs")}
        # 被拦下的文件（含隐私、重复、扫描件）也按 mtime+size 记一笔，
        # 否则每次启动都要把那几十个学生名单重新解析一遍，白白等十几秒。
        prev_skips = {row["path"]: row for row in self._conn.execute("SELECT * FROM skips")}
        seen_paths = set()
        seen_skips = set()
        removed_any = False

        def remember_skip(reason):
            report.skipped.append((path.name, reason))
            if key in known:      # 以前进过库，现在不符合条件了（比如改了排除词）→ 清掉旧的
                with self._conn:
                    self._conn.execute("DELETE FROM chunks WHERE doc_id=?", (known[key]["id"],))
                    self._conn.execute("DELETE FROM docs WHERE id=?", (known[key]["id"],))
            if stat is not None:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO skips(path,mtime,size,reason) VALUES(?,?,?,?) "
                        "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, "
                        "size=excluded.size, reason=excluded.reason",
                        (key, stat.st_mtime, stat.st_size, reason))
                seen_skips.add(key)

        for path, source in self._iter_files():
            if report.scanned >= self.max_docs:
                report.skipped.append(("（还有更多文件）",
                                       "超过单次上限 %d 份，先建这批" % self.max_docs))
                break
            report.scanned += 1
            key = str(path)
            seen_paths.add(key)
            stat = None
            if any(kw in path.name for kw in self.exclude_keywords):
                remember_skip("文件名命中排除词")
                continue
            try:
                stat = path.stat()
            except OSError as exc:
                report.skipped.append((path.name, "读不到文件：%s" % exc))
                continue
            if source == "wechat":
                hint = roster_like_name(path.name)
                if hint:
                    remember_skip("文件名像花名册（含「%s」），微信自动收集的不入库" % hint)
                    continue
            old = known.get(key)
            if (old and not force
                    and abs(old["mtime"] - stat.st_mtime) < 0.5
                    and old["size"] == stat.st_size):
                report.unchanged += 1
                continue
            prev = prev_skips.get(key)
            if (prev is not None and not force
                    and abs(prev["mtime"] - stat.st_mtime) < 0.5
                    and prev["size"] == stat.st_size):
                seen_skips.add(key)              # 上次就跳过了，文件没动过，不用再看
                report.skipped_unchanged += 1
                continue
            if stat.st_size > self.max_file_mb * 1024 * 1024:
                remember_skip("文件太大（%.1f MB）" % (stat.st_size / 1048576.0))
                continue
            suffix = path.suffix.lower()
            text, kind, ocr_pages, ocr_ran = "", "", 0, False
            if suffix in IMG_EXTS:
                kind = "image"                     # 图片没有文字层，直接走 OCR
            else:
                try:
                    text, kind = extract_text(path)
                except Unsupported as exc:
                    if not (self.ocr_enabled and suffix == ".pdf"):
                        remember_skip(str(exc))
                        continue
                    kind = "pdf"                   # 扫描件：正文空，交给 OCR 兜底
                except Exception as exc:
                    remember_skip("读取失败：%s" % exc)
                    continue
            if (self.ocr_enabled and suffix in IMG_EXTS + (".pdf",)
                    and len(text) < self.ocr_trigger_chars):
                ocr_ran = True
                try:
                    if suffix in IMG_EXTS:
                        ocr_text, ocr_pages = ocr_image(path, lang=self.ocr_lang), 1
                    else:
                        ocr_text, ocr_pages, _total = ocr_pdf(
                            path, dpi=self.ocr_dpi, max_pages=self.ocr_max_pages,
                            max_chars=self.ocr_max_chars, lang=self.ocr_lang)
                except OcrUnavailable as exc:
                    if not text.strip():
                        remember_skip("扫描件，OCR 用不了：%s" % exc)
                        continue
                except Exception as exc:
                    if not text.strip():
                        remember_skip("扫描件，OCR 失败：%s" % exc)
                        continue
                else:
                    if len(ocr_text) > len(text):
                        text, kind = ocr_text, kind + "+ocr"
            if len(text) < self.min_chars:
                if ocr_ran:
                    remember_skip("文字太少（OCR 只认出 %d 字），大概是照片/表情包"
                                  % len(text))
                else:
                    remember_skip("文字太少（%d 字），大概是图片/扫描件" % len(text))
                continue
            reason = sensitive_reason(text)
            if reason:
                remember_skip(reason)
                continue
            digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
            twin = self._conn.execute(
                "SELECT title FROM docs WHERE sha1=? AND path<>? LIMIT 1",
                (digest, key)).fetchone()
            if twin is not None:
                # 之前把这副本存进库了的话，remember_skip 会顺手清掉，只留一份
                remember_skip("内容与《%s》相同（重复文件）" % twin["title"])
                continue
            try:
                self._store(path, text, kind, stat)
            except Exception as exc:
                remember_skip("入库失败：%s" % exc)
                continue
            with self._conn:                     # 这份现在进库了，旧的跳过记录作废
                self._conn.execute("DELETE FROM skips WHERE path=?", (key,))
            seen_skips.add(key)
            note = "（OCR %d 页）" % ocr_pages if ocr_pages and "ocr" in kind else ""
            if old:
                report.updated += 1
                if verbose:
                    print("   更新 %s（%d 字%s）" % (path.name, len(text), note))
            else:
                report.added += 1
                if verbose:
                    print("   新增 %s（%d 字%s）" % (path.name, len(text), note))
        for path in [p for p in known if p not in seen_paths]:
            # 文件人还在，只是这轮没扫它（--no-wechat、或者到单次上限被截断了）：
            # 那就不该当它被删了 —— 否则一次「只看 knowledge 目录」的小跑
            # 会把微信收来的材料全清空。
            if os.path.isfile(path):
                report.unchanged += 1
                continue
            doc_id = known[path]["id"]
            with self._conn:
                self._conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
                self._conn.execute("DELETE FROM docs WHERE id=?", (doc_id,))
            report.removed += 1
            removed_any = True
        if removed_any:
            # 材料被删掉以后，"重复文件"的判断前提变了，缓存全部作废重算
            with self._conn:
                self._conn.execute("DELETE FROM skips")
        else:
            for path in [p for p in prev_skips if p not in seen_skips]:
                with self._conn:
                    self._conn.execute("DELETE FROM skips WHERE path=?", (path,))
        with self._conn:
            self._conn.execute("INSERT INTO meta(key,value) VALUES('gate',?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (str(_GATE_VERSION),))
        self._conn.commit()
        self._postings = None
        stats = self.stats()
        report.docs = stats["docs"]
        report.chars = stats["chars"]
        report.seconds = time.time() - started
        return report

    def _store(self, path: Path, text: str, kind: str, stat: Any) -> None:
        digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
        with self._conn:
            self._conn.execute(
                "INSERT INTO docs(path,title,date,kind,mtime,size,sha1,chars,added_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET title=excluded.title, date=excluded.date, "
                "kind=excluded.kind, mtime=excluded.mtime, size=excluded.size, "
                "sha1=excluded.sha1, chars=excluded.chars",
                (str(path), self._doc_title(path), self._doc_date(path), kind,
                 stat.st_mtime, stat.st_size, digest, len(text), time.time()),
            )
            row = self._conn.execute("SELECT id FROM docs WHERE path=?", (str(path),)).fetchone()
            doc_id = int(row["id"])
            self._conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            self._conn.executemany(
                "INSERT INTO chunks(doc_id, ord, text) VALUES(?,?,?)",
                [(doc_id, i, c) for i, c in enumerate(chunk_text(text))],
            )

    def clear(self) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM chunks")
            self._conn.execute("DELETE FROM docs")
            self._conn.execute("DELETE FROM skips")
        self._postings = None

    # ------------------------------------------------------------ 检索
    def _build_postings(self) -> None:
        rows = list(self._conn.execute(
            "SELECT c.text AS text, d.title AS title, d.date AS date "
            "FROM chunks c JOIN docs d ON d.id = c.doc_id ORDER BY d.date DESC, c.ord"
        ))
        self._rows = []
        self._lengths = []
        postings: Dict[str, List[Tuple[int, int]]] = {}
        for row in rows:
            idx = len(self._rows)
            self._rows.append((row["title"], row["date"], row["text"]))
            # 标题写两遍 = 标题里的词权重更高（问「体测」时体测通知排前面）
            tokens = self.tokenizer.tokens("%s %s %s" % (
                row["title"], row["title"], row["text"]))
            self._lengths.append(max(1, len(tokens)))
            counts: Dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            for token, tf in counts.items():
                postings.setdefault(token, []).append((idx, tf))
        self._postings = postings
        self._avg_len = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0

    def search(self, query: str, k: int = 4, min_score: float = 0.5) -> List[Chunk]:
        """BM25 召回：问句里的词在哪些块里出现得又多又稀罕，就取那几块。

        另外加了一条「命中词数」门槛：问句里有两三个实词，却只碰上一个的块多半
        是碰巧（问「助学贷款」撞上创业计划书里的「贷款」），宁可不要。

        分数下限取「绝对下限」和「最高分的一个零头」里大的那个：库里材料少的时候
        IDF 天然偏低，死抠绝对值会把唯一正确的那份也给滤掉。
        """
        if self._postings is None:
            self._build_postings()
        postings = self._postings or {}
        if not postings or not (query or "").strip():
            return []
        total = len(self._rows)
        scores: Dict[int, float] = {}
        matched: Dict[int, set] = {}
        k1, b = 1.5, 0.75
        tokens = set(t for t in self.tokenizer.tokens(query) if t not in _QUERY_STOP)
        if not tokens:
            return []
        need = 2 if len(tokens) >= 2 else 1
        for token in tokens:
            hits = postings.get(token)
            if not hits:
                continue
            df = len(hits)
            idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
            for idx, tf in hits:
                dl = self._lengths[idx] or 1
                denom = tf + k1 * (1 - b + b * dl / (self._avg_len or 1.0))
                scores[idx] = scores.get(idx, 0.0) + idf * tf * (k1 + 1) / denom
                matched.setdefault(idx, set()).add(token)
        if not scores:
            return []
        out: List[Chunk] = []
        per_doc: Dict[str, int] = {}
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        floor = max(min_score, ranked[0][1] * 0.25)
        for idx, score in ranked:
            if score < floor:
                break
            if len(matched.get(idx, ())) < need:
                continue
            title, date, text = self._rows[idx]
            used = per_doc.get(title, 0)
            if used >= 2:                    # 同一份材料最多出两块，留位置给别的
                continue
            per_doc[title] = used + 1
            out.append(Chunk(doc_id=idx, title=title, date=date, text=text, score=score))
            if len(out) >= k:
                break
        return out

    def context(self, query: str, *, k: int = 4, max_chars: int = 2000,
                recent: int = 5) -> str:
        """给模型看的材料：最近的通知清单 + 与问题最相关的原文片段。"""
        if self.stats()["docs"] == 0:
            return ""
        lines: List[str] = []
        recent_docs = self.list_docs(limit=recent)
        if recent_docs:
            lines.append("我手头最近的通知（按时间）：")
            for doc in recent_docs:
                lines.append("- %s（%s）" % (doc["title"], doc["date"] or "日期不详"))
        hits = self.search(query, k=k)
        if hits:
            lines.append("")
            lines.append("和这个问题可能相关的原文：")
        used = 0
        for hit in hits:
            head = "《%s》%s：" % (hit.title, ("（%s）" % hit.date) if hit.date else "")
            block = head + "\n" + hit.text
            if used and used + len(block) > max_chars:
                break
            lines.append(block)
            used += len(block)
        return "\n".join(lines).strip()

    # ------------------------------------------------------------ 查询
    def stats(self) -> Dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS docs, COALESCE(SUM(chars),0) AS chars FROM docs").fetchone()
        chunks = self._conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        return {"docs": int(row["docs"]), "chars": int(row["chars"]),
                "chunks": int(chunks), "tokenizer": self.tokenizer.name}

    def list_docs(self, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT title, date, kind, chars, path FROM docs ORDER BY date DESC, title LIMIT ?",
            (int(limit),)).fetchall()
        return [dict(row) for row in rows]
