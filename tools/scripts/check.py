#!/usr/bin/env python3
"""公開前チェック。

判定ルールの本体は templates/rules.json（B が管理）。このスクリプトはそれを読んで判定し、
加えて次を見る（設計書 §2.2・§4.3・重複）:
  - front matter の id / type / categories の形
  - A8.netリンクが links/a8-links.json の html と完全一致しているか（改変検知）
  - 公開済み記事とのタイトル重複・本文の類似

使い方:
    python3 scripts/check.py drafts/w-0001.md [drafts/...] [--json]

終了コード: 0 = 合格（warn のみ含む）, 1 = 不合格, 2 = ファイルがない。
本文中の {{a8:<service_id>}}（A8リンク）と {{<項目>:<service_id>}}（research/services-master.json の値。
例 {{price:unext}} {{trial:unext}}）は判定前に展開する（publish.py と同じ処理）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

RULES_JSON = C.rules_path()
SIMILARITY_LIMIT = 0.5
A8_SLOT = re.compile(r"^\s*\{\{a8:([a-z0-9_-]+)\}\}\s*$")
# A8 の広告URL（px.a8.net / www10.a8.net など）と計測パラメータ。本文の「A8.net」という語は対象外
A8_URL = re.compile(r"https?://[\w.-]*\ba8\.net\b|a8mat=", re.I)
FACT = re.compile(r"\{\{([a-z_]+):([a-z0-9_-]+)\}\}")
FACT_MAX_AGE_DAYS = 35
WORKS_LIST = re.compile(r"^\s*\{\{works_list\}\}\s*$")
WORKS_LIST_HTML = '<div class="works-list" data-works-list></div>'  # build_site.py が作品一覧に置き換える
DATE_JA = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")

# rules.json が読めないときの最低限
FALLBACK_RULES = {
    "required": [
        {"id": "pr_first_line", "severity": "error", "where": "body_first_nonempty_line",
         "equals": "※本記事はアフィリエイト広告（PR）を含みます。", "message": "PR表記がない"},
        {"id": "checked_at_sentence", "severity": "error", "where": "body_first_10_lines",
         "regex": r"(配信|料金|特典)(状況|情報)?は\s?\d{4}年\d{1,2}月\d{1,2}日\s?時点", "message": "確認日の一文がない"},
    ],
    "length": {"work": {"min": 1500, "max": 3000, "severity": "error"},
               "service": {"min": 3000, "max": 5000, "severity": "error"}},
    "banned": [{"id": "kanzen_muryo", "severity": "error", "regex": "完全無料", "message": "完全無料"}],
}


def load_rules() -> tuple[dict, str | None]:
    try:
        return json.loads(RULES_JSON.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return FALLBACK_RULES, f"{RULES_JSON} がないため最低限のルールで判定"
    except json.JSONDecodeError as e:
        return FALLBACK_RULES, f"rules.json が壊れています（{e}）。最低限のルールで判定"


def expand_a8(body: str, a8: dict) -> tuple[str, list[str]]:
    """{{a8:sid}} の行を a8-links.json の html（そのまま）に置き換える。ない sid の行は消す。"""
    import os

    site_name = os.environ.get("SITE_NAME", "動かして確かめるプログラミング帳")
    out, used = [], []
    for ln in body.splitlines():
        if WORKS_LIST.match(ln):
            out.append(WORKS_LIST_HTML)
            continue
        ln = ln.replace("{{blog_name}}", site_name)
        m = A8_SLOT.match(ln)
        if m:
            sid = m.group(1)
            html = (a8.get(sid) or {}).get("html", "")
            if html:
                out.append(html)
                used.append(sid)
            continue
        out.append(ln)
    return "\n".join(out) + ("\n" if body.endswith("\n") else ""), used


def lookup_fact(master: dict, field: str, sid: str) -> dict | None:
    """services-master.json から1項目を引く。
    形式は次のどちらでもよい:
      {"unext": {"checked_at": "...", "verified": true, "source": "https://...", "price": "2,189円"}}
      {"unext": {..., "facts": {"price": {"value": "2,189円", "source": "...", "checked_at": "...", "verified": true}}}}
    項目ごとの checked_at / verified / source があればサービス全体の値より優先する。"""
    svc = master.get(sid)
    if not isinstance(svc, dict):
        return None
    raw = svc.get(field, (svc.get("facts") or {}).get(field))
    if raw is None or field in ("checked_at", "verified", "source", "sources", "facts"):
        return None
    item = raw if isinstance(raw, dict) else {"value": raw}
    if item.get("value") in (None, ""):
        return None
    return {
        "value": str(item["value"]),
        "checked_at": item.get("checked_at", svc.get("checked_at", "")),
        "verified": item.get("verified", svc.get("verified", False)),
        "source": item.get("source", svc.get("source", "")),
    }


def expand_facts(body: str, master: dict) -> tuple[str, list[dict]]:
    """{{price:unext}} のような差し込みを services-master.json の値にする。{{a8:..}} は対象外。
    見つからない項目はそのまま残す（no_unresolved_placeholder で不合格になる）。"""
    used: list[dict] = []

    def rep(m):
        field, sid = m.group(1), m.group(2)
        if field == "a8":
            return m.group(0)
        f = lookup_fact(master, field, sid)
        if not f:
            return m.group(0)
        used.append({"field": field, "service_id": sid, **f})
        return f["value"]

    return FACT.sub(rep, body), used


def host_in(host: str, domains) -> bool:
    for d in domains:
        d = d.lower().lstrip("*.")
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def official_domains(meta: dict) -> set[str]:
    """出典として認める公式ドメイン: services-master.json と scripts/services.json の domains、
    research/programs.csv の official_url、front matter の official_site。"""
    doms: set[str] = set()
    for v in C.load_services_master().values():
        if isinstance(v, dict):
            doms.update(v.get("domains", []))
            for k in ("source", "official_url"):
                if v.get(k):
                    doms.add(urllib.parse.urlsplit(v[k]).hostname or "")
    sj = Path(__file__).resolve().parent / "services.json"
    if sj.exists():
        for k, v in json.loads(sj.read_text(encoding="utf-8")).items():
            if isinstance(v, dict):
                doms.update(v.get("domains", []))
    for row in program_rows():
        if row.get("official_url", "").startswith("http"):
            doms.add(urllib.parse.urlsplit(row["official_url"]).hostname or "")
    for u in C.as_list(meta.get("official_site")):
        doms.add(urllib.parse.urlsplit(str(u)).hostname or str(u))
    return {d for d in doms if d}


def program_rows() -> list[dict]:
    p = C.ROOT / "research" / "programs.csv"
    if not p.exists():
        return []
    import csv
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def program_names() -> set[str]:
    return {r.get("service_name", "").strip() for r in program_rows() if r.get("service_name")}


RUN_LOG_LABEL = "実行ログ"
SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9_-]{20,}", "APIキー（sk-…）"), (r"gh[pousr]_[A-Za-z0-9]{30,}", "GitHubトークン"),
    (r"AKIA[0-9A-Z]{16}", "AWSアクセスキー"), (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slackトークン"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "秘密鍵"), (r"(?i)(password|passwd|api[_-]?key|secret)\s*[=:]\s*\S{6,}", "パスワード・キーらしき値"),
]


def code_blocks(body: str) -> list[dict]:
    """すべてのコードブロック: {line, info, label(実行ログか), text}"""
    lines = body.splitlines()
    out, i = [], 0
    while i < len(lines):
        m = re.match(r"^\s*(```|~~~)(.*)$", lines[i])
        if not m:
            i += 1
            continue
        fence, info, start = m.group(1), m.group(2).strip(), i
        j = i + 1
        while j < len(lines) and not lines[j].strip().startswith(fence):
            j += 1
        prev = next((lines[k].strip() for k in range(start - 1, -1, -1) if lines[k].strip()), "")
        labeled = RUN_LOG_LABEL in info or bool(
            re.fullmatch(r"(\*\*)?\s*" + RUN_LOG_LABEL + r"[^\n]{0,40}?(\*\*)?\s*[:：]?", prev))
        out.append({"line": start + 1, "info": info, "label": labeled, "text": "\n".join(lines[start + 1:j])})
        i = j + 1
    return out


def run_log_blocks(body: str) -> list[dict]:
    return [b for b in code_blocks(body) if b["label"]]


def _old_run_log_blocks(body: str) -> list[dict]:
    """「実行ログ」ラベル付きのコードブロックを返す。ラベルの付け方は次のどちらか:
      ```text 実行ログ          … フェンスの情報文字列に「実行ログ」を含める
      **実行ログ** / 実行ログ: … ブロックの直前の行（空行を除く）に書く"""
    lines = body.splitlines()
    out, i = [], 0
    while i < len(lines):
        m = re.match(r"^\s*(```|~~~)(.*)$", lines[i])
        if not m:
            i += 1
            continue
        fence, info, start = m.group(1), m.group(2), i
        j = i + 1
        while j < len(lines) and not lines[j].strip().startswith(fence):
            j += 1
        prev = next((lines[k].strip() for k in range(start - 1, -1, -1) if lines[k].strip()), "")
        labeled = RUN_LOG_LABEL in info or bool(
            re.fullmatch(r"(\*\*)?\s*" + RUN_LOG_LABEL + r"[^\n]{0,40}?(\*\*)?\s*[:：]?", prev))
        if labeled:
            out.append({"line": start + 1, "text": "\n".join(lines[start + 1:j])})
        i = j + 1
    return out


