#!/bin/sh
# Run every check on the alert-closing system and print one summary. Standard tools only.
#
#   sh eval/check.sh
#
# 1. Format: every case has one valid line, reason codes belong to their verdicts, cited IDs exist.
# 2. Labels: agreement with the 160 hand-labelled development cases, scored with the hackathon cost table.
# 3. Reproducibility: three runs with different hash seeds must be byte-identical to the committed answers.
# 4. Wording: ignoring what customers said in chats must not change a verdict.
# 5. Mutations: awkward variants of real cases must move the safe way.
#
# Development aid only. The system never reads anything in eval/.

cd "$(dirname "$0")/.." || exit 1
ROOT=$(pwd)
OUT=$(mktemp -d)
fail=0

echo "== Format and labels"
for inst in bank emoney investment payments_fx; do
  hackathon/system/run "$ROOT/hackathon/institutions/$inst" "$OUT/$inst.jsonl" 2>"$OUT/$inst.err"
  report=$(python3 -I eval/score.py "hackathon/institutions/$inst" "$OUT/$inst.jsonl" "eval/labels/$inst.jsonl")
  problems=$(printf '%s\n' "$report" | grep -c ' x ')
  accuracy=$(printf '%s\n' "$report" | grep 'verdict accuracy' | sed 's/.*accuracy: //')
  wrongful=$(printf '%s\n' "$report" | grep 'wrongful closes' | sed 's/.*closes: //')
  printf '  %-12s format problems: %s | labelled verdicts: %s | wrongful closes: %s\n' "$inst" "$problems" "$accuracy" "$wrongful"
  [ "$problems" = "0" ] && [ "$wrongful" = "0" ] && [ ! -s "$OUT/$inst.err" ] || fail=1
done

echo "== Reproducibility (3 runs, different hash seeds, against the committed answers)"
for inst in bank emoney investment payments_fx; do
  same=yes
  for seed in 1 2 3; do
    PYTHONHASHSEED=$seed hackathon/system/run "$ROOT/hackathon/institutions/$inst" "$OUT/$inst.$seed.jsonl" 2>/dev/null
    cmp -s "$OUT/$inst.$seed.jsonl" "hackathon/output/$inst.jsonl" || same=NO
  done
  printf '  %-12s byte-identical: %s\n' "$inst" "$same"
  [ "$same" = "yes" ] || fail=1
done

echo "== Wording (verdicts that change when chat wording is ignored)"
python3 eval/robustness.py hackathon/institutions/bank hackathon/institutions/emoney hackathon/institutions/investment hackathon/institutions/payments_fx \
  | sed 's/^/  /' | grep -v '^   '
python3 eval/robustness.py hackathon/institutions/bank hackathon/institutions/emoney hackathon/institutions/investment hackathon/institutions/payments_fx \
  | grep -q '([1-9][0-9]* change verdict)' && fail=1

echo "== Mutations (awkward variants of real cases)"
python3 eval/mutations.py > "$OUT/mutations.txt" 2>&1 || fail=1
grep -c '^PASS' "$OUT/mutations.txt" | sed 's/^/  passed: /'
grep '^FAIL' "$OUT/mutations.txt" | sed 's/^/  /'

rm -rf "$OUT"
find . -name __pycache__ -not -path './.git/*' -exec rm -rf {} + 2>/dev/null
echo
if [ "$fail" = "0" ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED"; fi
exit $fail
