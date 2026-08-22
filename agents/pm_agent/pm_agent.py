"""
pm_agent.py — PM Agent standalone module
Reads a feature_request from the task dict, calls IBM watsonx to generate
concrete acceptance criteria, validates the response, and returns the
updated task dict.

Usage (called from pipeline.py):
    from agents.pm_agent.pm_agent import run_pm_agent
    task = run_pm_agent(task)

CLI (standalone test):
    python agents/pm_agent/pm_agent.py --request "Add rate limiting to login"
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
    pass  # python-dotenv optional; env vars can be set directly in environment

# ──────────────────────────────────────────────────────────────────────────────
# watsonx defaults — plain string literals so _call_watsonx() fallbacks are
# never stale import-time values. All env vars are read inside _call_watsonx().
# ──────────────────────────────────────────────────────────────────────────────
_WATSONX_URL_DEFAULT = "https://us-south.ml.cloud.ibm.com/ml/v1/text/generation?version=2023-05-29"
_WATSONX_MODEL_DEFAULT = "ibm/granite-13b-instruct-v2"

# Path to this agent's system prompt
_PROMPT_PATH = os.path.join(os.path.dirname(__file__), "system_prompt.md")

# ──────────────────────────────────────────────────────────────────────────────
# LLM helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_system_prompt() -> str:
    """Load agents/pm_agent/system_prompt.md at call time (so edits take effect immediately)."""
    with open(_PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _call_watsonx(prompt: str) -> str:
    """
    POST to IBM watsonx.ai text generation endpoint.
    Returns raw generated text.
    Raises RuntimeError if credentials missing or HTTP call fails.

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
            "max_new_tokens": 800,
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
    """
    Extract and parse a JSON object from the LLM's raw text output.
    Strips markdown code fences and surrounding prose before parsing.
    Raises ValueError if no valid JSON object can be found.
    """
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)
    cleaned = cleaned.strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def call_bob_pm(feature_request: str) -> dict:
    """
    Call the PM Agent Bob LLM with the feature_request.
    Returns the parsed JSON dict from the LLM response.
    Raises RuntimeError on API failure, ValueError on JSON parse failure.
    """
    system_prompt = _load_system_prompt()
    user_message = (
        f"{system_prompt}\n\n"
        f"Feature request: {feature_request}\n\n"
        "Output only valid JSON matching the schema above. No prose, no markdown fences."
    )
    raw = _call_watsonx(user_message)
    return _parse_json_response(raw)


# ──────────────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────────────

def validate_pm_output(output: dict) -> bool:
    """
    Validate PM Agent LLM output.

    Rules:
    - `acceptance_criteria` must be present and a list
    - Must have between 2 and 6 items
    - No empty strings
    - No exact duplicates (case-sensitive)
    - At least one criterion must reference "existing tests" + "pass"
      (the mandatory safety-net criterion required by the system prompt)

    Returns True if valid, False otherwise.
    Note: a `blocked` output with empty criteria is also considered valid
    (the pipeline handles it as a blocked task, not a PM failure).
    """
    # Blocked output — valid by design, pipeline sets status=blocked
    if output.get("blocked") is True:
        return True

    criteria = output.get("acceptance_criteria")
    if not isinstance(criteria, list):
        return False
    if not (2 <= len(criteria) <= 6):
        return False
    for c in criteria:
        if not isinstance(c, str) or not c.strip():
            return False
    # Fix C: case-insensitive duplicate check — catches capitalisation variants
    lower_criteria = [c.lower().strip() for c in criteria]
    if len(lower_criteria) != len(set(lower_criteria)):
        return False
    # Mandatory "all existing tests still pass" criterion
    combined_lower = " ".join(lower_criteria)
    if "existing tests" not in combined_lower or "pass" not in combined_lower:
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Main agent entry point
# ──────────────────────────────────────────────────────────────────────────────

def _append_history(task: dict, summary: str, success: bool) -> dict:
    """Append a pm_agent entry to task history."""
    task["history"].append({
        "agent": "pm_agent",
        "output_summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "success": success,
    })
    return task


def run_pm_agent(task: dict) -> dict:
    """
    Main entry point called by pipeline.py.

    Steps:
      a. Call Bob PM with task["feature_request"]
      b. Validate the output
      c. If valid (and not blocked):
           - Set task["acceptance_criteria"]
           - Set task["status"] = "in_progress"
           - Set task["current_agent"] = "architect_agent"
           - Append history entry: success=True
      d. If blocked by PM Agent (nonsensical request):
           - Set task["status"] = "blocked"
           - Set task["acceptance_criteria"] = []
           - Append history entry: success=False, output_summary = block_reason
      e. If LLM call failed or output invalid:
           - Set task["status"] = "blocked"
           - Append history entry: success=False, output_summary = specific error
      f. Return updated task
    """
    print("[pm_agent] Generating acceptance criteria...")
    task["current_agent"] = "pm_agent"
    task["status"] = "in_progress"

    try:
        output = call_bob_pm(task["feature_request"])

        # --- Handle explicitly blocked requests ---
        if output.get("blocked") is True:
            block_reason = output.get("block_reason", "feature request blocked by PM Agent")
            print(f"[pm_agent] Request blocked: {block_reason}")
            task["acceptance_criteria"] = []
            task["status"] = "blocked"
            task = _append_history(task, f"blocked: {block_reason}", False)
            return task

        # --- Validate criteria ---
        if not validate_pm_output(output):
            raise ValueError(
                f"PM output failed validation — acceptance_criteria must be a list of "
                f"2–6 non-empty, non-duplicate strings. Got: {output.get('acceptance_criteria')}"
            )

        task["acceptance_criteria"] = output["acceptance_criteria"]
        task["status"] = "in_progress"
        task["current_agent"] = "architect_agent"
        task = _append_history(
            task,
            f"acceptance criteria defined ({len(output['acceptance_criteria'])} criteria)",
            True,
        )
        print(f"[pm_agent] {len(output['acceptance_criteria'])} criteria generated.")

    except (RuntimeError, ValueError, json.JSONDecodeError, OSError) as e:
        print(f"[pm_agent] ERROR: {e}")
        task["status"] = "blocked"
        task["acceptance_criteria"] = []
        # Fix B: blocked task should hand off to human, not stay at pm_agent
        task["current_agent"] = "human"
        error_str = str(e)[:120]
        task = _append_history(task, f"pm_agent error: {error_str}", False)

    return task


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PM Agent — generate acceptance criteria")
    parser.add_argument("--request", required=True, help="Feature request in plain English")
    args = parser.parse_args()

    # Build a minimal task and run
    task = {
        "task_id": "cli_test",
        "feature_request": args.request,
        "acceptance_criteria": [],
        "scoped_files": [],
        "status": "pending",
        "current_agent": "pm_agent",
        "plan": None,
        "history": [],
        "code_diff": None,
        "test_results": None,
        "review_result": None,
        "retry_count": 0,
    }
    result = run_pm_agent(task)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
