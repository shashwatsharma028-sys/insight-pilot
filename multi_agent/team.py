"""Wire the four agents and the Supervisor together. Each LLM agent gets its own recording port."""

import time

from multi_agent import contracts as c
from multi_agent import dataset as dsmod
from multi_agent.agents import BusinessInsightAgent, DataAnalystAgent, PlanningAgent, VerificationAgent
from multi_agent.llm import RecordingLLM
from multi_agent.supervisor import Supervisor


def build_team(llm, execution, approval_port=None, clock=time.perf_counter, ds_loader=None):
    """Return (supervisor, llm_calls). `llm` is the shared model port; `execution` the analyst's ExecutionPort."""
    loader = ds_loader or dsmod.load
    calls = []
    planning = PlanningAgent(RecordingLLM(llm, c.PLANNING, calls, clock))
    analyst = DataAnalystAgent(RecordingLLM(llm, c.ANALYST, calls, clock), execution)
    verifier = VerificationAgent(loader)
    insight = BusinessInsightAgent(RecordingLLM(llm, c.INSIGHT, calls, clock))
    return Supervisor(planning, analyst, verifier, insight, approval_port, clock, loader), calls
