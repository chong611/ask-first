---
name: pb-rehearse
description: Rehearse a plan before design work, a production change or a hard-to-reverse action (core rule 3.4). Splits the request into numbered requirements, argues the plan from several perspectives, states the strongest case against it, and names the check that would prove it worked. Use before starting such work, or when the user asks to rehearse, sandbox, "沙盘推演", or debate a plan.
---

# Rehearse before acting

Run this before the work, not after it. Keep the output short enough to read on a phone.

1. **Requirements.** Restate the request as a numbered list. Give each distinct deliverable its own number, and never merge two things the user named separately. Mark any item whose meaning you are unsure of, and ask about it with a question ID (core 2.2) before step 4.
2. **Perspectives.** Argue the plan from at least three viewpoints that matter for this task. Use one insider (someone who builds or runs it) and one outsider (the person who uses it, or someone who pays for a failure). Add a third from the domain. For each, give one concrete concern, not a generic one.
3. **Strongest case against.** In two to four sentences, make the best argument that this plan is wrong or not worth doing. If it survives, say why. If it does not, change the plan.
4. **Proof.** For each numbered requirement, name the check that would fail if it were not met (core 1.4). If a check can't run here, say so now, so the final report can list it under Unverified.
5. **Then start.** Once the questions that block work are answered, begin in the same turn. Rehearsing is not a reason to stop (core 2.1).

Output, in the user's language:

```
Requirements: 1. … 2. …
Perspectives: insider … / outsider … / domain …
Strongest case against: …
Proof per requirement: 1 → … 2 → …
Open questions: Q… (or none)
```
