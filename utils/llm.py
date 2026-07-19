"""
LLM Client — Gemini wrapper with rate-limit handling.
- Automatic retry-with-wait on 429 errors (per-minute limits)
- Model configurable via .env (LLM_MODEL) without touching code
"""

import os
import re
import time
from langchain_google_genai import ChatGoogleGenerativeAI
from dotenv import load_dotenv

load_dotenv()

# Default: flash-lite has the highest free-tier daily quota.
# Override in .env with: LLM_MODEL=gemini-2.5-flash
DEFAULT_MODEL = os.getenv("LLM_MODEL", "gemini-2.5-flash-lite")

MAX_RATE_LIMIT_RETRIES = 3


def get_text(response) -> str:
    """
    Extract plain text from an LLM response.
    Newer Gemini models return content as a list of parts instead of a string —
    this helper normalizes both cases.
    """
    content = response.content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text", ""))
            else:
                parts.append(str(part))
        return "".join(parts)
    return content


class RateLimitedLLM:
    """
    Wraps the Gemini client with automatic retry-on-429.
    When the API says 'retry in Ns', we wait N+1 seconds and retry,
    up to MAX_RATE_LIMIT_RETRIES times. Daily-quota exhaustion still
    fails (waiting won't help), but per-minute limits self-heal.
    """

    def __init__(self, llm):
        self._llm = llm

    def invoke(self, *args, **kwargs):
        last_error = None
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                return self._llm.invoke(*args, **kwargs)
            except Exception as e:
                msg = str(e)
                if "429" not in msg and "RESOURCE_EXHAUSTED" not in msg:
                    raise  # Not a rate limit — real error, surface it

                last_error = e

                # Daily quota exhausted? Waiting won't help — fail with clear message.
                if "PerDay" in msg:
                    raise RuntimeError(
                        f"DAILY quota exhausted for model '{DEFAULT_MODEL}'. "
                        "Options: (1) wait until quota resets (midnight Pacific), "
                        "(2) switch model in .env, e.g. LLM_MODEL=gemma-3-27b-it, "
                        "(3) create a second API key under a new Google Cloud project."
                    ) from e

                # Per-minute limit: extract suggested wait, else default 30s
                wait_match = re.search(r"retry in (\d+(?:\.\d+)?)", msg)
                wait_s = float(wait_match.group(1)) + 2 if wait_match else 30

                if attempt < MAX_RATE_LIMIT_RETRIES:
                    print(f"⏳ Rate limit hit — waiting {wait_s:.0f}s (retry {attempt + 1}/{MAX_RATE_LIMIT_RETRIES})...")
                    time.sleep(wait_s)

        raise last_error


def get_llm(temperature: float = 0.1) -> RateLimitedLLM:
    """
    Returns a rate-limit-protected Gemini model instance.
    Low temperature (0.1) for code generation — we want determinism.
    Slightly higher (0.3) for interpretation/insights — we want fluency.
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError(
            "GOOGLE_API_KEY not set. Copy .env.example to .env and add your key."
        )

    llm = ChatGoogleGenerativeAI(
        model=DEFAULT_MODEL,
        google_api_key=api_key,
        temperature=temperature,
        convert_system_message_to_human=True
    )
    return RateLimitedLLM(llm)
