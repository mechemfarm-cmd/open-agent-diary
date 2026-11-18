#!/usr/bin/env bash
# Refuse to publish personal or machine identifiers from this tree.
#
# This repository is public. Design and run documents written as internal records
# keep leaking the operator's account, home path, host names and internal service
# names into it — twice now, in two different forms. A review catches it only if
# somebody remembers; this catches it every time.
#
# Usage:
#   scripts/check-public-tree.sh          # scan the working tree
#   scripts/check-public-tree.sh --staged # scan what is about to be committed
#
# Exit 0 clean, 1 with the offending lines printed.
set -euo pipefail

# Identifiers that must never appear in a published file. Extend deliberately:
# every entry is something whose appearance means a real person's machine or
# account is in the tree.
PATTERNS=(
  '\bwillard\b'          # operator account
  '\bwillardmechem\b'
  '\b(Bill|Alice|Tom)\b' # operator, reviewer, implementer by name
  '\b(Emily|Art|Lucy|blanka|David)\b'  # host names
  '\bnetmap-wifi\b'      # internal service naming
  'sudoers\.d/[a-z0-9-]+'  # specific grant filenames
  # No generic /home/ rule: tests legitimately use placeholders such as
  # /home/user and /home/example. The operator account is caught by name, which
  # is what actually leaked — the path always contained it.
)

# Deliberate exceptions: author attribution in licence/package metadata is
# correct and is not an operational leak.
ALLOW=(
  'LICENSE'
  'pyproject.toml'
  'package.json'
  'Cargo.toml'
)

if [[ "${1:-}" == "--staged" ]]; then
  files=$(git diff --cached --name-only --diff-filter=ACM)
else
  files=$(git ls-files)
fi

status=0
for f in $files; do
  [[ -f "$f" ]] || continue
  # The guard necessarily names what it forbids; scanning itself is a false positive.
  [[ "$f" == "scripts/check-public-tree.sh" ]] && continue
  for allowed in "${ALLOW[@]}"; do
    [[ "$f" == *"$allowed" ]] && continue 2
  done
  for pat in "${PATTERNS[@]}"; do
    while IFS= read -r hit; do
      [[ -z "$hit" ]] && continue
      printf '  %s:%s\n' "$f" "$hit"
      status=1
    done < <(grep -EnI "$pat" "$f" 2>/dev/null || true)
  done
done

if [[ $status -ne 0 ]]; then
  cat >&2 <<'EOF'

  Refusing: the lines above carry personal or machine identifiers into a public
  repository. Replace them with roles, labels or $HOME, then run this again.
EOF
fi

exit $status
