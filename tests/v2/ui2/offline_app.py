"""
OFFLINE V2 page for AppTest and visual review (UI-2). An injected mock provider answers every model call: no
network, no key, no live call. This file is a test fixture, not a production entry point.

    streamlit run tests/v2/ui2/offline_app.py      # local visual review only

Test hooks (session_state, set by AppTest before the first run):
- ip_v2_test_plans: planner replies in order (the last repeats); default: a plan for the fleet sample
- ip_v2_test_insight: insight reply; ip_v2_test_fail: {skill: message} provider failures
- ip_v2_test_config: ServiceConfig keyword overrides; ip_v2_test_base_dir: session directory parent
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import streamlit as st  # noqa: E402

from multi_agent import live_llm  # noqa: E402
from v2 import app_page  # noqa: E402
from v2.app_service import AnalysisService, Limits, ServiceConfig  # noqa: E402

st.set_page_config(page_title="InsightPilot V2 (offline)", layout="wide")

DEFAULT_PLAN = {"tasks": [
    {"id": "t1", "title": "Maintenance cost by depot", "description": "Total maintenance cost for each depot.",
     "analysis_type": "comparative",
     "claims": [{"key": "cost_by_depot", "op": "group_sum", "args": {"by": "depot", "column": "cost"}},
                {"key": "top_depot", "op": "argmax_group_sum", "args": {"by": "depot", "column": "cost"}}]},
    {"id": "t2", "title": "Service coverage", "description": "Share of vehicles that were serviced.",
     "analysis_type": "descriptive",
     "claims": [{"key": "serviced_rate", "op": "rate_where_equals", "args": {"column": "serviced", "value": "True"}},
                {"key": "mean_km", "op": "mean", "args": {"column": "km"}}]}]}


class MockModel:
    def __init__(self, plans, insight, fail):
        self.plans, self.insight, self.fail, self.calls = list(plans), insight, fail, []

    def __call__(self, skill, system, user):
        self.calls.append(skill)
        if skill in self.fail:
            raise live_llm.ProviderError(self.fail[skill])
        if skill == "planner":
            p = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
            return json.dumps(p)
        keys = list(json.loads(user.split("VERIFIED CLAIMS JSON: ", 1)[1].split("\n", 1)[0]))
        return json.dumps(self.insight or {"insights": [{"text": "The verified figures above answer this task.",
                                                         "cites": keys[:1]}]})


def factory():
    mock = MockModel(st.session_state.get("ip_v2_test_plans", [DEFAULT_PLAN]),
                     st.session_state.get("ip_v2_test_insight"), st.session_state.get("ip_v2_test_fail", {}))
    st.session_state["ip_v2_test_mock"] = mock
    cfg = dict(st.session_state.get("ip_v2_test_config", {}))
    if "limits" in cfg:
        cfg["limits"] = Limits(**cfg["limits"])
    return AnalysisService(live_llm.FakeProvider(mock), config=ServiceConfig(**cfg),
                           base_dir=st.session_state.get("ip_v2_test_base_dir"))


app_page.render(factory, provider_label="Offline mock provider: every model reply is scripted; no live model is "
                                       "called, and usage figures are synthetic.")
