# -*- coding: utf-8 -*-
"""配置加载。

两处来源：
  .env         只放密钥这种不该进版本库的东西（DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL）
  config.json  行为配置，见 config.example.json

设计原则：config.json 里没写的字段一律走默认值，所以配置文件可以写得很短。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_PERSONA = (
    "你是「{owner}」本人，正在用自己的微信和对方聊天。\n\n"
    "要求：\n"
    "1. 用中文口语回复，像真人发微信一样：短句、自然、偶尔用语气词，不要写成小作文。\n"
    "2. 默认 1~2 句话，除非对方问了需要展开的问题。\n"
    "3. 绝不暴露你是 AI、机器人或程序；被问到就自然带过。\n"
    "4. 不知道的事就说不清楚 / 回头确认，不要硬编。\n"
    "5. 涉及钱、身份证、银行卡、验证码、密码、合同、法律纠纷、体检报告等敏感话题，"
    "一律不要给具体承诺，回复类似「这个我回头跟你说」并把话题收住。\n"
    "6. 不发链接，不主动提任何产品、课程、投资。\n"
    "7. 通知类问题（时间、地点、要交什么材料、截止日期、金额）只依据「手头有的通知材料」"
    "回答；材料里没有的就说「我确认一下再回你」，绝不自己编时间或数字。"
)


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


@dataclass
class ModelConfig:
    name: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    temperature: float = 0.7
    max_tokens: int = 800
    timeout: int = 120


@dataclass
class ReplyConfig:
    delay_min: float = 3.0
    delay_max: float = 8.0
    batch_window: float = 2.5
    max_reply_chars: int = 300
    typing_speed_cps: float = 6.0
    # 生成失败（DeepSeek 超时/503）时怎么办：先记成待办，再自己补回来
    retry_delay: float = 20.0          # 失败后隔多少秒自动重来一次
    max_retries: int = 2               # 这种「马上重来」最多几次，之后交给后台慢慢试
    pending_flush_seconds: float = 120.0   # 后台多久扫一遍待办
    pending_max_age_hours: float = 2.0     # 拖过这么久还没补上就放弃（日志写明）


@dataclass
class QuietHours:
    enabled: bool = False
    start: str = "23:30"
    end: str = "07:30"


@dataclass
class TriggerConfig:
    mode: str = "whitelist"          # whitelist | all
    whitelist: List[str] = field(default_factory=list)
    blacklist: List[str] = field(default_factory=list)
    groups: str = "ignore"           # ignore | at | all
    at_names: List[str] = field(default_factory=list)
    extra_at_ids: List[str] = field(default_factory=list)   # 也算「我」的 wxid（例如另一个号）
    at_text_fallback: bool = False   # 微信没记 @ 名单时，正文里手打的「@我」要不要也算
    require_prefix: str = ""
    cooldown: float = 5.0            # 同一个会话两次回复的最小间隔（秒）
    ignore_keywords: List[str] = field(default_factory=list)
    # 对方只是在应一声（「行」「ok」「好的」「谢谢」）时不用回 —— 回了反而像话痨。
    skip_acks: bool = True
    ack_words: List[str] = field(default_factory=list)   # 除了内置那批，还想略过哪些词
    quiet_hours: QuietHours = field(default_factory=QuietHours)


@dataclass
class MemoryConfig:
    max_turns: int = 12
    ttl_hours: int = 72
    db_path: str = "data/memory.sqlite3"


@dataclass
class WeChatConfig:
    account: str = ""          # 空 = 自动挑最近活跃的账号（同机挂了两个号时有用）
    # 发送后回读数据库确认。关掉会快一点，但发消息那条链路是靠界面状态判断
    # "发出去没有"的，界面偶发误判就会让它重发一遍 —— 对方收到两条一样的。
    verify: bool = True


@dataclass
class SyncConfig:
    """班级记忆同步：和 Codex 的「班长助理」会话共用一份记忆（详见 dsbot/session_sync.py）。

    dir 指向共享目录：机器人往里写同学侧对话，会话往里写班级知识。
    相对路径按项目根目录算；留空 = 这个功能关掉。
    """

    enabled: bool = False
    dir: str = ""
    max_chars: int = 4000        # 每次塞进提示词的共享记忆上限
    dialog_keep: int = 200       # dialog.md 里保留最近多少条对话
    refresh_seconds: float = 15.0   # 后台每隔多少秒把新对话推出去一次（0=只在回复时推）
    # ---- 直接读另一个 Codex 会话的记忆（不用它写文件）----
    session_read: bool = True
    session_thread: str = ""        # 那个会话的 thread id；留空 = 不读
    session_items: int = 12         # 取它最近多少条「我说了什么 / 它最后回了什么」
    session_max_chars: int = 3000   # 这段记忆塞进提示词的上限
    session_tail_mb: float = 3.0    # 第一次只从记录文件尾部读这么多 MB（文件可能几十 MB）
    session_skip_user: List[str] = field(default_factory=list)   # 这些开头的「用户消息」是自动注入的，不算


@dataclass
class OcrConfig:
    """扫描件 OCR：没文字层的 PDF（打印后拍照/扫描的那种）自动认成文字。

    用 Windows 自带的 OCR，离线、免费、不用装 Tesseract。
    """

    enabled: bool = True
    lang: str = ""              # 空 = 自动挑（优先简体中文 zh-Hans-CN）
    dpi: int = 300              # 渲染分辨率，中文 OCR 一般要 300
    max_pages: int = 10         # 一份文件最多认几页（防着几百页的扫描书）
    max_chars: int = 3000       # 认够这么多字就停，后面的页不看了
    trigger_chars: int = 60     # 原文少于这个字数就试 OCR


@dataclass
class KnowledgeConfig:
    """通知材料库：让 AI 能照着通知原文回答（详见 dsbot/knowledge.py）。"""

    enabled: bool = True
    dirs: List[str] = field(default_factory=lambda: ["knowledge"])
    wechat_files: bool = True          # 顺带索引微信里收到/发出的文件
    auto_index: bool = True            # 启动时自动扫一遍（只处理新增/改动的）
    max_chunks: int = 4                # 每次提问取几块原文
    max_chars: int = 2000              # 塞进提示词的材料上限
    recent: int = 5                    # 附带的「最近的通知」条数
    min_chars: int = 40
    max_file_mb: float = 12.0
    max_docs: int = 400
    exclude_keywords: List[str] = field(default_factory=list)   # 按文件名排除
    db_path: str = "data/knowledge.sqlite3"
    ocr: OcrConfig = field(default_factory=OcrConfig)


@dataclass
class LogConfig:
    file: str = "data/bot.log"
    level: str = "INFO"


@dataclass
class AppConfig:
    root: Path
    model: ModelConfig = field(default_factory=ModelConfig)
    persona: str = DEFAULT_PERSONA
    owner_name: str = "我"
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    wechat: WeChatConfig = field(default_factory=WeChatConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    log: LogConfig = field(default_factory=LogConfig)

    def abspath(self, relative: str) -> Path:
        """把配置里的相对路径锚定到项目根目录（而不是当前工作目录）。"""
        p = Path(relative)
        return p if p.is_absolute() else (self.root / p)

    def system_prompt(self) -> str:
        return self.persona.replace("{owner}", self.owner_name)


def load_env_file(path: Path) -> Dict[str, str]:
    """极简 .env 解析：KEY=VALUE，支持 # 注释和引号。已存在的环境变量优先。"""
    result: Dict[str, str] = {}
    if not path.is_file():
        return result
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
            result[key] = value
    return result


