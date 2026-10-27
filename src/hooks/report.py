"""Report check for core rule 1.3, and the deferral detector (S3, log-only).

Deterministic, line-based (linear time). It checks that:
  - all five headings are present (English or Chinese), outside code fences and quotes
  - each heading carries a count, e.g. "Done (2)", "**完成（2 项）**", "**Not done:** 0"
  - where top-level items are listed under a heading, their number equals the count
  - nothing but items follows the last heading (the report ends the reply)
It cannot check that each Done item's evidence really proves it (a grader's job).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import headings  # noqa: E402
import policy  # noqa: E402

LABELS = [(en, en) for en in headings.EN] + list(zip(headings.ZH, headings.EN))
LABELS.sort(key=lambda p: -len(p[0]))  # "Not done" before "Done", "未完成" before "完成"
COUNT = (r"\s*(?:[(（]\s*(\d+)\s*(?:项|個|个|items?)?\s*[)）]|[:：]\s*(\d+)(?:\s*(?:项|個|个|items?))?"
         r"|[·・•]\s*(\d+)(?:\s*(?:项|個|个|items?))?)\s*$")  # 完成（2） · 完成：2 · 完成 · 2
PREFIX = r"^\s{0,3}(?:#{1,6}\s*)?(?:\d+[.)]\s*)?(?:[-•]\s+)?"
HEADING_RE = [(re.compile(PREFIX + re.escape(label) + COUNT, re.I), canon) for label, canon in LABELS]
BARE_RE = [(re.compile(PREFIX + re.escape(label) + r"\s*[:：]?\s*$", re.I), canon) for label, canon in LABELS]
TOP_ITEM = re.compile(r"^\s{0,1}(?:[-*•+]|\d+[.)])\s+\S")
SUB_ITEM = re.compile(r"^\s{2,}\S")


@dataclass
class Report:
    counts: dict[str, int] = field(default_factory=dict)
    items: dict[str, list[str]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    uncounted: list[str] = field(default_factory=list)
    trailing: str = ""


def visible_lines(message: str) -> list[str]:
    """Lines outside ``` fences and > quotes, with * and _ emphasis removed."""
    out, fenced = [], False
    for line in message.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            out.append("")
            continue
        if fenced or line.lstrip().startswith(">"):
            out.append("")
            continue
        out.append(line.replace("**", "").replace("__", ""))
    return out


def match_heading(line: str):
    for rx, canon in HEADING_RE:
        m = rx.match(line)
        if m:
            return canon, int(m.group(1) or m.group(2) or m.group(3))
    for rx, canon in BARE_RE:
        if rx.match(line):
            return canon, None
    return None


def parse(message: str) -> Report:
    rep = Report()
    lines = visible_lines(message)
    found: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        hit = match_heading(line)
        if hit and hit[0] not in rep.counts and hit[0] not in rep.uncounted:
            canon, n = hit
            if n is None:
                rep.uncounted.append(canon)
            else:
                rep.counts[canon] = n
            found.append((i, canon))
    rep.missing = [en for en in headings.EN if en not in rep.counts and en not in rep.uncounted]
    for k, (start, canon) in enumerate(found):
        end = found[k + 1][0] if k + 1 < len(found) else len(lines)
        body = lines[start + 1:end]
        rep.items[canon] = [l.strip()[1:].strip() if not l.strip()[0].isdigit() else l.strip()
                            for l in body if TOP_ITEM.match(l)]
        if k + 1 == len(found):
            stray = [l for l in body if l.strip() and not TOP_ITEM.match(l) and not SUB_ITEM.match(l)]
            rep.trailing = stray[0].strip() if stray else ""
    return rep


def check(message: str) -> str | None:
    """Return a reason to continue the turn, or None if the report is acceptable."""
    rep = parse(message)
    problems = []
    if rep.missing:
        problems.append("missing headings: " + ", ".join(rep.missing))
    if rep.uncounted:
        problems.append("headings without a count: " + ", ".join(rep.uncounted))
    for canon, n in rep.counts.items():
        items = rep.items.get(canon, [])
        if items and len(items) != n:
            problems.append(f"{canon} count {n} but {len(items)} items listed")
        elif n == 0 and items:
            problems.append(f"{canon} count 0 but items listed")
    if rep.trailing and not rep.missing:
        problems.append(f"text after the report ({rep.trailing[:40]!r}); the report must end the reply")
    if not problems:
        return None
    labels = " · ".join(f"{e}/{z}" for e, z in zip(headings.EN, headings.ZH))
    return ("Rule 1.3: this turn changed files or state, so end the reply with the report. "
            + "; ".join(problems) + f". Headings: {labels}, each with a count; one top-level item per thing.")


# --- deferral detector (S3, log-only) ---------------------------------------------

DEFER_PATTERNS = [
    r"下次再", r"以后再", r"之后再(?:做|改|处理|修)", r"改天", r"留到下次", r"先不(?:做|处理|修|改)",
    r"暂(?:时)?不(?:做|处理|修)", r"稍后再", r"明天再", r"下一轮再", r"等下次",
    r"不急(?:今天|现在|着做)", r"先排队", r"到时(?:候)?(?:我)?再",
    r"\bnext time\b", r"\bin a (?:later|follow-?up) (?:pass|PR|change)\b", r"\bdefer(?:red|ring)?\b",
    r"\bpostpone[ds]?\b", r"\bleave (?:it|this|that) for (?:later|now)\b", r"\bdo (?:it|this|that) later\b",
]
DEFER_RE = re.compile("|".join(DEFER_PATTERNS), re.I)
QUOTED = re.compile(r"```.*?```|`[^`]*`|「[^」]*」|“[^”]*”|\"[^\"\n]*\"", re.S)
QUESTION_ID = re.compile(r"(?<![A-Za-z0-9])Q(\d+)[A-Z]?(?![A-Za-z0-9])")  # not \b: 选Q1A要 has none


