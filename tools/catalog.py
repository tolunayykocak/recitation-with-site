#!/usr/bin/env python3
"""Reciter catalog tools (SPEC §5.0, docs/okuyucu-ekleme.md).

The catalog is a folder:

    reciters.json          schemaVersion, revision, reciters[]
    manifests/<id>.json    audio manifests (tools/audio/build_manifest.py)
    avatars/<id>.webp      optional photos, 512x512 WebP

The master copy is app/src/main/assets/catalog/ (embedded in the app). The same folder is
published as catalog/ in the public recitation-with-site repo (GitHub Pages), where the app
checks it at most once a day.

    catalog.py check   DIR [--base OLD_RECITERS_JSON]   validate (CI in both repos)
    catalog.py stamp   DIR [--base OLD_RECITERS_JSON]   write sha256s; bump revision if changed
    catalog.py changed [DIR]                            ids whose manifest must be (re)built
    catalog.py avatar  IMAGE --id ID [--catalog DIR]    make avatars/<id>.webp (needs Pillow)
    catalog.py publish DIR SITE_DIR                     mirror DIR into SITE_DIR (+ this script)
    catalog.py compare DIR URL                          same as the published catalog?

Standard library only, except `avatar` (Pillow).
"""

import argparse
import hashlib
import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = ROOT / "app" / "src" / "main" / "assets" / "catalog"
SCHEMA_VERSION = 1  # keep in step with ReciterCatalog.SCHEMA_VERSION in the app
AUDIO_TYPES = {"ayah_files", "surah_files"}
STYLES = {"murattal", "mujawwad", "muallim"}
REWAYAHS = {"hafs", "warsh", "qalun"}
PATH = re.compile(r"[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*")
SHA256 = re.compile(r"[0-9a-f]{64}")
PHOTO_SIZE = 512
MAX_PHOTO_BYTES = 256 * 1024  # the app refuses larger; aim for well under 100 KB
MAX_MANIFEST_BYTES = 8 * 1024 * 1024


class CatalogError(Exception):
    pass


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CatalogError(f"{path}: {e}")


def content(catalog):
    """Everything but the revision, for 'did anything change?'."""
    return json.dumps({k: v for k, v in catalog.items() if k != "revision"}, sort_keys=True, ensure_ascii=False)


def write(path, catalog):
    Path(path).write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def surah_set(spec):
    out = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        bounds = [int(x) for x in part.split("-")]
        if len(bounds) not in (1, 2) or not (1 <= bounds[0] <= bounds[-1] <= 114):
            raise CatalogError(f"bad surah range {part!r}")
        out.update(range(bounds[0], bounds[-1] + 1))
    if not out:
        raise CatalogError("no surahs")
    return out


# --- WebP --------------------------------------------------------------------------------


