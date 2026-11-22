# P3.6 mutation checks (stages 1–3)

Each mutant was applied to a detached worktree of commit `7dd7dbbb`, and `tests/v2/p36/` was then run against it. A mutant is detected ("killed") when at least one test fails.

| # | Deliberate defect | Result | First failing test |
|---|---|---|---|
| A1 | network not disabled | killed | `test_preflight_checks_host_image_and_three_trusted_self_tests` |
| A2 | capabilities kept | killed | `test_preflight_checks_host_image_and_three_trusted_self_tests` |
| A3 | writable root filesystem | killed | `test_preflight_checks_host_image_and_three_trusted_self_tests` |
| A4 | host name services mounted | killed | `test_preflight_checks_host_image_and_three_trusted_self_tests` |
| A5 | comma allowed in mount source (option injection) | killed | `test_unsafe_values_are_refused_before_any_argv_exists[inputs_dir-/tmp/x,destinat` |
| A6 | any directory may be mounted | killed | `test_unsafe_values_are_refused_before_any_argv_exists[inputs_dir-/tmp/x/runs/fff` |
| A7 | image by tag accepted | killed | `test_the_podman_binary_must_be_trustworthy` |
| A8 | argv grammar allows spaces | killed | `test_validate_argv_rejects_shell_and_control_characters[a b]` |
| A9 | memory not from Limits | killed | `test_the_run_argv_is_exactly_the_reviewed_command` |
| D1 | identity digest not compared with the pin | killed | `test_rejected_datasets_never_reach_a_container[identity0-approved SHA-256]` |
| D2 | staged size not compared | killed | `test_identity_staging_refuses_and_leaves_no_copy[<lambda>2]` |
| R1 | timeout reported as a code error | killed | `test_code_errors_timeouts_and_oom_are_ordinary_outcomes[mode1-timed_out]` |
| R2 | failed run keeps its chart | killed | `test_every_frame_status_maps_to_exactly_one_outcome[kw1-code_error]` |
| R3 | frame run id not checked | killed | `test_a_frame_for_another_run_is_a_protocol_error` |
| C1 | dispatcher accepts any chart path | killed | `test_dispatcher_accepts_only_the_store_published_chart` |
| C2 | dispatcher accepts a symlinked chart | killed | `test_dispatcher_accepts_only_the_store_published_chart` |
| K1 | run-time attestation not verified | killed | `test_a_weakened_container_at_run_time_is_refused` |
| K2 | preflight attestation not verified | killed | `test_preflight_refuses_and_stays_unusable[scenario6-attestation does not match]` |
| K3 | OOM self-test not required | killed | `test_preflight_checks_host_image_and_three_trusted_self_tests` |
| K4 | image entrypoint not checked | killed | `test_preflight_refuses_and_stays_unusable[scenario1-trusted supervisor]` |
| K5 | container removal not verified | killed | `test_a_container_that_survives_removal_is_an_error_after_cleanup_was_attempted` |
| K6 | deadline does not kill | killed | `test_the_host_deadline_kills_and_removes_the_container` |
| K7 | execute without preflight | killed | `test_preflight_refuses_and_stays_unusable[scenario0-not present]` |
| K8 | podman binary not checked | killed | `test_the_podman_binary_must_be_trustworthy` |
| K9 | host environment passed to podman | killed | `test_only_the_resolved_podman_binary_is_ever_started` |
| K10 | workspace not removed | killed | `test_execution_stages_inputs_by_identity_and_cleans_up_everything` |
| K11 | dataset staged without identity check | killed | `test_rejected_datasets_never_reach_a_container[identity0-approved SHA-256]` |
| B1 | validation entry allows root | killed | `test_install_for_validation_refuses_root_doubles_subclasses_and_missing_opt_in` |
| B2 | validation entry accepts doubles | killed | `test_install_for_validation_refuses_root_doubles_subclasses_and_missing_opt_in` |
| B3 | opt-in variable not required | killed | `test_install_for_validation_refuses_root_doubles_subclasses_and_missing_opt_in` |
| S1 | frame despite surviving processes | killed | `test_malformed_or_missing_frames_fail_closed_and_still_clean_up[behaviour5]` |
| S2 | supervisor accepts extra flags | killed | `test_any_other_supervisor_argv_exits_78_without_a_frame_or_a_child[<lambda>10]` |
| S3 | child started before PR_SET_DUMPABLE | killed | `test_success_produces_one_valid_frame_after_every_safety_step` |
| S4 | stage A follows symlinks | killed | `test_unsafe_or_invalid_chart_entries_are_rejected_by_reason_code[symlink-x-symli` |
| S5 | lazy prelude also rewrites user code | killed | `test_runner_composition_equals_the_v1_and_p41_wrappers_byte_for_byte[all_indente` |
| S6 | prelude hash not checked | killed | `test_prelude_file_is_pinned_and_tampering_is_refused` |

**Final: 36/36 detected on the first run.**
