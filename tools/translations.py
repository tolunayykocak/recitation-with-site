#!/usr/bin/env python3
"""Quran translations (meal) from QuranEnc.com for the app (SPEC §6.1).

QuranEnc allows republishing its translations without any change, with the source
named, and asks that the current version is followed. So this script:
  - downloads each translation listed in tools/translations/sources.json, surah by surah,
    through QuranEnc's public API;
  - checks it: 114 surahs, 6236 ayahs, every surah has exactly the ayahs of the embedded
    surahs.json (Tanzil) in order 1..n, no empty translation;
  - writes the text exactly as QuranEnc returns it (not trimmed, not edited). Footnotes are
    kept in the file (the app does not show them yet);
  - writes translations.json, the index: version and last update from QuranEnc, sha256 of
    each file, and a revision that goes up whenever a file changes.

Commands:
  build   [--dir D] [--force]    download what is missing or has a newer QuranEnc version
  outdated [--dir D]             print ids whose QuranEnc version differs from D (exit 2 if any)
  check   DIR                    validate an index + files (app CI, site CI); no network
  publish DIR SITE               copy DIR to SITE/catalog/translations and this script to SITE/tools

Runs in GitHub Actions (.github/workflows/translations.yml): the Claude sandbox cannot reach
quranenc.com. `check` needs only the standard library and works anywhere.
"""

import argparse
import hashlib
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
DEFAULT_DIR = REPO / "app/src/main/assets/translations"
SURAHS_JSON = REPO / "app/src/main/assets/surahs.json"
SOURCES = HERE / "sources.json"

API = "https://quranenc.com/api/v1"
UA = {"User-Agent": "recitation-with-build/1.0 (+https://github.com/tolunayykocak/recitation-with)"}
SCHEMA = 1
TOTAL_AYAHS = 6236
INDEX = "translations.json"


class TranslationError(Exception):
    pass


# --- helpers -----------------------------------------------------------------------------


def fetch_json(url, tries=4):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise TranslationError(f"{url}: {last}")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(obj, path, indent=None):
    text = json.dumps(obj, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":"))
    Path(path).write_text(text + "\n", encoding="utf-8")


def ayah_counts():
    data = json.loads(SURAHS_JSON.read_text(encoding="utf-8"))
    counts = [s["ayahCount"] for s in sorted(data["surahs"], key=lambda s: s["number"])]
    assert len(counts) == 114 and sum(counts) == TOTAL_AYAHS, "surahs.json must list 114 surahs / 6236 ayahs"
    return counts


def sources():
    data = json.loads(SOURCES.read_text(encoding="utf-8"))
    items = data["translations"]
    ids = [t["id"] for t in items]
    assert len(ids) == len(set(ids)), "duplicate id in sources.json"
    for t in items:
        assert t["language"] and "en" in t["translator"], f"{t['id']}: language and an English translator name are required"
    return items


def remote_info(language):
    """QuranEnc's list of translations for a language: {key: {version, last_update, title, ...}}."""
    data = fetch_json(f"{API}/translations/list/{language}")
    items = data.get("translations", data) if isinstance(data, dict) else data
    return {t["key"]: t for t in items}


def remote_infos(items):
    out = {}
    for language in sorted({t["language"] for t in items}):
        out.update(remote_info(language))
    for t in items:
        if t["id"] not in out:
            raise TranslationError(f"{t['id']}: not in QuranEnc's list for {t['language']!r}")
    return out


# --- build -------------------------------------------------------------------------------


