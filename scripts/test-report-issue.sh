#!/usr/bin/env bash
# report-issue.sh の挙動テスト。CI から実行される。
#
# **GitHub へは出ない。** `gh` を身代わりに差し替え、何を呼んだかだけを見る。
#
# 見張りたいのは 3 つ。**同じタイトルが open なら追記する** (毎週の不調で Issue が
# 積み上がらない)、**無ければ立てる** (誰も見ていない週の失敗が消えない)、
# **引用符を含むタイトルでも取り違えない** (静かに毎回新しく立てるのが一番たちが悪い)。
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
target="$here/report-issue.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failed=0

mkdir -p "$work/bin"
# 身代わりの gh。issue list は $work/issues.json を返し、残りは呼ばれた記録を残す。
cat >"$work/bin/gh" <<'SH'
#!/usr/bin/env bash
if [ "$1" = "issue" ] && [ "$2" = "list" ]; then
  cat "$GH_FAKE_ISSUES"
  exit 0
fi
echo "$*" >>"$GH_FAKE_CALLS"
SH
chmod +x "$work/bin/gh"
export PATH="$work/bin:$PATH"
export GH_FAKE_ISSUES="$work/issues.json"
export GH_FAKE_CALLS="$work/calls.txt"

echo "本文" >"$work/body.md"

run() {
  : >"$GH_FAKE_CALLS"
  bash "$target" "$@" >/dev/null 2>&1
}

expect_call() {
  local label=$1 want=$2 got
  got=$(cat "$GH_FAKE_CALLS")
  if [ "$got" != "$want" ]; then
    echo "NG: ${label} は「${want}」を期待したが「${got}」" >&2
    failed=1
  fi
}

# open な Issue が無ければ立てる
echo '[]' >"$GH_FAKE_ISSUES"
run "週次の差分更新が失敗した" "$work/body.md"
expect_call "既存なし" "issue create --title 週次の差分更新が失敗した --body-file $work/body.md"

# 同じタイトルが open なら追記する (毎週立てない)
echo '[{"number": 42, "title": "週次の差分更新が失敗した"}]' >"$GH_FAKE_ISSUES"
run "週次の差分更新が失敗した" "$work/body.md"
expect_call "既存あり" "issue comment 42 --body-file $work/body.md"

# 別のタイトルの Issue は関係ない
echo '[{"number": 42, "title": "別の話"}]' >"$GH_FAKE_ISSUES"
run "週次の差分更新が失敗した" "$work/body.md"
expect_call "別のタイトル" "issue create --title 週次の差分更新が失敗した --body-file $work/body.md"

# 引用符を含むタイトルでも取り違えない (jq へ文字列補間していると壊れる)
echo '[{"number": 7, "title": "\"data\" が壊れた"}]' >"$GH_FAKE_ISSUES"
run '"data" が壊れた' "$work/body.md"
expect_call "引用符つき" "issue comment 7 --body-file $work/body.md"

# 引数が足りなければ使い方を出して落ちる
bash "$target" "題だけ" >/dev/null 2>&1
if [ $? -ne 2 ]; then
  echo "NG: 引数不足は exit 2 を期待した" >&2
  failed=1
fi

# 本文のファイルが無ければ落ちる (空の Issue を立てない)
bash "$target" "題" "$work/ない.md" >/dev/null 2>&1
if [ $? -ne 2 ]; then
  echo "NG: 本文のファイルが無いときは exit 2 を期待した" >&2
  failed=1
fi

if [ "$failed" -eq 0 ]; then
  echo "OK: report-issue.sh は期待どおり"
fi
exit "$failed"
