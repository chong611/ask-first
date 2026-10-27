# Security

agent-playbook is a guard: its hooks stop or question risky agent actions. A way around a guard is a security issue.

## Reporting
Report privately through GitHub's **Report a vulnerability** (Security tab → Advisories). Do not open a public issue for a bypass. Please include:
- the exact tool call, or the command line;
- what the hook decided (allow / ask / deny), and what you expected;
- the Claude Code version and the OS.

## Already known
`docs/ENFORCEMENT.md` lists each rule with how far it is enforced and its **known holes**. For example, a script file the agent writes and then runs is not inspected. Reports that only restate a known hole are welcome, but they are not new vulnerabilities.

## Design rules that keep it safe
- Any internal error on PreToolUse answers "ask", never "allow".
- In modes that show no prompt (bypassPermissions, dontAsk), an "ask" becomes "deny".
- The installer previews every change, backs up every file it edits, and never writes secrets.
- Tests never touch the network. The paid measurement tools need an explicit `--run` and a spending cap.
