#!/usr/bin/env bash
# データリポジトリへ commit して push する。週次 (weekly.yml) から使う。
#
# **中身が動いたかと、確かめたかを分けて数える。** `status.json` の確認日は毎週
# 動くので (ADR 0023)、それも「更新」に数えると全部が更新に見え、静かな週と
# データが動いた週を区別できなくなる。中身が動いたかは `data` / `meta.json` /
# `removed.jsonl` / `source-issues.jsonl` (ADR 0030) の差分で見て、コミット
# メッセージも分ける。
#
# 何も動いていないリポジトリは押さない。`status.json` すら動いていないのは
# `update-records` がそのリポジトリに触れていない (分類や地域を絞った実行) ときで、
# 確かめた証跡が無いのに確認日だけ進めるわけにはいかない。
#
# **1 つ押せなくても止めない** (ADR 0029)。押せなかったものを記録して次へ進み、
# 最後に 1 つでもあれば 1 を返す。押す相手は 26 個の別々のリポジトリで、互いに
# 依存しない — 1 つの不調で残り 25 を道連れにする理由が無い。
#
# **ワークフローの YAML に書かない**のは、ここが 26 リポジトリへ実際に押す唯一の
# 場所で、テストを当てたいため (`test-push-data-repos.sh`)。
#
# 使い方: push-data-repos.sh <リポジトリ名の一覧> <データの親ディレクトリ> <日付>
# 標準出力に Markdown のまとめを出し、数は $GITHUB_OUTPUT へ書く。
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"

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

# **相手が弾くのは一時的なことがある。** 2026-09-20 の週次は 2 番目のリポジトリが
# `remote: fatal error in commit_refs` で弾かれた (GitHub 側の不調で、こちらの
# 入力に原因は無い)。数秒〜数十秒で明けるので、間隔を空けて粘る。回数と待ちを
# 環境変数で上げ下げできるのはテストのため (実際の push で再試行を確かめる)。
attempts=${PUSH_RETRY_ATTEMPTS:-3}
delay=${PUSH_RETRY_DELAY:-15}

changed=0
checked=0
failed=0
failed_names=""

# 押せなかったことを記録して次へ進む。**配列を使わない**のは、空の配列への
# `${#a[@]}` が古い bash (macOS の 3.2) の `set -u` で落ちるため。
note_failure() {
  failed=$((failed + 1))
  failed_names="${failed_names} $1"
  echo "- $1: **$2**"
}

echo '## push'
while read -r repo; do
  [ -n "$repo" ] || continue
  # `cd` ではなく `git -C`。ループを抜けたあとに相対パスを使っても、最後の
  # リポジトリの中を指さない。
  dir="${data_dir}/${repo}"
  if ! git -C "$dir" add -A; then
    note_failure "$repo" "git add に失敗した (押していない)"
    continue
  fi

  if git -C "$dir" diff --cached --quiet; then
    echo "- ${repo}: 差分なし (このリポジトリは対象外)"
    continue
  fi

  if git -C "$dir" diff --cached --quiet -- data meta.json removed.jsonl source-issues.jsonl; then
    message="chore(data): ${today} に確認 (差分なし)"
    note="- ${repo}: 確認のみ"
    kind=checked
  else
    message="chore(data): ${today} の差分を反映"
    note="- ${repo}: $(git -C "$dir" diff --cached --shortstat)"
    kind=changed
  fi

  # clone のときに埋め込んだトークンは期限切れになりうる。取り直したものへ
  # 張り替えてから押す。
  git -C "$dir" remote set-url origin \
    "https://x-access-token:${GH_TOKEN}@github.com/code4heritage/${repo}.git"
  if ! git -C "$dir" \
    -c "user.name=${BOT}" \
    -c "user.email=${BOT}@users.noreply.github.com" \
    commit --quiet -m "${message}" \
    -m "出典: 国指定文化財等データベース (文化庁)" \
    -m "実行: ${RUN_URL}"; then
    note_failure "$repo" "commit に失敗した (押していない)"
    continue
  fi

  # **繰り返すのは push だけ。** commit からやり直すと、同じ内容のコミットが
  # 試行のぶんだけ積み上がる。
  if ! "${here}/retry.sh" "$attempts" "$delay" git -C "$dir" push --quiet; then
    note_failure "$repo" "push に失敗した (${attempts} 回試した)"
    continue
  fi

  # **数えるのは押せたぶんだけ。** 手元で commit しただけのものを数に入れると、
  # 失敗 Issue の本文が「押した」と言ってしまう。
  if [ "$kind" = changed ]; then
    changed=$((changed + 1))
  else
    checked=$((checked + 1))
  fi
  echo "${note}"
done <"$repos_file"

echo
echo "中身が変わったリポジトリ: ${changed} / 確認のみ: ${checked}"

if [ "$failed" -gt 0 ]; then
  echo
  echo "**押せなかったリポジトリ: ${failed}** —${failed_names}"
  echo
  echo "押せたぶんは更新済み。押せなかったぶんは前回のまま (確認日も古いままなので、"
  echo "止まっていることがデータリポジトリ側から見える) で、翌週の実行が同じ内容を"
  echo "押し直す。待たずに揃えるなら \`gh workflow run weekly.yml\`。"
fi

# **数はステップの出力にも出す。** サマリの文字列を後段が読み直さずに済む
# (以前はパイプラインのサブシェルで数えていて、常に 0 が書かれていた。#67)。
# 失敗した数も出す — 失敗 Issue の本文が「全部押した / 一部押した / 押す前に
# 落ちた」を書き分けるのに使う (ADR 0029)。
if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "changed=${changed}" >>"$GITHUB_OUTPUT"
  echo "checked=${checked}" >>"$GITHUB_OUTPUT"
  echo "failed=${failed}" >>"$GITHUB_OUTPUT"
fi

if [ "$failed" -gt 0 ]; then
  exit 1
fi
