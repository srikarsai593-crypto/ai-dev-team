"""
pipeline.py -- AI Dev Team orchestration pipeline
Calls agents in sequence: PM -> Architect -> Coding -> Testing -> Review -> Manager -> (Reflection)
Each agent call is an isolated function that takes a task dict and returns an updated task dict.

Usage:
    python orchestration/pipeline.py --request "Add rate limiting to the login endpoint"
    python orchestration/pipeline.py --request "..." --task-id task_005

Week 1/2 status: agent functions (pm, architect, coding, testing, review) are STUBS.
Replace each stub body with real Bob API calls as teammates complete their agents.
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone

# -- Path setup ----------------------------------------------------------------
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "agents", "manager_agent"))
sys.path.insert(0, os.path.join(REPO_ROOT, "agents", "reflection_agent"))

import manager as manager_module
import reflection as reflection_module

try:
    import jsonschema
    JSONSCHEMA_AVAILABLE = True
except ImportError:
    JSONSCHEMA_AVAILABLE = False
    print("[pipeline] WARNING: jsonschema not installed -- skipping schema validation. Run: pip install jsonschema")

# -- Config -------------------------------------------------------------------
SCHEMA_PATH = os.path.join(REPO_ROOT, "schemas", "task_schema.json")
STATS_PATH = os.path.join(REPO_ROOT, "agents", "manager_agent", "agent_stats.json")
RUN_HISTORY_PATH = os.path.join(REPO_ROOT, "dashboard", "run_history.json")
TASKS_DIR = os.path.join(REPO_ROOT, "dashboard", "tasks")
MAX_RETRIES = 2


# ------------------------------------------------------------------------------
# Schema validation
# ------------------------------------------------------------------------------

_schema_cache = None

def load_schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            _schema_cache = json.load(f)
    return _schema_cache


def validate_task(task: dict, stage: str) -> None:
    """Validate task against task_schema.json. Print warning on failure (don't crash pipeline)."""
    if not JSONSCHEMA_AVAILABLE:
        return
    try:
        schema = load_schema()
        jsonschema.validate(task, schema)
    except jsonschema.ValidationError as e:
        print(f"[pipeline] SCHEMA VALIDATION WARNING at stage '{stage}': {e.message}")
    except Exception as e:
        print(f"[pipeline] Schema load error: {e}")


# ------------------------------------------------------------------------------
# Task initialisation
# ------------------------------------------------------------------------------

def initialize_task(feature_request: str, task_id: str = None) -> dict:
    """Create a fresh task object matching task_schema.json."""
    return {
        "task_id": task_id or f"task_{uuid.uuid4().hex[:6]}",
        "feature_request": feature_request,
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


def append_history(task: dict, agent: str, summary: str, success) -> dict:
    """Append a history entry to the task object.
    success must be True, False, or None (null in JSON).
      True  — agent ran and succeeded
      False — agent ran and its own work failed
      None  — agent was blocked by upstream bad input (success: null in schema)
    """
    task["history"].append({
        "agent": agent,
        "output_summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "success": success,
    })
    return task


def print_stage(label: str, task: dict) -> None:
    """Print a visible pipeline stage banner for the demo transparency log."""
    width = 60
    print(f"\n{'-' * width}")
    print(f"  STAGE: {label}")
    print(f"  task_id : {task['task_id']}")
    print(f"  status  : {task['status']}")
    print(f"  retries : {task['retry_count']}/{MAX_RETRIES}")
    print(f"{'-' * width}")


# ------------------------------------------------------------------------------
# Agent stubs -- replace each stub body with real Bob API calls
# ------------------------------------------------------------------------------

def call_pm_agent(task: dict) -> dict:
    """
    PM Agent: turns the feature_request into acceptance_criteria.
    STUB -- replace with real Bob call when Person A's PM Agent is ready.
    """
    print("[pm_agent] STUB: generating acceptance criteria...")
    task["current_agent"] = "pm_agent"
    task["status"] = "in_progress"

    # Stub output: derive simple criteria from the feature request
    req = task["feature_request"].lower()
    task["acceptance_criteria"] = [
        f"Feature works as described: {task['feature_request']}",
        "All existing tests still pass",
        "No new security vulnerabilities introduced",
    ]
    task = append_history(task, "pm_agent", "acceptance criteria defined (stub)", True)
    task["current_agent"] = "architect_agent"
    return task


def call_architect_agent(task: dict) -> dict:
    """
    Architect Agent: scopes relevant files and writes an implementation plan.
    STUB -- replace with real Bob call when Person A's Architect Agent is ready.
    """
    print("[architect_agent] STUB: scoping files and writing plan...")
    task["current_agent"] = "architect_agent"

    # Stub: real FlaskBB file paths that exist in sample_repo/flaskbb/
    # (tests/unit/test_auth.py does NOT exist in FlaskBB's layout — removed)
    task["scoped_files"] = [
        "flaskbb/auth/views.py",
        "flaskbb/auth/forms.py",
        "flaskbb/extensions.py",
    ]
    task["plan"] = (
        f"STUB PLAN: Implement '{task['feature_request']}' by modifying "
        "flaskbb/auth/views.py (add rate limiting to login view), "
        "flaskbb/extensions.py (register Flask-Limiter), "
        "and flaskbb/auth/forms.py (update login form if needed)."
    )
    task = append_history(task, "architect_agent", "plan written, 4 files scoped (stub)", True)
    task["current_agent"] = "coding_agent"
    return task


def _parse_new_files(plan: str) -> set:
    """
    Extract paths declared with 'NEW FILE: <path>' from the Architect's plan.
    Returns a set of path strings (stripped).
    """
    new_files = set()
    if not plan:
        return new_files
    for line in plan.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("NEW FILE:"):
            path = stripped[len("NEW FILE:"):].strip()
            if path:
                new_files.add(path)
    return new_files


def _validate_architect_output(task: dict) -> str:
    """
    Validate that the Architect's scoped_files and NEW FILE: declarations are
    consistent with the state of disk.

    Returns an error string if an Architect error is detected, or "" if clean.

    Rules (from docs/agent-conventions.md):
      1. Any scoped_file that does not exist on disk must be declared NEW FILE: in plan.
      2. Every NEW FILE: path must also appear in scoped_files.
      3. A NEW FILE: path must NOT already exist on disk.
    """
    scoped_files = task.get("scoped_files", [])
    plan = task.get("plan") or ""
    new_file_set = _parse_new_files(plan)

    # Resolve paths relative to sample_repo root.
    # Read REPO_ROOT from module globals at call time so tests can monkeypatch it.
    import orchestration.pipeline as _self
    sample_repo_root = os.path.join(_self.REPO_ROOT, "sample_repo", "flaskbb")

    # Rule 1: scoped file missing from disk but not declared NEW FILE
    for path in scoped_files:
        abs_path = os.path.join(sample_repo_root, path)
        if not os.path.exists(abs_path) and path not in new_file_set:
            return (
                f"scoped file does not exist on disk and is not declared NEW FILE: '{path}' "
                "— Architect error (add 'NEW FILE: <path>' to plan, or fix the path)"
            )

    # Rule 2: NEW FILE path not in scoped_files
    for path in new_file_set:
        if path not in scoped_files:
            return (
                f"NEW FILE declared in plan but missing from scoped_files: '{path}' "
                "— Architect error (add the path to scoped_files)"
            )

    # Rule 3: NEW FILE path already exists on disk
    for path in new_file_set:
        abs_path = os.path.join(sample_repo_root, path)
        if os.path.exists(abs_path):
            return (
                f"NEW FILE declared for a path that already exists on disk: '{path}' "
                "— Architect error (remove NEW FILE: prefix or use a different path)"
            )

    return ""


def call_coding_agent(task: dict) -> dict:
    """
    Coding Agent: implements the plan, produces a code diff.
    STUB -- replace with real Bob call when Person B's Coding Agent is ready.

    Before implementing, validates Architect output against docs/agent-conventions.md:
    - scoped_files paths must exist OR be declared NEW FILE: in plan
    - NEW FILE: paths must be in scoped_files and must not already exist on disk
    If validation fails, the task is blocked immediately and routed to Manager Agent.
    """
    task["current_agent"] = "coding_agent"

    # -- Pre-implementation validation (cross-agent convention Section 3) --------
    arch_error = _validate_architect_output(task)
    if arch_error:
        print(f"[coding_agent] BLOCKED — Architect error detected: {arch_error}")
        task["status"] = "blocked"
        task["current_agent"] = "manager_agent"
        task = append_history(
            task,
            "coding_agent",
            f"blocked: {arch_error}",
            None,  # success: null — this agent was blocked, not failed
        )
        return task

    # -- Normal stub implementation ----------------------------------------------
    print("[coding_agent] STUB: implementing code changes...")

    # Stub: realistic diff against FlaskBB auth views
    task["code_diff"] = (
        "--- a/flaskbb/auth/views.py\n"
        "+++ b/flaskbb/auth/views.py\n"
        "@@ -1,6 +1,8 @@\n"
        " from flask import Blueprint, redirect, url_for\n"
        " from flask_login import login_user\n"
        "+from flaskbb.extensions import limiter\n"
        " \n"
        " auth = Blueprint('auth', __name__)\n"
        " \n"
        "+@limiter.limit('5 per 10 minutes')\n"
        " @auth.route('/login', methods=['GET', 'POST'])\n"
        " def login():\n"
        "     pass\n"
    )
    task = append_history(task, "coding_agent", "rate limiting added to login view (stub)", True)
    task["current_agent"] = "testing_agent"
    return task


def call_testing_agent(task: dict) -> dict:
    """
    Testing Agent: runs tests against the code diff and acceptance criteria.
    STUB -- replace with real Bob call when Person C's Testing Agent is ready.
    """
    print("[testing_agent] STUB: running tests...")
    task["current_agent"] = "testing_agent"

    # Stub: all criteria matched, no failures
    task["test_results"] = {
        "passed": True,
        "criteria_matched": task["acceptance_criteria"][:],
        "failures": [],
    }
    task = append_history(task, "testing_agent", "tests passed (stub)", True)
    task["current_agent"] = "review_agent"
    return task


def call_review_agent(task: dict) -> dict:
    """
    Review Agent: reviews the code diff against the security checklist.
    STUB -- replace with real Bob call when Person D's Review Agent is ready.

    To test the retry loop: set review_passed = False in this stub.
    To test the Reflection Agent trigger: run the pipeline multiple times with
    review_passed = False and watch coding_agent's success rate drop below 0.6.
    """
    print("[review_agent] STUB: reviewing code...")
    task["current_agent"] = "review_agent"

    review_passed = True  # <- TOGGLE to False to test retry / reflection loop

    if review_passed:
        task["review_result"] = {"passed": True, "findings": []}
        task = append_history(task, "review_agent", "review passed (stub)", True)
        task["status"] = "awaiting_human_approval"
    else:
        task["review_result"] = {
            "passed": False,
            "findings": [
                {
                    "checklist_item": "missing input validation",
                    "file": "src/main.py",
                    "line": 5,
                    "severity": "high",
                    "description": "STUB: user input not validated before use",
                }
            ],
        }
        task = append_history(
            task,
            "review_agent",
            "review rejected: missing input validation (stub)",
            False,
        )
        # Also mark the coding_agent's contribution as failed when review rejects
        for entry in reversed(task["history"]):
            if entry["agent"] == "coding_agent" and entry["success"] is True:
                entry["success"] = False
                entry["output_summary"] += " [marked failed by review rejection]"
                break
        task["status"] = "needs_retry"

    task["current_agent"] = "manager_agent"
    return task


# ------------------------------------------------------------------------------
# Manager + Reflection integration
# ------------------------------------------------------------------------------

def call_manager_agent(task: dict) -> dict:
    """
    Manager Agent: updates agent_stats.json and produces a report.
    Triggers Reflection Agent if underperformers are found.
    """
    print("[manager_agent] Analysing pipeline run stats...")
    stats = manager_module.load_stats(STATS_PATH)
    stats = manager_module.update_stats(task, stats)
    manager_module.save_stats(stats, STATS_PATH)
    report = manager_module.generate_report(task, stats)

    print(f"[manager_agent] Underperformers: {report['underperformers'] or 'none'}")
    if report["underperformers"]:
        print(f"[manager_agent] Reasoning: {report['reasoning']}")
        print("[manager_agent] -> Triggering Reflection Agent...")
        reflection_results = reflection_module.run_reflection(report)
        task["_reflection_results"] = reflection_results  # non-schema field for dashboard use
    else:
        task["_reflection_results"] = []

    task["_manager_report"] = report  # non-schema field for dashboard use
    task = append_history(
        task,
        "manager_agent",
        f"stats updated; underperformers={report['underperformers']}",
        True,
    )
    return task, report


# ------------------------------------------------------------------------------
# Run history (dashboard feed)
# ------------------------------------------------------------------------------

def append_run_history(report: dict, reflection_results: list) -> None:
    """Append a run record to dashboard/run_history.json for the dashboard graph."""
    os.makedirs(os.path.dirname(RUN_HISTORY_PATH), exist_ok=True)

    history = []
    if os.path.exists(RUN_HISTORY_PATH):
        try:
            with open(RUN_HISTORY_PATH, "r", encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, ValueError):
            history = []

    run_id = len(history) + 1
    agent_rates = {
        agent: data["rate"]
        for agent, data in report.get("stats_snapshot", {}).items()
    }
    history.append({
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_id": report.get("task_id", "unknown"),
        "agent_rates": agent_rates,
        "reflections_triggered": [r["agent_name"] for r in reflection_results],
    })

    with open(RUN_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)

    print(f"[pipeline] Run history updated: run #{run_id} appended to {RUN_HISTORY_PATH}")


def save_task(task: dict, tasks_dir: str = TASKS_DIR) -> None:
    """
    Persist the final task object to dashboard/tasks/{task_id}.json so the
    dashboard's 'Needs Your Review' tab can list blocked/awaiting-approval tasks.
    Non-schema private fields (_manager_report, _reflection_results) are stripped
    before saving so the file stays clean JSON matching task_schema.json.
    """
    os.makedirs(tasks_dir, exist_ok=True)
    # Strip internal pipeline fields not in the schema
    clean = {k: v for k, v in task.items() if not k.startswith("_")}
    path = os.path.join(tasks_dir, f"{clean['task_id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
    print(f"[pipeline] Task saved: {path}")


# ------------------------------------------------------------------------------
# Final report printer (demo transparency log)
# ------------------------------------------------------------------------------

def print_final_report(task: dict) -> None:
    """Print a visible summary of the completed pipeline run for the demo."""
    width = 60
    print(f"\n{'=' * width}")
    print("  PIPELINE COMPLETE")
    print(f"{'=' * width}")
    print(f"  task_id  : {task['task_id']}")
    print(f"  request  : {task['feature_request']}")
    print(f"  status   : {task['status']}")
    print(f"  retries  : {task['retry_count']}/{MAX_RETRIES}")
    print()
    print("  Agent history:")
    for entry in task["history"]:
        icon = "+" if entry.get("success") else "X" if entry.get("success") is False else "."
        print(f"    {icon} [{entry['agent']}] {entry['output_summary']}")

    if task.get("code_diff"):
        print("\n  Code diff (first 300 chars):")
        print("  " + task["code_diff"][:300].replace("\n", "\n  "))

    if task.get("review_result") and task["review_result"].get("findings"):
        print("\n  Review findings:")
        for f in task["review_result"]["findings"]:
            print(f"    [{f['severity'].upper()}] {f['checklist_item']} -- {f['file']}:{f.get('line','?')}")

    reflection_results = task.get("_reflection_results", [])
    if reflection_results:
        print("\n  Reflection Agent rewrites:")
        for r in reflection_results:
            print(f"    {r['agent_name']}: v{r['old_version']} -> v{r['new_version']}")
            for bullet in r["change_summary"]:
                print(f"      - {bullet}")

    print(f"\n{'=' * width}\n")


# ------------------------------------------------------------------------------
# Main pipeline loop
# ------------------------------------------------------------------------------

def run_pipeline(feature_request: str, task_id: str = None) -> dict:
    """
    Execute the full pipeline: PM -> Architect -> Coding -> Testing -> Review -> Manager
    Returns the final task object.
    """
    task = initialize_task(feature_request, task_id)
    print(f"\n[pipeline] Starting pipeline for task: {task['task_id']}")
    print(f"[pipeline] Feature request: {feature_request}\n")

    # -- PM Agent --------------------------------------------------------------
    print_stage("PM Agent", task)
    task = call_pm_agent(task)
    validate_task(task, "post-pm_agent")

    # -- Architect Agent -------------------------------------------------------
    print_stage("Architect Agent", task)
    task = call_architect_agent(task)
    validate_task(task, "post-architect_agent")

    # -- Coding -> Testing -> Review loop (max MAX_RETRIES retries) -------------
    while True:
        print_stage(f"Coding Agent (attempt {task['retry_count'] + 1})", task)
        task = call_coding_agent(task)
        validate_task(task, "post-coding_agent")

        # Architect error detected by Coding Agent — skip loop, go straight to Manager
        if task["status"] == "blocked":
            print(
                "\n[pipeline] ! Coding Agent blocked due to Architect error. "
                "Skipping Testing/Review — routing straight to Manager Agent."
            )
            break

        print_stage(f"Testing Agent (attempt {task['retry_count'] + 1})", task)
        task = call_testing_agent(task)
        validate_task(task, "post-testing_agent")

        print_stage(f"Review Agent (attempt {task['retry_count'] + 1})", task)
        task = call_review_agent(task)
        validate_task(task, "post-review_agent")

        if task["status"] == "awaiting_human_approval":
            # Review passed -- exit loop
            break

        # Review failed
        if task["retry_count"] >= MAX_RETRIES:
            task["status"] = "blocked"
            print(
                f"\n[pipeline] ! Max retries ({MAX_RETRIES}) reached. "
                "Escalating to human review -- task is BLOCKED."
            )
            break

        task["retry_count"] += 1
        print(f"\n[pipeline] Review failed. Retrying... (retry {task['retry_count']}/{MAX_RETRIES})")

    # -- Manager Agent (always runs, even on blocked tasks) --------------------
    print_stage("Manager Agent", task)
    task, manager_report = call_manager_agent(task)

    # -- Update run history for dashboard --------------------------------------
    append_run_history(manager_report, task.get("_reflection_results", []))

    # -- Persist task for dashboard "Needs Your Review" tab -------------------
    save_task(task)

    # -- Final report ---------------------------------------------------------
    print_final_report(task)

    if task["status"] == "awaiting_human_approval":
        print("[pipeline] + Awaiting human approval. Review the diff above and approve/reject.")
    elif task["status"] == "blocked":
        print("[pipeline] X Task blocked after max retries. Human intervention required.")

    return task


# ------------------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AI Dev Team Pipeline")
    parser.add_argument("--request", required=True, help="Feature request in plain English")
    parser.add_argument("--task-id", default=None, help="Optional task ID (auto-generated if omitted)")
    args = parser.parse_args()

    run_pipeline(args.request, args.task_id)


if __name__ == "__main__":
    main()
