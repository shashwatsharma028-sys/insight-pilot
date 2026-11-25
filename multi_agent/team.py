"""Wire the four agents and the Supervisor together. Each LLM agent gets its own recording port."""

import time

from multi_agent import contracts as c
from multi_agent import dataset as dsmod
from multi_agent.agents import BusinessInsightAgent, DataAnalystAgent, PlanningAgent, VerificationAgent
from multi_agent.llm import RecordingLLM
from multi_agent.supervisor import Supervisor


def build_team(llm, execution, approval_port=None, clock=time.perf_counter, ds_loader=None, analyst=None,
               authority=None):
    """
    Return (supervisor, llm_calls). `llm` is the shared model port; `execution` the analyst's ExecutionPort.
    P6: `analyst` replaces the code-writing Data Analyst Agent (e.g. op_engine.TrustedOpsAnalyst, which runs
    no generated code); `execution` must then be None.
    SEC: one RuntimeAuthority (policy engine) per team; each agent's port gets its runtime identity and the authority.
    """
    from multi_agent.security import RuntimeAuthority
    authority = authority or RuntimeAuthority(clock=clock)
    loader = ds_loader or dsmod.load
    calls = []
    planning = PlanningAgent(RecordingLLM(llm, c.PLANNING, calls, clock, authority))
    if analyst is not None:
        if execution is not None:
            raise ValueError("pass either an execution port or a replacement analyst, not both")
    else:
        analyst = DataAnalystAgent(RecordingLLM(llm, c.ANALYST, calls, clock, authority), execution)
    if hasattr(analyst, "bind_authority"):
        analyst.bind_authority(authority)
    verifier = VerificationAgent(loader)
    insight = BusinessInsightAgent(RecordingLLM(llm, c.INSIGHT, calls, clock, authority))
    return Supervisor(planning, analyst, verifier, insight, approval_port, clock, loader, authority=authority), calls
