from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .audit import record_event
from .database import Database
from .errors import KnowledgeWorkbenchError, PolicyDeniedError
from .models import Classification
from .policy import (
    ModelRequestPolicy,
    ProviderLocation,
    enforce_model_policy,
    enforce_model_selection,
)
from .utils import new_id, sha256_text


class TextModel(Protocol):
    name: str
    location: ProviderLocation

    def generate(self, prompt: str) -> str: ...


@dataclass(slots=True)
class GuardedModel:
    """Policy-enforcing wrapper used by every future model integration."""

    provider: TextModel

    def generate(
        self,
        prompt: str,
        classification: Classification,
        *,
        allow_internal_cloud_once: bool = False,
    ) -> str:
        enforce_model_selection(self.provider.name)
        enforce_model_policy(
            ModelRequestPolicy(
                classification=classification,
                provider_location=self.provider.location,
                allow_internal_cloud_once=allow_internal_cloud_once,
            )
        )
        return self.provider.generate(prompt)


@dataclass(slots=True)
class DeepSeekChatModel:
    api_key: str
    name: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"
    timeout_seconds: float = 120.0
    location: ProviderLocation = ProviderLocation.CLOUD

    @classmethod
    def from_environment(cls, **kwargs) -> "DeepSeekChatModel":
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise KnowledgeWorkbenchError(
                "未配置 DEEPSEEK_API_KEY；主链路仍可使用 faithful 模式"
            )
        return cls(api_key=api_key, **kwargs)

    def generate(self, prompt: str) -> str:
        payload = json.dumps(
            {
                "model": self.name,
                "messages": [
                    {
                        "role": "system",
                        "content": "你是证据分析器。只返回符合请求 Schema 的 JSON，不使用 Markdown。",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.load(response)
            content = result["choices"][0]["message"]["content"]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise KnowledgeWorkbenchError(f"DeepSeek 调用失败：{exc}") from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise KnowledgeWorkbenchError("DeepSeek 返回结构缺少 choices.message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise KnowledgeWorkbenchError("DeepSeek 返回了空内容")
        return content


@dataclass(slots=True)
class AuditedModelGateway:
    database: Database
    provider: TextModel

    def generate(
        self,
        prompt: str,
        classification: Classification,
        *,
        actor: str,
        allow_internal_cloud_once: bool = False,
    ) -> str:
        request_id = new_id("modelcall")
        details = {
            "provider": type(self.provider).__name__,
            "model": self.provider.name,
            "location": self.provider.location.value,
            "classification": classification.value,
            "prompt_sha256": sha256_text(prompt),
            "internal_cloud_authorized": allow_internal_cloud_once,
        }
        started = time.perf_counter()
        try:
            output = GuardedModel(self.provider).generate(
                prompt,
                classification,
                allow_internal_cloud_once=allow_internal_cloud_once,
            )
        except PolicyDeniedError:
            with self.database.transaction() as connection:
                record_event(
                    connection,
                    "model_call_denied",
                    "model_call",
                    request_id,
                    actor=actor,
                    details=details,
                )
            raise
        except Exception as exc:
            with self.database.transaction() as connection:
                record_event(
                    connection,
                    "model_call_failed",
                    "model_call",
                    request_id,
                    actor=actor,
                    details={**details, "error_type": type(exc).__name__},
                )
            raise
        with self.database.transaction() as connection:
            record_event(
                connection,
                "model_call_succeeded",
                "model_call",
                request_id,
                actor=actor,
                details={
                    **details,
                    "response_sha256": sha256_text(output),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
        return output