def deferral_hits(message: str) -> list[str]:
    """Deferral phrases outside quotes/code, excluding lines that ask the user (carry a Q-ID)."""
    text = QUOTED.sub(" ", message)
    hits = []
    for line in text.splitlines():
        if QUESTION_ID.search(line) or line.lstrip().startswith(">"):
            continue
        if DEFER_RE.search(line):
            hits.append(line.strip()[:200])
    return hits


# --- structured questions (rule 2.2, Q78) -------------------------------------------

def interactive_client(env=None) -> bool:
    """True when the client has the AskUserQuestion tool (desktop app, CLI, IDE).

    Headless runs (claude -p, the SDK) set CLAUDE_CODE_ENTRYPOINT to sdk-*; unset is treated as headless.
    """
    entry = str((os.environ if env is None else env).get("CLAUDE_CODE_ENTRYPOINT") or "")
    return bool(entry) and not entry.lower().startswith("sdk")


def question_ids(text: str) -> set[str]:
    """Question numbers in text: "Q12A" and "Q12" both give "Q12"."""
    return {"Q" + n for n in QUESTION_ID.findall(text)}


# Explicit ways of leaving a decision to the user in the text (Q89A). Lines with a Q-ID are already questions.
ASK_PATTERNS = [
    r"(?:由你|请你|你来)(?:来)?(?:决定|定|选)", r"(?<!等)你决定(?:吧|一下|[。！？?!]|\s*$)",
    r"(?<!等)你(?:来)?决定[^。\n]{0,10}(?:哪|是否|要不要)", r"请你?选(?:择)?(?:一个|哪)", r"选哪(?:个|一个|种)[^。\n]{0,20}[？?]",
    r"要不要我", r"需不需要我", r"是否需要我", r"需要我[^。\n]{0,30}吗[？?]",
    r"(?:^|[，。；：,;:、\s])要不要[^。\n]{0,40}[？?]", r"(?:选|用|要)[^。\n？?]{0,15}还是[^。\n]{0,20}[？?]",
    r"(?<!不)(?:可以|好|行)吗[？?]",
    r"\bshould I\b[^.\n]{0,80}\?", r"\bshall (?:I|we)\b[^.\n]{0,80}\?", r"\b(?:do|would) you (?:want|like|prefer)\b",
    r"^\s*want me to\b", r"\bhow would you like\b", r"\b(?:it'?s|it is|that'?s) (?:your call|up to you)\b",
    r"\bwhich (?:one |option )?(?:do|would) you\b",
]
ASK_RE = re.compile("|".join(ASK_PATTERNS), re.I)


def prose_asks(message: str) -> list[str]:
    """Lines that leave a decision to the user, outside quotes, code and lines that carry a Q-ID."""
    return [line.strip()[:200] for line in QUOTED.sub(" ", message).splitlines()
            if not QUESTION_ID.search(line) and not line.lstrip().startswith(">") and ASK_RE.search(line)]


def ask_check(message: str, asked: bool, interactive: bool, asked_ids: set[str] = frozenset()) -> str | None:
    """Decisions must be asked as structured questions, not left as a line, a sentence or a card.

    Interactive clients: each Waiting on you item carries the ID of a question asked with
    AskUserQuestion this turn (asked_ids, Q90A), and a decision in the text needs a question too.
    Headless clients have no AskUserQuestion: each item and each decision needs a numbered question.
    """
    rep = parse(message)
    n = rep.counts.get("Waiting on you", 0)
    items = rep.items.get("Waiting on you", [])
    prose = prose_asks(message)
    if interactive:
        if not asked and (n or prose):
            what = f"Waiting on you lists {n} item(s)" if n else f"the reply leaves a decision to the user ({prose[0][:60]!r})"
            return (f"Rule 2.2: {what}, but no AskUserQuestion was asked this turn. "
                    "Ask each decision with AskUserQuestion (IDs Q1, Q1A; recommended option first). "
                    "A line under Waiting on you, a question in the text or a session card is not a question.")
        unasked = [i for i in items if not question_ids(i) & asked_ids]
        if unasked:
            return (f"Rule 2.2: {len(unasked)} Waiting on you item(s) carry no ID of a question asked with "
                    f"AskUserQuestion this turn ({unasked[0][:60]!r}). Ask each one with its ID in the question "
                    "(Q1, options Q1A/Q1B), and put that ID in the item.")
        return None
    unnumbered = [i for i in items if not question_ids(i)]
    if n and not items and not question_ids(message):
        unnumbered = ["(no items listed)"]
    if not unnumbered and not prose:
        return None
    what = f"Waiting on you lists {len(unnumbered)} item(s) without question IDs" if unnumbered else \
        f"the reply leaves a decision to the user ({prose[0][:60]!r})"
    return (f"Rule 2.2: {what}. "
            "Ask each one as a numbered question (Q1, options Q1A/Q1B, recommended option first).")


