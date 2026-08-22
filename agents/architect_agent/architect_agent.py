"""
architect_agent.py — Architect Agent standalone module
Receives a task with acceptance_criteria set, calls IBM watsonx to produce
a plan and scoped_files list, validates the output, and returns the
updated task dict.

Usage (called from pipeline.py):
    from agents.architect_agent.architect_agent import run_architect_agent
    task = run_architect_agent(task, repo_path="/path/to/sample_repo")

CLI (standalone test):
    python agents/architect_agent/architect_agent.py --task task.json --repo sample_repo/
"""
import argparse
import json
import os
import re
from datetime import datetime, timezone

try:
    from dotenv import load_dotenv
    _ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
    load_dotenv(_ENV_PATH)
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# watsonx defaults — plain string literals so _call_watsonx() fallbacks are
# never stale import-time values. All env vars are read inside _call_watsonx().
# ──────────────────────────────────────────────────────────────────────────────
_WATSONX_URL_DEFAULT = "https://us-south.ml.cloud.ibm.com/ml/v1/text/generation?version=2023-05-29"
_WATSONX_MODEL_DEFAULT = "ibm/granite-13b-instruct-v2"

_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "system_prompt.md")

# Dirs/files to skip when building the repo file listing
_SKIP_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "env", "node_modules",
    ".tox", "dist", "build", "*.egg-info", ".mypy_cache", ".pytest_cache",
}
_SKIP_EXTENSIONS = {".pyc", ".pyo", ".pyd", ".so", ".dylib", ".dll"}

# Fix 8: only send these file types to the LLM — keeps the listing focused
# and prevents ballooning context size on large repos like FlaskBB (~400 files)
_LISTING_RELEVANT_EXTS = {".py", ".html", ".jinja2", ".cfg", ".ini", ".toml", ".md"}
_LISTING_MAX_FILES = 150


# ──────────────────────────────────────────────────────────────────────────────
# Repo file listing
# ──────────────────────────────────────────────────────────────────────────────

def get_repo_file_listing(repo_path: str) -> list:
    """
    Walk repo_path and return all file paths relative to repo_path.
    Excludes .git, __pycache__, venv, node_modules, compiled artifacts.
    """
    listing = []
    repo_path = os.path.abspath(repo_path)
    for root, dirs, files in os.walk(repo_path):
        # Prune skip dirs in-place so os.walk doesn't descend into them
        dirs[:] = [
            d for d in dirs
            if d not in _SKIP_DIRS and not d.endswith(".egg-info")
        ]
        for fname in files:
            _, ext = os.path.splitext(fname)
            if ext in _SKIP_EXTENSIONS:
                continue
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, repo_path).replace("\\", "/")
            listing.append(rel_path)
    return sorted(listing)


