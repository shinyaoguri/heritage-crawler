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
for repo in changed-repo checked-repo untouched-repo; do
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
printf 'changed-repo\nchecked-repo\nuntouched-repo\n' >"$work/repos.txt"

# 今週の状態。中身が動いた / 確認日だけ動いた / 何も動いていない、の 3 通り。
echo '{"checked_date":"2026-08-22"}' >"$work/data/changed-repo/status.json"
echo '{"ledger_id":"401","managed_id":"2"}' >>"$work/data/changed-repo/data/13_tokyo.jsonl"
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

# 何も動いていないリポジトリは押さない (確かめていないのに確認日を進めない)
check "触れていないリポジトリ" "先週まで" \
  "$(git -C "$work/remotes/untouched-repo.git" log -1 --format='%s' main)"

# 数が出力に出る (#67: パイプラインのサブシェルで数えていたころは常に 0 だった)
check "changed の出力" "changed=1" "$(grep '^changed=' "$GITHUB_OUTPUT")"
check "checked の出力" "checked=1" "$(grep '^checked=' "$GITHUB_OUTPUT")"

# まとめの文言も同じ数を言う
if ! echo "$summary" | grep -q "中身が変わったリポジトリ: 1 / 確認のみ: 1"; then
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
