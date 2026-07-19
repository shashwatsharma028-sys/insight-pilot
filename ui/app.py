"""
Autonomous Data Analyst Agent — Streamlit UI
"""

import streamlit as st
import pandas as pd
import os
import sys
import time
import json
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.graph import analyst_graph
from agent.state import AgentState, AnalysisStatus
from agent.memory.conversation import handle_followup_node
from agent.token_monitor import monitor as token_monitor
from agent.context_manager import measure_context, COMPRESSION_THRESHOLD

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Autonomous Data Analyst",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── Custom CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
.stApp { background-color: #0f1117; }
.metric-card {
    background: #1e2130;
    border-radius: 12px;
    padding: 16px 20px;
    border-left: 4px solid #4f8ef7;
    margin-bottom: 12px;
}
.insight-card {
    background: #1a2340;
    border-radius: 10px;
    padding: 14px 18px;
    border-left: 4px solid #00d4aa;
    margin-bottom: 10px;
    color: #e0e0e0;
}
.task-completed { color: #00d4aa; font-weight: 600; }
.task-failed { color: #ff4b4b; font-weight: 600; }
.task-running { color: #ffa500; font-weight: 600; }
.task-pending { color: #888; }
.chat-user {
    background: #1e2130;
    border-radius: 10px;
    padding: 10px 14px;
    margin: 6px 0;
    border-left: 3px solid #4f8ef7;
}
.chat-agent {
    background: #162030;
    border-radius: 10px;
    padding: 10px 14px;
    margin: 6px 0;
    border-left: 3px solid #00d4aa;
}
</style>
""", unsafe_allow_html=True)


# ── Session state init ─────────────────────────────────────────────────────────
def init_session():
    defaults = {
        "agent_state": None,
        "analysis_complete": False,
        "awaiting_approval": False,
        "uploaded_path": None,
        "dataset_name": "",
        "chat_input": "",
        "run_count": 0
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


init_session()


# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("📊 Data Analyst Agent")
    st.markdown("*Autonomous • Self-correcting • Insightful*")
    st.divider()

    uploaded = st.file_uploader("Upload CSV Dataset", type=["csv"])
    if uploaded:
        save_path = f"/tmp/{uploaded.name}"
        with open(save_path, "wb") as f:
            f.write(uploaded.getbuffer())
        st.session_state.uploaded_path = save_path
        st.session_state.dataset_name = uploaded.name.replace(".csv", "").replace("_", " ").title()
        st.success(f"✅ {uploaded.name}")

        preview = pd.read_csv(save_path)
        st.caption(f"{preview.shape[0]} rows × {preview.shape[1]} columns")

    st.divider()

    # Demo datasets
    st.markdown("**Or use a demo dataset:**")
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    demo_files = {
        "🛒 Sales Data": "sales_data.csv",
        "👷 Attendance Data": "attendance_data.csv",
        "🎓 Student Performance": "student_data.csv",
        "📈 E-commerce Time Series": "ecommerce_timeseries.csv"
    }

    for label, fname in demo_files.items():
        fpath = os.path.join(data_dir, fname)
        if os.path.exists(fpath) and st.button(label, use_container_width=True):
            st.session_state.uploaded_path = fpath
            st.session_state.dataset_name = label.split(" ", 1)[1]
            st.session_state.agent_state = None
            st.session_state.analysis_complete = False
            st.rerun()

    st.divider()
    st.markdown("**Settings**")
    safe_mode = st.toggle("🔐 Safe Mode (approve before execution)", value=True)
    max_retries = st.slider("Max retries per task", 1, 5, 3)

    if st.button("🔄 Reset Session", use_container_width=True):
        for key in ["agent_state", "analysis_complete", "awaiting_approval", "run_count"]:
            st.session_state[key] = None if key == "agent_state" else False if "complete" in key or "approval" in key else 0
        st.rerun()

    # ── Context usage panel ──
    st.divider()
    with st.expander("🧠 Context Window", expanded=False):
        _cstate = st.session_state.get("agent_state")
        if not _cstate:
            st.caption("No active session context yet.")
        else:
            _cm = measure_context(_cstate)
            st.progress(min(_cm["pct"], 1.0),
                        text=f"{_cm['tokens']:,} / {_cm['max_tokens']:,} tokens ({_cm['pct']:.0%})")
            if _cm["pct"] > COMPRESSION_THRESHOLD:
                st.warning("Context above 80% — older conversation is being summarized. "
                           "Critical facts, rules, skills, and lessons are preserved.")
            elif _cstate.get("context_summary"):
                st.caption("Older conversation compressed earlier this session; "
                           "summary in use, recent turns verbatim.")

    # ── Token Usage panel (compact, read-only) ──
    with st.expander("🪙 Token Usage", expanded=False):
        _tm = token_monitor.summary()
        if _tm["calls"] == 0:
            st.caption("No LLM calls yet this session.")
        else:
            tcol1, tcol2 = st.columns(2)
            tcol1.metric("Input", f"{_tm['input_tokens']:,}")
            tcol2.metric("Output", f"{_tm['output_tokens']:,}")
            tcol1.metric("Total", f"{_tm['total_tokens']:,}")
            tcol2.metric("Est. Cost", f"${_tm['estimated_cost_usd']:.4f}")
            st.caption(f"{_tm['calls']} calls"
                       + (f" · {_tm['estimated_calls']} estimated" if _tm['estimated_calls'] else ""))
            if _tm["by_skill"]:
                st.markdown("**By skill:**")
                for _skill, _d in _tm["by_skill"].items():
                    st.caption(f"`{_skill}` — {_d['total']:,} tok ({_d['calls']} calls)")


# ── Main area ──────────────────────────────────────────────────────────────────
st.title("🤖 Autonomous Data Analyst Agent")
st.markdown("*Upload a dataset → Agent plans, codes, self-corrects, and produces insights automatically*")

if not st.session_state.uploaded_path:
    st.info("👈 Upload a CSV or select a demo dataset from the sidebar to begin.")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("""
        <div class="metric-card">
        <h4>🧠 Autonomous Planning</h4>
        <p>Agent decides what analyses to run based on your data structure</p>
        </div>
        """, unsafe_allow_html=True)
    with col2:
        st.markdown("""
        <div class="metric-card">
        <h4>🔁 Self-Correction</h4>
        <p>When code fails, agent reads the error, classifies it, and rewrites intelligently</p>
        </div>
        """, unsafe_allow_html=True)
    with col3:
        st.markdown("""
        <div class="metric-card">
        <h4>💬 Memory & Follow-up</h4>
        <p>Ask follow-up questions after analysis — agent remembers all findings</p>
        </div>
        """, unsafe_allow_html=True)
    st.stop()


# ── Run analysis ───────────────────────────────────────────────────────────────
if not st.session_state.analysis_complete and not st.session_state.awaiting_approval:

    col1, col2 = st.columns([3, 1])
    with col1:
        st.subheader(f"📁 Dataset: {st.session_state.dataset_name}")
        preview = pd.read_csv(st.session_state.uploaded_path)
        st.dataframe(preview.head(5), use_container_width=True)

    with col2:
        st.metric("Rows", preview.shape[0])
        st.metric("Columns", preview.shape[1])
        st.metric("Missing Values", int(preview.isna().sum().sum()))

    if st.button("🚀 Run Autonomous Analysis", type="primary", use_container_width=True):
        initial_state: AgentState = {
            "csv_path": st.session_state.uploaded_path,
            "dataset_name": st.session_state.dataset_name,
            "df_summary": None, "df_dtypes": None, "df_shape": None, "df_sample": None,
            "data_quality_report": None,
            "analysis_plan": [], "plan_approved": False, "user_edits_to_plan": None,
            "current_task_index": 0, "completed_tasks": [], "failed_tasks": [],
            "max_retries": max_retries,
            "chart_paths": [], "execution_log": [],
            "final_report_md": None, "final_report_pdf": None, "notebook_path": None,
            "business_insights": [],
            "conversation_history": [],
            "current_user_message": None, "agent_response": None,
            "safe_mode": safe_mode,
            "is_followup_query": False,
            "error_message": None, "next_action": None
        }

        progress_bar = st.progress(0, text="Initializing agent...")
        status_placeholder = st.empty()

        with st.spinner("Agent is working..."):
            try:
                # Run until await_approval or completion
                for i, step_state in enumerate(analyst_graph.stream(initial_state)):
                    node_name = list(step_state.keys())[0]
                    current = step_state[node_name]

                    progress = min(0.1 + i * 0.05, 0.85)
                    progress_bar.progress(progress, text=f"Running: {node_name}...")

                    last_log = current.get("execution_log", [{}])[-1]
                    status_placeholder.info(f"**{node_name}:** {last_log.get('detail', '...')}")

                    st.session_state.agent_state = current

                    # Pause at approval gate
                    if node_name == "await_approval" and safe_mode:
                        st.session_state.awaiting_approval = True
                        progress_bar.progress(0.45, text="Waiting for your approval...")
                        st.rerun()
                        break

                if not st.session_state.awaiting_approval:
                    progress_bar.progress(1.0, text="Analysis complete!")
                    st.session_state.analysis_complete = True
                    st.rerun()

            except Exception as e:
                st.error(f"Agent error: {str(e)}")
                st.exception(e)


# ── Human-in-the-loop approval ─────────────────────────────────────────────────
elif st.session_state.awaiting_approval:
    state = st.session_state.agent_state
    st.subheader("🔐 Review & Approve Analysis Plan")

    if state.get("data_quality_report"):
        q = state["data_quality_report"]
        cols = st.columns(4)
        cols[0].metric("Rows", q["total_rows"])
        cols[1].metric("Columns", q["total_columns"])
        cols[2].metric("Duplicates", q["duplicate_rows"])
        cols[3].metric("Quality Score", f"{q['quality_score']}/100")

        if q.get("recommendations"):
            with st.expander("⚠️ Data Quality Recommendations"):
                for rec in q["recommendations"]:
                    st.warning(rec)

    st.markdown("### 📋 Proposed Analysis Plan")
    st.info("Review the tasks below. Edit the instructions field to modify, then approve to run.")

    plan = state.get("analysis_plan", [])
    type_emojis = {
        "descriptive": "📊", "correlation": "🔗", "outlier": "🎯",
        "trend": "📈", "forecast": "🔮", "distribution": "📉",
        "comparative": "⚖️", "custom": "🔧"
    }

    for i, task in enumerate(plan):
        emoji = type_emojis.get(task["analysis_type"], "📌")
        with st.expander(f"{emoji} **{i+1}. {task['title']}** *(Priority {task['priority']})*", expanded=i < 3):
            st.markdown(f"**Type:** `{task['analysis_type']}`")
            st.markdown(f"**What it will reveal:** {task['description']}")

    user_edits = st.text_area(
        "✏️ Optional: Edit or skip tasks (e.g., 'Skip task 3, also add a revenue by product analysis')",
        placeholder="Leave empty to run plan as-is...",
        height=80
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("✅ Approve & Run", type="primary", use_container_width=True):
            state["plan_approved"] = True
            state["user_edits_to_plan"] = user_edits if user_edits.strip() else None
            state["awaiting_approval"] = False

            if user_edits.strip():
                # Replan with edits first
                from agent.nodes.planner import planner_node
                state = planner_node({**state, "user_edits_to_plan": user_edits})

            st.session_state.awaiting_approval = False

            progress_bar = st.progress(0.45, text="Resuming analysis...")
            status_placeholder = st.empty()

            with st.spinner("Executing analysis tasks..."):
                try:
                    # Continue from generate_code
                    from agent.nodes.code_generator import code_generator_node
                    from agent.nodes.executor import executor_node
                    from agent.nodes.interpreter import interpreter_node, advance_task_node
                    from agent.nodes.report_generator import report_generator_node

                    current = state
                    total_tasks = len([t for t in current["analysis_plan"] if t["status"] == "pending"])
                    done_tasks = 0

                    while True:
                        idx = current.get("current_task_index", 0)
                        plan = current.get("analysis_plan", [])

                        if idx >= len(plan):
                            break

                        task = plan[idx]
                        if task["status"] in (AnalysisStatus.COMPLETED, AnalysisStatus.FAILED):
                            current = advance_task_node(current)
                            continue

                        # Generate code
                        status_placeholder.info(f"🧠 Generating code: **{task['title']}**")
                        current = code_generator_node(current)

                        # Execute with retry loop
                        while True:
                            status_placeholder.info(f"⚡ Executing: **{task['title']}** (attempt {plan[idx].get('retry_count', 0) + 1})")
                            current = executor_node(current)
                            next_a = current.get("next_action")

                            if next_a == "generate_code":
                                current = code_generator_node(current)
                            else:
                                break

                        # Interpret
                        if current.get("next_action") == "interpret":
                            status_placeholder.info(f"💡 Interpreting: **{task['title']}**")
                            current = interpreter_node(current)

                        current = advance_task_node(current)
                        done_tasks += 1
                        progress = 0.45 + (done_tasks / max(total_tasks, 1)) * 0.45
                        progress_bar.progress(min(progress, 0.9), text=f"Completed {done_tasks}/{total_tasks} analyses")

                    # Generate report
                    status_placeholder.info("📄 Generating final report...")
                    current = report_generator_node(current)
                    progress_bar.progress(1.0, text="Done!")

                    st.session_state.agent_state = current
                    st.session_state.analysis_complete = True
                    st.rerun()

                except Exception as e:
                    st.error(f"Execution error: {str(e)}")
                    st.exception(e)

    with col2:
        if st.button("🔄 Re-plan", use_container_width=True):
            st.session_state.agent_state["analysis_plan"] = []
            st.session_state.awaiting_approval = False
            st.rerun()


# ── Results display ────────────────────────────────────────────────────────────
elif st.session_state.analysis_complete:
    state = st.session_state.agent_state

    # Tabs
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📈 Analysis Results", "💡 Business Insights",
        "💬 Follow-up Chat", "⏱️ Execution Log", "📥 Downloads"
    ])

    with tab1:
        st.subheader(f"Analysis: {state['dataset_name']}")

        # Quality summary
        q = state.get("data_quality_report", {})
        cols = st.columns(4)
        cols[0].metric("Rows Analyzed", q.get("total_rows", "N/A"))
        cols[1].metric("Columns", q.get("total_columns", "N/A"))
        cols[2].metric("Tasks Completed", len(state.get("completed_tasks", [])))
        cols[3].metric("Quality Score", f"{q.get('quality_score', 0)}/100")

        st.divider()

        # Each completed task
        for task in state.get("completed_tasks", []):
            if task["status"] != AnalysisStatus.COMPLETED:
                continue

            conf = task.get("confidence_score", 0)
            conf_color = "🟢" if conf >= 0.8 else "🟡" if conf >= 0.5 else "🔴"

            with st.expander(f"**{task['title']}** {conf_color} {conf:.0%} confidence", expanded=True):
                col1, col2 = st.columns([3, 2])
                with col1:
                    if task.get("interpretation"):
                        st.markdown(task["interpretation"])
                    if task.get("assumptions"):
                        st.caption(f"*Assumptions: {'; '.join(task['assumptions'])}*")
                with col2:
                    if task.get("chart_path") and os.path.exists(task["chart_path"]):
                        st.image(task["chart_path"], use_container_width=True)

        if state.get("failed_tasks"):
            st.warning(f"⚠️ {len(state['failed_tasks'])} analyses failed after max retries.")

    with tab2:
        st.subheader("💡 Key Business Insights")
        insights = state.get("business_insights", [])
        if insights:
            for insight in insights:
                st.markdown(f"""
                <div class="insight-card">{insight}</div>
                """, unsafe_allow_html=True)
        else:
            st.info("No business insights generated yet.")

    with tab3:
        st.subheader("💬 Follow-up Questions")
        st.info("Ask questions about the analysis results. The agent remembers all findings.")

        # Show conversation history
        for turn in state.get("conversation_history", []):
            if turn["role"] == "user":
                st.markdown(f'<div class="chat-user">🧑 {turn["message"]}</div>', unsafe_allow_html=True)
            else:
                st.markdown(f'<div class="chat-agent">🤖 {turn["message"]}</div>', unsafe_allow_html=True)

        user_input = st.chat_input("Ask a follow-up question...")
        if user_input:
            with st.spinner("Thinking..."):
                updated = handle_followup_node({
                    **state,
                    "current_user_message": user_input,
                    "is_followup_query": True
                })
                st.session_state.agent_state = updated
                st.rerun()

    with tab4:
        st.subheader("⏱️ Agent Execution Timeline")
        log = state.get("execution_log", [])
        if log:
            for entry in log:
                ts = entry.get("timestamp", "")[:19].replace("T", " ")
                node = entry.get("node", "")
                action = entry.get("action", "")
                status = entry.get("status", "")
                detail = entry.get("detail", "")

                status_icon = {"success": "✅", "error": "❌", "retry": "🔄",
                               "warning": "⚠️", "failed": "💀"}.get(status, "▶️")

                st.markdown(f"`{ts}` {status_icon} **{node}** — {action}")
                if detail:
                    st.caption(f"  └─ {detail}")

    with tab5:
        st.subheader("📥 Download Reports")
        col1, col2, col3 = st.columns(3)

        with col1:
            md_path = state.get("final_report_md")
            if md_path and os.path.exists(md_path):
                with open(md_path, "r") as f:
                    st.download_button(
                        "📄 Download Markdown Report",
                        f.read(),
                        file_name=os.path.basename(md_path),
                        mime="text/markdown",
                        use_container_width=True
                    )

        with col2:
            pdf_path = state.get("final_report_pdf")
            if pdf_path and os.path.exists(str(pdf_path)):
                with open(pdf_path, "rb") as f:
                    st.download_button(
                        "📕 Download PDF Report",
                        f.read(),
                        file_name=os.path.basename(pdf_path),
                        mime="application/pdf",
                        use_container_width=True
                    )

        with col3:
            nb_path = state.get("notebook_path")
            if nb_path and os.path.exists(nb_path):
                with open(nb_path, "r") as f:
                    st.download_button(
                        "📓 Download Jupyter Notebook",
                        f.read(),
                        file_name=os.path.basename(nb_path),
                        mime="application/json",
                        use_container_width=True
                    )

        st.divider()
        st.markdown("### 📊 Generated Charts")
        chart_paths = state.get("chart_paths", [])
        if chart_paths:
            cols = st.columns(2)
            for i, cp in enumerate(chart_paths):
                if os.path.exists(cp):
                    with cols[i % 2]:
                        st.image(cp, caption=os.path.basename(cp), use_container_width=True)
        else:
            st.info("No charts generated yet.")
