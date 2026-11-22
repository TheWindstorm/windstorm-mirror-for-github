#!/usr/bin/env python3
"""Tests for Windstorm Mirror for GitHub. Standard library only: python3 tests/test_mirror.py -v"""
import atexit
import contextlib
import hashlib
import signal
import http.server
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "mirror.py")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import mirror  # noqa: E402
from fake_github import FakeGitHub  # noqa: E402


def fread(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def version():
    """The version being built: the plugin's own, the one place it is written."""
    return jload(os.path.join(ROOT, ".claude-plugin", "plugin.json"))["version"]


def v(s):
    return tuple(int(x) for x in s.split("."))


# ------------------------------------------------------------------ helpers for the forge genes

STARTED = []           # every forge process a test started, stopped whatever way the suite ends
TEMPS = []


def _stop_everything(*_):
    for pr in STARTED:
        if pr.poll() is None:
            pr.terminate()
            try:
                pr.wait(10)
            except subprocess.TimeoutExpired:
                pr.kill()
    for d in TEMPS:
        shutil.rmtree(d, ignore_errors=True)


def _on_signal(sig, frame):
    _stop_everything()
    os._exit(128 + sig)


atexit.register(_stop_everything)
for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(_sig, _on_signal)


def real_gitea():
    """The real pinned Gitea for this system, downloaded once into WSM_TEST_CACHE and checked against the pin.
    Without WSM_TEST_CACHE the tests that need it are skipped. CI sets WSM_REQUIRE_GITEA=1 as well, so there a
    missing binary fails instead of skipping."""
    pin = mirror.PINS.get(mirror.system())
    if not os.environ.get("WSM_TEST_CACHE"):
        return None        # the real forge runs only where WSM_TEST_CACHE says so: CI, or a booked test account
    cache = os.path.join(os.environ["WSM_TEST_CACHE"], f"gitea-{mirror.GITEA_VERSION}")
    if pin and os.path.exists(cache) and mirror.sha256_of(cache) == pin[1]:
        return cache
    if not pin:
        return None
    try:
        mirror.fetch_forge({"bin": cache})
        return cache
    except SystemExit:
        return None


class Home:
    """A person's home of their own for one test: WSM_HOME, HOME and XDG_CONFIG_HOME all inside a temp folder, and
    the service managers (systemctl, loginctl, launchctl) stood in for, so a test never touches the real ones."""

    def __init__(self, linger="no", enable_linger_ok=False, service_ok=True):
        self.tmp = tempfile.mkdtemp(prefix="wsm-")
        TEMPS.append(self.tmp)
        self.calls = []
        self.linger, self.enable_ok, self.service_ok = linger, enable_linger_ok, service_ok
        self.timer_next = "Sun 2026-11-22 19:15:00 UTC"     # what the system says of the sync timer's next run
        self.env = {"WSM_HOME": os.path.join(self.tmp, "wsm"), "HOME": os.path.join(self.tmp, "home"),
                    "XDG_CONFIG_HOME": os.path.join(self.tmp, "home", ".config")}
        os.makedirs(self.env["HOME"])
        self.saved_env = {k: os.environ.get(k) for k in self.env}
        self.saved_run = mirror.run
        self.procs = []

    def fake_run(self, cmd, **kw):
        if cmd[0] in ("systemctl", "loginctl", "launchctl"):
            self.calls.append(cmd)
            if cmd[:2] == ["loginctl", "show-user"]:
                return subprocess.CompletedProcess(cmd, 0, f"Linger={self.linger}\n", "")
            if cmd[:3] == ["systemctl", "--user", "show"]:
                if self.timer_next is None:
                    return subprocess.CompletedProcess(cmd, 0, "ActiveState=inactive\nNextElapseUSecRealtime=\n", "")
                return subprocess.CompletedProcess(cmd, 0, f"ActiveState=active\nNextElapseUSecRealtime={self.timer_next}\n", "")
            if cmd[:2] == ["launchctl", "print"]:
                if self.timer_next is None:
                    return subprocess.CompletedProcess(cmd, 113, "", "Could not find service")
                return subprocess.CompletedProcess(cmd, 0, "state = waiting\n\trun interval = 900 seconds\n", "")
            if cmd[:2] == ["loginctl", "enable-linger"]:
                if self.enable_ok:
                    self.linger = "yes"
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                return subprocess.CompletedProcess(cmd, 1, "", "Access denied")
            return subprocess.CompletedProcess(cmd, 0 if self.service_ok else 1, "", "" if self.service_ok else "Failed to connect to bus")
        return self.saved_run(cmd, **kw)

    def __enter__(self):
        os.environ.update(self.env)
        mirror.run = self.fake_run
        return self

    def __exit__(self, *a):
        for pr in self.procs:
            if pr.poll() is None:
                pr.terminate()
                try:
                    pr.wait(10)
                except subprocess.TimeoutExpired:
                    pr.kill()
        mirror.run = self.saved_run
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def place_gitea(self):
        g = real_gitea()
        if not g:
            if os.environ.get("WSM_REQUIRE_GITEA"):
                raise AssertionError("the real pinned Gitea is required here and could not be fetched")
            raise unittest.SkipTest("no real pinned Gitea for this system")
        p = mirror.paths()
        os.makedirs(os.path.dirname(p["bin"]), exist_ok=True)
        shutil.copyfile(g, p["bin"])
        os.chmod(p["bin"], 0o755)
        return p

    def setup(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mirror.main(["setup", *args])
        return rc, out.getvalue()

    def start_forge(self):
        p = mirror.paths()
        pr = subprocess.Popen([sys.executable, "-I", p["script"], "run-forge"], env=dict(os.environ),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(pr)
        STARTED.append(pr)
        port = int(mirror.ini_value(p["app_ini"], "server", "HTTP_PORT"))
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/healthz", timeout=2)
                return port, pr
            except (urllib.error.URLError, OSError):
                if pr.poll() is not None:
                    raise AssertionError("the forge stopped: " + pr.stdout.read()[-400:])
                time.sleep(0.5)
        raise AssertionError("the forge did not answer in 60 s")


def http_get(url, key=None):
    req = urllib.request.Request(url, headers={"Authorization": f"token {key}"} if key else {})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        r = urllib.request.build_opener(NoRedirect).open(req, timeout=10)
        return r.status, r.headers, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


def tree(root):
    out = set()
    for d, dirs, files in os.walk(root):
        for f in dirs + files:
            out.add(os.path.relpath(os.path.join(d, f), root))
    return out


class ServeFile:
    """A local download site serving one file at any path, for the checksum tests."""

    def __init__(self, data):
        body = data

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# ------------------------------------------------------------------ G1 install the forge

class G1(unittest.TestCase):
    def test_G1_1_the_forge_is_the_pinned_version_and_its_checksum_is_checked_first(self):
        # Every system 0.1 supports has a pin, and the pins are SHA-256 values.
        self.assertEqual(set(mirror.PINS), {("linux", "x86_64"), ("darwin", "x86_64"), ("darwin", "arm64")})
        for suffix, sha in mirror.PINS.values():
            self.assertRegex(sha, r"^[0-9a-f]{64}$")
        body = b"a stand-in for the forge binary"
        good = hashlib.sha256(body).hexdigest()
        site = ServeFile(body)
        saved = dict(mirror.PINS)
        try:
            with Home():
                p = mirror.paths()
                key = mirror.system()
                mirror.PINS[key] = ("any", "0" * 64)                 # the file served does not match the pin
                with self.assertRaises(SystemExit) as e:
                    mirror.fetch_forge(p, site.url)
                self.assertIn("does not match its pinned checksum", str(e.exception))
                self.assertFalse(os.path.exists(p["bin"]), "a file that failed its checksum was kept")
                self.assertFalse(os.path.exists(p["bin"] + ".download"), "the partial download was kept")
                mirror.PINS[key] = ("any", good)                     # now it matches
                self.assertTrue(mirror.fetch_forge(p, site.url))
                self.assertTrue(os.stat(p["bin"]).st_mode & stat.S_IXUSR)
                self.assertFalse(mirror.fetch_forge(p, site.url), "a binary that matches its pin is fetched again")
        finally:
            mirror.PINS.clear()
            mirror.PINS.update(saved)
            site.close()
        # The real download names the skill in its User-Agent: Gitea's site refused Python's default (M16).
        self.assertIn("windstorm-mirror-for-github/", mirror.USER_AGENT)
        self.assertIn('headers={"User-Agent": USER_AGENT}', fread(SCRIPT))

    def test_G1_2_the_forge_runs_as_the_person_and_comes_back_after_a_reboot(self):
        with Home(linger="yes") as h:
            p = h.place_gitea()
            rc, out = h.setup()
            self.assertEqual(rc, 0, out)
            if mirror.system()[0] == "darwin":
                txt = fread(p["plist"])
                self.assertIn("<key>RunAtLoad</key><true/>", txt)
                self.assertIn(p["script"], txt)
                self.assertTrue(any(c[:2] == ["launchctl", "bootstrap"] for c in h.calls))
            else:
                txt = fread(p["unit"])
                self.assertTrue(p["unit"].startswith(os.environ["XDG_CONFIG_HOME"]), "the unit is not the person's own")
                self.assertNotIn("User=", txt)                       # a user unit: it runs as the person
                self.assertIn(f"-I {p['script']} run-forge", txt)
                self.assertIn("WantedBy=default.target", txt)
                self.assertIn(f'Environment="WSM_HOME={os.environ["WSM_HOME"]}"', txt)
                self.assertIn(["systemctl", "--user", "enable", "--now", mirror.SERVICE], h.calls)
                self.assertIn("After a restart it starts by itself.", out)
            self.assertFalse(any("sudo" in c for c in h.calls), "setup asked for administrator rights")

    def test_G1_3_the_forge_listens_on_this_computer_only_with_signup_closed(self):
        with Home(linger="yes") as h:
            h.place_gitea()
            self.assertEqual(h.setup()[0], 0)
            p = mirror.paths()
            for section, key, want in (("server", "HTTP_ADDR", "127.0.0.1"), ("server", "DISABLE_SSH", "true"),
                                       ("service", "DISABLE_REGISTRATION", "true"), ("service", "REQUIRE_SIGNIN_VIEW", "true"),
                                       ("actions", "ENABLED", "false"), ("mirror", "ENABLED", "false"),
                                       ("repository", "DISABLE_MIGRATIONS", "true"), ("cron.update_checker", "ENABLED", "false")):
                self.assertEqual(mirror.ini_value(p["app_ini"], section, key), want, f"[{section}] {key}")
            port, _ = h.start_forge()
            base = f"http://127.0.0.1:{port}"
            # It answers on 127.0.0.1 and on none of this computer's other addresses.
            others = set()
            try:
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                    others.add(info[4][0])
            except OSError:
                pass
            for a in others - {"127.0.0.1"}:
                s = socket.socket()
                s.settimeout(2)
                self.assertNotEqual(s.connect_ex((a, port)), 0, f"the forge answers on {a}")
                s.close()
            code, _, body = http_get(base + "/user/sign_up")
            self.assertIn("Registration is disabled", body)
            code, hdr, _ = http_get(base + "/explore/repos")
            self.assertEqual(code, 303)
            self.assertIn("/user/login", hdr.get("Location", ""))
            self.assertEqual(http_get(base + "/api/v1/repos/search")[0], 403)
            key = fread(p["mirror_key"]).strip()
            settings = json.loads(http_get(base + "/api/v1/settings/repository", key)[2])
            self.assertTrue(settings["mirrors_disabled"] and settings["migrations_disabled"])
            # Gitea's own pull mirror, the one that writes a key to disk, cannot be made at all.
            req = urllib.request.Request(base + "/api/v1/repos/migrate", method="POST",
                                         data=json.dumps({"clone_addr": "https://example.invalid/x.git", "repo_name": "x",
                                                          "mirror": True}).encode(),
                                         headers={"Authorization": f"token {key}", "Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(e.exception.code, 403)
            # Every start checks the port: a taken port is said, and the forge does not start.
            out = subprocess.run([sys.executable, "-I", p["script"], "run-forge"], env=dict(os.environ),
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 3)
            self.assertIn(f"port {port} on 127.0.0.1 is taken by another program", out.stdout)

    def test_G1_4_setup_makes_the_two_accounts_and_never_prints_a_password(self):
        with Home(linger="yes") as h:
            p = h.place_gitea()
            rc, out = h.setup()
            self.assertEqual(rc, 0, out)
            pw = fread(p["password"]).strip()
            key = fread(p["mirror_key"]).strip()
            self.assertGreaterEqual(len(pw), 8)
            self.assertGreaterEqual(len(key), 20)
            for f in (p["password"], p["mirror_key"], p["app_ini"]):
                self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600, f)
            self.assertEqual(stat.S_IMODE(os.stat(p["conf"]).st_mode), 0o700)
            self.assertNotIn(pw, out)
            self.assertNotIn(key, out)
            self.assertIn(p["password"], out)                       # it says where the password is
            users = mirror.forge_users(p)
            self.assertIn(mirror.person_name(), users)
            self.assertIn("mirror", users)
            port, _ = h.start_forge()
            me = json.loads(http_get(f"http://127.0.0.1:{port}/api/v1/user", key)[2])
            self.assertEqual(me["login"], "mirror")
            self.assertFalse(me["is_admin"], "the mirror account must not be an administrator")
            log = "".join(open(os.path.join(d, f), errors="replace").read()
                          for d, _, fs in os.walk(os.path.join(p["forge"], "log")) for f in fs)
            self.assertNotIn(pw, log)
            self.assertNotIn(key, log)

    def test_G1_5_setup_dry_run_changes_nothing_and_setup_twice_is_one_setup(self):
        with Home(linger="yes") as h:
            before = tree(h.tmp)
            saved = mirror.fetch_forge
            mirror.fetch_forge = lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry run downloaded"))
            try:
                rc, out = h.setup("--dry-run")
            finally:
                mirror.fetch_forge = saved
            self.assertEqual(rc, 0)
            self.assertIn("nothing is changed", out)
            self.assertIn("download Gitea", out)
            self.assertEqual(tree(h.tmp), before, "the dry run changed files")
            self.assertFalse([c for c in h.calls if c[:2] != ["loginctl", "show-user"]], "the dry run ran a command that changes something")
            p = h.place_gitea()
            self.assertEqual(h.setup()[0], 0)
            first = (fread(p["app_ini"]), fread(p["password"]), fread(p["mirror_key"]), mirror.forge_users(p))
            rc, out = h.setup()
            self.assertEqual(rc, 0, out)
            again = (fread(p["app_ini"]), fread(p["password"]), fread(p["mirror_key"]), mirror.forge_users(p))
            self.assertEqual(first, again, "a second setup changed the forge, its accounts or its keys")
            self.assertIn("already here", out)

    def test_G1_6_when_it_will_not_come_back_by_itself_it_says_so(self):
        if mirror.system()[0] == "darwin":
            with Home() as h:
                h.place_gitea()
                rc, out = h.setup()
                self.assertIn(mirror.NO_LOGIN_NOTE, out)
                self.assertIn("FileVault", out)
            return
        with Home(linger="no", enable_linger_ok=False) as h:      # the system refuses: an administrator's step
            h.place_gitea()
            rc, out = h.setup()
            self.assertEqual(rc, 0, out)
            self.assertIn(mirror.NO_LOGIN_NOTE, out)
            self.assertIn(f"sudo loginctl enable-linger {mirror.getpass.getuser()}", out)
            self.assertIn(["loginctl", "enable-linger", mirror.getpass.getuser()], h.calls)
            rc, out = h.setup("--dry-run")
            self.assertIn(mirror.NO_LOGIN_NOTE, out)
        with Home(linger="no", enable_linger_ok=True) as h:       # the person may turn it on: setup does
            h.place_gitea()
            rc, out = h.setup()
            self.assertIn("After a restart it starts by itself.", out)
            self.assertNotIn(mirror.NO_LOGIN_NOTE, out)


# ------------------------------------------------------------------ G2 the GitHub keys

def files_holding(root, needle):
    hits, read = [], 0
    for d, _, fs in os.walk(root):
        for f in fs:
            path = os.path.join(d, f)
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            read += 1
            if needle.encode() in data:
                hits.append(path)
    return hits, read


class G2(unittest.TestCase):
    def setUp(self):
        self.gh_root = tempfile.mkdtemp(prefix="wsm-gh-")
        self.gh = FakeGitHub(self.gh_root, key="ghkey-0123456789abcdef")
        self.gh.add_repo("someone", "alpha")
        self.gh.add_repo("acme", "tools", owner_type="Organization")
        os.environ["WSM_GITHUB_API"] = self.gh.start()

    def tearDown(self):
        self.gh.stop()
        os.environ.pop("WSM_GITHUB_API", None)
        shutil.rmtree(self.gh_root, ignore_errors=True)

    def key_set(self, owner, key):
        out, saved = io.StringIO(), sys.stdin
        sys.stdin = io.StringIO(key + "\n")
        try:
            with contextlib.redirect_stdout(out):
                rc = mirror.main(["key", "set", owner])
        finally:
            sys.stdin = saved
        return rc, out.getvalue()

    def test_G2_2_each_key_is_kept_in_its_own_file_and_nowhere_else(self):
        key = self.gh.key
        with Home() as h:
            p = mirror.paths()
            rc, out = self.key_set("someone", key)
            self.assertEqual(rc, 0, out)
            rc, out2 = self.key_set("acme", key)              # the stand-in lets one key see both; GitHub would not
            self.assertEqual(rc, 0, out2)
            for owner, kind in (("someone", "user"), ("acme", "org")):
                f = mirror.key_path(p, owner)
                self.assertEqual(fread(f).strip(), key)
                self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
                self.assertEqual(mirror.load_json(p["settings"], {})["owners"][owner]["kind"], kind)
            self.assertEqual(stat.S_IMODE(os.stat(p["keys"]).st_mode), 0o700)
            hits, read = files_holding(h.tmp, key)
            self.assertGreater(read, 0, "the scan read no files")
            self.assertEqual(sorted(hits), sorted([mirror.key_path(p, "someone"), mirror.key_path(p, "acme")]),
                             "the key was written somewhere else")
            self.assertNotIn(key, out + out2)
            # A key is never taken from the command line.
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    mirror.main(["key", "set", "someone", key])
            # A key GitHub refuses, or one that cannot see the owner, is not kept.
            rc, out = self.key_set("other", key)
            self.assertEqual(rc, 1)
            self.assertIn("cannot see 'other'", out)
            self.assertFalse(os.path.exists(mirror.key_path(p, "other")))
            rc, out = self.key_set("someone", "not-the-key")
            self.assertEqual(rc, 1)
            self.assertIn("refused", out)
            self.assertNotIn("not-the-key", out)
            self.assertEqual(fread(mirror.key_path(p, "someone")).strip(), key, "a refused key replaced a good one")
            self.assertEqual(self.gh.writes(), [], "something was written to GitHub")


# ------------------------------------------------------------------ G3 find and copy (with G2.5)

def quiet(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = fn(*args)
    return rc, out.getvalue()


class World:
    """A stand-in GitHub, and a real forge of the person's own, set up and running, with the person's key kept."""

    def __init__(self, tc):
        self.tc = tc

    def __enter__(self):
        self.home = Home(linger="yes").__enter__()
        try:
            self.home.place_gitea()
            rc, out = self.home.setup()
            self.tc.assertEqual(rc, 0, out)
            self.port, _ = self.home.start_forge()
        except BaseException:
            self.home.__exit__(None, None, None)
            raise
        self.gh_root = tempfile.mkdtemp(prefix="wsm-gh-")
        TEMPS.append(self.gh_root)
        self.gh = FakeGitHub(self.gh_root, key="ghkey-" + "a1" * 10)
        base = self.gh.start()
        os.environ["WSM_GITHUB_API"] = base
        os.environ["WSM_GITHUB_GIT"] = base
        self.p = mirror.paths()
        return self

    def __exit__(self, *a):
        self.gh.stop()
        os.environ.pop("WSM_GITHUB_API", None)
        os.environ.pop("WSM_GITHUB_GIT", None)
        shutil.rmtree(self.gh_root, ignore_errors=True)
        self.home.__exit__(*a)

    def key_set(self, owner="someone", key=None):
        saved = sys.stdin
        sys.stdin = io.StringIO((key or self.gh.key) + "\n")
        try:
            return quiet(mirror.main, ["key", "set", owner])
        finally:
            sys.stdin = saved

    def sync(self, *args):
        return quiet(mirror.main, ["sync", *args])

    def first_sync(self):
        self.tc.assertEqual(self.sync("--announce")[0], 0)
        return self.sync()

    def forge_get(self, path):
        key = fread(self.p["mirror_key"]).strip()
        code, _, body = http_get(f"http://127.0.0.1:{self.port}/api/v1{path}", key)
        return code, (json.loads(body) if body.strip().startswith(("{", "[")) else body)

    def forge_refs(self, name):
        key = fread(self.p["mirror_key"]).strip()
        hdr = "Authorization: Basic " + __import__("base64").b64encode(f"mirror:{key}".encode()).decode()
        env = dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"http.http://127.0.0.1:{self.port}/.extraheader",
                   GIT_CONFIG_VALUE_0=hdr, GIT_TERMINAL_PROMPT="0")
        out = subprocess.run(["git", "ls-remote", f"http://127.0.0.1:{self.port}/mirror/{name}.git"],
                             env=env, capture_output=True, text=True)
        return {r: s for s, r in (line.split("\t") for line in out.stdout.splitlines()) if not r.endswith("^{}") and r != "HEAD"}

    def gh_refs(self, full):
        return {r: s for s, r in (line.split(" ") for line in self.gh.git(full, "for-each-ref", "--format=%(objectname) %(refname)").splitlines())}


class G3(unittest.TestCase):
    def test_G3_1_every_repository_is_found_page_by_page_to_the_end(self):
        with World(self) as w:
            w.gh.page_cap = 2
            for i in range(5):
                w.gh.add_repo("someone", f"r{i}")
                w.gh.commit(f"someone/r{i}", {"f": b"x"})
            self.assertEqual(w.key_set()[0], 0)
            rc, out = w.first_sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("sync: 5 on GitHub, 5 copied now, 0 unchanged, 0 problem(s), 3 GitHub API call(s)", out)
            # A page that fails stops that owner's listing: nothing is changed or marked gone for it.
            w.gh.add_repo("someone", "r5")
            w.gh.fail_page = ("/user/repos", 2)
            rc, out = w.sync()
            self.assertEqual(rc, 1)
            self.assertIn("the listing of someone failed at page 2", out)
            self.assertIn("nothing was changed for someone", out)
            self.assertNotIn("gone from GitHub", out)
            st = mirror.load_json(w.p["state"], {})
            self.assertEqual(len(st["repos"]), 5)
            self.assertTrue(all(r["problem"].startswith("cannot refresh") for r in st["repos"].values()))
            self.assertEqual(w.forge_get("/repos/mirror/someone--r5")[0], 404, "a repository from half a list was copied")

    def test_G3_2_a_rename_or_a_reused_name_never_writes_one_project_over_another(self):
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"alpha's own history"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            fid = w.forge_get("/repos/mirror/someone--alpha")[1]["id"]
            alpha_refs = w.forge_refs("someone--alpha")
            # Renamed on GitHub: the same copy follows, under its new name.
            w.gh.rename("someone/alpha", "beta")
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("renamed on GitHub", out)
            code, beta = w.forge_get("/repos/mirror/someone--beta")
            self.assertEqual((code, beta["id"]), (200, fid))
            self.assertEqual(w.forge_refs("someone--beta"), alpha_refs)
            # A new repository takes the old name on GitHub: it gets a copy of its own, and nothing is overwritten.
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"b": b"a different project"})
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            code, new = w.forge_get("/repos/mirror/someone--alpha")
            self.assertEqual(code, 200)
            self.assertNotEqual(new["id"], fid)
            self.assertEqual(w.forge_refs("someone--beta"), alpha_refs, "the renamed project's copy was overwritten")
            self.assertEqual(w.forge_refs("someone--alpha"), w.gh_refs("someone/alpha"))
            # A copy left under a name another project now needs is moved aside, never overwritten.
            w.gh.delete("someone/beta")
            w.gh.rename("someone/alpha", "beta")
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("moved aside to someone--beta--was-", out)
            moved = [r for r in w.forge_get("/user/repos")[1] if r["name"].startswith("someone--beta--was-")]
            self.assertEqual(len(moved), 1)
            self.assertEqual(w.forge_refs(moved[0]["name"]), alpha_refs, "the old copy did not survive being moved aside")
            # The script never lets git follow a redirect, and reads the name by id before each push.
            src = fread(SCRIPT)
            self.assertIn('"http.followRedirects=false"', src)
            self.assertIn("forge.by_id(rec[\"forge_id\"])", src)

    def test_G3_3_branches_and_tags_are_copied_and_what_is_not_is_said(self):
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.gh.commit("someone/alpha", {"b": b"2"}, branch="side")
            w.gh.git("someone/alpha", "tag", "v1", "main")
            w.gh.git("someone/alpha", "update-ref", "refs/pull/1/head", "side")
            w.key_set()
            rc, out = w.first_sync()
            self.assertEqual(rc, 0, out)
            want = {r: s for r, s in w.gh_refs("someone/alpha").items() if not r.startswith("refs/pull/")}
            self.assertEqual(w.forge_refs("someone--alpha"), want)
            self.assertIn(mirror.NOT_COPIED, out)
            code, repo = w.forge_get("/repos/mirror/someone--alpha")
            self.assertTrue(repo["private"])
            self.assertFalse(repo["has_actions"])
            self.assertEqual(w.gh.writes(), [])
        # M17: a copy that comes back not private is deleted at once and the sync stops.
        with World(self) as w:
            for n in ("alpha", "beta"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"a": b"1"})
            w.key_set()
            saved = mirror.Forge.call

            def no_private(self_, method, path, body=None):
                if method == "POST" and path == "/user/repos":
                    body = {k: v for k, v in body.items() if k != "private"}
                return saved(self_, method, path, body)
            mirror.Forge.call = no_private
            try:
                w.sync("--announce")
                rc, out = w.sync()
            finally:
                mirror.Forge.call = saved
            self.assertEqual(rc, 1)
            self.assertIn("STOPPED:", out)
            self.assertIn("it was deleted at once (deleted)", out)
            self.assertEqual(w.forge_get("/user/repos")[1], [], "a copy that came out public was kept, or the sync went on")

    def test_G3_4_the_key_travels_only_in_the_fetchs_own_environment(self):
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            key = w.gh.key
            seen = []
            inner = mirror.run

            def spy(cmd, **kw):
                seen.append((list(cmd), dict(kw.get("env") or {})))
                return inner(cmd, **kw)
            mirror.run = spy
            try:
                rc, out = w.first_sync()
            finally:
                mirror.run = inner
            self.assertEqual(rc, 0, out)
            self.assertTrue(seen)
            for cmd, env in seen:
                self.assertFalse(any(key in a for a in cmd), f"the key was on a command line: {cmd[:4]}")
                holds = any(key in v or __import__("base64").b64encode(f"x-access-token:{key}".encode()).decode() in v
                            for v in env.values())
                if holds:
                    self.assertIn("fetch", cmd, f"the key was given to a process that does not fetch: {cmd[:6]}")
            hits, read = files_holding(w.home.tmp, key)
            self.assertGreater(read, 0)
            self.assertEqual(hits, [mirror.key_path(w.p, "someone")], "the key was written somewhere else")
            b64 = __import__("base64").b64encode(f"x-access-token:{key}".encode()).decode()
            self.assertEqual(files_holding(w.home.tmp, b64)[0], [], "the key's header was written somewhere")
            self.assertNotIn(key, out)

    def test_G3_5_what_is_left_out_is_chosen_and_listed(self):
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.add_repo("someone", "old", archived=True)
            w.gh.add_repo("someone", "forked", fork=True)
            w.gh.add_repo("someone", "empty")
            w.gh.add_repo("someone", "secret-notes")
            for n in ("alpha", "old", "forked", "secret-notes"):
                w.gh.commit(f"someone/{n}", {"a": b"1"})
            w.key_set()
            self.assertEqual(quiet(mirror.main, ["config", "exclude", "someone/secret-*"])[0], 0)
            rc, out = w.first_sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("left out: someone/secret-notes (left out by 'someone/secret-*')", out)
            names = sorted(r["name"] for r in w.forge_get("/user/repos")[1])
            self.assertEqual(names, ["someone--alpha", "someone--empty", "someone--forked", "someone--old"])
            # An include that matches nothing stops the sync; it never widens to everything.
            quiet(mirror.main, ["config", "include", "someone/nothing-like-this"])
            w.gh.add_repo("someone", "later")
            rc, out = w.sync()
            self.assertEqual(rc, 2)
            self.assertIn("matches no repository", out)
            self.assertEqual(w.forge_get("/repos/mirror/someone--later")[0], 404)
            quiet(mirror.main, ["config", "include", "someone/alpha"])
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("left out: someone/later (not in include)", out)

    def test_G3_6_a_repository_gone_from_github_keeps_its_copy(self):
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            before = w.forge_refs("someone--alpha")
            w.gh.delete("someone/alpha")
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("someone/alpha: gone from GitHub (or no longer visible to its key); its copy is kept", out)
            self.assertEqual(w.forge_refs("someone--alpha"), before)
            st = mirror.load_json(w.p["state"], {})
            self.assertTrue(next(iter(st["repos"].values()))["gone_since"])
            self.assertEqual(fread(SCRIPT).count('"DELETE"'), 1, "the script can delete a copy outside the M17 stop")

    def test_G3_7_the_first_copy_is_announced_with_its_size_and_time_before_it_starts(self):
        with World(self) as w:
            for i in range(5):
                w.gh.add_repo("someone", f"r{i}")
                w.gh.commit(f"someone/r{i}", {"a": b"1"})
            w.gh.repos["someone/r0"]["size"] = 2048
            w.key_set()
            rc, out = w.sync()
            self.assertEqual(rc, 2)
            self.assertIn("run: sync --announce", out)
            self.assertEqual(w.forge_get("/user/repos")[1], [])
            rc, out = w.sync("--announce")
            self.assertEqual(rc, 0)
            self.assertIn("The first copy: 5 repositories from 1 owner(s), 2.0 MB as GitHub reports it", out)
            self.assertIn("as private as this computer", out)
            self.assertIn("When the person says go, run: sync", out)
            self.assertEqual(w.forge_get("/user/repos")[1], [], "the announcement copied something")
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertRegex(out, r"first copy: 3 of 5 done; about \d+ min left at this pace")
            rc, out = w.sync()
            self.assertNotIn("first copy:", out)

    def test_G3_8_large_files_are_copied_or_named_as_missing(self):
        if not mirror.has_git_lfs():
            if os.environ.get("WSM_REQUIRE_GITEA"):
                raise AssertionError("git lfs is needed for this test in CI")
            raise unittest.SkipTest("no git lfs here")
        data = os.urandom(200_000)
        with World(self) as w:
            w.gh.add_repo("someone", "big")
            w.gh.commit("someone/big", {".gitattributes": b"*.bin filter=lfs diff=lfs merge=lfs -text\n",
                                         "blob.bin": w.gh.lfs_file(data)})
            w.key_set()
            rc, out = w.first_sync()
            self.assertEqual(rc, 0, out)
            key = fread(w.p["mirror_key"]).strip()
            req = urllib.request.Request(f"http://127.0.0.1:{w.port}/api/v1/repos/mirror/someone--big/media/blob.bin",
                                         headers={"Authorization": f"token {key}"})
            self.assertEqual(urllib.request.urlopen(req, timeout=10).read(), data)
        with World(self) as w:
            w.gh.add_repo("someone", "big")
            w.gh.commit("someone/big", {".gitattributes": b"*.bin filter=lfs diff=lfs merge=lfs -text\n",
                                         "blob.bin": w.gh.lfs_file(data)})
            w.key_set()
            saved = mirror.has_git_lfs
            mirror.has_git_lfs = lambda: False
            try:
                rc, out = w.first_sync()
            finally:
                mirror.has_git_lfs = saved
            self.assertEqual(rc, 0, out)
            self.assertIn("Large files NOT copied (git lfs is not installed here): someone/big", out)


class G2Sync(unittest.TestCase):
    def test_G2_5_a_new_key_is_one_step_and_the_next_sync_proves_it(self):
        with World(self) as w:
            for n in ("alpha", "beta"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            w.gh.key = "ghkey-" + "b2" * 10            # the old key is changed on GitHub
            rc, out = w.sync()
            self.assertEqual(rc, 1)
            self.assertIn("GitHub refused the key for someone (401)", out)
            st = mirror.load_json(w.p["state"], {})
            self.assertTrue(all("cannot refresh" in r["problem"] for r in st["repos"].values()))
            self.assertEqual(w.key_set(key=w.gh.key)[0], 0)      # one step
            w.gh.commit("someone/alpha", {"b": b"2"})
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("sync: 2 on GitHub, 2 copied now, 0 unchanged, 0 problem(s)", out)
            self.assertEqual(w.forge_refs("someone--alpha"), w.gh_refs("someone/alpha"))


def put_settings(p, **kw):
    s = mirror.load_json(p["settings"], {})
    s.update(kw)
    mirror.write_file(p["settings"], json.dumps(s), 0o600)


def git_calls(gh, full=None):
    return [x for x in gh.log if ".git/" in x[1] and (full is None or x[1].startswith(f"/{full}.git/"))]


class G4(unittest.TestCase):
    def test_G4_1_a_timer_runs_sync_and_status_reads_its_next_run(self):
        with World(self) as w:
            p, h = w.p, w.home
            if mirror.system()[0] == "darwin":
                plist = fread(os.path.join(os.path.dirname(p["plist"]), mirror.SYNC_LABEL + ".plist"))
                self.assertIn("<integer>900</integer>", plist)
                self.assertIn("<string>--from-timer</string>", plist)
            else:
                base = os.path.dirname(p["unit"])
                self.assertIn("OnUnitActiveSec=15min", fread(os.path.join(base, mirror.SYNC_UNIT + ".timer")))
                self.assertIn("Persistent=true", fread(os.path.join(base, mirror.SYNC_UNIT + ".timer")))
                unit = fread(os.path.join(base, mirror.SYNC_UNIT + ".service"))
                self.assertIn(f"{p['script']} sync --from-timer", unit)
                self.assertIn("WSM_HOME=", unit)
                self.assertIn(["systemctl", "--user", "enable", "--now", mirror.SYNC_UNIT + ".timer"], h.calls)
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            # Before the person's go, the timer copies nothing and is not a failure.
            rc, out = w.sync("--from-timer")
            self.assertEqual(rc, 0, out)
            self.assertIn("waiting for the person's go", out)
            self.assertEqual(w.forge_get("/user/repos")[1], [])
            self.assertEqual(w.first_sync()[0], 0)
            w.gh.commit("someone/alpha", {"b": b"2"})
            rc, out = w.sync("--from-timer")
            self.assertEqual(rc, 0, out)
            self.assertIn("1 copied now", out)
            self.assertEqual(w.forge_refs("someone--alpha"), w.gh_refs("someone/alpha"))
            rc, out = quiet(mirror.main, ["status"])
            self.assertEqual(rc, 0, out)
            self.assertIn("next sync: " + ("every 15 min" if mirror.system()[0] == "darwin" else h.timer_next), out)
            self.assertRegex(out, r"last sync: \S+, 1 copied, 0 unchanged, 0 problem\(s\), \d+ GitHub API call\(s\)")
            # A timer that is not running, or (after a restart) is on with no next run, is a problem, not "on".
            h.timer_next = None
            rc, out = quiet(mirror.main, ["status"])
            self.assertEqual(rc, 1)
            self.assertIn("problem: the sync timer is not", out)
            if mirror.system()[0] != "darwin":
                h.timer_next = "n/a"
                rc, out = quiet(mirror.main, ["status"])
                self.assertEqual(rc, 1)
                self.assertIn("the sync timer is on but has no next run", out)

    def test_G4_2_one_slow_repository_never_holds_up_the_rest(self):
        with World(self) as w:
            for n in ("alpha", "beta"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"a": b"1"})
            w.key_set()
            put_settings(w.p, git_timeout_s=2)
            w.gh.slow["someone/alpha"] = 5
            started = time.time()
            rc, out = w.first_sync()
            self.assertEqual(rc, 1, out)
            self.assertLess(time.time() - started, 40)
            self.assertIn("problem: someone/alpha: the fetch from GitHub took longer than 2 s, and so did the retry", out)
            self.assertIn("2 on GitHub, 1 copied now", out)
            self.assertEqual(len([x for x in git_calls(w.gh, "someone/alpha") if "info/refs" in x[1]]), 2,
                             "the slow fetch was not tried exactly twice")
            self.assertEqual(w.forge_refs("someone--beta"), w.gh_refs("someone/beta"))
            st = mirror.load_json(w.p["state"], {})
            alpha = next(r for r in st["repos"].values() if r["full_name"] == "someone/alpha")
            self.assertTrue(alpha["problem"].startswith("cannot refresh: the fetch from GitHub took longer"))
            # When it is quick again, the next sync copies it.
            w.gh.slow.clear()
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertEqual(w.forge_refs("someone--alpha"), w.gh_refs("someone/alpha"))

    def test_G4_3_one_sync_at_a_time(self):
        with Home() as h:
            p = mirror.paths()
            lock = os.path.join(p["data"], "sync.lock")
            os.makedirs(p["data"], exist_ok=True)
            ok, _ = mirror.take_lock(p)                 # a sync of this process holds the lock
            self.assertTrue(ok)
            rc, out = quiet(mirror.main, ["sync"])
            self.assertEqual(rc, 3)
            self.assertIn(f"another sync is running (process {os.getpid()}); this one did not start", out)
            mirror.drop_lock(p)
            self.assertFalse(os.path.exists(lock))
            gone = subprocess.Popen([sys.executable, "-c", "pass"])
            gone.wait()
            mirror.write_file(lock, str(gone.pid), 0o600)  # a lock left by a sync that is gone
            rc, out = quiet(mirror.main, ["sync"])
            self.assertIn(f"a lock left by a sync that is gone (process {gone.pid}) was taken over", out)
            self.assertIn("Run setup first.", out)
            self.assertFalse(os.path.exists(lock), "the lock outlived its sync")

    def test_G4_4_a_sync_spends_few_api_calls_and_stops_above_a_floor(self):
        with World(self) as w:
            w.gh.page_cap = 2
            for i in range(5):
                w.gh.add_repo("someone", f"r{i}")
                w.gh.commit(f"someone/r{i}", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            before = {f"r{i}": w.forge_refs(f"someone--r{i}") for i in range(5)}
            w.gh.rate_remaining = 450                  # below the floor of 500
            rc, out = w.sync()
            self.assertEqual(rc, 1, out)
            self.assertIn("GitHub's remaining API allowance (449) is below the floor (500), so the listing of someone "
                          "stopped at page 1", out)
            self.assertIn("0 copied now", out)
            self.assertIn("1 GitHub API call(s)", out)
            self.assertNotIn("gone from GitHub", out)
            self.assertEqual({f"r{i}": w.forge_refs(f"someone--r{i}") for i in range(5)}, before)
            put_settings(w.p, api_floor=100)           # the floor is a setting
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("3 GitHub API call(s)", out)
            self.assertIn("3 GitHub API call(s)", quiet(mirror.main, ["status"])[1])

    def test_G4_5_a_full_disk_stops_the_sync_cleanly(self):
        with World(self) as w:
            for n in ("alpha", "beta"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            before = w.forge_refs("someone--alpha")
            w.gh.commit("someone/alpha", {"b": b"2"})
            put_settings(w.p, disk_floor_mb=10 ** 9)
            rc, out = w.sync()
            self.assertEqual(rc, 1)
            self.assertRegex(out, r"The sync did not start: only \d+ MB free on this disk, below the floor of "
                                  r"1000000000 MB\. Every copy is as it was\.")
            self.assertEqual(w.forge_refs("someone--alpha"), before)
            self.assertIn("stopped: only", quiet(mirror.main, ["status"])[1])
            put_settings(w.p, disk_floor_mb=0)
            # A sync killed part-way through a copy: that copy stays as it was and is not counted current.
            w.gh.slow["someone/alpha"] = 3
            pr = subprocess.Popen([sys.executable, "-I", w.p["script"], "sync"], env=dict(os.environ),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            STARTED.append(pr)
            for _ in range(100):
                st = mirror.load_json(w.p["state"], {})
                if any(r.get("copying_since") for r in st.get("repos", {}).values()):
                    break
                time.sleep(0.1)
            pr.kill()
            pr.wait()
            st = mirror.load_json(w.p["state"], {})
            alpha = next(r for r in st["repos"].values() if r["full_name"] == "someone/alpha")
            self.assertTrue(alpha.get("copying_since"), "the copy in progress was not marked")
            self.assertEqual(w.forge_refs("someone--alpha"), before)
            w.gh.slow.clear()
            time.sleep(4)                              # the killed sync's own git child finishes its stalled request
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("was taken over", out)
            self.assertEqual(w.forge_refs("someone--alpha"), w.gh_refs("someone/alpha"))
            st = mirror.load_json(w.p["state"], {})
            self.assertFalse(any(r.get("copying_since") for r in st["repos"].values()))

    def test_G4_6_a_sync_fetches_only_what_changed_and_a_daily_pass_fetches_all(self):
        with World(self) as w:
            for i in range(3):
                w.gh.add_repo("someone", f"r{i}")
                w.gh.commit(f"someone/r{i}", {"a": b"1"})
            w.key_set()
            time.sleep(1.1)                            # pushed_at has whole seconds: a push in a fetch's own second is fetched again
            rc, out = w.first_sync()
            self.assertIn("a full pass", out)
            time.sleep(1.1)                            # pushed_at has whole seconds
            # A change the listing does not show: a branch made with no new pushed_at.
            bare = os.path.join(w.gh_root, "someone", "r2.git")
            subprocess.run(["git", "--git-dir", bare, "branch", "side", "main"], check=True)
            n = len(git_calls(w.gh))
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("sync: 3 on GitHub, 0 copied now, 3 unchanged, 0 problem(s), 1 GitHub API call(s)", out)
            self.assertEqual(git_calls(w.gh)[n:], [], "a quiet sync fetched")
            w.gh.commit("someone/r1", {"b": b"2"})
            rc, out = w.sync()
            self.assertIn("1 copied now, 2 unchanged", out)
            self.assertNotIn("refs/heads/side", w.forge_refs("someone--r2"))
            st = mirror.load_json(w.p["state"], {})
            st["last_full_pass"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 25 * 3600))
            mirror.save_state(w.p, st)
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("3 copied now, 0 unchanged, 0 problem(s), 1 GitHub API call(s), a full pass", out)
            self.assertIn("refs/heads/side", w.forge_refs("someone--r2"))
            for i in range(3):
                self.assertEqual(w.forge_refs(f"someone--r{i}"), w.gh_refs(f"someone/r{i}"))


class Harness(unittest.TestCase):
    def test_a_suite_stopped_part_way_leaves_no_forge_running(self):
        if not real_gitea():
            if os.environ.get("WSM_REQUIRE_GITEA"):
                raise AssertionError("the real pinned Gitea is required here")
            raise unittest.SkipTest("no real pinned Gitea for this system")
        marker = tempfile.mkdtemp(prefix="wsm-stop-")
        child = f"""
import os, sys, time
sys.path.insert(0, {os.path.join(ROOT, "tests")!r})
import test_mirror as t
h = t.Home(linger="yes").__enter__()
h.place_gitea(); h.setup(); port, pr = h.start_forge()
open(os.path.join({marker!r}, "ready"), "w").write(f"{{pr.pid}} {{h.tmp}}")
time.sleep(600)
"""
        pr = subprocess.Popen([sys.executable, "-I", "-c", child], env=dict(os.environ), stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        try:
            ready = os.path.join(marker, "ready")
            for _ in range(240):
                if os.path.exists(ready) and fread(ready).strip():
                    break
                time.sleep(0.5)
            gpid, tmp = fread(ready).split()
            pr.send_signal(signal.SIGTERM)
            pr.wait(30)
            time.sleep(1)
            self.assertFalse(os.path.exists(f"/proc/{gpid}") if os.path.exists("/proc") else
                             subprocess.run(["kill", "-0", gpid], capture_output=True).returncode == 0,
                             "the forge kept running after the suite was stopped")
            self.assertFalse(os.path.exists(tmp), "the suite's temporary home was left behind")
        finally:
            if pr.poll() is None:
                pr.kill()
            shutil.rmtree(marker, ignore_errors=True)


class Promises(unittest.TestCase):
    """PROMISES.json holds what real machines, and the people using them, showed must change, and the version
    each change is due in. A lesson is a gate, not a note: these tests refuse a version while a promise due in it
    is not built, and the version after it until the promise was seen to work on a real machine."""

    def setUp(self):
        self.doc = jload(os.path.join(ROOT, "PROMISES.json"))
        self.rows = self.doc["rows"]
        self.version = v(version())
        self.tests = set(re.findall(r"def (test_[A-Za-z0-9_]+)\(", fread(os.path.abspath(__file__))))
        self.plan = fread(os.path.join(ROOT, "DNA-STRAND-MASTER-PLAN.md"))

    def test_PROMISES_every_row_is_whole_and_its_codon_is_in_the_plan(self):
        ids = [r["id"] for r in self.rows]
        self.assertEqual(len(ids), len(set(ids)), "two rows share an id")
        for r in self.rows:
            for k in ("id", "seen", "promise", "codon", "owner", "first_ship_in", "ship_in"):
                self.assertTrue(r.get(k), f"{r.get('id')}: '{k}' is empty")
            for k in ("test", "proven", "issue", "moved_why", "moved_by_person"):
                self.assertIn(k, r, f"{r['id']}: the column '{k}' is missing")
            self.assertIn(r["owner"], ("person", "maintainer"), r["id"])
            self.assertIn(f"| {r['codon']} |", self.plan, f"{r['id']}: codon {r['codon']} is not in the plan")

    def test_PROMISES_a_lesson_not_taken_is_written_down_with_its_reason(self):
        for d in self.doc.get("declined", []):
            self.assertTrue(d.get("heard") and d.get("why"), "a declined lesson needs what was heard and why it was not taken")

    def test_PROMISES_no_version_ships_while_a_promise_due_in_it_is_not_built(self):
        for r in self.rows:
            if v(r["ship_in"]) <= self.version:
                self.assertIn(r.get("test"), self.tests,
                              f"{r['id']} ({r['promise'][:60]}...) is due in {r['ship_in']} and has no test. "
                              "Build it, or have its date moved (see how_to_use in PROMISES.json).")
                self.assertIn(f"`{r['test']}`", self.plan, f"{r['id']}: its test is not named in the plan")

    def test_PROMISES_the_version_after_needs_proof_on_a_real_machine(self):
        for r in self.rows:
            if v(r["ship_in"]) < self.version:
                self.assertTrue(r.get("proven"), f"{r['id']} shipped in {r['ship_in']} and no real machine has proven it yet")

    def test_PROMISES_a_date_moves_only_with_a_reason_and_a_persons_row_only_by_their_word(self):
        for r in self.rows:
            if r["ship_in"] != r["first_ship_in"]:
                self.assertTrue(r.get("moved_why"), f"{r['id']}: its date moved with no reason written")
                if r["owner"] == "person":
                    self.assertTrue(r.get("moved_by_person"), f"{r['id']}: only the person who saw it can move its date")


class Strand(unittest.TestCase):
    def test_every_proof_named_in_the_plan_is_a_real_test_and_every_test_has_a_codon(self):
        plan = fread(os.path.join(ROOT, "DNA-STRAND-MASTER-PLAN.md"))
        named = set(re.findall(r"`(test_[A-Za-z0-9_]+)`", plan))
        to_build = set(re.findall(r"`(test_[A-Za-z0-9_]+)` \*\(to build\)\*", plan))
        real = set(re.findall(r"def (test_(?:G|INV)[A-Za-z0-9_]+)\(", fread(os.path.abspath(__file__))))
        self.assertEqual(to_build & real, set(), "a test marked 'to build' exists: remove the mark")
        if to_build:
            self.assertEqual(version(), "0.0.0", "a proof may be marked 'to build' only while nothing is released")
        self.assertEqual(named - real - to_build, set(), "the plan names tests that do not exist")
        self.assertEqual(real - named, set(), "tests with no codon in the plan")

    def test_no_codon_is_used_twice_in_the_plan(self):
        import collections
        plan = fread(os.path.join(ROOT, "DNA-STRAND-MASTER-PLAN.md"))
        seen = collections.Counter(re.findall(r"^\| ((?:G|INV)[0-9.]+) \|", plan, re.M))
        self.assertEqual([c for c, n in seen.items() if n > 1], [], "a codon is used for two claims")


class Package(unittest.TestCase):
    def test_the_script_and_the_plugin_carry_the_same_version(self):
        self.assertEqual(mirror.VERSION, version())

    def test_nothing_is_offered_for_install_before_0_1_0(self):
        """Until the first release the version stays 0.0.0 and the README says nothing can be installed."""
        if v(version()) < (0, 1, 0):
            self.assertIn("Nothing here can be installed yet", fread(os.path.join(ROOT, "README.md")))


if __name__ == "__main__":
    unittest.main()
