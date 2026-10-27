---
name: lessons
description: Turn a user correction into a proposed rule change, for the user to approve. Use right after the user corrects you, when the same mistake happens twice, or when the user asks for a retrospective. Outside the agent-playbook repo it only notes the lesson in one line and goes back to the user's task. Never writes a rule without explicit approval.
user-invocable: false
---

# Lessons loop (DECISIONS Q34, Q92)

0. **Check where you are.** The agent-playbook repo has `src/rules/core.md` and `tools/build.py`. If the current project is anything else:
   - Do not open, edit or run anything of the playbook.
   - Append one line to `~/.claude/agent-playbook/lessons-inbox.md`: the date, the project folder, the user's correction quoted, and the fix in one sentence. Append with the shell (`>>` in Bash, `Add-Content -Encoding utf8` in PowerShell); never rewrite the file.
   - Tell the user in one sentence that the lesson was noted for the playbook, then go back to their task.

   Stop here. Steps 1 to 5 run only in the agent-playbook repo. Start there by reading the inbox, and propose its items one by one. Remove an item only after the user has decided on it.

1. **Name the mistake** in one line, quoting the user's correction.
2. **Choose the strongest fix that can enforce it.** Try them in this order:
   - a hook or test (`src/hooks/`, `tests/`);
   - a profile line;
   - a core rule;
   - a line in the project's handoff.

   Prose is the last resort, because in the sessions this playbook was mined from, rules alone did not stop repeats.
3. **Write the proposal.** It has four parts:
   - the exact text or diff;
   - where it goes;
   - the evidence (quote and date);
   - which existing line it replaces.

   Core has a 6,200-byte budget (`python3 tools/check.py`), so adding a line means removing one.
4. **Ask the user** with a structured question (core 2.2): approve, edit, or reject. Changing your own instructions is tier-2 (core 2.3). Write nothing before the user answers.
5. **After approval**, apply the change, add a row to `docs/LESSONS.md` (create it if it is missing), and run `python3 tools/build.py && python3 tools/check.py`. Report the result.
