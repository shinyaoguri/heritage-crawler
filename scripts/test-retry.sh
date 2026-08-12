#!/usr/bin/env bash
# retry.sh の挙動テスト。CI から実行される。
#
# 見張りたいのは 3 つ。**成功したらそこで止まる** (無駄に叩かない)、
# **失敗し続けたら諦めて終了コードを返す** (黙って成功にしない)、
# **待ち時間を挟む** (相手が落ちているときに連打しない)。
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
target="$here/retry.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failed=0

# 呼ばれた回数を数え、指定回数目までは失敗する身代わり
cat >"$work/flaky.sh" <<'SH'
#!/usr/bin/env bash
count_file="$1"
succeed_at="$2"
count=$(( $(cat "$count_file") + 1 ))
echo "$count" >"$count_file"
[ "$count" -ge "$succeed_at" ] && exit 0
exit 7
SH
chmod +x "$work/flaky.sh"

run() {
  local succeed_at=$1 attempts=$2 delay=$3
  echo 0 >"$work/count"
  bash "$target" "$attempts" "$delay" "$work/flaky.sh" "$work/count" "$succeed_at" >/dev/null 2>&1
}

expect() {
  local label=$1 want_status=$2 want_calls=$3 got_status=$4 got_calls
  got_calls=$(cat "$work/count")
  if [ "$got_status" -ne "$want_status" ] || [ "$got_calls" -ne "$want_calls" ]; then
    echo "NG: ${label} は exit ${want_status} / ${want_calls} 回を期待したが" \
      "exit ${got_status} / ${got_calls} 回" >&2
    failed=1
  fi
}

# 1 回目で成功すれば、そこで止まる
run 1 3 0
expect "一度で成功" 0 1 $?

# 2 回目で成功すれば、3 回目は呼ばない
run 2 3 0
expect "二度目で成功" 0 2 $?

# 最後まで失敗したら、身代わりの終了コードをそのまま返す
run 99 3 0
expect "三度とも失敗" 7 3 $?

# 試行 1 回なら再試行しない
run 99 1 0
expect "再試行なし" 7 1 $?

# 待ち時間を挟む (1 秒 × 1 回で 1 秒以上かかる)
started=$SECONDS
run 2 2 1
elapsed=$((SECONDS - started))
if [ "$elapsed" -lt 1 ]; then
  echo "NG: 待ち時間を挟んでいない (${elapsed} 秒)" >&2
  failed=1
fi

# 引数が足りなければ使い方を出して落ちる
bash "$target" 3 >/dev/null 2>&1
if [ $? -ne 2 ]; then
  echo "NG: 引数不足は exit 2 を期待した" >&2
  failed=1
fi

# 数でない指定は弾く (誤って待ち秒を書き忘れてもコマンドを走らせない)
bash "$target" 3 まいにち true >/dev/null 2>&1
if [ $? -ne 2 ]; then
  echo "NG: 数でない待ち秒は exit 2 を期待した" >&2
  failed=1
fi

if [ "$failed" -eq 0 ]; then
  echo "OK: retry.sh は期待どおり"
fi
exit "$failed"
