"""GitHub REST client built for flaky networks and rate limits.

Every call goes through GH.request(), which retries network errors / 5xx with back-off,
sleeps through GitHub rate limits (primary and secondary) and turns everything else into
a GitHubError with a plain explanation.
"""
import base64
import http.client
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from . import VERSION
from .config import GITHUB_API, PACE


class Cancelled(Exception):
    pass


class UserError(Exception):
    """A problem the user can fix (shown as-is in the UI)."""


class GitHubError(Exception):
    def __init__(self, status, message, kind="error", headers=None):
        super().__init__(message)
        self.status, self.message, self.kind, self.headers = status, message, kind, headers or {}

    def __str__(self):
        return "GitHub [%s]: %s" % (self.status, self.message) if self.status else self.message


def friendly(e):
    """Short, human explanation for the most common failures (Hinglish where it helps)."""
    if not isinstance(e, GitHubError):
        return str(e)
    k, m = e.kind, e.message
    if k == "auth":
        return "GitHub token galat ya expire ho gaya hai. Settings me naya token daalo. (%s)" % m
    if k == "forbidden":
        return ("GitHub ne permission nahi di. Fine-grained token me is repo ko select karo aur "
                "'Contents: Read and write' do (classic token me 'repo' tick karo). (%s)" % m)
    if k == "notfound":
        return ("GitHub ne 404 diya: repo/branch exist nahi karta, ya token ko is repo ka access nahi hai. "
                "Token me repo select karo aur Contents: Read and write permission do. (%s)" % m)
    if k == "rate":
        return "GitHub rate limit lag gayi. Thodi der baad Resume dabao. (%s)" % m
    if k == "network":
        return "Network problem: %s" % m
    return str(e)


def _msg(raw):
    try:
        j = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(j, dict):
            return str(j.get("message") or j.get("error") or raw[:200])
    except Exception:
        pass
    return raw[:200].decode("utf-8", "replace") if raw else "request failed"


def enc(path):
    return quote(path, safe="/")


