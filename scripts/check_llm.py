"""Check which LLM keys load from .env and whether one call succeeds (prints errors, never keys)."""
from pathlib import Path
import os

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
gk, qk = os.environ.get("GEMINI_API_KEY", ""), os.environ.get("GROQ_API_KEY", "")
print("GEMINI_API_KEY set:", bool(gk), "| GROQ_API_KEY set:", bool(qk))
gm = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
qm = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
if gk:
    r = httpx.get("https://generativelanguage.googleapis.com/v1beta/models", headers={"x-goog-api-key": gk}, timeout=30)
    names = [m["name"].split("/")[-1] for m in r.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
    print("gemini models:", r.status_code, [n for n in names if "flash" in n][:12])
    r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{gm}:generateContent", headers={"x-goog-api-key": gk},
                   json={"contents": [{"parts": [{"text": "Reply with the word ok"}]}]}, timeout=60)
    print(f"gemini call ({gm}):", r.status_code, r.text[:300])
if qk:
    r = httpx.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {qk}"}, timeout=30)
    print("groq models:", r.status_code, [m["id"] for m in r.json().get("data", [])][:15])
    r = httpx.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {qk}"},
                   json={"model": qm, "messages": [{"role": "user", "content": "Reply with the word ok"}]}, timeout=60)
    print(f"groq call ({qm}):", r.status_code, r.text[:300])
