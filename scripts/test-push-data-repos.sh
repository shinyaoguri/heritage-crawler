#!/usr/bin/env bash
# push-data-repos.sh の挙動テスト。CI から実行される。
#
# **GitHub へは出ない。** 押し先を手元の bare リポジトリにして、`insteadOf` で
# `https://.../code4heritage/<名前>.git` をそこへ向ける。押した結果は bare 側の
# コミットを読んで確かめる。
#
# 見張りたいのは 3 つ。**中身が動いた回と確認だけの回でコミットメッセージが違う**
# (履歴から「いつ何が変わったか」を読めなくしない)、**何も動いていないリポジトリは
# 押さない** (確かめていないのに確認日を進めない)、**数が出力に出る** (#67 で
# 常に 0 だった)。
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
target="$here/push-data-repos.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failed=0

export GIT_CONFIG_GLOBAL="$work/gitconfig"
git config --global user.name tester
git config --global user.email tester@example.invalid
git config --global init.defaultBranch main
git config --global protocol.file.allow always
git config --global url."$work/remotes/".insteadOf \
  "https://x-access-token:dummy@github.com/code4heritage/"

mkdir -p "$work/remotes" "$work/data"
for repo in changed-repo issues-repo checked-repo untouched-repo; do
  git init --quiet --bare "$work/remotes/${repo}.git"
  git clone --quiet "$work/remotes/${repo}.git" "$work/data/${repo}" 2>/dev/null
  dir="$work/data/${repo}"
  mkdir -p "$dir/data"
  echo '{"ledger_id":"401","managed_id":"1"}' >"$dir/data/13_tokyo.jsonl"
  echo '{"counts":{"records":1}}' >"$dir/meta.json"
  echo '{"checked_date":"2026-08-15"}' >"$dir/status.json"
  git -C "$dir" add -A
  git -C "$dir" commit --quiet -m "先週まで"
  git -C "$dir" push --quiet origin main
done
printf 'changed-repo\nissues-repo\nchecked-repo\nuntouched-repo\n' >"$work/repos.txt"

# 今週の状態。中身が動いた / 不具合の一覧だけ動いた / 確認日だけ動いた /
# 何も動いていない、の 4 通り。
echo '{"checked_date":"2026-08-22"}' >"$work/data/changed-repo/status.json"
echo '{"ledger_id":"401","managed_id":"2"}' >>"$work/data/changed-repo/data/13_tokyo.jsonl"
echo '{"checked_date":"2026-08-22"}' >"$work/data/issues-repo/status.json"
echo '{"ledger_id":"401","managed_id":"1","kind":"stale"}' >"$work/data/issues-repo/source-issues.jsonl"
echo '{"checked_date":"2026-08-22"}' >"$work/data/checked-repo/status.json"

export GH_TOKEN=dummy BOT="tester[bot]" RUN_URL="https://example.invalid/run/1"
export GITHUB_OUTPUT="$work/output.txt"
: >"$GITHUB_OUTPUT"

summary=$(bash "$target" "$work/repos.txt" "$work/data" 2026-08-22)
status=$?

check() {
  local label=$1 want=$2 got=$3
  if [ "$got" != "$want" ]; then
    echo "NG: ${label} は「${want}」を期待したが「${got}」" >&2
    failed=1
  fi
}

if [ "$status" -ne 0 ]; then
  echo "NG: 正常な入力で exit ${status}" >&2
  failed=1
fi

# 中身が動いた回と、確認だけの回でメッセージが違う
check "中身が動いた回" "chore(data): 2026-08-22 の差分を反映" \
  "$(git -C "$work/remotes/changed-repo.git" log -1 --format='%s' main)"
check "確認だけの回" "chore(data): 2026-08-22 に確認 (差分なし)" \
  "$(git -C "$work/remotes/checked-repo.git" log -1 --format='%s' main)"
# 不具合の一覧 (ADR 0030) が動いた回を「差分なし」と書かない
check "不具合の一覧だけ動いた回" "chore(data): 2026-08-22 の差分を反映" \
  "$(git -C "$work/remotes/issues-repo.git" log -1 --format='%s' main)"

# 何も動いていないリポジトリは押さない (確かめていないのに確認日を進めない)
check "触れていないリポジトリ" "先週まで" \
  "$(git -C "$work/remotes/untouched-repo.git" log -1 --format='%s' main)"

# 数が出力に出る (#67: パイプラインのサブシェルで数えていたころは常に 0 だった)
check "changed の出力" "changed=2" "$(grep '^changed=' "$GITHUB_OUTPUT")"
check "checked の出力" "checked=1" "$(grep '^checked=' "$GITHUB_OUTPUT")"

# まとめの文言も同じ数を言う
if ! echo "$summary" | grep -q "中身が変わったリポジトリ: 2 / 確認のみ: 1"; then
  echo "NG: まとめの数が合わない" >&2
  echo "$summary" >&2
  failed=1
fi

# 出典と実行の URL をコミットに残す (どの実行が押したか辿れるように)
if ! git -C "$work/remotes/changed-repo.git" log -1 --format='%b' main |
  grep -q "https://example.invalid/run/1"; then
  echo "NG: コミット本文に実行の URL が無い" >&2
  failed=1
fi

# 押したのが誰かを名乗る (履歴から人の手か週次かを見分けられるように)
check "コミットの名乗り" "tester[bot]" \
  "$(git -C "$work/remotes/changed-repo.git" log -1 --format='%an' main)"

check "failed の出力" "failed=0" "$(grep '^failed=' "$GITHUB_OUTPUT")"

