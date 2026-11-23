"""共通処理: パス、front matter の読み込み、Markdown→HTML 変換、ログ入出力。

外部ライブラリに依存しない（Python 3.9+ の標準ライブラリのみ）。
"""
from __future__ import annotations

import csv
import datetime as _dt
import html
import json
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get("AFFILIATE_ROOT", Path(__file__).resolve().parent.parent))
DRAFTS = ROOT / "drafts"
LOGS = ROOT / "logs"
LINKS_JSON = ROOT / "links" / "a8-links.json"
TEMPLATES = ROOT / "templates"
KEYWORDS_CSV = ROOT / "research" / "keywords.csv"
SERVICES_MASTER = ROOT / "research" / "services-master.json"
PUBLISH_LOG = LOGS / "publish-log.csv"
ERRORS_LOG = LOGS / "errors.log"

LOG_COLUMNS = [
    "id", "title", "url", "hatena_entry_id", "published_at", "updated_at",
    "services_available", "check_result",
]

JST = _dt.timezone(_dt.timedelta(hours=9))


def now_jst() -> str:
    return _dt.datetime.now(JST).isoformat(timespec="seconds")


# ---------------------------------------------------------------- front matter

def _parse_scalar(v: str):
    v = v.strip()
    if v == "":
        return ""
    if (v[0] == v[-1]) and v[0] in "\"'" and len(v) >= 2:
        return v[1:-1]
    if v.startswith("{") and v.endswith("}"):
        inner = v[1:-1].strip()
        d = {}
        for part in [x for x in inner.split(",") if x.strip()]:
            k, _, val = part.partition(":")
            d[k.strip().strip("\"'")] = _parse_scalar(val)
        return d
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(x) for x in inner.split(",")]
    return v


def parse_front_matter(text: str) -> tuple[dict, str]:
    """`---` で囲まれた YAML のうち、設計書§4で使う範囲だけを読む:
    スカラー、インライン配列 [a, b]、インライン辞書 {a: 1}、ブロック配列 `- x`、
    1段の入れ子の辞書（env: の下に字下げした `os: Ubuntu` など）。"""
    text = text.lstrip("﻿")
    if not text.startswith("---"):
        raise ValueError("front matter がありません（先頭行が --- ではない）")
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise ValueError("front matter の終わりの --- がありません")
    meta: dict = {}
    key = None
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = re.match(r"^\s+([A-Za-z_][\w.-]*)\s*:\s*(.*)$", raw)
        if m and key is not None and not re.match(r"^\s+-", raw):
            if not isinstance(meta.get(key), dict):
                meta[key] = {}
            meta[key][m.group(1)] = _parse_scalar(m.group(2))
            continue
        m = re.match(r"^\s+-\s*(.*)$", raw) or re.match(r"^-\s+(.*)$", raw)
        if m and key is not None:
            if not isinstance(meta.get(key), list):
                meta[key] = []
            meta[key].append(_parse_scalar(m.group(1)))
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", raw)
        if not m:
            raise ValueError(f"front matter を読めない行: {raw!r}")
        key, val = m.group(1), m.group(2)
        meta[key] = _parse_scalar(val) if val.strip() else []
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def load_draft(path: Path) -> tuple[dict, str]:
    return parse_front_matter(Path(path).read_text(encoding="utf-8"))


def as_list(v) -> list:
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


# ---------------------------------------------------------------- markdown

