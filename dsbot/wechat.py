# -*- coding: utf-8 -*-
"""微信侧的薄封装：把 wechatauto 的接口收拢成「收」「发」两个动作。

外面只依赖这个文件。将来要换成企业微信 / 微信客服 / 别的通道，
只要再写一个同样接口的类，main.py 不用动。

关于 wechatauto-replica（1.2.6.1）实测确认的几个细节：
  * 收消息 = 读微信本地 SQLCipher 数据库，增量轮询（Listener 内部调
    ``db.get_new_messages``），不注入、不 Hook、不改内存。
  * 发消息 = 窗口坐标 + OCR，剪贴板粘贴后回车，需要窗口可见且桌面未锁屏。
  * 消息 dict 的 ``type`` 是**中文字符串**（如 "文本" / "系统消息"），
    不是数字，所以类型判断要按字符串来。
  * 群消息的 ``content`` 前面带发送者前缀（``wxid_xxx:\\n真正的正文``），
    需要剥掉，否则会把 wxid 一起喂给模型。
  * ``sender_username`` 已经是解析好的发送者 wxid，可用来判断"是不是我自己发的"。
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback
import logging
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("dsbot")


def _squash(text: str) -> str:
    """去掉所有空白再比较：数据库里存的正文和我们要发的原文常常差在空格和换行。"""
    return "".join(str(text or "").split())


def _flush_output() -> None:
    """导入 wechatauto 之前先把缓冲区刷出去。

    实测：wechatauto 的导入过程会丢弃当前进程里尚未 flush 的 stdout 缓冲，
    导致"启动阶段打印的几行日志凭空消失"。所有进入 wechatauto 之前都先刷一次。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass

# 库返回的 type 中文字符串 -> 语义化类型
_KIND_BY_NAME: Dict[str, str] = {
    "文本": "text",
    "图片": "image",
    "语音": "voice",
    "视频": "video",
    "动画表情": "emoji",
    "位置": "location",
    "文件/链接/卡片": "file",
    "音视频通话": "call",
    "系统消息": "system",
    "红包": "redpacket",
}

# 老版本/别处可能给数字类型码，一并兼容
_KIND_BY_CODE: Dict[int, str] = {
    1: "text",
    3: "image",
    34: "voice",
    43: "video",
    47: "emoji",
    48: "location",
    49: "file",
    50: "call",
    10000: "system",
    11000: "emoji",
}

_PREFIX_RE = re.compile(r"^([A-Za-z0-9_@\.\-]{4,64}):\n")


def kind_of(msg_type: Any) -> str:
    """把消息类型统一成 text / image / ... 只有 text 才会被回复。"""
    if isinstance(msg_type, str):
        name = msg_type.strip()
        if name in _KIND_BY_NAME:
            return _KIND_BY_NAME[name]
        return "other"
    try:
        return _KIND_BY_CODE.get(int(msg_type), "other")
    except (TypeError, ValueError):
        return "other"


def strip_sender_prefix(content: str, sender_username: str = "") -> str:
    """群消息正文形如 ``wxid_xxx:\\n真正的正文``，把前缀剥掉。"""
    if not content:
        return ""
    if sender_username and content.startswith(sender_username + ":\n"):
        return content[len(sender_username) + 2:].lstrip("\n")
    m = _PREFIX_RE.match(content)
    if m:
        return content[m.end():]
    return content


def is_group_chat(username: str) -> bool:
    return bool(username) and username.endswith("@chatroom")


def list_accounts() -> List[Any]:
    """这台机器上所有「登录过微信」的账号目录，按最近活跃时间倒序。

    只读目录时间，不解密、不读内容，所以微信没登录也能用。返回
    ``[(目录名, 最近活跃时间戳)]``；目录名形如 ``wxid_xxx_abcd``。

    用途：同一台电脑上挂了两个微信号时，确认程序到底读的是哪一个。
    """
    try:
        from wechatauto.db import auto_detect_db_dir
        db_dir = auto_detect_db_dir()
    except Exception:
        return []
    if not db_dir:
        return []
    try:
        names = os.listdir(db_dir)
    except OSError:
        return []
    found = []
    for name in names:
        storage = os.path.join(db_dir, name, "db_storage")
        if not os.path.isdir(storage):
            continue
        newest = 0.0
        for root, _, files in os.walk(storage):
            for f in files:
                if f.endswith(".db"):
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(root, f)))
                    except OSError:
                        pass
        found.append((name, newest))
    found.sort(key=lambda item: item[1], reverse=True)
    return found


