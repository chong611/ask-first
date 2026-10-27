#!/bin/sh
# Cloud environment setup script (DECISIONS Q132). Paste this whole file into the
# environment's settings under "Setup script". Every new cloud session then starts
# with agent-playbook installed in the container's ~/.claude, as on a local install.
# It needs chong611/ask-first under the environment's Repositories. It never
# fails the environment: on any problem it prints one line and the session starts anyway.
set -u
PB_PROFILES=""   # optional profiles, e.g. PB_PROFILES="lang-zh-CN advisor"
PB_REPO="https://github.com/chong611/ask-first"
SRC=""
NAME="${PB_REPO##*/}"   # the folder a cloud session clones the repo into
for d in "/home/user/$NAME" "$HOME/$NAME"; do
  [ -f "$d/tools/install.py" ] && SRC="$d" && break
done
TMP=""
if [ -z "$SRC" ]; then
  TMP=$(mktemp -d)
  if git clone -q --depth 1 "$PB_REPO" "$TMP/pb" 2>/dev/null; then
    SRC="$TMP/pb"
  else
    echo "agent-playbook: not installed (clone failed; add the repo under the environment's Repositories)"
  fi
fi
if [ -n "$SRC" ]; then
  FLAGS=""
  for p in $PB_PROFILES; do FLAGS="$FLAGS --profile $p"; done
  if (cd "$SRC" && python3 tools/install.py global $FLAGS --yes >/dev/null 2>&1); then
    echo "agent-playbook: installed into $HOME/.claude"
  else
    echo "agent-playbook: install failed"
  fi
fi
[ -n "$TMP" ] && rm -rf "$TMP"
exit 0
