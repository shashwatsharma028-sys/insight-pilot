"""
LLM port used by the Planning, Data Analyst and Business Insight agents.

A port has:
    call_source  "mock_scripted" or "provider" (the labels of evaluation.provenance)
    code_trust   the trust of code this model writes (multi_agent.execution.CodeTrust)
    complete(skill, system, user) -> object with .content (and optional .usage_metadata)

RecordingLLM wraps a port for one agent and records every call (agent, skill,
prompt and response sizes, usage metadata, duration). The Verification Agent
has no LLM port at all.

LangChainChatAdapter adapts a LangChain chat model for a later live phase.
P5.0 never constructs a provider model; its code is always UNTRUSTED.
"""

import time

from multi_agent.execution import CodeTrust

MOCK = "mock_scripted"
PROVIDER = "provider"


class LLMOutputError(ValueError):
    """The model's reply could not be parsed into the agent's output contract."""


class RecordingLLM:
    """With an `authority` (the trusted runtime's policy engine, set by team.build_team), every call is checked
    BEFORE it reaches the provider: the agent (identity assigned here, never by a model) must hold the capability
    llm:<skill>, and the serialized prompt must pass the outbound policy. Replies over MAX_REPLY_CHARS are refused
    before anything parses them (always, with or without an authority)."""

    def __init__(self, inner, agent: str, calls: list, clock=time.perf_counter, authority=None):
        self.inner, self.agent, self.calls, self.clock = inner, agent, calls, clock
        self.call_source = inner.call_source
        self.code_trust = CodeTrust(inner.code_trust)
        self.authority = authority

    def complete(self, skill: str, system: str, user: str):
        from multi_agent.security import outbound
        if self.authority is not None:
            self.authority.capability(self.agent, "llm:" + str(skill))
            outbound.check_prompt(self.authority, self.agent, skill, system, user)
        t0 = self.clock()
        response = self.inner.complete(skill, system, user)
        content = getattr(response, "content", "")
        if not isinstance(content, str):
            raise LLMOutputError("model reply content is not text")
        if len(content) > outbound.MAX_REPLY_CHARS or (
                self.authority is not None and not outbound.check_reply(self.authority, self.agent, content)):
            raise LLMOutputError(f"model reply is larger than {outbound.MAX_REPLY_CHARS} characters")
        self.calls.append({
            "agent": self.agent, "skill": skill, "call_source": self.call_source,
            "system_chars": len(system), "user_chars": len(user), "response_chars": len(content),
            "usage_metadata": getattr(response, "usage_metadata", None), "seconds": self.clock() - t0,
        })
        return response


class LangChainChatAdapter:
    """Provider adapter (not used offline). Generated code from a provider is never trusted."""
    call_source = PROVIDER
    code_trust = CodeTrust.UNTRUSTED

    def __init__(self, chat_model):
        self.chat_model = chat_model

    def complete(self, skill, system, user):
        from langchain_core.messages import HumanMessage, SystemMessage
        return self.chat_model.invoke([SystemMessage(content=system), HumanMessage(content=user)])
