---
skill: memory
type: code-implemented
used_by: agent/state.py, agent/memory/conversation.py
purpose: Documentation of state & memory responsibility (no LLM prompt)
governed_by: rules/llm_usage.rules.md
---
# Memory Skill (code-implemented)

This responsibility has NO LLM prompt — it is the state architecture.

1. Working memory: the AgentState TypedDict flows through every node —
   dataset profile, plan, task results, logs, insights. Single source of truth.
2. Conversational memory: conversation_history stores user/agent turns;
   the followup_chat skill receives the last 3 turns plus all stored
   findings, enabling multi-turn context without re-analysis.
3. Persistence boundary: memory is session-scoped (Streamlit session state).
   Cross-session persistence is future scope.
