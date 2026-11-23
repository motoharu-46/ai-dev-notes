# tools/

サイト生成・公開に使うスクリプトと定期実行の手順書の控え（作業用の正本はプロジェクトの共有フォルダ `affiliate/` 側）。

- `scripts/publish.py` … チェック → content 保存 → ビルド → commit/push
- `scripts/build_site.py` … `content/*.md` から `public/` を生成（HTMLテンプレートもこの中）
- `scripts/check.py`, `scripts/common.py`, `scripts/rules-ai.json` … 公開前チェックと共通処理（本来は `templates/rules-ai.json` に置く）
- `routine/daily.md`, `routine/weekly.md` … 毎日・毎週の定期実行の指示文

認証情報・トークンは含めない。配信は `public/` だけなので、このフォルダは公開されない。
