# -*- coding: utf-8 -*-
"""主程序：监听微信消息 → DeepSeek 生成回复 → 发回去。

常用命令：
    python main.py --list-sessions      列出所有会话（用来填白名单）
    python main.py --once "在吗"        只跑一次大脑，不碰微信，验证配置
    python main.py --mock --dry-run     全流程演练：不出网、不发送
    python main.py                      正式运行（Ctrl+C 停止）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    os.system("chcp 65001 >nul 2>&1")
except Exception:
    pass
try:
    # line_buffering=True 很关键：wechatauto 的导入过程会丢弃尚未 flush 的输出缓冲，
    # 不逐行刷的话，启动阶段打印的日志会凭空消失。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except AttributeError:
    pass

from dsbot import DeepSeekClient, Memory, MockLLM, Policy, ReplyEngine, load_config
from dsbot.config import AppConfig
from dsbot.knowledge import Knowledge
from dsbot.lockfile import InstanceLock
from dsbot.retryqueue import RetryQueue
from dsbot.session_sync import SessionSync
from dsbot.session_memory import SessionMemoryReader
from dsbot.wechat import (
    WeChatIO,
    is_group_chat,
    kind_of,
    list_accounts,
    strip_sender_prefix,
)

log = logging.getLogger("dsbot")


def setup_logging(cfg: AppConfig) -> None:
    """日志同时写屏幕和 data/bot.log。

    这里不能用 logging.basicConfig：微信自动化那套库（wechatauto/logger.py）在
    第一次真正连微信时会执行 root_logger.handlers.clear()，把挂在 root logger 上
    的文件 handler 一并清掉 —— 现象就是「机器人明明在跑，data\\bot.log 里一个字
    都没有」。所以 handler 直接钉在「dsbot」这个 logger 上，并关掉向上传播
    （否则屏幕上会打出两份）。
    """
    log_path = cfg.abspath(cfg.log.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            datefmt="%m-%d %H:%M:%S")
    for handler in list(log.handlers):          # 重复调用时别叠加
        log.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)
    try:
        to_file = logging.FileHandler(log_path, encoding="utf-8")
    except Exception as exc:
        print("警告：日志文件打不开（%s），只输出到屏幕" % exc)
    else:
        to_file.setFormatter(fmt)
        log.addHandler(to_file)
    log.setLevel(getattr(logging, cfg.log.level, logging.INFO))
    log.propagate = False


def warn_already_running(pid: str) -> None:
    """第二个实例启动时的提示。

    桌面快捷方式是「最小化启动」的，光在控制台里打印，用户根本看不见 —— 会以为
    双击没反应，然后再戳几下。所以除了打印，再弹一个最顶层的小窗口说清楚。
    """
    lines = [
        "已经有一个机器人在跑了（PID %s），不用再开一个。" % pid,
        "重复开两个，同一条消息会被回复两遍。",
        "",
        "要重启的话，先关掉那个最小化的黑窗口，再双击桌面快捷方式。",
    ]
    print()
    for line in lines:
        print(line)
    try:
        if os.environ.get("DSBOT_NO_POPUP"):
            return          # 自测/脚本里不弹窗，免得卡住没人点确定
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, "\n".join(lines), "DeepSeek 微信自动回复",
            0x00040040,      # MB_ICONINFORMATION | MB_TOPMOST
        )
    except Exception:
        pass


def build_llm(cfg: AppConfig, mock: bool):
    if mock:
        log.warning("使用 MockLLM（离线回声模式），不会真的调用 DeepSeek")
        return MockLLM()
    return DeepSeekClient(
        api_key=cfg.model.api_key,
        base_url=cfg.model.base_url,
        model=cfg.model.name,
        temperature=cfg.model.temperature,
        max_tokens=cfg.model.max_tokens,
        timeout=cfg.model.timeout,
    )


def build_knowledge(cfg: AppConfig) -> Optional[Knowledge]:
    """通知材料库。配置里关掉就返回 None，整套逻辑照旧跑。"""
    kcfg = cfg.knowledge
    if not kcfg.enabled:
        return None
    return Knowledge(
        cfg.abspath(kcfg.db_path),
        dirs=kcfg.dirs,
        wechat_files=kcfg.wechat_files,
        exclude_keywords=kcfg.exclude_keywords,
        min_chars=kcfg.min_chars,
        max_file_mb=kcfg.max_file_mb,
        max_docs=kcfg.max_docs,
        root=cfg.root,
        ocr=kcfg.ocr,
    )


def load_name_cache(cfg: AppConfig) -> Dict[str, str]:
    """上次跑机器人时存下的「wxid -> 名字」。没有微信窗口时也能把同步文件写成人名。"""
    try:
        data = json.loads(cfg.abspath("data/names.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if str(v).strip()}


def build_sync(cfg: AppConfig, memory: Memory, name_of=None) -> Optional[SessionSync]:
    """班级记忆同步。配置里没开就返回 None，整套逻辑照旧跑。"""
    scfg = cfg.sync
    if not scfg.enabled or not scfg.dir:
        return None
    cached = load_name_cache(cfg)

    def resolve(conv_id: str) -> str:
        if name_of is not None:
            try:
                got = name_of(conv_id)
            except Exception:
                got = ""
            if got:
                return str(got)
        return cached.get(conv_id) or conv_id

    sync = SessionSync(
        cfg.abspath(scfg.dir),
        memory,
        name_of=resolve,
        max_chars=scfg.max_chars,
        dialog_keep=scfg.dialog_keep,
        log=log,
        session=(SessionMemoryReader(
            scfg.session_thread,
            max_chars=scfg.session_max_chars,
            items=scfg.session_items,
            tail_mb=scfg.session_tail_mb,
            skip_user=tuple(scfg.session_skip_user),
            log=log,
        ) if (scfg.session_read and scfg.session_thread) else None),
    )
    sync.prepare()
    return sync


def index_knowledge(cfg: AppConfig, force: bool = False, verbose: bool = True) -> int:
    """建/更新通知索引，把结果说清楚（含被拦下的文件）。"""
    know = build_knowledge(cfg)
    if know is None:
        print("配置里 knowledge.enabled = false，材料库没启用。")
        return 1
    report = know.index(force=force, verbose=verbose)
    print("通知材料库：%s" % report.summary())
    if report.skipped:
        print("\n跳过 %d 个（这些不会被 AI 引用）：" % len(report.skipped))
        for name, why in report.skipped[:40]:
            print("   %s —— %s" % (name[:52], why))
        if len(report.skipped) > 40:
            print("   …… 还有 %d 个" % (len(report.skipped) - 40))
    docs = know.list_docs(limit=12)
    if docs:
        print("\n库里最新的 12 份：")
        for doc in docs:
            print("   %-10s %-46s %d 字" % (doc["date"] or "?", doc["title"][:44], doc["chars"]))
    print("\n分词方式：%s（装了 jieba 会明显更准）" % know.stats()["tokenizer"])
    know.close()
    return 0


class WeChatBot:
    def __init__(self, cfg: AppConfig, llm: Any, *, no_send: bool = False,
                 account: str = "") -> None:
        self.cfg = cfg
        self.no_send = no_send
        self.account = account or cfg.wechat.account
        self.io = WeChatIO(send_interval=1.2, verify=cfg.wechat.verify,
                           account=self.account)
        self.memory = Memory(
            cfg.abspath(cfg.memory.db_path),
            max_turns=cfg.memory.max_turns,
            ttl_hours=cfg.memory.ttl_hours,
        )
        self.knowledge = build_knowledge(cfg)
        self.sync = build_sync(cfg, self.memory, name_of=self.io.display_name)
        self.engine = ReplyEngine(cfg, llm, self.memory, knowledge=self.knowledge,
                                  sync=self.sync)
        # 生成失败（DeepSeek 超时/503）时别把消息丢了：记成待办，自己补回来。
        self.retry = RetryQueue(
            self.memory,
            retry_delay=cfg.reply.retry_delay,
            max_retries=cfg.reply.max_retries,
            flush_seconds=cfg.reply.pending_flush_seconds,
            max_age_hours=cfg.reply.pending_max_age_hours,
            deliver=self._redeliver,
            log=log,
        )
        self.policy: Optional[Policy] = None
        self.nick = ""
        self.wxid = ""
        self._lock = threading.Lock()
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._replied = 0
        self._skipped = 0
        self._mirrored = 0            # 记了多少条「你自己手动回的」
        self._own_sent: Dict[str, list] = {}   # 机器人刚发出去的内容，用来认自己的回声
        self._own_at: Dict[str, float] = {}    # 你最近一次手动回某个会话的时间
        self._seen_group: set = set()
        self._sync_text = ""

    # ------------------------------------------------------------ 启动
    def start(self) -> None:
        accounts = list_accounts()
        if accounts:
            log.info("这台机器上有 %d 个微信号：%s", len(accounts),
                     "、".join("%s（%s）" % (name, _ago(ts)) for name, ts in accounts))
        info = self.io.connect()
        self.nick = info.get("nick_name") or ""
        self.wxid = info.get("username") or ""
        log.info("已连上微信账号：%s (%s)", self.nick or "?", self.wxid or "?")
        if self.account:
            log.info("账号是按配置锁定的（%s）", info.get("account_dir") or self.account)
        if accounts and self.wxid and not accounts[0][0].startswith(self.wxid):
            log.warning("注意：最近活跃的微信号是 %s，本次读的是 %s。"
                        "如果现在微信登录的不是后者，读到的消息会是另一个号的——"
                        "确认一下，或者用 --account 锁定。", accounts[0][0], self.wxid)

        at_names = [n for n in (self.nick,) if n] + list(self.cfg.trigger.at_names)
        self.policy = Policy(self.cfg.trigger, self_names=at_names, self_wxid=self.wxid)

        if self.cfg.trigger.mode == "whitelist":
            log.info("白名单模式，共 %d 个对象：%s", len(self.cfg.trigger.whitelist),
                     "、".join(self.cfg.trigger.whitelist) or "（空 —— 谁都不会回）")
        else:
            log.warning("trigger.mode=all —— 会回复每一个私聊，请确认这是你要的")
        log.info("群聊策略：%s", {"ignore": "不回复", "at": "只有 @ 我才回", "all": "全部回复"}.get(
            self.cfg.trigger.groups, self.cfg.trigger.groups))
        if self.cfg.trigger.groups != "ignore":
            log.info("群里判断「@我」的方式：优先看微信记录的 @ 名单（按 wxid，重名也不会认错）；"
                     "读不到时才按名字认：%s", "、".join(at_names) or "（空）")
            if self.cfg.trigger.extra_at_ids:
                log.info("另外把这些 wxid 也当成「我」（trigger.extra_at_ids）：%s",
                         "、".join(self.cfg.trigger.extra_at_ids))
        if self.cfg.trigger.groups == "at":
            log.info("群回复条件：被 @ 到我 + 发送者在白名单里。"
                     "想让某个群谁 @ 都回，把群名也加进 trigger.whitelist。")

        names = self.io.name_map()
        log.info("已载入 %d 个联系人/群的名称映射", len(names))
        self._dump_names(names)

        if self.knowledge is not None:
            if self.cfg.knowledge.auto_index:
                report = self.knowledge.index()
                log.info("通知材料库：%s", report.summary())
                for name, why in report.skipped[:10]:
                    log.info("   跳过 %s —— %s", name[:48], why)
                if len(report.skipped) > 10:
                    log.info("   …… 另有 %d 个跳过（跑 run.bat --index 看全清单）",
                             len(report.skipped) - 10)
            else:
                stats = self.knowledge.stats()
                log.info("通知材料库：%d 份材料、约 %d 字（自动索引已关）",
                         stats["docs"], stats["chars"])

        if self.sync is not None:
            self._announce_sync()
            self._start_sync_thread()

        # 上次运行欠着没回的消息，接着补（进程重启也不会丢）
        left = self.memory.list_pending(limit=40)
        if left:
            log.warning("记忆库里有 %d 条「生成失败、还没补回来」的消息，现在开始补：", len(left))
            for row in left[:10]:
                log.warning("    %s（失败 %s 次）：%s",
                            row.get("conv_name") or row.get("conv_id"),
                            row.get("attempts"),
                            str(row.get("body") or "")[:60].replace("\n", " "))
        if not self.no_send:
            log.info("生成失败的兜底：先记成待办（不会丢），%.0f 秒后自动重来，最多 %d 次；"
                     "之后后台每 %.0f 秒扫一遍接着试，超过 %.0f 小时才放弃。",
                     self.retry.retry_delay, self.retry.max_retries,
                     self.retry.flush_seconds, self.retry.max_age_seconds / 3600.0)
            self.retry.start()

        listener = self.io.listen(self.on_message)
        listener.start()
        log.info("监听已启动。微信窗口别关、别锁屏、别退出。Ctrl+C 停止。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("收到 Ctrl+C，正在收尾……")
        finally:
            try:
                listener.stop()
            except Exception:
                pass
            self.memory.close()
            stat = self.retry.stats()
            log.info("已停止。累计回复 %d 条，因策略跳过 %d 条，记下你自己回的 %d 条；"
                     "补回 %d 条，放弃 %d 条，还欠着 %d 条。",
                     self._replied, self._skipped, self._mirrored,
                     stat["redelivered"], stat["given_up"], stat["pending"])

    def conv_name(self, conv_id: str) -> str:
        return self.io.display_name(conv_id)

    def _dump_names(self, names: Dict[str, str]) -> None:
        """把名称映射存下来：--sync 这种不连微信的模式也能把 wxid 写成名字。"""
        if not names:
            return
        try:
            path = self.cfg.abspath("data/names.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            log.debug("名称映射没存下来：%r", exc)

    # ------------------------------------------------------------ 班级记忆同步
    def _announce_sync(self) -> None:
        """把同步这件事在启动日志里说清楚：目录在哪、读回了多少字、推出去多少条。"""
        assert self.sync is not None
        st = self.sync.status()
        log.info("班级记忆同步：%s", st["dir"])
        self._sync_text = self.sync.pull(force=True)
        if self._sync_text:
            st = self.sync.status()          # pull 之后才有「读到哪几份」
            log.info("共享记忆已读回 %d 字（%s）—— 回答同学时会用上",
                     len(self._sync_text), "、".join(st["pulled"]) or "?")
            sess = st.get("session") or {}
            if sess.get("entries"):
                log.info("会话记忆：从 %s 读到 %d 条（脱敏 %d 条、丢弃 %d 条）",
                         Path(sess.get("file") or "").name or sess.get("thread_id", "")[:8],
                         sess["entries"], sess.get("masked", 0), sess.get("dropped", 0))
            elif st.get("session"):
                log.warning("会话记忆：没找到 thread %s 的记录文件",
                            (st["session"] or {}).get("thread_id", ""))
        else:
            log.info("共享目录里还没有班级记忆（等「班长助理」会话写 knowledge.md 或 inbox/）")
        pushed = self.sync.export(force=True)
        log.info("已同步 %d 条历史对话给「班长助理」会话（累计 %d 条）",
                 pushed, self.sync.status()["exported_total"])

    def _start_sync_thread(self) -> None:
        """后台定时把新对话推出去、把会话写回的班级记忆读进来。

        回复链路本身也会推/读一遍；这个线程是为了「同学聊完但机器人没回（被策略
        跳过）」以及「会话中途改了 knowledge.md」这两种情况也能及时反映。
        """
        assert self.sync is not None
        interval = float(self.cfg.sync.refresh_seconds or 0)
        if interval <= 0:
            log.info("共享记忆：后台刷新已关（refresh_seconds=0），只在回复时同步")
            return
        interval = max(5.0, interval)

        def loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    assert self.sync is not None
                    self.sync.export()
                    text = self.sync.pull()
                    if text != self._sync_text:
                        self._sync_text = text
                        log.info("共享记忆变了：现在 %d 字，下一条回复就用新的", len(text))
                except Exception as exc:
                    log.warning("后台同步出错（不影响回复）：%r", exc)

        thread = threading.Thread(target=loop, name="dsbot-sync", daemon=True)
        thread.start()
        log.info("共享记忆：后台每 %.0f 秒对一次表", interval)

    # ------------------------------------------------------------ 收消息
    def on_message(self, msg: Dict[str, Any]) -> None:
        conv_id = str(msg.get("username") or "")
        if not conv_id:
            return
        group = is_group_chat(conv_id)
        sender_username = str(msg.get("sender_username") or "")
        sender_id = msg.get("sender_id")

        is_self = bool(sender_username) and sender_username == self.wxid
        if not is_self and not sender_username and sender_id == 2 and not group:
            is_self = True          # 兜底：极端情况下拿不到 sender_username

        kind = kind_of(msg.get("type"))
        text = strip_sender_prefix(str(msg.get("content") or ""), sender_username)
        conv_name = self.conv_name(conv_id)
        sender_name = self.io.display_name(sender_username) if sender_username else conv_name

        if is_self:
            self._own_echo(conv_id, conv_name, group, kind, text, msg.get("create_time"))

        # 群聊：@ 判定用微信自己记的名单（精确到 wxid），并带上本群的群昵称兜底
        alias_names: list = []
        at_usernames = None
        if group and kind == "text" and self.cfg.trigger.groups != "ignore":
            gnick = self.io.my_group_name(conv_id)
            if gnick:
                alias_names.append(gnick)
            if self.cfg.trigger.groups == "at":
                at_usernames = self.io.at_usernames(
                    conv_id, msg.get("local_id"), str(msg.get("shard") or ""))
                if conv_id not in self._seen_group:
                    self._seen_group.add(conv_id)
                    log.info("群「%s」里 @ 我用的名字：%s（以后按微信 @ 名单精确判断）",
                             conv_name, gnick or self.nick or "（这个群没设群昵称）")

        assert self.policy is not None
        decision = self.policy.decide(
            conv_id=conv_id,
            conv_name=conv_name,
            sender_name=sender_name,
            is_group=group,
            kind=kind,
            text=text,
            is_self=is_self,
            alias_names=alias_names,
            at_usernames=at_usernames,
        )
        if not decision.allow:
            self._skipped += 1
            if decision.ack:
                # 人家只是应一声（「行」「ok」「好的」「谢谢」）：不回，但记进记忆，
                # 免得下次开口时不知道对方已经应过了。
                self.memory.append(conv_id, "user", decision.text or text)
                log.info("[只记不回] %s%s：%s",
                         conv_name, ("（%s）" % sender_name) if group else "",
                         (decision.text or text)[:60].replace("\n", " "))
                return
            # 带 @ 的群消息被拦下时要写清楚原因：这类"我 @ 了它却不回"最容易让人
            # 以为程序坏了。普通群消息太吵，仍然只写 debug。
            if group and "@" in text:
                log.info("[跳过] %s（%s）：%s", conv_name, sender_name or "?", decision.reason)
            else:
                log.debug("跳过「%s」：%s", conv_name, decision.reason)
            return

        who = sender_name if group else conv_name
        log.info("[收到] %s%s：%s", conv_name, ("（%s）" % sender_name) if group else "",
                 decision.text[:100].replace("\n", " "))
        self._schedule(conv_id, decision.text, conv_name, who, group)

    # ------------------------------------------------- 你自己手动回的那些话
    def _own_echo(self, conv_id: str, conv_name: str, group: bool,
                  kind: str, text: str, created: Any = None) -> None:
        """在微信里由**你本人**发出的消息。

        两件事：
        1. 记进它的记忆（记成 assistant，也就是「我说过的话」）。不记的话，你看
           自己回过同学了，它下次开口还当对方的问题没被回答过，重复问、重复答。
        2. 把已经排队、还没发出去的那条回复撤掉。机器人收到消息要等 3~8 秒（装
           真人打字）才回，这几秒里你要是自己先回了，它不该再补一句 —— 两头抢着
           说，同学会看到两种语气混在一起。

        机器人自己发出去的也会被监听读到，那是回声，不能重复记，靠 _own_sent 认出来。
        """
        text = (text or "").strip()
        if not text or kind != "text":
            return
        age = _msg_age(created)
        if age is not None and age > 900:
            # 十几分钟前的老消息，多半是重启或新会话时从头读出来的，别当刚回的记
            log.debug("跳过一条很久以前的自己发的消息：「%s」（%.0f 分钟前）",
                      conv_name, age / 60.0)
            return
        if self._is_own_echo(conv_id, text):
            return
        if self.policy is None or not self.policy.watches(
                conv_id=conv_id, conv_name=conv_name, is_group=group):
            return                      # 它本来就不管这个会话，记了也用不上

        self.memory.append(conv_id, "assistant", text, origin="human")
        self._mirrored += 1
        self._own_at[conv_id] = time.time()

        with self._lock:
            slot = self._pending.pop(conv_id, None)
        if slot and slot.get("timer"):
            slot["timer"].cancel()
            log.info("[你自己回了] %s：%s —— 已记进记忆，机器人这条不抢了",
                     conv_name, text[:60].replace("\n", " "))
        else:
            log.info("[你自己回了] %s：%s —— 已记进记忆", conv_name,
                     text[:60].replace("\n", " "))

        if self.policy is not None:
            self.policy.note_reply(conv_id)     # 你回过了，冷却从这一刻算
        if self.retry.clear(conv_id):
            log.info("「%s」你自己回过了，欠着的那条待办撤掉（不再补回）", conv_name)

    def _is_own_echo(self, conv_id: str, text: str) -> bool:
        """这条是不是机器人自己刚发出去的？是的话记忆里已经记过一次，别记第二遍。"""
        key = _squash(text)
        now = time.time()
        book = self._own_sent.get(conv_id)
        if not book:
            return False
        book[:] = [item for item in book if now - item[1] < 300]
        for item in list(book):
            if item[0] == key:
                book.remove(item)       # 用掉就销账，免得你恰好说了同样的话被吞掉
                return True
        return False

    def _note_own_send(self, conv_id: str, text: str) -> None:
        """记下机器人刚发出去的内容（监听线程马上会读到它，用来跟回声对账）。"""
        book = self._own_sent.setdefault(conv_id, [])
        book.append((_squash(text), time.time()))
        del book[:-5]

    # ------------------------------------------------------------ 合并 + 延迟
    def _schedule(self, conv_id: str, text: str, conv_name: str, sender_name: str, group: bool) -> None:
        """连发多条时合并成一次请求，并且等真人打字那么久再回。"""
        with self._lock:
            slot = self._pending.get(conv_id)
            if slot and slot.get("timer"):
                slot["timer"].cancel()
            else:
                slot = {"texts": []}
                self._pending[conv_id] = slot
            slot["texts"].append(text)
            slot.update(conv_name=conv_name, sender_name=sender_name, is_group=group)

            delay = max(
                self.cfg.reply.batch_window,
                random.uniform(self.cfg.reply.delay_min, self.cfg.reply.delay_max),
            )
            timer = threading.Timer(delay, self._fire, args=(conv_id,))
            timer.daemon = True
            slot["timer"] = timer
            timer.start()
        log.debug("「%s」将在 %.1fs 后回复（已合并 %d 条）",
                  conv_name, delay, len(slot["texts"]))

    def _fire(self, conv_id: str) -> None:
        with self._lock:
            slot = self._pending.pop(conv_id, None)
        if not slot:
            return
        texts = slot.get("texts") or []
        if not texts:
            return
        conv_name = slot.get("conv_name") or conv_id
        sender_name = slot.get("sender_name") or conv_name
        group = bool(slot.get("is_group"))

        # 之前生成失败、还欠着的消息，一并处理掉：它们跟这次的新话合成一次回复，
        # 免得同学收到两条前后不接的话，或者干脆没人回。
        owed = self.retry.pending_for(conv_id)
        if owed:
            for row in reversed(owed):
                old = str(row.get("body") or "").strip()
                if old:
                    texts = [old] + list(texts)
            log.info("「%s」有 %d 条欠着没回，这次一起补上", conv_name, len(owed))

        started = time.time()
        try:
            reply = self.engine.reply(
                conv_id, "\n".join(texts),
                conv_name=conv_name,
                sender_name=sender_name,
                is_group=group,
            )
        except Exception as exc:
            if self.no_send:
                log.error("生成回复失败（%s）：%s", conv_name, exc)
                return
            # 别丢：记成待办，20 秒后自己重来，之后交给后台接着试。
            log.error("生成回复失败（%s）：%s —— 先记成待办，稍后自动补回", conv_name, exc)
            self.retry.note_failure(
                conv_id, "\n".join(texts),
                conv_name=conv_name,
                sender_name=sender_name,
                is_group=group,
                reason=str(exc),
            )
            return

        if self.no_send:
            log.info("[演练] 本该发给「%s」：%s", conv_name, reply)
            self._replied += 1
            return

        if not self._user_already_answered(conv_id, started, reply):
            self._send_reply(conv_id, conv_name, reply, owed=owed)

    def _user_already_answered(self, conv_id: str, started: float, reply: str) -> bool:
        """生成这几秒里，你是不是自己在微信里回了？回了的话这条就别发了。

        生成慢的时候（DeepSeek 高峰期几十秒）光撤timer不够：timer 早触发过了，
        消息正在路上。这里按「你自己发言的时间」再拦一道，并且把已经写进记忆的
        那句没发出去的草稿删掉，免得它以为那句话说过。
        """
        when = self._own_at.get(conv_id, 0.0)
        if not when or when < started:
            return False
        log.info("[不抢了] 你已经在微信里回了「%s」，机器人这句不发：%s",
                 self.conv_name(conv_id), reply[:60].replace("\n", " "))
        self.memory.drop_message(conv_id, "assistant", reply)
        return True

    def _send_reply(self, conv_id: str, conv_name: str, reply: str, *, owed: list = ()) -> None:
        """把生成好的回复发出去，发成功才记自己发过、清待办、抬冷却。"""
        ok = self.io.send_text(conv_name, reply)
        if ok:
            assert self.policy is not None
            self.policy.note_reply(conv_id)
            if owed:
                self.retry.clear(conv_id)   # 欠的这次一起回了，待办清掉
            self._note_own_send(conv_id, reply)
            self._replied += 1
            log.info("[发出] %s：%s", conv_name, reply[:100].replace("\n", " "))
        else:
            log.error("[发送失败] 目标「%s」没找到或微信窗口不可用，详见上方异常信息", conv_name)

    # ------------------------------------------------------- 欠着的消息补回来
    def _redeliver(self, conv_id: str, body: str, meta: Dict[str, Any]) -> None:
        """RetryQueue 到点回调用它：把欠着的消息重新生成一遍再发出去。

        抛异常 = 这次没补上，RetryQueue 会留着这条、下一轮再来。
        """
        conv_name = meta.get("conv_name") or conv_id
        sender_name = meta.get("sender_name") or conv_name
        group = bool(meta.get("is_group"))

        # 期间同学又发了新话，_fire 可能已经连这条一起回了；那就别重复说一遍。
        if not self.retry.pending_for(conv_id):
            log.info("[补回] 「%s」已经在这之前的回复里处理过了，跳过", conv_name)
            return

        log.info("[补回] 重新生成「%s」的回复（第 %s 次尝试，已经欠了 %.0f 分钟）",
                 conv_name, meta.get("attempts"), float(meta.get("age") or 0) / 60.0)
        started = time.time()
        reply = self.engine.reply(
            conv_id, body,
            conv_name=conv_name,
            sender_name=sender_name,
            is_group=group,
        )
        if self.no_send:
            log.info("[演练] 本该补发给「%s」：%s", conv_name, reply)
            return
        if self._user_already_answered(conv_id, started, reply):
            self.retry.clear(conv_id)     # 你自己回过了，这条待办就没必要再补
            return
        if not self.io.send_text(conv_name, reply):
            raise RuntimeError("补回时发送失败（微信窗口不可用或找不到「%s」）" % conv_name)
        if self.policy is not None:       # 后台线程里别用 assert，免得整条重投白跑
            self.policy.note_reply(conv_id)
        self._note_own_send(conv_id, reply)
        self.retry.clear(conv_id)
        self._replied += 1
        log.info("[补回成功] %s：%s", conv_name, reply[:100].replace("\n", " "))


# -------------------------------------------------------------------- 命令行
def _squash(text: str) -> str:
    """压掉所有空白再比。微信里存的正文和发出去的那串偶尔差个换行，不算两句。"""
    return re.sub(r"\s+", "", text or "")


def _msg_age(created: Any) -> Optional[float]:
    """这条消息是多久以前发的（秒）。拿不到时间就返回 None，当作「刚收到」。

    用来挡掉重启/新会话时可能被从头读出来的老消息 —— 那些不该当成「你刚回的」。
    """
    try:
        ts = float(created)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts > 1e11:               # 毫秒时间戳
        ts /= 1000.0
    age = time.time() - ts
    if age < 0 or age > 86400 * 7:      # 明显不是时间戳，别瞎猜
        return None
    return age


def _ago(ts: float) -> str:
    """把时间戳说成人话，用于启动日志里「另一个号多久没登过了」。"""
    if not ts:
        return "从没登录过"
    delta = max(0.0, time.time() - float(ts))
    if delta < 3600:
        return "%d 分钟前还在用" % max(1, int(delta // 60))
    if delta < 86400:
        return "%d 小时前还在用" % int(delta // 3600)
    return "%d 天前用过" % int(delta // 86400)


def cmd_list_sessions(cfg: AppConfig, account: str = "") -> int:
    io = WeChatIO(account=account or cfg.wechat.account)
    info = io.connect()
    print("当前账号：%s (%s)" % (info.get("nick_name") or "?", info.get("username") or "?"))
    others = [name for name, _ in list_accounts()
              if not name.startswith(info.get("username") or "\0")]
    if others:
        print("（这台机器上还有别的微信号：%s —— 想读那一个，加参数 --account %s）"
              % ("、".join(others), others[0]))
    print()
    io.name_map()
    sessions = io.list_sessions(limit=500)
    print("%-5s %-24s %-28s %s" % ("类型", "名称", "username", "最近一条"))
    print("-" * 104)
    for s in sessions:
        username = str(s.get("username") or "")
        if username.startswith("gh_") or "placeholder" in username:
            continue
        name = io.display_name(username)
        kind = "群" if is_group_chat(username) else "人"
        summary = str(s.get("summary") or "").replace("\n", " ")[:28]
        print("%-5s %-24s %-28s %s" % (kind, name[:22], username, summary))
    print("\n把想自动回复的对象的「名称」填进 config.json 的 trigger.whitelist 即可。")
    return 0


def cmd_once(cfg: AppConfig, text: str, who: str, mock: bool) -> int:
    """只跑大脑，不碰微信，用来验证 Key、人设、白名单逻辑。"""
    llm = build_llm(cfg, mock)
    memory = Memory(cfg.abspath(cfg.memory.db_path), cfg.memory.max_turns, cfg.memory.ttl_hours)
    knowledge = build_knowledge(cfg)
    sync = build_sync(cfg, memory)
    engine = ReplyEngine(cfg, llm, memory, knowledge=knowledge, sync=sync)
    print("模型：%s @ %s" % (cfg.model.name, cfg.model.base_url))
    print("触发模式：%s  名单：%s" % (cfg.trigger.mode, "、".join(cfg.trigger.whitelist) or "（空）"))
    if sync is not None:
        shared = sync.pull(force=True)
        print("共享班级记忆：%d 字（%s）" % (len(shared), sync.status()["dir"]))
    if knowledge is not None:
        stats = knowledge.stats()
        print("通知材料：%d 份、约 %d 字（分词：%s）"
              % (stats["docs"], stats["chars"], stats["tokenizer"]))
        material = knowledge.context(text, k=cfg.knowledge.max_chunks,
                                     max_chars=cfg.knowledge.max_chars,
                                     recent=cfg.knowledge.recent)
        if material:
            print("—— 检索到的材料（前 400 字）——")
            print(material[:400])
    print("人设（前 100 字）：%s…" % cfg.system_prompt()[:100].replace("\n", " "))
    print("-" * 60)
    print("对方说：%s" % text)
    reply = engine.reply("__once__", text, conv_name=who or "测试对象")
    print("AI 回复：%s" % reply)
    print("-" * 60)
    print("（这是本地演练，没有发送任何微信消息）")
    memory.close()
    return 0


def cmd_sync(cfg: AppConfig, reset: bool = False) -> int:
    """手动对一次表：把新对话推给「班长助理」会话，把会话写回的记忆读进来。"""
    scfg = cfg.sync
    if not scfg.enabled or not scfg.dir:
        print("配置里没开同步（sync.enabled=false 或 sync.dir 为空）。")
        print("在 config.json 里写：\"sync\": {\"enabled\": true, \"dir\": \"<共享目录>\"}")
        return 1
    memory = Memory(cfg.abspath(cfg.memory.db_path), cfg.memory.max_turns, cfg.memory.ttl_hours)
    sync = build_sync(cfg, memory)
    assert sync is not None
    shared = sync.pull(force=True)
    pushed = sync.rebuild() if reset else sync.export(force=True)
    st = sync.status()
    print("共享目录：%s" % st["dir"])
    print("推进去：%d 条新对话（累计 %d 条）→ %s" % (pushed, st["exported_total"], sync.dialog_path))
    print("读回来：%d 字  ← %s" % (len(shared), st["knowledge"]))
    if st["pulled"]:
        print("        来源：%s" % "、".join(st["pulled"]))
    if st["refused"]:
        print("        被隐私闸门拦下：%s" % "；".join(st["refused"]))
    sess = st.get("session") or {}
    if sess:
        print("会话记忆：读到 %d 条（脱敏 %d、丢弃 %d）← %s" % (
            sess.get("entries", 0), sess.get("masked", 0), sess.get("dropped", 0),
            sess.get("file") or "（没找到记录文件）"))
        if shared:
            print("        —— 最后两条 ——")
            tail = [ln for ln in shared.splitlines() if ln.startswith("- [")][-2:]
            for ln in tail:
                print("        %s" % ln[:160])
    memory.close()
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 DeepSeek 接到微信个人号上做 24 小时自动回复",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", default=str(ROOT / "config.json"), help="配置文件路径")
    parser.add_argument("--list-sessions", action="store_true", help="列出所有会话（填白名单用）")
    parser.add_argument("--once", metavar="文本", help="只跑一次大脑，不碰微信")
    parser.add_argument("--who", default="", help="配合 --once，指定对方名字（只影响提示词）")
    parser.add_argument("--mock", action="store_true", help="用离线假模型，不调用 DeepSeek")
    parser.add_argument("--dry-run", action="store_true", help="真监听但不发送，只打印要发的内容")
    parser.add_argument("--account", default="",
                        help="指定读哪个微信号（默认自动挑最近活跃的；--account list 可列出全部）")
    parser.add_argument("--index", action="store_true",
                        help="只建/更新通知材料索引，建完就退出（不监听、不发送）")
    parser.add_argument("--sync", action="store_true",
                        help="只和「班长助理」会话对一次表，对完就退出")
    parser.add_argument("--reindex", action="store_true",
                        help="配合 --index：忽略缓存，把所有文件重新读一遍；"
                             "配合 --sync：清掉已同步的对话记录重新生成")
    args = parser.parse_args(argv)

    cfg = load_config(ROOT)
    custom = Path(args.config)
    if custom.is_file() and custom.resolve() != (ROOT / "config.json").resolve():
        cfg = load_config(ROOT, config_path=custom.resolve())
    setup_logging(cfg)

    if args.account == "list":
        accounts = list_accounts()
        if not accounts:
            print("没找到任何登录过的微信号数据目录。")
            return 1
        print("这台机器上登录过的微信号（最新的在最上面）：")
        for i, (name, ts) in enumerate(accounts, 1):
            print("  %d. %-28s %s" % (i, name, _ago(ts)))
        print("\n要用某一个：--account <目录名>，或写进 config.json 的 wechat.account")
        return 0

    if args.list_sessions:
        return cmd_list_sessions(cfg, args.account)
    if args.once:
        return cmd_once(cfg, args.once, args.who, args.mock)
    if args.index:
        return index_knowledge(cfg, force=args.reindex)
    if args.sync:
        return cmd_sync(cfg, reset=args.reindex)

    # 走到这里都是「要一直跑下去」的模式（正式监听 / 演练监听），先抢单实例锁。
    # --once、--index、--list-sessions 这些一次性命令在上面就返回了，不参与加锁。
    lock = InstanceLock(cfg.abspath("data/bot.lock"))
    if not lock.acquire():
        warn_already_running(lock.holder_pid())
        return 1

    try:
        llm = build_llm(cfg, args.mock)
        bot = WeChatBot(cfg, llm, no_send=args.dry_run, account=args.account)
        if args.dry_run:
            log.warning("演练模式：会真的监听微信，但不会发送任何消息")
        bot.start()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
