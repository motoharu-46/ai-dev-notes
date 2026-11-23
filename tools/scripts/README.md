# scripts/ — 公開前チェック・自動公開・公式ページ取得（メンバーC）

更新: 2026-11-09 / 対応する設計書: `design/site-design.md` v2（AI・プログラミング実践、公開先 = Cloudflare Pages）
Python 3.9+ の標準ライブラリのみ（pip 不要）。JS描画だけ、実行環境に同梱の Node 版 Playwright を使う。

## コマンド（定期実行が使うもの）

作業ディレクトリ `/mnt/project-files/affiliate` で実行する。サイトのリポジトリは `motoharu-46/ai-dev-notes`（プロジェクトに登録済み。セッション内では `/home/user/ai-dev-notes` に clone される）。

| やること | コマンド1行 | 終了コード |
|---|---|---|
| 公式ページ（料金・プラン）を読む | `python3 scripts/fetch_page.py <URL> --grep`（JS必須のページは `--browser` も付ける） | 0（結果はJSONの status を見る） |
| 公開前チェックだけ | `python3 scripts/check.py drafts/h-0001.md` | 0 合格 / 1 不合格 |
| チェック → 公開 → ログ・キュー更新 | `SITE_DIR=/home/user/ai-dev-notes python3 scripts/publish.py drafts/h-0001.md` | 0 公開 / 1 チェック不合格 / 3 push失敗 |
| 公開せず確認（ビルドまで） | 上の行に `--dry-run` を付ける | 同上 |
| サイトだけ作り直して push | `SITE_DIR=/home/user/ai-dev-notes python3 scripts/publish.py --rebuild` | 0 / 3 |

- **公開URL: https://ai-dev-notes.cloud-ruto02.workers.dev/** （Cloudflare Workers の静的アセット配信。A8.netの媒体登録にもこのURLを使う）。`SITE_BASE_URL` の既定もこのURL。サイト名は `SITE_NAME`（既定「動かして確かめるプログラミング帳」）。
- 同じ id をもう一度 `publish.py` すると更新扱いになる（`published_at` はそのまま、`updated_at` と記事上の「更新日」が新しくなる）。週次の再実行・更新はこれでよい。
- 不合格・失敗のときは `logs/errors.log` に1行（`日時<TAB>id<TAB>段階<TAB>理由`）を追記する。`keywords.csv` の status を `todo`/`skip` に戻すのは定期実行の側（設計書§5）。公開に成功したら `publish.py` が status を `published` にする。

## howto の実行ログ（最重要の判定）

作文された出力を落とすため、`check.py` は howto（`run_log_types`）で次をすべて必須にする。

1. front matter に `env:`（検証環境）があり、`os` とほかに1項目以上（言語・ライブラリのバージョン）がある。
2. front matter の `run_log:`（例 `logs/runs/h-0001.txt`）が共有フォルダ内に実在し、空でない。
3. 本文に「実行ログ」ラベル付きのコードブロックが1つ以上あり、**その中身が run_log のファイルに逐語で含まれている**（行末の空白と改行コードの違いだけ無視）。1文字でも作り替えると不合格。
4. 実行ログに APIキー・トークン・秘密鍵・パスワードらしき値があれば不合格。実行環境のパス（`/tmp/claude-…`、`/root/` など）が写っていれば警告。

ラベルの付け方（どちらでもよい）:

````markdown
```text 実行ログ
$ python3 -c "import pandas"
ModuleNotFoundError: No module named 'pandas'
```

**実行ログ**

```console
$ pip install pandas
```
````

ログの取り方（定期実行向け）: コマンドとその出力を、そのまま `logs/runs/{id}.txt` に追記していく（`{ echo '$ コマンド'; コマンド 2>&1; } >> logs/runs/h-0001.txt`）。記事にはその一部を切り出して貼る。作業用ディレクトリ（例 `~/work`）で実行すると、環境のパスが写りにくい。

## 各スクリプト

