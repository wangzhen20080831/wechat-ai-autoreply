# -*- coding: utf-8 -*-
"""把「配置 + 大模型 + 记忆」拼成一句话进、一句话出。

这一层不碰微信，所以可以完全离线测试。main.py 只负责把微信的消息喂进来、
把结果发出去。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from .config import AppConfig
from .llm import LLMError
from .memory import Memory

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _time_context(now: Optional[float] = None) -> str:
    lt = time.localtime(now if now is not None else time.time())
    hour = lt.tm_hour
    if hour < 6:
        part = "凌晨"
    elif hour < 11:
        part = "早上"
    elif hour < 13:
        part = "中午"
    elif hour < 18:
        part = "下午"
    else:
        part = "晚上"
    return "%s%s %d:%02d" % (_WEEKDAYS[lt.tm_wday], part, hour, lt.tm_min)


class ReplyEngine:
    def __init__(self, cfg: AppConfig, llm: Any, memory: Memory,
                 knowledge: Any = None, sync: Any = None) -> None:
        self.cfg = cfg
        self.llm = llm
        self.memory = memory
        self.knowledge = knowledge      # 可选：通知材料库（dsbot/knowledge.py）
        self.sync = sync                # 可选：班级记忆同步（dsbot/session_sync.py）

    # ------------------------------------------------------------------ 上下文
    def build_messages(
        self,
        conv_id: str,
        user_text: str,
        *,
        conv_name: str = "",
        sender_name: str = "",
        is_group: bool = False,
        now: Optional[float] = None,
    ) -> List[Dict[str, str]]:
        system = self.cfg.system_prompt()
        facts = ["现在是%s。" % _time_context(now)]
        if is_group:
            who = sender_name or "群成员"
            facts.append("这是在群「%s」里，正在跟你说话的是「%s」，回复要简短、像群聊。" % (conv_name, who))
        elif conv_name:
            facts.append("你正在和「%s」单聊。" % conv_name)
        facts.append("直接输出要发出去的内容本身，不要加任何前缀、引号、括号里的动作描述或表情说明。")
        system = system + "\n\n补充信息：" + "".join(facts)

        material = self._material(user_text)
        if material:
            system += (
                "\n\n【手头有的通知材料】下面是我发过的通知（原文摘录）。回答通知相关的问题时：\n"
                "- 时间、地点、金额、截止日期这些必须跟材料完全一致，不要自己推、不要四舍五入、不要编。\n"
                "- 材料里没有的，就说「我确认一下再回你」这类话，别猜。\n"
                "- 别把材料原文整段复述给对方，用自己的话简短说，必要时提一句是哪份通知。\n\n" + material
            )

        shared = self._shared_memory()
        if shared:
            system += (
                "\n\n【班级记忆（其中一部分是我和班长助理刚聊过的，自动同步过来的）】"
                "这是我这边关于班级事务的记忆，用来回答同学关于通知、安排、值日、谁在负责之类的问题：\n"
                "- 里面写了的，就按它说，别自己另编一套；写的时间和数字必须和它一致。\n"
                "- 里面没写的、或者写着「待确认」的，就说「我问一下再回你」，不要猜。\n"
                "- 这是内部备忘，不要提「记忆」「文件」「共享」「会话」这些字眼，就当是自己记得的事。\n"
                "- 同学的身份证号、手机号、学号、宿舍号一律不说，问就说不方便说。\n"
                "- 里面「待确认」的、或者要替我做承诺的事，先问过我再说，不要自己答应。\n\n" + shared
            )

        messages: List[Dict[str, str]] = [{"role": "system", "content": system}]
        messages.extend(self.memory.history(conv_id))
        messages.append({"role": "user", "content": user_text})
        return messages

    def _material(self, query: str) -> str:
        if self.knowledge is None or not getattr(self.cfg, "knowledge", None):
            return ""
        kcfg = self.cfg.knowledge
        try:
            return self.knowledge.context(
                query, k=kcfg.max_chunks, max_chars=kcfg.max_chars, recent=kcfg.recent)
        except Exception as exc:      # 材料库坏了也不能把回复拖死
            log_msg = "通知材料检索失败：%r" % (exc,)
            try:
                import logging
                logging.getLogger("dsbot").warning(log_msg)
            except Exception:
                pass
            return ""

    def _shared_memory(self) -> str:
        """读一遍共享班级记忆（内容没变时是缓存命中，很便宜）。坏了也不能拖死回复。"""
        if self.sync is None:
            return ""
        try:
            return self.sync.pull()
        except Exception as exc:
            import logging
            logging.getLogger("dsbot").warning("共享记忆读取失败：%r", exc)
            return ""

    # ------------------------------------------------------------------ 主入口
    def reply(
        self,
        conv_id: str,
        user_text: str,
        *,
        conv_name: str = "",
        sender_name: str = "",
        is_group: bool = False,
        now: Optional[float] = None,
    ) -> str:
        messages = self.build_messages(
            conv_id,
            user_text,
            conv_name=conv_name,
            sender_name=sender_name,
            is_group=is_group,
            now=now,
        )
        raw = self.llm.chat(messages)
        answer = clean_reply(raw, self.cfg.reply.max_reply_chars)
        if not answer:
            raise LLMError("清洗之后回复为空")
        self.memory.append(conv_id, "user", user_text)
        self.memory.append(conv_id, "assistant", answer)
        if self.sync is not None:
            try:
                self.sync.export()
            except Exception as exc:
                import logging
                logging.getLogger("dsbot").warning("共享记忆同步失败：%r", exc)
        return answer


def clean_reply(text: str, max_chars: int = 300) -> str:
    """把模型输出收拾成「像真人发的一条微信」。"""
    out = (text or "").strip()
    if not out:
        return ""

    for left, right in (("「", "」"), ("“", "”"), ('"', '"'), ("'", "'")):
        if len(out) > 1 and out.startswith(left) and out.endswith(right):
            out = out[len(left):-len(right)].strip()

    out = re.sub(r"^\s*(?:assistant|AI|回复)\s*[:：]\s*", "", out, flags=re.IGNORECASE)
    out = out.replace("**", "").replace("__", "")
    out = re.sub(r"^\s*#{1,6}\s*", "", out, flags=re.MULTILINE)
    out = out.replace("`", "")
    out = re.sub(r"^\s*[-*]\s+", "", out, flags=re.MULTILINE)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()

    if max_chars > 0 and len(out) > max_chars:
        hard = out[:max_chars]
        # 优先在句末截断
        best = max([hard.rfind(m) for m in ("。", "！", "？", "…", "\n")] or [-1])
        if best >= max_chars * 0.5:
            return hard[:best + 1].rstrip()
        # 退而求其次，在逗号/顿号处断开并补省略号
        soft = max([hard.rfind(m) for m in ("，", ",", "、", "；", ";", " ")] or [-1])
        if soft > max_chars * 0.3:
            return hard[:soft].rstrip() + "…"
        return hard[:max_chars - 1].rstrip() + "…"
    return out
