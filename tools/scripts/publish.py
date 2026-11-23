#!/usr/bin/env python3
"""記事を公開する（公開前チェック → 投稿 → ログとキュー更新）。

使い方:
    python3 scripts/publish.py drafts/w-0001.md            # チェックして公開
    python3 scripts/publish.py drafts/w-0001.md --dry-run  # チェックとビルドだけ（push・投稿しない）
    python3 scripts/publish.py --rebuild                   # 原稿は変えずにサイトを作り直して push

投稿先は環境変数 PUBLISH_TARGET で切り替える:
    static（既定）: SITE_DIR/content に原稿を置き、build_site.py でビルドし、git commit & push。
                     Cloudflare Pages が push を受けて公開する。
    hatena        : はてなブログ AtomPub に投稿（HATENA_ID / HATENA_BLOG_ID / HATENA_API_KEY）。

同じ id を再度公開すると更新扱い（published_at は保ち、updated_at を更新）。
終了コード: 0 = 公開した / 1 = チェック不合格 / 3 = 投稿・push の失敗。
失敗時は logs/errors.log に1行追記する（キューの status を戻すのは定期実行側）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check as CHK  # noqa: E402
import common as C  # noqa: E402


class PublishError(Exception):
    pass


# ------------------------------------------------------------------ static

def front_matter_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8").lstrip("\ufeff")
    lines = text.splitlines()
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    return "\n".join(lines[: end + 1]) + "\n"


def publish_static(path: Path, meta: dict, body: str, existing: dict | None, dry_run: bool) -> dict:
    import build_site as S

    S.CONTENT.mkdir(parents=True, exist_ok=True)
    did = meta["id"]
    # A8リンクを展開済みの本文で保存する（サイトはこの content/ だけから作る）
    (S.CONTENT / f"{did}.md").write_text(front_matter_text(path) + body, encoding="utf-8")
    now = C.now_jst()
    pending = {"id": did, "published_at": (existing or {}).get("published_at") or now, "updated_at": now}
    verb = "update" if existing else "publish"
    return {"url": f"{S.BASE_URL}/{did}/", "hatena_entry_id": "",
            "build": rebuild_static(dry_run, f"{verb} {did}", pending)}


def rebuild_static(dry_run: bool, message: str, pending: dict | None = None) -> dict:
    import build_site as S

    info = S.build(pending)
    for b in info.get("broken_links", []):
        C.log_error(pending["id"] if pending else "-", "broken_link", b)
    if dry_run:
        info["pushed"] = False
        return info
    if not (S.SITE_DIR / ".git").exists():
        raise PublishError(f"SITE_DIR {S.SITE_DIR} が git リポジトリではありません（README の初回設定を参照）")

    def git(*args):
        r = subprocess.run(["git", "-C", str(S.SITE_DIR), *args], capture_output=True, text=True)
        if r.returncode != 0:
            raise PublishError(f"git {' '.join(args)} 失敗: {r.stderr.strip() or r.stdout.strip()}")
        return r.stdout

    paths = [d for d in ("content", "public", "static") if (S.SITE_DIR / d).exists()]
    git("add", "-A", *paths)
    if git("status", "--porcelain", "--", *paths).strip():
        git("-c", "user.name=affiliate-bot", "-c", "user.email=affiliate-bot@users.noreply.github.com",
            "commit", "-m", message)
    branch = os.environ.get("SITE_BRANCH", "main")
    last = ""
    for _ in range(4):
        r = subprocess.run(["git", "-C", str(S.SITE_DIR), "push", "-u", "origin", f"HEAD:{branch}"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            info["pushed"] = True
            return info
        last = r.stderr.strip()
    raise PublishError(f"git push 失敗: {last}")


# ------------------------------------------------------------------ IndexNow

def indexnow_key() -> str:
    """環境変数 INDEXNOW_KEY、なければ SITE_DIR/static/<32桁16進>.txt のファイル名。"""
    import build_site as S

    key = os.environ.get("INDEXNOW_KEY", "").strip()
    if key:
        return key
    for f in sorted(S.STATIC.glob("*.txt")) if S.STATIC.is_dir() else []:
        if re.fullmatch(r"[0-9a-f]{32}", f.stem) and f.read_text(encoding="utf-8").strip() == f.stem:
            return f.stem
    return ""


def notify_indexnow(urls: list[str], did: str) -> str:
    """IndexNow へ URL を通知する。失敗しても例外は出さず、errors.log に警告を書いて理由を返す。"""
    import build_site as S
    from urllib.parse import urlparse

    if os.environ.get("INDEXNOW", "1") == "0":
        return "skipped"
    key = indexnow_key()
    if not key:
        C.log_error(did, "indexnow", "警告: キーファイルがありません（SITE_DIR/static/<key>.txt）")
        return "no_key"
    host = urlparse(S.BASE_URL).netloc
    payload = json.dumps({"host": host, "key": key, "keyLocation": f"{S.BASE_URL}/{key}.txt",
                          "urlList": urls}).encode("utf-8")
    ep = os.environ.get("INDEXNOW_ENDPOINT", "https://api.indexnow.org/indexnow")
    req = urllib.request.Request(ep, data=payload, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return f"ok {res.status}"  # 200 / 202 が成功
    except urllib.error.HTTPError as e:
        msg = f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001  通信不可（許可ドメイン外など）も警告だけ
        msg = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
    C.log_error(did, "indexnow", f"警告: 通知失敗 {msg}（公開は完了）")
    return f"failed {msg}"


# ------------------------------------------------------------------ hatena

ATOM = "http://www.w3.org/2005/Atom"


def hatena_endpoint() -> tuple[str, str]:
    hid, blog, key = (os.environ.get(k, "") for k in ("HATENA_ID", "HATENA_BLOG_ID", "HATENA_API_KEY"))
    if not (hid and blog and key):
        raise PublishError("環境変数 HATENA_ID / HATENA_BLOG_ID / HATENA_API_KEY が未設定です")
    ep = os.environ.get("HATENA_ENDPOINT") or f"https://blog.hatena.ne.jp/{hid}/{blog}/atom"
    auth = "Basic " + base64.b64encode(f"{hid}:{key}".encode()).decode()
    return ep.rstrip("/"), auth


def hatena_xml(meta: dict, body: str) -> bytes:
    cats = "".join(f'<category term="{xml_escape(str(c), {chr(34): "&quot;"})}"/>'
                   for c in C.as_list(meta.get("categories")))
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<entry xmlns="http://www.w3.org/2005/Atom" xmlns:app="http://www.w3.org/2007/app">'
        f"<title>{xml_escape(str(meta['title']))}</title>"
        f"<author><name>{xml_escape(os.environ.get('HATENA_ID', ''))}</name></author>"
        f'<content type="text/x-markdown">{xml_escape(body)}</content>'
        f"{cats}"
        "<app:control><app:draft>no</app:draft></app:control>"
        "</entry>"
    ).encode("utf-8")


def publish_hatena(meta: dict, body: str, existing: dict | None, dry_run: bool) -> dict:
    ep, auth = hatena_endpoint()
    data = hatena_xml(meta, body)
    entry_id = (existing or {}).get("hatena_entry_id", "")
    url, method = (f"{ep}/entry/{entry_id}", "PUT") if entry_id else (f"{ep}/entry", "POST")
    if dry_run:
        return {"url": (existing or {}).get("url", ""), "hatena_entry_id": entry_id, "request": f"{method} {url}"}
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": auth, "Content-Type": "application/xml; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            xml = res.read()
    except urllib.error.HTTPError as e:
        raise PublishError(f"はてな {method} {e.code}: {e.read()[:300].decode('utf-8', 'replace')}") from e
    except urllib.error.URLError as e:
        raise PublishError(f"はてなに接続できません: {e.reason}") from e
    root = ET.fromstring(xml)
    alt = root.find(f"{{{ATOM}}}link[@rel='alternate']")
    edit = root.find(f"{{{ATOM}}}link[@rel='edit']")
    new_id = edit.get("href").rstrip("/").rsplit("/", 1)[-1] if edit is not None else entry_id
    return {"url": alt.get("href") if alt is not None else "", "hatena_entry_id": new_id}


# ------------------------------------------------------------------ main

def run(path: Path, dry_run: bool) -> int:
    res = CHK.check(path)
    did = res["id"]
    if not res["ok"]:
        C.log_error(did, "check", " | ".join(res["errors"]))
        res.pop("body", None)
        print(json.dumps({"id": did, "status": "check_failed", **res}, ensure_ascii=False, indent=2))
        return 1
    meta, _ = C.load_draft(path)
    body = res["body"]  # {{a8:...}} 展開済み
    existing = next((r for r in C.read_log() if r["id"] == did), None)
    target = os.environ.get("PUBLISH_TARGET", "static")
    try:
        if target == "static":
            out = publish_static(path, meta, body, existing, dry_run)
        elif target == "hatena":
            out = publish_hatena(meta, body, existing, dry_run)
        else:
            raise PublishError(f"PUBLISH_TARGET が不正です: {target}")
    except PublishError as e:
        C.log_error(did, f"publish:{target}", str(e))
        print(json.dumps({"id": did, "status": "publish_failed", "error": str(e)}, ensure_ascii=False, indent=2))
        return 3

    now = C.now_jst()
    check_result = "ok" + (f" warn:{len(res['warnings'])}" if res["warnings"] else "")
    if not dry_run:
        C.upsert_log({
            "id": did, "title": meta.get("title", ""), "url": out.get("url", ""),
            "hatena_entry_id": out.get("hatena_entry_id", ""),
            "published_at": (existing or {}).get("published_at") or now, "updated_at": now,
            "services_available": "|".join(C.as_list(meta.get("services_available"))),
            "check_result": check_result,
        })
        C.set_keyword_status(did, "published")
        if target == "static" and out.get("url"):
            import build_site as S
            out["indexnow"] = notify_indexnow([out["url"], f"{S.BASE_URL}/"], did)
    print(json.dumps({"id": did, "status": "dry_run" if dry_run else ("updated" if existing else "published"),
                      "target": target, "warnings": res["warnings"], **out}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("draft", nargs="?", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="サイトを作り直して push だけする（static）")
    a = ap.parse_args()
    if a.rebuild:
        try:
            print(json.dumps(rebuild_static(a.dry_run, "rebuild"), ensure_ascii=False))
            return 0
        except PublishError as e:
            C.log_error("-", "rebuild", str(e))
            print(f"rebuild 失敗: {e}", file=sys.stderr)
            return 3
    if not a.draft or not a.draft.exists():
        ap.error("原稿ファイルを指定してください")
    return run(a.draft, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
