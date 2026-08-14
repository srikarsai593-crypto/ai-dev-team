"""
app.py — AI Dev Team Live Dashboard
Three-tab Streamlit app:
  Tab 1 — Success Rate Graph: per-agent line chart across pipeline runs
  Tab 2 — Prompt Diff View: before/after diff for any Reflection Agent rewrite
  Tab 3 — Needs Your Review: tasks awaiting human approval or blocked at retry cap

Run with: streamlit run dashboard/app.py
"""
import difflib
import json
import os
import sys
import time

import plotly.graph_objects as go
import streamlit as st

# -- Paths -----------------------------------------------------------------------
DASHBOARD_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(DASHBOARD_DIR, ".."))
RUN_HISTORY_PATH = os.path.join(DASHBOARD_DIR, "run_history.json")
TASKS_DIR = os.path.join(DASHBOARD_DIR, "tasks")
PROMPT_HISTORY_DIR = os.path.join(REPO_ROOT, "agents", "reflection_agent", "prompt_history")

TRACKED_AGENTS = ["pm_agent", "architect_agent", "coding_agent", "testing_agent", "review_agent"]
AGENT_COLORS = {
    "pm_agent":        "#3b82d4",
    "architect_agent": "#7c5cd8",
    "coding_agent":    "#e25c5c",
    "testing_agent":   "#2ecc71",
    "review_agent":    "#f39c12",
}

# ──────────────────────────────────────────────────────────────────────────────
# Data loaders
# ──────────────────────────────────────────────────────────────────────────────

