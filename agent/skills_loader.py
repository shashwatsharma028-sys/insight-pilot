"""
Skills Loader
Loads agent skills (system prompts + responsibility definitions) from /skills.

How the skill system works:
- Each agent responsibility lives in its own markdown file under /skills.
- Files have YAML-style frontmatter (between --- markers) documenting the
  skill: its purpose, inputs, outputs, and governing rules. The loader
  STRIPS the frontmatter — only the body (the actual prompt text) is
  injected into the LLM, so documentation never pollutes prompts.
- Prompt-bearing skills (planner, code_generator, business_insights,
  followup_chat) are loaded at import time by their nodes.
- Documentation-only skills (executor, memory, reflection, chart_analysis)
  describe responsibilities implemented in code rather than prompts.
- Every load has a fallback: if a skill file is missing, the node's
  embedded default prompt is used, so the agent never breaks from a
  missing file. Files are cached after first read.
"""

import os
from functools import lru_cache

SKILLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills"
)


def _strip_frontmatter(text: str) -> str:
    """Remove a leading --- ... --- frontmatter block, if present."""
    if text.startswith("---\n"):
        parts = text.split("\n---\n", 1)
        if len(parts) == 2:
            return parts[1].lstrip("\n")
    return text


@lru_cache(maxsize=None)
def _read_skill_file(name: str) -> str | None:
    """Read and cache a skill file's body, or None if missing."""
    path = os.path.join(SKILLS_DIR, f"{name}.md")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return _strip_frontmatter(f.read())


def load_skill(name: str, fallback: str = "") -> str:
    """
    Load a skill's prompt body by name (e.g. "planner").

    Returns the skill file's body with frontmatter stripped. If the file
    is missing, returns the provided fallback (the node's embedded default)
    and prints a warning — the agent degrades gracefully, never crashes.
    """
    body = _read_skill_file(name)
    if body is None:
        if fallback:
            print(f"⚠️ Skill file missing: skills/{name}.md — using embedded default")
        return fallback
    return body


def skills_available() -> bool:
    """True if the skills directory exists with at least one skill file."""
    return os.path.isdir(SKILLS_DIR) and any(
        f.endswith(".md") for f in os.listdir(SKILLS_DIR)
    )
