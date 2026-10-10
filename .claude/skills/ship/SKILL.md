---
description: Finish the current SkyRules AI task. Runs lint and tests, checks for secrets, explains the diff to the member, then commits, pushes and opens a pull request with the AI usage line and the right reviewer.
argument-hint: [optional issue number to close]
disable-model-invocation: true
---

Ship the current task. Issue to close (optional): $ARGUMENTS

1. **Branch check:** confirm the current branch is a feature branch, not `main`. If on `main`, stop: create the right branch from `CLAUDE.md` section 9 first and move the changes there.

2. **Quality gate:** run `make lint test`. If anything fails, fix it and run again. If it still fails after a real attempt, stop and explain what is failing instead of shipping.

3. **Safety checks on the diff** (`git diff main...HEAD` plus uncommitted changes):
   - **Secrets:** no `.env`, no AWS keys, tokens or passwords. Search the diff for `AKIA`, `aws_secret`, `secret_access_key`, `token`, `password`. Any hit: stop.
   - **No large data files:** nothing under `data/raw/`, `data/chunks/`, `data/index/`, `chroma/`, `.cache/` or `logs/`.
   - **No golden set items written by Claude** (`CLAUDE.md` rule 4) and no test-split runs (rule 5).
   - **Report numbers:** any number added to `reports/` comes from a file in `reports/artifacts/` (rule 6).

4. **Explain before committing:**
   - Show `git diff --stat`.
   - Give a short plain-language explanation of what the code does, and the 2 to 4 things the member should check or be ready to explain in Q&A.
   - Ask: "Commit and open the PR? (yes / changes)". Wait for yes.

5. **Commit:** stage only this task's files, with a present-tense, type-prefixed message that says what and why (for example `feat: add robots-aware crawler with 1 req/s limit`). Never "update" or "wip".

6. **Push and open the PR:**
   - `git push -u origin <branch>`
   - `gh pr create` with a title matching the commit and this body:
     ```
     ## What and why
     <2 to 4 lines>

     ## How it was tested
     <commands run and results>

     ## Checks against docs/DELIVERABLES.md
     <which checklist items this covers>

     ## AI usage
     Claude Code drafted <files>; <member name> reviewed, ran the tests and <what they changed or checked>.

     Closes #<issue>   (only if an issue number was given)
     ```
   - Add the reviewer for this member from the table in `CLAUDE.md` section 9: `gh pr edit --add-reviewer <handle>`.

7. **Wrap up:** print the PR link and remind the member to move the issue on the project board. Then suggest running `/start <name>` again to pick the next task.
