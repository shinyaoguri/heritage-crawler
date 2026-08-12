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

<!-- generated: heritage-crawler render-readme -->
| 分類コード | 文化財種別 | 指定行為 | 指定件数 | 取得対象 | 収録行数 |
|---|---|---|---|---|---|
| 101 | 登録有形文化財（建造物） | 登録 | 14,748 | 14,748 | 14,748 |
| 102 | 国宝・重要文化財（建造物） | 指定 | 2,633 | 5,587 | 5,587 |
| 103 | 重要伝統的建造物群保存地区 | 選定 | 126 | 126 | 126 |
| 401 | 史跡名勝天然記念物 | 指定 | 3,281 | 3,281 | 3,395 |
| **計** | | | **20,788** | **23,742** | **23,856** |
<!-- /generated -->

- **この表は生成物。手で書き換えない。** 指定件数は
  `src/heritage_crawler/catalog.py` の `TARGET_CATEGORIES`、取得対象と収録行数は
  書き出したデータ (各データリポジトリの `meta.json` と JSON Lines) が正本で、
  `heritage-crawler render-readme` が差し込み口の中身を作り直す。散文に写した
  件数は新規指定・解除のたびに静かに嘘になる
- **指定件数と取得対象は単位が違う。** 検索結果の件数表示は指定単位、CSV の行数と
  出力レコードは棟単位。棟に展開されるのは 102 だけで (1 指定あたり約 2.5 棟)、
  101・103・401 は指定 = 1 行。網羅性の判定に使えるのは指定件数だけ
  (`report-ledger` が地域合計と全国件数を突き合わせる)
- **取得対象と収録行数の差は 401 の複合指定。** 種別を 2 つ持つ指定は両方の
  リポジトリへ同じ行を書くので (ADR 0012)、行数を足し上げると異なりキー数を
  上回る。突き合わせに使えるのは取得対象 (異なり) の側
- 指定行為の呼び方は分類から決まり、`designation_kind` として出力に載る
  (実装は `src/heritage_crawler/record.py` の `DESIGNATION_KINDS`)
- 指定件数は 2026-08-11 の実測。新規指定・解除で増減するため、厳密一致の検査では
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

クローラーが書くのはこの 10 リポジトリちょうど。**「重要」の付かない伝統的建造物群
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

初回の全件取得はローカルで実行し、以降の差分更新を GitHub Actions の月次実行で回す
([ADR 0006](docs/decisions/0006-run-initial-crawl-locally-updates-on-actions.md))。
差分は台帳の突き合わせと 1/12 の巡回で見つける
([ADR 0018](docs/decisions/0018-detect-monthly-changes-with-the-ledger-and-a-rotation.md)。
`update-records`)。

## 使い方

