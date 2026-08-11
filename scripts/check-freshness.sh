#!/usr/bin/env bash
# 参照ドリフト検査。ドキュメントが指す ADR 番号・ファイルパス・Issue が実体と
# ずれていないか、ADR が体裁を保っているかを見る。
#
# Issue の状態だけはコミット無しに変わるため、この検査は定期実行に意味がある
# (残りはリポ内部の整合性なので PR の CI でも捉えられる)。
#
# 外部サイト (国指定文化財等データベース) へは一切アクセスしない。定期実行の
# たびに相手先を叩くのは CLAUDE.md の方針 (CI からデータベースを叩かない) に反し、
# ドリフト検知に外部通信は要らないため。
#
# 検査対象は追跡済みの Markdown のみ。ドリフトを 1 件でも見つけたら exit 1。
set -uo pipefail

cd "$(git rev-parse --show-toplevel)"

adr_dir="docs/decisions"
sections=("## 状態" "## 文脈" "## 決定" "## 影響")
drift=0

report() {
  echo "DRIFT: $1" >&2
  drift=1
}

mapfile -t adr_files < <(git ls-files "$adr_dir/[0-9][0-9][0-9][0-9]-*.md" | sort)
if [ "${#adr_files[@]}" -eq 0 ]; then
  report "$adr_dir に ADR が 1 件も無い"
fi

# (1) ADR の連番。0000 から欠番なく並んでいるか (欠番は ADR の取りこぼしか削除の跡)
expected=0
for f in "${adr_files[@]}"; do
  num=$(basename "$f" | cut -c1-4)
  want=$(printf '%04d' "$expected")
  if [ "$num" != "$want" ]; then
    report "ADR の連番が飛んでいる: ${want} を期待したが ${num} (${f})"
  fi
  expected=$((10#$num + 1))
done

# (2) ADR の 4 節構成。節が落ちると決定の文脈と影響が読めなくなる
for f in "${adr_files[@]}"; do
  for s in "${sections[@]}"; do
    grep -qF "$s" "$f" || report "${f} に節「${s}」が無い"
  done
done

# 実在する ADR 番号の一覧
adr_numbers=""
for f in "${adr_files[@]}"; do
  adr_numbers+=" $(basename "$f" | cut -c1-4)"
done

mapfile -t docs < <(git ls-files '*.md')

# (3) 本文中の「ADR NNNN」参照が実在する ADR を指しているか。
#     連番範囲の記法 (ADR 0001〜0005) は始端と終端の両方を確かめる。
for f in "${docs[@]}"; do
  while read -r num; do
    [ -n "$num" ] || continue
    case "$adr_numbers" in
      *" $num"*) ;;
      *) report "${f} が実在しない ADR ${num} を参照している" ;;
    esac
  done < <(grep -oE 'ADR [0-9]{4}([〜~-][0-9]{4})?' "$f" |
    grep -oE '[0-9]{4}' | sort -u)
done

# (4) バッククォートで囲まれたリポ内パスの参照が実在するか。
#     ディレクトリ参照 (末尾 /) と、行番号・glob を含む表記は対象外にする。
for f in "${docs[@]}"; do
  while read -r path; do
    [ -n "$path" ] || continue
    [ -e "$path" ] || report "${f} が実在しないパス ${path} を参照している"
  done < <(grep -oE '`(src|tests|scripts|docs|\.github|\.claude)/[A-Za-z0-9_./-]+`' "$f" |
    tr -d '`' | grep -vE '/$' | sort -u)
done

# (5) 本文が参照する Issue が実在し、まだ open か。
#     Issue の状態はコミット無しに変わるため PR の CI では捉えられず、定期実行で
#     しか拾えない (CLAUDE.md は Issue #1 を残る論点の正本として参照している)。
#
#     ただし ADR は決定時点の記録であり、参照先が後からクローズされるのは正常。
#     ADR に限って「閉じた」は見逃し、実在しない番号 (タイポ) だけを拾う。
#     捉えたいのは生きた参照のドリフトであって、歴史的記述ではない。
#     gh が使えない・remote が無い環境では黙って飛ばす。
if command -v gh >/dev/null 2>&1 &&
  git remote get-url origin >/dev/null 2>&1 &&
  gh auth status >/dev/null 2>&1; then
  for f in "${docs[@]}"; do
    while read -r n; do
      [ -n "$n" ] || continue
      if ! state=$(gh issue view "$n" --json state --jq .state 2>/dev/null); then
        report "${f} が実在しない Issue #${n} を参照している"
      elif [ "$state" != "OPEN" ] && [[ $f != "$adr_dir/"* ]]; then
        report "${f} が閉じた Issue #${n} を参照している (state=${state})"
      fi
    done < <(grep -oE 'Issue #[0-9]+' "$f" | grep -oE '[0-9]+' | sort -u)
  done
fi

if [ "$drift" -eq 0 ]; then
  echo "OK: 参照ドリフトは見つからなかった (ADR ${#adr_files[@]} 件 / Markdown ${#docs[@]} 件)"
fi
exit "$drift"
