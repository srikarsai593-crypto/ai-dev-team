"""
Testing Agent — agents/testing_agent/testing_agent.py

Responsibilities:
  1. apply_diff_to_temp_copy  — copy repo, apply diff, return temp path
  2. run_repo_test_suite       — run pytest, compare to cached baseline
  3. call_bob_testing          — call Bob API with real test output
  4. validate_criteria_matched — confirm no paraphrasing/hallucination
  5. run_testing_agent         — orchestrator; replaces pipeline.py stub
"""

import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import openai

# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------

_SCHEMA_PATH = Path(__file__).parent.parent.parent / "schemas" / "task_schema.json"


def _load_schema() -> dict:
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


_TASK_SCHEMA = _load_schema()


def validate_task(task: dict) -> None:
    """Raises jsonschema.ValidationError if *task* does not conform to the schema."""
    jsonschema.validate(instance=task, schema=_TASK_SCHEMA)


# ---------------------------------------------------------------------------
# Baseline cache
# ---------------------------------------------------------------------------

# Keyed by repo_path so different repos don't share a baseline.
# This also means tests that mock different paths don't bleed into each other.
_baseline_cache: dict[str, dict] = {}


def _run_pytest(repo_path: str) -> dict:
    """
    Run pytest in *repo_path* with JSON output and return a summary dict.

    Uses ``--json-report`` when pytest-json-report is available; falls back
    to parsing the human-readable summary line so vanilla pytest installs work.
    """
    # Try JSON report for reliable counts (no regex fragility)
    json_report_path = os.path.join(repo_path, ".pytest_report.json")
    result = subprocess.run(
        [
            "python", "-m", "pytest",
            "--tb=short", "-q",
            f"--json-report-file={json_report_path}",
            "--json-report",
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=300,
    )

    # If json-report produced a file, parse it (most reliable)
    if os.path.isfile(json_report_path):
        try:
            with open(json_report_path, "r", encoding="utf-8") as fh:
                report = json.load(fh)
            summary = report.get("summary", {})
            n_passed = summary.get("passed", 0)
            n_failed = summary.get("failed", 0)
            n_error = summary.get("error", 0)
            failed_tests = [
                t["nodeid"]
                for t in report.get("tests", [])
                if t.get("outcome") in ("failed", "error")
            ]
            os.remove(json_report_path)
            return {
                "pytest_passed": result.returncode == 0,
                "total": n_passed + n_failed + n_error,
                "passed": n_passed,
                "failed": n_failed + n_error,
                "failed_tests": failed_tests,
                "raw_output": result.stdout + result.stderr,
            }
        except (json.JSONDecodeError, KeyError):
            pass  # fall through to regex fallback

    # Fallback: parse the pytest summary line  "N passed, M failed in X.Xs"
    stdout = result.stdout + result.stderr
    n_passed = 0
    n_failed = 0
    summary_match = re.search(
        r"(\d+) passed(?:.*?(\d+) failed)?", stdout
    )
    if summary_match:
        n_passed = int(summary_match.group(1))
        n_failed = int(summary_match.group(2) or 0)
    else:
        # All-failed case: "N failed"
        fail_only = re.search(r"(\d+) failed", stdout)
        if fail_only:
            n_failed = int(fail_only.group(1))

    failed_tests = re.findall(r"FAILED\s+([\w/.::\-]+)", stdout)

    return {
        "pytest_passed": result.returncode == 0,
        "total": n_passed + n_failed,
        "passed": n_passed,
        "failed": n_failed,
        "failed_tests": failed_tests,
        "raw_output": stdout,
    }


def _get_baseline(repo_path: str) -> dict:
    """Return (and cache) the baseline test results for the unmodified repo.

    Keyed by *repo_path* so multiple repos in one process don't share state.
    """
    if repo_path not in _baseline_cache:
        _baseline_cache[repo_path] = _run_pytest(repo_path)
    return _baseline_cache[repo_path]


# ---------------------------------------------------------------------------
# 1. apply_diff_to_temp_copy
# ---------------------------------------------------------------------------


def apply_diff_to_temp_copy(code_diff: str, repo_path: str) -> str:
    """
    Copy *repo_path* to a temp directory, apply *code_diff* using ``git apply``
    (falling back to ``patch -p1``), and return the temp directory path.

    Raises RuntimeError if the diff does not apply cleanly.
    """
    temp_dir = tempfile.mkdtemp(prefix="testing_agent_")
    shutil.copytree(repo_path, temp_dir, dirs_exist_ok=True)

    # Write diff to a temp file
    diff_file = os.path.join(temp_dir, "_agent.patch")
    with open(diff_file, "w", encoding="utf-8") as fh:
        fh.write(code_diff)

    # Try git apply first
    git_result = subprocess.run(
        ["git", "apply", "--check", diff_file],
        cwd=temp_dir,
        capture_output=True,
        text=True,
    )
    if git_result.returncode == 0:
        subprocess.run(
            ["git", "apply", diff_file],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        os.remove(diff_file)
        return temp_dir

    # Fall back to patch -p1
    patch_result = subprocess.run(
        ["patch", "-p1", "--dry-run", "-i", diff_file],
        cwd=temp_dir,
        capture_output=True,
        text=True,
    )
    if patch_result.returncode == 0:
        subprocess.run(
            ["patch", "-p1", "-i", diff_file],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        os.remove(diff_file)
        return temp_dir

    shutil.rmtree(temp_dir, ignore_errors=True)
    raise RuntimeError(
        f"diff did not apply cleanly.\ngit apply: {git_result.stderr}\npatch: {patch_result.stderr}"
    )


# ---------------------------------------------------------------------------
# 2. run_repo_test_suite
# ---------------------------------------------------------------------------


def run_repo_test_suite(temp_repo_path: str, original_repo_path: str = "") -> dict:
    """
    Run FlaskBB's pytest suite in *temp_repo_path*, compare against the
    cached baseline from *original_repo_path*, and return a structured dict.

    Returns:
        {
            "applied": True,
            "pytest_passed": bool,
            "total": int,
            "passed": int,
            "failed": int,
            "new_failures": [str, ...],   # failures NOT in baseline
            "baseline_failures": [str, ...],
            "error": None
        }
    """
    baseline = _get_baseline(original_repo_path)
    baseline_failed_set = set(baseline.get("failed_tests", []))

    run = _run_pytest(temp_repo_path)
    new_failures = [t for t in run["failed_tests"] if t not in baseline_failed_set]

    return {
        "applied": True,
        "pytest_passed": run["pytest_passed"],
        "total": run["total"],
        "passed": run["passed"],
        "failed": run["failed"],
        "new_failures": new_failures,
        "baseline_failures": list(baseline_failed_set),
        "error": None,
    }


# ---------------------------------------------------------------------------
# 3. call_bob_testing
# ---------------------------------------------------------------------------


def _load_system_prompt() -> str:
    prompt_path = Path(__file__).parent / "system_prompt.md"
    with open(prompt_path, "r", encoding="utf-8") as fh:
        return fh.read()


def call_bob_testing(task: dict, actual_test_output: dict) -> dict:
    """
    Call the Bob (OpenAI-compatible) API with the task and real pytest output.
    Returns the parsed ``test_results`` dict from the model response.

    Raises EnvironmentError if OPENAI_API_KEY is not set.
    Raises ValueError if the response is not valid JSON or is missing
    the ``test_results`` key.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise EnvironmentError(
            "OPENAI_API_KEY environment variable is not set. "
            "Export it before running the Testing Agent."
        )

    system_prompt = _load_system_prompt()
    user_payload = json.dumps(
        {"task": task, "actual_test_output": actual_test_output}, indent=2
    )

    client = openai.OpenAI(
        api_key=api_key,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    )

    response = client.chat.completions.create(
        model=os.environ.get("TESTING_AGENT_MODEL", "gpt-4o"),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Bob returned non-JSON content: {exc}\nRaw: {raw}") from exc

    if "test_results" not in parsed:
        raise ValueError(
            f"Bob response missing 'test_results' key. Got keys: {list(parsed.keys())}"
        )

    return parsed["test_results"]


# ---------------------------------------------------------------------------
# 4. validate_criteria_matched
# ---------------------------------------------------------------------------


def validate_criteria_matched(output: dict, acceptance_criteria: list) -> bool:
    """
    Return True iff every string in ``output["test_results"]["criteria_matched"]``
    is an exact match of one of the strings in *acceptance_criteria*.

    A paraphrased or invented string causes this to return False.
    """
    criteria_matched = output.get("criteria_matched", [])
    criteria_set = set(acceptance_criteria)
    return all(item in criteria_set for item in criteria_matched)


# ---------------------------------------------------------------------------
# 5. run_testing_agent
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_history(task: dict, agent: str, summary: str, success) -> None:
    task["history"].append(
        {
            "agent": agent,
            "output_summary": summary,
            "timestamp": _now_iso(),
            "success": success,
        }
    )


def run_testing_agent(task: dict, repo_path: str) -> dict:
    """
    Full orchestration for one Testing Agent pass.

    Steps:
      a. apply_diff_to_temp_copy — on failure, attribute to coding_agent
      b. run_repo_test_suite
      c. call_bob_testing with real results
      d. validate_criteria_matched
      e. populate task["test_results"], set current_agent = "review_agent"
      f. append history entries; return updated task

    Failures:
      - Diff won't apply          → coding_agent success:false, status "blocked"
      - Bob bad JSON / bad schema → testing_agent success:false
      - criteria_matched invalid  → testing_agent success:false
    """
    task = copy.deepcopy(task)

    code_diff = task.get("code_diff")
    acceptance_criteria = task.get("acceptance_criteria", [])

    # ── (a) Apply diff ───────────────────────────────────────────────────────
    temp_path = None
    try:
        temp_path = apply_diff_to_temp_copy(code_diff, repo_path)
    except Exception as exc:
        # Diff failure is a CODING AGENT error, not a testing agent error
        _append_history(
            task,
            "coding_agent",
            f"diff did not apply cleanly: {exc}",
            False,
        )
        task["status"] = "blocked"
        task["current_agent"] = "manager_agent"
        return task

    try:
        # ── (b) Run test suite ───────────────────────────────────────────────
        actual_test_output = run_repo_test_suite(
            temp_repo_path=temp_path,
            original_repo_path=repo_path,
        )

        # ── (c) Call Bob ─────────────────────────────────────────────────────
        test_results = call_bob_testing(task, actual_test_output)

        # ── (d) Validate criteria_matched ────────────────────────────────────
        if not validate_criteria_matched(test_results, acceptance_criteria):
            raise ValueError(
                "criteria_matched contains strings not in acceptance_criteria "
                "(paraphrasing or hallucination detected)"
            )

        # Validate test_results conforms to the schema's test_results sub-shape
        # (lightweight structural check)
        if not isinstance(test_results.get("passed"), bool):
            raise ValueError("test_results.passed must be a boolean")
        if not isinstance(test_results.get("criteria_matched"), list):
            raise ValueError("test_results.criteria_matched must be a list")
        if not isinstance(test_results.get("failures"), list):
            raise ValueError("test_results.failures must be a list")

        # ── (e) Populate task ────────────────────────────────────────────────
        task["test_results"] = test_results
        task["current_agent"] = "review_agent"
        task["status"] = "in_progress"

        matched = len(test_results["criteria_matched"])
        total = len(acceptance_criteria)
        passed_str = "passed" if test_results["passed"] else "failed"

        # ── (f) Append history — testing_agent success:true ──────────────────
        _append_history(
            task,
            "testing_agent",
            f"{matched}/{total} criteria matched; {passed_str}",
            True,
        )

        # Validate full task against schema before returning
        validate_task(task)
        return task

    except Exception as exc:
        # Testing Agent itself errored
        _append_history(
            task,
            "testing_agent",
            f"testing agent error: {exc}",
            False,
        )
        task["status"] = "blocked"
        task["current_agent"] = "manager_agent"
        return task

    finally:
        if temp_path:
            shutil.rmtree(temp_path, ignore_errors=True)
