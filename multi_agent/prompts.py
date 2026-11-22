"""
Prompts for the LLM-backed agents. Each prompt is built only from that agent's
typed input, so the prompt is the agent's whole context: tests check what each
prompt can and cannot contain.
"""

import json

from multi_agent.claims import describe_ops

PLANNING_SYSTEM = (
    "You are the Planning Agent of a data-analysis team. You see only the dataset's schema, never its rows. "
    "Propose analysis tasks. Every task must state the numbers it will establish as claims using only the "
    "allowed ops. Reply with JSON: {\"tasks\": [{\"id\", \"title\", \"description\", \"analysis_type\", "
    "\"claims\": [{\"key\", \"op\", \"args\"}]}]}."
)

ANALYST_SYSTEM = (
    "You are the Data Analyst Agent. Write Python (pandas) for one task. The DataFrame is preloaded as df; "
    "save any chart to CHART_PATH. For every claim print exactly one line: CLAIM <key>=<json value>. "
    "Reply with code only."
)

INSIGHT_SYSTEM = (
    "You are the Business Insight Agent. Write short business insights using ONLY the verified claims given. "
    "Every insight must cite the claim keys it relies on, and every number you state must come from them. "
    "Reply with JSON: {\"insights\": [{\"text\", \"cites\": [claim keys]}]}."
)


def _bullets(items):
    return "\n".join(f"- {x}" for x in items) if items else "- none"


def planning(req: dict) -> str:
    return (f"PLANNING ATTEMPT: {req['attempt']}\n"
            f"USER GOAL: {req['goal']}\n"
            f"DATASET PROFILE JSON:\n{json.dumps(req['profile'], sort_keys=True)}\n"
            f"CLAIM OPS JSON:\n{json.dumps(describe_ops(), sort_keys=True)}\n"
            f"MAX TASKS: {req['max_tasks']}\n"
            f"PREVIOUS PROBLEMS:\n{_bullets(req['feedback'])}\n")


def analysis(req: dict) -> str:
    t = req["task"]
    columns = [{"name": c["name"], "kind": c["kind"]} for c in req["profile"]["columns"]]
    return (f"TASK ID: {t['id']}\n"
            f"TASK: {t['title']}\n"
            f"DESCRIPTION: {t['description']}\n"
            f"ATTEMPT: {req['attempt']}\n"
            f"COLUMNS JSON: {json.dumps(columns)}\n"
            f"CLAIMS JSON: {json.dumps(t['claims'], sort_keys=True)}\n"
            f"FEEDBACK FROM PREVIOUS ATTEMPTS:\n{_bullets(req['feedback'])}\n")


def insight(req: dict) -> str:
    return (f"TASK ID: {req['task_id']}\n"
            f"TASK: {req['title']}\n"
            f"DESCRIPTION: {req['description']}\n"
            f"ATTEMPT: {req['attempt']}\n"
            f"VERIFIED CLAIMS JSON: {json.dumps(req['verified_claims'], sort_keys=True)}\n"
            f"FEEDBACK FROM PREVIOUS ATTEMPTS:\n{_bullets(req['feedback'])}\n")