def pick_account_name() -> str:
    """程序默认会读哪个账号：最近活跃的那个（和库内部的选择规则一致）。"""
    accounts = list_accounts()
    return accounts[0][0] if accounts else ""


# ---------------------------------------------------------------- 群昵称
# 微信把每个群成员的「群昵称」存在 contact.db 的 chat_room.ext_buffer 里，
# 是个 protobuf：外层是重复的 field 1，每个 field 1 是一个成员
#   { 1: username, 2: 群昵称, 3: 是否群主 }
# 没设群昵称的成员，field 2 直接不存在。
#
# 这件事很关键：群里 @ 人用的是**群昵称**，不是微信昵称。别人 @ 你时写的是
# "@群昵称\u2005"，所以判断"有没有 @ 我"必须知道自己的群昵称，否则会静默不响应。


def _read_varint(buf: bytes, i: int):
    shift = 0
    value = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 63:
            break
    return value, i


def _parse_proto(buf: bytes):
    """极简 protobuf 解析，只处理 varint(0) 和 length-delimited(2)。"""
    out = []
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(buf, i)
            out.append((field, value))
        elif wire == 2:
            length, i = _read_varint(buf, i)
            out.append((field, buf[i:i + length]))
            i += length
        elif wire == 5:
            out.append((field, buf[i:i + 4]))
            i += 4
        elif wire == 1:
            out.append((field, buf[i:i + 8]))
            i += 8
        else:
            break
    return out


def parse_group_nicknames(ext_buffer) -> Dict[str, str]:
    """从 chat_room.ext_buffer 解析出 ``username -> 群昵称``。"""
    if isinstance(ext_buffer, str):
        return {}
    if isinstance(ext_buffer, (bytes, bytearray, memoryview)):
        raw = bytes(ext_buffer)
    else:
        try:
            raw = bytes(ext_buffer)
        except Exception:
            return {}

    result: Dict[str, str] = {}
    try:
        for field, value in _parse_proto(raw):
            if field != 1 or not isinstance(value, (bytes, bytearray)):
                continue
            username, nick = "", ""
            for sub_field, sub_value in _parse_proto(bytes(value)):
                if sub_field == 1 and isinstance(sub_value, (bytes, bytearray)):
                    username = bytes(sub_value).decode("utf-8", errors="replace")
                elif sub_field == 2 and isinstance(sub_value, (bytes, bytearray)):
                    nick = bytes(sub_value).decode("utf-8", errors="replace")
            if username:
                result[username] = nick
    except Exception:
        return {}
    return result


# ---------------------------------------------------------------- 消息里的 @ 名单
# 别人 @ 人时，微信会把「被 @ 到的 wxid」写进消息行的 source 字段：
# source 是 zstd 压缩的 <msgsource> XML，里面形如
#     <atuserlist><![CDATA[wxid_xxx,wxid_yyy]]></atuserlist>
# @所有人 写成 notify@all。
#
# 这是判断「有没有 @ 我」唯一可靠的办法。只按正文里的 "@名字" 猜是不行的：
# 群里 @ 用的是**群昵称**，而群昵称会撞车——实测一个群里两个人显示名完全一样
# （本人 + 一位同学），只按文字判断就会把 @ 那位同学的消息也回掉。

AT_ALL_TOKENS = frozenset({"notify@all", "announcement@all"})

_ATUSERS_RE = re.compile(r"<atuserlist>(.*?)</atuserlist>", re.S)
_AT_SPLIT_RE = re.compile(r"[,;，、\s]+")


def _zstd_text(raw: bytes) -> Optional[str]:
    try:
        import zstandard
    except Exception:
        return None
    try:
        return zstandard.ZstdDecompressor().decompressobj().decompress(raw).decode("utf-8", "replace")
    except Exception:
        return None


def decode_msgsource(blob) -> Optional[str]:
    """把消息行的 source 解成 <msgsource> 文本。

    返回 None 表示**拿不到名单**（空 source、压缩算法不认识、压根不是 msgsource），
    调用方此时应退回「按正文里的名字猜」；返回字符串表示确实读到了。
    """
    if blob is None:
        return None
    if isinstance(blob, str):
        return blob if "msgsource" in blob else None
    if isinstance(blob, (bytes, bytearray, memoryview)):
        raw = bytes(blob)
    else:
        return None
    if not raw:
        return None                      # 空 source：微信没写名单，只能兜底
    text = _zstd_text(raw)
    if text is None:
        try:
            text = raw.decode("utf-8")
        except Exception:
            return None
    return text if "msgsource" in text else None


