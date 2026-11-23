#!/usr/bin/env python3
"""静的サイトのビルド（Cloudflare Pages 用）。

SITE_DIR/content/*.md（公開済み記事の原稿）から SITE_DIR/public/ を全部作り直す。
Cloudflare Pages 側の設定は「ビルドコマンドなし・出力ディレクトリ public」。

使い方:
    python3 scripts/build_site.py      # SITE_DIR/public を再生成
環境変数:
    SITE_DIR       サイト用 git リポジトリのパス（既定: <affiliate>/site）
    SITE_BASE_URL  公開URL（例 https://example.pages.dev）。canonical と sitemap に使う
    SITE_NAME      サイト名（既定: 作品別 配信状況ノート）
    SITE_DESC      サイトの説明文
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

SITE_DIR = Path(os.environ.get("SITE_DIR", C.ROOT / "site"))
CONTENT = SITE_DIR / "content"
PUBLIC = SITE_DIR / "public"
STATIC = SITE_DIR / "static"  # そのまま public/ 直下へコピーするファイル（IndexNow キー、Search Console 確認ファイルなど）
BASE_URL = os.environ.get("SITE_BASE_URL", "https://ai-dev-notes.cloud-ruto02.workers.dev").rstrip("/")
SITE_NAME = os.environ.get("SITE_NAME", "動かして確かめるプログラミング帳")
SITE_DESC = os.environ.get("SITE_DESC", "Python・生成AI・Web制作のエラー解決と手順を、実際に実行したログつきでまとめています。")

SITE = C.site_profile()
KIND_ORDER = list(SITE["kind_categories"].values())
LISTING_TYPE = SITE.get("listing_type", "work")  # サービス記事の一覧に並べる記事の種類


def slug(name: str) -> str:
    return C.category_slug(name, SITE)

MAX_SERVICE_WORKS = 20
MAX_RELATED = 3

CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e3e3e8;--accent:#0b62d6;--soft:#f5f6f8}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#ececf0;--muted:#a1a1a8;--line:#333338;--accent:#6aa8ff;--soft:#202024}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.85 -apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif}
a{color:var(--accent)}header,main,footer{max-width:760px;margin:0 auto;padding:0 16px}
header{padding-top:20px;padding-bottom:12px;border-bottom:1px solid var(--line)}header a{color:var(--fg);text-decoration:none;font-weight:700}
nav{font-size:14px;margin-top:6px}nav a{margin-right:12px;color:var(--muted)}
h1{font-size:1.55rem;line-height:1.4;margin:24px 0 8px}h2{font-size:1.25rem;margin-top:2.2em;padding-bottom:4px;border-bottom:2px solid var(--line)}h3{font-size:1.08rem;margin-top:1.8em}
.meta{color:var(--muted);font-size:13px}.pr-note{color:var(--muted);font-size:12px;margin:4px 0 0}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:14px;margin:1em 0}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{background:var(--soft)}
blockquote{margin:1em 0;padding:8px 14px;border-left:4px solid var(--line);color:var(--muted)}
.cards{list-style:none;padding:0}.cards li{padding:10px 0;border-bottom:1px solid var(--line)}.cards .meta{display:block}
footer{margin-top:48px;padding-top:16px;padding-bottom:32px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}footer a{color:var(--muted);margin-right:12px}
img{max-width:100%;height:auto}
.code-label{font-size:12px;color:var(--muted);margin:1em 0 -6px}pre{background:var(--soft);border:1px solid var(--line);border-radius:6px;padding:12px 14px;overflow-x:auto;font-size:13.5px;line-height:1.6}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em}p code,li code,td code{background:var(--soft);padding:1px 5px;border-radius:4px}
"""


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def load_articles(pending: dict | None = None) -> list[dict]:
    log = {r["id"]: r for r in C.read_log()}
    if pending:  # これから公開する記事（ログ追記前）の日付
        log[pending["id"]] = {**log.get(pending["id"], {}), **pending}
    arts = []
    for p in sorted(CONTENT.glob("*.md")):
        meta, body = C.load_draft(p)
        aid = str(meta.get("id", p.stem))
        lr = log.get(aid, {})
        published = (lr.get("published_at") or meta.get("published_at") or meta.get("checked_at") or "")[:10]
        updated = (lr.get("updated_at") or meta.get("updated_at") or published)[:10]
        arts.append({
            "id": aid, "type": meta.get("type", ""), "title": str(meta.get("title", aid)),
            "categories": C.as_list(meta.get("categories")),
            "services": C.as_list(meta.get("services_available")),
            "service_id": meta.get("service_id") or (aid[2:] if aid.startswith("s-") else ""),
            "related": C.as_list(meta.get("related")),
            "checked_at": str(meta.get("checked_at", "")),
            "published": published, "updated": updated, "meta": meta, "body": body,
            "url": f"{BASE_URL}/{aid}/",
        })
    arts.sort(key=lambda a: (a["published"], a["id"]), reverse=True)
    return arts


