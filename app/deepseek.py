"""DeepSeek（OpenAI 兼容）API 客户端：普通调用 / 流式 / JSON 模式 / 自动重试。

默认 base_url = https://api.deepseek.com，模型 deepseek-chat。
只要把 base_url 换成任何 OpenAI 兼容服务（硅基流动、Ollama、vLLM、OpenAI…）也能直接用。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator

import httpx


class DeepSeekError(RuntimeError):
    pass


def _extract_json(text: str) -> Any:
    """从模型输出里稳健地取出 JSON（容忍 ```json 包裹与前后废话）。"""
    if not text:
        raise DeepSeekError("模型返回为空")
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        pass
    # 退一步：截取第一个 { 到最后一个 }
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except Exception:
            pass
    start, end = s.find("["), s.rfind("]")
    if start >= 0 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except Exception:
            pass
    raise DeepSeekError(f"无法解析模型返回的 JSON：{text[:300]}")


class DeepSeekClient:
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com",
                 model: str = "deepseek-chat", timeout: float = 300.0, retry: int = 3):
        if not api_key:
            raise DeepSeekError("未配置 API Key（请在网页「设置」里填写，或设置环境变量 DEEPSEEK_API_KEY）")
        self.api_key = api_key.strip()
        self.base_url = (base_url or "https://api.deepseek.com").rstrip("/")
        self.model = model or "deepseek-chat"
        self.timeout = timeout
        self.retry = max(1, retry)
        self._client = httpx.Client(timeout=timeout, trust_env=True)

    # -------------------------------------------------------------- 基础
    @property
    def url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _payload(self, messages: list[dict], model: str | None, temperature: float | None,
                 json_mode: bool, max_tokens: int | None, stream: bool) -> dict:
        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "stream": stream,
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens:
            body["max_tokens"] = max_tokens
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def chat(self, messages: list[dict], model: str | None = None,
             temperature: float | None = None, json_mode: bool = False,
             max_tokens: int | None = None) -> str:
        body = self._payload(messages, model, temperature, json_mode, max_tokens, False)
        last: Exception | None = None
        for attempt in range(self.retry):
            try:
                r = self._client.post(self.url, headers=self._headers(), json=body)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise DeepSeekError(f"HTTP {r.status_code}: {r.text[:200]}")
                if r.status_code != 200:
                    raise DeepSeekError(f"HTTP {r.status_code}: {r.text[:500]}")
                data = r.json()
                return data["choices"][0]["message"]["content"] or ""
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt < self.retry - 1:
                    time.sleep(1.5 * (2 ** attempt))
                    continue
                break
        raise DeepSeekError(f"请求失败（已重试 {self.retry} 次）：{last}")

    def chat_json(self, messages: list[dict], **kw) -> Any:
        return _extract_json(self.chat(messages, json_mode=True, **kw))

    def stream(self, messages: list[dict], model: str | None = None,
               temperature: float | None = None, max_tokens: int | None = None) -> Iterator[str]:
        body = self._payload(messages, model, temperature, False, max_tokens, True)
        with self._client.stream("POST", self.url, headers=self._headers(), json=body) as r:
            if r.status_code != 200:
                detail = r.read().decode("utf-8", "replace")[:400]
                raise DeepSeekError(f"HTTP {r.status_code}: {detail}")
            for line in r.iter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except Exception:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if piece:
                    yield piece

    def list_models(self) -> list[str]:
        try:
            r = self._client.get(f"{self.base_url}/models", headers=self._headers())
            if r.status_code == 200:
                return [m.get("id", "") for m in r.json().get("data", [])]
        except Exception:
            pass
        return []

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def make_client(settings: dict):
    """按设置构造客户端；provider=mock（或 EE_MOCK=1）时返回离线模拟客户端。"""
    import os

    provider = str(settings.get("provider") or "").strip().lower()
    if provider == "mock" or os.getenv("EE_MOCK") == "1" or os.getenv("EASYESSay_MOCK") == "1":
        from .mock import MockClient

        return MockClient(settings)
    return DeepSeekClient(
        api_key=settings.get("api_key", ""),
        base_url=settings.get("base_url", "https://api.deepseek.com"),
        model=settings.get("model", "deepseek-chat"),
        retry=int(settings.get("retry", 3) or 3),
    )
