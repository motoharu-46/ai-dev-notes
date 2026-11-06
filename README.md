# ai-dev-notes

AI・プログラミング実践ブログの公開用リポジトリです。Cloudflare Pages がこのリポジトリの `public/` をそのまま配信します。

- `content/` … 公開済み記事の原稿（Markdown）。公開前チェックを通ったものだけが入る
- `public/` … `content/` から自動生成したサイト（手で編集しない）

記事の追加・更新は、定期実行のスクリプト（`publish.py`）が自動で commit / push します。

Cloudflare の設定: `wrangler.jsonc` で `public/` を静的アセットとして配信（Workers）。ビルドコマンドなし、デプロイコマンド `npx wrangler deploy`。
