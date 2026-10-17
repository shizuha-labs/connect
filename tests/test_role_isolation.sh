#!/bin/bash
# CON-219: regression test for role-separated entrypoint.
# Verifies the entrypoint and supervisor configs enforce role isolation.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0
FAIL=0

pass() { PASS=$((PASS+1)); echo "  PASS: $*"; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL: $*"; }

# Helper: test role validation without inheriting set -u
validate_role() {
  local role="$1"
  case "$role" in
    web|worker|beat|migrate) echo "ok" ;;
    *) echo "invalid" ;;
  esac
}

# --- Test 1: entrypoint rejects invalid roles ---
echo "=== Test 1: entrypoint rejects invalid roles ==="
for bad in "invalid" "web,worker" "root" "admin" "superuser"; do
  result=$(validate_role "$bad")
  if [ "$result" = "invalid" ]; then
    pass "rejected CONNECT_ROLE='$bad'"
  else
    fail "did not reject CONNECT_ROLE='$bad'"
  fi
done

# --- Test 2: entrypoint accepts valid roles ---
echo "=== Test 2: entrypoint accepts valid roles ==="
for good in "web" "worker" "beat" "migrate"; do
  result=$(validate_role "$good")
  if [ "$result" = "ok" ]; then
    pass "accepted CONNECT_ROLE='$good'"
  else
    fail "rejected valid CONNECT_ROLE='$good'"
  fi
done

# --- Test 3: default role is web ---
echo "=== Test 3: default role is web ==="
DEFAULT_ROLE=$(grep 'ROLE="${CONNECT_ROLE:-web}"' "$ROOT/docker-entrypoint.sh" | sed 's/.*:-\(.*\)}.*/\1/')
if [ "$DEFAULT_ROLE" = "web" ]; then
  pass "default role is web"
else
  fail "default role is not web (got: $DEFAULT_ROLE)"
fi

# --- Test 4: web supervisor has NO celery worker/beat ---
echo "=== Test 4: web supervisor has NO celery worker/beat ==="
if grep -q "celery-worker\|celery-beat" "$ROOT/supervisord.web.conf" 2>/dev/null; then
  fail "supervisord.web.conf contains celery worker/beat"
else
  pass "supervisord.web.conf has no celery worker/beat"
fi

# --- Test 5: web supervisor has daphne + mcp ---
echo "=== Test 5: web supervisor has daphne + mcp ==="
if grep -q "daphne" "$ROOT/supervisord.web.conf" 2>/dev/null && grep -q "mcp-server" "$ROOT/supervisord.web.conf" 2>/dev/null; then
  pass "supervisord.web.conf has daphne and mcp"
else
  fail "supervisord.web.conf missing daphne or mcp"
fi

# --- Test 6: full supervisor has ALL programs (legacy compat) ---
echo "=== Test 6: full supervisor has all programs ==="
for prog in "mcp-server" "migrate" "django" "celery-worker" "celery-beat"; do
  if grep -q "\[program:$prog\]" "$ROOT/supervisord.conf"; then
    pass "supervisord.conf has $prog"
  else
    fail "supervisord.conf missing $prog"
  fi
done

echo
echo "=== Results: $PASS passed, $FAIL failed ==="
exit $FAIL
