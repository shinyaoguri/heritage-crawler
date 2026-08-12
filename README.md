# heritage-crawler

[国指定文化財等データベース](https://kunishitei.bunka.go.jp/bsys/index) (文化庁) から
建造物と記念物に関連したデータを抽出し、JSON Lines として記録するクローラー。

抽出したデータの出力先は文化財の種別ごとの別リポジトリで、このリポジトリは
クローラー本体のみを持つ
([ADR 0001](docs/decisions/0001-split-repositories-by-heritage-type.md) /
[ADR 0009](docs/decisions/0009-output-to-existing-per-type-repositories.md))。

## 対象の文化財種別

建造物と記念物に関連する 4 つの文化財分類を取る。分類コードは検索フォームの
`register_sub_id` かつ CSV の台帳ID で、取得の単位でもある
(定義は `src/heritage_crawler/catalog.py` の `TARGET_CATEGORIES` が正本)。

| 分類コード | 文化財種別 | 指定行為 | 指定件数 | 取得対象 |
|---|---|---|---|---|
| 101 | 登録有形文化財（建造物） | 登録 | 14,748 | 14,748 |
| 102 | 国宝・重要文化財（建造物） | 指定 | 2,633 | 5,587 |
| 103 | 重要伝統的建造物群保存地区 | 選定 | 126 | 126 |
| 401 | 史跡名勝天然記念物 | 指定 | 3,281 | 3,280 |
| **計** | | | **20,788** | **23,741** |

- **指定件数と取得対象は単位が違う。** 検索結果の件数表示は指定単位、CSV の行数と
  出力レコードは棟単位。棟に展開されるのは 102 だけで (1 指定あたり約 2.5 棟)、
  101・103・401 は指定 = 1 行。網羅性の判定に使えるのは指定件数だけ
  (`report-ledger` が地域合計と全国件数を突き合わせる)
- 指定行為の呼び方は分類から決まり、`designation_kind` として出力に載る
  (実装は `src/heritage_crawler/record.py` の `DESIGNATION_KINDS`)
- 件数は 2026-08-12 時点の実測。新規指定・解除で増減するため、厳密一致の検査では
  なく欠損の目安に使う
- 対象外は美術工芸品・民俗文化財・選定保存技術など。世界遺産 (901) も建造物・
  記念物とは別軸の指定なので含めない

出力先のリポジトリは分類と 1:1 ではない。102 は詳細ページの「国宝・重文区分」で
2 つに、401 は `種別１` / `種別２` で 6 つに分かれる (定義は
`src/heritage_crawler/catalog.py` の `TARGET_DATASETS` が正本、根拠は
[ADR 0009](docs/decisions/0009-output-to-existing-per-type-repositories.md) /
[ADR 0012](docs/decisions/0012-crawl-monuments-and-route-by-kind.md))。

| リポジトリ | 取得対象 |
|---|---|
| `registered-tangible-cultural-properties` | 101 登録有形文化財（建造物） |
| `national-treasures` | 102 のうち国宝 |
| `important-cultural-properties` | 102 の残り (重要文化財) |
| `important-preservation-districts-for-groups-of-traditional-buildings` | 103 重要伝統的建造物群保存地区 |
| `special-historic-sites` | 401 のうち特別史跡 |
| `historic-sites` | 401 のうち史跡 |
| `special-places-of-scenic-beauty` | 401 のうち特別名勝 |
| `places-of-scenic-beauty` | 401 のうち名勝 |
| `special-natural-monuments` | 401 のうち特別天然記念物 |
| `natural-monuments` | 401 のうち天然記念物 |

- **国宝は重要文化財の、特別◯◯ は ◯◯ のうちから指定される**が、リポジトリには
  排他に振り分ける (特別史跡は `historic-sites` には書かない)
- **401 には種別を 2 つ持つ複合指定がある** (旧浜離宮庭園 = 特別名勝 + 特別史跡)。
  どちらの一覧から見ても構成員なので、**両方のリポジトリへ同じ行を書く**。
  リポジトリの行数を足し上げても指定件数にはならない — 重複は `(台帳ID, 管理対象ID)`
  と `url` が同じなので名寄せできる
- **区分が読めなかったときの行き先は分類で違う。** 102 は重要文化財側へ落ちるが、
  401 はどこへも書かない (受け皿を置くと振り分けの誤りが史跡に紛れる)。
  どちらも件数は `build-records` の報告に出して見張る

伝統的建造物群保存地区 (`preservation-districts-for-groups-of-traditional-buildings`)
だけは**データが存在しない**ため空のまま。国が選定するのは「重要」伝統的建造物群
保存地区で、伝統的建造物群保存地区の決定は市町村が行うためデータベースの対象外。

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

初回の全件取得はローカルで実行し、以降の差分更新を GitHub Actions の月次実行で回す
([ADR 0006](docs/decisions/0006-run-initial-crawl-locally-updates-on-actions.md))。

## 使い方

```bash
pip install -e .
heritage-crawler fetch-ledger     # 1 段目: 分類 × 地域の CSV をキャッシュへ
heritage-crawler report-ledger    # 取得状況と網羅性を確かめる
heritage-crawler fetch-detail     # 2 段目: 台帳の各行から詳細ページをキャッシュへ
heritage-crawler report-detail    # 詳細ページの取得状況を確かめる
heritage-crawler build-records    # キャッシュから JSON Lines を組み立てる
```

取得はいずれも `cache/` 配下へ生の取得物のまま置き、**中断しても同じコマンドで
取得済みを飛ばして再開する**。取得系に共通のオプションは次のとおり。

- `--interval` — リクエスト間隔の秒数 = レートの上限 (既定 1.0 = 1 req/s)。
  **これ以上詰めない** — 相手が 200 でエラーページを返す
  ([ADR 0011](docs/decisions/0011-back-off-to-1-rps-and-detect-error-pages.md))
- `--contact` — User-Agent に載せる連絡先 (環境変数 `HERITAGE_CRAWLER_CONTACT` でも指定できる)
- `--category` / `--area` — 対象を絞る (繰り返し指定できる)
- `--force` — 取得済みも取り直す

### 1 段目 — `fetch-ledger`

4 分類 × 51 地域 (47 都道府県 + ２県以上 + 地域を定めない + 未正規化の値 2 つ)
を順に取得する (204 リクエスト / 約 30 分)。

分類ごとに地域で絞らない検索も 1 回行い、その全国件数と地域合計を突き合わせて
網羅性を確かめる。差が出たら報告に出る (負 = どの地域でも引けない指定がある、
正 = 地域をまたぐ指定が重複している)。

### 2 段目 — `fetch-detail`

台帳の各行の `(台帳ID, 管理対象ID)` から詳細ページの URL を組み立てて巡回し、
生 HTML を gzip でキャッシュへ落とす (23,741 件 / 1 req/s で約 6.6 時間)。
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

データベースへのアクセスはレートに上限を設けて行い、User-Agent に連絡先を記載する
([ADR 0010](docs/decisions/0010-rate-limit-by-request-start.md))。

## 開発

Python で実装する ([ADR 0003](docs/decisions/0003-implement-in-python.md))。
CI と同じ内容をローカルで回せる。

```bash
pip install -e '.[dev]'   # 初回のみ
ruff check .
mypy src
pytest -q
./scripts/check-freshness.sh
```

設計判断は `docs/decisions/` の ADR に、進行状況と残る論点は
[Issue #1](https://github.com/shinyaoguri/heritage-crawler/issues/1) に記録している。

## ライセンス

- **コード** — MIT ([LICENSE](LICENSE))
- **取得したデータ** — 文化庁の利用規約に従う (上記「データの出典と利用条件」)
