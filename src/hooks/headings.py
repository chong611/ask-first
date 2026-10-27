"""Report headings required by core rule 1.3.

The M2 Stop hook imports these, so the rule text and the hook cannot drift
apart; tools/check.py fails if either source file stops matching.
Lives next to the hooks because install copies src/hooks/ into .claude/hooks/.
"""

EN = ("Done", "Not done", "Unverified", "Found but not fixed", "Waiting on you")
ZH = ("完成", "未完成", "未验证", "发现未修", "等你决定")
