"""
test_manager.py — Unit tests for Manager Agent logic

Run with: pytest agents/manager_agent/test_manager.py -v
"""
import json
import os
import tempfile
import pytest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from manager import (
    UNDERPERFORM_THRESHOLD,
    MIN_RUNS,
    WINDOW_SIZE,
    initialize_empty_stats,
    update_stats,
    get_underperformers,
    generate_report,
    load_stats,
    save_stats,
    extract_failure_patterns,
)


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------

def make_task(history_entries):
    return {
        "task_id": "task_test",
        "feature_request": "Add rate limiting",
        "acceptance_criteria": ["Returns 429 on limit exceeded"],
        "scoped_files": ["src/auth/login.py"],
        "status": "approved",
        "current_agent": "manager_agent",
        "history": history_entries,
        "code_diff": "--- a/src/auth/login.py\n+++ b/src/auth/login.py\n",
        "test_results": {"passed": True, "criteria_matched": [], "failures": []},
        "review_result": {"passed": True, "findings": []},
        "retry_count": 0,
    }


def make_history_entry(agent, success, summary="done"):
    return {
        "agent": agent,
        "output_summary": summary,
        "timestamp": "2026-08-10T12:00:00Z",
        "success": success,
    }


def feed_outcomes(agent, outcomes, window_size=WINDOW_SIZE):
    """Feed a list of True/False outcomes into fresh stats for one agent."""
    stats = initialize_empty_stats()
    for outcome in outcomes:
        task = make_task([make_history_entry(agent, outcome)])
        stats = update_stats(task, stats, window_size=window_size)
    return stats


# -----------------------------------------------------------------------
# update_stats — basic behaviour
# -----------------------------------------------------------------------

def test_update_stats_increments_runs_and_successes():
    stats = initialize_empty_stats()
    task = make_task([
        make_history_entry("pm_agent", True),
        make_history_entry("architect_agent", True),
        make_history_entry("coding_agent", False),
    ])
    stats = update_stats(task, stats)

    assert stats["pm_agent"]["runs"] == 1
    assert stats["pm_agent"]["successes"] == 1
    assert stats["pm_agent"]["rate"] == 1.0

    assert stats["architect_agent"]["runs"] == 1
    assert stats["architect_agent"]["successes"] == 1

    assert stats["coding_agent"]["runs"] == 1
    assert stats["coding_agent"]["successes"] == 0
    assert stats["coding_agent"]["rate"] == 0.0


def test_update_stats_records_recent_outcomes():
    """recent_outcomes should contain the actual True/False values."""
    stats = initialize_empty_stats()
    task = make_task([
        make_history_entry("coding_agent", True),
        make_history_entry("coding_agent", False),
    ])
    stats = update_stats(task, stats)
    assert stats["coding_agent"]["recent_outcomes"] == [True, False]


def test_update_stats_skips_none_success_entries():
    stats = initialize_empty_stats()
    task = make_task([
        {"agent": "pm_agent", "output_summary": "done", "timestamp": "2026-08-10T12:00:00Z", "success": None},
    ])
    stats = update_stats(task, stats)
    assert stats["pm_agent"]["runs"] == 0
    assert stats["pm_agent"]["recent_outcomes"] == []


def test_update_stats_skips_untracked_agents():
    stats = initialize_empty_stats()
    task = make_task([
        make_history_entry("human", True),
        make_history_entry("reflection_agent", True),
        make_history_entry("coding_agent", True),
    ])
    stats = update_stats(task, stats)
    assert "human" not in stats
    assert "reflection_agent" not in stats
    assert stats["coding_agent"]["runs"] == 1


def test_update_stats_accumulates_within_window():
    """Calling update_stats multiple times accumulates while under the window cap."""
    stats = initialize_empty_stats()
    stats = update_stats(make_task([make_history_entry("coding_agent", True)]), stats)
    stats = update_stats(make_task([make_history_entry("coding_agent", False)]), stats)

    assert stats["coding_agent"]["runs"] == 2
    assert stats["coding_agent"]["successes"] == 1
    assert stats["coding_agent"]["rate"] == 0.5
    assert stats["coding_agent"]["recent_outcomes"] == [True, False]


# -----------------------------------------------------------------------
# FIX 1 — Rolling window tests
# -----------------------------------------------------------------------

def test_update_stats_caps_at_window_size():
    """recent_outcomes length must never exceed WINDOW_SIZE; oldest entries drop first."""
    outcomes = [True, False, True, False, True, False, True]   # 7 items > WINDOW_SIZE=5
    stats = feed_outcomes("coding_agent", outcomes, window_size=5)

    assert len(stats["coding_agent"]["recent_outcomes"]) == 5
    # The last 5 outcomes are: False, True, False, True — wait, slice:
    # full list: [T,F,T,F,T,F,T], last 5 = [T,F,T,F,T]  — index 2..6
    assert stats["coding_agent"]["recent_outcomes"] == [True, False, True, False, True]


