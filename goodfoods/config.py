"""Central configuration. Everything is overridable through environment variables
(or a local `.env` file) so the same code runs on a laptop, in Docker, or on-prem."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Tiny .env loader (no extra dependency). Existing env vars always win."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

TZ = ZoneInfo(os.getenv("GOODFOODS_TZ", "Asia/Kolkata"))
CITY = os.getenv("GOODFOODS_CITY", "Bengaluru")
DB_PATH = Path(os.getenv("GOODFOODS_DB", ROOT / "data" / "goodfoods.db"))


def now() -> datetime:
    """Current local time. `GOODFOODS_NOW=2026-10-12T16:00` freezes the clock for demos/tests."""
    frozen = os.getenv("GOODFOODS_NOW")
    if frozen:
        return datetime.fromisoformat(frozen).replace(tzinfo=TZ)
    return datetime.now(TZ)


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.2
    timeout_s: float = 30.0
    max_retries: int = 3


def load_llm_settings() -> LLMSettings:
    # Default: Groq-hosted small open-weight model (OpenAI-compatible). Swap to Ollama/vLLM/Together
    # by changing two variables -- no code change (no vendor lock-in).
    return LLMSettings(
        base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
        api_key=os.getenv("LLM_API_KEY", os.getenv("GROQ_API_KEY", "")),
        # Groq retired llama-3.1-8b-instant on 16 Aug 2026; gpt-oss-20b is its recommended replacement.
        # For Llama 3.1 8B itself use Ollama (llama3.1:8b) or another OpenAI-compatible host.
        model=os.getenv("LLM_MODEL", "openai/gpt-oss-20b"),
        temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
        timeout_s=float(os.getenv("LLM_TIMEOUT_S", "30")),
        max_retries=int(os.getenv("LLM_MAX_RETRIES", "3")),
    )
