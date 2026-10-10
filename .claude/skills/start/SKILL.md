---
description: Start a SkyRules AI work session for one team member. Confirms who they are and their git identity, checks setup and repo state, then proposes that member's next task from CLAUDE.md.
argument-hint: [your first name: Pavan | Dorai | Nayan | Sai | Adinath]
disable-model-invocation: true
---

Start a work session for team member: $ARGUMENTS

Follow these steps in order. Do not write or change any code in this command; end by proposing the next task and waiting for the member to say go.

1. **Who is this?** If no name was given above, ask: "Which team member are you: Pavan, Dorai, Nayan, Sai or Adinath?" Match the name to the team table in `CLAUDE.md` section 9.

2. **Read the rules:** `CLAUDE.md` (all of it), `docs/DELIVERABLES.md`, and, for Pavan, also `docs/UI_SPEC.md`.

3. **Check the git identity:** run `git config user.name`, `git config user.email` and `gh auth status`. If the GitHub account does not match this member's handle in section 9, stop and explain how to fix it (`gh auth login`, `git config user.name`). Commits must come from their own account.

4. **Sync:** `git fetch origin`, then report the current branch and whether there are uncommitted changes. If on `main` with no changes, run `git pull`.

5. **Check setup, without printing any secret:**
   - `.venv/` exists and `requirements.txt` installs. If not, offer to run `make setup`.
   - `.env` exists. Check only which keys are set, never their values; if it is missing, offer to copy `.env.example`.
   - Pavan only: `aws sts get-caller-identity --profile "$AWS_PROFILE"` succeeds. If not, explain `aws configure --profile <name>`.

6. **Find this member's progress:**
   - Run `gh pr list --author @me --state all`, `gh issue list --assignee @me` and `gh pr list --search "review-requested:@me"`.
   - Check which files from their playbook in `CLAUDE.md` section 9 already exist on `main`.
   - Show a short table of their tasks (ID, branch, status: done / in progress / not started / blocked by).
   - List any PRs waiting for their review first, since reviews unblock teammates.

7. **Propose the next task:**
   - Name the first not-done task in their playbook order, and say whether its upstream input is merged. If not, plan to use `tests/fixtures/`.
   - Give the branch name, the files you will create, the tests you will write, and how it will be checked against `docs/DELIVERABLES.md`.
   - Note any hard rule in `CLAUDE.md` section 2 that applies (for example the crawl rules, the golden set rule or the test split rule).

8. End with: "Say **go** to start this task, or name a different one." Then wait.
