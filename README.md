# heritage-crawler

[国指定文化財等データベース](https://kunishitei.bunka.go.jp/bsys/index) (文化庁) から
**全データ**を抽出し、JSON Lines として記録するクローラー。

抽出したデータの出力先は文化財の種別ごとの別リポジトリで、このリポジトリは
クローラー本体のみを持つ
([ADR 0001](docs/decisions/0001-split-repositories-by-heritage-type.md) /
[ADR 0009](docs/decisions/0009-output-to-existing-per-type-repositories.md))。

## 対象の文化財種別

検索フォームの `register_sub_id` が提供する分類をすべて取る
([ADR 0025](docs/decisions/0025-crawl-every-category.md))。分類コードは取得の単位で、
**CSV の台帳ID とは別物** — 1 つの台帳ID に複数の分類が同居する (#74)。
定義は `src/heritage_crawler/catalog.py` の `TARGET_CATEGORIES` が正本で、
下の表はそこと書き出したデータから生成している。

<!-- generated: heritage-crawler render-readme -->
| 分類コード | 文化財種別 | 指定行為 | 指定件数 | 取得対象 | 収録行数 |
|---|---|---|---|---|---|
| 101 | 登録有形文化財（建造物） | 登録 | 14,748 | 14,885 | 14,885 |
| 102 | 国宝・重要文化財（建造物） | 指定 | 2,633 | 5,587 | 5,587 |
| 201 | 国宝・重要文化財（美術工芸品） | 指定 | 10,954 | 11,525 | 11,525 |
| 211 | 登録有形文化財（美術工芸品） | 登録 | 18 | 18 | 18 |
| 202 | 登録美術品 | 登録 | 40 | 40 | 40 |
| 301 | 重要有形民俗文化財 | 指定 | 229 | 229 | 229 |
| 311 | 登録有形民俗文化財 | 登録 | 56 | 56 | 56 |
| 302 | 重要無形民俗文化財 | 指定 | 338 | 338 | 338 |
| 322 | 登録無形民俗文化財 | 登録 | 9 | 9 | 9 |
| 312 | 記録作成等の措置を講ずべき無形の民俗文化財 | 選択 | 662 | 662 | 662 |
| 303 | 重要無形文化財 | 認定 | 100 | 100 | 100 |
| 323 | 登録無形文化財 | 登録 | 7 | 7 | 7 |
| 313 | 記録作成等の措置を講ずべき無形文化財 | 選択 | 132 | 132 | 132 |
| 304 | 選定保存技術 | 認定 | 82 | 82 | 82 |
| 103 | 重要伝統的建造物群保存地区 | 選定 | 126 | 126 | 126 |
| 401 | 史跡名勝天然記念物 | 指定 | 3,281 | 3,281 | 3,395 |
| 411 | 登録記念物 | 登録 | 148 | 148 | 148 |
| 412 | 重要文化的景観 | 選定 | 74 | 74 | 74 |
| 901 | 世界遺産 | 登録 | 21 | 20 | 20 |
| **計** | | | **33,658** | **37,319** | **37,433** |
<!-- /generated -->

- **この表は生成物。手で書き換えない。** 指定件数は
  `src/heritage_crawler/catalog.py` の `TARGET_CATEGORIES`、取得対象と収録行数は
  書き出したデータ (各データリポジトリの `meta.json` と JSON Lines) が正本で、
  `heritage-crawler render-readme` が差し込み口の中身を作り直す。散文に写した
  件数は新規指定・解除のたびに静かに嘘になる
- **指定件数と取得対象は単位が違う。** 検索結果の件数表示は指定単位、CSV の行数と
  出力レコードは棟・件の単位。展開される分類は `src/heritage_crawler/catalog.py` の
  `Category.expands_to_buildings` が持ち、残りは指定 = 1 行 (**表で指定件数と取得対象が
  食い違っているのがその分類**)。網羅性の判定に使えるのは指定件数だけ
  (`report-ledger` が地域合計と全国件数を突き合わせる)
- **取得対象と収録行数の差は 401 の複合指定。** 種別を 2 つ持つ指定は両方の
  リポジトリへ同じ行を書くので (ADR 0012)、行数を足し上げると異なりキー数を
  上回る。突き合わせに使えるのは取得対象 (異なり) の側
- 指定行為の呼び方は分類から決まり、`designation_kind` として出力に載る
  (実装は `src/heritage_crawler/record.py` の `DESIGNATION_KINDS`)
- 指定件数は 2026-08-11 の実測。新規指定・解除で増減するため、厳密一致の検査では
  なく欠損の目安に使う
- **世界遺産 (901) は他分類と名寄せしない。** 同じ物件が構成資産として別の
  リポジトリにも入るが、901 は別の台帳に立った別のレコードで、構成資産は 1 本の
  文字列。`(台帳ID, 管理対象ID)` では結び付かず、名称での突き合わせも誤爆する
  ([ADR 0025](docs/decisions/0025-crawl-every-category.md))

出力先のリポジトリは分類と 1:1 ではない。102 は詳細ページの「国宝・重文区分」で
2 つに、401 は `種別１` / `種別２` で 6 つに分かれる (定義は
`src/heritage_crawler/catalog.py` の `TARGET_DATASETS` が正本、根拠は
[ADR 0009](docs/decisions/0009-output-to-existing-per-type-repositories.md) /
[ADR 0012](docs/decisions/0012-crawl-monuments-and-route-by-kind.md))。

<!-- generated: heritage-crawler render-readme (datasets) -->
| リポジトリ | 取得対象 |
|---|---|
| `registered-tangible-cultural-properties` | 101 登録有形文化財（建造物） |
| `national-treasures` | 102 国宝（建造物） |
| `important-cultural-properties` | 102 重要文化財（建造物） |
| `important-preservation-districts-for-groups-of-traditional-buildings` | 103 重要伝統的建造物群保存地区 |
| `special-historic-sites` | 401 特別史跡 |
| `historic-sites` | 401 史跡 |
| `special-places-of-scenic-beauty` | 401 特別名勝 |
| `places-of-scenic-beauty` | 401 名勝 |
| `special-natural-monuments` | 401 特別天然記念物 |
| `natural-monuments` | 401 天然記念物 |
| `national-treasures-of-fine-arts` | 201 国宝（美術工芸品） |
| `important-cultural-properties-of-fine-arts` | 201 重要文化財（美術工芸品） |
| `registered-tangible-cultural-properties-of-fine-arts` | 211 登録有形文化財（美術工芸品） |
| `registered-art-works` | 202 登録美術品 |
| `important-tangible-folk-cultural-properties` | 301 重要有形民俗文化財 |
| `registered-tangible-folk-cultural-properties` | 311 登録有形民俗文化財 |
| `important-intangible-folk-cultural-properties` | 302 重要無形民俗文化財 |
| `registered-intangible-folk-cultural-properties` | 322 登録無形民俗文化財 |
| `documented-intangible-folk-cultural-properties` | 312 記録作成等の措置を講ずべき無形の民俗文化財 |
| `important-intangible-cultural-properties` | 303 重要無形文化財 |
| `registered-intangible-cultural-properties` | 323 登録無形文化財 |
| `documented-intangible-cultural-properties` | 313 記録作成等の措置を講ずべき無形文化財 |
| `selected-conservation-techniques` | 304 選定保存技術 |
| `registered-monuments` | 411 登録記念物 |
| `important-cultural-landscapes` | 412 重要文化的景観 |
| `world-heritage-sites` | 901 世界遺産 |
<!-- /generated -->

- **この表も生成物。手で書き換えない。** 正本は
  `src/heritage_crawler/catalog.py` の `TARGET_DATASETS` で、
  `heritage-crawler render-readme` が差し込み口の中身を作り直す。**件数表と違って
  データを読まないので、ずれはテストが PR の CI で捕まえる** (#102)
- **国宝は重要文化財の、特別◯◯ は ◯◯ のうちから指定される**が、リポジトリには
  排他に振り分ける (特別史跡は `historic-sites` には書かない)
- **401 には種別を 2 つ持つ複合指定がある** (旧浜離宮庭園 = 特別名勝 + 特別史跡)。
  どちらの一覧から見ても構成員なので、**両方のリポジトリへ同じ行を書く**。
  リポジトリの行数を足し上げても指定件数にはならない — 重複は `(台帳ID, 管理対象ID)`
  と `url` が同じなので名寄せできる
- **区分が読めなかったときの行き先は分類で違う。** 102 は重要文化財側へ落ちるが、
  401 はどこへも書かない (受け皿を置くと振り分けの誤りが史跡に紛れる)。
  どちらも件数は `build-records` の報告に出して見張る

クローラーが書くのはこの表のリポジトリちょうど。**「重要」の付かない伝統的建造物群
保存地区のデータは存在しない** — 国が選定するのは重要伝統的建造物群保存地区で、
伝統的建造物群保存地区の決定は市町村が行うためデータベースの対象外
([ADR 0013](docs/decisions/0013-delete-the-repository-without-data.md))。

## しくみ

取得は 2 段構え ([ADR 0002](docs/decisions/0002-two-stage-fetch-csv-then-detail.md))。

1. 分類 × 都道府県で検索して CSV の台帳を取得する (緯度経度はここにしかない)
2. 台帳の `(台帳ID, 管理対象ID)` から詳細ページ URL を組み立てて取得する
   (解説文・構造及び形式等・員数などはここにしかない)
3. 両者を `(台帳ID, 管理対象ID)` で結合し、種別ごとのリポジトリへ都道府県ごとの
   JSON Lines を書き出す
   ([ADR 0004](docs/decisions/0004-output-jsonl-per-prefecture.md) /
   [ADR 0008](docs/decisions/0008-normalize-schema-detail-page-wins.md) /
   [ADR 0009](docs/decisions/0009-output-to-existing-per-type-repositories.md))

初回の全件取得はローカルで実行し、以降の差分更新を GitHub Actions の週次実行で回す
([ADR 0006](docs/decisions/0006-run-initial-crawl-locally-updates-on-actions.md))。
差分は**前回の台帳との突き合わせ**と 1/52 の巡回で見つける
([ADR 0020](docs/decisions/0020-check-weekly-by-diffing-the-ledger-csv.md)。
`update-records`)。

書き出したデータは**種別ごとの別リポジトリ**が持ち、閲覧サイトと配布物は
`code4heritage/heritages` が受け持つ。3 つの持ち場の関係 — 週次の一巡・誰が何を
書き込むか・どこで何を確かめるか — は
**[データが流れる道すじ](docs/pipeline.md)** にまとめてある。

## 使い方

```bash
pip install -e .
heritage-crawler fetch-ledger     # 1 段目: 分類 × 地域の CSV をキャッシュへ
heritage-crawler report-ledger    # 取得状況と網羅性を確かめる
heritage-crawler audit-listing    # 検索結果一覧と突き合わせて取りこぼしを名指しする
heritage-crawler compare-ledgers  # 前回の台帳と今回をバイト単位で突き合わせる
heritage-crawler fetch-detail     # 2 段目: 台帳の各行から詳細ページをキャッシュへ
heritage-crawler report-detail    # 詳細ページの取得状況を確かめる
heritage-crawler build-records    # キャッシュから JSON Lines を組み立てる
heritage-crawler update-records   # 週次: 前回の出力と突き合わせて差分だけ取り直す
heritage-crawler render-readme    # この README の表を正本から作り直す
```

取得はいずれも `cache/` 配下へ生の取得物のまま置き、**中断しても同じコマンドで
取得済みを飛ばして再開する**。取得系に共通のオプションは次のとおり。

- `--interval` — リクエスト間隔の秒数 = レートの上限 (既定 1.0 = 1 req/s)。
  **これ以上詰めない** — 相手が 200 でエラーページを返す
  ([ADR 0011](docs/decisions/0011-back-off-to-1-rps-and-detect-error-pages.md))
- **相手が途中まで描いて 500 を返したページは、印を付けて残す。** 取れたところ
  (主情報・解説文) は採り、行に `source_issue` を付け、データリポジトリのルートの
  `source-issues.jsonl` に名指しする。週次が毎週確かめ直し、相手が直せば印は消える
  ([ADR 0030](docs/decisions/0030-record-source-side-defects.md))
- `--contact` — User-Agent に載せる連絡先 (環境変数 `HERITAGE_CRAWLER_CONTACT` でも指定できる)
- `--category` / `--area` — 対象を絞る (繰り返し指定できる)
- `--force` — 取得済みも取り直す

### 1 段目 — `fetch-ledger`

分類 × その分類の分割軸 (都道府県中心。分類によっては全国 1 回) を順に
取得する (約 570 リクエスト / 約 40 分)。

分類ごとに地域で絞らない検索も 1 回行い、その全国件数と地域合計を突き合わせて
網羅性を確かめる。差が出たら報告に出る (負 = どの地域でも引けない指定がある、
正 = 地域をまたぐ指定が重複している)。

### 2 段目 — `fetch-detail`

台帳の各行の `(分類コード, 管理対象ID)` から詳細ページの URL を組み立てて巡回し、
生 HTML を gzip でキャッシュへ落とす (約 37,000 件 / 1 req/s で約 10 時間)。
**URL の第 1 セグメントは分類コードで、CSV の台帳ID ではない** — 台帳ID には
複数の分類が同居する ([#74](https://github.com/shinyaoguri/heritage-crawler/issues/74))。
解析はしない — パース仕様を変えるたびに 2 万ページを取り直さずに済むよう、
取得と解析を分けてある ([ADR 0006](docs/decisions/0006-run-initial-crawl-locally-updates-on-actions.md))。

- 取得に失敗しても止まらない。記録して次へ進み、`--retry-failed` で拾い直す
- 続けて失敗したら打ち切る (相手が落ちているときに叩き続けないため)
- **HTTP 200 でもエラーページなら失敗として扱う。** 本文が「必要な情報が足りません」
  だったり実物 (33〜55 KB) に対して小さすぎるものは、キャッシュに残さず失敗にする
  ([ADR 0011](docs/decisions/0011-back-off-to-1-rps-and-detect-error-pages.md))
- `--limit` — 先頭から指定件数だけ取る (疎通確認や様子見に使う)
- `--concurrency` — 同時に投げる本数 (既定 1)。**レートの上限は間隔だけが決める**
  ので、増やしても超えない。埋まるのは応答待ちの隙間で、増えるのは同時接続だけ
  ([ADR 0010](docs/decisions/0010-rate-limit-by-request-start.md))
- `--recheck-cache` — キャッシュ済みの HTML を検査し、エラーページだったものを
  取り直す (通信せずに検査だけもできる)

### 3 段目 — `build-records`

キャッシュだけを読んで、都道府県ごとの JSON Lines を書き出す (通信はしない)。
スキーマを変えても 2 万件を取り直さずに済む。

`--output-dir` はデータリポジトリを並べた**親ディレクトリ**を指す
([ADR 0009](docs/decisions/0009-output-to-existing-per-type-repositories.md))。

```
<出力ディレクトリ>/<リポジトリ名>/data/<都道府県コード>_<ローマ字>.jsonl
<出力ディレクトリ>/<リポジトリ名>/meta.json
<出力ディレクトリ>/<リポジトリ名>/status.json   (週次の差分更新だけが書く)
例: national-treasures/data/29_nara.jsonl
```

既定は `data`。データリポジトリ群を置いたディレクトリへ symlink を張っておくと、
クローラーの手元からデータを辿れて `--output-dir` も渡さずに済む。

```
ln -s ~/Repos/bunkazai data
```

`data` は gitignore 済みで、**symlink も追跡されない** (だからデータリポジトリは
別リポジトリのまま独立した履歴を持つ。submodule にはしない)。

出力先は文化財の種別ごとに分かれる (対応表は「[対象の文化財種別](#対象の文化財種別)」)。

キーは英数字に正規化し、分類ごとに名前の違う項目 (指定番号 / 登録番号 /
告示番号など) は同じキーへ寄せる。日付は ISO 8601、値が空のキーは出さない。
重複する項目は詳細ページを正とし、CSV からは緯度経度だけを採る
([ADR 0008](docs/decisions/0008-normalize-schema-detail-page-wins.md) が対応表を含む正本、
実装は `src/heritage_crawler/record.py`)。

対応表に無いラベル・日付として読めない値・CSV と食い違う名称・所在地から
決めた都道府県は、**捨てずに実行のたび報告に出る**。サイト側の項目が増えたことに
気付けるのはここだけなので、報告に出たら対応表を見直す。

あわせて、データリポジトリのルートに `meta.json` を書く
([ADR 0014](docs/decisions/0014-machine-readable-dataset-metadata.md))。
JSON Lines を見ただけでは分からないものを機械可読で持つ。

| 項目 | 中身 |
|---|---|
| `source` | 出典表記と**利用日**。利用日は詳細ページを取得した日 (日本時間) |
| `labels` | キーの表示名。分類ごとに違う原文の呼び名を実測したもの |
| `facets` | 種別・時代などの取りうる値と件数 |
| `counts` / `files` | 収録件数・座標のある件数・ファイルごとの行数 |

利用日は規約が求める表示の一部で、散文に手で書くと更新のたびに嘘になる。
`meta.json` を正本にして、データを読む側が分類ごとの差異を知らずに済むようにする。

**日付をもう 1 つ、`status.json` に持つ**
([ADR 0023](docs/decisions/0023-stamp-every-check-into-the-data-repositories.md))。
`meta.json` の**利用日**が「そのデータを**取り出した**日」なのに対し、こちらの
**確認日**は「データベースを**見にいった**日」で、中身が動かない週も進む。片方だけでは
上流が静かなことと週次が止まっていることを区別できない。書くのは週次の
`update-records` だけで、`build-records` は触らない (生成物を決定的なまま保つため)。

### 週次 — `update-records`

**前回の状態はデータリポジトリの JSON Lines そのもの**として扱い、台帳と
突き合わせて差分だけを取り直す
([ADR 0018](docs/decisions/0018-detect-monthly-changes-with-the-ledger-and-a-rotation.md) /
[ADR 0020](docs/decisions/0020-check-weekly-by-diffing-the-ledger-csv.md))。
キャッシュも生 HTML も週次実行へ持ち回らない (**台帳の CSV だけは持ち回る**)。

```bash
heritage-crawler fetch-ledger                          # 台帳は毎週まるごと取り直す
heritage-crawler compare-ledgers --previous <前回の ledger/>   # CSV 同士で突き合わせる
heritage-crawler audit-listing                         # 網羅性を確かめる
heritage-crawler update-records --dry-run              # 何を取り直すかを見る
heritage-crawler update-records                        # 取り直して書き直す
```

取り直すのは 3 種類だけ。

1. 台帳に現れた新しいキー (新規指定)
2. **前回の台帳と値が食い違うキー** — CSV 同士なので 18 列すべてを素直に比べる。
   まずファイルごとにバイトで比べ、違ったファイルの行だけを突き合わせる
3. **巡回のぶん — 全体の 1/52。** `(台帳ID, 管理対象ID)` のハッシュを 52 で
   割った余りが実行週の ISO 週番号に一致するものを取る。ソース側に更新日が無く、
   詳細ページだけの項目 (解説文・員数など) の変更は取り直して比べるしか
   捕まえられないため

台帳から消えたキーは指定解除として落とすが、**その分類の網羅性が確かめられて
いるときに限る** — 取得の失敗や相手側の一時的な障害を指定解除と誤認して行を
消さないため。確かめられない分類の行は残したうえで報告に出す。

- `--dry-run` — 計画だけを出す (相手先へも出ず、出力も書き換えない)
- `--slot` — 巡回の枠 1〜52 (既定は実行週の ISO 週番号)
- `--previous-ledger` — 前回の台帳ディレクトリ。**渡さないと「値が変わった」を
  見つけられない** (追加・削除・巡回はそのまま働く)
- 利用日は**そのデータを取り出した日**。行が 1 つも動かなかった種別は前回の
  日付を据え置く
- 取り直していない行は前回の出力をそのまま使う。**生成物は決定的**なので、
  データが変わらない週は `meta.json` を含めて 1 バイトも差分が出ない
- **確認した日は毎週 `status.json` に残す**
  ([ADR 0023](docs/decisions/0023-stamp-every-check-into-the-data-repositories.md))。
  中身が動かなかった週も書くので、**静かなのか取得できていないのかがデータ
  リポジトリを見るだけで分かる**。`--checked-date` の既定は日本時間の今日、
  `--run-url` を渡すとその実行への入口も残る

### 件数表の作り直し — `render-readme`

書き出したデータを走査して、この README の
「[対象の文化財種別](#対象の文化財種別)」の表を作り直す (通信はしない)。

```bash
heritage-crawler render-readme            # 差し込み口の中身を書き直す
heritage-crawler render-readme --check    # 書き換えず、ずれていれば異常終了する
```

- 書き換えるのは差し込み口 (`<!-- generated: ... -->`) の中だけ
- **`meta.json` の申告件数と JSON Lines の行数が食い違ったら止まる。** 片方だけ
  古い状態から作ると、生成物なのに実態と合わないものができる
- `--check` は週次の差分更新でも走らせ、ずれていたら Issue に残す。CI では
  検証できない — `data` は追跡していないので、リポジトリの中にデータが無い

## データの出典と利用条件

データベースの掲載情報の著作権は文化庁にあり、
[利用規約](https://kunishitei.bunka.go.jp/top/policy) に従って利用する。

- **文字情報** — 出典を記載すれば自由に利用できる。商用利用・翻案も可
  (参照先の[文部科学省ウェブサイト利用規約](https://www.mext.go.jp/b_menu/1351168.htm)は
  政府標準利用規約 (第2.0版) に準拠し、CC BY 4.0 と互換である旨を明記している)
- **画像** — 作品毎に個別の許諾が必要なため、**このクローラーは画像を取得しない**

このクローラーが出力するデータには、次の出典表記を付す
([ADR 0007](docs/decisions/0007-redistribute-text-with-attribution.md))。

```
出典：「国指定文化財等データベース」（文化庁）
（https://kunishitei.bunka.go.jp/）（YYYY年M月D日に利用）
上記を加工して作成
```

**利用日 (`YYYY年M月D日`) は散文に書き込まない** — この README にも、各データ
リポジトリの README と LICENSE にも。データセットごとに違い、データが変わるたびに
動く (ADR 0020) ので、写せばドリフトする — そしてこの値は**ドリフトがそのまま
規約違反になる**。正本は各データリポジトリの `meta.json` で、日付を埋めた出典表記
そのものも `source.attribution` に組み立ててある
([ADR 0014](docs/decisions/0014-machine-readable-dataset-metadata.md) /
[ADR 0007](docs/decisions/0007-redistribute-text-with-attribution.md))。

データベースへのアクセスはレートに上限を設けて行い、User-Agent に連絡先を記載する
([ADR 0010](docs/decisions/0010-rate-limit-by-request-start.md))。

## 開発

Python で実装する ([ADR 0003](docs/decisions/0003-implement-in-python.md))。
CI と同じ内容をローカルで回せる。**相手先は公共サイトなので、手を入れる前に
[CONTRIBUTING.md](CONTRIBUTING.md) の約束事に目を通してほしい** (レートの上限を
詰めないこと・動作確認に本番を使わないこと)。

```bash
pip install -e '.[dev]'   # 初回のみ
ruff check .
mypy src
pytest -q
./scripts/check-freshness.sh
```

`pytest` は `tests/` に加えて `src/` の docstring 内の実例 (doctest) も実行する。
ゼロ詰め ID・未正規化の都道府県・和暦つきの日付といった非自明な入力を説明して
いる箇所なので、動く例であることを検査で保つ。

**CI からデータベースへはアクセスしない。** 外部サイトに依存するテストは不安定な
うえ、相手先に不要な負荷をかける。唯一の例外が疎通確認
(`.github/workflows/reachability.yml`) で、これは手で押したときだけ走る。

```bash
gh workflow run reachability.yml
```

差分更新は GitHub Actions の週次実行で回す
([ADR 0006](docs/decisions/0006-run-initial-crawl-locally-updates-on-actions.md)) が、
データセンター IP から取得できるかは相手先の WAF 次第で読めない。週次更新が使う
3 経路 — 台帳の CSV (CSRF + セッション)・検索結果一覧のページ送り・詳細ページ —
を既定で 126 件の分類 103 に対して通し、取れることを確かめる。**200 で返る
エラーページは取得層が弾いて失敗にする**ので、差し替えられていれば赤くなる
([ADR 0011](docs/decisions/0011-back-off-to-1-rps-and-detect-error-pages.md))。

### 週次の差分更新 (`.github/workflows/weekly.yml`)

毎週月曜 03:00 JST に走り、各データリポジトリを clone → 台帳を取り直す →
**前回の台帳とバイト単位で突き合わせる** → 変わったぶんだけ詳細を取り直す →
**確認日と、変わったぶんを push する**
([ADR 0020](docs/decisions/0020-check-weekly-by-diffing-the-ledger-csv.md) /
[ADR 0023](docs/decisions/0023-stamp-every-check-into-the-data-repositories.md))。
手で押すこともできる (`dry-run` なら**台帳までは取って計画を出し**、詳細の取得と push とサイトの起動はしない。`slot` で巡回の枠を指定)。

```bash
gh workflow run weekly.yml -f dry-run=true
```

- **前回の台帳は artifact で持ち回る** (499 ファイル / 11 MB、保持 90 日)。
  次回の基準になるので、push の成否によらず必ず残す
- **件数で当たりを付けない。** 増加と減少が同じ週に重なると数字が動かず、
  見逃すため。台帳を取り直すこと自体が正確さの担保になっている
- **データが変わらない週に動くのは確認日だけ。** 生成物が決定的で、利用日も
  「そのデータを取り出した日」なので動かない。各データリポジトリには
  `status.json` だけが動いた「◯◯ に確認 (差分なし)」のコミットが 1 つ立ち、
  中身が変わった週とはコミットメッセージで区別できる
  ([ADR 0023](docs/decisions/0023-stamp-every-check-into-the-data-repositories.md))。
  同じ確認日をサイトの「最終確認」にも渡す
- **押す手順は `scripts/push-data-repos.sh`。** データリポジトリへ実際に押す唯一の
  場所なので、ワークフローの YAML に埋めず、テストを当ててある
  (`scripts/test-push-data-repos.sh` が手元の bare リポジトリを押し先にして、
  コミットメッセージの出し分けと数え上げを検査する)
- **失敗したら Issue が立つ** (同じ Issue が open なら追記する。
  `scripts/report-issue.sh`)。誰も見ていないところで走るので、止まっていることに
  気付けるようにしておく。本文は**押す前に落ちたか、押したあとかで書き分ける** —
  一律に「データは前回のまま」と書くと、更新済みのデータを古いものと誤認する
- **サイトの起動で落ちてもジョブは赤くしない。** データの push は終わっており、
  配信と配布は heritages 側の保険 cron が拾う。赤にすると**取得そのものが止まった
  週と見分けが付かなくなる**ので、README の件数表と同じく Issue に残すだけにする
  (サイトの「最終確認」は古いままになるので、その旨も Issue に書く)
- 台帳の取り直しには `audit-listing --recover` を挟む。**分割軸の欄が空の行は
  どの地域でも引けず**、一覧から回収しないと網羅性が確かめられない
  ([ADR 0017](docs/decisions/0017-audit-completeness-with-the-search-listing.md) /
  [ADR 0026](docs/decisions/0026-audit-with-the-listing-where-its-keys-are-ledger-keys.md)。
  401 に 1 件、201 に 92 件実在する)。
  **CSV が 1 バイトも動いていない週は走らせない** (回収ぶんは artifact に残っている)
- **相手先が 504 を返す時間帯がある** (1 req/s を守っていても起きる)。取得は
  `scripts/retry.sh` で 5 分空けて繰り返す。60 秒の不調で 1 週ぶんの更新を
  落とさないため

push 先が別リポジトリなので `GITHUB_TOKEN` では足りない。`code4heritage` org に
GitHub App を作り、**対象のデータリポジトリすべて**に `contents: write` を、
**`heritages`** に `actions: write` を与えて、secret を 2 つ登録する
(押し終えてから heritages を起こすため。**確認日は渡さない** —
[ADR 0028](docs/decisions/0028-read-the-checked-date-from-the-data.md))。

| secret | 中身 |
|---|---|
| `DATA_PUSH_CLIENT_ID` | App の Client ID |
| `DATA_PUSH_PRIVATE_KEY` | App の秘密鍵 (PEM のまま) |

**App の権限はリポジトリごとには分けられない** (Permissions は App 単位で、
インストール先すべてに上限として付く)。代わりに**トークンを用途ごとに発行して
絞る**。ワークフローでは 3 回に分けている。

| 用途 | 対象 | 権限 |
|---|---|---|
| clone | インストール先すべて | `contents: write` |
| push | 同上 | `contents: write` |
| サイトを起こす | `heritages` だけ | `actions: write` / `variables: write` |

**push の直前にトークンを取り直すのが要**。App のトークンは 1 時間で切れるので、
取得に 30〜60 分かかると最初に発行したものでは押せなくなる。clone のときに
埋め込んだ URL も、取り直したトークンへ張り替えてから押す。

個人の PAT を使わないのは、**有効期限が切れた週に静かに失敗する**のを避けるため。
App のトークンは実行のたびに発行され、期限切れが無い。

設計判断は `docs/decisions/` の ADR に、進行状況と残る論点は
[Issue #1](https://github.com/shinyaoguri/heritage-crawler/issues/1) に記録している。

## ライセンス

- **コードとドキュメント** — MIT ([LICENSE](LICENSE))
- **取得したデータ** — 文化庁の利用規約に従う (上記「データの出典と利用条件」)

`LICENSE` には MIT の原文だけを置く。データが対象外であることを書き足すと
GitHub がライセンスを判定できなくなる (`NOASSERTION`) ので、2 層の分け方はここに書く。
