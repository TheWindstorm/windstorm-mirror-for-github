#!/usr/bin/env python3
"""A stand-in for GitHub, for tests only. Standard library plus `git http-backend`.

Serves, on 127.0.0.1 and a free port:
- the REST calls the skill makes: GET /user, /user/repos, /orgs/<org>/repos, /repos/<owner>/<name>
  (paged with a Link header, 100 at most per page), with the headers the skill reads:
  X-RateLimit-Remaining, X-OAuth-Scopes (classic keys), github-authentication-token-expiration;
- git over smart HTTP at /<owner>/<name>.git, through `git http-backend`, with Basic auth
  (any user name, the key as password), as GitHub does.

Everything it does is recorded in `self.log`, so a test can prove what the skill never did (no push, no
writing call). What a real GitHub does that this does not: everything else. A codon whose proof leans on this
file is seen against the real GitHub before its version is tagged (PROMISES.json, how_to_use).
"""
import base64
import hashlib
import json
import shutil
import tempfile
import os
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeGitHub:
    def __init__(self, root, key="good-key", scopes=None, expires=None):
        self.root = root                      # bare repositories live at <root>/<owner>/<name>.git
        self.key = key
        self.scopes = scopes                  # None = a fine-grained key (no X-OAuth-Scopes header)
        self.expires = expires                # e.g. "2026-12-01 00:00:00 UTC"
        self.repos = {}                       # full_name -> dict as GitHub's API shows it
        self.next_id = 1000
        self.rate_remaining = 5000
        self.fail_page = None                 # (path, page) that answers 502
        self.slow = {}                        # full_name -> seconds to stall a git request
        self.lfs_uploads = []                 # LFS batch calls that asked to upload (a batch call to download is a read)
        self.answers = {}                     # path -> (code, body) answered instead, after the key is checked
        self.contents = {}                    # path in the skill's repository -> bytes, served with no key
        self.release = None                   # what /repos/<o>/<n>/releases/latest answers (no key needed)
        self.release_calls = []               # for each such call: did it carry a key?
        self.page_cap = 100                   # GitHub's own cap; tests lower it to see paging with few repositories
        self.log = []                         # (method, path) of every request
        self.login = "someone"                # the account the key belongs to
        self.orgs = set()                     # organisations this key can see
        self.lfs = {}                         # oid -> bytes, served through the LFS batch API
        self.lock = threading.Lock()
        self.server = None

    # ---- the world on "GitHub" ----
    def add_repo(self, owner, name, private=True, archived=False, fork=False, owner_type="User"):
        if owner_type == "Organization":
            self.orgs.add(owner)
        path = os.path.join(self.root, owner, name + ".git")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", path], check=True)
        subprocess.run(["git", "-C", path, "config", "http.receivepack", "false"], check=True)
        self.next_id += 1
        full = f"{owner}/{name}"
        self.repos[full] = {"id": self.next_id, "name": name, "full_name": full, "private": private,
                            "archived": archived, "fork": fork, "size": 1, "default_branch": "main",
                            "owner": {"login": owner, "type": owner_type},
                            "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        return path

    def commit(self, full, files, branch="main", message="a commit"):
        """Make a commit on 'GitHub' with the given files {path: bytes}; returns its sha."""
        owner, name = full.split("/")
        bare = os.path.join(self.root, owner, name + ".git")
        work = tempfile.mkdtemp(prefix="fgw-")
        try:
            plain = ["-c", "filter.lfs.clean=", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.process=",
                     "-c", "filter.lfs.required=false", "-c", "core.hooksPath=/dev/null"]

            def run(*a):
                a = list(a)
                if a[0] == "git":
                    a[1:1] = plain
                return subprocess.run(a, check=True, capture_output=True, text=True)
            run("git", "init", "-q", "-b", branch, work)
            has = subprocess.run(["git", "--git-dir", bare, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                                 capture_output=True, text=True).stdout.strip()
            if has:
                run("git", "-C", work, "fetch", "-q", bare, f"{branch}:refs/remotes/o/{branch}")
                run("git", "-C", work, "reset", "-q", "--soft", f"refs/remotes/o/{branch}")
                run("git", "-C", work, "checkout", "-q", f"refs/remotes/o/{branch}", "--", ".")
            for path, data in files.items():
                full_path = os.path.join(work, path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "wb") as f:
                    f.write(data)
            run("git", "-C", work, "add", "-A")
            run("git", "-C", work, "-c", "user.email=a@example.invalid", "-c", "user.name=a", "commit", "-q",
                "--allow-empty", "-m", message)
            run("git", "-C", work, "push", "-q", "-f", bare, f"HEAD:refs/heads/{branch}")
            sha = run("git", "-C", work, "rev-parse", "HEAD").stdout.strip()
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self.touch(full)
        return sha

    def lfs_file(self, data):
        """An LFS pointer for data, with the object served by this stand-in."""
        oid = hashlib.sha256(data).hexdigest()
        self.lfs[oid] = data
        return (f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(data)}\n").encode()

    def git(self, full, *args):
        owner, name = full.split("/")
        bare = os.path.join(self.root, owner, name + ".git")
        out = subprocess.run(["git", "--git-dir", bare, *args], check=True, capture_output=True, text=True)
        self.touch(full)
        return out.stdout

    def touch(self, full):
        if full in self.repos:
            self.repos[full]["pushed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def rename(self, full, new_name):
        r = self.repos.pop(full)
        owner = r["owner"]["login"]
        os.rename(os.path.join(self.root, owner, r["name"] + ".git"), os.path.join(self.root, owner, new_name + ".git"))
        r["name"], r["full_name"] = new_name, f"{owner}/{new_name}"
        self.repos[r["full_name"]] = r

    def delete(self, full):
        r = self.repos.pop(full)
        shutil.rmtree(os.path.join(self.root, r["owner"]["login"], r["name"] + ".git"), ignore_errors=True)

    # ---- serving ----
    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def authed(self):
                h = self.headers.get("Authorization", "")
                if h.startswith(("token ", "Bearer ")):
                    return h.split(" ", 1)[1] == fake.key
                if h.startswith("Basic "):
                    _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                    return pw == fake.key
                return False

            def send_json(self, code, body, extra=None):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-RateLimit-Remaining", str(fake.rate_remaining))
                if fake.scopes is not None:
                    self.send_header("X-OAuth-Scopes", ", ".join(fake.scopes))
                if fake.expires:
                    self.send_header("github-authentication-token-expiration", fake.expires)
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.route("GET")

            def do_POST(self):
                self.route("POST")

            def do_PATCH(self):
                self.route("PATCH")

            def do_DELETE(self):
                self.route("DELETE")

            def do_PUT(self):
                self.route("PUT")

            def route(self, method):
                u = urllib.parse.urlsplit(self.path)
                with fake.lock:
                    fake.log.append((method, u.path + ("?" + u.query if u.query else "")))
                if "/info/lfs/" in u.path or u.path.startswith("/lfs-objects/"):
                    return self.lfs_api(method, u)
                if ".git/" in u.path or u.path.endswith(".git"):
                    return self.git(method, u)
                if method == "GET" and "/contents/" in u.path:
                    data = fake.contents.get(u.path.split("/contents/", 1)[1])
                    with fake.lock:
                        fake.release_calls.append(bool(self.headers.get("Authorization")))
                    if data is None:
                        return self.send_json(404, {"message": "Not Found"})
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if method == "GET" and u.path.endswith("/releases/latest"):
                    with fake.lock:
                        fake.release_calls.append(bool(self.headers.get("Authorization")))
                    return self.send_json(200, fake.release) if fake.release else self.send_json(404, {"message": "Not Found"})
                if not self.authed():
                    return self.send_json(401, {"message": "Bad credentials"})
                if method != "GET":
                    return self.send_json(403, {"message": "the stand-in refuses every writing call"})
                with fake.lock:
                    fake.rate_remaining -= 1
                if u.path in fake.answers:                # a set answer for one path, e.g. a key still waiting
                    return self.send_json(*fake.answers[u.path])
                q = urllib.parse.parse_qs(u.query)
                page, per = int(q.get("page", ["1"])[0]), min(int(q.get("per_page", ["30"])[0]), fake.page_cap)
                if fake.fail_page == (u.path, page):
                    return self.send_json(502, {"message": "stand-in: this page fails"})
                parts = u.path.strip("/").split("/")
                if parts == ["user"]:
                    return self.send_json(200, {"login": fake.login, "id": 1, "type": "User"})
                if len(parts) == 2 and parts[0] == "orgs":
                    if parts[1] in fake.orgs:
                        return self.send_json(200, {"login": parts[1], "id": 2, "type": "Organization"})
                    return self.send_json(404, {"message": "Not Found"})
                if parts == ["user", "repos"]:
                    items = [r for r in fake.repos.values() if r["owner"]["type"] == "User"]
                elif len(parts) == 3 and parts[0] == "orgs" and parts[2] == "repos":
                    items = [r for r in fake.repos.values() if r["owner"]["login"] == parts[1]]
                elif len(parts) == 3 and parts[0] == "repos":
                    r = fake.repos.get(f"{parts[1]}/{parts[2]}")
                    return self.send_json(200, r) if r else self.send_json(404, {"message": "Not Found"})
                else:
                    return self.send_json(404, {"message": "Not Found"})
                items = sorted(items, key=lambda r: r["id"])
                chunk = items[(page - 1) * per: page * per]
                extra = {}
                if page * per < len(items):
                    nq = dict(q, page=[str(page + 1)])
                    nxt = f"http://{self.headers['Host']}{u.path}?{urllib.parse.urlencode(nq, doseq=True)}"
                    extra["Link"] = f'<{nxt}>; rel="next"'
                return self.send_json(200, chunk, extra)

            def lfs_api(self, method, u):
                if not self.authed():
                    return self.send_json(401, {"message": "Bad credentials"})
                if u.path.startswith("/lfs-objects/"):
                    data = fake.lfs.get(u.path.rsplit("/", 1)[1])
                    if data is None:
                        return self.send_json(404, {"message": "no such object"})
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if req.get("operation") != "download":
                    with fake.lock:
                        fake.lfs_uploads.append(u.path)
                    return self.send_json(403, {"message": "the stand-in refuses uploads"})
                objs = []
                for o in req.get("objects", []):
                    if o["oid"] in fake.lfs:
                        objs.append({"oid": o["oid"], "size": o["size"], "authenticated": True, "actions": {"download": {
                            "href": f"http://{self.headers['Host']}/lfs-objects/{o['oid']}",
                            "header": {"Authorization": self.headers.get("Authorization", "")}}}})
                    else:
                        objs.append({"oid": o["oid"], "size": o["size"], "error": {"code": 404, "message": "not found"}})
                data = json.dumps({"transfer": "basic", "objects": objs}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.git-lfs+json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def git(self, method, u):
                if not self.authed():
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="GitHub"')
                    self.end_headers()
                    return
                full = u.path.strip("/").split(".git")[0]
                if full in fake.slow:
                    time.sleep(fake.slow[full])
                if "git-receive-pack" in u.path or "service=git-receive-pack" in u.query:
                    self.send_response(403)
                    self.end_headers()
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                env = dict(os.environ, GIT_PROJECT_ROOT=fake.root, GIT_HTTP_EXPORT_ALL="1",
                           PATH_INFO=u.path, QUERY_STRING=u.query, REQUEST_METHOD=method,
                           CONTENT_TYPE=self.headers.get("Content-Type", ""),
                           CONTENT_LENGTH=str(len(body)), REMOTE_USER="someone",
                           HTTP_CONTENT_ENCODING=self.headers.get("Content-Encoding", ""),
                           GIT_PROTOCOL=self.headers.get("Git-Protocol", ""))
                out = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True).stdout
                head, _, payload = out.partition(b"\r\n\r\n")
                status = 200
                hdrs = []
                for line in head.decode().split("\r\n"):
                    k, _, v = line.partition(": ")
                    if k.lower() == "status":
                        status = int(v.split()[0])
                    elif k:
                        hdrs.append((k, v))
                self.send_response(status)
                for k, v in hdrs:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def writes(self):
        """Every request that could have changed something on 'GitHub'."""
        return [(m, p) for m, p in self.log
                if "receive-pack" in p or (m != "GET" and not p.endswith(("/git-upload-pack", "/info/lfs/objects/batch")))
                ] + [("LFS upload", p) for p in self.lfs_uploads]