# --- 押せないリポジトリがあっても残りは押す (#98 / ADR 0029) ---
#
# 2026-09-20 の週次は 26 リポジトリの **2 番目**で GitHub が push を弾き
# (`remote: fatal error in commit_refs`)、残る 24 は試みてすらいなかった。
# **弾かれ方は bare 側の `pre-receive` で作る** — 相手が弾いたときと同じ経路で
# `remote rejected` になるので、押す側の挙動をそのまま確かめられる。
for repo in broken-repo flaky-repo ok-repo; do
  git init --quiet --bare "$work/remotes/${repo}.git"
  git clone --quiet "$work/remotes/${repo}.git" "$work/data/${repo}" 2>/dev/null
  dir="$work/data/${repo}"
  mkdir -p "$dir/data"
  echo '{"ledger_id":"401","managed_id":"1"}' >"$dir/data/13_tokyo.jsonl"
  echo '{"counts":{"records":1}}' >"$dir/meta.json"
  echo '{"checked_date":"2026-09-14"}' >"$dir/status.json"
  git -C "$dir" add -A
  git -C "$dir" commit --quiet -m "先週まで"
  git -C "$dir" push --quiet origin main
done

# フックは土台を押し終えてから置く (先に置くと初期状態すら作れない)。
cat >"$work/remotes/broken-repo.git/hooks/pre-receive" <<'HOOK'
#!/bin/sh
echo "fatal error in commit_refs" >&2
exit 1
HOOK
# 1 回目だけ弾く = 一時的な不調。再試行で通るはず。
cat >"$work/remotes/flaky-repo.git/hooks/pre-receive" <<HOOK
#!/bin/sh
n=\$(cat "$work/flaky-count" 2>/dev/null || echo 0)
echo \$((n + 1)) >"$work/flaky-count"
[ "\$n" -ge 1 ] || { echo "fatal error in commit_refs" >&2; exit 1; }
exit 0
HOOK
chmod +x "$work/remotes/broken-repo.git/hooks/pre-receive" \
  "$work/remotes/flaky-repo.git/hooks/pre-receive"

# 押せないものを**先頭**に置く (2026-09-20 と同じ並び)。
printf 'broken-repo\nflaky-repo\nok-repo\n' >"$work/repos-failing.txt"
echo '{"checked_date":"2026-09-21"}' >"$work/data/broken-repo/status.json"
echo '{"checked_date":"2026-09-21"}' >"$work/data/flaky-repo/status.json"
echo '{"checked_date":"2026-09-21"}' >"$work/data/ok-repo/status.json"
echo '{"ledger_id":"401","managed_id":"2"}' >>"$work/data/ok-repo/data/13_tokyo.jsonl"

export GITHUB_OUTPUT="$work/output-failing.txt"
: >"$GITHUB_OUTPUT"

# 待ちは 0 秒にする (再試行することの確認であって、待つことの確認ではない)。
failing_summary=$(PUSH_RETRY_DELAY=0 bash "$target" \
  "$work/repos-failing.txt" "$work/data" 2026-09-21)
failing_status=$?

# 押せないものがあれば落ちる (ジョブを赤くして Issue を立てる経路は残す)
if [ "$failing_status" -ne 1 ]; then
  echo "NG: 押せないリポジトリがあるときは exit 1 を期待したが ${failing_status}" >&2
  failed=1
fi

# **先頭が落ちても後続は押す** (これが #98 で失われたもの)
check "先頭が落ちた後のリポジトリ" "chore(data): 2026-09-21 の差分を反映" \
  "$(git -C "$work/remotes/ok-repo.git" log -1 --format='%s' main)"

# 一時的な失敗は再試行で通る (通らなければ「先週まで」のまま)
check "一時的に弾かれたリポジトリ" "chore(data): 2026-09-21 に確認 (差分なし)" \
  "$(git -C "$work/remotes/flaky-repo.git" log -1 --format='%s' main)"

# 押せなかったものは前回のまま (押せていないのに確認日を進めない)
check "押せなかったリポジトリ" "先週まで" \
  "$(git -C "$work/remotes/broken-repo.git" log -1 --format='%s' main)"

# 数えるのは押せたぶんだけ。押せなかった数も出す (Issue の本文が書き分けに使う)
check "押せた回の changed" "changed=1" "$(grep '^changed=' "$GITHUB_OUTPUT")"
check "押せた回の checked" "checked=1" "$(grep '^checked=' "$GITHUB_OUTPUT")"
check "押せなかった数" "failed=1" "$(grep '^failed=' "$GITHUB_OUTPUT")"

# まとめは押せなかったリポジトリを名指しする (どれを押し直せばよいか分かるように)
if ! echo "$failing_summary" | grep -q "broken-repo"; then
  echo "NG: まとめに押せなかったリポジトリ名が無い" >&2
  echo "$failing_summary" >&2
  failed=1
fi

# 引数が足りなければ使い方を出して落ちる
bash "$target" "$work/repos.txt" >/dev/null 2>&1
if [ $? -ne 2 ]; then
  echo "NG: 引数不足は exit 2 を期待した" >&2
  failed=1
fi

# 一覧が無ければ落ちる (空振りを成功にしない)
bash "$target" "$work/ない.txt" "$work/data" 2026-08-22 >/dev/null 2>&1
if [ $? -ne 2 ]; then
  echo "NG: 一覧が無いときは exit 2 を期待した" >&2
  failed=1
fi

if [ "$failed" -eq 0 ]; then
  echo "OK: push-data-repos.sh は期待どおり"
fi
exit "$failed"
