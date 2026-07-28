#!/usr/bin/env bash
# ---------------------------------------------------------------
# Run this from inside the repo BEFORE your first push, and again
# before flipping the repo to public.
#   chmod +x scrub_check.sh && ./scrub_check.sh
# ---------------------------------------------------------------
set -uo pipefail

RED=$'\033[0;31m'; YEL=$'\033[0;33m'; GRN=$'\033[0;32m'; NC=$'\033[0m'
FINDINGS=0

banner() { echo; echo "=== $1 ==="; }
flag()   { echo "${RED}[!]${NC} $1"; FINDINGS=$((FINDINGS+1)); }
ok()     { echo "${GRN}[ok]${NC} $1"; }
warn()   { echo "${YEL}[?]${NC} $1"; }

# Anything matching these in tracked files is suspicious.
PATTERNS=(
  'password[[:space:]]*=[[:space:]]*["'"'"'][^"'"'"']'
  'passwd[[:space:]]*=[[:space:]]*["'"'"'][^"'"'"']'
  'secret[[:space:]]*=[[:space:]]*["'"'"'][^"'"'"']'
  'token[[:space:]]*=[[:space:]]*["'"'"'][^"'"'"']'
  'api_?key[[:space:]]*=[[:space:]]*["'"'"'][^"'"'"']'
  'client_secret'
  'refresh_token[[:space:]]*=[[:space:]]*["'"'"']'
  'Bearer [A-Za-z0-9._-]\{20,\}'
  'eyJ[A-Za-z0-9._-]\{20,\}'          # JWT
  'https://hooks\.[A-Za-z0-9.-]*/'     # webhook URLs
  'discord\.com/api/webhooks'
  'api\.telegram\.org/bot[0-9]'
)

banner "1. Is .env tracked by git?"
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  flag ".env IS TRACKED. Run: git rm --cached .env"
else
  ok ".env is not tracked"
fi

banner "2. Scanning tracked files for hardcoded secrets"
for p in "${PATTERNS[@]}"; do
  hits=$(git grep -nIEi --untracked -- "$p" 2>/dev/null \
          | grep -v -e '\.env\.example' -e 'scrub_check.sh' -e 'README.md' || true)
  if [[ -n "$hits" ]]; then
    flag "pattern: $p"
    echo "$hits" | sed 's/^/      /'
  fi
done
[[ $FINDINGS -eq 0 ]] && ok "no obvious hardcoded secrets in tracked files"

banner "3. Files that should never be committed"
for f in .env sim_state.json credentials.json token.json; do
  if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    flag "$f is tracked — remove it: git rm --cached $f"
  fi
done
if git ls-files | grep -qE '\.(log|csv|parquet)$'; then
  warn "log/data files are tracked:"
  git ls-files | grep -E '\.(log|csv|parquet)$' | sed 's/^/      /'
fi

banner "4. Scanning git HISTORY (secrets survive deletion)"
if [[ -d .git ]] && git rev-parse HEAD >/dev/null 2>&1; then
  hist=$(git log --all -p --no-color 2>/dev/null \
          | grep -inE 'client_secret|refresh_token|api_?key *= *["'"'"']|password *= *["'"'"']' \
          | head -20 || true)
  if [[ -n "$hist" ]]; then
    flag "possible secrets in commit history:"
    echo "$hist" | sed 's/^/      /'
    echo "      -> Easiest fix: start a fresh repo with no history (see README of this kit)."
  else
    ok "nothing obvious in history"
  fi
else
  ok "no commit history yet — cleanest possible starting point"
fi

banner "5. Account identifiers"
acct=$(git grep -nIEi '(account_?id|accountId|acct)[[:space:]]*[=:][[:space:]]*["'"'"'][A-Za-z0-9]' 2>/dev/null \
        | grep -v -e '\.env\.example' -e 'scrub_check.sh' || true)
if [[ -n "$acct" ]]; then
  warn "possible account IDs — confirm these are placeholders:"
  echo "$acct" | sed 's/^/      /'
else
  ok "no hardcoded account identifiers found"
fi

echo
if [[ $FINDINGS -gt 0 ]]; then
  echo "${RED}${FINDINGS} issue(s) found. Fix before pushing.${NC}"
  exit 1
else
  echo "${GRN}Clean. Safe to push.${NC}"
  exit 0
fi
