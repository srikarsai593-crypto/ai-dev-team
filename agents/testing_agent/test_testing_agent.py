"""
agents/testing_agent/test_testing_agent.py

Unit tests for the Testing Agent.

All external I/O is mocked:
  - apply_diff_to_temp_copy  (no real repo copy / git)
  - run_repo_test_suite       (no real pytest execution)
  - call_bob_testing          (no real Bob / OpenAI call)

Tests cover:
  1. validate_criteria_matched — exact subset passes
  2. validate_criteria_matched — paraphrased string fails
  3. validate_criteria_matched — string not in original list fails
  4. run_testing_agent — diff failure → coding_agent success:false (attribution)
  5. run_testing_agent — valid output, test_results.passed=True  → testing_agent success:true
  6. run_testing_agent — valid output, test_results.passed=False → testing_agent success:true
  7. run_testing_agent — malformed Bob output → testing_agent success:false
"""

import copy
import json
from unittest.mock import MagicMock, patch

import pytest

from agents.testing_agent.testing_agent import (
    run_testing_agent,
    validate_criteria_matched,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ACCEPTANCE_CRITERIA = [
    "Max 5 login attempts per IP per 10 minutes",
    "Returns HTTP 429 when limit exceeded",
    "All existing login tests still pass",
]

_BASE_TASK = {
    "task_id": "task_001",
    "feature_request": "Add rate limiting to the login endpoint",
    "acceptance_criteria": ACCEPTANCE_CRITERIA,
    "scoped_files": [
        "flaskbb/auth/views.py",
        "flaskbb/utils/rate_limit.py",
    ],
    "status": "in_progress",
    "current_agent": "testing_agent",
    "plan": "Add a token-bucket rate limiter middleware keyed by IP.",
    "history": [
        {
            "agent": "pm_agent",
            "output_summary": "criteria defined",
            "timestamp": "2026-08-10T09:00:00Z",
            "success": True,
        },
        {
            "agent": "architect_agent",
            "output_summary": "plan + files scoped",
            "timestamp": "2026-08-10T09:05:00Z",
            "success": True,
        },
        {
            "agent": "coding_agent",
            "output_summary": "rate limiter added",
            "timestamp": "2026-08-10T09:15:00Z",
            "success": True,
        },
    ],
    "code_diff": (
        "--- a/flaskbb/utils/rate_limit.py\n"
        "+++ b/flaskbb/utils/rate_limit.py\n"
        "@@ -0,0 +1,5 @@\n"
        "+LIMIT = 5\n"
        "+WINDOW = 600\n"
    ),
    "test_results": None,
    "review_result": None,
    "retry_count": 0,
}

_GOOD_TEST_OUTPUT = {
    "applied": True,
    "pytest_passed": True,
    "total": 42,
    "passed": 42,
    "failed": 0,
    "new_failures": [],
    "baseline_failures": [],
    "error": None,
}

_PASSED_BOB_RESPONSE = {
    "test_results": {
        "passed": True,
        "criteria_matched": [
            "Max 5 login attempts per IP per 10 minutes",
            "Returns HTTP 429 when limit exceeded",
            "All existing login tests still pass",
        ],
        "failures": [],
    }
}

_FAILED_BOB_RESPONSE = {
    "test_results": {
        "passed": False,
        "criteria_matched": [
            "Max 5 login attempts per IP per 10 minutes",
        ],
        "failures": [
            "Returns HTTP 429 when limit exceeded — diff returns 200 instead of 429",
            "All existing login tests still pass — test_login_existing FAILED: AssertionError",
        ],
    }
}


def _task():
    """Return a fresh deep copy of the base task."""
    return copy.deepcopy(_BASE_TASK)


# ---------------------------------------------------------------------------
# 1–3. validate_criteria_matched
# ---------------------------------------------------------------------------


class TestValidateCriteriaMatched:
    def test_exact_subset_passes(self):
        """All strings in criteria_matched are exact copies — should pass."""
        output = {
            "criteria_matched": [
                "Max 5 login attempts per IP per 10 minutes",
                "Returns HTTP 429 when limit exceeded",
            ]
        }
        assert validate_criteria_matched(output, ACCEPTANCE_CRITERIA) is True

    def test_full_set_passes(self):
        """criteria_matched equals acceptance_criteria exactly — should pass."""
        output = {"criteria_matched": list(ACCEPTANCE_CRITERIA)}
        assert validate_criteria_matched(output, ACCEPTANCE_CRITERIA) is True

    def test_empty_criteria_matched_passes(self):
        """Empty criteria_matched is a valid (vacuous) subset — should pass."""
        output = {"criteria_matched": []}
        assert validate_criteria_matched(output, ACCEPTANCE_CRITERIA) is True

    def test_paraphrased_string_fails(self):
        """A paraphrased criterion string must NOT be accepted."""
        output = {
            "criteria_matched": [
                "Limits login to 5 attempts per IP in 10 minutes",  # paraphrase
            ]
        }
        assert validate_criteria_matched(output, ACCEPTANCE_CRITERIA) is False

    def test_fabricated_string_not_in_original_fails(self):
        """A string not present in acceptance_criteria at all must be rejected."""
        output = {
            "criteria_matched": [
                "Max 5 login attempts per IP per 10 minutes",
                "Sends an email on lockout",  # hallucinated — not in original list
            ]
        }
        assert validate_criteria_matched(output, ACCEPTANCE_CRITERIA) is False

    def test_case_difference_fails(self):
        """A string that differs only in case is still not an exact match."""
        output = {
            "criteria_matched": [
                "max 5 login attempts per ip per 10 minutes",  # lower-case
            ]
        }
        assert validate_criteria_matched(output, ACCEPTANCE_CRITERIA) is False


# ---------------------------------------------------------------------------
# 4. Diff failure → coding_agent attribution
# ---------------------------------------------------------------------------

MODULE = "agents.testing_agent.testing_agent"


class TestDiffFailureAttribution:
    @patch(f"{MODULE}.apply_diff_to_temp_copy", side_effect=RuntimeError("diff did not apply cleanly"))
    def test_diff_failure_attributed_to_coding_agent(self, mock_apply):
        """
        When apply_diff_to_temp_copy raises, run_testing_agent must:
          - append a history entry with agent="coding_agent" and success=False
          - NOT append a testing_agent entry
          - set status="blocked"
          - set current_agent="manager_agent"
        """
        task = _task()
        result = run_testing_agent(task, repo_path="/fake/repo")

        history_agents = [h["agent"] for h in result["history"]]
        history_successes = {h["agent"]: h["success"] for h in result["history"]}

        # coding_agent entry appended with success=False
        assert "coding_agent" in history_agents, "coding_agent history entry missing"
        # Find the NEW coding_agent entry (last one with that agent name)
        new_coding_entries = [
            h for h in result["history"]
            if h["agent"] == "coding_agent" and h["success"] is False
        ]
        assert len(new_coding_entries) == 1, (
            "Expected exactly one new coding_agent failure entry"
        )
        assert "diff did not apply cleanly" in new_coding_entries[0]["output_summary"]

        # No testing_agent entry should have been appended
        testing_entries = [h for h in result["history"] if h["agent"] == "testing_agent"]
        assert len(testing_entries) == 0, "testing_agent must NOT get a history entry on diff failure"

        assert result["status"] == "blocked"
        assert result["current_agent"] == "manager_agent"

    @patch(f"{MODULE}.apply_diff_to_temp_copy", side_effect=RuntimeError("diff did not apply cleanly"))
    def test_diff_failure_attribution_is_coding_agent_not_testing_agent(self, mock_apply):
        """Explicitly verify the attributed agent is coding_agent, not testing_agent."""
        task = _task()
        result = run_testing_agent(task, repo_path="/fake/repo")

        new_failure_agents = [
            h["agent"] for h in result["history"] if h["success"] is False
        ]
        assert "coding_agent" in new_failure_agents
        assert "testing_agent" not in new_failure_agents


# ---------------------------------------------------------------------------
# 5–6. Valid output → testing_agent success:true regardless of passed value
# ---------------------------------------------------------------------------

class TestTestingAgentSuccessTrue:
    def _run_with_bob_response(self, bob_response: dict) -> dict:
        task = _task()
        with (
            patch(f"{MODULE}.apply_diff_to_temp_copy", return_value="/tmp/fake_repo"),
            patch(f"{MODULE}.run_repo_test_suite", return_value=_GOOD_TEST_OUTPUT),
            patch(f"{MODULE}.call_bob_testing", return_value=bob_response["test_results"]),
            patch(f"{MODULE}.shutil.rmtree"),  # skip temp dir cleanup
        ):
            return run_testing_agent(task, repo_path="/fake/repo")

    def test_passed_true_produces_testing_agent_success_true(self):
        """
        When Bob returns test_results.passed=True with valid criteria_matched,
        testing_agent history entry must have success=True.
        """
        result = self._run_with_bob_response(_PASSED_BOB_RESPONSE)

        testing_entries = [h for h in result["history"] if h["agent"] == "testing_agent"]
        assert len(testing_entries) == 1
        assert testing_entries[0]["success"] is True

        assert result["test_results"]["passed"] is True
        assert result["current_agent"] == "review_agent"
        assert result["status"] == "in_progress"

    def test_passed_false_produces_testing_agent_success_true(self):
        """
        When Bob returns test_results.passed=False (code failed its tests),
        the testing_agent history entry must STILL have success=True because
        Testing Agent did its job correctly — the CODE failed, not the agent.
        """
        result = self._run_with_bob_response(_FAILED_BOB_RESPONSE)

        testing_entries = [h for h in result["history"] if h["agent"] == "testing_agent"]
        assert len(testing_entries) == 1
        assert testing_entries[0]["success"] is True

        # test_results.passed is False — testing agent recorded that accurately
        assert result["test_results"]["passed"] is False
        # Still moves to review_agent (review agent decides what to do with a failed test)
        assert result["current_agent"] == "review_agent"

    def test_criteria_matched_populated_correctly(self):
        """criteria_matched in result equals what Bob returned."""
        result = self._run_with_bob_response(_PASSED_BOB_RESPONSE)
        assert result["test_results"]["criteria_matched"] == ACCEPTANCE_CRITERIA

    def test_failures_populated_correctly(self):
        """failures in result equals what Bob returned."""
        result = self._run_with_bob_response(_FAILED_BOB_RESPONSE)
        assert len(result["test_results"]["failures"]) == 2


# ---------------------------------------------------------------------------
# 7. Malformed Bob output → testing_agent success:false
# ---------------------------------------------------------------------------

class TestMalformedBobOutput:
    def _run_with_bob_side_effect(self, side_effect) -> dict:
        task = _task()
        with (
            patch(f"{MODULE}.apply_diff_to_temp_copy", return_value="/tmp/fake_repo"),
            patch(f"{MODULE}.run_repo_test_suite", return_value=_GOOD_TEST_OUTPUT),
            patch(f"{MODULE}.call_bob_testing", side_effect=side_effect),
            patch(f"{MODULE}.shutil.rmtree"),
        ):
            return run_testing_agent(task, repo_path="/fake/repo")

    def test_bob_raises_value_error_produces_testing_agent_success_false(self):
        """If call_bob_testing raises ValueError (bad JSON), testing_agent success=False."""
        result = self._run_with_bob_side_effect(
            ValueError("Bob returned non-JSON content")
        )
        testing_entries = [h for h in result["history"] if h["agent"] == "testing_agent"]
        assert len(testing_entries) == 1
        assert testing_entries[0]["success"] is False
        assert result["status"] == "blocked"

    def test_paraphrased_criteria_matched_produces_testing_agent_success_false(self):
        """
        If Bob returns criteria_matched with paraphrased strings,
        validate_criteria_matched returns False → testing_agent success=False.
        """
        bad_response = {
            "passed": True,
            "criteria_matched": [
                "Limits login to 5 per IP per 10 min",  # paraphrase — not in original
            ],
            "failures": [],
        }
        task = _task()
        with (
            patch(f"{MODULE}.apply_diff_to_temp_copy", return_value="/tmp/fake_repo"),
            patch(f"{MODULE}.run_repo_test_suite", return_value=_GOOD_TEST_OUTPUT),
            patch(f"{MODULE}.call_bob_testing", return_value=bad_response),
            patch(f"{MODULE}.shutil.rmtree"),
        ):
            result = run_testing_agent(task, repo_path="/fake/repo")

        testing_entries = [h for h in result["history"] if h["agent"] == "testing_agent"]
        assert len(testing_entries) == 1
        assert testing_entries[0]["success"] is False

    def test_missing_passed_field_produces_testing_agent_success_false(self):
        """If test_results is missing the 'passed' boolean, testing_agent success=False."""
        bad_response = {
            # "passed" key is missing
            "criteria_matched": ACCEPTANCE_CRITERIA,
            "failures": [],
        }
        task = _task()
        with (
            patch(f"{MODULE}.apply_diff_to_temp_copy", return_value="/tmp/fake_repo"),
            patch(f"{MODULE}.run_repo_test_suite", return_value=_GOOD_TEST_OUTPUT),
            patch(f"{MODULE}.call_bob_testing", return_value=bad_response),
            patch(f"{MODULE}.shutil.rmtree"),
        ):
            result = run_testing_agent(task, repo_path="/fake/repo")

        testing_entries = [h for h in result["history"] if h["agent"] == "testing_agent"]
        assert len(testing_entries) == 1
        assert testing_entries[0]["success"] is False
