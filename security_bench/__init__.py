"""
InsightPilot V2 offline adversarial security benchmark (synthetic data, scripted models, no network).

The same scenario code runs against two source trees: the unmodified V2 baseline and the secured V2 branch.
It uses only public interfaces present in both (team.build_team, Supervisor, TrustedOpsAnalyst, AnalysisService,
BudgetedPort, contracts, claims), and judges every case by its observable EFFECT (what reached the provider, what
was executed or accepted), never by a detector's classification alone. See docs/security/v2/BENCHMARK_METHODOLOGY.md.
"""
