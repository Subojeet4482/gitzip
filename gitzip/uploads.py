"""Resumable browser -> server uploads (small chunks that can be retried) + background unzip."""
import json
import os
import shutil
import threading
import time
import uuid

from . import staging
from .config import MAX_UPLOAD_BYTES, TMP_DIR

CHUNK = 4 * 1048576
LOCK = threading.Lock()


class Conflict(Exception):
    def __init__(self, size):
        super().__init__("offset mismatch")
        self.size = size


def _meta_path(uid):
    if not uid.startswith("up_") or not uid[3:].isalnum():
        raise KeyError("bad upload id")
    return TMP_DIR / (uid + ".json")


def _part(uid):
    return TMP_DIR / (uid + ".part")


def _load(uid):
    try:
        return json.loads(_meta_path(uid).read_text("utf-8"))
    except (OSError, ValueError):
        raise KeyError("upload not found")


def init(name, size, staging_id=None, rel=None):
    size = int(size)
    if size <= 0:
        raise ValueError("Khaali file")
    if size > MAX_UPLOAD_BYTES:
        raise ValueError("File bahut badi hai (limit %d MB)" % (MAX_UPLOAD_BYTES >> 20))
    if shutil.disk_usage(str(TMP_DIR)).free < size * 2.5 + (50 << 20):
        raise ValueError("Server par disk space kam hai")
    if staging_id and staging.get(staging_id) is None:
        raise ValueError("Staging nahi mili")
    uid = "up_" + uuid.uuid4().hex[:14]
    meta = {"uid": uid, "name": os.path.basename(name or "file")[:200] or "file", "size": size,
            "staging_id": staging_id, "rel": rel, "created": time.time()}
    _meta_path(uid).write_text(json.dumps(meta), "utf-8")
    _part(uid).write_bytes(b"")
    return {"uid": uid, "chunk": CHUNK, "offset": 0}


def offset(uid):
    _load(uid)
    return _part(uid).stat().st_size


def write_chunk(uid, off, reader, length):
    """Append `length` bytes from reader at `off`. Idempotent for retries: wrong offset -> Conflict(current)."""
    meta = _load(uid)
    p = _part(uid)
    with LOCK:
        cur = p.stat().st_size
        if off != cur:
            # drain the body so the connection stays usable, then tell the client where we are
            _drain(reader, length)
            raise Conflict(cur)
        if cur + length > meta["size"]:
            _drain(reader, length)
            raise ValueError("Chunk file size se zyada hai")
        left = length
        try:
            with open(p, "ab") as f:
                while left > 0:
                    b = reader.read(min(262144, left))
                    if not b:
                        raise ConnectionError("client disconnected mid-chunk")
                    f.write(b)
                    left -= len(b)
        except Exception:
            with open(p, "r+b") as f:  # roll back the half chunk, the client will resend it
                f.truncate(cur)
            raise
        return cur + length


def _drain(reader, length):
    left = length
    while left > 0:
        b = reader.read(min(262144, left))
        if not b:
            break
        left -= len(b)


def finish(uid, staging_id=None):
    """Complete an upload. Zips are unzipped in a background thread; other files are added directly."""
    meta = _load(uid)
    p = _part(uid)
    size = p.stat().st_size
    if size != meta["size"]:
        raise Conflict(size)
    sid = staging_id or meta.get("staging_id")
    name = meta["name"]
    is_zip = name.lower().endswith(".zip") and not meta.get("rel")
    if is_zip:
        st = staging.get(sid) if sid else None
        if st is None:
            st = staging.create(name[:-4] or name, status="extracting")
        else:
            st = staging.update(st["id"], status="extracting", error="")
        threading.Thread(target=_unzip, args=(uid, st["id"]), daemon=True, name="unzip-" + st["id"]).start()
        return st
    st = staging.get(sid) if sid else None
    if st is None:
        st = staging.create("Files")
    rel = meta.get("rel") or name
    staging.add_file(st["id"], rel, p)
    try:
        _meta_path(uid).unlink()
    except OSError:
        pass
    with staging.LOCK:
        cur = staging.get(st["id"])
        return staging.update(st["id"], files=cur["files"] + 1, bytes=cur["bytes"] + size)


def _unzip(uid, sid):
    p = _part(uid)

    def prog(i, n):
        staging.update(sid, progress={"done": i, "total": n})

    try:
        res = staging.extract_zip(str(p), sid, prog)
        staging.recount(sid)
        staging.update(sid, status="ready", error="", notes=res["notes"])
        if res["files"] == 0:
            staging.update(sid, status="error", error="Zip khaali hai (koi file nahi mili).")
    except ValueError as e:
        staging.update(sid, status="error", error=str(e))
    except Exception as e:  # never leave a staging stuck in "extracting"
        staging.update(sid, status="error", error="Extract fail: %s" % e)
    finally:
        for f in (p, _meta_path(uid)):
            try:
                f.unlink()
            except OSError:
                pass


def cleanup(max_age_hours=6):
    cutoff = time.time() - max_age_hours * 3600
    for f in TMP_DIR.glob("up_*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass
