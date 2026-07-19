# Lessons Store

Machine-generated reflections from the agent's own runs, stored as
`lessons.json`. Each lesson records:

```json
{
  "issue": "what went wrong or was weak",
  "cause": "why (from execution facts: error types, retries, confidence)",
  "fix": "what to do differently next time",
  "confidence": 0.9,
  "timestamp": "ISO datetime",
  "dataset": "which dataset the lesson came from",
  "task_title": "which task",
  "analysis_type": "descriptive | correlation | ...",
  "kind": "failure | retry | weak_output | user_correction"
}
```

Lifecycle: generated deterministically after every task by
`agent/reflection_engine.py`; the most relevant lessons (same dataset,
same analysis types, newest first, max 5) are injected into the planner's
prompt before every new plan.

Honesty note: this is stored-context recall, NOT model learning. The LLM
is never retrained; delete `lessons.json` and the agent forgets everything.