| ファイル | 役割 |
|---|---|
| `check.py` | 公開前チェック。判定ルールは **`templates/rules-ai.json`（B管理。既定の RULES_PROFILE=ai）を読む**。加えて、id・type・categories の形、howto の実行ログ照合、A8リンクが `links/a8-links.json` の `html` と完全一致しているか（a8.net の URL と `a8mat=` だけを見る。本文中の「A8.net」という語は対象外）、`services-master.json` の値の鮮度、公開済み記事とのタイトル重複・本文の類似（文字5-gramで50%以上は不合格）を見る |
| `publish.py` | 差し込み記号を展開 → チェック → 投稿 → `logs/publish-log.csv` 追記（§4.5の列）→ `keywords.csv` 更新 |
| `build_site.py` | 静的サイト生成。`SITE_DIR/content/*.md` から `SITE_DIR/public/` を毎回まるごと作る。記事ページ、トップ（新着）、カテゴリ一覧、関連記事（related＋同テーマの howto 最大3件）、`{{works_list}}` の一覧、`sitemap.xml`、`robots.txt`、`404.html`、canonical、公開日・更新日、構造化データ(Article)、コードブロック（実行ログのラベル付き） |
| `fetch_page.py` | 公式ページを取得して本文テキストと料金らしき表記を JSON で返す。本文は既定で先頭60,000字（`--max N` で変更、切れたら `truncated: true`）。`--grep` を付けると、切り詰める前の全文から「料金・円・税込・月額・プラン・無料」を含む行を前後2行つきで `grep.matches` に抜き出す（`--grep 料金,年額` のように語の指定も可）。値を表に書くかは定期実行の Claude が本文を読んで決める。robots.txt で禁止されたURLは開かない |
| `render.js` | JS必須ページの描画（Node 版 Playwright） |
| `common.py` | front matter 読み込み（入れ子の `env:` とインライン辞書 `{a: 1}` に対応）、Markdown→HTML、ログ入出力、ジャンル設定 |
| `archive-vod/` | 動画配信ジャンル用の旧スクリプト（使わない） |

固定ページは `drafts/p-*.md`（`type: page`）。`publish.py` で他の記事と同じように公開でき、全ページのフッターにリンクが出る。

## 差し込み記号（publish.py と check.py が展開する）

| 記号 | 中身 |
|---|---|
| `{{a8:<service_id>}}`（1行だけで書く） | `links/a8-links.json` の `html` をそのまま。なければ行ごと削除 |
| `{{<項目>:<service_id>}}`（文中のどこでも） | `research/services-master.json` の値。例 `{{price:xserver}}` `{{trial:xserver}}`。項目がなければ残って不合格 |
| `{{works_list}}`（1行だけで書く） | その記事の `service_id`（id から接頭辞を除いたもの、または front matter の `service_id`）を `services_available` に持つ howto の一覧（ビルド時に自動生成） |
| `{{blog_name}}` | 環境変数 `SITE_NAME` |

差し込んだ値の確認日が35日より古い、または `verified` が true でないとき、`strict_fact_types`（既定は tool・school）では不合格、それ以外の種類では警告。値の出典が front matter の `sources` にないときも警告。コードブロックの中は、禁止表現・差し込み記号の残り・見出し順・表の判定から外し、文字数も地の文だけで数える。

## ジャンル設定

既定値は設計書 v2 のとおり（種類 howto/guide/tool/school/page、カテゴリ §2.2、A8上限 howto 1・guide 2・tool/school 3）。変えるときは rules ファイルに `"site"` を書く。

```json
"site": {
  "types": {"howto": "h-", "guide": "g-", "tool": "t-", "school": "s-"},
  "kind_categories": {"howto": "手順・エラー解決", "guide": "学習ロードマップ", "tool": "サーバー・ツール", "school": "スクール"},
  "genres": ["Python", "JavaScript", "生成AI", "Web制作", "サーバー・公開", "Git・開発環境"],
  "category_slugs": {"手順・エラー解決": "howto", "生成AI": "genai"},
  "listing_type": "howto",
  "a8_limits": {"howto": 1, "guide": 2, "tool": 3, "school": 3},
  "strict_fact_types": ["tool", "school"],
  "run_log_types": ["howto"]
}
```

別のルール一式を試すときは `templates/rules-<名前>.json` を作り `RULES_PROFILE=<名前>` で切り替える（チェックとビルドの両方に同じ値を渡す）。

## むーさんの初回作業

1. ~~GitHub に空のリポジトリを作る~~ → `motoharu-46/ai-dev-notes`（Private）で作成済み。初期ファイルも push 済み。
2. ~~Cloudflare に接続~~ → Workers として `ai-dev-notes` を接続済み（リポジトリの `wrangler.jsonc` が `public/` を配信）。URL は https://ai-dev-notes.cloud-ruto02.workers.dev/ 。push のたびに自動でデプロイされる。
3. **実行環境のネットワーク設定に、下の許可ドメインを追加**（Project settings → Cloud environment → 環境の歯車 → Network access を Custom にして Allowed domains に貼る。「Also include default list of common package managers」は**チェックしたまま**。pip・npm での howto の実行に使う）。
4. A8.net の提携が済んだら、広告リンクを `links/a8-links.json` に入れる（設計書§4.3）。

git push は環境の GitHub 連携で行うので、鍵の登録はいらない。

### 許可ドメイン（Allowed domains にそのまま貼る）

A の案件確定前の暫定版（`research/programs.csv` と `services-master.json` の公式URLから作成）。案件が確定したら、外れたものを消し、増えたものを足す。

```
api.indexnow.org
xserver.ne.jp
*.xserver.ne.jp
conoha.jp
*.conoha.jp
onamae.com
*.onamae.com
sakura.ad.jp
*.sakura.ad.jp
lolipop.jp
*.lolipop.jp
value-ai-writer.com
*.value-ai-writer.com
winschool.jp
*.winschool.jp
skillhacks.jp
*.skillhacks.jp
daytra.co.jp
*.daytra.co.jp
techacademy.jp
*.techacademy.jp
```

- `api.indexnow.org`: 公開・更新のたびに検索エンジンへURLを通知するため（下の「IndexNow」）。
- 用途: tool（サーバー）・school（スクール）記事の料金・プランを公式ページで確認するため。
- howto の実行に使うパッケージ置き場（pypi.org、registry.npmjs.org など）は既定リストに入っているので書かなくてよい（チェックを外さないこと）。
- GitHub（git push）は環境の GitHub 連携の経路なので書かなくてよい。
- 動画配信サービスのドメインは不要になったので入れない。
- JS で料金表を描くサイトは別ドメイン（API・画像配信）を使うことがある。`fetch_page.py` が `blocked` を返したら、出力の `host` を足す。

## テスト結果（2026-11-06、この環境）

- `check.py`（howto）: 実際に実行して取ったログ（pandas の ModuleNotFoundError → pip install → import 成功）で書いた試験原稿は合格。出力を1行書き足した原稿、`run_log` のファイルがない原稿、`env` がない原稿、APIキーらしき値を含む原稿は、それぞれ不合格になった。コードブロック内の `{{ }}` や `# ##` は誤検知しなかった。
- `publish.py`（static）: ローカルの git リモートに対して、チェック → content 保存 → ビルド → commit → push → ログ追記 → keywords.csv 更新まで通った。本物のリポジトリ `ai-dev-notes` には、記事なしの初期サイトを push 済み。
- `fetch_page.py`: 仕組みは確認済み。サーバー・スクールの公式サイトは、許可ドメインを追加するまでこの環境からは `blocked`。
- `publish.py`（hatena）: 使わない（設計書 v1.1 で媒体を変更）。コードは残してある。

## PR表記（2026-11-07 変更）

- PR表記「※本記事はアフィリエイト広告（PR）を含みます。」は、**A8リンク（`{{a8:...}}` の展開結果）がある記事だけ**、本文1行目に書く。広告のない howto には付けない。
- `check.py`: A8リンクがあるのに1行目がPR表記でなければ不合格。A8リンクがないのにPR表記があれば警告。ルールに `"always": true` を付けると全記事で必須に戻る。
- `build_site.py`: 本文1行目のPR表記は、タイトル直下に `.pr-note`（小さめの灰色の文字1行、枠・太字なし）として出し、本文からは外す。

## サイト直下に置くファイル（2026-11-09 追加）

- `build_site.py` は `public/` を毎回まるごと作り直すので、サイト直下に置きたい固定ファイルは **`SITE_DIR/static/`** に入れる。ビルドのたびに `public/` 直下へそのままコピーされ、`publish.py` が static/ も commit する。
- Google Search Console の HTML 確認ファイル（例 `google1234abcd.html`）も、受け取ったら中身を変えずに `static/` へ置いて `publish.py --rebuild` する。

## IndexNow（2026-11-09 追加）

- キー: `static/15f95cfd97663a9b747bfbf0753e8384.txt`（中身はキーそのもの）。公開済み: https://ai-dev-notes.cloud-ruto02.workers.dev/15f95cfd97663a9b747bfbf0753e8384.txt
- `publish.py` は公開・更新が push まで成功したあと、記事URLとトップURLを `https://api.indexnow.org/indexnow` に POST する（Bing など参加検索エンジンへ共有される。Google は対象外なので sitemap.xml で）。結果は出力 JSON の `indexnow`（`ok 200`/`ok 202` が成功）。
- 失敗しても公開は止めず、終了コードも変えない。`logs/errors.log` に `indexnow<TAB>警告: …` を1行書く。`api.indexnow.org` が許可ドメインにない間は `Tunnel connection failed: 403` になる。
- 止めたいときは `INDEXNOW=0`。キーを差し替えるときは `static/` のファイルを入れ替える（環境変数 `INDEXNOW_KEY` でも上書きできる）。`--dry-run` と `--rebuild` では通知しない。

## howto のA8リンク（2026-11-09 改定、設計書§2.3）

- ルール `howto_a8_allowed`（rules-ai.json）: テーマ（categories[1]）ごとに置ける案件と場所を決める。サーバー・公開 = xserver / onamae を1つ。Python・JavaScript = skillhacks を「## うまくいかないとき」節の**末尾**に1つ。タイトルが `exclude_title_regex`（pip install・git・環境構築など）に当たる記事と、ほかのテーマには置けない。
- PR表記は、A8リンクが展開された本文の1行目になければ `check.py` が自動で入れる（警告「PR表記を本文1行目に自動で入れました」が出る。公開される本文もこれ）。
- A8リンクの改変検知は、a8-links.json の `html` が複数行（a タグと計測用 img）でも、各行が一致すれば通る。
