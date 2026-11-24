# P6 live evidence (provider: Gemini via the OpenAI-compatible endpoint; key injected by the session proxy)

| Folder | What it is | Valid benchmark data? |
|---|---|---|
| `pilot1_unavailable_model/` | gemini-2.5-flash-lite: every generation returned HTTP 404 ("no longer available to new users"). Failed attempts charged the worst case. | **No** (no successful call) |
| `pilot2/` | gemini-3.5-flash-lite pilot, student dataset, 1 run per architecture; all calls provider-reported. | Yes (pilot only; n=1) |
| `baseline_attempt1_INVALID/` | First full baseline attempt. After the first runs, 91 of 133 calls failed within about 0.2 s (consistent with per-minute rate limiting; HTTP status not recorded then). Each was charged the worst case ($0.472 total), the ledger cap was reached, and the run stopped. | **No**: results mix real answers with provider failures |

Ledger after these runs: $0.495563 in total, of which $0.023445 is provider-reported usage of successful calls, $0.472118 is conservative worst-case charges for failed attempts (not expected to be billed), and the remainder is the small probes. Estimated cost = provider-reported tokens × confirmed list prices; it is not billing.
