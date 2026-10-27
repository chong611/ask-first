---
name: pb-handoff
description: Write a handoff pack that lets a fresh agent continue the work by reading only this pack. Use when the user asks for a handoff, a relay or succession document, "交接", "打包给下一个 agent", or when a session is ending with unfinished work.
---

# Handoff pack

Write or update `docs/HANDOFF.md`, or the handoff file the project already uses. Run this whenever the user asks for one. When a session is ending with open work, offer it first. Writing into the repo is reversible, but publishing or sending it anywhere else is tier-2.

The pack must let a fresh agent continue without this conversation:

1. **What this is and for whom.** One paragraph. Include the user's working rules, or point to the playbook profile in use.
2. **Current state.** Version, branch, and what runs. Then the verification table: each item marked verified (with the command and result), unverified (with the reason), or failed.
3. **Open items.** Not done, found but not fixed, and waiting on the user, each with its question ID. Copy them from the latest report.
4. **How to start.** Exact commands to set up, run the tests and run the app, and what output to expect.
5. **Rules and traps.** Project rules, and lessons learned with what each one cost.
6. **Where things are.** A file map limited to what the next agent needs.

Afterwards, check it like a stranger would. Read only the pack, follow "How to start" in a fresh shell if you can, and fix every step that fails. Report that check under Done or Unverified.
