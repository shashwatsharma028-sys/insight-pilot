# 🚀 InsightPilot — Autonomous Data Analyst Agent

An agentic AI system that autonomously analyzes CSV datasets — planning its own investigation, writing and self-correcting Python code, and producing business insights with cited confidence scores.

Built with **LangGraph + Gemini + Streamlit** as a summer training project demonstrating modern agentic AI patterns.

---

## 🎯 Problem Statement

Extracting insights from operational data requires manual analysis that is slow, error-prone, and skill-dependent. Business users can't query their own data without a data analyst. This project builds an autonomous agent that closes that gap — given any CSV, it plans, codes, corrects, and interprets without human intervention at every step.

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🧠 Autonomous Planning | Agent decides what analyses to run based on dataset structure |
| 👤 Human-in-the-Loop | Review and edit the analysis plan before execution |
| ⚡ Code Generation | LLM writes pandas/matplotlib code for each analysis |
| 🔁 Self-Correction Loop | Classifies errors (syntax/runtime/data/logic) and rewrites code intelligently |
| 🔍 Data Quality Checks | Auto-detects missing values, duplicates, outliers, type issues |
| 📊 Chart Generation | Produces matplotlib/seaborn visualizations per analysis |
| 💡 Business Insights | Translates technical findings into actionable recommendations |
| 🎯 Confidence Scores | Each insight comes with a confidence rating and assumptions |
| 💬 Conversation Memory | Ask follow-up questions — agent remembers all findings |
| 📄 Report Export | Markdown report + PDF + Jupyter notebook of all code |
| ⏱️ Execution Timeline | Full log of every agent action for transparency |
| 🔐 Safe Mode | Optional approval gate before any code executes |

---

## 🏗️ Architecture

```
CSV Upload
    │
    ▼
┌─────────────────┐
│  Data Ingestion │  → Loads CSV, runs quality checks, builds LLM-friendly summary
└────────┬────────┘
         │
    ▼
┌─────────────────┐
│     Planner     │  → Generates 5-8 analysis tasks based on dataset profile
└────────┬────────┘
         │
    ▼ (safe_mode=True)
┌─────────────────┐
│ Await Approval  │  → Human reviews/edits plan before execution
└────────┬────────┘
         │
    ┌────▼─────────────────────────────────┐
    │         Execution Loop               │
    │                                      │
    │  ┌──────────────┐                   │
    │  │ Code Generator│ ← retry on error  │
    │  └──────┬───────┘                   │
    │         │                            │
    │  ┌──────▼───────┐                   │
    │  │   Executor   │ → sandbox subprocess│
    │  └──────┬───────┘                   │
    │         │ success / error            │
    │  ┌──────▼───────┐                   │
    │  │  Interpreter │ → insights + scores│
    │  └──────┬───────┘                   │
    │         │                            │
    │  ┌──────▼───────┐                   │
    │  │  Next Task   │ → loop or report   │
    │  └──────────────┘                   │
    └──────────────────────────────────────┘
         │
    ▼
┌─────────────────┐
│ Report Generator│  → Markdown + PDF + Jupyter notebook
└────────┬────────┘
         │
    ▼
┌─────────────────┐
│ Follow-up Chat  │  → Conversational Q&A with memory of all findings
└─────────────────┘
```

### Key Agentic Patterns Demonstrated

1. **Planning Loop** — Agent decomposes a goal into sub-tasks autonomously
2. **Tool Use** — Code execution sandbox as an agent tool
3. **Self-Correction** — Error classification → targeted retry with different strategy
4. **Memory** — Conversation history retained across follow-up queries
5. **Human-in-the-Loop** — Approval gate with plan editing capability

---

## 🚀 Setup

### 1. Clone and install
```bash
git clone https://github.com/yourusername/insight-pilot.git
cd insight-pilot
pip install -r requirements.txt
```

### 2. Set up API key
```bash
cp .env.example .env
# Add your Gemini API key to .env:
# GOOGLE_API_KEY=your_key_here
```
Get a free Gemini API key at: https://aistudio.google.com/

### 3. Generate demo datasets
```bash
python data/generate_datasets.py
```

### 4. Run the app
```bash
streamlit run ui/app.py
```

---

## 📁 Project Structure

```
autonomous-data-analyst/
├── agent/
│   ├── state.py              # Shared AgentState TypedDict
│   ├── graph.py              # LangGraph pipeline definition
│   ├── nodes/
│   │   ├── data_ingestion.py # Node 1: Load + quality check
│   │   ├── planner.py        # Node 2: Generate analysis plan
│   │   ├── code_generator.py # Node 3: Write Python code
│   │   ├── executor.py       # Node 4: Run + retry loop
│   │   ├── interpreter.py    # Node 5: Generate insights
│   │   └── report_generator.py # Node 6: Export reports
│   └── memory/
│       └── conversation.py   # Follow-up query handling
├── sandbox/
│   └── executor.py           # Subprocess sandbox + error classification
├── data/
│   └── generate_datasets.py  # Synthetic dataset generator
├── reports/                  # Generated reports and charts
├── ui/
│   └── app.py               # Streamlit frontend
├── utils/
│   └── llm.py               # Gemini LLM wrapper
├── requirements.txt
├── .env.example
└── README.md
```

---

## 🧪 Demo Datasets

| Dataset | Rows | Use Case |
|---------|------|----------|
| Sales Data | ~290 | Regional sales trends, March drop detection |
| Attendance Data | ~15,600 | Ghost attendance, absenteeism, site analysis |
| Student Performance | 2,500 | Score distributions, pass/fail rates |
| E-commerce Time Series | 730 | Revenue trends, seasonality, forecasting |

---

## 🎓 Interview Talking Points

**"What makes this an agent and not just an LLM call?"**
The system maintains state across multiple steps, uses a tool (code executor), reads tool output (error messages), and modifies its next action based on what it observes — that planning-action-observation loop is what defines agency.

**"Why LangGraph over a simple chain?"**
LangGraph's graph structure allows conditional routing — the retry loop sends execution back to code generation only on failure, with different prompting strategy based on error type. A simple chain can't branch.

**"How does the self-correction work?"**
The sandbox executor classifies errors into four types (syntax, runtime, data, logic). The code generator node receives the error type and generates targeted fix instructions — different guidance for a NameError vs a KeyError vs a SyntaxError. This is smarter than just "fix this error."

---

## 🗺️ Future Scope

- Docker-based sandbox for stronger isolation
- Multi-dataset comparative analysis
- Real-time streaming of agent thoughts in UI
- Plugin architecture for custom analysis modules
- Integration with Canticles Technologies HRMS API
