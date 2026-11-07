"""Upload jobs: a single background worker that pushes staged files to GitHub.

Design (this is what fixes the old "close the tab = upload stops", "re-uploads everything"
and random-404 problems):

* The job runs on the server; the browser only watches it.
* Every run starts by diffing local git-blob SHAs against the repo tree, so unchanged files are
  never sent again and a re-run (resume / retry / restart) continues where it stopped.
* Files go through the Git Data API (blobs -> tree -> commit -> ref) in chunks, one commit per
  chunk, so progress is durable on GitHub itself. No file path ever goes into a URL, which
  removes the whole class of "404 because of a funny file name".
* The end result is verified against the real tree and missing files are repaired.
"""
import json
import os
import queue
import re
import threading
import time
import traceback
import uuid

from . import staging
from .config import JOBS_DIR, settings
from .ghclient import GH, Cancelled, GitHubError, RemoteIndex, UserError, friendly

INLINE_MAX = 200_000        # text files up to this size travel inside the tree request (no blob call)
INLINE_BUDGET = 2_000_000   # ... and at most this many bytes per commit
CHUNK_BYTES = 25 * 1048576
ACTIVE = ("queued", "running")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$")


# ======================================================================= store
class JobStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs = {}
        self._last_write = {}
        for d in sorted(JOBS_DIR.glob("j_*")):
            try:
                j = json.loads((d / "job.json").read_text("utf-8"))
                self.jobs[j["id"]] = j
            except (OSError, ValueError, KeyError):
                continue

    def _path(self, jid):
        return JOBS_DIR / jid

    def create(self, **fields):
        jid = staging.new_id("j")
        job = {"id": jid, "status": "queued", "phase": "", "note": "", "error": "", "created": time.time(),
               "started": None, "finished": None, "attempts": 1, "repo_url": "", "branch": "", "head": "",
               "commits": [], "errors": [], "verified": False, "bytes_total": 0, "bytes_done": 0,
               "chunk": 0, "chunks": 0,
               "counts": {"total": 0, "skipped": 0, "same": 0, "todo": 0, "done": 0, "failed": 0}}
        job.update(fields)
        self._path(jid).mkdir(parents=True, exist_ok=True)
        with self.lock:
            self.jobs[jid] = job
        self.save(jid, force=True)
        return job

    def get(self, jid):
        with self.lock:
            return self.jobs.get(jid)

    def snapshot(self, jid, errors_cap=100):
        with self.lock:
            j = self.jobs.get(jid)
            if not j:
                return None
            s = dict(j)
            s["counts"] = dict(j["counts"])
            s["errors"] = list(j["errors"][:errors_cap])
            s["errors_total"] = len(j["errors"])
            s["options"] = None
            return s

    def all(self):
        with self.lock:
            return sorted(self.jobs.values(), key=lambda j: j["created"], reverse=True)

    def update(self, jid, force=False, **kw):
        with self.lock:
            j = self.jobs.get(jid)
            if not j:
                return None
            for k, v in kw.items():
                if k == "counts":
                    j["counts"].update(v)
                else:
                    j[k] = v
        self.save(jid, force=force or "status" in kw)
        return j

    def save(self, jid, force=False):
        now = time.time()
        if not force and now - self._last_write.get(jid, 0) < 0.6:
            return
        with self.lock:
            j = self.jobs.get(jid)
            if not j:
                return
            self._last_write[jid] = now
            p = self._path(jid)
            p.mkdir(parents=True, exist_ok=True)
            tmp = p / "job.tmp"
            tmp.write_text(json.dumps(j), "utf-8")
            os.replace(tmp, p / "job.json")

    def log(self, jid, text):
        line = "[%s] %s\n" % (time.strftime("%H:%M:%S"), text)
        try:
            with open(self._path(jid) / "log.txt", "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass

    def read_log(self, jid, since=0):
        p = self._path(jid) / "log.txt"
        try:
            size = p.stat().st_size
            if since > size:
                since = 0
            with open(p, "rb") as f:
                f.seek(since)
                data = f.read(200_000)
            return data.decode("utf-8", "replace"), since + len(data)
        except OSError:
            return "", 0

    def delete(self, jid):
        import shutil
        with self.lock:
            self.jobs.pop(jid, None)
        shutil.rmtree(self._path(jid), ignore_errors=True)


store = JobStore()


# ======================================================================= helpers
def _token():
    return settings.get("github_token")


def classify(files, idx):
    """Split files into (new, changed, same) by comparing git SHAs with the remote index."""
    new, changed, same = [], [], []
    for f in files:
        r = idx.get(f["path"]) if idx else None
        if r is None:
            new.append(f)
        elif r[0] != f["sha"] or r[1] != f["mode"]:
            changed.append(f)
        else:
            same.append(f)
    return new, changed, same


def hash_files(files, gh=None, log=None):
    for i, f in enumerate(files):
        if gh:
            gh.check_cancel()
        f["sha"] = staging.git_blob_sha(f["src"], f["size"])
        if log and len(files) > 400 and i and i % 400 == 0:
            log("🔎 %d/%d files check ho gayi" % (i, len(files)))


def make_chunks(todo, max_files, max_bytes=CHUNK_BYTES):
    chunks, cur, b = [], [], 0
    for f in todo:
        if cur and (len(cur) >= max_files or b + f["size"] > max_bytes):
            chunks.append(cur)
            cur, b = [], 0
        cur.append(f)
        b += f["size"]
    if cur:
        chunks.append(cur)
    return chunks


def full_name(gh, repo):
    if "/" in repo:
        return repo
    return "%s/%s" % (gh.me()[0]["login"], repo)


def compare(staging_id, repo, branch="", opts=None):
    """Dry run: how many files are new / changed / already identical in the repo."""
    gh = GH(_token())
    eff = staging.effective(staging_id, opts)
    files = [f for f in eff["files"] if f["skip"] is None]
    hash_files(files)
    full = full_name(gh, repo)
    out = {"repo": full, "repo_exists": True, "branch": branch, "total": len(files), "skipped": eff["counts"]["skipped"]}
    try:
        info = gh.get_repo(full)
        branch = branch or info["default_branch"]
        out["branch"] = branch
        try:
            head = gh.get_ref(full, branch)
        except GitHubError as e:
            if e.kind in ("notfound", "empty"):
                out["branch_exists"] = False
                idx = None
            else:
                raise
        else:
            idx = RemoteIndex(gh, full, gh.get_commit(full, head)["tree"]["sha"])
        if (info.get("permissions") or {}).get("push") is False:
            out["warning"] = "Is repo me tumhe write access nahi hai"
    except GitHubError as e:
        if e.kind != "notfound":
            raise
        out["repo_exists"] = False
        idx = None
    new, changed, same = classify(files, idx)
    out.update({"new": len(new), "changed": len(changed), "same": len(same),
                "new_sample": [f["path"] for f in new[:12]], "changed_sample": [f["path"] for f in changed[:12]],
                "bytes": sum(f["size"] for f in new + changed)})
    return out


def start_job(staging_id, repo, branch="", message="", create_repo=False, private=True, source="ui"):
    repo = (repo or "").strip().strip("/")
    if not REPO_RE.match(repo):
        raise ValueError("Repo ka naam 'owner/repo' ya sirf 'repo' format me do")
    st = staging.get(staging_id)
    if st is None:
        raise ValueError("Staging nahi mili")
    if st["status"] != "ready":
        raise ValueError("Zip abhi extract ho rahi hai ya error me hai")
    if not _token():
        raise ValueError("Pehle Settings me GitHub token daalo")
    eff = staging.effective(staging_id)
    if not eff["counts"]["upload"]:
        raise ValueError("Upload karne layak koi file nahi (sab skip/ignore ho gayi)")
    job = store.create(staging_id=staging_id, staging_name=st["name"], repo=repo, branch=(branch or "").strip(),
                       message=(message or settings.get("commit_message") or "Upload via GitZip")[:200],
                       create_repo=bool(create_repo), private=bool(private), source=source,
                       options=dict(st["options"]), prefix=st["options"].get("prefix", ""))
    store.update(job["id"], counts={"total": eff["counts"]["upload"], "skipped": eff["counts"]["skipped"]})
    runner.enqueue(job["id"])
    return store.snapshot(job["id"])


# ======================================================================= the task
class UploadTask:
    def __init__(self, job, cancel_event):
        self.job, self.jid, self.ev = job, job["id"], cancel_event
        self.inline_ok = True
        self.gh = None
        self.todo_n = 0

    def log(self, s):
        store.log(self.jid, s)

    def upd(self, **kw):
        store.update(self.jid, **kw)

    def counts(self, **kw):
        store.update(self.jid, counts=kw)

    # ---------------------------------------------------------------- main flow
    def run(self):
        j = self.job
        if not _token():
            raise UserError("GitHub token nahi mila. Settings me token daalo.")
        self.gh = gh = GH(_token(), log=self.log, cancel=self.ev.is_set, on_wait=lambda s: self.upd(note=s))
        if staging.get(j["staging_id"]) is None:
            raise UserError("Staged files nahi mili (server restart me hat gayi?). Zip dobara upload karo — jo file pehle chadh chuki hai wo skip ho jayegi.")

        self.upd(phase="connect", note="")
        me, hdrs = gh.me()
        self.log("👤 GitHub user: %s" % me["login"])
        full = full_name(gh, j["repo"])
        fresh = False
        try:
            info = gh.get_repo(full)
        except GitHubError as e:
            if e.kind != "notfound":
                raise
            if not j.get("create_repo"):
                raise UserError("Repo '%s' nahi mila. Ya naam galat hai, ya token ko is repo ka access nahi (fine-grained token me repo select karo). "
                                "Naya repo banana ho to 'Create repo' option on karo." % full)
            self.log("🆕 Repo bana raha hoon: %s (%s)" % (full, "private" if j.get("private", True) else "public"))
            info = gh.create_repo(full, j.get("private", True), "", me["login"])
            fresh = True
        perms = info.get("permissions") or {}
        if perms.get("push") is False:
            raise UserError("Is repo (%s) me tumhe write access nahi hai." % full)
        if info.get("archived"):
            raise UserError("Repo archived hai — GitHub par unarchive karo.")
        self.full = full
        self.branch = j.get("branch") or info["default_branch"]
        self.upd(repo=full, repo_url=info.get("html_url", ""), branch=self.branch)

        # ---- what to upload
        self.upd(phase="scan")
        eff = staging.effective(j["staging_id"], j.get("options"))
        files = [f for f in eff["files"] if f["skip"] is None]
        if not files:
            raise UserError("Upload karne layak koi file nahi bachi.")
        self.log("📦 %d file upload ke liye (%d skip: %s)" % (len(files), eff["counts"]["skipped"], eff["counts"]["reasons"] or "-"))
        hash_files(files, gh, self.log)
        self.counts(total=len(files), skipped=eff["counts"]["skipped"], same=0, todo=0, done=0, failed=0)

        # ---- branch head (creates branch / first commit of an empty repo when needed)
        self.head = self._resolve_head(info, files, fresh)
        self.base_tree = gh.get_commit(full, self.head)["tree"]["sha"]
        self.remote = RemoteIndex(gh, full, self.base_tree)
        new, changed, same = classify(files, self.remote)
        todo = new + changed
        todo.sort(key=lambda f: f["path"])
        self.todo_n = len(todo)
        self.counts(same=len(same), todo=len(todo))
        self.log("🔍 Compare: %d nayi, %d badli hui, %d pehle se same (skip)" % (len(new), len(changed), len(same)))
        self.upd(bytes_total=sum(f["size"] for f in todo), bytes_done=0)
        if not todo:
            self.log("✅ Sab kuch pehle se up-to-date hai — kuch upload karne ki zarurat nahi.")
            self.finish(files, [])
            return

        # ---- upload in chunks (1 commit each)
        self.upd(phase="upload")
        chunks = make_chunks(todo, int(settings.get("chunk_files")))
        self.upd(chunks=len(chunks), chunk=0)
        failed = []
        for ci, ch in enumerate(chunks, 1):
            self.gh.check_cancel()
            self.upd(chunk=ci)
            ok, bad = self._push_chunk(ch, "%s (%d/%d)" % (j["message"], ci, len(chunks)) if len(chunks) > 1 else j["message"], ci, len(chunks))
            failed.extend(bad)
        self.finish(files, failed)

    def finish(self, files, failed):
        gh, full = self.gh, self.full
        self.upd(phase="verify", note="")
        errors = {f["path"]: msg for f, msg in failed}
        bad = []
        for rnd in range(3):
            gh.check_cancel()
            head = gh.get_ref(full, self.branch)
            idx = RemoteIndex(gh, full, gh.get_commit(full, head)["tree"]["sha"])
            bad = [f for f in files if (idx.get(f["path"]) or (None,))[0] != f["sha"]]
            if not bad:
                break
            if rnd == 2:
                break
            self.log("🔁 %d file GitHub par nahi mili/match nahi hui — dobara bhej raha hoon (round %d)" % (len(bad), rnd + 1))
            self.head, self.base_tree, self.remote = head, idx.root, idx
            self.inline_ok = False
            for ch in make_chunks(bad, int(settings.get("chunk_files"))):
                _, more = self._push_chunk(ch, self.job["message"] + " (repair)", 0, 0)
                errors.update({f["path"]: m for f, m in more})
        final_errors = [{"path": f["path"], "error": errors.get(f["path"], "GitHub par verify nahi hui")} for f in bad]
        self.counts(failed=len(final_errors), done=max(0, self.todo_n - len(final_errors)))
        if final_errors:
            self.upd(status="partial", phase="done", errors=final_errors, finished=time.time(), verified=False,
                     error="%d file upload nahi ho payi. Resume dabao ya error dekho." % len(final_errors))
            self.log("⚠ %d file fail hui: %s" % (len(final_errors), ", ".join(e["path"] for e in final_errors[:5])))
        else:
            self.upd(status="done", phase="done", errors=[], finished=time.time(), verified=True, error="", note="")
            self.log("🎉 Poora ho gaya — %d files GitHub par verify ho gayi." % len(files))

    # ---------------------------------------------------------------- head / bootstrap
    def _resolve_head(self, info, files, fresh):
        gh, full, branch = self.gh, self.full, self.branch
        default = info["default_branch"]
        if fresh:  # GitHub needs a few seconds before a new repo answers git calls
            base = gh.get_ref(full, default, retry_404=10, retry_409=10)
            if branch == default:
                return base
        try:
            return gh.get_ref(full, branch)
        except GitHubError as e:
            if e.kind not in ("notfound", "empty"):
                raise
        base = None
        if branch != default:
            try:
                base = gh.get_ref(full, default)
            except GitHubError as e:
                if e.kind not in ("notfound", "empty"):
                    raise
        if base:
            self.log("🌿 Branch '%s' nahi thi — '%s' se bana raha hoon" % (branch, default))
            gh.create_ref(full, branch, base)
            return base
        # empty repository: the Git Data API refuses to work until one commit exists
        self.log("🌱 Repo khaali hai — pehli file se initialize kar raha hoon")
        cand = [f for f in files if re.fullmatch(r"[A-Za-z0-9_./-]+", f["path"]) and f["size"] <= 1_000_000]
        cand.sort(key=lambda f: f["size"])
        if cand:
            path, data = cand[0]["path"], staging.read_bytes(cand[0]["src"])
        else:
            path, data = ".gitkeep", b""
        res = gh.put_contents(full, path, data, "Initial commit via GitZip")
        sha = res["commit"]["sha"]
        if branch != default:
            gh.create_ref(full, branch, sha)
        return sha

    # ---------------------------------------------------------------- one commit
    def _push_chunk(self, chunk, message, ci, total):
        gh, full = self.gh, self.full
        entries, ok_files, failed = [], [], []
        budget = INLINE_BUDGET
        for f in chunk:
            gh.check_cancel()
            ent = {"path": f["path"], "mode": f["mode"], "type": "blob"}
            if self.remote.has_sha(f["sha"]):
                ent["sha"] = f["sha"]               # blob already exists in the repo: just point at it
            else:
                data = staging.read_bytes(f["src"])
                txt = staging.as_text(data) if (self.inline_ok and len(data) <= INLINE_MAX and budget >= len(data)) else None
                if txt is not None:
                    ent["content"] = txt
                    budget -= len(data)
                else:
                    try:
                        ent["sha"] = gh.create_blob(full, data)
                    except GitHubError as e:
                        if e.kind in ("invalid", "error"):
                            failed.append((f, friendly(e)))
                            self.log("❌ %s — %s" % (f["path"], e.message))
                            self.counts(failed=len(failed))
                            continue
                        raise
            entries.append(ent)
            ok_files.append(f)
        if not entries:
            return [], failed

        for attempt in range(6):
            gh.check_cancel()
            try:
                tree = gh.create_tree(full, self.base_tree, entries)
            except GitHubError as e:
                if e.kind == "invalid" and any("content" in en for en in entries):
                    self.log("ℹ Inline text accept nahi hua — blobs se bhej raha hoon")
                    self.inline_ok = False
                    for en in entries:
                        if "content" in en:
                            en["sha"] = gh.create_blob(full, en.pop("content").encode("utf-8"))
                    continue
                raise
            commit = gh.create_commit(full, message, tree, [self.head])
            try:
                gh.update_ref(full, self.branch, commit)
            except GitHubError as e:
                if e.kind in ("invalid", "conflict"):
                    new_head = gh.get_ref(full, self.branch)
                    if new_head != self.head:
                        self.log("ℹ Branch par kisi ne naya commit kiya — uske upar dobara commit kar raha hoon")
                        self.head = new_head
                        self.base_tree = gh.get_commit(full, new_head)["tree"]["sha"]
                        self.remote = RemoteIndex(gh, full, self.base_tree)
                        continue
                raise
            self.head, self.base_tree = commit, tree
            for f in ok_files:
                self.remote.add(f["path"], f["sha"], f["mode"])
            with store.lock:
                cur = store.get(self.jid)
                done = cur["counts"]["done"] + len(ok_files)
                bd = cur["bytes_done"] + sum(f["size"] for f in ok_files)
                commits = cur["commits"] + [commit]
            self.upd(commits=commits[-50:], head=commit, bytes_done=bd, counts={"done": done, "failed": len(failed)}, force=True)
            self.log("✓ commit %s — %d file%s" % (commit[:7], len(ok_files), " (%d/%d)" % (ci, total) if total > 1 else ""))
            return ok_files, failed
        raise GitHubError(0, "Branch baar baar badal rahi hai — thodi der baad Resume karo", "conflict")


# ======================================================================= runner
class Runner:
    def __init__(self):
        self.q = queue.Queue()
        self.cancels = {}
        self.thread = None
        self.current = None
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._loop, name="job-runner", daemon=True)
            self.thread.start()

    def alive(self):
        return bool(self.thread and self.thread.is_alive())

    def enqueue(self, jid):
        self.cancels[jid] = threading.Event()
        store.update(jid, status="queued", phase="", note="", error="", finished=None)
        self.q.put(jid)
        self.start()

    def cancel(self, jid):
        job = store.get(jid)
        if not job or job["status"] not in ACTIVE:
            return False
        ev = self.cancels.get(jid)
        if ev:
            ev.set()
        if job["status"] == "queued":
            store.update(jid, status="cancelled", finished=time.time(), error="Cancel kiya")
        return True

    def resume(self, jid):
        job = store.get(jid)
        if not job or job["status"] in ACTIVE:
            return False
        store.log(jid, "—— Resume (attempt %d) ——" % (job["attempts"] + 1))
        store.update(jid, attempts=job["attempts"] + 1, errors=[], verified=False, counts={"done": 0, "failed": 0, "same": 0, "todo": 0})
        self.enqueue(jid)
        return True

    def resume_pending(self):
        """After a restart: pick up jobs that were queued/running when the process died."""
        n = 0
        for j in sorted(store.all(), key=lambda x: x["created"]):
            if j["status"] in ACTIVE:
                store.log(j["id"], "🔄 Server restart ke baad job dobara shuru (jo commit ho chuka wo skip hoga)")
                self.enqueue(j["id"])
                n += 1
        return n

    def busy_staging(self):
        return {j["staging_id"] for j in store.all() if j["status"] in ACTIVE}

    def _loop(self):
        while True:
            jid = self.q.get()
            job = store.get(jid)
            if not job or job["status"] != "queued":
                continue
            self.current = jid
            try:
                self._run(job)
            except Exception:  # the loop must never die
                store.log(jid, "Runner error:\n" + traceback.format_exc())
            finally:
                self.current = None

    def _run(self, job):
        jid = job["id"]
        ev = self.cancels.setdefault(jid, threading.Event())
        store.update(jid, status="running", started=time.time(), finished=None, phase="connect", error="", note="")
        store.log(jid, "▶ Job shuru: %s → %s" % (job.get("staging_name"), job["repo"]))
        try:
            UploadTask(job, ev).run()
        except Cancelled:
            store.update(jid, status="cancelled", phase="", note="", finished=time.time(), error="Cancel kiya gaya")
            store.log(jid, "⏹ Cancel kiya gaya (jo commit ho chuka wo GitHub par safe hai)")
        except UserError as e:
            store.update(jid, status="failed", phase="", note="", finished=time.time(), error=str(e))
            store.log(jid, "❌ " + str(e))
        except GitHubError as e:
            store.update(jid, status="failed", phase="", note="", finished=time.time(), error=friendly(e))
            store.log(jid, "❌ " + friendly(e))
        except Exception as e:
            store.log(jid, "💥 Internal error:\n" + traceback.format_exc())
            store.update(jid, status="failed", phase="", note="", finished=time.time(), error="Internal error: %s" % e)
        finally:
            self.cancels.pop(jid, None)


runner = Runner()


# ======================================================================= repo maintenance
def delete_paths(repo, branch, paths, message="Delete via GitZip"):
    """Delete files/folders from a repo in ONE commit (no per-file API calls)."""
    gh = GH(_token())
    full = full_name(gh, repo)
    info = gh.get_repo(full)
    branch = branch or info["default_branch"]
    head = gh.get_ref(full, branch)
    tree = gh.get_commit(full, head)["tree"]["sha"]
    idx = RemoteIndex(gh, full, tree)
    if idx.lazy:
        raise UserError("Repo bahut bada hai — yahan se delete nahi ho sakta.")
    wanted = [p.strip("/") for p in paths if p.strip("/")]
    ents = []
    for path in idx.map:
        if any(path == w or path.startswith(w + "/") for w in wanted):
            ents.append({"path": path, "mode": idx.map[path][1], "type": "blob", "sha": None})
    if not ents:
        raise UserError("Delete karne ke liye kuch mila nahi")
    t = gh.create_tree(full, tree, ents)
    c = gh.create_commit(full, message, t, [head])
    gh.update_ref(full, branch, c)
    return {"deleted": len(ents), "commit": c}
