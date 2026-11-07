"""Engine tests: zip -> staging -> upload job against the fake GitHub. Run: python3 tests/test_engine.py"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

TMP = tempfile.mkdtemp(prefix="gz-test-")
os.environ.update({"DATA_DIR": TMP, "GITHUB_API_URL": "http://127.0.0.1:9101", "GZ_PACE": "0",
                   "no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"})
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(k, None)

import mock_services as ms  # noqa: E402
ms.serve(ms.GitHubHandler, 9101)

from gitzip import jobs, staging, uploads  # noqa: E402
from gitzip.config import settings  # noqa: E402

settings.update({"github_token": "good-token", "chunk_files": 100})
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   ", name)
    else:
        FAIL += 1
        print("  FAIL ", name, extra)


def mock(path, body=None):
    req = urllib.request.Request("http://127.0.0.1:9101/__mock/" + path, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def remote_files(full, branch="main"):
    return mock("files", {"full": full, "branch": branch})


def stats():
    return mock("stats")


def make_zip(files, top=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            info = zipfile.ZipInfo((top + "/" if top else "") + name)
            info.external_attr = (0o755 if name.endswith(".sh") else 0o644) << 16
            if name.endswith("/"):
                info.external_attr = (0o755 << 16) | 0x10
            z.writestr(info, data)
    return buf.getvalue()


def upload_zip(name, data, staging_id=None):
    """Go through the real chunked-upload code path (tiny chunks to exercise offsets)."""
    up = uploads.init(name, len(data), staging_id)
    off = 0
    while off < len(data):
        piece = data[off:off + 7000]
        try:
            uploads.write_chunk(up["uid"], off, io.BytesIO(piece), len(piece))
        except uploads.Conflict:
            raise
        off += len(piece)
    # retry of the last chunk must be rejected cleanly, not corrupt the file
    try:
        uploads.write_chunk(up["uid"], off - len(piece), io.BytesIO(piece), len(piece))
        ok = False
    except uploads.Conflict as c:
        ok = c.size == len(data)
    assert ok, "duplicate chunk should give Conflict"
    st = uploads.finish(up["uid"], staging_id)
    for _ in range(100):
        st = staging.get(st["id"])
        if st["status"] != "extracting":
            break
        time.sleep(0.1)
    return st


def wait_job(jid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = jobs.store.get(jid)
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.1)
    raise TimeoutError(jid)


def run_job(sid, repo, **kw):
    j = jobs.start_job(sid, repo, **kw)
    return wait_job(j["id"])


BIG = ("line %d\n" * 1) * 1
PNG = bytes(range(256)) * 40                      # binary
FILES = {
    "README.md": "# demo\n",
    "src/app.py": "print('hi')\n" * 50,
    "src/utils.py": "def f():\n    return 1\r\n",     # CRLF inside
    "assets/logo.png": PNG,
    "docs/with space/नमस्ते (1).txt": "hindi file\n",
    "run.sh": "#!/bin/sh\necho hi\n",
    "empty-dir/": "",
    "node_modules/x/index.js": "junk",
    "__MACOSX/._README.md": "junk",
    ".git/config": "[core]",
    "big.txt": "x" * 300_000,                      # too big for inline -> blob
}
raw = {k: (v.encode() if isinstance(v, str) else v) for k, v in FILES.items()}

print("\n== 1. zip extraction + first upload (new repo, strip top folder, junk removed)")
st = upload_zip("demo-main.zip", make_zip(raw, top="demo-main"))
check("staging ready", st["status"] == "ready", st)
eff = staging.effective(st["id"])
check("top folder auto-stripped", eff["strip"] == "demo-main/", eff["strip"])
skipped = {f["path"]: f["skip"] for f in eff["files"] if f["skip"]}
check("junk skipped", skipped.get("node_modules/x/index.js") == "junk" and skipped.get("__MACOSX/._README.md") == "junk", skipped)
check(".git folder never extracted", not any(".git" in f["path"].split("/") for f in eff["files"]))
j = run_job(st["id"], "demo", create_repo=True, private=True)
check("job done + verified", j["status"] == "done" and j["verified"], (j["status"], j["error"]))
rf = remote_files("tester/demo")
check("repo has expected files", set(rf) == {"README.md", "src/app.py", "src/utils.py", "assets/logo.png", "docs/with space/नमस्ते (1).txt", "run.sh", "big.txt"}, sorted(rf))
check("CRLF preserved", rf["src/utils.py"].endswith("return 1\r\n"))
check("big file intact", len(rf["big.txt"]) == 300_000)
check("README replaced auto_init README", rf["README.md"] == "# demo\n")

print("\n== 2. same upload again -> nothing is re-sent")
mock("clearstats")
j2 = run_job(st["id"], "tester/demo")
s = stats()
check("job done", j2["status"] == "done", j2["error"])
check("0 files to upload", j2["counts"]["todo"] == 0 and j2["counts"]["same"] == 7, j2["counts"])
check("no blob/tree/commit calls", not any(k in s for k in ("blobs", "trees", "commits")) or (s.get("blobs", 0) == 0 and s.get("commits", 0) == 0), s)

print("\n== 3. change one file -> only that file uploads")
staging.write_text(st["id"], "demo-main/src/app.py", "print('changed')\n")
mock("clearstats")
j3 = run_job(st["id"], "tester/demo")
check("1 changed file", j3["counts"]["todo"] == 1 and j3["counts"]["same"] == 6 and j3["status"] == "done", j3["counts"])
check("remote updated", remote_files("tester/demo")["src/app.py"] == "print('changed')\n")
check("exactly one commit", stats().get("commits", 0) == 1, stats())
cmp = jobs.compare(st["id"], "tester/demo")
check("compare says all same", cmp["new"] == 0 and cmp["changed"] == 0 and cmp["same"] == 7, cmp)

print("\n== 4. flaky GitHub: 502 on blobs, secondary rate limit, then success")
mock("rules", {"rules": [{"match": "git/blobs", "status": 502, "times": 2},
                         {"match": "git/trees", "method": "POST", "status": 403, "retry_after": 1, "times": 1,
                          "message": "You have exceeded a secondary rate limit"}]})
staging.write_text(st["id"], "demo-main/new1.bin.txt", "n" * 300_000)   # forces a blob call
t0 = time.time()
j4 = run_job(st["id"], "tester/demo")
check("survived 502s + rate limit", j4["status"] == "done" and j4["verified"], (j4["status"], j4["error"]))
check("new file present", "new1.bin.txt" in remote_files("tester/demo"))
mock("rules", {"rules": []})

print("\n== 5. someone pushes to the branch while we upload (non-fast-forward)")
staging.write_text(st["id"], "demo-main/race.txt", "race\n")
mock("rules", {"rules": [{"match": "git/refs/heads", "method": "PATCH", "status": 422, "times": 1,
                          "message": "Update is not a fast forward",
                          "external_push": {"full": "tester/demo", "path": "theirs.txt", "content": "theirs\n"}}]})
j5 = run_job(st["id"], "tester/demo")
rf = remote_files("tester/demo")
check("rebased and finished", j5["status"] == "done", (j5["status"], j5["error"]))
check("both our and their file exist", rf.get("race.txt") == "race\n" and rf.get("theirs.txt") == "theirs\n")
mock("rules", {"rules": []})

print("\n== 6. empty repository (Git Data API refuses until first commit)")
mock("repo", {"full": "tester/empty", "init": False})
j6 = run_job(st["id"], "tester/empty")
check("bootstrapped empty repo", j6["status"] == "done" and j6["verified"], (j6["status"], j6["error"]))
check("all files in empty repo", len(remote_files("tester/empty")) >= 8, len(remote_files("tester/empty")))

print("\n== 7. new branch + repo that is not ready right after creation (404)")
j7 = run_job(st["id"], "tester/demo", branch="dev")
check("branch created and filled", j7["status"] == "done", (j7["status"], j7["error"]))
check("dev has our files", "run.sh" in remote_files("tester/demo", "dev"))
j7b = run_job(st["id"], "fresh-one", create_repo=True)
check("fresh repo survived 404 window", j7b["status"] == "done", (j7b["status"], j7b["error"]))

print("\n== 8. repo that does not exist / no access -> clear message, no crash")
j8 = run_job(st["id"], "tester/nope")
check("failed with explanation", j8["status"] == "failed" and ("nahi mila" in j8["error"] or "404" in j8["error"]), j8["error"])
mock("repo", {"full": "tester/readonly", "push": False})
j8b = run_job(st["id"], "tester/readonly")
check("read-only repo explained", j8b["status"] == "failed" and "write access" in j8b["error"], j8b["error"])
settings.update({"github_token": "bad-token"})
j8c = run_job(st["id"], "tester/demo")
check("bad token explained", j8c["status"] == "failed" and "token" in j8c["error"].lower(), j8c["error"])
settings.update({"github_token": "good-token"})

print("\n== 9. GitHub rewrites inline text (CRLF) -> verification catches it and repairs through blobs")
mock("rules", {"rules": [], "mangle_inline": True})
st2 = upload_zip("crlf.zip", make_zip({"a.txt": b"one\r\ntwo\r\n", "b.txt": b"plain\n"}))
j9 = run_job(st2["id"], "tester/crlf", create_repo=True)
rf = remote_files("tester/crlf")
check("repaired to exact bytes", j9["status"] == "done" and rf["a.txt"] == "one\r\ntwo\r\n", (j9["status"], j9["error"], rf))
mock("rules", {"rules": []})

print("\n== 10. huge repo (tree listing truncated) -> lazy folder walk, still skips identical")
mock("rules", {"rules": [], "truncate_over": 3})
mock("clearstats")
j10 = run_job(st["id"], "tester/demo")
check("lazy mode: nothing re-uploaded", j10["status"] == "done" and j10["counts"]["todo"] == 0, (j10["counts"], j10["error"]))
mock("rules", {"rules": []})

print("\n== 11. cancel while waiting on a rate limit, then resume")
staging.write_text(st["id"], "demo-main/cancel-me.txt", "c\n")
mock("rules", {"rules": [{"match": "git/trees", "method": "POST", "status": 403, "retry_after": 30, "times": 1,
                          "message": "You have exceeded a secondary rate limit"}]})
jj = jobs.start_job(st["id"], "tester/demo")
time.sleep(1.2)
check("job is waiting", jobs.store.get(jj["id"])["status"] == "running")
t0 = time.time()
jobs.runner.cancel(jj["id"])
j11 = wait_job(jj["id"], 10)
check("cancelled quickly", j11["status"] == "cancelled" and time.time() - t0 < 5, (j11["status"], time.time() - t0))
mock("rules", {"rules": []})
jobs.runner.resume(jj["id"])
j11b = wait_job(jj["id"])
check("resume finished the job", j11b["status"] == "done" and remote_files("tester/demo").get("cancel-me.txt") == "c\n", (j11b["status"], j11b["error"]))

print("\n== 12. server restart: job stuck in 'running' is picked up again")
jobs.store.update(j11b["id"], status="running")
n = jobs.runner.resume_pending()
j12 = wait_job(j11b["id"])
check("resumed after restart", n == 1 and j12["status"] == "done", (n, j12["status"], j12["error"]))

print("\n== 13. many files -> several commits (chunking) and path edge cases")
many = {"f%03d.txt" % i: "file %d\n" % i for i in range(250)}
many["weird #1/100%/a?b=c.txt"] = "special chars\n"
st3 = upload_zip("many.zip", make_zip({k: v.encode() for k, v in many.items()}))
settings.update({"chunk_files": 100})
j13 = run_job(st3["id"], "tester/many", create_repo=True)
check("251 files uploaded in 3 commits", j13["status"] == "done" and len(j13["commits"]) == 3 and j13["counts"]["done"] == 251, (j13["status"], len(j13["commits"]), j13["counts"], j13["error"]))
check("special-char path survived", remote_files("tester/many").get("weird #1/100%/a?b=c.txt") == "special chars\n")

print("\n== 14. zip safety")
evil = io.BytesIO()
with zipfile.ZipFile(evil, "w") as z:
    z.writestr("../../escape.txt", "x")
    z.writestr("/abs/path.txt", "x")
    z.writestr("ok/fine.txt", "ok")
st4 = upload_zip("evil.zip", evil.getvalue())
paths = [f["path"] for f in staging.effective(st4["id"])["files"]]
check("zip-slip blocked, absolute path normalised", st4["status"] == "ready" and not os.path.exists(os.path.join(TMP, "escape.txt")) and "ok/fine.txt" in paths, (st4["status"], paths))
bad = upload_zip("notzip.zip", b"this is not a zip at all" * 100)
check("corrupt zip -> readable error", bad["status"] == "error" and "zip" in bad["error"].lower(), bad)

print("\n== 15. ignore rules / prefix")
staging.set_options(st["id"], {"ignore": "*.png\ndocs/\n", "prefix": "site/v1"})
eff = staging.effective(st["id"])
paths = [f["path"] for f in eff["files"] if not f["skip"]]
check("custom ignore + prefix", all(p.startswith("site/v1/") for p in paths) and not any(p.endswith(".png") or "/docs/" in p for p in paths), paths)

print("\n%d passed, %d failed" % (PASS, FAIL))
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
