"""
Mechanism-level attack families (frozen before any new control was written).

Variants of one mechanism (different encodings, channels or targets of the SAME technique) share a family, and a
family is never split across dev and held-out. Families, not variants, are the unit of independence.
"""

ATTACK_FAMILIES = {
    # 1 prompt injection
    "goal_override": ["PI-01", "PI-02", "PI-03", "PI-04", "PI-05", "PI-06", "PI-07"],
    "section_spoof": ["PS-01", "PS-02", "PS-03", "PS-04", "PS-05", "PS-06"],
    "header_injection": ["PH-01", "PH-02", "PH-03"],
    "cell_injection": ["PC-01", "PC-02", "PC-03", "PC-04", "PC-05"],
    "filename_injection": ["PF-01"],
    "planner_title_injection": ["PT-01", "PT-02"],
    # 2 unauthorized operations
    "invalid_claim": ["UO-01", "UO-02", "UO-03", "UO-04", "UO-05", "UO-06", "UO-07", "UO-08", "UO-16"],
    "direct_op_call": ["UO-09"],
    "forged_analysis_request": ["UO-10"],
    "capability_abuse": ["UO-11", "UO-12"],
    "executor_trust": ["UO-13", "UO-14"],
    "plan_size_limits": ["UO-15"],
    # 3 sensitive disclosure
    "disclosure_declared": ["SD-01", "SD-02", "SD-03", "SD-04", "SD-05", "SD-06", "SD-15"],
    "disclosure_undeclared": ["SD-07", "SD-08", "SD-09", "SD-10"],
    "secret_in_prompt_inputs": ["SD-11", "SD-12"],
    "identifier_in_file_name": ["SD-13"],
    "service_disclosure": ["SD-14", "SD-16"],
    # 4 approval tampering
    "plan_tamper": ["AP-01", "AP-02", "AP-03"],
    "dataset_swap": ["AP-04", "AP-05"],
    "decision_replay": ["AP-06", "AP-07", "AP-08"],
    "forged_binding": ["AP-09", "AP-10", "AP-11"],
    "service_decision_integrity": ["AP-12", "AP-13", "AP-14", "AP-15"],
    "decision_payload_tamper": ["AP-16", "AP-17"],
    # 5 cross-session and context
    "cross_run_state_injection": ["CS-02", "CS-09"],
    "context_carryover": ["CS-03", "CS-04", "CS-10"],
    "service_isolation": ["CS-06", "CS-08"],
    "thread_collision": ["CS-07"],
    "foreign_cleanup": ["CS-11"],
    # 6 fabricated or unsafe output
    "lying_analyst": ["FR-01", "FR-02"],
    "ungrounded_insight": ["FR-03", "FR-04", "FR-05"],
    "unsafe_insight_content": ["FR-06", "FR-07", "FR-08", "FR-09", "FR-10", "FR-14"],
    "semantic_fabrication": ["FR-11", "FR-15"],
    "forged_verifier_output": ["FR-12", "FR-13"],
    # 7 resource exhaustion
    "retry_loops": ["RX-01", "RX-02", "RX-12", "RX-13"],
    "oversized_reply": ["RX-03", "RX-04", "RX-09"],
    "result_cardinality": ["RX-05", "RX-06"],
    "concurrent_accounting": ["RX-07", "RX-08"],
    "oversized_inputs": ["RX-10", "RX-11", "RX-14"],
    # 8 forged messages
    "malformed_envelope": ["FM-01", "FM-02", "FM-04", "FM-05", "FM-06", "FM-07", "FM-14"],
    "duplicate_delivery": ["FM-03"],
    "model_reply_spoof": ["FM-08"],
    "checkpoint_forgery": ["FM-09", "FM-10", "FM-11", "FM-12", "FM-13"],
}

BENIGN_PREFIX_FAMILIES = (
    ("BN-G-sales", "golden_sales"), ("BN-G-attendance", "golden_attendance"), ("BN-G-student", "golden_student"),
    ("BN-G-ecommerce", "golden_ecommerce"),
    ("BN-Q-08", "benign_unusual_format"), ("BN-Q-09", "benign_unusual_format"), ("BN-Q-10", "benign_unusual_format"),
    ("BN-Q-11", "benign_unusual_format"), ("BN-Q-12", "benign_unusual_format"),
    ("BN-Q", "benign_instruction_like"), ("BN-C", "benign_unusual_columns"), ("BN-L", "benign_unusual_labels"),
    ("BN-S", "benign_sensitive_local"), ("BN-I", "benign_grounded_insights"), ("BN-V", "benign_service_flows"),
    ("BN-R", "benign_recovery"), ("BN-B", "benign_bounds"),
)

_BY_ID = {sid: fam for fam, ids in ATTACK_FAMILIES.items() for sid in ids}


def family_of(sid: str) -> str:
    if sid in _BY_ID:
        return _BY_ID[sid]
    for prefix, fam in BENIGN_PREFIX_FAMILIES:
        if sid.startswith(prefix):
            return fam
    raise KeyError(f"scenario {sid} has no family")
