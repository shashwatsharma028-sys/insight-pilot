# Security test corpus (P1, inert)

These are example inputs for future security tests (P2, P3, P8). In P1 they are **inert data**:

- no `.py` files, so pytest does not collect anything here;
- no test reads them yet;
- they are never executed.

Safety rules for this folder:

- **Markers only.** Payloads only ask for a harmless marker such as `CANARY-P1-…` to appear. They never ask for destructive actions or real secrets.
- **Non-resolving hosts.** Any host name uses the reserved `.invalid` top-level domain (RFC 2606), which never resolves.
- **Text-only code.** Code shapes are described in `.txt` files, not as runnable programs. The runnable probes for V1 already exist in `tests/v2/test_v1_security_baseline.py`, and P1 does not change them.
- **Synthetic data.** CSV files contain synthetic values only.

| File | Abuse case | Findings | Intended future test |
|---|---|---|---|
| `injection_cell_values.csv` | AC-01 | R-10 | `test_prompt_builder_quotes_dataset_content`, `test_injection_corpus_flagged_in_monitor_mode` |
| `injection_header_names.csv` | AC-01, AC-10 | R-10, R-13 | same, plus `test_render_helpers_escape_untrusted` |
| `lesson_poisoning_inputs.txt` | AC-02 | R-11 | `test_lesson_store_rejects_free_text_from_stderr` |
| `scanner_bypass_shapes.txt` | AC-04 | R-03 | `test_hardened_sandbox_contains_bypass_corpus` |
| `filename_cases.txt` | AC-06 | R-06, R-07, R-15 | `test_upload_name_traversal_rejected`, `test_notebook_setup_cell_is_literal_safe` |
| `budget_exhaustion_plan.json` | AC-07 | R-16 | `test_plan_capped_at_5_tasks` |
| `html_injection_strings.txt` | AC-10 | R-13, R-21, R-22 | `test_no_unescaped_interpolation_in_unsafe_html`, `test_pdf_paragraph_text_escaped` |

Do not add these files to `data/`: the demo datasets are pinned by SHA-256 in `tests/v2/golden/expected/`.