class GH:
    def __init__(self, token, api=None, log=None, cancel=None, on_wait=None, pace=None):
        self.token = token or ""
        self.api = (api or GITHUB_API).rstrip("/")
        self.log = log or (lambda s: None)
        self._cancel = cancel or (lambda: False)
        self.on_wait = on_wait or (lambda s: None)
        self.pace = PACE if pace is None else pace
        self._last_content = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------ plumbing
    def check_cancel(self):
        if self._cancel():
            raise Cancelled()

    def sleep(self, seconds, why):
        seconds = max(0.0, float(seconds))
        if seconds >= 3:
            self.log("⏳ %s — %d sec wait" % (why, seconds))
            self.on_wait("%s (%d sec)" % (why, seconds))
        end = time.time() + seconds
        while time.time() < end:
            self.check_cancel()
            time.sleep(min(0.5, max(0.0, end - time.time())))
        self.on_wait("")

    def _pace(self):
        if self.pace <= 0:
            return
        with self._lock:
            gap = self._last_content + self.pace - time.time()
            if gap > 0:
                time.sleep(gap)
            self._last_content = time.time()

    def _headers(self, has_body):
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "GitZip-Bot/" + VERSION,
        }
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        if has_body:
            h["Content-Type"] = "application/json"
        return h

    def request(self, method, path, body=None, *, timeout=60, retry_404=0, retry_409=0, content=False):
        """Returns (status, json, headers). Raises GitHubError / Cancelled."""
        if not self.token:
            raise GitHubError(401, "GitHub token set nahi hai (Settings me daalo)", "auth")
        url = path if path.startswith("http") else self.api + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        net = srv = rl = 0
        while True:
            self.check_cancel()
            if content:
                self._pace()
            req = urllib.request.Request(url, data=data, method=method, headers=self._headers(data is not None))
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw, status, hdrs = r.read(), r.status, r.headers
                try:
                    return status, (json.loads(raw) if raw else None), hdrs
                except ValueError:
                    raise http.client.HTTPException("GitHub ne JSON nahi diya")
            except urllib.error.HTTPError as e:
                raw, status, hdrs = e.read(), e.code, e.headers
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                    http.client.HTTPException, ssl.SSLError, OSError) as e:
                net += 1
                if net > 6:
                    raise GitHubError(0, "%s" % (getattr(e, "reason", None) or e), "network")
                self.sleep(min(2 ** net, 30), "Network/GitHub reply problem (%s), retry %d" % (getattr(e, "reason", None) or e, net))
                continue

            msg = _msg(raw)
            low = msg.lower()
            if status in (500, 502, 503, 504):
                srv += 1
                if srv > 6:
                    raise GitHubError(status, msg, "server", hdrs)
                self.sleep(min(2 ** srv, 30), "GitHub server error %d, retry %d" % (status, srv))
                continue
            if status in (403, 429):
                wait = self._rate_wait(status, hdrs, low, rl)
                if wait is not None:
                    rl += 1
                    if rl > 12 or wait > 3900:
                        raise GitHubError(status, msg, "rate", hdrs)
                    self.sleep(wait, "GitHub rate limit")
                    continue
                raise GitHubError(status, msg, "forbidden", hdrs)
            if status == 401:
                raise GitHubError(status, msg, "auth", hdrs)
            if status == 404:
                if retry_404 > 0:
                    retry_404 -= 1
                    self.sleep(2, "GitHub abhi ready nahi (404), retry")
                    continue
                raise GitHubError(status, msg, "notfound", hdrs)
            if status == 409:
                if retry_409 > 0:
                    retry_409 -= 1
                    self.sleep(2, "GitHub abhi ready nahi (409), retry")
                    continue
                raise GitHubError(status, msg, "empty" if "empty" in low else "conflict", hdrs)
            if status == 422:
                raise GitHubError(status, msg, "invalid", hdrs)
            raise GitHubError(status, msg, "error", hdrs)

    @staticmethod
    def _rate_wait(status, hdrs, low, n):
        ra = hdrs.get("retry-after")
        if ra and str(ra).strip().isdigit():
            return int(ra) + 1
        if hdrs.get("x-ratelimit-remaining") == "0" and hdrs.get("x-ratelimit-reset", "").isdigit():
            return max(1, int(hdrs["x-ratelimit-reset"]) - int(time.time()) + 2)
        if "rate limit" in low or "abuse" in low or "secondary" in low or status == 429:
            return 60 * min(2 ** n, 8)
        return None

    # ------------------------------------------------------------ high level calls
    def me(self):
        _, j, h = self.request("GET", "/user")
        return j, h

    def list_repos(self, max_pages=10):
        out = []
        for page in range(1, max_pages + 1):
            _, j, _ = self.request(
                "GET", "/user/repos?per_page=100&sort=updated&affiliation=owner,collaborator,organization_member&page=%d" % page)
            out.extend(j or [])
            if not j or len(j) < 100:
                break
        return out

    def get_repo(self, full):
        return self.request("GET", "/repos/" + full)[1]

    def create_repo(self, full, private=True, description="", me_login=None):
        owner, _, name = full.rpartition("/")
        body = {"name": name, "private": bool(private), "auto_init": True}
        if description:
            body["description"] = description[:300]
        path = "/orgs/%s/repos" % owner if owner and me_login and owner.lower() != me_login.lower() else "/user/repos"
        return self.request("POST", path, body)[1]

    def get_ref(self, full, branch, **kw):
        j = self.request("GET", "/repos/%s/git/ref/heads/%s" % (full, enc(branch)), **kw)[1]
        return j["object"]["sha"]

    def get_commit(self, full, sha):
        return self.request("GET", "/repos/%s/git/commits/%s" % (full, sha))[1]

    def get_tree(self, full, sha, recursive=False):
        return self.request("GET", "/repos/%s/git/trees/%s%s" % (full, sha, "?recursive=1" if recursive else ""), timeout=120)[1]

    def create_blob(self, full, data):
        body = {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"}
        return self.request("POST", "/repos/%s/git/blobs" % full, body, timeout=180, content=True)[1]["sha"]

    def create_tree(self, full, base_tree, entries):
        body = {"tree": entries}
        if base_tree:
            body["base_tree"] = base_tree
        return self.request("POST", "/repos/%s/git/trees" % full, body, timeout=180, content=True)[1]["sha"]

    def create_commit(self, full, message, tree, parents):
        body = {"message": message, "tree": tree, "parents": parents}
        return self.request("POST", "/repos/%s/git/commits" % full, body, content=True)[1]["sha"]

    def update_ref(self, full, branch, sha):
        return self.request("PATCH", "/repos/%s/git/refs/heads/%s" % (full, enc(branch)), {"sha": sha, "force": False})[1]

    def create_ref(self, full, branch, sha):
        return self.request("POST", "/repos/%s/git/refs" % full, {"ref": "refs/heads/" + branch, "sha": sha})[1]

    def put_contents(self, full, path, data, message):
        """Contents API — only used to create the very first commit of an empty repo."""
        body = {"message": message, "content": base64.b64encode(data).decode("ascii")}
        return self.request("PUT", "/repos/%s/contents/%s" % (full, enc(path)), body, timeout=120, content=True)[1]


class RemoteIndex:
    """path -> (sha, mode) for blobs of one tree.

    One recursive call normally; if GitHub says the listing is truncated (huge repo) it
    falls back to walking only the directories we actually ask about.
    """

    def __init__(self, gh, full, tree_sha):
        self.gh, self.full, self.root = gh, full, tree_sha
        self.map, self.shas, self.over = {}, set(), {}
        self.dirs = {}
        data = gh.get_tree(full, tree_sha, recursive=True)
        self.lazy = bool(data.get("truncated"))
        if self.lazy:
            gh.log("ℹ Repo bahut bada hai — folder-by-folder check karunga")
        else:
            for t in data.get("tree", []):
                if t.get("type") == "blob":
                    self.map[t["path"]] = (t["sha"], t.get("mode", "100644"))
            self.shas = {v[0] for v in self.map.values()}

    def _dir(self, dirpath):
        if dirpath in self.dirs:
            return self.dirs[dirpath]
        if dirpath == "":
            sha = self.root
        else:
            parent, _, name = dirpath.rpartition("/")
            e = self._dir(parent).get(name)
            sha = e[1] if e and e[0] == "tree" else None
        entries = {}
        if sha:
            for t in self.gh.get_tree(self.full, sha).get("tree", []):
                entries[t["path"]] = (t["type"], t["sha"], t.get("mode", "100644"))
        self.dirs[dirpath] = entries
        return entries

    def get(self, path):
        if path in self.over:
            return self.over[path]
        if not self.lazy:
            return self.map.get(path)
        d, _, name = path.rpartition("/")
        e = self._dir(d).get(name)
        return (e[1], e[2]) if e and e[0] == "blob" else None

    def has_sha(self, sha):
        return (not self.lazy and sha in self.shas) or any(v[0] == sha for v in self.over.values())

    def add(self, path, sha, mode):
        self.over[path] = (sha, mode)


def list_dir(gh, full, branch, path=""):
    """Immediate children of a repo folder, walking non-recursive trees (no URL-path pitfalls)."""
    head = gh.get_ref(full, branch)
    sha = gh.get_commit(full, head)["tree"]["sha"]
    for seg in [s for s in path.strip("/").split("/") if s]:
        nxt = None
        for t in gh.get_tree(full, sha).get("tree", []):
            if t["path"] == seg and t["type"] == "tree":
                nxt = t["sha"]
                break
        if not nxt:
            raise GitHubError(404, "Folder nahi mila: " + path, "notfound")
        sha = nxt
    out = []
    for t in gh.get_tree(full, sha).get("tree", []):
        out.append({"name": t["path"], "type": "dir" if t["type"] == "tree" else "file", "size": t.get("size", 0)})
    out.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return out