def download(tid, counts):
    """Returns (ayahs per surah, footnotes {"s:a": text}), checked."""
    surahs, footnotes = [], {}
    for n in range(1, 115):
        data = fetch_json(f"{API}/translation/sura/{tid}/{n}")
        rows = data.get("result") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise TranslationError(f"{tid} surah {n}: unexpected response {str(data)[:300]}")
        if n == 1:
            print(f"  {tid} 1:1 fields: {sorted(rows[0].keys())}")
        texts = []
        for i, row in enumerate(rows, start=1):
            if int(row["sura"]) != n or int(row["aya"]) != i:
                raise TranslationError(f"{tid}: expected {n}:{i}, got {row['sura']}:{row['aya']}")
            text = row.get("translation")
            if not isinstance(text, str) or not text.strip():
                raise TranslationError(f"{tid} {n}:{i}: empty translation")
            texts.append(text)  # exactly as QuranEnc returns it
            note = row.get("footnotes")
            if isinstance(note, str) and note.strip():
                footnotes[f"{n}:{i}"] = note
        if len(texts) != counts[n - 1]:
            raise TranslationError(f"{tid} surah {n}: {len(texts)} ayahs, surahs.json says {counts[n - 1]}")
        surahs.append(texts)
        time.sleep(0.15)  # be gentle with the site
    total = sum(len(s) for s in surahs)
    if total != TOTAL_AYAHS:
        raise TranslationError(f"{tid}: {total} ayahs, expected {TOTAL_AYAHS}")
    return surahs, footnotes