def parse_at_usernames(msgsource: str) -> List[str]:
    """从 <msgsource> 里取「这条消息 @ 了谁」，返回 wxid 列表（@所有人 = notify@all）。"""
    if not msgsource:
        return []
    names: List[str] = []
    for chunk in _ATUSERS_RE.findall(msgsource):
        chunk = chunk.replace("<![CDATA[", "").replace("]]>", "")
        for item in _AT_SPLIT_RE.split(chunk):
            if item and item not in names:
                names.append(item)
    return names


def extract_at_usernames(blob) -> Optional[List[str]]:
    """None = 拿不到名单；[] = 这条消息确实没 @ 人；[...] = 精确的 wxid 列表。"""
    text = decode_msgsource(blob)
    if text is None:
        return None
    return parse_at_usernames(text)


class WeChatIO:
    """封装收发。所有发送串行化，避免并发操作抢占鼠标和剪贴板。"""

    def __init__(self, send_interval: float = 1.2, verify: bool = True,
                 account: str = "") -> None:
        self.send_interval = max(0.0, float(send_interval))
        self.verify = bool(verify)
        self.account = (account or "").strip()   # 空 = 自动挑最近活跃的账号
        self._send_lock = threading.Lock()
        self._last_send_at = 0.0
        self._gui = None
        self._db = None
        self._names: Dict[str, str] = {}
        self._self_username = ""
        self._group_nicks: Dict[str, Dict[str, str]] = {}

    # ------------------------------------------------------------------ 初始化
    def connect(self) -> Dict[str, Any]:
        _flush_output()
        from wechatauto.db import WeChatDB

        self._db = WeChatDB(account=self.account or None)
        info = self._db.get_self_info() or {}
        self._self_username = str(info.get("username") or "")
        return {
            "nick_name": info.get("nick_name") or "",
            "username": self._self_username,
            "account_dir": getattr(self._db, "account", "") or "",
            "raw": info,
        }

    @property
    def db(self):
        return self._db

    # ------------------------------------------------------------------ 名称
    def group_nicknames(self, chatroom: str) -> Dict[str, str]:
        """``username -> 群昵称``（只有设过群昵称的成员才有值）。"""
        cached = self._group_nicks.get(chatroom)
        if cached is not None:
            return cached
        conn = None
        try:
            conn = self._db._contact_conn()
            row = conn.execute(
                "SELECT ext_buffer FROM chat_room WHERE username=? LIMIT 1",
                (chatroom,),
            ).fetchone()
        except Exception:
            traceback.print_exc()
            return {}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        if not row:
            self._group_nicks[chatroom] = {}
            return {}
        raw = row["ext_buffer"] if not isinstance(row, (tuple, list)) else row[0]
        parsed = parse_group_nicknames(raw)
        self._group_nicks[chatroom] = parsed
        return parsed

    def my_group_name(self, chatroom: str, self_wxid: str = "") -> str:
        """我在这个群里被人 @ 时用的名字。"""
        self_wxid = self_wxid or self._self_username
        return self.group_nicknames(chatroom).get(self_wxid, "")

    def at_usernames(self, conv_id: str, local_id, shard: str = "") -> Optional[List[str]]:
        """读某条消息的「@ 名单」（精确到 wxid）。

        返回 None 表示读不到原始行（调用方退回按名字判断），[] 表示确实没 @ 人。
        """
        if not conv_id or local_id is None:
            return None
        try:
            row = self._db.get_message_row(conv_id, int(local_id), shard=shard or None)
        except Exception:
            traceback.print_exc()
            return None
        if not row:
            return None
        return extract_at_usernames(row.get("source"))

    def name_map(self, refresh: bool = False) -> Dict[str, str]:
        """``username -> 备注/昵称`` 一次性拿全（库内部有缓存，很快）。"""
        if not self._names or refresh:
            try:
                self._names = dict(self._db.nickname_map(refresh=refresh) or {})
            except Exception:
                traceback.print_exc()
        return self._names

    def display_name(self, username: str) -> str:
        """给会话找一个人能看懂的名字；找不到就退回 username。"""
        if not username:
            return ""
        cached = self._names.get(username)
        if cached:
            return cached
        name = self.name_map().get(username)
        if name:
            return name
        if is_group_chat(username):
            try:
                gname = self._db.group_id_to_name(username)
                if gname:
                    self._names[username] = gname
                    return gname
            except Exception:
                pass
        try:
            single = self._db.get_nickname(username)
            # get_nickname 查不到时会原样返回 username，那种情况不算找到
            if single and single != username:
                self._names[username] = single
                return single
        except Exception:
            pass
        return username

    # ------------------------------------------------------------------ 只读
    def list_sessions(self, limit: int = 500) -> List[Dict[str, Any]]:
        return list(self._db.get_sessions(limit=limit) or [])

    def resolve_username(self, keyword: str) -> str:
        """把昵称/备注换成会话 username（wxid_xxx 或 xxx@chatroom）。"""
        if not keyword:
            return keyword
        if keyword.startswith("wxid_") or is_group_chat(keyword):
            return keyword
        try:
            hits = self._db.search_contact(keyword)
            if hits:
                return str(hits[0].get("username") or keyword)
        except Exception:
            pass
        try:
            got = self._db.username_by_nickname(keyword)
            if got:
                return str(got)
        except Exception:
            pass
        return keyword

    # ------------------------------------------------------------------ 监听
    def listen(self, on_message: Callable[[Dict[str, Any]], None]) -> Any:
        """注册全局监听（运行中自动发现新会话），返回 Listener，需自行 start/stop。"""
        _flush_output()
        from wechatauto.db import Listener

        listener = Listener(self._db, interval=1.0)

        def _callback(msg: dict, _listener) -> None:
            try:
                on_message(dict(msg))
            except Exception:
                # 单条消息处理失败不能拖垮监听线程
                traceback.print_exc()

        listener.add_all(_callback, discover=True)
        return listener

    # ------------------------------------------------------------------ 发送
    def send_text(self, who: str, text: str) -> bool:
        """who 是对方在微信里显示的名字（昵称/备注/群名），走搜索框定位。

        发出去之后**不再重发**：wechatauto 判断"发出去没有"靠的是界面状态
        （回车后输入框有没有清空），界面上偶发的一两次误判会让它把同一段话
        再发一遍，对方就收到两条。所以判失败时我们回数据库看一眼——确实已经
        落库了就算成功，界面说了什么不算。
        """
        if not text.strip() or not who:
            return False
        with self._send_lock:
            gap = time.time() - self._last_send_at
            if gap < self.send_interval:
                time.sleep(self.send_interval - gap)
            started_at = time.time()
            try:
                gui = self._ensure_gui()
                resp = gui.send_msg(text, who, verify=self.verify)
                ok = bool(getattr(resp, "is_success", False))
                if not ok and isinstance(resp, dict):
                    ok = bool(resp.get("is_success")) or resp.get("status") == "成功"
                if not ok:
                    # 界面说没发出去 —— 但消息可能已经在路上了。回读数据库确认，
                    # 确认到了就按成功算（尤其不能再触发上层重发）。
                    ok = self.text_landed(who, text, since=started_at)
                    if ok:
                        log.warning("界面判定「没发出去」，但数据库回读确认已发出——"
                                    "按成功处理，不重发（目标「%s」）", who)
                return ok
            except Exception:
                traceback.print_exc()
                return False
            finally:
                self._last_send_at = time.time()

    def text_landed(self, who: str, text: str, since: float = 0.0,
                    limit: int = 15) -> bool:
        """回读数据库：这段文字在 since 之后是否真的落到了这个会话里。

        只看正文和时间，不看界面状态。读不到数据库（没连过、会话没解析出来）
        一律返回 False —— 宁可让上层记一条"发送失败"，也不要谎报成功。
        """
        want = _squash(text)
        if not want or self._db is None:
            return False
        try:
            username = self.resolve_username(who)
            rows = self._db.get_messages(username, limit=max(1, int(limit))) or []
        except Exception:
            traceback.print_exc()
            return False
        for row in rows:
            try:
                ts = float(row.get("create_time") or 0)
            except (TypeError, ValueError):
                continue
            if since and ts + 3 < since:      # 只认这次发送之后的
                continue
            if _squash(str(row.get("content") or "")) == want:
                return True
        return False

    def _ensure_gui(self):
        """发送依赖 GUI；只在真要发第一条消息时才初始化它。"""
        if self._gui is None:
            _flush_output()
            from wechatauto import WeChatGUI
            self._gui = WeChatGUI()
        return self._gui
