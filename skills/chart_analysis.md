---
skill: chart_analysis
type: embedded
used_by: agent/nodes/code_generator.py (chart rules within code generation), agent/nodes/interpreter.py (chart presence in interpretation)
purpose: Documentation of charting conventions
governed_by: rules/code_generation.rules.md (rules 2, 10)
---
# Chart Analysis Skill (embedded)

Charting is currently embedded in code generation rather than a separate
LLM call (vision-based chart critique is future scope).

Conventions enforced:
1. Charts save to CHART_PATH with bbox_inches='tight', then plt.close('all').
2. The sandbox auto-saves any open figure if generated code forgets.
3. Titles, axis labels, and readable tick formatting are required by
   code_generation rules; the interpreter is told whether a chart exists
   and factors that into its findings.