def _norm_log(t: str) -> str:
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(ln.rstrip() for ln in t.split("\n")).strip("\n")


def strip_for_wording(text: str) -> str:
    """禁止表現の判定用: HTMLタグ（A8リンク）とURLを除く（URL中の英字で誤検知しないため）。"""
    t = re.sub(r"<[^>]+>", " ", text)
    t = re.sub(r"\]\([^)]+\)", "]", t)
    return re.sub(r"https?://\S+", " ", t)


def ngrams(text: str, n: int = 5) -> set[str]:
    return {text[i:i + n] for i in range(max(0, len(text) - n + 1))}


def h2_list(body: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^##\s+(.+)$", body, re.M)]


def check(path: Path, body_override: str | None = None) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    def add(sev: str, msg: str):
        (errors if sev == "error" else warnings).append(msg)

    try:
        meta, raw_body = C.load_draft(path)
    except Exception as e:  # noqa: BLE001
        return {"id": path.stem, "ok": False, "errors": [f"読み込み失敗: {e}"], "warnings": []}

    rules, note = load_rules()
    if note:
        warnings.append(note)
    site = C.site_profile(rules)
    TYPES, KIND_CATEGORIES, GENRES = site["types"], site["kind_categories"], set(site["genres"])
    a8 = C.load_a8_links()
    master = C.load_services_master()
    body, facts_used = expand_facts(raw_body if body_override is None else body_override, master)
    body, a8_used = expand_a8(body, a8)
    # A8リンクがあるのにPR表記がなければ、本文1行目に自動で入れる（公開される本文もこれ）
    pr_rule = next((r for r in rules.get("required", []) if r.get("id") == "pr_first_line"), None)
    if pr_rule and pr_rule.get("equals") and any(A8_URL.search(ln) for ln in body.splitlines()):
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        if first != pr_rule["equals"]:
            body = pr_rule["equals"] + "\n\n" + body.lstrip("\n")
            warnings.append("A8リンクがあるため、PR表記を本文1行目に自動で入れました")

    did = str(meta.get("id", ""))
    typ = str(meta.get("type", ""))
    is_page = typ == "page"
    today = dt.datetime.now(C.JST).date()

    # ---------------- front matter（設計書 §4.4・§2.2）
    required = ["id", "type", "title"] if is_page else ["id", "type", "title", "categories", "checked_at"]
    for key in required:
        if meta.get(key) in (None, "", []):
            errors.append(f"front matter に {key} がありません")
    if typ not in TYPES:
        errors.append(f"type が不正です: {typ!r}（{'/'.join(TYPES)}）")
    elif did and not did.startswith(TYPES[typ]):
        errors.append(f"id {did!r} の接頭辞が type {typ} と合いません（{TYPES[typ]}）")
    if did and path.stem != did:
        errors.append(f"ファイル名 {path.name} と id {did} が一致しません")
    if did and not re.fullmatch(r"[a-z]-[a-z0-9-]+", did):
        errors.append(f"id はURLに使うため英小文字・数字・ハイフンのみ: {did!r}")
    cats = C.as_list(meta.get("categories"))
    if typ in KIND_CATEGORIES and (len(cats) != 2 or cats[0] != KIND_CATEGORIES[typ] or cats[1] not in GENRES):
        errors.append(f"categories は [{KIND_CATEGORIES[typ]}, ジャンル] の2つ（ジャンル: {'/'.join(sorted(GENRES))}）。現在: {cats}")
    if str(meta.get("trial", "")).lower() == "true":
        warnings.append("trial: true（試作記事）")

    checked = None
    if meta.get("checked_at"):
        try:
            checked = dt.date.fromisoformat(str(meta["checked_at"]))
        except ValueError:
            errors.append(f"checked_at が YYYY-MM-DD ではありません: {meta['checked_at']!r}")

    lines = body.splitlines()
    prose = C.strip_code(body)  # コードブロックを除いた本文（コード中の {{ }} や # を誤検知しない）
    nonempty = [ln.strip() for ln in lines if ln.strip()]
    sources = [str(s) for s in C.as_list(meta.get("sources"))]

    # ---------------- rules.json の required
    for r in rules.get("required", []):
        applies = r.get("applies_to")
        if applies and typ not in applies:
            continue
        sev, msg, rid, where = r.get("severity", "error"), r.get("message", r.get("id")), r.get("id"), r.get("where", "")
        if is_page and rid != "no_unresolved_placeholder":
            continue  # 固定ページは記事の必須要素の対象外（差し込み記号の残りだけ見る）
        if "equals" in r and where == "body_first_nonempty_line":
            has_pr = bool(nonempty) and nonempty[0] == r["equals"]
            if rid == "pr_first_line" and not r.get("always", False):
                # PR表記は A8リンク（展開後）がある記事だけ必須。ない記事に付いていれば警告（PL決定 2026-11-07）
                has_a8 = any(A8_URL.search(ln) for ln in lines)
                if has_a8 and not has_pr:
                    add(sev, msg)
                elif not has_a8 and r["equals"] in body:
                    warnings.append("広告リンクがないのにPR表記があります（広告のない記事には付けない）")
            elif not has_pr:
                add(sev, msg)
        elif rid == "no_unresolved_placeholder" or (r.get("invert") and "regex" in r):
            m = re.search(r["regex"], prose)
            if m:
                add(sev, f"{msg}: {m.group(0)}")
        elif "regex" in r and where == "body_first_10_lines":
            if not re.search(r["regex"], "\n".join(nonempty[:10])):
                add(sev, msg)
        elif rid == "checked_at_matches_meta":
            head = "\n".join(nonempty[:10])
            dre = r.get("date_regex", r"(?:配信状況|料金・特典)は\s?(\d{4}年\d{1,2}月\d{1,2}日)")
            m = re.search(dre, head)
            if m and checked:
                y, mo, d = map(int, DATE_JA.search(m.group(1)).groups())
                if dt.date(y, mo, d) != checked:
                    add(sev, f"{msg}（本文 {y}-{mo:02d}-{d:02d} / checked_at {checked}）")
        elif rid == "env_table":
            want = r.get("header_equals", ["項目", "バージョン"])
            t = next((t for t in C.parse_tables(body) if t["header"][:len(want)] == want), None)
            env = meta.get("env") if isinstance(meta.get("env"), dict) else {}
            if not t or not t["rows"]:
                add(sev, msg)
            else:
                cells = " ".join(" ".join(row) for row in t["rows"])
                if "OS" not in t["rows"][0][0].upper():
                    add(sev, f"{msg}（1行目が OS ではない: {t['rows'][0][0]}）")
                vals = []
                for v in env.values():
                    vals += [str(x) for x in v.values()] if isinstance(v, dict) else [str(v)]
                missing = [v for v in vals if v and v not in cells]
                if missing:
                    add(sev, f"{msg}（表にない env の値: {missing}）")
        elif rid == "run_log_blocks":
            n_blocks = len(run_log_blocks(body))
            if n_blocks < int(r.get("min_blocks", 1)):
                add(sev, f"{msg}（{n_blocks}個 / {r.get('min_blocks', 1)}個以上）")
        elif rid == "code_blocks_from_log":
            lp = C.ROOT / str(meta.get("run_log", ""))
            log_text = _norm_log(lp.read_text(encoding="utf-8", errors="replace")) if meta.get("run_log") and lp.is_file() else ""
            for b in code_blocks(body):
                if b["label"] or not b["text"].strip():
                    continue
                if _norm_log(b["text"]) not in log_text:
                    add(sev, f"{msg}（本文{b['line']}行目の ```{b['info']}）")
        elif rid == "howto_a8_only_hosting":
            hosting = r.get("category", "サーバー・公開")
            has_a8 = bool(C.as_list(meta.get("a8_slots"))) or any(A8_URL.search(ln) for ln in lines)
            if has_a8 and (len(cats) < 2 or cats[1] != hosting):
                add(sev, msg)
        elif rid == "howto_a8_allowed":
            # テーマ（categories[1]）ごとに置いてよいA8案件と場所を決める（設計書 v2 §2.3, 2026-11-09 改定）
            used = [sid for sid in a8_used]
            used += [sid for sid, v in a8.items() if v.get("html") and v["html"] in body and sid not in used]
            if used:
                theme = cats[1] if len(cats) > 1 else ""
                allow = r.get("allowed", {}).get(theme)
                excl = r.get("exclude_title_regex")
                if not allow:
                    add(sev, f"{msg}: テーマ「{theme}」の howto にはA8リンクを置けません（{', '.join(used)}）")
                elif allow.get("exclude_title") is not False and excl and re.search(excl, str(meta.get("title", ""))):
                    add(sev, f"{msg}: git・pip・環境構築がテーマの howto にはA8リンクを置けません（{', '.join(used)}）")
                else:
                    bad = [u for u in used if u not in allow.get("services", [])]
                    if bad:
                        add(sev, f"{msg}: テーマ「{theme}」で使えるのは {', '.join(allow.get('services', []))} だけです（{', '.join(bad)}）")
                    sec = allow.get("section")
                    if sec and not bad:
                        hm = re.search(rf"^##\s*{re.escape(sec)}\s*$", body, re.M)
                        part = ""
                        if hm:
                            rest = body[hm.end():]
                            nx = re.search(r"^#{1,2}\s", rest, re.M)
                            part = rest[: nx.start()] if nx else rest
                        for u in used:
                            h = a8[u]["html"]
                            if h not in part:
                                add(sev, f"{msg}: {u} のリンクは「## {sec}」節の中に置いてください")
                            elif allow.get("position") == "end" and not part.rstrip().endswith(h.rstrip()):
                                add(sev, f"{msg}: {u} のリンクは「## {sec}」節の末尾に置いてください")
        elif rid == "checked_at_fresh":
            if checked and (checked > today or (today - checked).days > 7):
                add(sev, f"{msg}（checked_at {checked}）")
        elif rid == "status_table":
            want = r.get("header_equals", [])
            t = next((t for t in C.parse_tables(body) if t["header"] == want), None)
            if not t or not t["rows"]:
                add(sev, msg + f"（見出し: {' | '.join(want)}）")
            else:
                for col, pat in r.get("cell_rules", {}).items():
                    ci = want.index(col) if col in want else None
                    for row in t["rows"]:
                        if ci is None or ci >= len(row) or not re.match(pat, row[ci]):
                            add(sev, f"{msg}: {row[0] if row else '?'} の{col}={row[ci] if ci is not None and ci < len(row) else '（なし）'!r}")
        elif rid == "sources_section":
            m = re.search(r["regex"], body, re.M)
            if not m:
                add(sev, msg)
            else:
                after = body[m.end():]
                nxt = re.search(r"^#{1,2}\s", after, re.M)
                section = after[: nxt.start()] if nxt else after
                urls = re.findall(r"https://[^\s)<>\]\"']+", re.sub(r"<[^>]+>", " ", section))
                if not urls:
                    add(sev, msg)
                else:
                    body_set, meta_set = set(urls), set(sources)
                    if meta_set != body_set:
                        add("error", "front matter の sources と本文の出典URLが食い違います"
                                     f"（本文のみ: {sorted(body_set - meta_set)} / sourcesのみ: {sorted(meta_set - body_set)}）")
        elif rid == "sources_meta":
            if not sources:
                add(sev, msg)
            for s in sources:
                if not s.startswith("https://"):
                    add(sev, f"出典URLは https:// で始めてください: {s}")
        elif rid == "sources_official_only":
            allowed = official_domains(meta) | set(r.get("allow_hosts", []))
            deny = [d.lower() for d in r.get("deny_hosts", [])]
            for u in sources:
                host = (urllib.parse.urlsplit(u).hostname or "").lower()
                if host_in(host, deny) or not host_in(host, allowed):
                    add(sev, f"{msg}: {host or u}")
        elif rid == "services_in_table":
            names = program_names()
            if names:
                for t in C.parse_tables(body):
                    if t["header"] and t["header"][0] in ("サービス", "サービス名"):
                        for row in t["rows"]:
                            if row and row[0] and re.sub(r"<[^>]+>|\[|\]\([^)]*\)", "", row[0]).strip() not in names:
                                add(sev, f"{msg}: {row[0]}")
        elif rid == "unverified_empty":
            if C.as_list(meta.get("unverified")):
                add(sev, f"{msg}: {C.as_list(meta.get('unverified'))}")
        elif rid == "headings_order":
            hs = h2_list(prose)
            pos = []
            for word in r.get("order", []):
                idx = next((i for i, h in enumerate(hs) if word in h), None)
                pos.append(idx)
            seq = [p for p in pos if p is not None]
            if None in pos or seq != sorted(seq):
                add(sev, msg)
        elif rid == "a8_link_count":
            n = sum(1 for ln in lines for m in re.finditer(r"<a\b[^>]*>", ln, re.I) if A8_URL.search(m.group(0)))
            limit = site["a8_limits"].get(typ)
            if typ == "compare":
                limit = len(C.as_list(meta.get("services_available"))) + 1
            if limit is not None and n > limit:
                add(sev, f"{msg}（{n}箇所 / 上限{limit}）")

    # ---------------- A8リンクの改変検知（a8-links.json の html と完全一致）
    known = [v.get("html", "") for v in a8.values() if v.get("html")]
    for ln in lines:
        if A8_URL.search(ln):
            if not any(k == ln.strip() or k in ln or ln.strip() in [x.strip() for x in k.splitlines()]
                       for k in known):
                errors.append(f"A8リンクが a8-links.json と一致しません（改変・手書きの疑い）: {ln.strip()[:80]}")
    for sid in C.as_list(meta.get("a8_slots")):
        if sid not in a8:
            warnings.append(f"a8_slots の {sid} は a8-links.json にないため、リンク行を削除しました")

    # ---------------- 実行ログの照合（設計書 v2 §3・§4: 作文された出力を落とす）
    if typ in site.get("run_log_types", []):
        env = meta.get("env")
        if not isinstance(env, dict) or not env.get("os") or len(env) < 2:
            errors.append("env（検証環境）がありません。os と、言語・ライブラリのバージョンを書いてください")
        rl = str(meta.get("run_log", "")).strip()
        blocks = run_log_blocks(body)
        log_text = None
        if not rl:
            errors.append("run_log（実行ログのファイル）がありません")
        else:
            lp = (C.ROOT / rl).resolve()
            if not str(lp).startswith(str(C.ROOT.resolve())):
                errors.append(f"run_log は共有フォルダ内のパスにしてください: {rl}")
            elif not lp.is_file():
                errors.append(f"run_log のファイルがありません: {rl}")
            else:
                log_text = _norm_log(lp.read_text(encoding="utf-8", errors="replace"))
                if not log_text:
                    errors.append(f"run_log が空です: {rl}")
        if not blocks:
            errors.append(f"「{RUN_LOG_LABEL}」ラベル付きのコードブロックがありません（```text {RUN_LOG_LABEL} で始める）")
        for b in blocks:
            t = _norm_log(b["text"])
            if not t:
                errors.append(f"本文{b['line']}行目の実行ログが空です")
            elif log_text is not None and t not in log_text:
                bad = next((ln for ln in t.split("\n") if ln.strip() and ln not in log_text), t.split("\n")[0])
                errors.append(f"本文{b['line']}行目の実行ログが run_log に逐語で含まれていません（例: {bad[:60]!r}）")
            for pat, name in SECRET_PATTERNS:
                if re.search(pat, b["text"]):
                    errors.append(f"本文{b['line']}行目の実行ログに{name}が含まれている可能性があります（公開不可）")
            if re.search(r"/tmp/claude-|/root/|/home/user\b|/mnt/project-files", b["text"]):
                warnings.append(f"本文{b['line']}行目の実行ログに実行環境のパスが写っています（読者向けに、ログを取るときに作業用ディレクトリで実行する）")

    # ---------------- サービス基本情報（services-master.json）の鮮度
    for f in facts_used:
        label = f"{{{{{f['field']}:{f['service_id']}}}}}"
        problems = []
        if f["verified"] is not True:
            problems.append("verified が true ではない")
        try:
            age = (today - dt.date.fromisoformat(str(f["checked_at"])[:10])).days
            if age > FACT_MAX_AGE_DAYS:
                problems.append(f"確認日 {f['checked_at']} が {age} 日前（{FACT_MAX_AGE_DAYS}日まで）")
        except ValueError:
            problems.append(f"確認日がない・読めない（{f['checked_at']!r}）")
        if problems:
            add("error" if typ in site["strict_fact_types"] else "warn",
                f"サービス基本情報 {label}={f['value']}: " + "、".join(problems))
        if f["source"] and f["source"] not in sources and not is_page:
            warnings.append(f"{label} の出典 {f['source']} が sources にありません")
    if FACT.search(raw_body) and not master:
        warnings.append(f"{C.SERVICES_MASTER} がないため {{{{項目:service_id}}}} を展開できません")

    # ---------------- 禁止表現（rules.json の banned）
    wording = strip_for_wording(str(meta.get("title", "")) + "\n" + str(meta.get("description", "")) + "\n" + prose)
    for b in rules.get("banned", []):
        try:
            m = re.search(b["regex"], wording)
        except re.error as e:
            warnings.append(f"rules.json banned {b.get('id')} の正規表現が不正: {e}")
            continue
        if m:
            sug = f" → 例: {b['suggest']}" if b.get("suggest") else ""
            add(b.get("severity", "error"), f"禁止表現「{m.group(0)}」: {b.get('message', b.get('id'))}{sug}")

    # ---------------- 文字数
    n = len(C.visible_text(body))
    lr = rules.get("length", {}).get(typ)
    if lr and not lr["min"] <= n <= lr["max"]:
        add(lr.get("severity", "error"), f"文字数 {n} 字が範囲外（{typ}: {lr['min']}〜{lr['max']}字）")

    desc = str(meta.get("description", ""))
    if len(desc) > 120:
        warnings.append(f"description が {len(desc)} 字（120字以内）")

    # ---------------- 重複（公開済みとのタイトル一致・本文の類似）
    log = C.read_log()
    title = str(meta.get("title", "")).strip()
    for r in log:
        if r["id"] != did and r["title"].strip() == title:
            errors.append(f"同じタイトルの記事が公開済みです: {r['id']}")
    mine = ngrams(C.visible_text(body))
    if mine and not is_page:
        for r in log:
            if r["id"] == did:
                continue
            other = C.DRAFTS / f"{r['id']}.md"
            if not other.exists():
                continue
            try:
                _, ob = C.load_draft(other)
            except Exception:  # noqa: BLE001
                continue
            theirs = ngrams(C.visible_text(expand_a8(expand_facts(ob, master)[0], a8)[0]))
            if not theirs:
                continue
            sim = len(mine & theirs) / len(mine | theirs)
            if sim >= SIMILARITY_LIMIT:
                errors.append(f"公開済み {r['id']} と本文が {sim:.0%} 重複（上限 {SIMILARITY_LIMIT:.0%}）")
            elif sim >= SIMILARITY_LIMIT * 0.7:
                warnings.append(f"公開済み {r['id']} と本文の重複 {sim:.0%}")

    return {"id": did or path.stem, "ok": not errors, "chars": n, "a8_inserted": a8_used,
            "facts_inserted": [f"{x['field']}:{x['service_id']}" for x in facts_used],
            "errors": errors, "warnings": warnings, "body": body}


def summary(res: dict) -> str:
    if res["ok"]:
        s = f"OK ({res.get('chars', '?')}字)"
        if res["warnings"]:
            s += "\n  warn: " + "\n  warn: ".join(res["warnings"])
        return s
    s = "NG\n  error: " + "\n  error: ".join(res["errors"])
    if res["warnings"]:
        s += "\n  warn: " + "\n  warn: ".join(res["warnings"])
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("drafts", nargs="+", type=Path)
    ap.add_argument("--json", action="store_true", help="結果を JSON で出す")
    a = ap.parse_args()
    worst, results = 0, []
    for p in a.drafts:
        if not p.exists():
            print(f"{p}: ファイルがありません", file=sys.stderr)
            worst = 2
            continue
        res = check(p)
        res.pop("body", None)
        results.append(res)
        if not res["ok"]:
            worst = max(worst, 1)
        if not a.json:
            print(f"{res['id']}: {summary(res)}")
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return worst


if __name__ == "__main__":
    sys.exit(main())