# --- reply language (Q134, log-only for now) ----------------------------------------

CODE_LIKE = re.compile(r"`[^`]*`|https?://\S+|[\w.-]*[/\\][\w./\\-]+|\b\w+\.(?:py|md|json|js|ts|sh|txt|csv)\b")
CJK_CHAR = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
LATIN_CHAR = re.compile(r"[A-Za-z]")


def mostly_english(message: str) -> tuple[int, int] | None:
    """(latin letters, CJK characters) of the prose when it is mostly English, else None.

    Code, quotes, inline code, paths and URLs are left out. One CJK character carries
    about as much as four letters, so the reply counts as English when CJK < letters / 4.
    """
    prose = CODE_LIKE.sub(" ", "\n".join(visible_lines(message)))
    latin, cjk = len(LATIN_CHAR.findall(prose)), len(CJK_CHAR.findall(prose))
    return (latin, cjk) if latin >= 200 and cjk < latin / 4 else None


# --- step-gating advice (Q68, remind only) -----------------------------------------

CHECK_CMD = re.compile(r"\b(pytest|unittest|check\.py|run\.sh|npm (run )?test|pnpm test|yarn test|go test|cargo test|"
                       r"make (test|check)|tox|ruff|mypy|eslint|tsc)\b")
FILTER = re.compile(r"^\s*(tail|head|grep|less|more|cut|sort|tee)\b")
RUNNER = {"uv": "run", "poetry": "run", "pipenv": "run", "pdm": "run", "hatch": "run", "npm": "exec"}
PASS_THROUGH = {"npx", "time", "env", "nice", "command", "exec", "sudo", "nohup"}


def runs_check(part: str) -> bool:
    """True if this pipeline stage's own command is a test or check (not, e.g., `cat run.sh`)."""
    words = part.split()
    while words:
        first = Path(words[0]).name.lower()
        if re.match(r"^[A-Za-z_]\w*=", words[0]) or first in PASS_THROUGH:
            words = words[1:]
        elif RUNNER.get(first) and words[1:2] == [RUNNER[first]]:
            words = words[2:]
        elif first == "timeout":
            words = words[2:]
        elif re.fullmatch(r"(python[\d.]*|py|sh|bash)(\.exe)?", first):
            words = words[1:]
            while words and words[0].startswith("-"):  # -m, -3, -u
                words = words[1:]
        else:
            break
    return bool(words) and bool(CHECK_CMD.match(" ".join([Path(words[0]).name] + words[1:3])))


def pipe_hides_exit(command: str) -> str | None:
    """A test/check piped into tail/head reports the pipe's exit code, not the check's."""
    if "pipefail" in command or "PIPESTATUS" in command:
        return None
    command, bodies = policy.split_heredocs(command)  # a heredoc fed to python is code, not commands
    command += "".join("\n" + body for head, body in bodies if re.search(r"\b(ba|z|da|k)?sh\s*(-\w+\s*)*$", head))
    for line in command.splitlines():
        for chain in re.split(r"\|\||&&|;", line):
            stages = chain.split("|")
            if any(runs_check(a) and FILTER.match(b) for a, b in zip(stages, stages[1:])):
                return ("This command pipes a check into a filter, so its exit status is the filter's, not the check's "
                        "(core 1.4). Use `set -o pipefail`, or run the check unpiped and read its status.")
    return None


PS_BLOCK = re.compile(r"```(?:powershell|ps1|pwsh|ps)\s*\n(.*?)```", re.S | re.I)


def powershell_and_and(message: str) -> str | None:
    """Windows PowerShell 5.1 has no &&: a chained step would run even after the previous one failed."""
    for block in PS_BLOCK.findall(message):
        if "&&" in block or "||" in block:
            return ("The PowerShell steps in your reply use && or ||, which Windows PowerShell 5.1 rejects. "
                    "Gate each step on the previous one, e.g. `if ($?) { ... }`, and say what success looks like (core 4.4).")
    return None
