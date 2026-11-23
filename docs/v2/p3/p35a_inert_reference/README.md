# P3.5a inert image definition: frozen reference

**Status:** superseded by the P3.6 supervisor image (P3.6-4A). Kept for comparison only. Nothing
builds, stages or runs these files.

P3.6-4A replaced the live image definition in `v2/execution/image/` with the P3.6 image: the entrypoint
is the PID-1 supervisor and `/opt/ip` holds the trusted runtime. The last inert definition is kept
here byte for byte, so its properties stay tested by `tests/v2/p3/test_image_p35a.py`.

| File | Source (commit `41536829957c27a053775989aa619d10cd114406`) | SHA-256 |
|---|---|---|
| `Containerfile` | `v2/execution/image/Containerfile` | `a49f1aadae1934ac1ed19094ccef7a40954d853b3134e59a0e25c0651536fc79` |
| `image-expectations.json` | `v2/execution/image/image-expectations.json` | `821cc5a0564911b79f7b281425f8d2037cc4ef5b104a4c1948f36911f7f0caf8` |
| `inert_entrypoint.py` | `v2/execution/image/inert_entrypoint.py` (still present there, unchanged) | `919e9eab62c1e768df6da24d23e44f0b46f14c7baee13ca01777be8b302c9586` |

`tests/v2/p36/test_image_integration_p364a.py` checks that each file equals the Git blob at that
commit and that these checksums match.

## How this relates to the CI evidence

- The expectations here carry the reconciled 1 GiB memory value (P3.6-0, R2).
- GitHub Actions run #4 (`docs/v2/p3/ci_run4/`) built and verified the inert image from the
  probe-branch definition at commit `4cd107e0`, which used 256 MiB. That historical evidence is
  unchanged and is not re-labelled by this copy.
- Neither the inert image nor the P3.6 image proves runtime isolation. See
  `docs/v2/P364A_IMPLEMENTATION.md`.
