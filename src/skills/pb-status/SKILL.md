---
name: pb-status
description: Full-session status. Lists everything done, not done, unverified, found but not fixed, and waiting on the user, and re-checks earlier "done" claims and promises. Use when the user asks what is still owed, what is finished, for the complete list, "还有什么没做完", "全部清单", or "status".
---

# Session status

This covers the whole session, not just the last reply.

1. Collect every claim of done, fixed or passed, every promise ("I will…") and every question the user asked in this session. After a compaction, also use the open items the hook restated.
2. **Re-check each Done claim now.** Re-run its check, or open the output it refers to. A claim whose evidence you can't reproduce moves to Unverified. Don't trust earlier replies, including your own.
3. A promise counts as delivered only if the item exists now; otherwise it goes under Not done. A question you never answered goes under Not done too.
4. Include anything you found but did not fix, inside or outside the task, and everything waiting on the user, with its question ID.

Start with the single most important next action, in one line. Then give the report with the five headings from core 1.3, each with a count, and end the reply with it. Put one top-level line per item: the item, then the evidence, or why it is not done.
