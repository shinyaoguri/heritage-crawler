#!/usr/bin/env bash
# check-freshness.sh の判定テスト。CI から実行される。
# 一時 git リポに fixture を組み立てて、ドリフトが無い場合と 4 種のドリフトを検査する。
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
target="$here/check-freshness.sh"
failed=0

# 節がそろった ADR を 1 件書き出す
write_adr() {
  local dir=$1 num=$2 slug=$3
  cat >"$dir/docs/decisions/${num}-${slug}.md" <<EOF
# ${num}: テスト用の決定

## 状態

採用

## 文脈

テスト用。

## 決定

テスト用。

## 影響

テスト用。
EOF
}

# 何もドリフトしていない fixture を作り、パスを返す
make_fixture() {
  local dir
  dir=$(mktemp -d)
  mkdir -p "$dir/docs/decisions" "$dir/src"
  write_adr "$dir" 0000 record-architecture-decisions
  write_adr "$dir" 0001 first-decision
  : >"$dir/src/example.py"
  printf '# fixture\n\nADR 0000〜0001 を参照する。`src/example.py` にある。\n' >"$dir/README.md"
  git -C "$dir" init -q
  git -C "$dir" add -A
  echo "$dir"
}

expect() {
  local want=$1 name=$2 dir=$3 got
  (cd "$dir" && bash "$target") >/dev/null 2>&1
  got=$?
  if [ "$got" -ne "$want" ]; then
    # 日本語の閉じ括弧が変数名の一部として解釈されるため、必ず ${} で括る
    echo "NG: ${name} は exit ${want} を期待したが ${got}" >&2
    failed=1
  fi
  rm -rf "$dir"
}

# ドリフトが無ければ通る
expect 0 "ドリフト無し" "$(make_fixture)"

# ADR の節が欠けている
d=$(make_fixture)
grep -v '^## 影響$' "$d/docs/decisions/0001-first-decision.md" >"$d/tmp" &&
  mv "$d/tmp" "$d/docs/decisions/0001-first-decision.md"
git -C "$d" add -A
expect 1 "ADR の節が欠けている" "$d"

# ADR の連番が飛んでいる
d=$(make_fixture)
mv "$d/docs/decisions/0001-first-decision.md" "$d/docs/decisions/0002-first-decision.md"
git -C "$d" add -A
expect 1 "ADR の連番が飛んでいる" "$d"

# 実在しない ADR を参照している
d=$(make_fixture)
printf '\n削除済みの ADR 0009 を参照する。\n' >>"$d/README.md"
git -C "$d" add -A
expect 1 "実在しない ADR 参照" "$d"

# 実在しないパスを参照している
d=$(make_fixture)
printf '\n`src/gone.py` を参照する。\n' >>"$d/README.md"
git -C "$d" add -A
expect 1 "実在しないパス参照" "$d"

# ADR が 1 件も無い
d=$(make_fixture)
rm -f "$d"/docs/decisions/*.md "$d/README.md"
git -C "$d" add -A
expect 1 "ADR が 0 件" "$d"

if [ "$failed" -eq 0 ]; then
  echo "OK: check-freshness.sh の判定は期待どおり"
fi
exit "$failed"
