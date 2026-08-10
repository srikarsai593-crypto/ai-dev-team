# AI Dev Team

Multi-agent system that plans, codes, tests, and reviews small features
end-to-end, with a Manager + Reflection layer that detects underperforming
agents and rewrites their prompts to improve them over time.

## Structure
- agents/ - one folder per agent (PM, Architect, Coding, Testing, Review, Manager)
- orchestration/ - pipeline chaining logic
- schemas/ - shared task_schema.json contract all agents read/write
- dashboard/ - live success-rate + prompt-diff dashboard
- sample_repo/ - the target codebase the agents operate on
- docs/ - design notes, meeting decisions
