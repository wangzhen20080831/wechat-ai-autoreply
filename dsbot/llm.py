# -*- coding: utf-8 -*-
"""DeepSeek 调用。

DeepSeek 提供 OpenAI 兼容接口，所以这里没必要引入 openai 这个包——
用标准库 urllib 发一个 POST 就够了，少一个依赖、少一堆版本坑
（openai 3.x 已经要求 Python 3.10+，而微信自动化库在 3.9 上更稳）。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence


class LLMError(RuntimeError):
    """调用大模型失败（网络、鉴权、限流、返回格式异常）。"""


def _endpoint(base_url: str) -> str:
    base = (base_url or "https://api.deepseek.com").strip().rstrip("/")
    # 用户可能填 https://api.deepseek.com 或 .../v1 或中转站的地址，都能拼出来
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        temperature: float = 0.7,
        max_tokens: int = 800,
        timeout: int = 120,
        retries: int = 3,
    ) -> None:
        if not api_key:
            raise LLMError("没有配置 DEEPSEEK_API_KEY，请复制 .env.example 为 .env 并填入 Key")
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.url = _endpoint(base_url)

    # ---------------------------------------------------------------- 底层
    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
                "Accept": "application/json",
                "User-Agent": "ds-wechat-bot/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:                     # 4xx / 5xx
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise LLMError(_explain_http(exc.code, detail)) from exc
        except urllib.error.URLError as exc:                      # DNS / 连接失败
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise LLMError(f"请求超时（连不上或 {self.timeout}s 没响应）") from exc
            raise LLMError(f"连不上 {self.url}：{reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            # 注意：Python 3.9 里 socket.timeout 并不是 TimeoutError 的子类（继承 OSError），
            # 所以必须单独列出来 —— 否则它会被原样抛出，重试逻辑（只认 LLMError）根本
            # 接不到，日志里就只剩一句英文 "The read operation timed out"，一次都不重试。
            raise LLMError(f"请求超时（{self.timeout}s），网络或服务端太慢") from exc
        except OSError as exc:                                    # 连接被重置等
            raise LLMError(f"网络错误：{exc}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"返回内容不是合法 JSON：{raw[:300]}") from exc

    def chat(self, messages: Sequence[Dict[str, str]], **overrides: Any) -> str:
        """传一段 OpenAI 格式的 messages，返回回复正文。"""
        payload: Dict[str, Any] = {
            "model": overrides.get("model", self.model),
            "messages": list(messages),
            "temperature": overrides.get("temperature", self.temperature),
            "max_tokens": overrides.get("max_tokens", self.max_tokens),
            "stream": False,
        }
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                data = self._post(payload)
                return _extract_text(data)
            except LLMError as exc:
                last_error = exc
                retryable = any(tag in str(exc) for tag in (
                    "429", "500", "502", "503", "504", "超时", "连不上", "网络错误"))
                if attempt >= self.retries or not retryable:
                    raise
                time.sleep(1.5 * (2 ** attempt))
        raise last_error if last_error else LLMError("调用失败")

    def ping(self) -> str:
        """连通性自检：只花十几个 token。"""
        return self.chat(
            [
                {"role": "system", "content": "只回复两个字，不要标点。"},
                {"role": "user", "content": "在吗"},
            ],
            max_tokens=16,
            temperature=0.0,
        )


def _explain_http(code: int, detail: str) -> str:
    hint = {
        401: "API Key 无效或已过期，去 platform.deepseek.com 重新生成一个",
        402: "账户余额不足，需要充值",
        403: "没有权限访问该模型",
        404: "接口地址不对，检查 model.base_url（官方是 https://api.deepseek.com）",
        429: "触发限流（429），稍后重试或降低频率",
        500: "服务端错误（500）",
        503: "服务繁忙（503）",
    }.get(code, "")
    msg = f"HTTP {code}"
    if hint:
        msg += f" — {hint}"
    if detail:
        msg += f"：{detail[:400]}"
    return msg


def _extract_text(data: Dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        err = (data.get("error") or {}).get("message") if isinstance(data.get("error"), dict) else None
        raise LLMError(f"返回里没有 choices：{err or json.dumps(data, ensure_ascii=False)[:300]}")
    message = choices[0].get("message") or {}
    # deepseek-reasoner 会把思考过程放在 reasoning_content，只取最终回答
    text = message.get("content") or ""
    text = text.strip()
    if not text:
        raise LLMError("模型返回了空内容")
    return text


class MockLLM:
    """离线自测用：不联网，按固定规则回声。"""

    def __init__(self, prefix: str = "[offline] ") -> None:
        self.prefix = prefix

    def chat(self, messages: Sequence[Dict[str, str]], **overrides: Any) -> str:
        user = ""
        for m in reversed(list(messages)):
            if m.get("role") == "user":
                user = m.get("content", "")
                break
        return self.prefix + user

    def ping(self) -> str:
        return "在啊"
