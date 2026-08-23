# Agent Workspace Rules

## Branch Restrictions
- AI agents must never commit or edit any branches other than `development-v1` (or the project's designated dev branch).

## Jira Tracking (MANDATORY)

- **Ticket → Fix → Close**: Create a Jira ticket in `SCRUM` (rubixitsolutions.atlassian.net) BEFORE writing code, with a detailed description. No exceptions.
- **Commit format**: every commit MUST start with `SCRUM-XX: <description>` (enforced by the `prepare-commit-msg` git hook).
- **Sync**: run `python3 /home/rubix/workspace/_AI_AGENTS/sync_to_jira.py` after pushing to log time.
- **Reference**: `_AI_AGENTS/JIRA_SETUP.md` for the full workflow, epics, classification, and priority rules.
