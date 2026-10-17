#!/usr/bin/env bash
# PLAT-4251: reject curl|sh / wget|sh install patterns in CI workflows.
# Also require install_helm (when present) to pin a SHA-256 before extract.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WF_DIR="${ROOT}/.forgejo/workflows"
fail=0

if [[ ! -d "$WF_DIR" ]]; then
  echo "no .forgejo/workflows — skip"
  exit 0
fi

# Strip comments so documentation of the ban does not self-fail.
strip_comments() {
  sed -E 's/[[:space:]]+#.*$//; /^[[:space:]]*#/d' "$1"
}

# curl/wget as a command (whitespace after), then pipe to sh/bash on same line.
PIPE_RE='(curl|wget)[[:space:]][^|]*\|\s*(ba)?sh\b'

shopt -s nullglob
for f in "$WF_DIR"/*.yml "$WF_DIR"/*.yaml; do
  [[ -f "$f" ]] || continue
  body="$(strip_comments "$f")"
  if printf '%s\n' "$body" | grep -nE "$PIPE_RE" >/dev/null 2>&1; then
    echo "::error file=${f#$ROOT/}::curl|sh / wget|sh install pattern forbidden (PLAT-4251)"
    printf '%s\n' "$body" | grep -nE "$PIPE_RE" || true
    fail=1
  fi
  # Multiline: curl/wget command line, then | bash/sh within 5 lines
  if printf '%s\n' "$body" | awk '
    BEGIN { found=0 }
    /(curl|wget)[[:space:]]/ { pending=5; next }
    pending>0 {
      if ($0 ~ /\|\s*(ba)?sh\b/) { print NR ": " $0; found=1 }
      pending--
      next
    }
    END { exit found ? 0 : 1 }
  '; then
    echo "::error file=${f#$ROOT/}::multiline curl|sh pattern forbidden (PLAT-4251)"
    fail=1
  fi
done

DEPLOY="${WF_DIR}/deploy.yml"
if [[ -f "$DEPLOY" ]] && grep -q 'install_helm()' "$DEPLOY"; then
  if ! grep -q 'sha256sum -c' "$DEPLOY"; then
    echo "::error file=.forgejo/workflows/deploy.yml::install_helm must verify SHA-256 (PLAT-4251)"
    fail=1
  fi
  if ! grep -qE 'HELM_SHA=' "$DEPLOY"; then
    echo "::error file=.forgejo/workflows/deploy.yml::install_helm must pin HELM_SHA (PLAT-4251)"
    fail=1
  fi
fi

if [[ "$fail" -ne 0 ]]; then
  echo "PLAT-4251 static check FAILED"
  exit 1
fi
echo "PLAT-4251 static check OK (no curl|sh; helm checksum present when install_helm exists)"
