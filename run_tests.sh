#!/usr/bin/env bash
# Full verification. Run before any client-facing report.
set -u
fail=0
for suite in tests/audit.py tests/test_no_fabrication.py tests/test_account_shapes.py; do
  echo "── $suite"
  python3 "$suite" | tail -2
  [ "${PIPESTATUS[0]}" -ne 0 ] && fail=1
done
echo
[ $fail -eq 0 ] && echo "ALL SUITES PASSED" || echo "FAILURES PRESENT"
exit $fail