def test_update_stats_rate_reflects_recent_window_only():
    """
    An agent that failed its first 3 runs then succeeded the next 5 should have
    rate=1.0 (window=5), not 5/8=0.625 (all-time).
    """
    # 3 failures, then 5 successes — all-time would be 5/8=0.625
    outcomes = [False, False, False, True, True, True, True, True]
    stats = feed_outcomes("coding_agent", outcomes, window_size=5)

    assert stats["coding_agent"]["recent_outcomes"] == [True, True, True, True, True]
    assert stats["coding_agent"]["rate"] == 1.0
    assert stats["coding_agent"]["runs"] == 5
    assert stats["coding_agent"]["successes"] == 5


def test_update_stats_window_drops_oldest_not_newest():
    """When the window overflows, the oldest entry is removed, not the newest."""
    outcomes = [False] * 5 + [True]   # 5 failures then 1 success
    stats = feed_outcomes("coding_agent", outcomes, window_size=5)

    # Window should be the last 5: [F, F, F, F, T]
    assert stats["coding_agent"]["recent_outcomes"] == [False, False, False, False, True]
    assert stats["coding_agent"]["successes"] == 1


# -----------------------------------------------------------------------
# get_underperformers
# -----------------------------------------------------------------------

def test_get_underperformers_flags_low_rate_agent():
    stats = initialize_empty_stats()
    stats["coding_agent"] = {"recent_outcomes": [True, False, False], "runs": 3, "successes": 1, "rate": 1 / 3}
    result = get_underperformers(stats)
    assert "coding_agent" in result


def test_get_underperformers_does_not_flag_below_min_runs():
    stats = initialize_empty_stats()
    stats["coding_agent"] = {"recent_outcomes": [False, False], "runs": 2, "successes": 0, "rate": 0.0}
    result = get_underperformers(stats)
    assert "coding_agent" not in result


def test_get_underperformers_does_not_flag_null_rate():
    stats = initialize_empty_stats()
    result = get_underperformers(stats)
    assert result == []


def test_get_underperformers_does_not_flag_at_threshold():
    """An agent exactly at 0.6 must NOT be flagged (strictly less than)."""
    stats = initialize_empty_stats()
    stats["testing_agent"] = {"recent_outcomes": [True, True, True, False, False], "runs": 5, "successes": 3, "rate": 0.6}
    result = get_underperformers(stats)
    assert "testing_agent" not in result


def test_get_underperformers_flags_only_failing_agents():
    stats = initialize_empty_stats()
    stats["pm_agent"] = {"recent_outcomes": [True, True, True, True, True], "runs": 5, "successes": 5, "rate": 1.0}
    stats["coding_agent"] = {"recent_outcomes": [True, False, False, False], "runs": 4, "successes": 1, "rate": 0.25}
    result = get_underperformers(stats)
    assert result == ["coding_agent"]


# -----------------------------------------------------------------------
# FIX 3 — Canonical threshold rule tests
# -----------------------------------------------------------------------

def test_get_underperformers_default_threshold_is_point_6():
    """Calling get_underperformers with no explicit threshold arg must use 0.6."""
    stats = initialize_empty_stats()
    # Just below threshold: 2/4 = 0.5
    stats["coding_agent"] = {"recent_outcomes": [True, False, True, False], "runs": 4, "successes": 2, "rate": 0.5}
    result = get_underperformers(stats)
    assert "coding_agent" in result, "Default threshold should be 0.6; 0.5 < 0.6 so agent must be flagged"


def test_get_underperformers_default_min_runs_is_3():
    """Calling get_underperformers with no explicit min_runs arg must use 3."""
    stats = initialize_empty_stats()
    # 0% rate but only 2 runs — must NOT be flagged with default min_runs=3
    stats["coding_agent"] = {"recent_outcomes": [False, False], "runs": 2, "successes": 0, "rate": 0.0}
    result = get_underperformers(stats)
    assert "coding_agent" not in result, "Default min_runs should be 3; 2 runs must not trigger flag"


# -----------------------------------------------------------------------
# generate_report
# -----------------------------------------------------------------------

def test_generate_report_no_underperformers():
    stats = initialize_empty_stats()
    stats["pm_agent"] = {"recent_outcomes": [True, True, True], "runs": 3, "successes": 3, "rate": 1.0}
    task = make_task([])
    task["task_id"] = "task_001"
    report = generate_report(task, stats)

    assert report["task_id"] == "task_001"
    assert report["underperformers"] == []
    assert report["recommended_action"] == "none"
    assert "stats_snapshot" in report
    assert "agent_stats" in report
    assert isinstance(report["agent_stats"], list)


