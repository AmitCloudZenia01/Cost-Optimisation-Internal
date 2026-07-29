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

# The suites above inspect the code. They all pass on a report whose tabs are
# missing, whose columns do not add up, or whose cells read as jargon — which
# is how the Reconciliation page went three runs without existing. After
# generating a report, read it the way a client would:
#
#     python3 tests/verify_report.py "<account> AWS Cost Report <date>.xlsx"
#
echo
echo "Reminder: run tests/verify_report.py against the generated .xlsx."
echo "The suites above check the code; that one checks what the client receives."