def description(body: str, meta: dict | None = None) -> str:
    """meta description / og:description。原稿 front matter の description を優先し、無いときだけ本文から作る。"""
    fm = re.sub(r"\s+", " ", str((meta or {}).get("description") or "")).strip()
    if fm:
        return fm
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith(("※", "#", "|", "<", ">", "-")) or "時点の各公式サイトの情報" in s:
            continue
        t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
        t = re.sub(r"[*`]", "", t)
        return t[:118] + ("…" if len(t) > 118 else "")
    return SITE_DESC


def page(title: str, body_html: str, *, canonical: str, desc: str, pages: list[dict],
         jsonld: dict | None = None, noindex: bool = False) -> str:
    cats = "".join(f'<a href="/category/{slug(c)}/">{esc(c)}</a>' for c in KIND_ORDER)
    fixed = "".join(f'<a href="/{p["id"]}/">{esc(p["title"])}</a>' for p in pages)
    ld = f'<script type="application/ld+json">{json.dumps(jsonld, ensure_ascii=False)}</script>' if jsonld else ""
    robots = '<meta name="robots" content="noindex">' if noindex else ""
    full_title = title if title == SITE_NAME else f"{title} | {SITE_NAME}"
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(full_title)}</title>
<meta name="description" content="{esc(desc)}">
<link rel="canonical" href="{esc(canonical)}">{robots}
<meta property="og:title" content="{esc(title)}"><meta property="og:description" content="{esc(desc)}">
<meta property="og:url" content="{esc(canonical)}"><meta property="og:site_name" content="{esc(SITE_NAME)}"><meta property="og:type" content="article">
<style>{CSS}</style>{ld}
</head><body>
<header><a href="/">{esc(SITE_NAME)}</a><nav>{cats}</nav></header>
<main>
{body_html}
</main>
<footer>{fixed}<div>© {dt.date.today().year} {esc(SITE_NAME)}</div></footer>
</body></html>
"""


def cards(items: list[dict]) -> str:
    if not items:
        return "<p>まだ記事がありません。</p>"
    li = "".join(
        f'<li><a href="/{a["id"]}/">{esc(a["title"])}</a>'
        f'<span class="meta">更新日 {esc(a["updated"])}</span></li>' for a in items
    )
    return f'<ul class="cards">{li}</ul>'


PR_NOTE = "※本記事はアフィリエイト広告（PR）を含みます。"


def split_pr_note(body: str, parts: list[str]) -> str:
    """本文1行目のPR表記を、タイトル下の控えめな1行（.pr-note）として出し、本文から外す。"""
    lines = body.splitlines()
    i = next((k for k, ln in enumerate(lines) if ln.strip()), None)
    if i is not None and lines[i].strip() == PR_NOTE:
        parts.insert(1, f'<p class="pr-note">{esc(PR_NOTE)}</p>')
        return "\n".join(lines[:i] + lines[i + 1:])
    return body


def article_html(a: dict, by_id: dict, arts: list[dict]) -> str:
    parts = [f"<h1>{esc(a['title'])}</h1>"]
    if a["type"] != "page":
        cat_links = " / ".join(
            f'<a href="/category/{slug(c)}/">{esc(c)}</a>' for c in a["categories"]
        )
        parts.append(
            f'<p class="meta">公開日 <time datetime="{esc(a["published"])}">{esc(a["published"])}</time>'
            f'　更新日 <time datetime="{esc(a["updated"])}">{esc(a["updated"])}</time>'
            f'　{cat_links}</p>'
        )
    parts.append(C.markdown_to_html(split_pr_note(a["body"], parts)))

    # サービス記事: このサービスで見られる作品（公開済みの作品記事から自動生成）
    marker = '<div class="works-list" data-works-list></div>'
    if a["type"] == "service" and a["service_id"]:
        works = [w for w in arts if w["type"] == LISTING_TYPE and a["service_id"] in w["services"]][:MAX_SERVICE_WORKS]
        listing = cards(works) if works else "<p>この一覧は、作品記事を公開するたびに自動で増えます。</p>"
        if marker in parts[-1]:
            parts[-1] = parts[-1].replace(marker, listing)
        elif works:
            parts.append("<h2>このサービスで見られる作品</h2>" + listing)
    parts[-1] = parts[-1].replace(marker, "")

    # 関連記事: front matter の related → 足りなければ同ジャンルの作品記事
    if a["type"] != "page":
        rel = [by_id[r] for r in a["related"] if r in by_id and r != a["id"]]
        if a["type"] == LISTING_TYPE:
            genre = a["categories"][1] if len(a["categories"]) > 1 else None
            seen = {r["id"] for r in rel} | {a["id"]}
            same = [w for w in arts if w["type"] == LISTING_TYPE and w["id"] not in seen
                    and genre and genre in w["categories"]]
            works_rel = [r for r in rel if r["type"] == LISTING_TYPE]
            others = [r for r in rel if r["type"] != LISTING_TYPE]
            rel = others + (works_rel + same)[:MAX_RELATED]
        if rel:
            parts.append("<h2>関連記事</h2>" + cards(rel))
    return "\n".join(parts)


def build(pending: dict | None = None) -> dict:
    arts = load_articles(pending)
    by_id = {a["id"]: a for a in arts}
    pages = [a for a in arts if a["type"] == "page"]
    posts = [a for a in arts if a["type"] != "page"]

    if PUBLIC.exists():
        shutil.rmtree(PUBLIC)
    PUBLIC.mkdir(parents=True)

    def write(rel: str, text: str):
        p = PUBLIC / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    for a in arts:
        ld = None
        if a["type"] != "page":
            ld = {"@context": "https://schema.org", "@type": "Article", "headline": a["title"],
                  "datePublished": a["published"], "dateModified": a["updated"],
                  "mainEntityOfPage": a["url"]}
        write(f"{a['id']}/index.html", page(a["title"], article_html(a, by_id, arts), canonical=a["url"],
                                            desc=description(a["body"], a["meta"]), pages=pages, jsonld=ld))

    write("index.html", page(SITE_NAME, f"<h1>新着記事</h1><p>{esc(SITE_DESC)}</p>" + cards(posts),
                             canonical=f"{BASE_URL}/", desc=SITE_DESC, pages=pages))

    cat_urls = []
    all_cats = list(dict.fromkeys(KIND_ORDER + list(SITE["genres"]) + [c for a in posts for c in a["categories"]]))
    for name in all_cats:
        items = [a for a in posts if name in a["categories"]]
        if not items and name not in KIND_ORDER:
            continue
        url = f"{BASE_URL}/category/{slug(name)}/"
        cat_urls.append((url, max((a["updated"] for a in items), default="")))
        write(f"category/{slug(name)}/index.html",
              page(name, f"<h1>{esc(name)}</h1>" + cards(items), canonical=url,
                   desc=f"{name}の記事一覧。{SITE_DESC}", pages=pages, noindex=not items))

    write("404.html", page("ページが見つかりません", '<h1>ページが見つかりません</h1><p><a href="/">トップへ</a></p>',
                           canonical=f"{BASE_URL}/", desc=SITE_DESC, pages=pages, noindex=True))
    write("robots.txt", f"User-agent: *\nAllow: /\n\nSitemap: {BASE_URL}/sitemap.xml\n")

    urls = [(f"{BASE_URL}/", max((a["updated"] for a in posts), default=""))]
    urls += [(a["url"], a["updated"]) for a in arts]
    urls += [u for u in cat_urls if u[1]]
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u, lm in urls:
        sm.append(f"<url><loc>{esc(u)}</loc>" + (f"<lastmod>{esc(lm)}</lastmod>" if lm else "") + "</url>")
    sm.append("</urlset>")
    write("sitemap.xml", "\n".join(sm) + "\n")
    # Cloudflare Pages: 記事ページを長くキャッシュしすぎないように
    write("_headers", "/*\n  Cache-Control: public, max-age=600\n"
          "/sitemap.xml\n  Content-Type: application/xml; charset=utf-8\n  X-Robots-Tag: noindex\n"
          "/robots.txt\n  Content-Type: text/plain; charset=utf-8\n")
    copied = []
    if STATIC.is_dir():
        for f in sorted(STATIC.rglob("*")):
            if f.is_file():
                dst = PUBLIC / f.relative_to(STATIC)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                copied.append(str(f.relative_to(STATIC)))
    return {"static": copied, "articles": len(arts), "posts": len(posts), "pages": len(pages), "public": str(PUBLIC),
            "broken_links": broken_links()}


def broken_links() -> list[str]:
    """public/ 内のサイト内リンク（/xxx/）で、行き先のページがないもの。"""
    bad = []
    for f in sorted(PUBLIC.rglob("*.html")):
        for h in re.findall(r'href="(/[^"#?]*)"', f.read_text(encoding="utf-8")):
            t = PUBLIC / h.lstrip("/")
            if not ((t / "index.html").exists() or t.is_file()):
                bad.append(f"{f.relative_to(PUBLIC)} → {h}")
    return sorted(set(bad))


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False))
