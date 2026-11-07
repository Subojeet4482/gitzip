"""Tiny in-memory fakes of the GitHub Git Data API and the Inception chat API, for offline tests.

The GitHub fake mimics the behaviours that matter for the uploader: nested trees, empty repos
(409), 404 right after creating a repo, `.git` path rejection, non-fast-forward ref updates,
secondary rate limits and flaky 502s. Failures are injected through POST /__mock/rules.
"""
import base64
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class GitHubState:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        self.repos = {}
        self.rules = []
        self.stats = {}
        self.truncate_over = 0
        self.mangle_inline = False
        self.user = "tester"

    # ---- objects (global store keyed by sha, fine for a fake)
    def new_repo(self, full, private=True, init=True):
        r = {"full": full, "private": private, "blobs": {}, "trees": {}, "commits": {}, "refs": {}, "default": "main",
             "not_ready": 0, "archived": False, "push": True}
        self.repos[full] = r
        if init:
            b = b"# %s\n" % full.encode()
            r["blobs"][blob_sha(b)] = b
            t = self.build(r, {"README.md": (blob_sha(b), "100644")})
            c = self.commit(r, t, [], "Initial commit")
            r["refs"]["main"] = c
            r["not_ready"] = 2
        return r

    def build(self, r, flat):
        def mk(prefix):
            ents = {}
            for path, (sha, mode) in flat.items():
                if not path.startswith(prefix):
                    continue
                rest = path[len(prefix):]
                if "/" in rest:
                    d = rest.split("/")[0]
                    ents[d] = ("tree", None, "040000")
                else:
                    ents[rest] = ("blob", sha, mode)
            for d, e in list(ents.items()):
                if e[0] == "tree":
                    ents[d] = ("tree", mk(prefix + d + "/"), "040000")
            body = json.dumps(sorted(ents.items()))
            sha = hashlib.sha1(("tree " + body).encode()).hexdigest()
            r["trees"][sha] = ents
            return sha
        return mk("")

    def flat(self, r, tree_sha, prefix=""):
        out = {}
        for name, (typ, sha, mode) in r["trees"][tree_sha].items():
            if typ == "blob":
                out[prefix + name] = (sha, mode)
            else:
                out.update(self.flat(r, sha, prefix + name + "/"))
        return out

    def commit(self, r, tree, parents, msg):
        sha = hashlib.sha1(json.dumps([tree, parents, msg, len(r["commits"])]).encode()).hexdigest()
        r["commits"][sha] = {"tree": tree, "parents": parents, "message": msg}
        return sha

    def is_ancestor(self, r, anc, tip):
        seen, stack = set(), [tip]
        while stack:
            c = stack.pop()
            if c == anc:
                return True
            if c in seen or c not in r["commits"]:
                continue
            seen.add(c)
            stack.extend(r["commits"][c]["parents"])
        return False

    def files(self, full, branch="main"):
        r = self.repos[full]
        return {p: r["blobs"][s] for p, (s, m) in self.flat(r, r["commits"][r["refs"][branch]]["tree"]).items()}


G = GitHubState()


class GitHubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, status, obj=None, headers=None):
        body = json.dumps(obj if obj is not None else {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def err(self, status, msg, headers=None):
        self.reply(status, {"message": msg}, headers)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def inject(self, key):
        with G.lock:
            for rule in G.rules:
                if rule["match"] in key and rule.get("times", 1) > 0 and rule.get("method", self.command) == self.command:
                    rule["times"] = rule.get("times", 1) - 1
                    ep = rule.get("external_push")
                    if ep:  # somebody else pushes right before our ref update
                        r = G.repos[ep["full"]]
                        b = ep["content"].encode()
                        sha = blob_sha(b)
                        r["blobs"][sha] = b
                        head = r["refs"]["main"]
                        flat = G.flat(r, r["commits"][head]["tree"])
                        flat[ep["path"]] = (sha, "100644")
                        r["refs"]["main"] = G.commit(r, G.build(r, flat), [head], "external")
                    return rule
        return None

    def handle_any(self):
        path = self.path.split("?")[0]
        key = self.command + " " + path
        with G.lock:
            G.stats[path.split("/")[-1] if "git/" in path else path] = G.stats.get(path.split("/")[-1] if "git/" in path else path, 0) + 1
            G.stats["__total__"] = G.stats.get("__total__", 0) + 1
        if path.startswith("/__mock/"):
            return self.control(path)
        if not (self.headers.get("Authorization") or "").startswith("Bearer good"):
            return self.err(401, "Bad credentials")
        rule = self.inject(key)
        if rule:
            hdrs = {"retry-after": str(rule["retry_after"])} if rule.get("retry_after") else {}
            return self.err(rule["status"], rule.get("message", "injected"), hdrs)
        body = self.body() if self.command in ("POST", "PUT", "PATCH") else {}
        m = re.fullmatch(r"/user", path)
        if m:
            return self.reply(200, {"login": G.user}, {"x-oauth-scopes": "repo"})
        if path == "/user/repos":
            if self.command == "GET":
                return self.reply(200, [self.repo_json(r) for r in G.repos.values()])
            name = body["name"]
            full = "%s/%s" % (G.user, name)
            if full in G.repos:
                return self.err(422, "name already exists on this account")
            r = G.new_repo(full, body.get("private", True), body.get("auto_init", False))
            return self.reply(201, self.repo_json(r))
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)", path)
        if m:
            r = G.repos.get(m.group(1))
            return self.reply(200, self.repo_json(r)) if r else self.err(404, "Not Found")
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/(git/.*|contents/.*)", path)
        if not m:
            return self.err(404, "Not Found")
        r = G.repos.get(m.group(1))
        if not r:
            return self.err(404, "Not Found")
        if not r["push"] and self.command != "GET":
            return self.err(403, "Resource not accessible by personal access token")
        return self.git(r, m.group(2), body)

    def repo_json(self, r):
        return {"full_name": r["full"], "name": r["full"].split("/")[1], "owner": {"login": r["full"].split("/")[0]},
                "private": r["private"], "default_branch": r["default"], "archived": r["archived"],
                "permissions": {"push": r["push"]}, "html_url": "https://github.com/" + r["full"],
                "size": 0 if not r["refs"] else 1}

    def git(self, r, sub, body):
        c = self.command
        if sub.startswith("contents/"):
            fp = sub[len("contents/"):]
            if c != "PUT":
                return self.err(404, "Not Found")
            data = base64.b64decode(body["content"])
            if r["refs"]:
                return self.err(422, "sha wasn't supplied")
            sha = blob_sha(data)
            r["blobs"][sha] = data
            t = G.build(r, {fp: (sha, "100644")})
            cm = G.commit(r, t, [], body["message"])
            r["refs"][r["default"]] = cm
            return self.reply(201, {"commit": {"sha": cm}, "content": {"path": fp}})
        m = re.fullmatch(r"git/ref/heads/(.+)", sub)
        if m:
            if r["not_ready"] > 0:
                r["not_ready"] -= 1
                return self.err(404, "Not Found")
            if not r["refs"]:
                return self.err(409, "Git Repository is empty.")
            sha = r["refs"].get(m.group(1))
            return self.reply(200, {"object": {"sha": sha}}) if sha else self.err(404, "Not Found")
        if sub == "git/refs" and c == "POST":
            ref = body["ref"].replace("refs/heads/", "")
            if body["sha"] not in r["commits"]:
                return self.err(422, "Object does not exist")
            r["refs"][ref] = body["sha"]
            return self.reply(201, {"ref": body["ref"]})
        m = re.fullmatch(r"git/refs/heads/(.+)", sub)
        if m and c == "PATCH":
            cur = r["refs"].get(m.group(1))
            if cur is None:
                return self.err(422, "Reference does not exist")
            if not G.is_ancestor(r, cur, body["sha"]):
                return self.err(422, "Update is not a fast forward")
            r["refs"][m.group(1)] = body["sha"]
            return self.reply(200, {"object": {"sha": body["sha"]}})
        m = re.fullmatch(r"git/commits/([0-9a-f]{40})", sub)
        if m and c == "GET":
            cm = r["commits"].get(m.group(1))
            return self.reply(200, {"sha": m.group(1), "tree": {"sha": cm["tree"]}}) if cm else self.err(404, "Not Found")
        if sub == "git/commits" and c == "POST":
            if body["tree"] not in r["trees"]:
                return self.err(422, "Tree does not exist")
            return self.reply(201, {"sha": G.commit(r, body["tree"], body["parents"], body["message"])})
        if sub == "git/blobs" and c == "POST":
            data = base64.b64decode(body["content"])
            sha = blob_sha(data)
            r["blobs"][sha] = data
            return self.reply(201, {"sha": sha})
        if sub == "git/trees" and c == "POST":
            flat = G.flat(r, body["base_tree"]) if body.get("base_tree") else {}
            for e in body["tree"]:
                p = e["path"]
                if any(seg.lower() == ".git" or seg in ("", "..") for seg in p.split("/")):
                    return self.err(422, "tree.path contains a malformed path component")
                if e.get("sha") is None and "content" not in e:
                    flat.pop(p, None)
                    continue
                if "content" in e:
                    data = e["content"].encode("utf-8")
                    if G.mangle_inline:
                        data = data.replace(b"\r\n", b"\n")
                    sha = blob_sha(data)
                    r["blobs"][sha] = data
                else:
                    sha = e["sha"]
                    if sha not in r["blobs"]:
                        return self.err(422, "tree.sha %s is not a valid blob" % sha)
                flat[p] = (sha, e.get("mode", "100644"))
            return self.reply(201, {"sha": G.build(r, flat)})
        m = re.fullmatch(r"git/trees/([0-9a-f]{40})", sub)
        if m and c == "GET":
            t = r["trees"].get(m.group(1))
            if t is None:
                return self.err(404, "Not Found")
            if "recursive=1" in self.path:
                flat = G.flat(r, m.group(1))
                items = [{"path": p, "mode": mo, "type": "blob", "sha": s, "size": len(r["blobs"][s])} for p, (s, mo) in sorted(flat.items())]
                trunc = False
                if G.truncate_over and len(items) > G.truncate_over:
                    items, trunc = items[:G.truncate_over], True
                return self.reply(200, {"sha": m.group(1), "tree": items, "truncated": trunc})
            items = []
            for name, (typ, sha, mo) in sorted(t.items()):
                it = {"path": name, "mode": mo, "type": typ, "sha": sha}
                if typ == "blob":
                    it["size"] = len(r["blobs"][sha])
                items.append(it)
            return self.reply(200, {"sha": m.group(1), "tree": items, "truncated": False})
        return self.err(404, "Not Found")

    def control(self, path):
        body = self.body() if self.command == "POST" else {}
        with G.lock:
            if path == "/__mock/reset":
                G.reset()
            elif path == "/__mock/rules":
                G.rules = body.get("rules", [])
                G.truncate_over = body.get("truncate_over", 0)
                G.mangle_inline = body.get("mangle_inline", False)
            elif path == "/__mock/repo":
                r = G.new_repo(body["full"], True, body.get("init", True))
                r["not_ready"] = body.get("not_ready", 0)
                r["push"] = body.get("push", True)
                r["refs"] = r["refs"] if body.get("init", True) else {}
            elif path == "/__mock/stats":
                return self.reply(200, dict(G.stats))
            elif path == "/__mock/clearstats":
                G.stats = {}
            elif path == "/__mock/files":
                try:
                    return self.reply(200, {k: v.decode("utf-8", "replace") for k, v in G.files(body["full"], body.get("branch", "main")).items()})
                except KeyError:
                    return self.reply(404, {})
            elif path == "/__mock/push":  # someone else pushes a commit to the branch
                r = G.repos[body["full"]]
                b = body["content"].encode()
                sha = blob_sha(b)
                r["blobs"][sha] = b
                head = r["refs"][body.get("branch", "main")]
                flat = G.flat(r, r["commits"][head]["tree"])
                flat[body["path"]] = (sha, "100644")
                t = G.build(r, flat)
                r["refs"][body.get("branch", "main")] = G.commit(r, t, [head], "external")
        return self.reply(200, {"ok": True})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any


# ------------------------------------------------------------------ fake Inception
class AIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls = []

    def log_message(self, *a):
        pass

    def reply(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer inc-good":
            return self.reply(401, {"error": {"message": "invalid api key"}})
        self.reply(200, {"data": [{"id": "mercury-3"}, {"id": "mercury-2.5"}, {"id": "mercury-2"}]})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n))
        AIHandler.calls.append(req)
        if self.headers.get("Authorization") != "Bearer inc-good":
            return self.reply(401, {"error": {"message": "invalid api key"}})
        msgs = req["messages"]
        last = msgs[-1]
        if last["role"] == "tool":
            prev = [m for m in msgs if m.get("tool_calls")][-1]["tool_calls"][-1]["function"]["name"]
            res = json.loads(last["content"])
            if prev == "list_staging" and res.get("staging"):
                sid = res["staging"][0]["id"]
                args = {"staging_id": sid, "repo": "tester/ai-demo", "create_repo": True, "private": True}
                return self.reply(200, {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_2", "type": "function", "function": {"name": "start_upload", "arguments": json.dumps(args)}}]}}]})
            return self.reply(200, {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Done: " + last["content"][:80]}}]})
        text = last["content"] if isinstance(last["content"], str) else ""
        if "upload" in text.lower():
            return self.reply(200, {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "list_staging", "arguments": "{}"}}]}}]})
        self.reply(200, {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Namaste! Main madad ke liye taiyaar hoon."}}]})


def serve(handler, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    import sys
    import time
    serve(GitHubHandler, int(sys.argv[1]) if len(sys.argv) > 1 else 9101)
    serve(AIHandler, int(sys.argv[2]) if len(sys.argv) > 2 else 9102)
    print("mock github :9101  mock ai :9102", flush=True)
    while True:
        time.sleep(3600)
