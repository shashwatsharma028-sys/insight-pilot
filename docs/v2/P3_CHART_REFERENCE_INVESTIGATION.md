# P3.5b — golden chart mismatch investigation (GitHub Actions run #3)

**Run:** https://github.com/shashwatsharma028-sys/insight-pilot/actions/runs/45031896461 (commit `83596758`).

**Symptom:**
- All 28 numerical golden answers matched.
- The 5 tasks that draw no chart correctly drew none.
- All **7 chart pixel hashes differed** from the P3.5a references, and so did their sizes. For example, att_t1 was 1014×696 in the container against a reference of 1006×688.

## Cause (reproduced, not inferred)

The P3.5a chart references were rendered on the development host in a **contaminated environment**. `workload_compat.py` calls the V1 executor, which passes `os.environ` to the child process, so two host settings leaked into the rendering:

1. **`MATPLOTLIBRC=/etc/matplotlib`**, set by the host environment. That rc file contains `font.family: Inter, sans-serif, DejaVu Sans`, `text.hinting: no_hinting` and `figure.dpi: 200`. The V1 prelude overrides the dpi and the `seaborn-v0_8-darkgrid` style overrides the family, but `text.hinting: no_hinting` survived.
2. **109 system fonts.** The style's sans-serif list is `Arial, Liberation Sans, DejaVu Sans, …`. Arial is absent, so Matplotlib picked `/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf`.

The container runs with `--env-host=false` and has no system fonts. It therefore renders with Matplotlib's packaged defaults and its bundled `DejaVuSans.ttf`. The fixtures save with `bbox_inches="tight"`, so different glyph metrics change both the image size and the pixels.

## Evidence

All 12 hash-pinned golden scripts (the same text the container runs) were run with the lock-only venv (Matplotlib 3.11.1, Pillow 13.0.0, bundled FreeType 2.14.3) on the development host:

| Experiment | Environment | Result |
|---|---|---|
| A | host as-is: `MATPLOTLIBRC` set, system fonts visible | reproduces the **P3.5a references** exactly (7/7) |
| B | `MATPLOTLIBRC` removed, system fonts visible | matches **neither** set: both factors matter |
| C | `MATPLOTLIBRC` removed, system fonts hidden (private mount namespace) | reproduces the **container's run #3 hashes and sizes** exactly (7/7) |

Experiment C ran on a different machine from the GitHub runner, with the same wheels, and produced identical pixels. The rendering is deterministic and is **not** platform-dependent here.

**Render environment reported by the trusted epilogue:**

| Field | Hermetic (C, and container) | Leaky (A) |
|---|---|---|
| `matplotlibrc` | `mpl-data:matplotlibrc` | `/etc/matplotlib/matplotlibrc` |
| `rc_env` | `MPLBACKEND, MPLCONFIGDIR` | `MATPLOTLIBRC, MPLBACKEND, MPLCONFIGDIR` |
| `system_fonts` | 0 | 109 |
| `font_file` | `mpl-data:fonts/ttf/DejaVuSans.ttf` (sha256 `3fdf69ca…`) | `LiberationSans-Regular.ttf` (sha256 `4659bc0c…`) |
| `text.hinting` | `default` | `no_hinting` |

These are identical between A and C:
- Matplotlib 3.11.1, Pillow 13.0.0, FreeType 2.14.3 (bundled);
- the Agg backend;
- `figure.dpi` 120, `figure.figsize` 10×6, `savefig.dpi` `figure`.

## Correction (exact matching kept; nothing overwritten)

- **`scripts/p3/golden_fixtures.json`:**
  - `chart`: the P3.5a values, **preserved unchanged**, with a frozen hash in the tests and their provenance recorded. They are not used for acceptance.
  - `chart_hermetic`: new acceptance references, rendered under experiment C and reproduced by `scripts/p3/hermetic_reference.py`. They were corroborated independently by the container in run #3.
  - `render_environment`: the exact hermetic environment.
- **The epilogue** of every golden script now prints `RENDERENV`. `golden()` fails if any field differs from `render_environment`: a host rc file, `MATPLOTLIBRC`, visible system fonts, another font file, hinting, FreeType, Matplotlib or Pillow. It also fails unless the chart equals `chart_hermetic` exactly, with decoded-pixel SHA-256 and no tolerance. Numerical and chart results are reported separately.
- **`scripts/p3/hermetic_reference.py`** reproduces the references in a cleared environment. It refuses on the first non-hermetic render environment, and never writes the manifest.
- **The container's runtime check** already rejects unexpected environment variables. A regression case now proves that `MATPLOTLIBRC` would be rejected.

**What does not change:** the fixtures, datasets, numerical golden answers, V1 code and snapshots, the sandbox security options, and the image.

**Limitation:** the host-side P3.5a statement that Pillow 12.3.0 and 13.0.0 gave identical pixels still holds, but both runs were in the same contaminated environment. It is now superseded by the hermetic comparison.
