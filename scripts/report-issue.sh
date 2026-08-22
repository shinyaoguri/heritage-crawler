#!/usr/bin/env bash
# 週次の実行結果を Issue に残す。**同じタイトルの open Issue があれば追記する。**
#
# 週次は誰も見ていないところで走るので (weekly.yml)、気付ける場所へ残さないと
# データが何週も止まっていることに気付けない。一方で同じことが毎週起きると
# Issue が積み上がるので、open なものがあれば追記に倒す。
#
# タイトルが一致の鍵になる。**呼ぶ側は日付や件数をタイトルに入れない** —
# 毎回違うタイトルになると、同じ不調で Issue が毎週立つ。
#
# 使い方: report-issue.sh <タイトル> <本文のファイル>
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "使い方: $0 <タイトル> <本文のファイル>" >&2
  exit 2
fi

title=$1
body_file=$2

if [ ! -f "$body_file" ]; then
  echo "本文のファイルが無い: ${body_file}" >&2
  exit 2
fi

# **タイトルは jq へ --arg で渡す。** クエリへ文字列として埋め込むと、引用符を
# 含むタイトルでフィルタが壊れる (静かに「既存なし」と読み、毎回新しく立ててしまう)。
existing=$(gh issue list --state open --limit 100 --json number,title |
  jq -r --arg title "$title" '[.[] | select(.title == $title)] | .[0].number // empty')

if [ -n "$existing" ]; then
  gh issue comment "$existing" --body-file "$body_file"
else
  gh issue create --title "$title" --body-file "$body_file"
fi
