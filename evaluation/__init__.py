"""
InsightPilot V2 P4.0: deterministic offline evaluation and benchmarking.

The harness runs the UNMODIFIED V1 LangGraph pipeline on the four demo
datasets, with the scripted LLM from tests/v2 (no network and no API key),
and measures:
- task completion and numerical accuracy against the 28 independently
  computed golden answers;
- data-quality accuracy against the 20 reference values;
- retries, failures, sandbox and pipeline latency;
- chart and report validity;
- LLM calls, with explicit token provenance.

It writes JSON, CSV and Markdown reports. evaluation.paired compares two
benchmark results fairly: identical workload, paired samples and
correctness guards.

Nothing in V1 (agent/, sandbox/, utils/, ui/) imports this package. All
outputs go to a caller-chosen directory, never to reports/ or lessons/.
"""

HARNESS_VERSION = "p4.1"     # p4.1: chart pixel hashes, execute_code variants, A/B runner
