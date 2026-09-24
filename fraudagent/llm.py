"""LLM access for evidence synthesis and explanations.

Order: Gemini (GEMINI_API_KEY) then Groq (GROQ_API_KEY). With no key, or when
every provider fails, `complete()` returns None and callers use their
deterministic template, so a run never depends on a free-tier quota.
Model names come from GEMINI_MODEL / GROQ_MODEL.
"""
from __future__ import annotations

import json
import os
import re
import time

import httpx

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
GEMINI_FALLBACKS = [m for m in (GEMINI_MODEL, "gemini-3-flash-preview", "gemini-flash-lite-latest") if m]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")


class LLM:
    def __init__(self) -> None:
        self.tokens = 0
        self.calls = []
        self.gemini_key = os.environ.get("GEMINI_API_KEY", "")
        self.groq_key = os.environ.get("GROQ_API_KEY", "")
        self._last = 0.0

    @property
    def available(self) -> bool:
        return bool(self.gemini_key or self.groq_key)

    def _throttle(self, gap: float = 4.5) -> None:
        wait = self._last + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _gemini(self, system: str, prompt: str) -> str | None:
        last = None
        for model in dict.fromkeys(GEMINI_FALLBACKS):
            try:
                return self._gemini_model(model, system, prompt)
            except httpx.HTTPStatusError as exc:  # 404 retired model, 503 overloaded: try the next model
                last = exc
                if exc.response.status_code not in (404, 503, 500):
                    raise
        raise last

    def _gemini_model(self, model: str, system: str, prompt: str) -> str | None:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}}
        r = httpx.post(url, params={"key": self.gemini_key}, json=body, timeout=90)
        r.raise_for_status()
        d = r.json()
        self.tokens += d.get("usageMetadata", {}).get("totalTokenCount", 0)
        return d["candidates"][0]["content"]["parts"][0]["text"]

    def _groq(self, system: str, prompt: str) -> str | None:
        body = {"model": GROQ_MODEL, "temperature": 0.2, "max_tokens": 4096, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                       headers={"Authorization": f"Bearer {self.groq_key}"}, json=body, timeout=90)
        if r.status_code == 400:  # JSON mode not supported by this model: ask for JSON in plain text instead
            body.pop("response_format")
            r = httpx.post("https://api.groq.com/openai/v1/chat/completions",
                           headers={"Authorization": f"Bearer {self.groq_key}"}, json=body, timeout=90)
        r.raise_for_status()
        d = r.json()
        self.tokens += d.get("usage", {}).get("total_tokens", 0)
        return d["choices"][0]["message"]["content"]

    def complete_json(self, system: str, prompt: str, purpose: str) -> dict | None:
        for name, fn, key in (("gemini", self._gemini, self.gemini_key), ("groq", self._groq, self.groq_key)):
            if not key:
                continue
            for attempt in range(3):
                try:
                    self._throttle()
                    t0 = time.time()
                    txt = fn(system, prompt)
                    m = re.search(r"\{.*\}", txt or "", re.S)
                    out = json.loads(m.group(0)) if m else None
                    self.calls.append({"provider": name, "purpose": purpose, "ms": int((time.time() - t0) * 1000), "ok": out is not None})
                    if out is not None:
                        return out
                except Exception as exc:  # quota, network or parse errors fall through to the next provider
                    self.calls.append({"provider": name, "purpose": purpose, "ok": False,
                                       "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
                    print(f"  [llm] {name} failed: {type(exc).__name__}: {str(exc)[:160]}")
                    if "429" in str(exc) or "503" in str(exc):
                        time.sleep(8 * (attempt + 1))
                        continue
                    break
        return None