def load_config(root: Optional[Path] = None, config_path: Optional[Path] = None) -> AppConfig:
    """``root`` 决定 .env / 日志 / 记忆库的位置；``config_path`` 可指定别的 JSON。"""
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    load_env_file(root / ".env")

    raw: Dict[str, Any] = {}
    cfg_path = Path(config_path) if config_path else (root / "config.json")
    if cfg_path.is_file():
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))

    model_raw = _as_dict(raw.get("model"))
    reply_raw = _as_dict(raw.get("reply"))
    trigger_raw = _as_dict(raw.get("trigger"))
    wechat_raw = _as_dict(raw.get("wechat"))
    sync_raw = _as_dict(raw.get("sync"))
    knowledge_raw = _as_dict(raw.get("knowledge"))
    memory_raw = _as_dict(raw.get("memory"))
    log_raw = _as_dict(raw.get("log"))
    quiet_raw = _as_dict(trigger_raw.get("quiet_hours"))

    model = ModelConfig(
        name=str(model_raw.get("name", ModelConfig.name)),
        base_url=str(model_raw.get("base_url", ModelConfig.base_url)),
        api_key=os.environ.get("DEEPSEEK_API_KEY", "") or str(model_raw.get("api_key", "")),
        temperature=float(model_raw.get("temperature", ModelConfig.temperature)),
        max_tokens=int(model_raw.get("max_tokens", ModelConfig.max_tokens)),
        timeout=int(model_raw.get("timeout", ModelConfig.timeout)),
    )
    if os.environ.get("DEEPSEEK_BASE_URL"):
        model.base_url = os.environ["DEEPSEEK_BASE_URL"]
    if os.environ.get("DEEPSEEK_MODEL"):
        model.name = os.environ["DEEPSEEK_MODEL"]

    reply = ReplyConfig(
        delay_min=float(reply_raw.get("delay_min", ReplyConfig.delay_min)),
        delay_max=float(reply_raw.get("delay_max", ReplyConfig.delay_max)),
        batch_window=float(reply_raw.get("batch_window", ReplyConfig.batch_window)),
        max_reply_chars=int(reply_raw.get("max_reply_chars", ReplyConfig.max_reply_chars)),
        typing_speed_cps=float(reply_raw.get("typing_speed_cps", ReplyConfig.typing_speed_cps)),
        retry_delay=float(reply_raw.get("retry_delay", ReplyConfig.retry_delay)),
        max_retries=int(reply_raw.get("max_retries", ReplyConfig.max_retries)),
        pending_flush_seconds=float(
            reply_raw.get("pending_flush_seconds", ReplyConfig.pending_flush_seconds)),
        pending_max_age_hours=float(
            reply_raw.get("pending_max_age_hours", ReplyConfig.pending_max_age_hours)),
    )
    if reply.delay_max < reply.delay_min:
        reply.delay_max = reply.delay_min

    trigger = TriggerConfig(
        mode=str(trigger_raw.get("mode", TriggerConfig.mode)).lower(),
        whitelist=_as_list(trigger_raw.get("whitelist")),
        blacklist=_as_list(trigger_raw.get("blacklist")),
        groups=str(trigger_raw.get("groups", TriggerConfig.groups)).lower(),
        at_names=_as_list(trigger_raw.get("at_names")),
        extra_at_ids=_as_list(trigger_raw.get("extra_at_ids")),
        at_text_fallback=bool(trigger_raw.get("at_text_fallback", False)),
        require_prefix=str(trigger_raw.get("require_prefix") or ""),
        cooldown=float(trigger_raw.get("cooldown", TriggerConfig.cooldown)),
        ignore_keywords=_as_list(trigger_raw.get("ignore_keywords")),
        skip_acks=bool(trigger_raw.get("skip_acks", TriggerConfig.skip_acks)),
        ack_words=_as_list(trigger_raw.get("ack_words")),
        quiet_hours=QuietHours(
            enabled=bool(quiet_raw.get("enabled", False)),
            start=str(quiet_raw.get("start", QuietHours.start)),
            end=str(quiet_raw.get("end", QuietHours.end)),
        ),
    )

    memory = MemoryConfig(
        max_turns=int(memory_raw.get("max_turns", MemoryConfig.max_turns)),
        ttl_hours=int(memory_raw.get("ttl_hours", MemoryConfig.ttl_hours)),
        db_path=str(memory_raw.get("db_path", MemoryConfig.db_path)),
    )
    log = LogConfig(
        file=str(log_raw.get("file", LogConfig.file)),
        level=str(log_raw.get("level", LogConfig.level)).upper(),
    )
    wechat = WeChatConfig(
        account=str(wechat_raw.get("account") or "").strip(),
        # 以前这里漏了 verify —— config.json 里写 false 不生效，只能一直走回读确认
        verify=bool(wechat_raw.get("verify", WeChatConfig.verify)),
    )
    sync_dir = str(sync_raw.get("dir") or "").strip()
    sync = SyncConfig(
        enabled=bool(sync_raw.get("enabled", bool(sync_dir))) and bool(sync_dir),
        dir=sync_dir,
        max_chars=int(sync_raw.get("max_chars", SyncConfig.max_chars)),
        dialog_keep=int(sync_raw.get("dialog_keep", SyncConfig.dialog_keep)),
        refresh_seconds=float(sync_raw.get("refresh_seconds", SyncConfig.refresh_seconds)),
        session_read=bool(sync_raw.get("session_read", SyncConfig.session_read)),
        session_thread=str(sync_raw.get("session_thread") or "").strip(),
        session_items=int(sync_raw.get("session_items", SyncConfig.session_items)),
        session_max_chars=int(sync_raw.get("session_max_chars", SyncConfig.session_max_chars)),
        session_tail_mb=float(sync_raw.get("session_tail_mb", SyncConfig.session_tail_mb)),
        session_skip_user=_as_list(sync_raw.get("session_skip_user")),
    )
    ocr_raw = _as_dict(knowledge_raw.get("ocr"))
    knowledge = KnowledgeConfig(
        enabled=bool(knowledge_raw.get("enabled", True)),
        dirs=_as_list(knowledge_raw.get("dirs")) or ["knowledge"],
        wechat_files=bool(knowledge_raw.get("wechat_files", True)),
        auto_index=bool(knowledge_raw.get("auto_index", True)),
        max_chunks=int(knowledge_raw.get("max_chunks", KnowledgeConfig.max_chunks)),
        max_chars=int(knowledge_raw.get("max_chars", KnowledgeConfig.max_chars)),
        recent=int(knowledge_raw.get("recent", KnowledgeConfig.recent)),
        min_chars=int(knowledge_raw.get("min_chars", KnowledgeConfig.min_chars)),
        max_file_mb=float(knowledge_raw.get("max_file_mb", KnowledgeConfig.max_file_mb)),
        max_docs=int(knowledge_raw.get("max_docs", KnowledgeConfig.max_docs)),
        exclude_keywords=_as_list(knowledge_raw.get("exclude_keywords")),
        db_path=str(knowledge_raw.get("db_path", KnowledgeConfig.db_path)),
        ocr=OcrConfig(
            enabled=bool(ocr_raw.get("enabled", OcrConfig.enabled)),
            lang=str(ocr_raw.get("lang", OcrConfig.lang) or ""),
            dpi=int(ocr_raw.get("dpi", OcrConfig.dpi)),
            max_pages=int(ocr_raw.get("max_pages", OcrConfig.max_pages)),
            max_chars=int(ocr_raw.get("max_chars", OcrConfig.max_chars)),
            trigger_chars=int(ocr_raw.get("trigger_chars", OcrConfig.trigger_chars)),
        ),
    )

    return AppConfig(
        root=root,
        model=model,
        persona=str(raw.get("persona") or DEFAULT_PERSONA),
        owner_name=str(raw.get("owner_name") or "我"),
        reply=reply,
        trigger=trigger,
        wechat=wechat,
        sync=sync,
        knowledge=knowledge,
        memory=memory,
        log=log,
    )