# ──────────────────────────────────────────────────────────────────────────────
# LLM helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_system_prompt() -> str:
    with open(_PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _call_watsonx(prompt: str) -> str:
    """
    POST to IBM watsonx.ai text generation endpoint.
    Returns raw generated text. Raises RuntimeError on failure.

    All config is read at call time so .env loaded after import is picked up.
    """
    import urllib.request

    # All four values read at call time — no stale module-level constants used
    api_key    = os.environ.get("BOB_API_KEY", "")
    project_id = os.environ.get("WATSONX_PROJECT_ID", "")
    model_id   = os.environ.get("WATSONX_MODEL_ID", _WATSONX_MODEL_DEFAULT)
    url        = os.environ.get("WATSONX_URL",       _WATSONX_URL_DEFAULT)

    if not api_key:
        raise RuntimeError("BOB_API_KEY is not set. Add it to your .env file.")
    if not project_id:
        raise RuntimeError("WATSONX_PROJECT_ID is not set. Add it to your .env file.")

    payload = json.dumps({
        "model_id": model_id,
        "input": prompt,
        "parameters": {
            "decoding_method": "greedy",
            "max_new_tokens": 1000,
            "min_new_tokens": 50,
            "stop_sequences": [],
            "repetition_penalty": 1.05,
        },
        "project_id": project_id,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status != 200:
            raise RuntimeError(f"watsonx API returned HTTP {resp.status}")
        result = json.loads(resp.read().decode("utf-8"))

    generated = result.get("results", [{}])[0].get("generated_text", "").strip()
    if not generated:
        raise RuntimeError("watsonx API returned empty generated_text")
    return generated


def _parse_json_response(raw: str) -> dict:
    """Strip markdown fences, extract first JSON object, parse and return."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)
    cleaned = cleaned.strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def _filter_listing_for_llm(listing: list) -> list:
    """
    Fix 8: filter the repo listing before sending to the LLM.
    - Keep only files with relevant extensions (.py, .html, etc.)
    - Cap at _LISTING_MAX_FILES entries
    This prevents context bloat on large repos (FlaskBB has ~400+ files).
    """
    filtered = [
        f for f in listing
        if os.path.splitext(f)[1].lower() in _LISTING_RELEVANT_EXTS
    ]
    if len(filtered) > _LISTING_MAX_FILES:
        filtered = filtered[:_LISTING_MAX_FILES]
    return filtered


def call_bob_architect(
    feature_request: str,
    acceptance_criteria: list,
    file_listing: list,
) -> dict:
    """
    Call the Architect Agent LLM.
    Returns the parsed JSON dict from the LLM response.
    """
    system_prompt = _load_system_prompt()
    filtered = _filter_listing_for_llm(file_listing)
    listing_str = "\n".join(filtered) if filtered else "(no repo listing available)"
    user_message = (
        f"{system_prompt}\n\n"
        f"Feature request: {feature_request}\n\n"
        f"Acceptance criteria:\n{json.dumps(acceptance_criteria, indent=2)}\n\n"
        f"Repository file listing (relative paths):\n{listing_str}\n\n"
        "Output only valid JSON matching the schema above. No prose, no markdown fences."
    )
    raw = _call_watsonx(user_message)
    return _parse_json_response(raw)


# ──────────────────────────────────────────────────────────────────────────────
# NEW FILE line parsing
# ──────────────────────────────────────────────────────────────────────────────

def parse_new_file_lines(plan: str) -> set:
    """
    Extract every path that follows an exact 'NEW FILE: ' prefix in the plan text.
    Uses strict string matching — 'new file:' (lowercase) or prose like
    "we will create a new file" does NOT match. Only exact 'NEW FILE: <path>'.

    The prefix can appear at the start of a line OR mid-line (e.g. after a sentence).
    Returns a set of path strings (stripped of whitespace and trailing punctuation).
    """
    new_files = set()
    TOKEN = "NEW FILE:"
    parts = plan.split(TOKEN)
    for part in parts[1:]:  # skip everything before the first TOKEN
        # Path ends at the next newline (or end of string)
        path_line = part.split("\n")[0].strip()
        # Fix 9: strip trailing punctuation that LLMs commonly add (. , ; at end of sentence)
        path_line = path_line.rstrip(".,;")
        if path_line:
            new_files.add(path_line)
    return new_files


# ──────────────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────────────

def validate_architect_output(
    output: dict,
    repo_path: str,
) -> tuple:
    """
    Validate Architect Agent output. Returns (is_valid: bool, error_message: str).

    Checks:
      a. plan and scoped_files both present and non-empty
      b. scoped_files has at most 5 entries
      c. every NEW FILE path is also in scoped_files
      d. every NEW FILE path does NOT already exist on disk
      e. every scoped_files entry NOT in NEW FILE set DOES exist on disk
    """
    plan = output.get("plan", "")
    scoped_files = output.get("scoped_files", [])

    # a. Presence checks
    if not isinstance(plan, str) or not plan.strip():
        return False, "plan is missing or empty"
    if not isinstance(scoped_files, list) or len(scoped_files) == 0:
        return False, "scoped_files is missing or empty"

    # b. 5-file cap
    if len(scoped_files) > 5:
        return False, f"scoped_files has {len(scoped_files)} entries — maximum is 5"

    new_files = parse_new_file_lines(plan)
    scoped_set = set(scoped_files)

    # c. Every NEW FILE entry must be in scoped_files
    for nf in new_files:
        if nf not in scoped_set:
            return (
                False,
                f"NEW FILE '{nf}' appears in plan but is missing from scoped_files",
            )

    # d & e. Disk existence checks (only if repo_path is provided and exists)
    if repo_path and os.path.isdir(repo_path):
        for path in scoped_files:
            full = os.path.join(repo_path, path)
            if path in new_files:
                # NEW FILE — must NOT already exist
                if os.path.exists(full):
                    return (
                        False,
                        f"NEW FILE '{path}' already exists on disk — remove the NEW FILE: marker",
                    )
            else:
                # Existing file — MUST exist
                if not os.path.exists(full):
                    return (
                        False,
                        # Fix 7: was missing f-prefix — {path} was printed literally
                        f"scoped_files entry '{path}' does not exist in the repo — "
                        f"if it is a new file, mark it with 'NEW FILE: {path}' in the plan",
                    )

    return True, ""


# ──────────────────────────────────────────────────────────────────────────────
# Main agent entry point
# ──────────────────────────────────────────────────────────────────────────────

def _append_history(task: dict, summary: str, success: bool) -> dict:
    task["history"].append({
        "agent": "architect_agent",
        "output_summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "success": success,
    })
    return task


def run_architect_agent(task: dict, repo_path: str = "") -> dict:
    """
    Main entry point called by pipeline.py.

    Steps:
      a. Build repo file listing (if repo_path provided)
      b. Call Bob Architect with feature_request, acceptance_criteria, listing
      c. Validate output (plan, scoped_files, NEW FILE consistency, disk existence)
      d. If valid:
           - Enforce 5-file cap (truncate silently if LLM exceeds it despite prompt)
           - Set task["plan"], task["scoped_files"]
           - Set task["current_agent"] = "coding_agent"
           - Append history entry success=True
      e. If invalid:
           - Set task["status"] = "blocked"
           - Append history entry success=False with specific error
      f. Return updated task
    """
    print("[architect_agent] Scoping files and writing implementation plan...")
    task["current_agent"] = "architect_agent"

    try:
        # Build file listing
        listing = get_repo_file_listing(repo_path) if repo_path and os.path.isdir(repo_path) else []
        if not listing:
            print("[architect_agent] WARNING: no repo listing available — LLM will scope without it")

        output = call_bob_architect(
            task["feature_request"],
            task.get("acceptance_criteria", []),
            listing,
        )

        # Hard-enforce 5-file cap before validation (catch LLM over-scoping)
        if isinstance(output.get("scoped_files"), list) and len(output["scoped_files"]) > 5:
            print(
                f"[architect_agent] WARNING: LLM returned {len(output['scoped_files'])} files — "
                "truncating to 5."
            )
            output["scoped_files"] = output["scoped_files"][:5]

        is_valid, error_msg = validate_architect_output(output, repo_path)

        if not is_valid:
            raise ValueError(f"Architect output validation failed: {error_msg}")

        task["plan"] = output["plan"]
        task["scoped_files"] = output["scoped_files"]
        task["status"] = "in_progress"
        task["current_agent"] = "coding_agent"
        task = _append_history(
            task,
            f"plan written, {len(output['scoped_files'])} files scoped",
            True,
        )
        print(f"[architect_agent] Plan written, {len(output['scoped_files'])} files scoped.")

    except (RuntimeError, ValueError, json.JSONDecodeError, OSError) as e:
        print(f"[architect_agent] ERROR: {e}")
        task["status"] = "blocked"
        # Fix F: blocked task should hand off to human, not stay at architect_agent
        task["current_agent"] = "human"
        error_str = str(e)[:120]
        task = _append_history(task, f"architect_agent error: {error_str}", False)

    return task


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Architect Agent — scope files and write plan")
    parser.add_argument("--task", required=True, help="Path to task JSON file")
    parser.add_argument("--repo", default="", help="Path to sample repo root (for file listing)")
    args = parser.parse_args()

    with open(args.task, "r", encoding="utf-8") as f:
        task = json.load(f)
    result = run_architect_agent(task, repo_path=args.repo)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