def load_run_history() -> list:
    """Load run_history.json. Returns empty list if file doesn't exist or is invalid."""
    if not os.path.exists(RUN_HISTORY_PATH):
        return []
    try:
        with open(RUN_HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return []


def get_prompt_versions(agent_name: str) -> list:
    """
    Return sorted list of (version_int, file_path) tuples for an agent's prompt history.
    """
    if not os.path.exists(PROMPT_HISTORY_DIR):
        return []
    import re
    pattern = re.compile(rf"^{re.escape(agent_name)}_v(\d+)\.md$")
    versions = []
    for fname in os.listdir(PROMPT_HISTORY_DIR):
        match = pattern.match(fname)
        if match:
            versions.append((int(match.group(1)), os.path.join(PROMPT_HISTORY_DIR, fname)))
    return sorted(versions, key=lambda x: x[0])


def load_prompt_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# Tab 1 — Success Rate Graph
# ──────────────────────────────────────────────────────────────────────────────

def render_success_rate_tab():
    st.header("Agent Success Rates Over Time")
    st.caption("Updates automatically every 5 seconds as the pipeline runs.")

    run_history = load_run_history()

    if not run_history:
        st.info(
            "No pipeline runs recorded yet. "
            "Run the pipeline with:\n\n"
            "```\npython orchestration/pipeline.py --request \"Your feature request\"\n```"
        )
        return

    # Build x-axis (run IDs) and per-agent y-axis (rates)
    run_ids = [r["run_id"] for r in run_history]

    fig = go.Figure()

    for agent in TRACKED_AGENTS:
        rates = []
        for run in run_history:
            agent_rates = run.get("agent_rates", {})
            rates.append(agent_rates.get(agent))  # None if agent hasn't run yet

        # Replace None with previous value (forward-fill) for cleaner graph
        filled = []
        last = None
        for r in rates:
            if r is not None:
                last = r
            filled.append(last)

        fig.add_trace(go.Scatter(
            x=run_ids,
            y=filled,
            mode="lines+markers",
            name=agent,
            line=dict(color=AGENT_COLORS.get(agent, "#888"), width=2),
            marker=dict(size=6),
            hovertemplate=f"<b>{agent}</b><br>Run: %{{x}}<br>Success rate: %{{y:.0%}}<extra></extra>",
        ))

    # Underperform threshold line — canonical rule: rate < 0.6 AND runs >= 3
    fig.add_hline(
        y=0.6,
        line_dash="dash",
        line_color="#e25c5c",
        annotation_text="Threshold (60%, min. 3 runs)",
        annotation_position="bottom right",
    )

    fig.update_layout(
        xaxis_title="Pipeline Run #",
        yaxis_title="Success Rate",
        yaxis=dict(range=[0, 1.05], tickformat=".0%"),
        xaxis=dict(tickmode="linear", dtick=1),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=420,
        margin=dict(l=40, r=40, t=30, b=40),
        plot_bgcolor="#f7f8fa",
        paper_bgcolor="#ffffff",
    )

    st.plotly_chart(fig, use_container_width=True)

    # Stats table
    st.subheader("Latest Run Summary")
    latest = run_history[-1]
    cols = st.columns(len(TRACKED_AGENTS))
    for i, agent in enumerate(TRACKED_AGENTS):
        rate = latest.get("agent_rates", {}).get(agent)
        rate_str = f"{rate:.0%}" if rate is not None else "N/A"
        color = "normal"
        if rate is not None:
            color = "inverse" if rate < 0.6 else "normal"
        with cols[i]:
            st.metric(
                label=agent.replace("_", " ").title(),
                value=rate_str,
                delta=None,
            )

    if latest.get("reflections_triggered"):
        st.warning(
            f"⚠ Reflection Agent triggered for: **{', '.join(latest['reflections_triggered'])}** "
            f"(run #{latest['run_id']})"
        )

    # Run history table
    with st.expander("All Run Records"):
        st.json(run_history)


# ──────────────────────────────────────────────────────────────────────────────
# Tab 2 — Prompt Diff View
# ──────────────────────────────────────────────────────────────────────────────

def render_prompt_diff_tab():
    st.header("Reflection Agent — Prompt Diff View")
    st.caption(
        "Shows before/after diffs for every system prompt rewrite. "
        "Each rewrite traces to a specific failure pattern identified by the Manager Agent."
    )

    # Agent selector
    agents_with_history = []
    for agent in TRACKED_AGENTS:
        if get_prompt_versions(agent):
            agents_with_history.append(agent)

    if not agents_with_history:
        st.info(
            "No prompt rewrites yet. The Reflection Agent triggers automatically when an agent's "
            "success rate drops below 60% across 3+ runs.\n\n"
            "To test manually: set `review_passed = False` in `orchestration/pipeline.py` "
            "and run the pipeline 4+ times."
        )
        return

    selected_agent = st.selectbox(
        "Select agent",
        options=agents_with_history,
        format_func=lambda a: a.replace("_", " ").title(),
    )

    versions = get_prompt_versions(selected_agent)

    if len(versions) < 2:
        st.info(
            f"Only one version exists for **{selected_agent}** (the baseline). "
            "Run the pipeline more times to trigger a rewrite."
        )
        st.subheader("Current prompt (v0 — baseline)")
        st.code(load_prompt_file(versions[0][1]), language="markdown")
        return

    # Version pair selector
    version_labels = [f"v{v}" for v, _ in versions]
    col1, col2 = st.columns(2)
    with col1:
        from_label = st.selectbox("From version", options=version_labels[:-1], index=0)
    with col2:
        to_label = st.selectbox(
            "To version",
            options=version_labels[1:],
            index=len(version_labels) - 2,
        )

    from_version = int(from_label[1:])
    to_version = int(to_label[1:])

    if from_version >= to_version:
        st.error("'From' version must be earlier than 'To' version.")
        return

    from_path = next(p for v, p in versions if v == from_version)
    to_path = next(p for v, p in versions if v == to_version)

    from_text = load_prompt_file(from_path)
    to_text = load_prompt_file(to_path)

    # Unified diff
    diff_lines = list(difflib.unified_diff(
        from_text.splitlines(keepends=True),
        to_text.splitlines(keepends=True),
        fromfile=f"{selected_agent}_{from_label}.md",
        tofile=f"{selected_agent}_{to_label}.md",
        lineterm="",
    ))

    if not diff_lines:
        st.success("No changes between these versions.")
    else:
        st.subheader(f"Diff: {from_label} → {to_label}")
        diff_str = "".join(diff_lines)
        st.code(diff_str, language="diff")

    # Side-by-side view
    st.subheader("Side-by-side comparison")
    left, right = st.columns(2)
    with left:
        st.markdown(f"**{from_label} (before)**")
        st.code(from_text, language="markdown")
    with right:
        st.markdown(f"**{to_label} (after)**")
        st.code(to_text, language="markdown")


# -----------------------------------------------------------------------
# Tab 3 — Needs Your Review
# -----------------------------------------------------------------------

def load_tasks() -> list:
    """
    Load all persisted task JSONs from dashboard/tasks/.
    Returns list of task dicts, sorted by task_id.
    """
    if not os.path.exists(TASKS_DIR):
        return []
    tasks = []
    for fname in os.listdir(TASKS_DIR):
        if not fname.endswith(".json"):
            continue
        path = os.path.join(TASKS_DIR, fname)
        try:
            with open(path, "r", encoding="utf-8") as f:
                tasks.append(json.load(f))
        except (json.JSONDecodeError, ValueError):
            continue
    return sorted(tasks, key=lambda t: t.get("task_id", ""))


def render_needs_review_tab():
    st.header("Needs Your Review")
    st.caption(
        "Tasks that are either awaiting human approval (pipeline passed) or "
        "blocked (hit the 2-retry cap and could not be auto-resolved)."
    )

    tasks = load_tasks()

    # Filter to tasks needing human attention
    review_statuses = {"awaiting_human_approval", "blocked"}
    pending = [t for t in tasks if t.get("status") in review_statuses]

    if not pending:
        st.info(
            "No tasks waiting for review right now.\n\n"
            "Tasks appear here when the pipeline finishes a run "
            "(status: **awaiting_human_approval**) or hits the retry cap "
            "(status: **blocked**)."
        )
        # Show all completed tasks as a reference
        if tasks:
            with st.expander(f"All saved tasks ({len(tasks)})"):
                for t in tasks:
                    st.markdown(f"- `{t['task_id']}` — {t.get('status', 'unknown')}")
        return

    # Group by status for clarity
    awaiting = [t for t in pending if t.get("status") == "awaiting_human_approval"]
    blocked = [t for t in pending if t.get("status") == "blocked"]

    if awaiting:
        st.subheader(f"Awaiting Approval ({len(awaiting)})")
        for task in awaiting:
            with st.expander(
                f"[APPROVE?]  {task['task_id']} — {task.get('feature_request', '')[:80]}",
                expanded=True,
            ):
                col1, col2 = st.columns([2, 1])
                with col1:
                    st.markdown(f"**Feature request:** {task.get('feature_request', 'N/A')}")
                    st.markdown("**Acceptance criteria:**")
                    for crit in task.get("acceptance_criteria", []):
                        st.markdown(f"- {crit}")
                with col2:
                    st.metric("Retries used", task.get("retry_count", 0))
                    st.metric("Status", task.get("status", "N/A"))

                if task.get("code_diff"):
                    st.markdown("**Code diff:**")
                    st.code(task["code_diff"], language="diff")

                review = task.get("review_result", {})
                if review and review.get("findings"):
                    st.markdown("**Review findings (informational):**")
                    for f in review["findings"]:
                        sev = f.get("severity", "?").upper()
                        st.markdown(
                            f"- `[{sev}]` **{f.get('checklist_item', '?')}** "
                            f"— {f.get('file', '?')}:{f.get('line', '?')}"
                        )
                else:
                    st.success("Review passed with no findings.")

    if blocked:
        st.subheader(f"Blocked — Hit Retry Cap ({len(blocked)})")
        for task in blocked:
            with st.expander(
                f"[BLOCKED]  {task['task_id']} — {task.get('feature_request', '')[:80]}",
                expanded=True,
            ):
                st.error(
                    f"This task was rejected by the Review Agent {task.get('retry_count', 0)+1} "
                    f"time(s) and hit the hard cap of {task.get('retry_count', 2)} retries. "
                    "Manual intervention required."
                )
                st.markdown(f"**Feature request:** {task.get('feature_request', 'N/A')}")

                review = task.get("review_result", {})
                if review and review.get("findings"):
                    st.markdown("**Last review findings (why it was rejected):**")
                    for f in review["findings"]:
                        sev = f.get("severity", "?").upper()
                        st.markdown(
                            f"- `[{sev}]` **{f.get('checklist_item', '?')}** "
                            f"— {f.get('file', '?')}:{f.get('line', '?')} — "
                            f"{f.get('description', '')}"
                        )
                else:
                    st.warning("No structured review findings saved for this task.")

                if task.get("code_diff"):
                    with st.expander("View last code diff"):
                        st.code(task["code_diff"], language="diff")


# -----------------------------------------------------------------------
# App layout
# -----------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="AI Dev Team Dashboard",
        page_icon="AI",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    st.title("AI Dev Team -- Live Dashboard")
    st.markdown(
        "Tracks per-agent success rates across pipeline runs and shows "
        "Reflection Agent prompt rewrites in real time."
    )

    tab1, tab2, tab3 = st.tabs(["Success Rates", "Prompt Diffs", "Needs Your Review"])

    with tab1:
        render_success_rate_tab()

    with tab2:
        render_prompt_diff_tab()

    with tab3:
        render_needs_review_tab()

    # Auto-refresh every 5 seconds
    time.sleep(5)
    st.rerun()


if __name__ == "__main__":
    main()
