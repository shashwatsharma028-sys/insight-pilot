"""
Offline scripted LLM for V2 tests.

It replaces only the *provider* chat model: the real utils.llm.RateLimitedLLM
wrapper and the real token monitor still run around it, so the V1 LLM client
code path is exercised unchanged. Nothing here contacts a network service.

Token numbers recorded while this mock is active are the token monitor's
character-based ESTIMATES of mock prompts. They are not provider tokens and
must never be reported as provider cost or savings.
"""

import re
import sys

from tests.v2.golden import scenarios as sc


class MockResponse:
    """Mimics a LangChain AIMessage without usage_metadata (forces estimate)."""

    def __init__(self, content: str):
        self.content = content


class ScriptedLLMError(LookupError):
    """The pipeline sent a prompt the script has no answer for."""


class ScriptedChat:
    def __init__(self, skill: str, scenario: dict, calls: list):
        self.skill = skill
        self.scenario = scenario
        self.calls = calls

    def invoke(self, messages, *args, **kwargs):
        human = messages[-1].content if messages else ""
        system = messages[0].content if len(messages) > 1 else ""
        self.calls.append({"skill": self.skill,
                           "system_chars": len(system), "user_chars": len(human)})
        return MockResponse(self._reply(human))

    def _reply(self, prompt: str) -> str:
        s = self.scenario
        if self.skill == "planner":
            return sc.plan_json(s, edited="USER EDITS/INSTRUCTIONS:" in prompt)
        if self.skill == "code_generator":
            m = re.search(r"^TASK: (.+)$", prompt, re.M)
            title = m.group(1).strip() if m else None
            if title not in s["code"]:
                raise ScriptedLLMError(f"no scripted code for task {title!r}")
            r = re.search(r"RETRY #(\d+)", prompt)
            attempt = int(r.group(1)) if r else 0
            attempts = s["code"][title]
            return attempts[min(attempt, len(attempts) - 1)]
        if self.skill == "business_insights":
            m = re.search(r"^ANALYSIS PERFORMED: (.+)$", prompt, re.M)
            return sc.interpretation_json(m.group(1).strip() if m else "unknown")
        if self.skill == "followup_chat":
            return sc.FOLLOWUP_REPLY
        raise ScriptedLLMError(f"unexpected LLM skill {self.skill!r}")


class MockLLMController:
    """Installs the scripted model into every module that imported get_llm."""

    def __init__(self, scenario: dict):
        self.scenario = scenario
        self.calls: list = []

    def get_llm(self, temperature: float = 0.1, skill: str = "unknown"):
        from utils.llm import RateLimitedLLM
        return RateLimitedLLM(ScriptedChat(skill, self.scenario, self.calls), skill=skill)

    def install(self, monkeypatch):
        import utils.llm
        import agent.graph  # noqa: F401  (imports every node module)
        import agent.memory.conversation  # noqa: F401
        original = utils.llm.get_llm
        patched = []
        for name, module in list(sys.modules.items()):
            if module is not None and getattr(module, "get_llm", None) is original:
                monkeypatch.setattr(module, "get_llm", self.get_llm)
                patched.append(name)
        assert "agent.nodes.planner" in patched, patched
        return patched

    def calls_by_skill(self) -> dict:
        out = {}
        for c in self.calls:
            out[c["skill"]] = out.get(c["skill"], 0) + 1
        return dict(sorted(out.items()))
