"""
Prompt-injection and sensitive-pattern SIGNALS (defence in depth; never an authorization mechanism).

normalize() builds a detection-only copy of a text (NFKC, invisible format characters removed, case-folded); the
original value is never modified or used for computation. Signals require structure, not single keywords: an
override needs a verb AND an object ("ignore ... previous instructions"), so ordinary analytical phrasing such as
"ignore rows with missing values" or a column named "system_prompt" raises nothing.
"""

import base64
import binascii
import re
import unicodedata

LINE_BREAKS = ("\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\U000e0000-\U000e007f]")
SECTION_MARKERS = ("PLANNING ATTEMPT", "USER GOAL", "DATASET PROFILE JSON", "CLAIM OPS JSON", "CLAIM ARGUMENT SCHEMA",
                   "CLAIM RULES", "CLAIM OP RESULTS", "CLAIM SEMANTICS", "MAX TASKS", "PREVIOUS PROBLEMS", "TASK ID",
                   "TASK", "DESCRIPTION", "ATTEMPT", "COLUMNS JSON", "CLAIMS JSON", "VERIFIED CLAIMS JSON",
                   "FEEDBACK FROM PREVIOUS ATTEMPTS", "SYSTEM", "ASSISTANT", "USER")

_OVERRIDE = re.compile(r"\b(ignore|disregard|forget|override|bypass|skip)\b[^.;\n]{0,40}?\b(previous|prior|above|"
                       r"earlier|all|any|your|the|these|those|system)\b[^.;\n]{0,30}?\b(instructions?|rules?|prompts?|"
                       r"directives?|guidelines?|polic(?:y|ies)|guardrails?)\b")
_ROLE = re.compile(r"\b(system prompt|developer mode|you are now|jailbreak|system override|new (?:rule|instruction)s?\s*:|"
                   r"act as (?:an? )?(?:admin|administrator|system|root|developer))")
_SYSTEM_TAG = re.compile(r"\b(system|assistant|developer)\s*:")
_EXFIL = re.compile(r"\b(send|email|post|upload|exfiltrate|export|leak|reveal|print|list|dump|include)\b[^.;\n]{0,40}?"
                    r"\b(every|all|each|full|complete)\b[^.;\n]{0,30}?(e-?mail|password|token|secret|api[ _-]?key|"
                    r"salary|salaries|account|credential)")
_SOLICIT = re.compile(r"\b(reply|send|enter|provide|share|confirm|type)\b[^.;\n]{0,30}?\b(password|passcode|api[ _-]?key|"
                      r"credentials?|token|pin code|login)\b")
_LINK = re.compile(r"(https?://|ftp://|www\.|javascript:)", re.I)
_MARKUP = re.compile(r"(<\s*/?\s*[a-zA-Z!][^>]*>|!\[[^\]]*\]\(|\[[^\]]*\]\([^)]*\))")
_B64 = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
CREDENTIAL = re.compile(r"(\b(?:sk|pk|rk)[-_](?:live|test|proj|cnry|[a-z]{2,8})?[-_]?[A-Za-z0-9]{16,}"
                        r"|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_\-]{30,}|\bgh[pousr]_[A-Za-z0-9]{30,}"
                        r"|\bxox[abpr]-[A-Za-z0-9\-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
                        r"|(?i:\b(?:api[_-]?key|secret|password|passwd|bearer)\b\s*[:=]\s*\S{8,}))")

SIGNAL_CODES = ("injection_override", "injection_role", "injection_section_spoof", "injection_exfil_directive",
                "injection_encoded", "injection_hidden_chars", "credential_solicitation", "link_or_markup",
                "email_address", "credential_pattern")


def normalize(text: str) -> str:
    """Detection-only normal form: NFKC, invisible/bidi/tag characters removed, case-folded."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", str(text))).casefold()


def has_line_break(text: str) -> bool:
    return any(b in text for b in LINE_BREAKS)


def _spoofs_section(text: str) -> bool:
    if not has_line_break(text):
        return False
    parts = re.split("|".join(re.escape(b) for b in LINE_BREAKS), text)
    return any(re.match(rf"\s*({'|'.join(re.escape(m) for m in SECTION_MARKERS)})\s*:", p.upper()) for p in parts[1:])


def _decoded_payloads(text: str):
    for m in _B64.finditer(text):
        try:
            raw = base64.b64decode(m.group(0) + "=" * (-len(m.group(0)) % 4), validate=True)
            s = raw.decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
        if s.isprintable() and len(s) >= 12:
            yield s


def signals(text, location="goal") -> list:
    """Signal codes for one untrusted text (sorted, de-duplicated). Links and markup are signals in data and model
    output, not in the user's own goal, where a reference URL is ordinary."""
    if not isinstance(text, str) or not text:
        return []
    found = set()
    n = normalize(text)
    if _INVISIBLE.search(text):
        found.add("injection_hidden_chars")
    if _OVERRIDE.search(n):
        found.add("injection_override")
    if _ROLE.search(n) or _SYSTEM_TAG.search(n):
        found.add("injection_role")
    if _spoofs_section(text):
        found.add("injection_section_spoof")
    if _EXFIL.search(n):
        found.add("injection_exfil_directive")
    if _SOLICIT.search(n):
        found.add("credential_solicitation")
    for dec in _decoded_payloads(text):
        d = normalize(dec)
        if _OVERRIDE.search(d) or _ROLE.search(d) or _EXFIL.search(d):
            found.add("injection_encoded")
    if location != "goal" and (_LINK.search(text) or _MARKUP.search(text)):
        found.add("link_or_markup")
    if EMAIL.search(text):
        found.add("email_address")
    if CREDENTIAL.search(text):
        found.add("credential_pattern")
    return sorted(found)


def is_injection(codes) -> bool:
    return any(c.startswith("injection_") or c == "credential_solicitation" for c in codes)


def sensitive_label(text) -> bool:
    """A data label that must not leave the application by heuristic (emails, credential-shaped strings). This is a
    supplement to the explicit sensitive-column policy, NOT comprehensive PII detection."""
    return isinstance(text, str) and bool(EMAIL.search(text) or CREDENTIAL.search(text))


def quarantine_label(text) -> bool:
    """A data label that carries instruction-like, hidden or markup content: it is pseudonymised before any model
    sees it, so indirect injection through cell values is not delivered as text."""
    if not isinstance(text, str):
        return False
    codes = signals(text, "dataset_value")
    return bool(codes) and (is_injection(codes) or "link_or_markup" in codes)