def test_generate_report_with_underperformer():
    stats = initialize_empty_stats()
    stats["coding_agent"] = {"recent_outcomes": [True, False, False, False, False], "runs": 5, "successes": 1, "rate": 0.2}
    task = make_task([make_history_entry("coding_agent", False, "review rejected: missing input validation")])
    report = generate_report(task, stats)

    assert "coding_agent" in report["underperformers"]
    assert report["recommended_action"] == "rewrite_prompt"
    assert "coding_agent" in report["reasoning"]


def test_generate_report_agent_stats_array_has_all_agents():
    """agent_stats array must contain an entry for every tracked agent."""
    stats = initialize_empty_stats()
    task = make_task([])
    report = generate_report(task, stats)

    agents_in_report = [e["agent"] for e in report["agent_stats"]]
    for agent in ["pm_agent", "architect_agent", "coding_agent", "testing_agent", "review_agent"]:
        assert agent in agents_in_report


def test_generate_report_trigger_reflection_set_correctly():
    """trigger_reflection must be True only for underperforming agents."""
    stats = initialize_empty_stats()
    stats["coding_agent"] = {"recent_outcomes": [False, False, False], "runs": 3, "successes": 0, "rate": 0.0}
    stats["pm_agent"] = {"recent_outcomes": [True, True, True], "runs": 3, "successes": 3, "rate": 1.0}
    task = make_task([])
    report = generate_report(task, stats)

    coding_entry = next(e for e in report["agent_stats"] if e["agent"] == "coding_agent")
    pm_entry = next(e for e in report["agent_stats"] if e["agent"] == "pm_agent")
    assert coding_entry["trigger_reflection"] is True
    assert pm_entry["trigger_reflection"] is False


def test_generate_report_recent_outcomes_in_agent_stats():
    """Each agent_stats entry must include recent_outcomes list."""
    stats = initialize_empty_stats()
    stats["testing_agent"] = {"recent_outcomes": [True, False, True], "runs": 3, "successes": 2, "rate": 2/3}
    task = make_task([])
    report = generate_report(task, stats)

    testing_entry = next(e for e in report["agent_stats"] if e["agent"] == "testing_agent")
    assert testing_entry["recent_outcomes"] == [True, False, True]
    assert testing_entry["failures"] == 1


# -----------------------------------------------------------------------
# load_stats / save_stats
# -----------------------------------------------------------------------

def test_load_stats_missing_file_returns_empty():
    result = load_stats("/nonexistent/path/agent_stats.json")
    assert result == initialize_empty_stats()


def test_save_and_load_stats_roundtrip():
    stats = initialize_empty_stats()
    stats["coding_agent"] = {"recent_outcomes": [True, False, True], "runs": 3, "successes": 2, "rate": 2 / 3}

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        tmp_path = f.name

    try:
        save_stats(stats, tmp_path)
        loaded = load_stats(tmp_path)
        assert loaded["coding_agent"]["runs"] == 3
        assert loaded["coding_agent"]["successes"] == 2
        assert loaded["coding_agent"]["recent_outcomes"] == [True, False, True]
    finally:
        os.unlink(tmp_path)


def test_load_stats_migrates_old_format_without_recent_outcomes():
    """Old agent_stats.json without recent_outcomes must load without crashing."""
    old_format = {
        "pm_agent": {"runs": 3, "successes": 2, "rate": 0.67},
        "architect_agent": {"runs": 0, "successes": 0, "rate": None},
        "coding_agent": {"runs": 0, "successes": 0, "rate": None},
        "testing_agent": {"runs": 0, "successes": 0, "rate": None},
        "review_agent": {"runs": 0, "successes": 0, "rate": None},
    }
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(old_format, f)
        tmp_path = f.name

    try:
        loaded = load_stats(tmp_path)
        assert "recent_outcomes" in loaded["pm_agent"]
        assert loaded["pm_agent"]["recent_outcomes"] == []  # migrated to empty list
    finally:
        os.unlink(tmp_path)


# -----------------------------------------------------------------------
# extract_failure_patterns
# -----------------------------------------------------------------------

def test_extract_failure_patterns_from_review_findings():
    task = make_task([make_history_entry("coding_agent", False)])
    task["review_result"] = {
        "passed": False,
        "findings": [
            {"checklist_item": "missing input validation", "file": "login.py", "line": 10, "severity": "high", "description": "..."},
            {"checklist_item": "hardcoded secrets", "file": "login.py", "line": 5, "severity": "critical", "description": "..."},
        ],
    }
    patterns = extract_failure_patterns(task, ["coding_agent"])
    assert "missing input validation" in patterns["coding_agent"]
    assert "hardcoded secrets" in patterns["coding_agent"]