```bash
pip install -e .
heritage-crawler fetch-ledger     # 1 段目: 分類 × 地域の CSV をキャッシュへ
heritage-crawler report-ledger    # 取得状況と網羅性を確かめる
heritage-crawler audit-listing    # 検索結果一覧と突き合わせて取りこぼしを名指しする
heritage-crawler fetch-detail     # 2 段目: 台帳の各行から詳細ページをキャッシュへ
heritage-crawler report-detail    # 詳細ページの取得状況を確かめる
heritage-crawler build-records    # キャッシュから JSON Lines を組み立てる
heritage-crawler update-records   # 月次: 前回の出力と突き合わせて差分だけ取り直す
heritage-crawler render-readme    # この README の件数表を書き出したデータから作り直す
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
生 HTML を gzip でキャッシュへ落とす (23,742 件 / 1 req/s で約 6.6 時間)。
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

### 月次 — `update-records`

**前回の状態はデータリポジトリの JSON Lines そのもの**として扱い、台帳と
突き合わせて差分だけを取り直す
([ADR 0018](docs/decisions/0018-detect-monthly-changes-with-the-ledger-and-a-rotation.md))。
キャッシュも生 HTML も月次実行へ持ち回らない。

```bash
heritage-crawler fetch-ledger                  # 台帳は毎月まるごと取り直す
heritage-crawler audit-listing                 # 網羅性を確かめる
heritage-crawler update-records --dry-run      # 何を取り直すかを見る
heritage-crawler update-records                # 取り直して書き直す
```

取り直すのは 3 種類だけ。

1. 台帳に現れた新しいキー (新規指定)
2. 台帳の値が前回の出力と食い違うキー — 名称・所在地・所有者名・時代・種別・
   緯度経度で比べる (**手元の 23,742 件で一致率を実測して選んだ列**)
3. **巡回のぶん — 全体の 1/12。** `(台帳ID, 管理対象ID)` のハッシュを 12 で
   割った余りが実行月に一致するものを取る。ソース側に更新日が無く、詳細ページ
   だけの項目 (解説文・員数など) の変更は取り直して比べるしか捕まえられないため

台帳から消えたキーは指定解除として落とすが、**その分類の網羅性が確かめられて
いるときに限る** — 取得の失敗や相手側の一時的な障害を指定解除と誤認して行を
消さないため。確かめられない分類の行は残したうえで報告に出す。

- `--dry-run` — 計画だけを出す (相手先へも出ず、出力も書き換えない)
- `--month` — 巡回の枠に使う月 (既定は実行月)
- 利用日は**実行日** (日本時間)。既存の行がいつ取得されたかは出力から分からず、
  台帳は毎月まるごと取り直しているため
- 取り直していない行は前回の出力をそのまま使う。**生成物は決定的**なので、
  データが変わらない月は JSON Lines に 1 バイトも差分が出ない
  (`meta.json` の利用日だけは毎月動く)

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
- `--check` は月次の差分更新でも走らせ、ずれていたら Issue に残す。CI では
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

**利用日 (`YYYY年M月D日`) をこの README に書き込まない。** データセットごとに違い、
月次更新では実行日になる (ADR 0018) ので、写せば毎月ドリフトする — そしてこの値は
**ドリフトがそのまま規約違反になる**。正本は各データリポジトリの `meta.json` で、
日付を埋めた出典表記そのものも `source.attribution` に組み立ててある (ADR 0014)。

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

`pytest` は `tests/` に加えて `src/` の docstring 内の実例 (doctest) も実行する。
ゼロ詰め ID・未正規化の都道府県・和暦つきの日付といった非自明な入力を説明して
いる箇所なので、動く例であることを検査で保つ。

**CI からデータベースへはアクセスしない。** 外部サイトに依存するテストは不安定な
うえ、相手先に不要な負荷をかける。唯一の例外が疎通確認
(`.github/workflows/reachability.yml`) で、これは手で押したときだけ走る。

```bash
gh workflow run reachability.yml
```

差分更新は GitHub Actions の月次実行で回す
([ADR 0006](docs/decisions/0006-run-initial-crawl-locally-updates-on-actions.md)) が、
データセンター IP から取得できるかは相手先の WAF 次第で読めない。月次更新が使う
3 経路 — 台帳の CSV (CSRF + セッション)・検索結果一覧のページ送り・詳細ページ —
を既定で 126 件の分類 103 に対して通し、取れることを確かめる。**200 で返る
エラーページは取得層が弾いて失敗にする**ので、差し替えられていれば赤くなる
([ADR 0011](docs/decisions/0011-back-off-to-1-rps-and-detect-error-pages.md))。

### 月次の差分更新 (`.github/workflows/monthly.yml`)

毎月 2 日 03:00 JST に走り、10 のデータリポジトリを clone → 台帳を取り直す →
差分だけ詳細を取り直す → 変わったリポジトリだけ push する (所要 約 70 分)。
手で押すこともできる (`dry-run` で計画だけ、`month` で巡回の枠を指定)。

```bash
gh workflow run monthly.yml -f dry-run=true
```

- **データが変わらない月は JSON Lines が動かない** (生成物が決定的なため)。
  動くのは `meta.json` の利用日だけで、その月のコミットは
  `chore(data): YYYY-MM に確認 (データの変更なし)` になる。**履歴で「確認しただけの
  月」と「データが変わった月」を見分けられる**
- **失敗したら Issue が立つ** (同じ Issue が open なら追記する)。誰も見ていない
  ところで走るので、止まっていることに気付けるようにしておく
- 台帳の取り直しには `audit-listing --recover` を挟む。**都道府県が空の行は
  どの地域でも引けず**、一覧から回収しないと網羅性が確かめられない
  ([ADR 0017](docs/decisions/0017-audit-completeness-with-the-search-listing.md))
- **相手先が 504 を返す時間帯がある** (1 req/s を守っていても起きる)。取得は
  `scripts/retry.sh` で 5 分空けて繰り返す。60 秒の不調で 1 か月ぶんの更新を
  落とさないため

push 先が別リポジトリなので `GITHUB_TOKEN` では足りない。`code4heritage` org に
GitHub App を作り、**対象の 10 リポジトリにだけ**インストールして
`contents: write` を与え、secret を 2 つ登録する。

| secret | 中身 |
|---|---|
| `DATA_PUSH_CLIENT_ID` | App の Client ID |
| `DATA_PUSH_PRIVATE_KEY` | App の秘密鍵 (PEM のまま) |

個人の PAT を使わないのは、**有効期限が切れた月に静かに失敗する**のを避けるため。
App のトークンは実行のたびに発行され、期限切れが無い。

設計判断は `docs/decisions/` の ADR に、進行状況と残る論点は
[Issue #1](https://github.com/shinyaoguri/heritage-crawler/issues/1) に記録している。

## ライセンス

- **コード** — MIT ([LICENSE](LICENSE))
- **取得したデータ** — 文化庁の利用規約に従う (上記「データの出典と利用条件」)
