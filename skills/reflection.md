---
skill: reflection
type: code-implemented
used_by: agent/nodes/code_generator.py (_get_retry_instructions), agent/nodes/executor.py
purpose: Documentation of the self-correction responsibility
governed_by: rules/retry_and_failure.rules.md
---
# Reflection Skill (code-implemented)

The agent's self-correction loop — how it learns from its own failures.

1. Every execution failure is classified: SYNTAX / DATA / RUNTIME / LOGIC / UNKNOWN.
2. The retry prompt is error-type-specific (_get_retry_instructions):
   syntax errors get syntax-repair guidance; data errors get column/type
   re-verification guidance; runtime errors get import/defensive-coding
   guidance. Blind regeneration is forbidden.
3. The failing code travels WITH the error to the generator — the model
   sees exactly what broke and why.
4. Budget-bounded: max_retries per task (default 3), then graceful failure.
