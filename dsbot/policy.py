# -*- coding: utf-8 -*-
"""触发策略：这条消息到底回不回。

这是整套系统里最该保守的一层。默认配置下只回复白名单里的私聊、纯文本、
不在免打扰时段、且不违反冷却时间的消息；群聊一律不回。
宁可漏回，也不要乱回——乱回一次就可能得罪人，甚至把消息串到别人的会话里。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Set, Tuple

from .config import TriggerConfig
from .wechat import AT_ALL_TOKENS


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    text: str = ""          # 清洗后的文本（去掉 @我、命令前缀、多余空白）
    ack: bool = False       # 只是在应一声（行 / ok / 好的），不用回但值得记进记忆


# 「对方只是在应一声」：单独发这些就不回。叠着说也算（「嗯好的」「好的谢谢」）。
# 为什么要有这一层：同学回个「行」，机器人再回一句「好嘞」，像话痨，也更容易露馅。
ACK_WORDS = (
    "行", "好的", "好嘞", "好滴", "好哒", "好呢", "好", "中", "妥了", "妥",
    "嗯", "恩", "哦", "噢", "喔", "哦了",
    "知道了", "知道啦", "知道", "明白", "明白了", "明白啦", "了解", "了解啦",
    "收到", "可以", "可以了", "没问题", "没问题的", "是的", "对", "对的",
    "谢谢", "谢谢你", "谢谢啦", "谢谢班长", "谢了", "谢啦", "多谢", "感谢",
    "辛苦了", "辛苦", "麻烦你了", "麻烦你", "麻烦了", "麻烦", "有劳", "受累了",
    "ok", "ok啦", "okk", "okkk", "ojbk", "3q", "thx", "thanks", "thankyou",
)
# 尾巴语气词：「行吧」「可以了」「嗯呢」这种也算应一声
ACK_TAIL = ("吧", "了", "啦", "呀", "哈", "哦", "啊", "的", "呢", "噢", "喔", "叻", "哒")
_ACK_SORTED = tuple(sorted(ACK_WORDS, key=len, reverse=True))
_ACK_QUESTION = ("吗", "多少", "怎么", "什么", "哪", "几", "是不是", "有没有")


def is_ack(text: str, extra: Sequence[str] = ()) -> bool:
    """这句是不是「只是在应一声」？

    判断得保守一点：整句能拆成一串应答词（允许叠加），或者拆完只剩一个语气尾巴。
    带问号、带「吗/怎么/多少」这类疑问词的，一律当正常消息回。
    """
    raw = (text or "").strip()
    if not raw or any(ch in raw for ch in "?？"):
        return False
    if any(word in raw for word in _ACK_QUESTION):
        return False
    core = re.sub(r"[^\w\u4e00-\u9fff]+", "", raw.lower())
    if not core:
        return True                 # 只有表情或标点（「👍」「。。。」）
    if len(core) > 12:
        return False
    words = _ACK_SORTED + tuple(
        sorted((str(w).strip().lower() for w in extra if str(w).strip()),
               key=len, reverse=True))
    rest, guard = core, 0
    while rest and guard < 8:
        guard += 1
        for word in words:
            if rest.startswith(word):
                rest = rest[len(word):]
                break
        else:
            break
    if not rest:
        return True
    return rest in ACK_TAIL


def _norm(value: str) -> str:
    return (value or "").strip().lower()


def _in_quiet_hours(now_ts: float, start: str, end: str) -> bool:
    """判断是否落在免打扰时段，支持跨零点（例如 23:30 ~ 07:30）。"""
    def _minutes(spec: str) -> Optional[int]:
        try:
            hh, mm = spec.strip().split(":")
            h, m = int(hh), int(mm)
            if not (0 <= h < 24 and 0 <= m < 60):
                return None
            return h * 60 + m
        except Exception:
            return None

    start_m, end_m = _minutes(start), _minutes(end)
    if start_m is None or end_m is None or start_m == end_m:
        return False
    cur = time.localtime(now_ts)
    now_m = cur.tm_hour * 60 + cur.tm_min
    if start_m < end_m:
        return start_m <= now_m < end_m
    return now_m >= start_m or now_m < end_m       # 跨零点


class Policy:
    def __init__(
        self,
        trigger: TriggerConfig,
        self_names: Optional[Sequence[str]] = None,
        self_wxid: str = "",
    ) -> None:
        self.trigger = trigger
        self.self_names = [n for n in (self_names or []) if n]
        # 「我」的 wxid 集合：当前账号 + 配置里额外登记的（例如同一台机器上的另一个号）
        self.self_ids = set()
        for one in [self_wxid] + list(getattr(trigger, "extra_at_ids", []) or []):
            one = (one or "").strip()
            if one:
                self.self_ids.add(one)
        self.self_wxid = (self_wxid or "").strip()
        self._lock = threading.Lock()
        self._last_reply: dict = {}

    # ------------------------------------------------------------------ 工具
    def _mention_names(self, alias_names: Sequence[str] = ()) -> List[str]:
        """判断/剥离「@我」时认的名字。

        三个来源取并集：配置里的 ``trigger.at_names``、我的微信昵称、
        以及**本群的群昵称**（群昵称每个群都可能不一样，所以按条传进来）。
        """
        out: List[str] = []
        for raw in list(self.trigger.at_names) + list(self.self_names) + list(alias_names or ()):
            name = (raw or "").strip().lstrip("@").strip()
            if name and name not in out:
                out.append(name)
        return out

    def _strip_mention(self, text: str, names: Optional[Sequence[str]] = None) -> Tuple[bool, str]:
        """判断是否 @ 了我，并把 @我的那块去掉，剩下的才是真正的内容。"""
        mentioned = False
        out = text
        pool = list(names) if names is not None else self._mention_names()
        for name in sorted([n for n in pool if n], key=len, reverse=True):
            for token in ("@" + name, "@" + name + "\u2005", "@" + name + " "):
                if token and token in out:
                    mentioned = True
                    out = out.replace(token, " ")
        return mentioned, " ".join(out.split()).strip()

    def _match_any(self, candidates: Sequence[str], values: Set[str]) -> bool:
        return any(_norm(c) in values for c in candidates if c)

    # ------------------------------------------------------------------ 主逻辑
    def decide(
        self,
        *,
        conv_id: str,
        conv_name: str = "",
        sender_name: str = "",
        is_group: bool = False,
        kind: str = "text",
        text: str = "",
        is_self: bool = False,
        now_ts: Optional[float] = None,
        alias_names: Sequence[str] = (),
        at_usernames: Optional[Sequence[str]] = None,
    ) -> Decision:
        """``alias_names``：本群里我可能被 @ 到的名字（群昵称）。

        ``at_usernames``：微信自己记录的「这条消息 @ 了谁」的 wxid 列表。
        传了就以它为准（精确，不受重名影响）；传 None 才退回按正文里的名字猜。
        """
        now = float(now_ts if now_ts is not None else time.time())
        t = self.trigger

        if is_self:
            return Decision(False, "自己发的消息")
        if kind != "text":
            return Decision(False, f"非文本消息（{kind}）")

        raw = (text or "").strip()
        if not raw:
            return Decision(False, "空消息")
        if len(raw) > 3000:
            return Decision(False, "消息过长（>3000 字）")

        at_names = self._mention_names(alias_names)
        text_mentioned, cleaned = self._strip_mention(raw, at_names)
        at_all = False
        if at_usernames is None:
            mentioned = text_mentioned
        else:
            ats = [str(u).strip() for u in at_usernames if str(u).strip()]
            at_all = any(u in AT_ALL_TOKENS for u in ats)
            mentioned = any(u in self.self_ids for u in ats)
            # 微信没记名单（有人只是手打「@名字」而没从列表里选）时可开这个兜底
            if not mentioned and not at_all and t.at_text_fallback:
                mentioned = text_mentioned
        body = cleaned or raw

        if is_group:
            if t.groups == "ignore":
                return Decision(False, "群聊默认不回复")
            if t.groups == "at" and not mentioned:
                if at_all:
                    return Decision(False, "群里是 @所有人，不是单独 @ 我")
                if at_usernames is None:
                    return Decision(False, "群里没有 @ 我")
                return Decision(False, "群里没有 @ 我（微信的 @ 名单里没有我）")

        if t.ignore_keywords and any(k in raw for k in t.ignore_keywords):
            return Decision(False, "命中忽略关键词")

        if t.require_prefix:
            if not body.startswith(t.require_prefix):
                return Decision(False, "没有以「%s」开头" % t.require_prefix)
            body = body[len(t.require_prefix):].strip()
            if not body:
                return Decision(False, "只有前缀没有内容")

        names = {_norm(conv_id), _norm(conv_name), _norm(sender_name)}
        if t.blacklist and self._match_any(t.blacklist, names):
            return Decision(False, "在黑名单里")

        if t.quiet_hours.enabled and _in_quiet_hours(now, t.quiet_hours.start, t.quiet_hours.end):
            return Decision(False, "免打扰时段")

        if t.mode == "whitelist":
            if not t.whitelist:
                return Decision(False, "白名单为空（防止误回所有人）")
            if not self._match_any(t.whitelist, names):
                return Decision(False, "不在白名单里")
        elif t.mode != "all":
            return Decision(False, "未知的 trigger.mode=%s" % t.mode)

        # 只是在应一声（行 / ok / 好的 / 谢谢）：不用回，但会记进记忆 ——
        # 不然下次开口时它不知道对方已经应过了，容易又问一遍。
        # 放在这里（白名单、免打扰都过了之后）是为了：本来就不该回的会话，
        # 别因为一句「行」把内容也记进记忆、再同步出去。
        if t.skip_acks and is_ack(body, t.ack_words):
            return Decision(False, "对方只是在应一声，不用回", body, True)

        with self._lock:
            last = float(self._last_reply.get(conv_id, 0.0))
        if t.cooldown > 0 and now - last < t.cooldown:
            return Decision(False, "冷却中（%gs 内只回一次）" % t.cooldown)

        return Decision(True, "ok", body)

    def note_reply(self, conv_id: str, ts: Optional[float] = None) -> None:
        """回完一条之后打个时间戳，用于冷却判断。"""
        with self._lock:
            self._last_reply[conv_id] = float(ts if ts is not None else time.time())

    def watches(self, *, conv_id: str, conv_name: str = "", is_group: bool = False) -> bool:
        """这个会话在不在机器人的管辖范围里（只看名单，不看免打扰和冷却）。

        用来判断「你自己在微信里回的话」值不值得记进它的记忆：只管它本来会插话的
        那些会话。别的私聊不进记忆，也就不会被同步到共享记忆里。
        """
        t = self.trigger
        names = {_norm(conv_id), _norm(conv_name)}
        if t.blacklist and self._match_any(t.blacklist, names):
            return False
        if is_group:
            return t.groups != "ignore"
        if t.mode == "all":
            return True
        if t.mode != "whitelist" or not t.whitelist:
            return False
        return self._match_any(t.whitelist, names)
