"""
InsightPilot V2 components.

Nothing in the V1 application (agent/, sandbox/, utils/, ui/) imports this
package. `streamlit run ui/app.py` never loads it.

Status (P3.6): v2.execution holds the pure contracts (P3.1), the launcher,
bootstrap, binding and dispatcher (P3.2), dataset authorisation, workspace
and lease (P3.3), the frame, artifact and chart-store code (P3.4), the inert
image definition (P3.5a), and the P3.6 container-runtime code. Production
hardened execution is still refused: no container backend has been
validated. See docs/v2/P3_SECURE_EXECUTION_DESIGN.md.
"""