_INLINE_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+|/[^)\s]*|\.{0,2}/?[\w\-./#]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_AUTOLINK = re.compile(r"(?<![\"'=>])(https?://[^\s<]+)")
_HTML_TAG = re.compile(r"<[^>]+>")


def _inline(text: str) -> str:
    """インライン要素。HTML タグ（A8のリンクなど）はそのまま通す。"""
    out, pos = [], 0
    for m in _HTML_TAG.finditer(text):
        out.append(_inline_plain(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_inline_plain(text[pos:]))
    return "".join(out)


def _inline_plain(t: str) -> str:
    codes: list[str] = []

    def keep_code(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    t = _INLINE_CODE.sub(keep_code, t)
    links: list[str] = []

    def keep_link(m):
        links.append(f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1))}</a>')
        return f"\x01{len(links) - 1}\x01"

    t = _LINK.sub(keep_link, t)
    t = html.escape(t, quote=False)
    t = _AUTOLINK.sub(lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', t)
    t = _BOLD.sub(r"<strong>\1</strong>", t)
    t = re.sub("\x01(\\d+)\x01", lambda m: links[int(m.group(1))], t)
    t = re.sub("\x00(\\d+)\x00", lambda m: f"<code>{html.escape(codes[int(m.group(1))])}</code>", t)
    return t


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def is_table_sep(line: str) -> bool:
    return bool(re.match(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", line))


def parse_tables(body: str) -> list[dict]:
    """本文中の Markdown 表を [{header: [...], rows: [[...], ...]}] で返す（コードブロック内は除く）。"""
    lines = strip_code(body).splitlines()
    tables = []
    i = 0
    while i < len(lines) - 1:
        if "|" in lines[i] and is_table_sep(lines[i + 1]):
            header = _split_row(lines[i])
            rows = []
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append(_split_row(lines[j]))
                j += 1
            tables.append({"header": header, "rows": rows, "line": i + 1})
            i = j
        else:
            i += 1
    return tables


_FENCE = re.compile(r"^\s*(```|~~~)")


def strip_code(md: str) -> str:
    """コードブロック（``` / ~~~）とインラインコードを空にした本文。行数は保つ。
    禁止表現・差し込み記号の残り・見出し順・表の判定と文字数カウントはこれに対して行う。"""
    out, fence = [], None
    for ln in md.splitlines():
        m = _FENCE.match(ln)
        if fence:
            out.append("")
            if m and m.group(1) == fence:
                fence = None
            continue
        if m:
            fence = m.group(1)
            out.append("")
            continue
        out.append(_INLINE_CODE.sub("``", ln))
    return "\n".join(out)


def markdown_to_html(md: str) -> str:
    """設計書の記事で使う範囲の Markdown を HTML にする:
    見出し / 段落 / 箇条書き（- * 1.）/ 表 / 引用 / 水平線 / 生のHTML行 / リンク / 太字 / コード。"""
    lines = md.splitlines()
    out: list[str] = []
    para: list[str] = []
    i = 0

    def flush_para():
        if para:
            out.append("<p>" + "<br>\n".join(_inline(p) for p in para) + "</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            flush_para()
            i += 1
            continue
        fm = _FENCE.match(line)
        if fm:
            flush_para()
            fence = fm.group(1)
            lang = re.sub(r"[^\w+#.-]", "", s[len(fence):].strip().split(" ")[0]) if s[len(fence):].strip() else ""
            code = []
            i += 1
            while i < len(lines) and not (_FENCE.match(lines[i]) and _FENCE.match(lines[i]).group(1) == fence):
                code.append(lines[i])
                i += 1
            i += 1  # 閉じのフェンス
            cls = f' class="language-{lang}"' if lang else ""
            if "実行ログ" in s:
                out.append('<div class="code-label">実行ログ</div>')
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(code), quote=False)}</code></pre>")
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush_para()
            lv = len(m.group(1))
            out.append(f"<h{lv}>{_inline(m.group(2).strip())}</h{lv}>")
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,})$", s):
            flush_para()
            out.append("<hr>")
            i += 1
            continue
        if "|" in s and i + 1 < len(lines) and is_table_sep(lines[i + 1]):
            flush_para()
            header = _split_row(s)
            out.append('<div class="table-wrap"><table>')
            out.append("<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in header) + "</tr></thead><tbody>")
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _split_row(lines[i])) + "</tr>")
                i += 1
            out.append("</tbody></table></div>")
            continue
        if re.match(r"^([-*])\s+", s) or re.match(r"^\d+\.\s+", s):
            flush_para()
            ordered = bool(re.match(r"^\d+\.\s+", s))
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>")
            pat = r"^\d+\.\s+(.*)$" if ordered else r"^[-*]\s+(.*)$"
            while i < len(lines) and re.match(pat, lines[i].strip()):
                item = re.match(pat, lines[i].strip()).group(1)
                out.append(f"<li>{_inline(item)}</li>")
                i += 1
            out.append(f"</{tag}>")
            continue
        if s.startswith(">"):
            flush_para()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(_inline(lines[i].strip()[1:].strip()))
                i += 1
            out.append("<blockquote><p>" + "<br>\n".join(quote) + "</p></blockquote>")
            continue
        if re.match(r"^<(div|p|a|img|table|section|aside|figure|ul|ol|iframe|script|span)\b", s, re.I):
            flush_para()
            out.append(s)
            i += 1
            continue
        para.append(s)
        i += 1
    flush_para()
    return "\n".join(out)


