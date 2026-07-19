"""
Conversation Memory
Handles multi-turn context so follow-up queries understand previous analyses.
"""

from datetime import datetime
from langchain_core.messages import HumanMessage, SystemMessage
from agent.state import AgentState, ConversationTurn
from utils.llm import get_llm, get_text
from agent.skills_loader import load_skill


_DEFAULT_FOLLOWUP_SYSTEM_PROMPT = """You are a data analyst AI with memory of a completed analysis session.
The user is asking a follow-up question about the dataset or analysis results.

Answer conversationally and specifically, referencing the actual findings from the analysis.
If the question requires a new analysis, say so clearly and describe what you would do.
Keep responses concise — 2-4 sentences unless more detail is needed.
"""

# Loaded from skills/followup_chat.md — falls back to embedded default if missing
FOLLOWUP_SYSTEM_PROMPT = load_skill("followup_chat", fallback=_DEFAULT_FOLLOWUP_SYSTEM_PROMPT)



def handle_followup_node(state: AgentState) -> AgentState:
    """Handle conversational follow-up queries using analysis context."""

    user_message = state.get("current_user_message", "")
    if not user_message:
        return {**state, "next_action": "done"}

    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "node": "conversation",
        "action": f"Handling follow-up: {user_message[:60]}..."
    }

    llm = get_llm(temperature=0.3)

    # Build context from completed analyses
    analysis_context = "\n\n".join([
        f"Analysis: {t['title']}\nFindings: {t.get('interpretation', 'No interpretation')}\n"
        f"Confidence: {t.get('confidence_score', 0):.0%}"
        for t in state.get("completed_tasks", [])
        if t.get("interpretation")
    ])

    # Build conversation history for context
    history_str = "\n".join([
        f"{turn['role'].upper()}: {turn['message']}"
        for turn in state.get("conversation_history", [])[-6:]  # Last 3 turns
    ])

    prompt = f"""
DATASET: {state['dataset_name']}
DATASET SHAPE: {state.get('df_shape', 'Unknown')}

COMPLETED ANALYSES AND FINDINGS:
{analysis_context or "No analyses completed yet."}

BUSINESS INSIGHTS GENERATED:
{chr(10).join(state.get("business_insights", [])) or "None yet."}

CONVERSATION HISTORY:
{history_str or "This is the first follow-up."}

USER'S QUESTION: {user_message}

Answer the question based on the analysis results above.
If a new analysis is needed, say "I can run a new analysis for this — shall I add it to the plan?"
"""

    try:
        messages = [
            SystemMessage(content=FOLLOWUP_SYSTEM_PROMPT),
            HumanMessage(content=prompt)
        ]
        response = llm.invoke(messages)
        agent_response = get_text(response)

        # Update conversation history
        history = state.get("conversation_history", [])
        new_turns = [
            ConversationTurn(
                role="user",
                message=user_message,
                timestamp=datetime.now().isoformat(),
                related_task_id=None
            ),
            ConversationTurn(
                role="agent",
                message=agent_response,
                timestamp=datetime.now().isoformat(),
                related_task_id=None
            )
        ]

        log_entry["status"] = "success"

        return {
            **state,
            "agent_response": agent_response,
            "conversation_history": history + new_turns,
            "execution_log": state.get("execution_log", []) + [log_entry],
            "next_action": "done"
        }

    except Exception as e:
        log_entry["status"] = "error"
        return {
            **state,
            "agent_response": f"I couldn't process that question: {str(e)}",
            "execution_log": state.get("execution_log", []) + [log_entry],
            "next_action": "done"
        }