def webp_info(data):
    """(width, height, animated) of a WebP file, or raises CatalogError."""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise CatalogError("not a WebP file")
    chunk, body = data[12:16], data[20:]
    if chunk == b"VP8 ":
        if body[3:6] != b"\x9d\x01\x2a":
            raise CatalogError("bad VP8 header")
        w = int.from_bytes(body[6:8], "little") & 0x3FFF
        h = int.from_bytes(body[8:10], "little") & 0x3FFF
        return w, h, False
    if chunk == b"VP8L":
        if body[0] != 0x2F:
            raise CatalogError("bad VP8L header")
        bits = int.from_bytes(body[1:5], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1, False
    if chunk == b"VP8X":
        flags = body[0]
        w = int.from_bytes(body[4:7], "little") + 1
        h = int.from_bytes(body[7:10], "little") + 1
        return w, h, bool(flags & 0x02)
    raise CatalogError(f"unknown WebP chunk {chunk!r}")


# --- check -------------------------------------------------------------------------------


def check(directory, base=None):
    """Validates a catalog folder. Returns a list of problems (empty: valid)."""
    directory = Path(directory)
    problems = []
    try:
        catalog = load(directory / "reciters.json")
    except CatalogError as e:
        return [str(e)]
    if catalog.get("schemaVersion") != SCHEMA_VERSION:
        problems.append(f"schemaVersion must be {SCHEMA_VERSION} (the app ignores a file it does not know)")
    if not isinstance(catalog.get("revision"), int) or catalog["revision"] < 1:
        problems.append("revision must be a positive integer")
    reciters = catalog.get("reciters")
    if not isinstance(reciters, list) or not reciters:
        return problems + ["reciters must be a non-empty list"]
    seen, used = set(), {"reciters.json"}
    for i, r in enumerate(reciters):
        rid = r.get("id") if isinstance(r, dict) else None
        where = rid or f"#{i}"

        def bad(msg):
            problems.append(f"{where}: {msg}")

        if not isinstance(r, dict) or not isinstance(rid, str) or not re.fullmatch(r"[a-z0-9_-]+", rid):
            bad("id must be lower case letters, digits, '-' or '_'")
            continue
        if rid in seen:
            bad("listed twice")
        seen.add(rid)
        if r.get("schemaVersion", SCHEMA_VERSION) > SCHEMA_VERSION:
            # Allowed: older apps hide it. This checker just cannot validate it.
            print(f"note: {rid} needs schemaVersion {r['schemaVersion']}; apps that do not know it hide it")
            continue
        for lang in ("en", "tr", "ar"):
            if not (r.get("name") or {}).get(lang):
                bad(f"name.{lang} is missing")
        for key in ("monogram", "initial"):
            if not r.get(key):
                bad(f"{key} is missing")
        if r.get("style") not in STYLES:
            bad(f"style {r.get('style')!r} is not one of {sorted(STYLES)} (a new one needs an app update)")
        if r.get("rewayah") not in REWAYAHS:
            bad(f"rewayah {r.get('rewayah')!r} is not one of {sorted(REWAYAHS)} (a new one needs an app update)")
        try:
            surah_set(r.get("surahs", ""))
        except (CatalogError, ValueError) as e:
            bad(str(e))
        audio = r.get("audio") or []
        if not audio:
            bad("no audio source")
        for a in audio:
            if a.get("type") not in AUDIO_TYPES:
                bad(f"audio type {a.get('type')!r} is unknown: apps hide this reciter (a new type needs an app update)")
            if not str(a.get("url", "")).startswith("https://"):
                bad("audio url must be https")
        for key, ext, required in (("manifest", ".json", True), ("photo", ".webp", False)):
            ref = r.get(key)
            if ref is None:
                if required:
                    bad(f"{key} is missing")
                continue
            path, digest = ref.get("path", ""), ref.get("sha256", "")
            expected = f"{'manifests' if key == 'manifest' else 'avatars'}/{rid}{ext}"
            if path != expected:
                bad(f"{key}.path must be {expected}")
                continue
            if not PATH.fullmatch(path):
                bad(f"bad {key} path {path!r}")
                continue
            file = directory / path
            used.add(path)
            if not file.is_file():
                bad(f"{path} does not exist")
                continue
            if not SHA256.fullmatch(digest) or digest != sha256(file):
                bad(f"{key}.sha256 does not match {path} (run: tools/catalog/catalog.py stamp)")
            data = file.read_bytes()
            if key == "manifest":
                if len(data) > MAX_MANIFEST_BYTES:
                    bad(f"{path} is larger than {MAX_MANIFEST_BYTES} bytes")
                try:
                    m = json.loads(data)
                    if m.get("version") != 1 or m.get("reciter") != rid:
                        bad(f"{path}: version must be 1 and reciter {rid!r}")
                except ValueError as e:
                    bad(f"{path}: {e}")
            else:
                if len(data) > MAX_PHOTO_BYTES:
                    bad(f"{path} is larger than {MAX_PHOTO_BYTES} bytes")
                try:
                    w, h, animated = webp_info(data)
                    if (w, h) != (PHOTO_SIZE, PHOTO_SIZE) or animated:
                        bad(f"{path} must be a still {PHOTO_SIZE}x{PHOTO_SIZE} WebP, is {w}x{h}{' animated' if animated else ''}")
                except CatalogError as e:
                    bad(f"{path}: {e}")
    stray = sorted(
        str(p.relative_to(directory)) for p in directory.rglob("*")
        if p.is_file() and str(p.relative_to(directory)) not in used and p.name != "README.md" and p.name != ".gitkeep"
    )
    for p in stray:
        problems.append(f"{p}: not listed in reciters.json (delete it)")
    if base is not None:
        try:
            old = load(base)
            if content(old) != content(catalog) and catalog.get("revision", 0) <= old.get("revision", 0):
                problems.append(
                    f"the catalog changed but revision {catalog.get('revision')} is not above {old.get('revision')}: "
                    "apps would not download it (run: tools/catalog/catalog.py stamp --base …)"
                )
            elif catalog.get("revision", 0) < old.get("revision", 0):
                problems.append("revision went down")
        except CatalogError:
            pass  # no previous catalog (first publish)
    return problems


# --- stamp / changed ---------------------------------------------------------------------


def stamp(directory, base=None):
    """Writes each file's sha256 into reciters.json; revision = base + 1 if anything changed."""
    directory = Path(directory)
    catalog = load(directory / "reciters.json")
    for r in catalog["reciters"]:
        for key in ("manifest", "photo"):
            ref = r.get(key)
            if ref and (directory / ref["path"]).is_file():
                ref["sha256"] = sha256(directory / ref["path"])
    if base is not None:
        try:
            old = load(base)
        except CatalogError:
            old = None
        if old is not None:
            if content(old) != content(catalog):
                catalog["revision"] = max(catalog.get("revision", 1), old.get("revision", 0) + 1)
            else:
                catalog["revision"] = old.get("revision", catalog.get("revision", 1))
    write(directory / "reciters.json", catalog)
    return catalog["revision"]


def changed(directory):
    """Reciters whose manifest has to be (re)built: none yet, or it was built for other audio
    sources or other surahs than reciters.json lists now (the manifest records both)."""
    directory = Path(directory)
    out = []
    for r in load(directory / "reciters.json")["reciters"]:
        path = (r.get("manifest") or {}).get("path")
        try:
            m = load(directory / path) if path else None
        except CatalogError:
            m = None
        key = lambda a: (a.get("type"), a.get("url"), a.get("timestampsFrom"))
        if (
            m is None
            or [key(a) for a in m.get("sources", [])] != [key(a) for a in r.get("audio", [])]
            or {e.get("surah") for e in m.get("surahs", [])} != surah_set(r.get("surahs", ""))
        ):
            out.append(r["id"])
    return out


# --- avatar ------------------------------------------------------------------------------


def avatar(image, rid, directory):
    """Centre-crops IMAGE to a square, scales it to 512x512 and writes avatars/<id>.webp
    without metadata (EXIF, GPS, …)."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        sys.exit("needs Pillow: pip install pillow")
    with Image.open(image) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        side = min(im.size)
        left, top = (im.width - side) // 2, (im.height - side) // 2
        im = im.crop((left, top, left + side, top + side)).resize((PHOTO_SIZE, PHOTO_SIZE), Image.LANCZOS)
        out = Path(directory) / "avatars" / f"{rid}.webp"
        out.parent.mkdir(parents=True, exist_ok=True)
        for quality in (82, 75, 68, 60):
            im.save(out, "WEBP", quality=quality, method=6)
            if out.stat().st_size <= 100 * 1024:
                break
    print(f"wrote {out} ({out.stat().st_size // 1024} KB). Add to the reciter:")
    print(f'  "photo": {{ "path": "avatars/{rid}.webp", "sha256": "{sha256(out)}" }}')


# --- publish / compare -------------------------------------------------------------------


def publish(directory, site):
    """Makes SITE/catalog an exact copy of DIR and SITE/tools/catalog.py this script."""
    directory, site = Path(directory), Path(site)
    target = site / "catalog"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(directory, target)
    (site / "tools").mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__), site / "tools" / "catalog.py")


def compare(directory, url):
    """True if the published catalog at URL (…/catalog/) has the same reciters.json."""
    url = url.rstrip("/") + "/reciters.json"
    req = urllib.request.Request(url, headers={"User-Agent": "recitation-with-build/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=30) as r:
        live = json.loads(r.read().decode("utf-8"))
    local = load(Path(directory) / "reciters.json")
    return json.dumps(live, sort_keys=True) == json.dumps(local, sort_keys=True), live.get("revision"), local.get("revision")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "stamp"):
        p = sub.add_parser(name)
        p.add_argument("dir", nargs="?", default=str(DEFAULT_DIR))
        p.add_argument("--base", help="the previous reciters.json (e.g. from main or the site)")
    p = sub.add_parser("changed")
    p.add_argument("dir", nargs="?", default=str(DEFAULT_DIR))
    p = sub.add_parser("avatar")
    p.add_argument("image")
    p.add_argument("--id", required=True)
    p.add_argument("--catalog", default=str(DEFAULT_DIR))
    p = sub.add_parser("publish")
    p.add_argument("dir")
    p.add_argument("site")
    p = sub.add_parser("compare")
    p.add_argument("dir")
    p.add_argument("url")
    args = ap.parse_args()

    try:
        if args.cmd == "check":
            problems = check(args.dir, args.base)
            for p in problems:
                print(f"::error::{p}")
            if problems:
                sys.exit(1)
            print(f"{args.dir}: ok")
        elif args.cmd == "stamp":
            print(f"revision {stamp(args.dir, args.base)}")
        elif args.cmd == "changed":
            print(" ".join(changed(args.dir)))
        elif args.cmd == "avatar":
            avatar(args.image, args.id, args.catalog)
        elif args.cmd == "publish":
            publish(args.dir, args.site)
        elif args.cmd == "compare":
            same, live, local = compare(args.dir, args.url)
            print(f"published revision {live}, embedded revision {local}: {'same' if same else 'DIFFERENT'}")
            sys.exit(0 if same else 2)
    except CatalogError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
