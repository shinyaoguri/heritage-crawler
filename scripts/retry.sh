#!/usr/bin/env bash
# 一時的な失敗を挟んでコマンドを繰り返す。月次の取得 (monthly.yml) から使う。
#
# 相手先は時間帯によって 504 を返す (2026-08-12 の初回実行で実測。CloudFront が
# origin のタイムアウトを 504 にして返し、HTTP クライアント側の 3 回の再試行
# =約 2 分では抜けられなかった)。月次は誰も見ていないところで走るので、**60 秒の
# 不調で 1 か月ぶんの更新が飛ぶ**のは割に合わない。
#
# 待ち時間を分単位にするのは、相手が落ちているときに叩き続けないため。
# fetch-ledger は取得済みを飛ばして再開するので、繰り返しても取りに行くのは
# 未取得のぶんだけで、相手への総リクエスト数は増えない。
#
# 使い方: retry.sh <試行回数> <待ち秒> <コマンド> [引数...]
set -uo pipefail

if [ "$#" -lt 3 ]; then
  echo "使い方: $0 <試行回数> <待ち秒> <コマンド> [引数...]" >&2
  exit 2
fi

attempts=$1
delay=$2
shift 2

if ! [ "$attempts" -ge 1 ] 2>/dev/null || ! [ "$delay" -ge 0 ] 2>/dev/null; then
  echo "試行回数は 1 以上、待ち秒は 0 以上の整数にする (指定: ${attempts} ${delay})" >&2
  exit 2
fi

for attempt in $(seq 1 "$attempts"); do
  # 終了コードは else 側で拾う。**if 文を抜けた後の $? は 0 になる**ので、
  # そこで受けると失敗をそのまま成功として返してしまう。
  if "$@"; then
    exit 0
  else
    status=$?
  fi
  if [ "$attempt" -ge "$attempts" ]; then
    echo "${attempts} 回とも失敗した (exit ${status}): $*" >&2
    exit "$status"
  fi
  echo "失敗した (exit ${status})。${delay} 秒待って再試行する (${attempt}/${attempts}): $*" >&2
  sleep "$delay"
done
