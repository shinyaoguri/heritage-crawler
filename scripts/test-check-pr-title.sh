#!/usr/bin/env bash
# check-pr-title.sh の判定テスト。CI から実行される。
# 通る例だけでなく、落ちるべき例 (type 無し・未知の type・要約が空) も検査する。
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
target="$here/check-pr-title.sh"
failed=0

expect() {
  local want=$1 title=$2 got
  PR_TITLE="$title" bash "$target" >/dev/null 2>&1
  got=$?
  if [ "$got" -ne "$want" ]; then
    # 日本語の閉じ括弧が変数名の一部として解釈されるため、必ず ${} で括る
    echo "NG: 「${title}」は exit ${want} を期待したが ${got}" >&2
    failed=1
  fi
}

# 通るべきもの
expect 0 "feat: CSV の分割取得を実装する"
expect 0 "fix(detail): ゼロ詰め ID が 404 になる"
expect 0 "docs: README を更新する"
expect 0 "chore(repo-standards): 標準の構成ファイルを追加する"
expect 0 "build(deps): bump actions/checkout"
expect 0 "refactor!: 破壊的変更を含む"

# 落ちるべきもの
expect 1 "CSV の分割取得を実装する"          # type が無い
expect 1 "update: README"                     # 未知の type
expect 1 "feat:"                              # 要約が空
expect 1 "feat CSV の分割取得を実装する"      # コロンが無い
expect 1 "Feat: 大文字の type"                # type は小文字のみ

# PR_TITLE が未設定なら落ちる (set -u で検出させている)
if PR_TITLE= bash "$target" >/dev/null 2>&1; then
  echo "NG: PR_TITLE が空のとき成功してしまった" >&2
  failed=1
fi

if [ "$failed" -eq 0 ]; then
  echo "OK: check-pr-title.sh の判定は期待どおり"
fi
exit "$failed"
