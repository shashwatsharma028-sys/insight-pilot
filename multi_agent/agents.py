"""
The four specialist agents. Each one:
- consumes exactly one request kind and produces one reply kind (multi_agent.contracts);
- receives only its own typed input (its context boundary), built by the Supervisor;
- holds only the tools listed in its AgentSpec.

Agents never call each other: every handoff goes back through the Supervisor.
"""

import json
import re
from dataclasses import dataclass

from multi_agent import contracts as c
from multi_agent import prompts
from multi_agent import verification
from multi_agent.execution import (CodeTrust, ExecutionInfrastructureError, ExecutionOutcome,
                                   UntrustedCodeRefused, classify_execution_failure)
from multi_agent.llm import LLMOutputError


@dataclass(frozen=True)
class AgentSpec:
    name: str
    responsibility: str
    consumes: tuple
    produces: tuple
    tools: tuple
    sees: tuple
    never_sees: tuple


SPECS = {
    c.PLANNING: AgentSpec(
        c.PLANNING, "decompose the goal into tasks with machine-checkable claims",
        ("plan_request",), ("plan",), ("llm",),
        ("dataset profile: column names, kinds, missing counts, row count, SHA-256", "user goal", "plan problems"),
        ("raw rows", "code", "execution output", "verification results")),
    c.ANALYST: AgentSpec(
        c.ANALYST, "write and run analysis code for one task and report its claims",
        ("analysis_request",), ("analysis_result",), ("llm", "execution_port"),
        ("one task and its claim specs", "column names and kinds", "feedback from earlier attempts"),
        ("other tasks", "recomputed values", "insights")),
    c.VERIFIER: AgentSpec(
        c.VERIFIER, "recompute every claim from the dataset; check insight grounding",
        ("verification_request", "insight_check_request"), ("verification_report", "insight_check_report"),
        ("dataset_reader",),
        ("claim specs", "reported claim values", "dataset path and expected SHA-256", "insights to check"),
        ("analyst code", "analyst stdout", "any LLM")),
    c.INSIGHT: AgentSpec(
        c.INSIGHT, "turn verified claims into cited business insights",
        ("insight_request",), ("insight_report",), ("llm",),
        ("task title and description", "verified claims only", "feedback from earlier attempts"),
        ("refuted or unverified claims", "code", "raw rows", "dataset path")),
}

_FENCE = re.compile(r"^```[a-zA-Z]*\n(.*?)\n```\s*$", re.S)


def _unfence(text: str) -> str:
    m = _FENCE.match(text.strip())
    return m.group(1) if m else text


def _json_reply(text: str):
    try:
        return json.loads(_unfence(text))
    except json.JSONDecodeError as e:
        raise LLMOutputError(f"reply is not valid JSON: {e.msg}") from None


class PlanningAgent:
    spec = SPECS[c.PLANNING]

    def __init__(self, llm):
        self.llm = llm

    def run(self, req: dict) -> dict:
        reply = self.llm.complete("planner", prompts.PLANNING_SYSTEM, prompts.planning(req))
        plan = _json_reply(reply.content)
        if isinstance(plan, list):
            plan = {"tasks": plan}
        if not isinstance(plan, dict):
            raise LLMOutputError("plan must be a JSON object")
        return plan


_CLAIM_LINE = re.compile(r"^CLAIM (\S+?)=(.*)$")


def parse_claims(stdout: str, planned: set) -> tuple:
    """(claims, claim_errors, unplanned) from CLAIM lines; a duplicated key is an error, never last-wins."""
    claims, errors, unplanned, seen = {}, {}, [], set()
    for line in (stdout or "").splitlines():
        m = _CLAIM_LINE.match(line.strip())
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if key not in planned:
            unplanned.append(key[:64])
            continue
        if key in seen:
            claims.pop(key, None)
            errors[key] = "claim reported more than once"
            continue
        seen.add(key)
        try:
            claims[key] = json.loads(raw)
        except json.JSONDecodeError:
            errors[key] = "value is not valid JSON"
    return claims, errors, sorted(set(unplanned))


class DataAnalystAgent:
    spec = SPECS[c.ANALYST]

    def __init__(self, llm, execution):
        self._check_trust(llm, execution)
        self.llm, self.execution = llm, execution

    @staticmethod
    def _check_trust(llm, execution):
        if CodeTrust(llm.code_trust) is not CodeTrust.SCRIPTED_FIXTURES and not execution.hardened:
            raise UntrustedCodeRefused(
                f"code from a {llm.call_source} model is untrusted and cannot run on {execution.name}")

    def run(self, req: dict) -> dict:
        self._check_trust(self.llm, self.execution)
        task = req["task"]
        reply = self.llm.complete("data_analyst", prompts.ANALYST_SYSTEM, prompts.analysis(req))
        code = _unfence(reply.content)
        if len(code) > c.MAX_CODE_CHARS:
            raise LLMOutputError("generated code is too long")
        try:
            outcome = self.execution.execute(code, req["dataset_path"], f"{task['id']}_a{req['attempt']}",
                                             dataset_sha256=req["profile"]["dataset_sha256"])
        except Exception as e:
            if classify_execution_failure(e) is None:      # unknown port failure: fail closed, never retried
                raise ExecutionInfrastructureError(f"execution port failed ({type(e).__name__})") from e
            raise
        if not isinstance(outcome, ExecutionOutcome):
            raise ExecutionInfrastructureError("execution port returned something other than an ExecutionOutcome")
        if self.execution.hardened and outcome.isolation != "rootless_container":
            raise ExecutionInfrastructureError("hardened port reported a result without container isolation")
        execution = outcome.as_dict()
        execution["stdout"] = execution["stdout"][:c.MAX_STDOUT_CHARS]
        execution["error"] = execution["error"][:2000]
        planned = {cl["key"] for cl in task["claims"]}
        claims, errors, unplanned = parse_claims(outcome.stdout, planned) if outcome.ok else ({}, {}, [])
        return {"task_id": task["id"], "attempt": req["attempt"], "code": code, "execution": execution,
                "claims": claims, "claim_errors": errors, "unplanned": unplanned}


class VerificationAgent:
    """Deterministic and LLM-free by construction: it has no llm attribute and accepts none."""
    spec = SPECS[c.VERIFIER]

    def __init__(self, ds_loader=None):
        from multi_agent import dataset
        self.ds_loader = ds_loader or dataset.load

    def run(self, kind: str, req: dict) -> dict:
        if kind == "verification_request":
            return verification.verify_claims(req, self.ds_loader)
        if kind == "insight_check_request":
            return verification.check_insights(req)
        raise c.HandoffRejected([f"verification agent cannot handle {kind!r}"])


class BusinessInsightAgent:
    spec = SPECS[c.INSIGHT]

    def __init__(self, llm):
        self.llm = llm

    def run(self, req: dict) -> dict:
        reply = self.llm.complete("business_insight", prompts.INSIGHT_SYSTEM, prompts.insight(req))
        out = _json_reply(reply.content)
        if not isinstance(out, dict) or "insights" not in out:
            raise LLMOutputError("insight reply must be an object with 'insights'")
        return {"task_id": req["task_id"], "insights": out["insights"]}
