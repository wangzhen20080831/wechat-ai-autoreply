# -*- coding: utf-8 -*-
"""dsbot —— 把 DeepSeek 接到微信个人号上的自动回复引擎。

分层：
    config  配置加载（.env + config.json）
    llm     DeepSeek 调用（OpenAI 兼容协议，纯标准库实现）
    memory  按会话隔离的对话记忆（SQLite）
    policy  回不回、什么时候回（白名单/群聊/冷却/免打扰）
    engine  把上面几件组装成"给一句话，还一句话"
"""

from .config import AppConfig, load_config
from .engine import ReplyEngine
from .knowledge import Knowledge
from .llm import DeepSeekClient, LLMError, MockLLM
from .memory import Memory
from .policy import Decision, Policy
from .session_sync import SessionSync

__all__ = [
    "AppConfig",
    "load_config",
    "ReplyEngine",
    "SessionSync",
    "DeepSeekClient",
    "LLMError",
    "MockLLM",
    "Memory",
    "Policy",
    "Decision",
    "Knowledge",
]