def visible_text(md: str) -> str:
    """文字数カウント用: コード・マークアップ・URL を除いた本文テキスト（地の文だけ数える）。"""
    t = _HTML_TAG.sub("", strip_code(md))
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*\|?\s*:?-{2,}.*$", "", t, flags=re.M)
    t = re.sub(r"[|*`>#\-]", "", t)
    t = re.sub(r"\s+", "", t)
    return t


# ---------------------------------------------------------------- links / logs

def load_a8_links() -> dict:
    if not LINKS_JSON.exists():
        return {}
    return json.loads(LINKS_JSON.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- ジャンル切替
# 判定ルールは templates/rules.json（既定）。ジャンルを替えるときは
# templates/rules-<名前>.json を作り、環境変数 RULES_PROFILE=<名前> で切り替える
# （RULES_FILE でファイルを直接指定してもよい）。
# rules.json の "site" に次を書くと、記事の種類・カテゴリ・URL用の名前もジャンルごとに替えられる。
# 既定は設計書 v2（AI・プログラミング実践）。rules の "site" で上書きできる。
DEFAULT_SITE = {
    "types": {"howto": "h-", "guide": "g-", "tool": "t-", "school": "s-", "page": "p-"},
    "kind_categories": {"howto": "手順・エラー解決", "guide": "学習ロードマップ",
                        "tool": "サーバー・ツール", "school": "スクール"},
    "genres": ["Python", "JavaScript", "生成AI", "Web制作", "サーバー・公開", "Git・開発環境"],
    "category_slugs": {
        "手順・エラー解決": "howto", "学習ロードマップ": "guide", "サーバー・ツール": "tools", "スクール": "school",
        "Python": "python", "JavaScript": "javascript", "生成AI": "genai", "Web制作": "web",
        "サーバー・公開": "hosting", "Git・開発環境": "devenv",
    },
    "listing_type": "howto",         # tool/school 記事の {{works_list}} に並べる種類（services_available が一致するもの）
    "a8_limits": {"howto": 1, "guide": 2, "tool": 3, "school": 3},
    "strict_fact_types": ["tool", "school"],  # services-master の古い・未確認の値を不合格にする種類（他は警告）
    "run_log_types": ["howto"],      # 実行ログの照合を必須にする種類
}


def rules_path() -> Path:
    if os.environ.get("RULES_FILE"):
        return Path(os.environ["RULES_FILE"])
    prof = os.environ.get("RULES_PROFILE", "ai").strip()  # 既定は設計書 v2 の AI・プログラミング
    p = TEMPLATES / f"rules-{prof}.json"
    return p if (p.exists() or prof) else TEMPLATES / "rules.json"


def load_rules_json() -> dict:
    try:
        return json.loads(rules_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def site_profile(rules: dict | None = None) -> dict:
    rules = load_rules_json() if rules is None else rules
    site = {**DEFAULT_SITE, **(rules.get("site") or {})}
    site["types"] = {**site["types"], "page": "p-"}
    return site


def category_slug(name: str, site: dict) -> str:
    slug = site.get("category_slugs", {}).get(name)
    if slug:
        return slug
    import hashlib
    ascii_ = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return ascii_ or "c-" + hashlib.sha1(name.encode()).hexdigest()[:8]


def load_services_master() -> dict:
    if not SERVICES_MASTER.exists():
        return {}
    return json.loads(SERVICES_MASTER.read_text(encoding="utf-8"))


def read_log() -> list[dict]:
    if not PUBLISH_LOG.exists():
        return []
    with PUBLISH_LOG.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_log(rows: list[dict]) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    tmp = PUBLISH_LOG.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    tmp.replace(PUBLISH_LOG)


def upsert_log(row: dict) -> None:
    rows = read_log()
    for r in rows:
        if r["id"] == row["id"]:
            row = {**r, **{k: v for k, v in row.items() if v != ""}}
            row["published_at"] = r.get("published_at") or row.get("published_at", "")
            rows[rows.index(r)] = row
            break
    else:
        rows.append(row)
    write_log(rows)


def log_error(draft_id: str, stage: str, message: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    msg = message.replace("\n", " / ")
    with ERRORS_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{now_jst()}\t{draft_id}\t{stage}\t{msg}\n")


def set_keyword_status(draft_id: str, status: str) -> bool:
    """research/keywords.csv の該当 id の status を書き換える（列順は保つ）。"""
    if not KEYWORDS_CSV.exists():
        return False
    with KEYWORDS_CSV.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        rows = list(reader)
    hit = False
    for r in rows:
        if r.get("id") == draft_id:
            r["status"] = status
            hit = True
    if hit:
        tmp = KEYWORDS_CSV.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        tmp.replace(KEYWORDS_CSV)
    return hit
