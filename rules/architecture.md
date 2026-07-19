# Architecture Rules

Scope: the structure of InsightPilot itself — module boundaries, state design,
and how architectural change is allowed to happen.

## Change control (MUST)

1. Architecture changes MUST be approved by the user before implementation.
   "Architecture change" means any of: adding/removing/renaming top-level
   directories or modules, changing the AgentState schema, altering the
   LangGraph node graph (nodes or edges), changing the skill/rule loading
   contracts, or swapping core frameworks (LangGraph, Streamlit, Gemini).
2. Existing features MUST NEVER be removed. Refactors move or restructure
   behavior; they do not delete it. A feature may only be removed with
   explicit user approval naming that feature.
3. Refactors MUST be behavior-neutral by default and MUST prove it:
   the full test suite passes before and after, and where prompts are
   involved, byte-identity is asserted (see tests/test_skills_loader.py
   TestNoBehaviorChange for the pattern).

## Structure (MUST / SHOULD)

4. Module boundaries MUST be respected:
   - agent/nodes/    → graph nodes (one responsibility each)
   - agent/          → state, graph wiring, engines (rules, skills)
   - sandbox/        → untrusted-code execution only
   - skills/         → prompts and responsibility definitions (data, not code)
   - rules/          → governance (this folder)
   - utils/          → cross-cutting helpers (LLM client)
   - tests/          → the safety net
5. The single-agent, shared-state design is the current architecture:
   all nodes read/write one AgentState. Multi-agent patterns (independent
   critic agents) are future scope and require approval (rule 1).
6. Dependencies SHOULD flow one way: nodes may import engines and utils;
   engines and utils MUST NOT import nodes.
7. All LLM access MUST go through utils/llm.py; all prompt text MUST come
   from skills/ (with embedded fallbacks); all governance text MUST come
   from rules/. No hardcoded prompts or rules in node logic.
