# heritage-crawler

[国指定文化財等データベース](https://kunishitei.bunka.go.jp/bsys/index) (文化庁) から
建造物に関連したデータを抽出し、JSON Lines として記録するクローラー。

対象は文化財分類コード 101 (登録有形文化財)・102 (国宝・重要文化財)・
103 (重要伝統的建造物群保存地区)。抽出したデータの出力先は別リポジトリで、
このリポジトリはクローラー本体のみを持つ
([ADR 0001](docs/decisions/0001-split-repositories-by-heritage-type.md))。

## しくみ

取得は 2 段構え ([ADR 0002](docs/decisions/0002-two-stage-fetch-csv-then-detail.md))。

1. 分類 × 都道府県で検索して CSV の台帳を取得する (緯度経度はここにしかない)
2. 台帳の `(台帳ID, 管理対象ID)` から詳細ページ URL を組み立てて取得する
   (解説文・構造及び形式等・員数などはここにしかない)
3. 両者を `(台帳ID, 管理対象ID)` で結合して JSON Lines を書き出す

初回の全件取得はローカルで実行し、以降の差分更新を GitHub Actions の月次実行で回す
([ADR 0006](docs/decisions/0006-run-initial-crawl-locally-updates-on-actions.md))。

## 使い方

```bash
pip install -e .
heritage-crawler fetch-ledger     # 1 段目: 分類 × 地域の CSV をキャッシュへ
heritage-crawler report-ledger    # 取得状況を既知の指定件数と突き合わせる
```

`fetch-ledger` は 3 分類 × 49 地域 (47 都道府県 + ２県以上 + 地域を定めない) を
順に取得し、`cache/` 配下へ生の CSV のまま置く。**中断しても同じコマンドで
取得済みを飛ばして再開する**。

- `--interval` — リクエスト間隔の秒数 (既定 1.0)。逐次アクセスは既定のふるまい
- `--contact` — User-Agent に載せる連絡先 (環境変数 `HERITAGE_CRAWLER_CONTACT` でも指定できる)
- `--category` / `--area` — 対象を絞る (繰り返し指定できる)
- `--force` — 取得済みも取り直す

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

データベースへのアクセスは逐次・間隔を空けて行い、User-Agent に連絡先を記載する。

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
