"""Narrow adapter around a local Ollama server. No cloud fallback exists."""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx


class ModelState(StrEnum):
    READY = "ready"
    SERVER_UNAVAILABLE = "model_unavailable"
    MODEL_NOT_PULLED = "model_not_pulled"


@dataclass(frozen=True)
class ModelStatus:
    state: ModelState
    model: str
    digest: str | None = None
    details: dict[str, Any] | None = None
    server_version: str | None = None
    message: str = ""


class InferenceError(RuntimeError):
    """The model call failed (transport, server error or timeout); not an empty extraction."""


@dataclass(frozen=True)
class ChatResult:
    content: str
    raw: dict[str, Any]
    duration_ms: int


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout_s: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def status(self) -> ModelStatus:
        try:
            with httpx.Client(timeout=5) as c:
                version = c.get(f"{self.base_url}/api/version").json().get("version")
                tags = c.get(f"{self.base_url}/api/tags").json().get("models", [])
        except (httpx.HTTPError, ValueError) as exc:
            return ModelStatus(ModelState.SERVER_UNAVAILABLE, self.model,
                               message=f"Ollama is not reachable at {self.base_url} ({type(exc).__name__}). Start it with `ollama serve`.")
        for m in tags:
            if m.get("name") == self.model or m.get("model") == self.model:
                return ModelStatus(ModelState.READY, self.model, digest=m.get("digest"), details=m.get("details"),
                                   server_version=version)
        return ModelStatus(ModelState.MODEL_NOT_PULLED, self.model, server_version=version,
                           message=f"{self.model} is not pulled. Run `make model` (ollama pull {self.model}).")

    def chat_image(self, prompt: str, image_png: bytes, schema: dict[str, Any], options: dict[str, Any]) -> ChatResult:
        body = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": options,
            "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(image_png).decode()]}],
        }
        t0 = time.monotonic()
        try:
            with httpx.Client(timeout=self.timeout_s) as c:
                r = c.post(f"{self.base_url}/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise InferenceError(f"{type(exc).__name__}: {exc}") from exc
        if r.status_code != 200:
            raise InferenceError(f"ollama returned HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        content = (data.get("message") or {}).get("content")
        if content is None:
            raise InferenceError("ollama response has no message content")
        return ChatResult(content=content, raw={k: v for k, v in data.items() if k != "message"},
                          duration_ms=int((time.monotonic() - t0) * 1000))
