# InsightPilot: Streamlit HTML rendering audit (P1)

**Target:** `ui/app.py` at `feature/insightpilot-v2` @ `6152da3a`. This is the only module that renders; `agent/`, `sandbox/` and `utils/` contain no Streamlit rendering calls.

**Finding:** [R-13](risk_register.md#r-13-html-injection-in-the-streamlit-ui) (HTML injection); related R-22 (Markdown in status messages) and R-14 (tracebacks).

## Method and counts

The counts below come from the code, not from earlier reports.

- **Inventory.** A read-only AST pass over `ui/app.py` lists every call to a Streamlit rendering or widget method: `markdown`, `info`, `error`, `exception`, `expander`, `progress`, `image`, `button`, `download_button`, `text_input`, `text_area`, `toggle`, `slider`, `spinner`, `file_uploader`, `set_page_config`, `html`. It records the receiver (for example `st`, `k1`, `status_placeholder`), whether `unsafe_allow_html=True` is set, and every expression interpolated into the first argument or into `text=` / `caption=` / `label=`.
- **Classification.** Each interpolated expression was then traced by hand to its source.
- **Not performed:** browser execution of payloads.

| Measure | Count |
|---|---|
| Rendering-related calls in `ui/app.py` | **111** |
| `unsafe_allow_html=True` occurrences (`grep -c`) | **62**, all on `.markdown(` calls (47 `st.markdown`, 15 on column objects `k1`–`k4`) |
| of which fully static strings | **20** |
| of which interpolating values | **42** |
| interpolating values that are code constants or numbers only (class C) | 23 |
| interpolating untrusted text **with** escaping (class E) | 4 |
| interpolating untrusted text **without** escaping (class U) | **15** |
| `st.html`, `components.html`, `st.write` | 0 |
| Other rendering calls (Markdown-rendered or widgets), listed below | 49 |

The earlier figure of "62" was correct as a count of `unsafe_allow_html=True` occurrences. It does **not** mean 62 vulnerable sites: 15 carry untrusted, unescaped text.

### Classes

| Class | Meaning |
|---|---|
| **U** | User-controlled, dataset-derived or LLM- or sandbox-generated text inserted into HTML without escaping. Injection is possible. |
| **E** | Untrusted text inserted only after `html.escape` (`df_preview_html`, `md_inline`). |
| **C** | Only code constants (icons, theme tokens, fixed labels, skill names) or numbers. |
| **S** | Static string. |

## Root causes (helpers)

| Helper | Lines | Behaviour |
|---|---|---|
| `md_inline(text)` | `ui/app.py:636-644` | ✅ `html.escape`, then re-introduces `<strong>`, `<em>`, `<code>` from Markdown markers. The regexes act on already-escaped text, so no raw tags are introduced. |
| `df_preview_html(df)` | `:619-633` | ✅ escapes headers and cells. |
| `clean_detail(text)` | `:647-652` | ⚠️ strips file paths only; **does not escape**. |
| `chat_thread_html(state)` | `:711-721` | ❌ inserts `t["message"]` raw, for both user and LLM turns (`:719`). |
| `agent_activity_html(state)` | `:683-708` | ❌ inserts `clean_detail(detail)[:96]` raw (`:701, :706`). |
| `page_head(title, subtitle, …)` | `:606-616` | ❌ inserts its arguments raw. Callers pass the dataset name (`:909`, `:1123`), which for uploads is derived from the file name (`:807`). |
| `kpi(label, value, sub, …)` | `:595-603` | Inserts its arguments raw. Every caller passes numbers or constants (class C), so it's safe today but fragile. |

## Data sources that reach class-U sites

| Source | How it is produced | Reaches |
|---|---|---|
| Upload file name | `uploaded.name` (`:803-807`): unsanitised, may contain `<`, `>`, `"` | `:784`, `:607` (via `:909`, `:1123`) |
| CSV column names | `data_ingestion.py:26, 91, 104-110` (missing-value map, outlier labels, recommendations) | `:1000`, `:1270`, `:1277`, `:1285` |
| LLM plan fields | `planner.py:73-92` (only defaults; `title`, `analysis_type`, `priority` not validated) | `:1009`, `:1161`, `:1238` |
| LLM interpretation fields | `interpreter.py:93-101` (`assumptions` list) | `:1175` |
| Chat | user input (`:1216`, `:1369`) and LLM reply (`conversation.py:80-102`) | `:1210`, `:1359` |
| Execution log | `executor.py:32, 85, 108` (task titles, stdout/stderr excerpts), `conversation.py:37` (user text), node error strings | `:1194`, `:1293`, `:1355` |

## Inventory: all 62 `unsafe_allow_html=True` sites

Line numbers are those of the `.markdown(` call; the interpolated f-string may start on the next lines. "module level" means top-level page code.

| # | Line | Function | Class | Interpolated values | Source of text | Finding |
|---|---|---|---|---|---|---|
| 1 | `ui/app.py:141` | inject_css | C: constant/numeric | `t['bg']; t['surface']; t['surface_alt']; t['border']; t['border_strong']; t['text']; t['text_muted']; t['text_` | Code constants / numbers | - |
| 2 | `ui/app.py:607` | page_head (page_head()) | **U: untrusted, unescaped** | `subtitle` = dataset name (from upload file name) via callers :909, :1123 | File name | R-13 |
| 3 | `ui/app.py:748` | module level | C: constant/numeric | `icon('logo', 18)` | Code constants / numbers | - |
| 4 | `ui/app.py:772` | module level | S: static | static string | Code | - |
| 5 | `ui/app.py:784` | module level (sidebar dataset card) | **U: untrusted, unescaped** | `os.path.basename(uploaded_path)` | File name | R-13 |
| 6 | `ui/app.py:796` | module level | S: static | static string | Code | - |
| 7 | `ui/app.py:823` | module level | S: static | static string | Code | - |
| 8 | `ui/app.py:831` | module level | S: static | static string | Code | - |
| 9 | `ui/app.py:845` | module level | S: static | static string | Code | - |
| 10 | `ui/app.py:851` | module level | S: static | static string | Code | - |
| 11 | `ui/app.py:870` | module level | C: constant/numeric | `icon('upload', 26)` | Code constants / numbers | - |
| 12 | `ui/app.py:881` | module level | S: static | static string | Code | - |
| 13 | `ui/app.py:895` | module level | C: constant/numeric | `icon(ic, 18); title; body` | Code constants / numbers | - |
| 14 | `ui/app.py:914` | module level | C: constant/numeric | `preview.shape[0]; ARG:kpi('Rows', f'{preview.shape[0]:,}', 'Total records', 'rows')` | Code constants / numbers | - |
| 15 | `ui/app.py:915` | module level | C: constant/numeric | `preview.shape[1]; ARG:kpi('Columns', f'{preview.shape[1]}', 'Total columns', 'columns')` | Code constants / numbers | - |
| 16 | `ui/app.py:916` | module level | C: constant/numeric | `_missing; ARG:kpi('Missing values', f'{_missing:,}', 'Across all cells', 'alert', tone='tone-warn' if _m` | Code constants / numbers | - |
| 17 | `ui/app.py:919` | module level | S: static | static string | Code | - |
| 18 | `ui/app.py:921` | module level | E: escaped | `df_preview_html` (`html.escape` on headers and cells, `:627-630`) | Dataset / LLM | - |
| 19 | `ui/app.py:987` | module level | C: constant/numeric | `q.get('total_rows', 0); ARG:kpi('Rows', f"{q.get('total_rows', 0):,}", 'Total records', 'rows')` | Code constants / numbers | - |
| 20 | `ui/app.py:989` | module level | C: constant/numeric | `q.get('total_columns', 0); ARG:kpi('Columns', f"{q.get('total_columns', 0)}", 'Total columns', 'columns')` | Code constants / numbers | - |
| 21 | `ui/app.py:991` | module level | C: constant/numeric | `q.get('duplicate_rows', 0); ARG:kpi('Duplicates', f"{q.get('duplicate_rows', 0):,}", 'Repeated rows', 'alert',` | Code constants / numbers | - |
| 22 | `ui/app.py:993` | module level | C: constant/numeric | `q.get('quality_score', 0); ARG:kpi('Quality score', f"{q.get('quality_score', 0)}", 'out of 100', 'quality', t` | Code constants / numbers | - |
| 23 | `ui/app.py:1000` | module level (approval page: quality notes) | **U: untrusted, unescaped** | `r` = recommendations containing CSV column names (`data_ingestion.py:104-110`) | Dataset | R-13 |
| 24 | `ui/app.py:1002` | module level | S: static | static string | Code | - |
| 25 | `ui/app.py:1009` | module level (approval page: task tags) | **U: untrusted, unescaped** | `task['analysis_type']`, `task['priority']` raw; description escaped by `md_inline` | LLM | R-13 |
| 26 | `ui/app.py:1129` | module level | C: constant/numeric | `q.get('total_rows', 0); ARG:kpi('Rows analyzed', f"{q.get('total_rows', 0):,}", 'Total records', 'rows')` | Code constants / numbers | - |
| 27 | `ui/app.py:1131` | module level | C: constant/numeric | `q.get('total_columns', 0); ARG:kpi('Columns analyzed', f"{q.get('total_columns', 0)}", 'Total columns', 'colum` | Code constants / numbers | - |
| 28 | `ui/app.py:1133` | module level | C: constant/numeric | `len(completed); len(failed); ARG:kpi('Tasks completed', f'{len(completed)}', f'{len(failed)} failed' if failed` | Code constants / numbers | - |
| 29 | `ui/app.py:1136` | module level | C: constant/numeric | `q.get('quality_score', 0); ARG:kpi('Quality score', f"{q.get('quality_score', 0)}", 'out of 100', 'quality', t` | Code constants / numbers | - |
| 30 | `ui/app.py:1142` | module level | S: static | static string | Code | - |
| 31 | `ui/app.py:1147` | module level | E: escaped | `md_inline(ins)` (`html.escape` then bold/italic/code) | Dataset / LLM | - |
| 32 | `ui/app.py:1151` | module level | S: static | static string | Code | - |
| 33 | `ui/app.py:1154` | module level | S: static | static string | Code | - |
| 34 | `ui/app.py:1161` | module level (Overview: task tags) | **U: untrusted, unescaped** | `task['analysis_type']` | LLM | R-13 |
| 35 | `ui/app.py:1171` | module level | E: escaped | `md_inline(task['interpretation'])` | Dataset / LLM | - |
| 36 | `ui/app.py:1175` | module level (Overview: assumptions) | **U: untrusted, unescaped** | `'; '.join(task['assumptions'])` | LLM | R-13 |
| 37 | `ui/app.py:1183` | module level | C: constant/numeric | `icon('alert', 14); len(failed)` | Code constants / numbers | - |
| 38 | `ui/app.py:1193` | module level | S: static | static string | Code | - |
| 39 | `ui/app.py:1194` | module level (Overview: activity timeline) | **U: untrusted, unescaped** | `agent_activity_html` → log `detail` (sandbox stdout/stderr excerpts, error text) `:701-706` | Sandbox / LLM | R-13 |
| 40 | `ui/app.py:1200` | module level | C: constant/numeric | `retries; len(state.get('chart_paths', [])); len(state.get('execution_log', []))` | Code constants / numbers | - |
| 41 | `ui/app.py:1210` | module level (Overview: chat) | **U: untrusted, unescaped** | `chat_thread_html` → `t['message']` raw `:719` | User + LLM | R-13 |
| 42 | `ui/app.py:1227` | module level | E: escaped | `md_inline(ins)` | Dataset / LLM | - |
| 43 | `ui/app.py:1230` | module level | S: static | static string | Code | - |
| 44 | `ui/app.py:1233` | module level | S: static | static string | Code | - |
| 45 | `ui/app.py:1238` | module level (Insights: confidence cards) | **U: untrusted, unescaped** | `task['title']` | LLM | R-13 |
| 46 | `ui/app.py:1253` | module level | C: constant/numeric | `q.get('quality_score', 0); ARG:kpi('Quality score', f"{q.get('quality_score', 0)}", 'out of 100', 'quality', t` | Code constants / numbers | - |
| 47 | `ui/app.py:1255` | module level | C: constant/numeric | `q.get('duplicate_rows', 0); ARG:kpi('Duplicate rows', f"{q.get('duplicate_rows', 0):,}", 'Exact repeats', 'ale` | Code constants / numbers | - |
| 48 | `ui/app.py:1257` | module level | C: constant/numeric | `missing_total; len(missing_map); ARG:kpi('Missing values', f'{missing_total:,}', f'across {len(missing_map)} c` | Code constants / numbers | - |
| 49 | `ui/app.py:1260` | module level | C: constant/numeric | `len(q.get('outlier_columns') or []); ARG:kpi('Outlier columns', f"{len(q.get('outlier_columns') or [])}", 'Fla` | Code constants / numbers | - |
| 50 | `ui/app.py:1263` | module level | S: static | static string | Code | - |
| 51 | `ui/app.py:1270` | module level (Data Quality: missing values) | **U: untrusted, unescaped** | `col` = CSV column names `:1267` | Dataset | R-13 |
| 52 | `ui/app.py:1277` | module level (Data Quality: outliers) | **U: untrusted, unescaped** | `o` = column-name labels `:1275` | Dataset | R-13 |
| 53 | `ui/app.py:1283` | module level | S: static | static string | Code | - |
| 54 | `ui/app.py:1285` | module level (Data Quality: recommendations) | **U: untrusted, unescaped** | `r` (column names) `:1284` | Dataset | R-13 |
| 55 | `ui/app.py:1293` | module level (Agent Activity: timeline) | **U: untrusted, unescaped** | `agent_activity_html(limit=20)` → log `detail` | Sandbox / LLM | R-13 |
| 56 | `ui/app.py:1299` | module level | C: constant/numeric | `_cm2['tokens']; _cm2['max_tokens']; 'warn' if _warn2 else ''; min(_cm2['pct'], 1) * 100; _cm2['pct']; ' — olde` | Code constants / numbers | - |
| 57 | `ui/app.py:1315` | module level | C: constant/numeric | `retries; len(completed); len(failed); _tm2['total_tokens']; _tm2['estimated_cost_usd']; _tm2['calls']; by_skil` | Code constants / numbers | - |
| 58 | `ui/app.py:1355` | module level (Execution Log) | **U: untrusted, unescaped** | `node`, `action` (task titles; user chat text first 60 chars `conversation.py:37`), `detail` (sandbox output) `:1343-1352` | User + LLM + Sandbox | R-13 |
| 59 | `ui/app.py:1359` | module level (Follow-up Chat page) | **U: untrusted, unescaped** | `chat_thread_html` | User + LLM | R-13 |
| 60 | `ui/app.py:1366` | module level | S: static | static string | Code | - |
| 61 | `ui/app.py:1402` | module level | S: static | static string | Code | - |
| 62 | `ui/app.py:1412` | module level | S: static | static string | Code | - |

## Markdown-rendered and widget calls without `unsafe_allow_html`

Streamlit escapes raw HTML in these, but **Markdown is still interpreted** in `st.info`, `st.error`, expander labels and progress text. Images and links in Markdown are therefore relevant even though HTML tags are not.

| Call | Count | Lines |
|---|---|---|
| `button` | 12 | :762, :813, :835, :840, :847, :855, :862, :923, :1026, :1107, :1219, :1373 |
| `download_button` | 3 | :1384, :1391, :1398 |
| `error` | 2 | :973, :1103 |
| `exception` | 2 | :974, :1104 |
| `expander` | 6 | :810, :825, :830, :998, :1008, :1160 |
| `file_uploader` | 1 | :801 |
| `image` | 2 | :1180, :1410 |
| `info` | 5 | :956, :1066, :1071, :1084, :1094 |
| `progress` | 7 | :942, :953, :963, :968, :1038, :1090, :1096 |
| `set_page_config` | 1 | :28 |
| `slider` | 1 | :826 |
| `spinner` | 3 | :728, :945, :1041 |
| `text_area` | 1 | :1018 |
| `text_input` | 2 | :1216, :1369 |
| `toggle` | 1 | :824 |

Notable among them:

| Site | Content | Class | Finding |
|---|---|---|---|
| `status_placeholder.info` `:956` | `**{node_name}:** {last_log.get('detail')}`, where log detail includes sandbox stdout/stderr excerpts | Markdown with untrusted text | R-22 (Plausible: remote image through Markdown) |
| `status_placeholder.info` `:1066, :1071, :1084` | `task['title']` (LLM) | Markdown with LLM text | R-22 (Low) |
| `st.expander` `:1008, :1160` | `task['title']` (LLM) as label | Markdown label | R-22 (Low) |
| `st.error` / `st.exception` `:973-974, :1103-1104` | `str(e)` and full traceback | Information disclosure | R-14 |
| `st.image` `:1180, :1410` | chart path from executor (spoofable through stdout marker, checked by `os.path.exists`) | Local file display | R-20 |
| `st.download_button` `:1384-1400` | report/notebook files from session state | Exports | R-14, R-15 |

## Injection risk assessment

- **Confirmed (code):** untrusted text reaches raw HTML at 15 sites. Header names, file names, LLM fields, chat text and sandbox output can therefore insert arbitrary markup into the page.
- **Not established: script execution.** Streamlit's frontend bundle includes DOMPurify, but whether `st.markdown(..., unsafe_allow_html=True)` content passes through it was not determined from the minified bundle. Even with sanitisation, ordinary HTML usually permits:
  - **remote images** (`<img src="https://…">`), a beacon or data-exfiltration channel if an LLM is induced to embed data in the URL;
  - **links and phishing text** styled as the app's own UI;
  - **layout overlays and spoofing** through inline styles.
- **Severity:** Medium for the single-user V1 (an attacker needs a crafted dataset or model manipulation). It is higher if the app is shared (R-18), or if chat or log content from one session were ever shown to another.

## Recommended fixes (P2)

1. Escape at the boundary: wrap **every** interpolated value in an `unsafe_allow_html` call with `html.escape` (or `md_inline` where bold/italic is wanted). This includes `chat_thread_html`, `agent_activity_html`, `page_head`, Data Quality rows, task fields and the Execution Log.
2. Make `kpi()` and `page_head()` escape their arguments internally, so future callers are safe by default.
3. Escape Markdown control characters (or use plain-text rendering) in `st.info`, expander labels and progress text that include LLM or sandbox text.
4. Replace `st.exception(e)` with a generic message plus a local log entry (R-14).
5. Validate plan fields (`analysis_type` allowlist, integer `priority`, title length) in the P2 plan schema.

## Acceptance tests (to implement in P2)

| Test | Checks |
|---|---|
| `test_no_unescaped_interpolation_in_unsafe_html` | An AST lint over `ui/app.py`: every `FormattedValue` inside a call with `unsafe_allow_html=True` is wrapped in an allow-listed escaping helper or proven numeric or constant. It must report 0 class-U sites. |
| `test_render_helpers_escape_untrusted` | Each helper given the strings in `tests/security/corpus/html_injection_strings.txt` returns text with no unescaped `<`, `>` or `"`. |
| `test_chat_and_log_render_escaped_in_apptest` | An AppTest run with a scripted LLM whose reply and sandbox output contain the corpus strings; the rendered Markdown element values contain only escaped entities. |
| `test_status_text_markdown_escaped` | No `![` or `](` survives in `.info` text built from untrusted content. |
| `test_ui_error_shows_no_traceback` | An exception during a run renders no `st.exception` element. |
| Browser check (needs approved tooling) | In a real browser on Streamlit 1.59.2, a corpus payload creates no element and no network request. |

## Reproducing the counts

These read-only commands count the occurrences:

```bash
grep -c 'unsafe_allow_html=True' ui/app.py
```

```bash
grep -c 'st\.markdown(' ui/app.py
```

The full inventory came from a throwaway AST script run in the session scratchpad; it is not committed, because P1 adds no executable code. P2's lint test (`test_no_unescaped_interpolation_in_unsafe_html`) will make the inventory a permanent, executable check.
