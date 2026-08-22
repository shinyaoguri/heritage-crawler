#!/usr/bin/env bash
# データリポジトリへ commit して push する。週次 (weekly.yml) から使う。
#
# **中身が動いたかと、確かめたかを分けて数える。** `status.json` の確認日は毎週
# 動くので (ADR 0023)、それも「更新」に数えると全部が更新に見え、静かな週と
# データが動いた週を区別できなくなる。中身が動いたかは `data` / `meta.json` /
# `removed.jsonl` の差分で見て、コミットメッセージも分ける。
#
# 何も動いていないリポジトリは押さない。`status.json` すら動いていないのは
# `update-records` がそのリポジトリに触れていない (分類や地域を絞った実行) ときで、
# 確かめた証跡が無いのに確認日だけ進めるわけにはいかない。
#
# **ワークフローの YAML に書かない**のは、ここが 10 リポジトリへ実際に押す唯一の
# 場所で、テストを当てたいため (`test-push-data-repos.sh`)。
#
# 使い方: push-data-repos.sh <リポジトリ名の一覧> <データの親ディレクトリ> <日付>
# 標準出力に Markdown のまとめを出し、数は $GITHUB_OUTPUT へ書く。
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "使い方: $0 <リポジトリ名の一覧> <データの親ディレクトリ> <日付>" >&2
  exit 2
fi

repos_file=$1
data_dir=$2
today=$3

if [ ! -f "$repos_file" ]; then
  echo "リポジトリ名の一覧が無い: ${repos_file}" >&2
  exit 2
fi

: "${GH_TOKEN:?GH_TOKEN が要る (押すためのトークン)}"
: "${BOT:?BOT が要る (コミットの名乗り)}"
: "${RUN_URL:?RUN_URL が要る (コミットに残す実行の URL)}"

changed=0
checked=0

echo '## push'
while read -r repo; do
  [ -n "$repo" ] || continue
  # `cd` ではなく `git -C`。ループを抜けたあとに相対パスを使っても、最後の
  # リポジトリの中を指さない。
  dir="${data_dir}/${repo}"
  git -C "$dir" add -A

  if git -C "$dir" diff --cached --quiet; then
    echo "- ${repo}: 差分なし (このリポジトリは対象外)"
    continue
  fi

  if git -C "$dir" diff --cached --quiet -- data meta.json removed.jsonl; then
    message="chore(data): ${today} に確認 (差分なし)"
    note="- ${repo}: 確認のみ"
    checked=$((checked + 1))
  else
    message="chore(data): ${today} の差分を反映"
    note="- ${repo}: $(git -C "$dir" diff --cached --shortstat)"
    changed=$((changed + 1))
  fi

  # clone のときに埋め込んだトークンは期限切れになりうる。取り直したものへ
  # 張り替えてから押す。
  git -C "$dir" remote set-url origin \
    "https://x-access-token:${GH_TOKEN}@github.com/code4heritage/${repo}.git"
  git -C "$dir" \
    -c "user.name=${BOT}" \
    -c "user.email=${BOT}@users.noreply.github.com" \
    commit --quiet -m "${message}" \
    -m "出典: 国指定文化財等データベース (文化庁)" \
    -m "実行: ${RUN_URL}"
  git -C "$dir" push --quiet
  echo "${note}"
done <"$repos_file"

echo
echo "中身が変わったリポジトリ: ${changed} / 確認のみ: ${checked}"

# **数はステップの出力にも出す。** サマリの文字列を後段が読み直さずに済む
# (以前はパイプラインのサブシェルで数えていて、常に 0 が書かれていた。#67)。
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "changed=${changed}" >>"$GITHUB_OUTPUT"
  echo "checked=${checked}" >>"$GITHUB_OUTPUT"
fi
