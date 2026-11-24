"""
P6: live provider port (Gemini) with hard budget, call and token limits, and provider-reported usage.

Key handling: the API key is read from the GOOGLE_API_KEY environment variable when a GeminiPort is
constructed. It is passed only to the provider client. It is never stored in a record, ledger, prompt,
report or exception text (redact() removes it), and nothing here starts a container or subprocess.

Budget: BudgetedPort checks BEFORE every call that the worst case still fits:
    worst-case cost = (UTF-8 bytes of the prompt, an upper bound on input tokens) x input price
                    + max_output_tokens x output price
    refuse if  ledger spend + this run's spend + worst case > budget,  or the call/token limits are reached.
After the call it records the PROVIDER-REPORTED usage (input, output; output = max(output, total - input),
so reasoning tokens are counted) and its cost. A response without usable usage metadata is charged the
worst case. The ledger is an append-only JSON-lines file outside the repository; it holds numbers only.

Prices are list prices in USD per million tokens, entered from the provider's pricing page. They change:
the operator must confirm them (evaluation.live --prices-confirmed) before any live call.
"""

import json
import math
import os
import threading
import time
from types import SimpleNamespace

from multi_agent.execution import CodeTrust
from multi_agent.llm import MOCK, PROVIDER

# USD per 1M tokens (input, output). Confirm against https://ai.google.dev/gemini-api/docs/pricing before use.
PRICES_USD_PER_MTOK = {
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
}
DEFAULT_MODEL = "gemini-2.5-flash-lite"
DEFAULT_MAX_OUTPUT_TOKENS = 2048
DEFAULT_LEDGER = os.path.join(os.path.expanduser("~"), ".local", "state", "insightpilot", "live_llm_ledger.jsonl")


class BudgetExceeded(RuntimeError):
    """A call was refused BEFORE it was made: it could exceed the budget or a hard limit."""


class ProviderError(RuntimeError):
    """The provider call failed; the message is redacted."""


def redact(text, secret):
    text = str(text)
    return text.replace(secret, "<redacted>") if secret else text


def text_of(content) -> str:
    """Gemini may return a list of parts; normalise to text (as utils.llm.get_text does)."""
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return content if isinstance(content, str) else str(content)


def billed_usage(usage):
    """(input, output) from provider usage metadata, or None if unusable. Output includes reasoning tokens."""
    if not isinstance(usage, dict):
        return None
    i, o, t = usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens")
    if type(i) is not int or i < 0 or type(o) is not int or o < 0:
        return None
    if type(t) is int and t >= i:
        o = max(o, t - i)
    return i, o


class Ledger:
    """Append-only spend ledger (numbers only), shared by every run that uses the same path."""

    def __init__(self, path=DEFAULT_LEDGER):
        self.path = path
        self._lock = threading.Lock()

    def spent_usd(self) -> float:
        if not os.path.exists(self.path):
            return 0.0
        total = 0.0
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    total += float(json.loads(line)["cost_usd"])
        return total

    def append(self, record: dict):
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")


class GeminiPort:
    """The live provider. Code a provider writes is never trusted (it is not executed in trusted-ops mode)."""
    call_source = PROVIDER
    code_trust = CodeTrust.UNTRUSTED

    def __init__(self, model=DEFAULT_MODEL, max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS, timeout_s=90,
                 env=None):
        env = os.environ if env is None else env
        key = env.get("GOOGLE_API_KEY")
        if not key:
            raise ValueError("GOOGLE_API_KEY is not set in the environment (it is never read from files here)")
        if model not in PRICES_USD_PER_MTOK:
            raise ValueError(f"no confirmed price for model {model!r}")
        from langchain_google_genai import ChatGoogleGenerativeAI
        self.model, self.max_output_tokens = model, max_output_tokens
        self._secret = key
        self._chat = ChatGoogleGenerativeAI(model=model, google_api_key=key, temperature=0.0,
                                            max_output_tokens=max_output_tokens, timeout=timeout_s, max_retries=2)

    def complete(self, skill, system, user):
        from langchain_core.messages import HumanMessage, SystemMessage
        try:
            r = self._chat.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        except Exception as e:                                   # never let the key reach a log or report
            raise ProviderError(f"{type(e).__name__}: {redact(e, self._secret)[:500]}") from None
        return SimpleNamespace(content=text_of(r.content), usage_metadata=getattr(r, "usage_metadata", None))


class BudgetedPort:
    """Wraps a provider port; enforces the budget and hard limits; records usage and cost per call."""

    def __init__(self, inner, *, model, budget_usd, ledger, max_calls, max_total_tokens,
                 max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS, prices=None, run_label="", clock=time.time):
        self.inner, self.model = inner, model
        self.call_source, self.code_trust = inner.call_source, CodeTrust(inner.code_trust)
        self.prices = prices or PRICES_USD_PER_MTOK[model]
        self.budget_usd, self.ledger = float(budget_usd), ledger
        self.max_calls, self.max_total_tokens, self.max_output_tokens = max_calls, max_total_tokens, max_output_tokens
        self.run_label, self.clock = run_label, clock
        self.calls = self.tokens = 0
        self.spent_usd = 0.0
        self.exhausted = False
        self.records = []

    def cost(self, tokens_in, tokens_out):
        return tokens_in / 1e6 * self.prices[0] + tokens_out / 1e6 * self.prices[1]

    def worst_case(self, system, user):
        tin = len((system + user).encode("utf-8"))
        return tin, self.max_output_tokens, self.cost(tin, self.max_output_tokens)

    def _refuse(self, why):
        self.exhausted = True
        raise BudgetExceeded(why)

    def complete(self, skill, system, user):
        tin, tout, worst = self.worst_case(system, user)
        if self.calls >= self.max_calls:
            self._refuse(f"call limit {self.max_calls} reached")
        if self.tokens + tin + tout > self.max_total_tokens:
            self._refuse(f"token limit {self.max_total_tokens} could be exceeded")
        prior = self.ledger.spent_usd()
        if prior + worst > self.budget_usd:
            self._refuse(f"budget ${self.budget_usd:.4f} could be exceeded (spent ${prior:.6f}, "
                         f"worst case of this call ${worst:.6f})")
        self.calls += 1
        t0 = self.clock()
        response = self.inner.complete(skill, system, user)
        usage = billed_usage(getattr(response, "usage_metadata", None))
        charged_worst = usage is None
        i, o = (tin, tout) if charged_worst else usage
        cost = self.cost(i, o)
        self.tokens += i + o
        self.spent_usd += cost
        record = {"ts": round(t0, 3), "run": self.run_label, "model": self.model, "skill": skill,
                  "call_source": self.call_source, "input_tokens": i, "output_tokens": o, "cost_usd": round(cost, 8),
                  "charged_worst_case": charged_worst, "seconds": round(self.clock() - t0, 3)}
        self.records.append(record)
        self.ledger.append(record)
        return response


# ── offline stand-in for dry runs and tests (labelled MOCK; never reported as live) ──────────────

class FakeProvider:
    """Deterministic offline provider: answers from a callable; reports synthetic usage; call_source MOCK."""
    call_source = MOCK
    code_trust = CodeTrust.UNTRUSTED

    def __init__(self, answer):
        self.answer = answer

    def complete(self, skill, system, user):
        text = self.answer(skill, system, user)
        return SimpleNamespace(content=text, usage_metadata={
            "input_tokens": math.ceil(len(system + user) / 4), "output_tokens": math.ceil(len(text) / 4),
            "total_tokens": math.ceil(len(system + user) / 4) + math.ceil(len(text) / 4)})
