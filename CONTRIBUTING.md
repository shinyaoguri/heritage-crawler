# 開発に参加するときに

このリポジトリは公共サイト ([国指定文化財等データベース](https://kunishitei.bunka.go.jp/bsys/index)、
文化庁) を相手にするクローラーです。**相手先に負荷をかけないことが、他のどの品質より
先に来ます。** 以下はそのための約束事です。

## 相手先を叩かない

### レートの上限を詰めない

`--interval` の既定は 1.0 秒 = **1 req/s** で、これが上限です。

**超えると相手は HTTP 200 のままエラーページを返します。** 4 req/s を試したときは
応答の 15% が壊れ、17,338 件中 2,696 件を取り直すことになりました。速くなるどころか
相手に無駄な負荷をかけただけで、以後**相手の限界を探る試行はしません**
([ADR 0011](docs/decisions/0011-back-off-to-1-rps-and-detect-error-pages.md))。

並列度 (`--concurrency`) を上げても上限は超えません。レートの上限を決めるのは間隔
だけで、埋まるのは応答待ちの隙間です
([ADR 0010](docs/decisions/0010-rate-limit-by-request-start.md))。

### 動作確認に本番を使わない

動きを見たいだけなら、**まずテストで足ります。** 実応答を切り出した fixture が
`tests/fixtures/` にあり、解析は通信なしで全部試せます
([tests/fixtures/README.md](tests/fixtures/README.md))。

どうしても実際に取る必要があるときは、いちばん軽い分類を少量だけにしてください。

```bash
heritage-crawler fetch-detail --category 103 --limit 5 --contact "あなたの連絡先"
```

`--contact` は User-Agent に載ります。相手先が問い合わせられる先を必ず書いてください
(環境変数 `HERITAGE_CRAWLER_CONTACT` でも渡せます)。

### CI からはデータベースへ出ない

**外部サイトへアクセスするテストは置きません。** 不安定なうえ、PR のたびに相手先を
叩くことになります。唯一の例外が疎通確認 (`.github/workflows/reachability.yml`) で、
これは `workflow_dispatch` のみ — 人が押したときしか走りません。

## 画像は扱わない

画像は作品毎に個別の許諾が必要なので、**取得も再配布もしません**
([ADR 0007](docs/decisions/0007-redistribute-text-with-attribution.md))。
「写真の有無」をメタデータとして記録するところまでです。

文字情報は出典表示のもとで再配布できます。出力データには出典表記を必ず付します。

## 手元で回す検査

CI と同じ内容です。

```bash
pip install -e '.[dev]'   # 初回のみ
ruff check .
mypy src
pytest -q
./scripts/check-freshness.sh
```

`pytest` は `tests/` に加えて `src/` の docstring 内の実例 (doctest) も実行します。

**README の表は生成物です。** 手で書き換えず、`heritage-crawler render-readme` で
作り直してください。出力先リポジトリの表がずれているとテストが赤くなります
(件数表の方は書き出したデータを読むので、手元にデータが無ければ週次の実行に任せて
構いません)。

## 変更の出し方

- **main が唯一の長命ブランチ**です。main から `<type>/<短い説明>` のブランチを切り、
  小さく作って早めに PR を出してください (作業中なら Draft で)
- コミットと PR タイトルは
  [Conventional Commits](https://www.conventionalcommits.org/ja/v1.0.0/) —
  `<type>(<scope>): <要約>`。type は feat / fix / docs / refactor / test / chore /
  ci / perf / build。**squash merge なので PR タイトルがそのまま main の
  コミットメッセージ**になります (`scripts/check-pr-title.sh` が検査します)
- PR 本文に目的・変更点・確認方法を書いてください。Issue を閉じる `Closes #N` も
  **コミットではなく PR 本文**に書きます (squash ではコミット側の名乗りが GitHub へ
  届きません)
- **テストは実装と同じ PR に含めます。** バグ修正は失敗する再現テストが先、
  正常系に加えて失敗系・境界値を最低ひとつ
- 1 PR = 1 関心事。本題以外の改善に気付いたら直接直さず Issue を立ててください

設計判断は `docs/decisions/` の ADR に記録しています。仕組みの全体像は
[データが流れる道すじ](docs/pipeline.md) が 1 枚にまとめています。