def load_index(directory):
    path = Path(directory) / INDEX
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build(directory, force=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    counts = ayah_counts()
    items = sources()
    infos = remote_infos(items)
    old = load_index(directory) or {"revision": 0, "translations": []}
    old_by_id = {t["id"]: t for t in old.get("translations", [])}

    entries = []
    for t in items:
        tid = t["id"]
        info = infos[tid]
        version = str(info.get("version", ""))
        file = directory / f"{tid}.json"
        prev = old_by_id.get(tid)
        if not force and prev and prev.get("version") == version and file.exists() and sha256(file) == prev["file"]["sha256"]:
            print(f"{tid}: version {version} already here")
            meta = json.loads(file.read_text(encoding="utf-8"))
            footnote_count = len(meta.get("footnotes", {}))
        else:
            print(f"{tid}: downloading version {version} ({info.get('title')})")
            surahs, footnotes = download(tid, counts)
            footnote_count = len(footnotes)
            dump({
                "schemaVersion": SCHEMA,
                "id": tid,
                "language": t["language"],
                "source": "QuranEnc.com",
                "title": info.get("title"),
                "version": version,
                "lastUpdate": info.get("last_update"),
                "surahs": surahs,
                "footnotes": footnotes,
            }, file)
            print(f"  1:1 = {surahs[0][0]}")
            print(f"  67:1 = {surahs[66][0]}")
            print(f"  footnotes: {len(footnotes)}")
        entries.append({
            "id": tid,
            "language": t["language"],
            "direction": info.get("direction", "ltr"),
            "translator": t["translator"],
            "title": info.get("title"),
            "description": info.get("description"),
            "version": version,
            "lastUpdate": info.get("last_update"),
            "source": "QuranEnc.com",
            "url": f"https://quranenc.com/en/browse/{tid}",
            "footnotes": footnote_count,
            "file": {"path": f"{tid}.json", "sha256": sha256(file)},
        })

    changed = any(old_by_id.get(e["id"]) != e for e in entries) or len(entries) != len(old_by_id)
    index = {
        "schemaVersion": SCHEMA,
        "revision": old.get("revision", 0) + (1 if changed else 0),
        "translations": entries,
    }
    keep = {e["file"]["path"] for e in entries} | {INDEX}
    for p in directory.iterdir():
        if p.is_file() and p.name not in keep:
            print(f"removing {p.name} (no longer in sources.json)")
            p.unlink()
    dump(index, directory / INDEX, indent=2)
    problems = check(directory)
    if problems:
        raise TranslationError("\n".join(problems))
    print(f"{directory}: revision {index['revision']}, {len(entries)} translations")


def outdated(directory):
    index = load_index(directory) or {"translations": []}
    have = {t["id"]: t.get("version") for t in index["translations"]}
    items = sources()
    infos = remote_infos(items)
    out = []
    for t in items:
        remote = str(infos[t["id"]].get("version", ""))
        if have.get(t["id"]) != remote:
            print(f"{t['id']}: here {have.get(t['id'])}, QuranEnc {remote}", file=sys.stderr)
            out.append(t["id"])
    return out


# --- check / publish ---------------------------------------------------------------------


def check(directory):
    """Problems with DIR's index and files (empty list: all good). Standard library only."""
    directory = Path(directory)
    problems = []
    try:
        index = json.loads((directory / INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"{INDEX}: {e}"]
    if index.get("schemaVersion") != SCHEMA:
        problems.append(f"{INDEX}: schemaVersion must be {SCHEMA}")
    if not isinstance(index.get("revision"), int) or index["revision"] < 1:
        problems.append(f"{INDEX}: revision must be a positive integer")
    counts = None
    if SURAHS_JSON.exists():
        counts = ayah_counts()
    used = {INDEX}
    seen = set()
    for t in index.get("translations", []):
        tid = t.get("id", "?")
        if tid in seen:
            problems.append(f"{tid}: listed twice")
        seen.add(tid)
        for key in ("language", "translator", "version", "source", "file"):
            if not t.get(key):
                problems.append(f"{tid}: {key} missing")
        if "en" not in (t.get("translator") or {}):
            problems.append(f"{tid}: translator needs an English name")
        path = (t.get("file") or {}).get("path", "")
        used.add(path)
        file = directory / path
        if not path or "/" in path or not file.exists():
            problems.append(f"{tid}: file {path!r} missing")
            continue
        if sha256(file) != t["file"].get("sha256"):
            problems.append(f"{path}: sha256 does not match {INDEX}")
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except ValueError as e:
            problems.append(f"{path}: {e}")
            continue
        surahs = data.get("surahs")
        if data.get("id") != tid or data.get("schemaVersion") != SCHEMA:
            problems.append(f"{path}: id must be {tid!r} and schemaVersion {SCHEMA}")
        if not isinstance(surahs, list) or len(surahs) != 114:
            problems.append(f"{path}: must hold 114 surahs")
            continue
        if sum(len(s) for s in surahs) != TOTAL_AYAHS:
            problems.append(f"{path}: must hold {TOTAL_AYAHS} ayahs")
        if counts:
            for n, (s, c) in enumerate(zip(surahs, counts), start=1):
                if len(s) != c:
                    problems.append(f"{path}: surah {n} has {len(s)} ayahs, expected {c}")
        if any(not isinstance(a, str) or not a.strip() for s in surahs for a in s):
            problems.append(f"{path}: empty translation")
    stray = sorted(p.name for p in directory.iterdir() if p.is_file() and p.name not in used and p.name != "README.md")
    for p in stray:
        problems.append(f"{p}: not listed in {INDEX} (delete it)")
    return problems


def publish(directory, site):
    directory, site = Path(directory), Path(site)
    target = site / "catalog" / "translations"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(directory, target)
    (site / "tools").mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__), site / "tools" / "translations.py")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build")
    p.add_argument("--dir", default=str(DEFAULT_DIR))
    p.add_argument("--force", action="store_true", help="download every translation again")
    p = sub.add_parser("outdated")
    p.add_argument("--dir", default=str(DEFAULT_DIR))
    p = sub.add_parser("check")
    p.add_argument("dir", nargs="?", default=str(DEFAULT_DIR))
    p = sub.add_parser("publish")
    p.add_argument("dir")
    p.add_argument("site")
    args = ap.parse_args()
    try:
        if args.cmd == "build":
            build(args.dir, args.force)
        elif args.cmd == "outdated":
            ids = outdated(args.dir)
            print(" ".join(ids))
            sys.exit(2 if ids else 0)
        elif args.cmd == "check":
            problems = check(args.dir)
            for p in problems:
                print(f"::error::{p}")
            if problems:
                sys.exit(1)
            print(f"{args.dir}: ok")
        elif args.cmd == "publish":
            publish(args.dir, args.site)
    except TranslationError as e:
        sys.exit(f"::error::{e}")


if __name__ == "__main__":
    main()
