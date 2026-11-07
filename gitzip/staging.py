"""Staging areas: a zip (or loose files) extracted to disk, waiting to be uploaded.

A staging area lives in DATA_DIR/staging/<id>/{meta.json, files/...}. What actually gets
uploaded is computed by effective(): junk/ignore rules, "strip the top folder", destination
folder, size limits and GitHub's invalid path rules are applied there, on the fly, so the
options can be changed at any time without re-extracting.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
import zipfile
from fnmatch import fnmatchcase

from .config import (MAX_EXTRACT_BYTES, MAX_FILE_BYTES, STAGING_DIR, settings)

LOCK = threading.RLock()

JUNK = ["__MACOSX/", ".DS_Store", "Thumbs.db", "desktop.ini", "node_modules/", "__pycache__/",
        "*.pyc", ".venv/", "venv/", ".idea/"]


def new_id(prefix):
    return "%s_%s_%s" % (prefix, time.strftime("%y%m%d%H%M%S"), uuid.uuid4().hex[:5])


def sdir(sid):
    if not re.fullmatch(r"st_[0-9]{12}_[0-9a-f]{5}", sid or ""):
        raise KeyError("bad staging id")
    return STAGING_DIR / sid


def files_dir(sid):
    return sdir(sid) / "files"


# ---------------------------------------------------------------- meta
def _write_meta(meta):
    p = sdir(meta["id"]) / "meta.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta), "utf-8")
    os.replace(tmp, p)


def get(sid):
    try:
        return json.loads((sdir(sid) / "meta.json").read_text("utf-8"))
    except (OSError, ValueError, KeyError):
        return None


def update(sid, **fields):
    with LOCK:
        meta = get(sid)
        if meta is None:
            return None
        meta.update(fields)
        meta["updated"] = time.time()
        _write_meta(meta)
        return meta


def default_options():
    return {"strip_top": "auto", "skip_junk": bool(settings.get("skip_junk")), "ignore": "", "prefix": "", "empty_dirs": False}


def create(name, status="ready"):
    sid = new_id("st")
    (sdir(sid) / "files").mkdir(parents=True, exist_ok=True)
    meta = {"id": sid, "name": (name or "upload")[:120], "created": time.time(), "updated": time.time(),
            "status": status, "error": "", "progress": {"done": 0, "total": 0}, "files": 0, "bytes": 0,
            "notes": [], "options": default_options()}
    with LOCK:
        _write_meta(meta)
    return meta


def list_all():
    out = []
    for p in sorted(STAGING_DIR.glob("st_*"), reverse=True):
        m = get(p.name)
        if m:
            out.append(m)
    return out


def delete(sid):
    shutil.rmtree(sdir(sid), ignore_errors=True)


def recount(sid):
    n = b = 0
    for dp, _, fns in os.walk(files_dir(sid)):
        for fn in fns:
            try:
                st = os.lstat(os.path.join(dp, fn))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                n += 1
                b += st.st_size
    return update(sid, files=n, bytes=b)


def set_options(sid, patch):
    meta = get(sid)
    if meta is None:
        raise KeyError(sid)
    o = dict(meta["options"])
    if "strip_top" in patch:
        v = patch["strip_top"]
        o["strip_top"] = {True: "on", False: "off"}.get(v, v if v in ("auto", "on", "off") else "auto")
    if "skip_junk" in patch:
        o["skip_junk"] = bool(patch["skip_junk"])
    if "empty_dirs" in patch:
        o["empty_dirs"] = bool(patch["empty_dirs"])
    if "ignore" in patch:
        o["ignore"] = str(patch["ignore"])[:4000]
    if "prefix" in patch:
        raw = str(patch["prefix"]).strip()
        if raw:
            c = clean_rel(raw)
            if c is None or any(s.lower() == ".git" for s in c.split("/")):
                raise ValueError("Folder ka naam valid nahi hai")
            raw = c
        o["prefix"] = raw
    return update(sid, options=o)


# ---------------------------------------------------------------- paths
def clean_rel(name):
    """Normalise a path coming from a zip / browser. None if it is unsafe."""
    parts = []
    for p in name.replace("\\", "/").split("/"):
        if p in ("", "."):
            continue
        if p == "..":
            return None
        parts.append(p)
    if parts and re.fullmatch(r"[A-Za-z]:", parts[0]):
        parts = parts[1:]
    if not parts or any("\x00" in p or len(p.encode("utf-8", "replace")) > 250 for p in parts):
        return None
    return "/".join(parts)


def safe_target(root, rel):
    target = os.path.normpath(os.path.join(str(root), rel))
    if not (target == str(root) or target.startswith(str(root) + os.sep)):
        return None
    return target


def _zip_name(info):
    n = info.filename
    if not (info.flag_bits & 0x800):
        try:  # many tools write UTF-8 names without setting the flag; Python then guesses cp437
            n = n.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return n


def extract_zip(zip_path, sid, progress=None):
    """Safe extraction (zip-slip, bombs, symlinks, .git). Returns dict(files, bytes, notes)."""
    root = files_dir(sid)
    notes, n_files, total = [], 0, 0

    def note(s):
        if len(notes) < 40:
            notes.append(s)

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise ValueError("Ye valid zip file nahi hai (corrupt ya adhoora upload).")
    with zf:
        infos = zf.infolist()
        declared = sum(i.file_size for i in infos if not i.is_dir())
        if declared > MAX_EXTRACT_BYTES:
            raise ValueError("Zip ke andar ka data bahut bada hai (%d MB). Limit %d MB." % (declared >> 20, MAX_EXTRACT_BYTES >> 20))
        free = shutil.disk_usage(str(root)).free
        if declared * 1.1 > free:
            raise ValueError("Server par disk space kam hai (%d MB free)." % (free >> 20))
        for i, info in enumerate(infos):
            if progress and i % 25 == 0:
                progress(i, len(infos))
            rel = clean_rel(_zip_name(info))
            if rel is None:
                note("skip (unsafe path): " + info.filename[:80])
                continue
            if any(s.lower() == ".git" for s in rel.split("/")):
                continue
            mode = (info.external_attr >> 16) & 0xFFFF
            if info.is_dir():
                target = safe_target(root, rel)
                if target:
                    os.makedirs(target, exist_ok=True)
                continue
            if stat.S_ISLNK(mode):
                note("skip (symlink): " + rel)
                continue
            if info.flag_bits & 0x1:
                raise ValueError("Zip password-protected hai — pehle password hatao.")
            target = safe_target(root, rel)
            if target is None:
                note("skip (unsafe path): " + rel)
                continue
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if os.path.isdir(target):
                    note("skip (folder/file clash): " + rel)
                    continue
                with zf.open(info) as src, open(target, "wb") as dst:
                    while True:
                        buf = src.read(1 << 20)
                        if not buf:
                            break
                        total += len(buf)
                        if total > MAX_EXTRACT_BYTES:
                            raise ValueError("Zip zyada bada nikla (zip-bomb?). Extract rok diya.")
                        dst.write(buf)
                if mode & 0o111:
                    os.chmod(target, 0o755)
                n_files += 1
            except (OSError, NotADirectoryError) as e:
                note("skip (%s): %s" % (e.strerror or e, rel))
    if progress:
        progress(len(infos), len(infos))
    return {"files": n_files, "bytes": total, "notes": notes}


def add_file(sid, rel, src_path):
    """Move an uploaded temp file into the staging area."""
    rel = clean_rel(rel)
    if rel is None or any(s.lower() == ".git" for s in rel.split("/")):
        raise ValueError("File ka path valid nahi hai")
    target = safe_target(files_dir(sid), rel)
    if target is None:
        raise ValueError("File ka path valid nahi hai")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.isdir(target):
        shutil.rmtree(target)
    shutil.move(str(src_path), target)
    return rel


# ---------------------------------------------------------------- effective file list
def make_ignore(text):
    rules = [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not ln.strip().startswith("#")]

    def match(rel):
        segs = rel.split("/")
        for p in rules:
            if p.endswith("/"):
                d = p.rstrip("/").lstrip("/")
                if "/" in d:
                    if rel.startswith(d + "/"):
                        return True
                elif any(fnmatchcase(s, d) for s in segs[:-1]):
                    return True
            elif "/" in p:
                pp = p.lstrip("/")
                if fnmatchcase(rel, pp) or rel.startswith(pp + "/"):
                    return True
            elif any(fnmatchcase(s, p) for s in segs):
                return True
        return False
    return match


def effective(sid, opts=None):
    """What would be uploaded right now. Returns dict(files, strip, counts, notes).
    `opts` lets a job use the options snapshot it was created with."""
    meta = get(sid)
    if meta is None:
        raise KeyError(sid)
    o = opts or meta["options"]
    root = str(files_dir(sid))
    ign_user = make_ignore(o.get("ignore", ""))
    ign_junk = make_ignore("\n".join(JUNK)) if o.get("skip_junk") else (lambda r: False)

    raw, empty = [], []
    for dp, dns, fns in os.walk(root):
        dns.sort()
        rd = os.path.relpath(dp, root).replace(os.sep, "/")
        rd = "" if rd == "." else rd
        if rd and not dns and not fns:
            empty.append(rd)
        for fn in sorted(fns):
            fp = os.path.join(dp, fn)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                raw.append(((rd + "/" if rd else "") + fn, st.st_size, "100755" if st.st_mode & 0o111 else "100644", fp))
    if o.get("empty_dirs"):
        for d in empty:
            raw.append((d + "/.gitkeep", 0, "100644", None))

    rows = []
    for rel, size, mode, fp in raw:
        skip = None
        segs = rel.split("/")
        if any(s.lower() == ".git" or any(ord(c) < 32 for c in s) for s in segs) or len(rel) > 900:
            skip = "invalid"
        elif ign_user(rel):
            skip = "ignored"
        elif ign_junk(rel):
            skip = "junk"
        elif size > MAX_FILE_BYTES:
            skip = "too_big"
        rows.append({"rel": rel, "size": size, "mode": mode, "src": fp, "skip": skip})

    strip = ""
    if o.get("strip_top", "auto") != "off":
        kept = [r["rel"] for r in rows if r["skip"] is None]
        tops = {p.split("/")[0] for p in kept}
        if kept and len(tops) == 1 and all("/" in p for p in kept):
            strip = tops.pop() + "/"
    prefix = (o.get("prefix") or "").strip("/")
    out = []
    for r in rows:
        rel = r["rel"][len(strip):] if strip and r["rel"].startswith(strip) else r["rel"]
        if r["skip"] is None and (ign_user(rel) or ign_junk(rel)):
            r["skip"] = "ignored"
        dest = (prefix + "/" if prefix else "") + rel
        out.append({"path": dest, "size": r["size"], "mode": r["mode"], "src": r["src"], "skip": r["skip"]})
    out.sort(key=lambda r: r["path"])
    up = [r for r in out if r["skip"] is None]
    reasons = {}
    for r in out:
        if r["skip"]:
            reasons[r["skip"]] = reasons.get(r["skip"], 0) + 1
    return {"files": out, "strip": strip,
            "counts": {"total": len(out), "upload": len(up), "skipped": len(out) - len(up),
                       "bytes": sum(r["size"] for r in up), "reasons": reasons}}


# ---------------------------------------------------------------- hashing / reading
def git_blob_sha(src, size=None):
    if src is None:
        return hashlib.sha1(b"blob 0\0").hexdigest()
    size = os.path.getsize(src) if size is None else size
    h = hashlib.sha1(b"blob %d\0" % size)
    with open(src, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def read_bytes(src):
    if src is None:
        return b""
    with open(src, "rb") as f:
        return f.read()


def as_text(data):
    """UTF-8 text if the blob is safely representable as a JSON string, else None."""
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ---------------------------------------------------------------- text files inside staging
def read_text(sid, rel, limit=200000):
    rel = clean_rel(rel)
    t = safe_target(files_dir(sid), rel) if rel else None
    if not t or not os.path.isfile(t):
        raise FileNotFoundError(rel)
    with open(t, "rb") as f:
        data = f.read(limit + 1)
    txt = as_text(data[:limit])
    if txt is None:
        raise ValueError("Binary file hai, text me nahi khul sakti")
    return txt, len(data) > limit


def write_text(sid, rel, text):
    rel = clean_rel(rel)
    if rel is None or any(s.lower() == ".git" for s in rel.split("/")):
        raise ValueError("Path valid nahi hai")
    t = safe_target(files_dir(sid), rel)
    os.makedirs(os.path.dirname(t), exist_ok=True)
    with open(t, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    recount(sid)
    return rel


def cleanup_old(max_age_hours, busy_ids=()):
    cutoff = time.time() - max_age_hours * 3600
    for m in list_all():
        if m["id"] in busy_ids:
            continue
        if m.get("updated", m["created"]) < cutoff:
            delete(m["id"])


def dest_to_rel(sid, dest):
    """Map an upload (destination) path back to the path inside the staging folder."""
    meta = get(sid)
    dest = clean_rel(dest or "")
    if dest is None:
        raise ValueError("Path valid nahi hai")
    prefix = (meta["options"].get("prefix") or "").strip("/")
    if prefix and dest.startswith(prefix + "/"):
        dest = dest[len(prefix) + 1:]
    strip = effective(sid)["strip"]
    return strip + dest
