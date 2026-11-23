#!/usr/bin/env python3
"""Tests for Windstorm Mirror for GitHub. Standard library only: python3 tests/test_mirror.py -v"""
import atexit
import contextlib
import hashlib
import signal
import tarfile
import http.server
import inspect
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


def fread_bytes(path):
    with open(path, "rb") as f:
        return f.read()


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
        self.forge_follows = False      # when True, stopping and starting the service stops and starts the forge
        self.auto_login, self.filevault = None, False      # a Mac's automatic login after a restart, and FileVault
        self.pkcheck = 1             # polkit's exit for "may this person switch on their own lingering"; None: no pkcheck
        self.timer_mono = ""         # the timer's next run on the boot clock (systemd's NextElapseUSecMonotonic)
        self.kws = []                # the keyword arguments of each command the stand-in answered
        self.env = {"WSM_HOME": os.path.join(self.tmp, "wsm"), "HOME": os.path.join(self.tmp, "home"),
                    "XDG_CONFIG_HOME": os.path.join(self.tmp, "home", ".config")}
        os.makedirs(self.env["HOME"])
        self.saved_env = {k: os.environ.get(k) for k in self.env}
        self.saved_run = mirror.run
        self.procs = []

    def fake_run(self, cmd, **kw):
        if cmd[0] == "pkcheck":
            self.calls.append(cmd)
            self.kws.append(kw)
            if self.pkcheck == "timeout":
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            if self.pkcheck is None:
                return subprocess.CompletedProcess(cmd, 127, "", "pkcheck is not installed on this computer")
            return subprocess.CompletedProcess(cmd, self.pkcheck, "", "")
        if cmd[0] in ("defaults", "fdesetup"):
            self.calls.append(cmd)
            if cmd[0] == "defaults":
                return subprocess.CompletedProcess(cmd, 0 if self.auto_login else 1, (self.auto_login or "") + "\n", "")
            return subprocess.CompletedProcess(cmd, 0, f"FileVault is {'On' if self.filevault else 'Off'}.\n", "")
        if cmd[0] in ("systemctl", "loginctl", "launchctl"):
            self.calls.append(cmd)
            if cmd[:2] == ["loginctl", "show-user"]:
                return subprocess.CompletedProcess(cmd, 0, f"Linger={self.linger}\n", "")
            if self.forge_follows:
                label = f"gui/{os.getuid()}/{mirror.LAUNCHD_LABEL}"
                if cmd in (["systemctl", "--user", "stop", mirror.SERVICE], ["launchctl", "bootout", label]):
                    for pr in self.procs:
                        if pr.poll() is None:
                            pr.terminate()
                            pr.wait(30)
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                if cmd == ["systemctl", "--user", "start", mirror.SERVICE] or (
                        cmd[:2] == ["launchctl", "bootstrap"] and cmd[-1] == mirror.paths()["plist"]):
                    self.start_forge()
                    return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[:3] == ["systemctl", "--user", "show"]:
                if self.timer_next is None:
                    return subprocess.CompletedProcess(cmd, 0, "ActiveState=inactive\nNextElapseUSecRealtime=\n", "")
                return subprocess.CompletedProcess(cmd, 0, f"ActiveState=active\nNextElapseUSecRealtime={self.timer_next}\n"
                                                            f"NextElapseUSecMonotonic={self.timer_mono}\n", "")
            if cmd[:2] == ["launchctl", "print"]:
                if self.timer_next is None:
                    return subprocess.CompletedProcess(cmd, 113, "", "Could not find service")
                return subprocess.CompletedProcess(cmd, 0, "state = waiting\n\trun interval = 900 seconds\n", "")
            if cmd[:2] == ["loginctl", "disable-linger"]:
                self.linger = "no"
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if [c for c in cmd if c != "--no-ask-password"][:2] == ["loginctl", "enable-linger"]:
                if self.enable_ok:
                    self.linger = "yes"
                    return subprocess.CompletedProcess(cmd, 0, "", "")
                return subprocess.CompletedProcess(cmd, 1, "", "Access denied")
            return subprocess.CompletedProcess(cmd, 0 if self.service_ok else 1, "", "" if self.service_ok else "Failed to connect to bus")
        return self.saved_run(cmd, **kw)

    def __enter__(self):
        os.environ.update(self.env)
        mirror.run = self.fake_run
        self.saved_wait, mirror.SETUP_WAIT = mirror.SETUP_WAIT, 0     # no forge follows a stand-in service start
        return self

    def __exit__(self, *a):
        mirror.SETUP_WAIT = self.saved_wait
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

    def setup(self, *args, choose=True):
        """choose: the person already said yes to lingering (--linger), unless the test says otherwise."""
        if choose and not {"--dry-run", "--linger", "--no-linger"} & set(args):
            args = (*args, "--linger")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mirror.main(["setup", *args])
        return rc, out.getvalue()

    def start_forge(self, seconds=None):
        """Start the forge and wait for its first answer, as long as setup itself waits (G1.9). If it stops or does
        not answer, the failure carries the forge's own last log lines, not only that it did not answer."""
        seconds = mirror.FIRST_START_WAIT if seconds is None else seconds
        p = mirror.paths()
        script = p["script"] if os.path.exists(p["script"]) else SCRIPT
        pr = subprocess.Popen([sys.executable, "-I", script, "run-forge"], env=dict(os.environ),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(pr)
        STARTED.append(pr)
        port = int(mirror.ini_value(p["app_ini"], "server", "HTTP_PORT"))
        end = time.monotonic() + seconds
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/healthz", timeout=2)
                return port, pr
            except (urllib.error.URLError, OSError):
                if pr.poll() is not None:
                    raise AssertionError("the forge stopped: " + pr.stdout.read()[-400:] + forge_log_tail(p))
                if time.monotonic() > end:
                    raise AssertionError(f"the forge did not answer in {seconds} s" + forge_log_tail(p))
                time.sleep(0.5)


KEY_BEFORE_SETUP_SOMEONE = ("Setup comes first, and it has not run on this computer: setup is your agent's step (or run setup "
                    "yourself), then run key set someone again. No key was read, and nothing was kept.")


def forge_log_tail(p, n=8):
    """The forge's own last log lines, for a failure message (G1.9)."""
    f = os.path.join(p["forge"], "log", "gitea.log")
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()[-n:]
    except OSError:
        return f"\n(the forge's log {f} is not there)"
    return f"\nthe forge's own last log lines ({f}):\n" + "\n".join(lines)


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
            # Actions are off for the whole forge (C10). A project's Actions cannot be turned on, even when asked,
            # and the runner service is not served at all, so no runner can join, even holding a registration
            # token (this Gitea still hands one out). With only [actions] ENABLED=true, the same Ping answers 200,
            # a runner registers and has_actions turns on (research log of 2026-11-23).
            def send(method, path, data, auth=True):
                r = urllib.request.Request(base + path, method=method, data=json.dumps(data).encode(),
                                           headers={"Content-Type": "application/json",
                                                    **({"Authorization": f"token {key}"} if auth else {})})
                try:
                    with urllib.request.urlopen(r, timeout=10) as resp:
                        return resp.status, json.loads(resp.read().decode() or "null")
                except urllib.error.HTTPError as err:
                    return err.code, None
            user = json.loads(http_get(base + "/api/v1/user", key)[2])["login"]
            self.assertEqual(send("POST", "/api/v1/user/repos", {"name": "c10", "private": True})[0], 201)
            code, repo = send("PATCH", f"/api/v1/repos/{user}/c10", {"has_actions": True})
            self.assertEqual(code, 200)
            self.assertFalse(repo["has_actions"], "a project's Actions could be turned on")
            for svc, body in (("ping.v1.PingService/Ping", {"data": "x"}),
                              ("runner.v1.RunnerService/Register", {"name": "r", "token": "x", "labels": ["x"]})):
                self.assertEqual(send("POST", "/api/actions/" + svc, body, auth=False)[0], 404,
                                 f"the forge serves the runner service ({svc})")
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
            reads = (["loginctl", "show-user"], ["defaults", "read"], ["fdesetup", "status"],
                     ["pkcheck", "--action-id"])                                                  # read-only checks
            changed = [c for c in h.calls if c[:2] not in reads and c[:3] != ["systemctl", "--user", "show-environment"]]
            self.assertFalse(changed, "the dry run ran a command that changes something")
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
                self.assertIn(mirror.NO_LOGIN_NOTE, out)       # each Mac case, FileVault's too: the G1.7 test
            return
        with Home(linger="no", enable_linger_ok=False) as h:      # the system refuses: an administrator's step
            h.place_gitea()
            rc, out = h.setup()
            self.assertEqual(rc, 0, out)
            self.assertIn(mirror.NO_LOGIN_NOTE, out)
            self.assertIn(f"sudo loginctl enable-linger {mirror.getpass.getuser()}", out)
            self.assertIn(["loginctl", "--no-ask-password", "enable-linger", mirror.getpass.getuser()], h.calls)
            rc, out = h.setup("--dry-run")
            self.assertIn(mirror.NO_LOGIN_NOTE, out)
        with Home(linger="no", enable_linger_ok=True) as h:       # the person may turn it on: setup does
            h.place_gitea()
            rc, out = h.setup()
            self.assertIn("After a restart it starts by itself.", out)
            self.assertNotIn(mirror.NO_LOGIN_NOTE, out)


    def test_G1_6_on_a_mac_it_comes_back_by_itself_only_with_the_systems_own_login(self):
        saved = mirror.system
        mirror.system = lambda: ("darwin", "arm64")
        try:
            with Home() as h:
                me = mirror.getpass.getuser()
                h.auto_login, h.filevault = me, False      # what the Mac test box had on 2026-11-22
                self.assertEqual(mirror.comes_back_by_itself(), (True, ""))
                for auto, vault in ((None, False), ("someone-else", False), (me, True)):
                    h.auto_login, h.filevault = auto, vault
                    back, note = mirror.comes_back_by_itself()
                    self.assertFalse(back, (auto, vault))
                    self.assertIn(mirror.NO_LOGIN_NOTE, note)
                    self.assertEqual("(FileVault is on)" in note, vault)
                    self.assertEqual("another account" in note, auto == "someone-else")
                    self.assertNotIn("someone-else", note)      # another person's account name is never said
                self.assertFalse([c for c in h.calls if c[0] not in ("defaults", "fdesetup")], "it changed something")
        finally:
            mirror.system = saved

    def test_G1_8_a_computer_without_git_is_told_before_anything_is_downloaded(self):
        """Seen on a stock Ubuntu 24.04 with git removed: setup downloaded Gitea, then stopped on Gitea's own words."""
        with Home() as h:
            which, fetch = mirror.shutil.which, mirror.fetch_forge
            mirror.shutil.which = lambda c, *a, **k: None if c == "git" else which(c, *a, **k)
            mirror.fetch_forge = lambda *a, **k: (_ for _ in ()).throw(AssertionError("setup downloaded Gitea"))
            try:
                for args in (["--dry-run"], []):
                    rc, out = h.setup(*args)
                    self.assertEqual(rc, 2, out)
                    self.assertIn("git is not installed on this computer", out)
                    self.assertIn("sudo apt update && sudo apt install git", out)
                    self.assertIn("Nothing was changed", out)
                self.assertFalse(os.path.exists(mirror.paths()["app_ini"]), "setup wrote settings with no git")
            finally:
                mirror.shutil.which, mirror.fetch_forge = which, fetch
        with Home() as h:                                             # git only where the service never looks
            which, fetch = mirror.shutil.which, mirror.fetch_forge
            mirror.shutil.which = (lambda c, *a, **k: (None if k.get("path") else "/home/someone/bin/git")
                                   if c == "git" else which(c, *a, **k))
            mirror.fetch_forge = lambda *a, **k: (_ for _ in ()).throw(AssertionError("setup downloaded Gitea"))
            try:
                for args in (["--dry-run"], []):
                    rc, out = h.setup(*args)
                    self.assertEqual(rc, 2, out)
                    self.assertIn("git is at /home/someone/bin/git, in a folder the mirror's service does not look in",
                                  out)
                    self.assertIn("Nothing was changed", out)
                if mirror.system()[0] == "linux":                     # a Mac's launchd PATH is fixed, not asked
                    self.assertIn(["systemctl", "--user", "show-environment"], h.calls)
                self.assertFalse(os.path.exists(mirror.paths()["app_ini"]))
            finally:
                mirror.shutil.which, mirror.fetch_forge = which, fetch
        r = mirror.run(["wsm-a-program-that-is-not-here"])            # never a traceback
        self.assertEqual((r.returncode, "not installed" in r.stderr), (127, True))
        with Home() as h:
            lfs = mirror.has_git_lfs
            mirror.has_git_lfs = lambda: False
            try:
                rc, out = h.setup("--dry-run")
            finally:
                mirror.has_git_lfs = lfs
            self.assertIn("git lfs is not installed here", out)

        # Every install command the tool names was run as written on a fresh system: it is in the plan's table.
        # (Seen: the plain apt lines fail on fresh Ubuntu and Debian images; brew failed on an older Mac.)
        plan, readme = fread(os.path.join(ROOT, "DNA-STRAND-MASTER-PLAN.md")), fread(os.path.join(ROOT, "README.md"))
        table = plan.split("### Install lines, as seen", 1)[1].split("Lines tried and NOT used", 1)[0]
        cmd = r"(?:sudo apt update && )?sudo (?:apt|dnf) install [^;`)\n]+|xcode-select --install|brew install [^;`)\n]+"
        for text, where in ((mirror.INSTALL_GIT + "; " + mirror.INSTALL_LFS, "the install lines"),
                            (fread(SCRIPT), "the script"), (readme, "README.md")):
            named = {c.strip().rstrip(".,") for c in re.findall(cmd, text)}
            if where == "the install lines":
                self.assertEqual(len(named), 5, named)
            for line in sorted(named):
                self.assertIn(f"`{line}`", table, f"{where} names a line never seen on a fresh system: {line}")
            self.assertNotIn("brew install", text, where)
            self.assertIsNone(re.search(r"(?<!&& )sudo apt install", text), f"an apt line without apt update in {where}")
        self.assertIn("https://git-lfs.com", mirror.INSTALL_LFS)

    def test_G1_10_setup_says_the_forge_runs_only_once_it_answers(self):
        with Home() as h:
            h.place_gitea()
            saved = mirror.wait_forge
            try:
                mirror.wait_forge = lambda port, up=True, seconds=60: False
                rc, out = h.setup()
                self.assertIn("not answering yet", out)
                self.assertNotIn("The forge runs as your own service", out)
                mirror.wait_forge = lambda port, up=True, seconds=60: True
                rc, out = h.setup()
                self.assertIn("The forge runs as your own service", out)
            finally:
                mirror.wait_forge = saved

    def test_G1_12_an_older_python_is_told_first_and_nothing_is_made(self):
        """Rule 11 (the family's): the lowest Python the skill names is checked as its first act; below it the tool
        stops with the version found and what it needs, and has made nothing; the file stays readable by the older
        grammar so that line is reached. Whatever floor is named, CI runs it."""
        import ast
        src = fread(SCRIPT)
        tree_ = ast.parse(src, feature_version=(3, 8))         # an older Python reaches the check
        body = tree_.body
        self.assertIsInstance(body[0], ast.Expr)                # the module's own description
        self.assertEqual([a.name for a in body[1].names], ["sys"], "the first act is not the version check")
        self.assertIsInstance(body[2], ast.If)
        self.assertEqual(ast.unparse(body[2].test), "sys.version_info < (3, 9)")
        self.assertEqual(ast.unparse(body[2].body[-1]), "sys.exit(3)")
        # Below the floor, for every command: the stop line, exit 3, nothing made.
        with tempfile.TemporaryDirectory() as home:
            for cmd in (["setup"], ["setup", "--dry-run"], ["sync"], ["status"], ["uninstall"]):
                code = ("import sys; sys.version_info = (3, 8, 20, 'final', 0); sys.argv = [%r] + %r; "
                        "exec(compile(open(%r).read(), %r, 'exec'), {'__name__': '__main__'})"
                        % (SCRIPT, cmd, SCRIPT, SCRIPT))
                out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30,
                                     env=dict(os.environ, HOME=home, WSM_HOME=os.path.join(home, "wsm")))
                self.assertEqual(out.returncode, 3, (cmd, out.stdout, out.stderr))
                self.assertTrue(out.stdout.startswith("Stopped: this is Python 3.8.20 ("), out.stdout)
                self.assertIn("this skill needs Python 3.9 or newer. Nothing was made.", out.stdout)
                self.assertEqual(os.listdir(home), [], f"{cmd}: something was made below the floor")
        # Where a real older Python is here (WSM_OLD_PYTHON, or python3.8 on the PATH), it is run for real.
        old = os.environ.get("WSM_OLD_PYTHON") or shutil.which("python3.8")
        if old:
            with tempfile.TemporaryDirectory() as home:
                out = subprocess.run([old, "-I", SCRIPT, "setup"], capture_output=True, text=True, timeout=30,
                                     env=dict(os.environ, HOME=home, WSM_HOME=os.path.join(home, "wsm")))
                self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
                self.assertIn("this skill needs Python 3.9 or newer. Nothing was made.", out.stdout)
                self.assertEqual(os.listdir(home), [])
        # CI runs the floor it names, on runner images it names.
        wf = fread(os.path.join(ROOT, ".github", "workflows", "tests.yml"))
        self.assertIn('{os: ubuntu-24.04, python: "3.9"}', wf)
        self.assertIn('{os: macos-26, python: "3.9"}', wf)
        self.assertNotRegex(wf, r"runs-on:.*-latest|os: [a-z]+-latest", "a job runs on an image nobody named")

    def test_G1_11_the_first_start_waits_are_measured_and_said(self):
        # The waits come from the figure measured on a slow machine, and the plan says where it was measured.
        self.assertGreaterEqual(mirror.FIRST_START_WAIT, 3 * mirror.FIRST_START_SEEN)
        plan = fread(os.path.join(ROOT, "DNA-STRAND-MASTER-PLAN.md"))
        notes = plan.split("### First start, as measured", 1)[1].split("\n## ", 1)[0]
        self.assertIn("1 processor, 512 MB", notes)
        self.assertIn(f"`FIRST_START_SEEN` is {mirror.FIRST_START_SEEN} and `FIRST_START_WAIT` is "
                      f"{mirror.FIRST_START_WAIT}", notes)
        self.assertIn(f"| {mirror.FIRST_START_SEEN} at most", notes)
        src = fread(SCRIPT)
        self.assertEqual(mirror.SETUP_WAIT, mirror.FIRST_START_WAIT)
        self.assertNotRegex(src, r"wait_forge\([^)]*up=True, seconds=\d", "a first-start wait that is not the measured one")
        self.assertEqual(inspect.signature(Home.start_forge).parameters["seconds"].default, None)
        self.assertIn("seconds = mirror.FIRST_START_WAIT if seconds is None", inspect.getsource(Home.start_forge))
        # A test that waits for a forge that never answers fails with the forge's own last log lines.
        with Home() as h:
            p = mirror.paths()
            os.makedirs(os.path.dirname(p["app_ini"]), exist_ok=True)
            os.makedirs(os.path.join(p["forge"], "log"), exist_ok=True)
            mirror.write_file(p["app_ini"], f"[server]\nHTTP_PORT = {mirror.free_port()}\n", 0o600)
            os.makedirs(os.path.dirname(p["bin"]), exist_ok=True)
            log = os.path.join(p["forge"], "log", "gitea.log")
            with open(p["bin"], "w") as f:
                f.write(f"#!/bin/sh\necho 'PING DATABASE sqlite3: held up by the stand-in' >> '{log}'\nexec sleep 30\n")
            os.chmod(p["bin"], 0o755)
            with self.assertRaises(AssertionError) as e:
                h.start_forge(seconds=3)
            self.assertIn("the forge did not answer in 3 s", str(e.exception))
            self.assertIn("PING DATABASE sqlite3: held up by the stand-in", str(e.exception))
        # (needs the real Gitea: setup prepares its database)
        # Setup says what "not answering yet" means, when to look again, and where the forge's own words are.
        with Home() as h:
            h.place_gitea()
            saved = mirror.wait_forge
            try:
                mirror.wait_forge = lambda port, up=True, seconds=60: False
                rc, out = h.setup()
            finally:
                mirror.wait_forge = saved
            p = mirror.paths()
            self.assertIn(f"not answering yet on http://127.0.0.1:{mirror.load_json(p['state'], {})['port']}/ after "
                          f"{mirror.SETUP_WAIT} s", out)
            self.assertIn(f"it answered within {mirror.FIRST_START_SEEN} s", out)
            self.assertIn("run `status` in 5 minutes", out)
            self.assertIn(os.path.join(p["forge"], "log", "gitea.log"), out)

    def test_G1_9_lingering_is_said_first_asked_of_this_computer_and_undone_only_if_setup_did_it(self):
        saved, saved_proc = mirror.system, mirror.own_process_for_polkit
        mirror.system = lambda: ("linux", "x86_64")
        if not os.path.exists("/proc/self/stat"):        # a Mac runner standing in for Linux has no /proc to read
            mirror.own_process_for_polkit = lambda: f"{os.getpid()},1,{os.getuid()}"
        me = mirror.getpass.getuser()
        try:
            # (a, b) The dry run says what lingering means, from this computer's own three answers.
            for code, said in ((0, "This computer lets you do that yourself"), (1, "administrator runs one command"),
                               (2, "administrator runs one command"), (127, "so setup --linger tries"),
                               (None, "so setup --linger tries"), ("timeout", "so setup --linger tries")):
                with Home(linger="no") as h:
                    h.pkcheck = code
                    rc, out = h.setup("--dry-run")
                    self.assertEqual(rc, 0, out)
                    self.assertIn(mirror.LINGER_MEANS, out, code)
                    self.assertIn(said, out, code)
                    if code != 0:
                        self.assertIn(f"sudo loginctl enable-linger {me}", out, code)
                    asked = [c for c in h.calls if c[0] == "pkcheck"]
                    self.assertEqual(len(asked), 1, code)
                    self.assertEqual(asked[0][:4], ["pkcheck", "--action-id", "org.freedesktop.login1.set-self-linger",
                                                    "--process"])
                    self.assertRegex(asked[0][4], r"^\d+,\d+,\d+$")     # pid,pid-start-time,uid, as its manual asks
                    self.assertEqual(len(asked[0]), 5, "pkcheck may never be allowed to ask for a password")
                    self.assertEqual(h.kws[0].get("timeout"), 10)
                    self.assertFalse([c for c in h.calls if "enable-linger" in c], "the dry run switched lingering on")
            # (c) Setup switches it on without a password prompt and keeps a record; uninstall undoes only that.
            with Home(linger="no", enable_linger_ok=True) as h:
                st = {}
                back, note = mirror.linger_step(st)
                self.assertTrue(back)
                self.assertEqual(st["linger_by_setup"], True)
                self.assertIn(mirror.LINGER_MEANS, note)
                self.assertIn(["loginctl", "--no-ask-password", "enable-linger", me], h.calls)
                mirror.linger_step(st)                                   # a second setup keeps the record
                self.assertEqual(st["linger_by_setup"], True)
                p = mirror.paths()
                os.makedirs(os.path.dirname(p["state"]), exist_ok=True)
                mirror.save_state(p, st)
                rc, out = quiet(mirror.main, ["uninstall"])
                self.assertIn("Lingering was switched on by setup, and is switched off again.", out)
                self.assertEqual(h.linger, "no")
                self.assertNotIn("linger_by_setup", mirror.load_json(p["state"], {}))
            with Home(linger="yes") as h:
                st = {}
                self.assertTrue(mirror.linger_step(st)[0])
                self.assertEqual(st["linger_by_setup"], False)
                p = mirror.paths()
                os.makedirs(os.path.dirname(p["state"]), exist_ok=True)
                mirror.save_state(p, st)
                rc, out = quiet(mirror.main, ["uninstall"])
                self.assertIn("Lingering was already on before setup, so it is left on.", out)
                self.assertEqual(h.linger, "yes")
                self.assertFalse([c for c in h.calls if "disable-linger" in c])
            # Refused: the administrator's one command, never a dialog.
            with Home(linger="no", enable_linger_ok=False) as h:
                back, note = mirror.linger_step({})
                self.assertFalse(back)
                self.assertIn(f"administrator runs one command: sudo loginctl enable-linger {me}", note)
            # (e) Plain setup, lingering off: nothing changes until the person has said yes or no (--linger or
            # --no-linger); not even a download. With lingering already on, there is nothing to choose.
            for may in (0, 1):
                with Home(linger="no", enable_linger_ok=True) as h:
                    h.pkcheck = may
                    fetch = mirror.fetch_forge
                    mirror.fetch_forge = lambda *a, **k: (_ for _ in ()).throw(AssertionError("setup downloaded"))
                    try:
                        rc, out = h.setup(choose=False)
                    finally:
                        mirror.fetch_forge = fetch
                    self.assertEqual(rc, 2, out)
                    self.assertIn(mirror.LINGER_MEANS, out)
                    self.assertIn("Needs you: lingering is the person's choice", out)
                    self.assertIn("--linger", out)
                    self.assertIn("--no-linger", out)
                    self.assertFalse([c for c in h.calls if "enable-linger" in c], "plain setup switched lingering on")
                    self.assertFalse(os.path.exists(mirror.paths()["app_ini"]), "plain setup changed something")
                    if may == 1:
                        self.assertIn(f"sudo loginctl enable-linger {me}", out)
            ns = mirror.argparse.Namespace
            with Home(linger="no") as h:
                self.assertTrue(mirror.linger_unchosen(ns(), {}))
                self.assertFalse(mirror.linger_unchosen(ns(), {"linger_said": "no"}))   # a no is kept for next time
                self.assertFalse(mirror.linger_unchosen(ns(linger=True), {}))
            with Home(linger="yes") as h:
                self.assertFalse(mirror.linger_unchosen(ns(), {}))
            # (d) The person says no: setup still works, and says what that costs.
            with Home(linger="no", enable_linger_ok=True) as h:
                st = {}
                back, note = mirror.linger_step(st, allow=False)
                self.assertFalse(back)
                self.assertIn("syncs only while you are logged in", note)
                self.assertFalse([c for c in h.calls if "enable-linger" in c])
                self.assertNotIn("linger_by_setup", st)
                rc, out = h.setup("--dry-run", "--no-linger")
                self.assertIn("syncs only while you are logged in", out)
        finally:
            mirror.system, mirror.own_process_for_polkit = saved, saved_proc

    def test_G1_13_a_mac_is_written_as_what_was_seen(self):
        """The family's rule on Macs (2026-11-23): we have no stock Mac. A Mac sighting is written as what it was, a
        Mac that carries Apple's developer tools, and in which account; nothing of ours says what a Mac without
        those tools shows, because nobody here has seen it."""
        texts = {"the script": fread(SCRIPT), "SKILL.md": fread(SKILL), "README.md": fread(os.path.join(ROOT, "README.md")),
                 "the plan": fread(os.path.join(ROOT, "DNA-STRAND-MASTER-PLAN.md")),
                 "PROMISES.json": fread(os.path.join(ROOT, "PROMISES.json")),
                 "CHANGELOG.md": fread(os.path.join(ROOT, "CHANGELOG.md"))}
        for where, text in texts.items():
            flat = " ".join(text.replace('" "', "").split())
            for sentence in re.findall(r"[^.!?|]*\b(?:Macs?|macOS)\b[^.!?|]*", flat):
                self.assertIsNone(re.search(r"\bstock\b", sentence), f"{where} calls a Mac stock: {sentence[:160]}")
            for sentence in re.findall(r"[^.;|]*xcode-select --install[^.;|]*", flat):
                self.assertIsNone(re.search(r"\bopens\b|installer|window|dialog|at the screen|asks you", sentence),
                                  f"{where} describes what a Mac without the tools shows: {sentence[:160]}")
        self.assertIn("on a Mac: git comes with Apple's developer tools, and xcode-select --install is Apple's "
                      "command for them", mirror.INSTALL_GIT)
        # Where the README lists what was seen (a release branch), each Mac seen outside GitHub's runners is
        # written as what it was.
        readme = texts["README.md"]
        if "## Seen to work" in readme:
            seen = readme.split("## Seen to work", 1)[1].split("\n## ", 1)[0]
            macs = [b for b in seen.split("\n- ")[1:] if re.search(r"\b(?:Mac|macOS)\b", b)
                    and "runners" not in b and not b.startswith("**Not seen")]
            self.assertTrue(macs, "no Mac sighting found under Seen to work")
            for b in macs:
                b = " ".join(b.split())
                self.assertIn("a Mac that carries Apple's developer tools", b)
                self.assertRegex(b, r"\baccount\b")


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

    def key_set(self, owner, key, after_setup=True):
        if after_setup and not mirror.load_json(mirror.paths()["state"], {}).get("port"):
            mirror.save_state(mirror.paths(), {"port": 1})    # what setup leaves; these tests are about the keys
        out, saved = io.StringIO(), sys.stdin
        sys.stdin = io.StringIO(key + "\n")
        try:
            with contextlib.redirect_stdout(out):
                rc = mirror.main(["key", "set", owner])
        finally:
            sys.stdin = saved
        return rc, out.getvalue()

    def test_G2_6_a_key_is_never_taken_before_setup(self):
        """The cold start's run B told the person to run key set before setup. Setup comes first: before it, key set
        reads no key and keeps nothing, so every key lands where uninstall and the key scan already look."""
        with Home():
            p = mirror.paths()
            saved = sys.stdin
            sys.stdin = io.StringIO(self.gh.key + "\n")
            try:
                rc, out = quiet(mirror.main, ["key", "set", "someone"])
                left = sys.stdin.read()
            finally:
                sys.stdin = saved
            self.assertEqual(rc, 2, out)
            self.assertIn(KEY_BEFORE_SETUP_SOMEONE,
                          out)
            self.assertEqual(left, self.gh.key + "\n", "the key was read before the refusal")
            self.assertFalse(os.path.exists(mirror.key_path(p, "someone")))
            self.assertNotIn("owners", mirror.load_json(p["settings"], {}))
            self.assertEqual(self.gh.log, [], "GitHub was asked before setup")
            rc, out = self.key_set("someone", self.gh.key)       # after setup, the same step keeps it
            self.assertEqual(rc, 0, out)
            self.assertTrue(os.path.exists(mirror.key_path(p, "someone")))

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
            self.setup_out = out
            self.port, _ = self.home.start_forge()
            self.home.forge_follows = True
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

    def forge_push(self, cache, name, *specs):
        key = fread(self.p["mirror_key"]).strip()
        hdr = "Authorization: Basic " + __import__("base64").b64encode(f"mirror:{key}".encode()).decode()
        env = dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"http.http://127.0.0.1:{self.port}/.extraheader",
                   GIT_CONFIG_VALUE_0=hdr, GIT_TERMINAL_PROMPT="0")
        subprocess.run(["git", "--git-dir", cache, "push", "-q", f"http://127.0.0.1:{self.port}/mirror/{name}.git", *specs],
                       env=env, check=True, capture_output=True)

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
    def test_G3_10_the_page_size_is_githubs_largest_unless_a_test_or_lab_sets_it(self):
        """A real owner with few repositories has one page; to see paging (M5) and the floor (M9) there, a lab
        makes the page small with WSM_PAGE_SIZE. Nothing a person does sets it, and only 1 to 100 is taken."""
        asked = []
        saved, env = mirror.gh_get, os.environ.get("WSM_PAGE_SIZE")
        mirror.gh_get = lambda key, url, *a, **k: (asked.append(url), (500, {}, None))[1]
        try:
            for val, want in ((None, 100), ("3", 3), ("1", 1), ("100", 100), ("0", 100), ("101", 100), ("x", 100),
                              ("-5", 100)):
                os.environ.pop("WSM_PAGE_SIZE", None)
                if val is not None:
                    os.environ["WSM_PAGE_SIZE"] = val
                for kind in ("user", "org"):
                    asked.clear()
                    mirror.list_owner("someone", kind, "k")
                    self.assertIn(f"per_page={want}", asked[0], (val, kind))
        finally:
            mirror.gh_get = saved
            os.environ.pop("WSM_PAGE_SIZE", None)
            if env is not None:
                os.environ["WSM_PAGE_SIZE"] = env
        self.assertNotIn("WSM_PAGE_SIZE", fread(SKILL))
        self.assertNotIn("WSM_PAGE_SIZE", fread(os.path.join(ROOT, "README.md")))

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
            self.assertIn("left out: 1 by your exclude patterns; their names are not shown here (to see them, run: "
                          "left-out).", out)
            self.assertNotIn("secret-notes", out)
            rc, asked = quiet(mirror.main, ["left-out"])
            self.assertIn("left out: someone/secret-notes (left out by 'someone/secret-*')", asked)
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
            self.assertIn("left out: 5 by your include list; their names", out)
            self.assertNotIn("someone/later", out)
            rc, asked = quiet(mirror.main, ["left-out"])
            self.assertIn("left out: someone/later (not in include)", asked)

    def test_G3_11_what_is_left_out_is_a_count_and_its_names_come_only_when_asked(self):
        """Five cold starts (2026-11-23): every plain sync printed the name of each project the include list left
        out, so every agent transcript held names the person had chosen to leave out. The tool names what it copies;
        what is left out is a count, and its names come only from left-out, which the count line names."""
        with World(self) as w:
            for n in ("alpha", "beta", "hidden-one", "hidden-two"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"a": b"1"})
            w.key_set()
            quiet(mirror.main, ["config", "exclude", "someone/hidden-*"])
            outs = [quiet(mirror.main, ["setup", "--dry-run"])[1]]
            rc, out = w.sync("--announce")
            outs.append(out)
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            outs.append(out)
            self.assertIn("left out: 2 by your exclude patterns; their names are not shown here (to see them, run: "
                          "left-out).", out)
            quiet(mirror.main, ["config", "include", "someone/alpha"])
            rc, out = w.sync()
            outs.append(out)
            self.assertIn("left out: 3 by your include list; their names are not shown here", out)
            outs += [quiet(mirror.main, ["status"])[1], quiet(mirror.main, ["restore", "--check"])[1]]
            for o in outs:
                for hidden in ("hidden-one", "hidden-two"):
                    self.assertNotIn(hidden, o, "a left-out name was printed without being asked for")
            self.assertNotIn("someone/beta", outs[-3] + outs[-2] + outs[-1])
            before = sorted(r["name"] for r in w.forge_get("/user/repos")[1])
            rc, asked = quiet(mirror.main, ["left-out"])
            self.assertEqual(rc, 0, asked)
            for line in ("left out: someone/beta (not in include)", "left out: someone/hidden-one (not in include)",
                         "left out: someone/hidden-two (not in include)"):
                self.assertIn(line, asked)
            self.assertNotIn("someone/alpha", asked)
            self.assertEqual(asked.strip().splitlines()[-1], "left-out: 3 left out. Nothing was copied or changed.")
            self.assertEqual(sorted(r["name"] for r in w.forge_get("/user/repos")[1]), before)

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
            self.assertIn("Nothing was copied. When the person says go, run: sync", out)
            self.assertEqual(w.forge_get("/user/repos")[1], [], "the announcement copied something")
            # Seen in a cold start (P1a run B, 2026-11-23): an agent ran --announce again "to re-check", and the
            # tool made the first copy without the go. Every later announcement says it all again, copies nothing.
            for again in (2, 3):
                rc, out = w.sync("--announce")
                self.assertEqual(rc, 0, out)
                self.assertIn("The first copy: 5 repositories", out)
                self.assertIn("Nothing was copied.", out)
                self.assertEqual(w.forge_get("/user/repos")[1], [], f"announcement {again} copied something")
            rc, out = w.sync("--from-timer")
            self.assertEqual(rc, 0, out)
            self.assertIn("waiting for the person's go", out)
            self.assertEqual(w.forge_get("/user/repos")[1], [], "the timer copied before the go")
            st = mirror.load_json(w.p["state"], {})
            self.assertTrue(st.get("announced"))
            self.assertNotIn("go", st)
            self.assertIn("first copy: waiting for your go", quiet(mirror.main, ["status"])[1])
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertRegex(out, r"first copy: 3 of 5 done; about \d+ min left at this pace")
            go = mirror.load_json(w.p["state"], {})["go"]
            self.assertRegex(go, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertIn(f"first copy: made on {mirror.local_time(go)}, on your go",
                          quiet(mirror.main, ["status"])[1])
            rc, out = w.sync()
            self.assertNotIn("first copy:", out)
            before = w.forge_refs("someone--r1")
            w.gh.commit("someone/r1", {"b": b"2"})
            rc, out = w.sync("--announce")
            self.assertEqual(rc, 0, out)
            self.assertIn("The first copy was made on", out)
            self.assertNotIn("The first copy: 5", out)
            self.assertEqual(mirror.load_json(w.p["state"], {})["go"], go, "an announcement moved the go")
            self.assertEqual(w.forge_refs("someone--r1"), before, "an announcement after the go copied")

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
            gone = f"problem: someone/big: {mirror.LFS_NOT_IN_COPY}"
            self.assertIn(gone, out)
            self.assertIn("1 copy without its large files", out)
            # Seen in a cold start (P1a run A, 2026-11-23): git lfs installed after the first copy, and the next
            # sync skipped the project as unchanged until the daily full pass, while status said no problem.
            mirror.has_git_lfs = lambda: False
            try:
                rc, out = quiet(mirror.main, ["status"])
                self.assertIn(gone, out)
                self.assertLess(out.index(gone), out.index("copies:"), "the worst true thing is not said first")
                self.assertIn("1 without its large files", out)
                rc, out = quiet(mirror.main, ["restore", "--check"])
                self.assertEqual(rc, 1, out)
                self.assertIn(f"someone/big: {mirror.LFS_NOT_IN_COPY}", out)
            finally:
                mirror.has_git_lfs = saved
            st = mirror.load_json(w.p["state"], {})
            self.assertTrue(st.get("last_full_pass"), "the first copy was not a full pass")
            rc, out = w.sync()                          # git lfs is here now; no full pass is due
            self.assertEqual(rc, 0, out)
            self.assertNotIn(", a full pass", out)
            self.assertIn("1 copied now, 0 unchanged", out)
            self.assertNotIn("without its large files", out)
            req = urllib.request.Request(f"http://127.0.0.1:{w.port}/api/v1/repos/mirror/someone--big/media/blob.bin",
                                         headers={"Authorization": f"token {fread(w.p['mirror_key']).strip()}"})
            self.assertEqual(urllib.request.urlopen(req, timeout=10).read(), data)
            rc, out = quiet(mirror.main, ["status"])
            self.assertNotIn("large files", out)
            rc, out = quiet(mirror.main, ["restore", "--check"])
            self.assertEqual(rc, 0, out)
            self.assertIn("1 copies, 1 whole, 0 difference(s)", out)



    def test_G3_9_the_go_copies_only_what_the_last_announcement_named(self):
        """The person says yes to a list; "seven projects" must not become seventy between the yes and the copy,
        whoever changed the settings in between (a cold start saw the include list kept only by the person's care).
        The announcement names the projects; the first copy copies exactly the last one announced, or nothing."""
        def told(out):
            return [x.split(": ", 1)[1] for x in out.splitlines() if x.startswith("tell the person ")]

        with World(self) as w:
            for n in ("a", "b", "c"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"f": n.encode()})
            w.key_set()
            self.assertEqual(quiet(mirror.main, ["config", "include", "someone/a", "someone/b"])[0], 0)
            rc, out = w.sync("--announce")
            self.assertEqual(rc, 0, out)
            self.assertIn("They are: someone/a, someone/b.", told(out))
            lst = w.p["first_copy_list"]
            self.assertEqual(fread(lst), "someone/a\nsomeone/b\n")
            self.assertEqual(os.stat(lst).st_mode & 0o777, 0o600)

            def refused(why, counts):
                rc, out = w.sync()
                self.assertEqual(rc, 2, f"{why}: {out}")
                self.assertIn(f"Needs you: the first copy would not be what the person was told ({counts} than the "
                              "last announcement). Nothing was copied.", out)
                self.assertEqual(w.forge_get("/user/repos")[1], [], f"{why}: copied after a change")
                self.assertNotIn("go", mirror.load_json(w.p["state"], {}), f"{why}: a go recorded")
                self.assertTrue(out.rstrip().endswith("when they say go to this list, run: sync"), out)
                return told(out)

            # 1. The include list grows between the announcement and the go.
            quiet(mirror.main, ["config", "include", "someone/a", "someone/b", "someone/c"])
            self.assertIn("They are: someone/a, someone/b, someone/c.", refused("include list", "1 more and 0 fewer"))
            # 2. A project appears on GitHub.
            w.gh.add_repo("someone", "d")
            w.gh.commit("someone/d", {"f": b"d"})
            quiet(mirror.main, ["config", "include", "--clear"])
            self.assertIn("They are: someone/a, someone/b, someone/c, someone/d.",
                          refused("a project appeared", "1 more and 0 fewer"))
            # 3. A project goes.
            w.gh.delete("someone/d")
            self.assertIn("They are: someone/a, someone/b, someone/c.", refused("a project went", "0 more and 1 fewer"))
            # 4. A key for another owner arrives.
            w.gh.add_repo("acme", "x", owner_type="Organization")
            w.gh.commit("acme/x", {"f": b"x"})
            self.assertEqual(w.key_set("acme")[0], 0)
            said = refused("another owner's key", "1 more and 0 fewer")
            self.assertTrue(said[0].startswith("The first copy: 4 repositories from 2 owner(s)"), said)
            self.assertIn("They are: acme/x, someone/a, someone/b, someone/c.", said)
            # The go to the list as it was last said copies exactly that list.
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            names = sorted(r["name"] for r in w.forge_get("/user/repos")[1])
            self.assertEqual(names, ["acme--x", "someone--a", "someone--b", "someone--c"])
            # After the first copy, a new project joins by itself, as the README says.
            w.gh.add_repo("someone", "e")
            w.gh.commit("someone/e", {"f": b"e"})
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertNotIn("Needs you", out)
            self.assertIn("someone--e", [r["name"] for r in w.forge_get("/user/repos")[1]])
        with World(self) as w:                                   # many: the first 20 named, the rest in the file
            for i in range(25):
                w.gh.add_repo("someone", f"r{i:02d}")
            w.key_set()
            rc, out = w.sync("--announce")
            self.assertEqual(rc, 0, out)
            first = ", ".join(f"someone/r{i:02d}" for i in range(mirror.ANNOUNCE_NAMES))
            self.assertIn(f"They are: {first}, and 5 more; the whole list is in {w.p['first_copy_list']}.", told(out))
            self.assertEqual(fread(w.p["first_copy_list"]).splitlines(), [f"someone/r{i:02d}" for i in range(25)])

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
                self.assertIn("OnStartupSec=2min", fread(os.path.join(base, mirror.SYNC_UNIT + ".timer")))   # G4.7
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
            self.assertEqual(rc, 0, out)               # G4.8: below the floor is said as spent, until the reset
            self.assertIn("GitHub's allowance for your keys is spent until", out)
            self.assertNotIn("gone from GitHub", out)
            self.assertNotIn("cannot refresh", out)
            st = mirror.load_json(w.p["state"], {})
            self.assertEqual(st["last_sync"]["calls"], 1)
            self.assertEqual(st["github_wait_until"], mirror.stamp_of(w.gh.rate_reset))
            self.assertEqual({f"r{i}": w.forge_refs(f"someone--r{i}") for i in range(5)}, before)
            st["github_wait_until"] = mirror.stamp_of(time.time() - 1)     # the reset has come
            mirror.save_state(w.p, st)
            put_settings(w.p, api_floor=100)           # the floor is a setting
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("3 GitHub API call(s)", out)
            self.assertIn("3 GitHub API call(s)", quiet(mirror.main, ["status"])[1])

    def test_G4_8_a_spent_allowance_waits_for_its_reset_and_fails_no_project(self):
        """GitHub's allowance is shared with everything else the person runs with their keys (another program on a
        shared login once spent it in minutes). Both of GitHub's ways of saying "spent" become one line with the reset in the person's
        own time; nothing is asked of GitHub before it, by this process or a fresh one after a restart; no project
        is marked as failed, and every copy is as it was."""
        with World(self) as w:
            for i in range(3):
                w.gh.add_repo("someone", f"r{i}")
                w.gh.commit(f"someone/r{i}", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            before = {f"r{i}": w.forge_refs(f"someone--r{i}") for i in range(3)}
            w.gh.commit("someone/r0", {"b": b"2"})
            line = "GitHub's allowance for your keys is spent until {}. The sync waits until then"
            # 1. The hour's allowance is spent: a 403 with no calls remaining, back at X-RateLimit-Reset.
            w.gh.limit_answer = (403, None)
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            until = mirror.stamp_of(w.gh.rate_reset)
            self.assertIn(line.format(mirror.local_time(until)), out)
            self.assertIn("every copy is as it was", out)
            self.assertNotIn("problem:", out)
            st = mirror.load_json(w.p["state"], {})
            self.assertEqual(st["github_wait_until"], until)
            self.assertFalse([r for r in st["repos"].values() if r.get("problem")], "a project was marked failed")
            self.assertEqual({f"r{i}": w.forge_refs(f"someone--r{i}") for i in range(3)}, before)
            out = quiet(mirror.main, ["status"])[1]
            self.assertIn("github: " + line.format(mirror.local_time(until)), out)
            self.assertNotIn("problem:", out)
            # 2. Nothing is asked of GitHub before the reset, even when it would answer again.
            w.gh.limit_answer = None
            asked = len(w.gh.log)
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn(line.format(mirror.local_time(until)), out)
            self.assertEqual(len(w.gh.log), asked, "GitHub was asked before the reset")
            # 3. Nor by a fresh process: the timer's own run after a restart of the service or the computer.
            r = subprocess.run([sys.executable, "-I", w.p["script"], "sync", "--from-timer"], env=dict(os.environ),
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("spent until", r.stdout)
            self.assertEqual(len(w.gh.log), asked, "a fresh process asked GitHub before the reset")
            # 4. At the reset, the sync runs again and copies what waited.
            st = mirror.load_json(w.p["state"], {})
            st["github_wait_until"] = mirror.stamp_of(time.time() - 1)
            mirror.save_state(w.p, st)
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertRegex(out, r"[1-9]\d* copied now")
            self.assertNotEqual(w.forge_refs("someone--r0"), before["r0"], "what waited was not copied at the reset")
            # 5. GitHub's other way: a 429 with Retry-After, back that many seconds on.
            w.gh.limit_answer = (429, 600)
            t0 = time.time()
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("spent until", out)
            wait = mirror.epoch_of(mirror.load_json(w.p["state"], {})["github_wait_until"])
            self.assertTrue(t0 + 590 <= wait <= time.time() + 610, wait - t0)
            self.assertNotIn("problem:", quiet(mirror.main, ["status"])[1])

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

    def test_G4_7_after_a_restart_the_timer_always_has_a_next_run(self):
        """Seen on a stock Ubuntu 24.04: with OnBootSec, a person who logs in more than 5 min after boot has a
        timer that is 'active' with no next run, forever. Counted from the start of the person's own service
        manager, it runs 2 min after boot with lingering, and 2 min after the login without (seen, both ways)."""
        t = mirror.sync_timer_text(15)
        self.assertIn("\nOnStartupSec=2min\n", t)
        self.assertIn("\nOnUnitActiveSec=15min\n", t)
        self.assertNotIn("OnBootSec", t)
        self.assertNotIn("Persistent", t)          # it applies to calendar timers only, and promised a catch-up


class G5(unittest.TestCase):
    def test_G5_1_a_rewritten_or_deleted_branch_is_kept_before_the_copy_follows(self):
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            first = w.gh.commit("someone/alpha", {"a": b"1"})
            second = w.gh.commit("someone/alpha", {"a": b"2"})
            w.gh.git("someone/alpha", "branch", "side", second)
            w.gh.git("someone/alpha", "tag", "v1", second)
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            day = time.strftime("%Y-%m-%d", time.gmtime())
            # On GitHub: main forced back, side deleted, the tag moved.
            w.gh.git("someone/alpha", "update-ref", "refs/heads/main", first)
            w.gh.git("someone/alpha", "branch", "-D", "side")
            w.gh.git("someone/alpha", "tag", "-f", "v1", first)
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn(f"someone/alpha: main was rewritten or moved back on GitHub; its old commit is kept as kept/{day}/main", out)
            self.assertIn(f"someone/alpha: side was deleted on GitHub; its old commit is kept as kept/{day}/side", out)
            self.assertIn(f"someone/alpha: tags/v1 was moved on GitHub; its old commit is kept as kept/{day}/tags/v1", out)
            refs = w.forge_refs("someone--alpha")
            self.assertEqual(refs["refs/heads/main"], first)
            self.assertNotIn("refs/heads/side", refs)
            self.assertEqual(refs["refs/tags/v1"], first)
            self.assertEqual(refs[f"refs/heads/kept/{day}/main"], second)
            self.assertEqual(refs[f"refs/heads/kept/{day}/side"], second)
            self.assertEqual(refs[f"refs/heads/kept/{day}/tags/v1"], second)
            # Moving forward keeps nothing; a second rewrite the same day gets a name of its own.
            third = w.gh.commit("someone/alpha", {"a": b"3"})
            rc, out = w.sync()
            self.assertNotIn("is kept as", out)
            w.gh.git("someone/alpha", "update-ref", "refs/heads/main", first)
            rc, out = w.sync()
            self.assertIn(f"its old commit is kept as kept/{day}/main-2", out)
            refs = w.forge_refs("someone--alpha")
            self.assertEqual(refs[f"refs/heads/kept/{day}/main-2"], third)
            self.assertEqual(refs[f"refs/heads/kept/{day}/main"], second)
            # Every kept ref survives later syncs, and a lost cache is rebuilt from the copy with them.
            shutil.rmtree(w.p["cache"])
            w.gh.commit("someone/alpha", {"a": b"4"})
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertEqual(len([r for r in w.forge_refs("someone--alpha") if "/kept/" in r]), 4)

    def test_G5_2_kept_refs_live_for_the_set_period(self):
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            cache = next(os.path.join(w.p["cache"], d) for d in os.listdir(w.p["cache"]))
            g = lambda *a: subprocess.run(["git", "--git-dir", cache, *a], check=True, capture_output=True, input="",
                                          text=True, env=dict(os.environ, GIT_AUTHOR_NAME="a", GIT_AUTHOR_EMAIL="a@example.invalid",
                                                              GIT_COMMITTER_NAME="a", GIT_COMMITTER_EMAIL="a@example.invalid")).stdout.strip()
            main = g("rev-parse", "refs/heads/main")
            lone = g("commit-tree", "-m", "only a kept ref reaches me", g("mktree", "--missing"))
            # Two kept refs from long ago, in the copy: one whose commit is on main too, one that alone reaches its commit.
            w.forge_push(cache, "someone--alpha", f"{main}:refs/heads/kept/2020-01-01/reached",
                         f"{lone}:refs/heads/kept/2020-01-01/lone")
            refs = w.forge_refs("someone--alpha")
            self.assertIn("refs/heads/kept/2020-01-01/reached", refs)
            self.assertIn("refs/heads/kept/2020-01-01/lone", refs)
            # The default period (90 days): the old kept ref whose commit is reached otherwise goes; the lone one stays.
            w.gh.commit("someone/alpha", {"c": b"3"})
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("someone/alpha: 1 kept ref(s) older than the kept period were dropped", out)
            refs = w.forge_refs("someone--alpha")
            self.assertNotIn("refs/heads/kept/2020-01-01/reached", refs)
            self.assertEqual(refs["refs/heads/kept/2020-01-01/lone"], lone)
            # A period the person set is kept to, even for the only ref to a commit.
            put_settings(w.p, kept_days=30)
            w.gh.commit("someone/alpha", {"d": b"4"})
            self.assertEqual(w.sync()[0], 0)
            self.assertNotIn("refs/heads/kept/2020-01-01/lone", w.forge_refs("someone--alpha"))

    def test_G5_3_nothing_is_ever_written_to_github(self):
        src = fread(SCRIPT)
        self.assertNotRegex(src, r'"push"[^\n]*"github"', "the script has a push to GitHub")
        for fn in (mirror.gh_get, mirror.gh_public):           # the only two callers of GitHub's API
            get = inspect.getsource(fn)
            self.assertNotIn("method=", get)
            self.assertNotIn("data=", get)
        self.assertEqual(src.count("github_api()"), 3, "a call to GitHub's API goes around gh_get and gh_public")
        with World(self) as w:
            for n in ("alpha", "beta"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            w.gh.rename("someone/beta", "gamma")
            w.gh.delete("someone/alpha")
            self.assertEqual(w.sync()[0], 0)
            self.assertEqual(w.gh.writes(), [], "something was written to the stand-in GitHub")
            for d in os.listdir(w.p["cache"]):
                cache = os.path.join(w.p["cache"], d)
                url = subprocess.run(["git", "--git-dir", cache, "config", "remote.github.pushurl"],
                                     capture_output=True, text=True).stdout.strip()
                self.assertEqual(url, mirror.NO_PUSH)
                out = subprocess.run(["git", "--git-dir", cache, "push", "github", "HEAD"], capture_output=True, text=True)
                self.assertNotEqual(out.returncode, 0)
            self.assertEqual(w.gh.writes(), [])

    def test_G5_4_a_read_that_failed_is_never_taken_for_a_no(self):
        """A read that failed is a third answer, "could not be read", never a no (the family's rule, after a
        sister skill read GitHub's 502 as "not listed"). Every read of GitHub and of the forge is failed in turn,
        once (a blip) and from then on (an outage), under sync, status and restore. After each, the forge holds
        exactly what it held: no copy deleted, renamed, made, or given a kept/ ref; no copy marked or said gone;
        and an owner whose list could not be read is never announced as having 0 repositories."""
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            one = w.gh.commit("someone/alpha", {"a": b"1"})
            w.gh.git("someone/alpha", "branch", "side", one)
            w.gh.git("someone/alpha", "tag", "v1", one)
            w.gh.add_repo("someone", "beta")
            w.gh.commit("someone/beta", {"b": b"1"})
            w.key_set()
            rc, out = w.first_sync()
            self.assertEqual(rc, 0, out)

            def forge_now():
                code, body = w.forge_get("/user/repos?limit=50")
                self.assertEqual(code, 200, body)
                return sorted((r["id"], r["name"]) for r in body), {r["name"]: w.forge_refs(r["name"]) for r in body}
            before = forge_now()
            self.assertEqual(len(before[0]), 2)
            ids = {g: r["forge_id"] for g, r in mirror.load_json(w.p["state"], {})["repos"].items()}
            said_no = ("gone from GitHub", "not in the forge", "There is no copy", "0 repositories", "moved aside",
                       "renamed on GitHub", "is kept as kept/")
            real = (mirror.gh_get, mirror.Forge.call, mirror.git)

            def under(cmd, kind=None, n=None, mode=None):
                seen = {"github api": 0, "forge api": 0, "git read": 0}

                def fails(k):
                    i = seen[k]
                    seen[k] += 1
                    return k == kind and (i == n or (mode == "outage" and i > n))

                def gh_get(key, path):
                    return (502, {}, {"message": "Bad Gateway"}) if fails("github api") else real[0](key, path)

                def call(forge, method, path, body=None):
                    if method == "GET" and fails("forge api"):
                        return 500, None
                    return real[1](forge, method, path, body)

                def git(cache, *args, **kw):
                    if any(a in ("fetch", "ls-remote", "clone") for a in args) and fails("git read"):
                        return subprocess.CompletedProcess(["git", *args], 128, "",
                                                           "fatal: unable to access: The requested URL returned error: 502")
                    return real[2](cache, *args, **kw)
                st = mirror.load_json(w.p["state"], {})
                st["last_full_pass"] = ""                    # a full pass, so that every copy is read every time
                mirror.save_state(w.p, st)
                mirror.gh_get, mirror.Forge.call, mirror.git = gh_get, call, git
                try:
                    rc, out = quiet(mirror.main, cmd)
                finally:
                    mirror.gh_get, mirror.Forge.call, mirror.git = real
                return rc, out, seen

            def unchanged(label, out):
                for word in said_no:
                    self.assertNotIn(word, out, f"{label}: said '{word}' on a failed read\n{out}")
                self.assertEqual(forge_now(), before, f"{label}: the forge changed on a failed read\n{out}")
                st = mirror.load_json(w.p["state"], {})
                self.assertEqual({g: r["forge_id"] for g, r in st["repos"].items()}, ids, label)
                self.assertEqual([r["full_name"] for r in st["repos"].values() if r.get("gone_since")], [], label)

            commands = (["sync"], ["status"], ["restore", "--check"],
                        ["restore", "--to", "file:///nonexistent-wsm-home.git", "someone/alpha"])
            tried = 0
            for cmd in commands:
                rc, out, counts = under(cmd)
                unchanged(f"{' '.join(cmd)}, nothing failed", out)
                for kind, total in counts.items():
                    for n in range(total):
                        for mode in ("blip", "outage"):
                            rc, out, _ = under(cmd, kind, n, mode)
                            unchanged(f"{' '.join(cmd)}: {kind} read {n + 1} of {total} failed ({mode})", out)
                            tried += 1
            self.assertGreater(tried, 20, "too few reads were failed to call this a proof")
            # Before the first copy: a list that could not be read is never announced as 0 repositories.
            st = mirror.load_json(w.p["state"], {})
            saved = st.pop("announced"), st.pop("first_copy_done", None), st.pop("go", None)
            mirror.save_state(w.p, st)
            rc, out, _ = under(["sync", "--announce"], "github api", 0, "outage")
            self.assertNotEqual(rc, 0, out)
            self.assertNotIn("0 repositories", out)
            self.assertIn("could not be read", out)
            self.assertFalse(mirror.load_json(w.p["state"], {}).get("announced"), "an announcement made on a failed read")
            st = mirror.load_json(w.p["state"], {})
            st["announced"], st["first_copy_done"], st["go"] = saved
            mirror.save_state(w.p, st)
            # GitHub answers 404 for a private repository both when it was deleted and when the key can no longer see
            # it. So "gone" is earned only when a list read that SUCCEEDED, with the same key, no longer names the
            # project; a 404 on the project is "could not be read", and a refused key is said as a key that stopped.
            not_found = "remote: Repository not found.\nfatal: repository 'x' not found"

            def github_says(listing=None, fetch_404=False):
                def gh_get(key, path):
                    if listing is None:
                        return real[0](key, path)
                    if listing == "fails":
                        return 502, {}, {"message": "Bad Gateway"}
                    if listing == "refused":
                        return 401, {}, {"message": "Bad credentials"}
                    code, headers, body = real[0](key, path)                 # read, then leave out the named one
                    return code, headers, [r for r in body if r.get("full_name") != listing]

                def git(cache, *args, **kw):
                    if fetch_404 and "fetch" in args and "github" in args:
                        return subprocess.CompletedProcess(["git", *args], 128, "", not_found)
                    return real[2](cache, *args, **kw)
                st = mirror.load_json(w.p["state"], {})
                st["last_full_pass"] = ""
                mirror.save_state(w.p, st)
                mirror.gh_get, mirror.git = gh_get, git
                try:
                    return quiet(mirror.main, ["sync"])
                finally:
                    mirror.gh_get, mirror.git = real[0], real[2]
            rc, out = github_says("fails", fetch_404=True)              # (a) the list cannot be read; projects 404
            unchanged("the list could not be read, and every project answered 404", out)
            self.assertIn("nothing was changed for someone", out)
            rc, out = github_says("refused", fetch_404=True)            # (b) the day a key expires
            unchanged("the key was refused, and every project answered 404", out)
            self.assertIn("the key stopped working", out)
            self.assertIn("run key set someone", out)
            rc, out = github_says(fetch_404=True)                       # (d) listed, but every project answers 404
            unchanged("the list named every project, and every project answered 404", out)
            self.assertIn("the fetch from GitHub failed", out)
            rc, out = github_says("someone/alpha", fetch_404=True)      # (c) a list read that succeeded, without it
            self.assertIn("someone/alpha: gone from GitHub (or no longer visible to its key); its copy is kept", out)
            self.assertEqual(forge_now(), before, "a copy was touched when its project left the list")
            st = mirror.load_json(w.p["state"], {})
            self.assertEqual([r["full_name"] for r in st["repos"].values() if r.get("gone_since")], ["someone/alpha"])
            # And with every read answering again, the next sync is clean, and alpha is back.
            rc, out, _ = under(["sync"])
            self.assertEqual(rc, 0, out)
            self.assertEqual(forge_now(), before)
            st = mirror.load_json(w.p["state"], {})
            self.assertEqual([r["full_name"] for r in st["repos"].values() if r.get("gone_since")], [])


def status():
    return quiet(mirror.main, ["status"])


class G6(unittest.TestCase):
    def test_G6_1_status_says_how_many_are_current_and_names_the_rest(self):
        with World(self) as w:
            for n in ("a", "b", "c"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"f": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            rc, out = status()
            self.assertEqual(rc, 0, out)
            self.assertIn("copies: 3 on GitHub, 3 copied, 0 behind, 0 unreadable", out)
            self.assertNotIn("problem:", out)
            # b cannot be fetched any more; c's copy is changed behind the skill's back; a's last refresh is old.
            shutil.rmtree(os.path.join(w.gh_root, "someone", "b.git"))
            w.gh.touch("someone/b")
            self.assertEqual(w.sync()[0], 1)
            cache = os.path.join(w.p["cache"], str(w.gh.repos["someone/c"]["id"]) + ".git")
            w.forge_push(cache, "someone--c", "refs/heads/main:refs/heads/extra")
            st = mirror.load_json(w.p["state"], {})
            a = next(r for r in st["repos"].values() if r["full_name"] == "someone/a")
            a["last_good"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
            mirror.save_state(w.p, st)
            rc, out = status()
            self.assertEqual(rc, 1)
            lines = out.splitlines()
            at = next(i for i, x in enumerate(lines) if x.startswith("copies:"))
            self.assertEqual(lines[at], "copies: 3 on GitHub, 3 copied, 3 behind, 0 unreadable")
            problems = [x for x in lines[:at] if x.startswith("problem:")]
            self.assertTrue(any("someone/a: last good refresh" in x and "older than twice the sync interval" in x for x in problems))
            self.assertTrue(any(x.startswith("problem: someone/b: cannot refresh: the fetch from GitHub failed") for x in problems))
            self.assertIn("problem: someone/c: the copy's branches and tags differ from what GitHub last showed", problems)

    def test_G6_2_a_copy_that_cannot_refresh_is_never_shown_as_current(self):
        fresh = mirror.now_utc()
        good = {"last_good": fresh, "refs": {"refs/heads/main": "1" * 40}}
        held = {"refs/heads/main": "1" * 40, "refs/heads/kept/2026-01-01/x": "2" * 40}
        self.assertEqual(mirror.copy_state(good, held, 1800), (True, ""))
        for why, rec, refs in [
                ("a problem", dict(good, problem="cannot refresh: anything"), held),
                ("stopped part-way", dict(good, copying_since=fresh), held),
                ("never copied", {"refs": good["refs"]}, held),
                ("old", dict(good, last_good="2020-01-01T00:00:00Z"), held),
                ("refs differ", good, {"refs/heads/main": "3" * 40}),
                ("refs unreadable", good, None),
                ("a pardon asked for", dict(good, problem="cannot refresh: x", known_stale=True, pardon=True), held)]:
            self.assertFalse(mirror.copy_state(rec, refs, 1800)[0], why)
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            put_settings(w.p, known_stale=["someone/alpha"], pardon=True)
            w.gh.key = "ghkey-" + "c3" * 10              # the key stops working: the copy cannot refresh
            self.assertEqual(w.sync()[0], 1)
            rc, out = status()
            self.assertEqual(rc, 1)
            self.assertIn("copies: 1 on GitHub, 1 copied, 1 behind, 0 unreadable", out)
            for pr in w.home.procs:                    # the forge stops: no copy can be read, none is current
                pr.terminate()
                pr.wait(20)
            rc, out = status()
            self.assertEqual(rc, 1)
            self.assertIn("problem: the forge does not answer", out)
            self.assertIn("copies: 1 on GitHub, 1 copied, 0 behind, 1 unreadable", out)

    def test_G6_3_the_key_scan_counts_what_it_read_and_zero_is_a_failure(self):
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            rc, out = status()
            self.assertEqual(rc, 0, out)
            m = re.search(r"key scan: (\d+) file\(s\) read, no key found", out)
            self.assertTrue(m and int(m.group(1)) >= 3, out)
            forge_cfg = [f for f in tree(w.p["data"]) if "gitea-repositories" in f and f.endswith(".git/config")]
            self.assertTrue(forge_cfg, "the forge's repository configs are not where the scan looks")
            # A key that got into a repository config of the forge is found and named, never shown.
            path = os.path.join(w.p["data"], forge_cfg[0])
            with open(path, "a") as f:
                f.write(f"[http]\n\textraheader = Authorization: Bearer {w.gh.key}\n")
            rc, out = status()
            self.assertEqual(rc, 1)
            self.assertIn(f"problem: something shaped like a key, or a key itself, is in {path}", out)
            self.assertNotIn(w.gh.key, out)
            # Something shaped like a GitHub key, in a file of the skill, is found too.
            with open(path, "w") as f:
                f.write("[core]\n\tbare = true\n")
            mirror.write_file(os.path.join(w.p["data"], "note.txt"), "ghp_" + "Z9" * 18 + "\n")
            rc, out = status()
            self.assertIn("note.txt", out)
            os.remove(os.path.join(w.p["data"], "note.txt"))
            # A scan that read nothing proves nothing: it is a failure.
            saved = mirror.key_scan
            mirror.key_scan = lambda p, st: (0, [])
            try:
                rc, out = status()
            finally:
                mirror.key_scan = saved
            self.assertEqual(rc, 1)
            self.assertIn("problem: the key scan read no files, so it proves nothing", out)

    def test_G6_4_no_secret_reaches_the_output_or_the_log(self):
        mirror.secret("a-secret-for-the-test-only")
        self.assertEqual(quiet(mirror.say, "it is a-secret-for-the-test-only here")[1].strip(), "it is <a key> here")
        src = fread(SCRIPT)
        self.assertEqual(len(re.findall(r"\bprint\(", src)), 1, "something prints around say()")
        self.assertNotRegex(src, r"say\([^)]*open\(", "a file is printed whole")
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            outs = [w.key_set()[1], w.first_sync()[1], w.sync()[1], status()[1],
                    quiet(mirror.main, ["setup"])[1], w.key_set(key="ghkey-" + "d4" * 10)[1]]
            secrets_ = [w.gh.key, fread(w.p["mirror_key"]).strip(), fread(w.p["password"]).strip(), "ghkey-" + "d4" * 10]
            logs = [fread(os.path.join(d, f)) for d, _, fs in os.walk(os.path.join(w.p["forge"], "log")) for f in fs]
            for text in outs + logs:
                for sec in secrets_:
                    self.assertNotIn(sec, text)

    def test_G6_11_a_reader_that_stops_reading_never_gets_a_traceback(self):
        """A real machine (2026-11-23): `status | head -3` ended in a Python traceback (a broken pipe). The tool is
        run with its output going to a pipe nobody reads: it ends with its own code, says nothing on stderr, and
        does the whole of its work (setup --dry-run reads this computer to its end and changes nothing)."""
        home = tempfile.mkdtemp(prefix="wsm-pipe-")
        TEMPS.append(home)
        r, w = os.pipe()
        os.close(r)                                   # the reader is gone before the first word
        try:
            pr = subprocess.run([sys.executable, "-I", SCRIPT, "setup", "--dry-run"], stdout=w, stderr=subprocess.PIPE,
                                text=True, timeout=120, env=dict(os.environ, WSM_HOME=home))
        finally:
            os.close(w)
        self.assertEqual(pr.returncode, 0, pr.stderr)
        self.assertEqual(pr.stderr, "")
        self.assertEqual(os.listdir(home), [])

    def test_G6_10_a_sync_says_when_the_forge_does_not_answer(self):
        """A real machine (2026-11-23): the forge's service was stopped, nothing had changed on GitHub, and sync said
        "0 problem(s)" and ended with 0, though no copy had been read. The sync asks the forge first. A forge that
        does not answer is a problem line with its next step and a failing code; nothing is copied, changed or used
        up (not the person's go either); once the forge answers again, the next sync is clean."""
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.sync("--announce")[0], 0)
            port = w.port

            def stop():
                pr = w.home.procs[-1]
                pr.terminate()
                pr.wait(60)
            stop()
            rc, out = w.sync()
            self.assertEqual(rc, 1, out)
            self.assertIn(f"problem: the forge does not answer on 127.0.0.1:{port}, so no copy was read", out)
            self.assertIn("run status", out)
            self.assertNotIn("go", mirror.load_json(w.p["state"], {}))
            w.home.start_forge()
            self.assertEqual(w.sync()[0], 0)
            before = w.forge_refs("someone--alpha")
            self.assertTrue(before)
            stop()
            rc, out = w.sync()
            self.assertEqual(rc, 1, out)
            self.assertIn("problem: the forge does not answer", out)
            self.assertNotIn("0 problem(s)", out)
            self.assertEqual(mirror.load_json(w.p["state"], {})["last_sync"]["problems"], 1)
            self.assertIn("the forge does not answer", status()[1])
            w.home.start_forge()
            self.assertEqual(w.forge_refs("someone--alpha"), before)
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn("0 problem(s)", out)

    def test_G6_9_a_keys_end_date_is_a_date_or_said_unread_and_no_warning_is_promised_without_it(self):
        """A cold start (2026-11-23): the smallest model told the person the mirror warns two weeks before the key
        ends while its end date could not be read, and SKILL.md itself promised the warning. Each key's end has
        three answers: a date (the warning is promised), no end (said only once GitHub's way of saying it is seen,
        contract C3), or could not be read: said so, with no warning promised, in the block before the go."""
        tell = re.compile(r"^tell the person \d+ of \d+: (.*)$", re.M)
        unread = ("The end date of the key for someone could not be read, so no warning before it ends can be "
                  f"promised; its end date is on GitHub's key page, {mirror.KEY_PAGE}.")
        date = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() + 60 * 86400))
        for sent, want in ((date, f"The key for someone ends {date}; the mirror warns you two weeks before."),
                           (None, unread), ("next Tuesday-ish", unread)):
            with World(self) as w:
                w.gh.add_repo("someone", "alpha")
                w.gh.commit("someone/alpha", {"a": b"1"})
                w.gh.expires = sent
                w.key_set()
                rc, out = w.sync("--announce")
                self.assertEqual(rc, 0, out)
                said = tell.findall(out)
                self.assertIn(want, said, sent)
                if sent != date:
                    self.assertFalse(any("warns you" in x for x in said), "a warning promised without a date")
                    rc, st = quiet(mirror.main, ["status"])
                    self.assertIn("no warning before it ends can be promised", st)
        skill = fread(os.path.join(ROOT, "skills", "mirror", "SKILL.md"))
        self.assertNotIn("warn you", skill.lower(), "SKILL.md promises a warning the tool may not be able to give")
        self.assertIn("Never promise a\nwarning before a key ends", skill)

    def test_G6_5_status_shows_version_expiry_disk_and_the_last_and_next_sync(self):
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            soon = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() + 60 * 86400))
            w.gh.expires = soon
            self.assertEqual(w.first_sync()[0], 0)
            rc, out = status()
            self.assertEqual(rc, 0, out)
            self.assertIn(f"forge: Gitea {mirror.GITEA_VERSION} on http://127.0.0.1:{w.port}/", out)
            self.assertIn(f"key for someone: expires {soon}", out)
            self.assertRegex(out, r"disk: \d+ MB free \(floor 2048 MB\)")
            self.assertRegex(out, r"last good sync: 20\d\d-")
            self.assertIn("next sync: " + ("every 15 min" if mirror.system()[0] == "darwin" else w.home.timer_next), out)
            near = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() + 5 * 86400 + 600))
            w.gh.expires = near
            self.assertEqual(w.sync()[0], 0)
            rc, out = status()
            self.assertEqual(rc, 1)
            self.assertIn(f"problem: the key for someone expires {near}, in 5 day(s): make a new one, then `key set someone`", out)
            w.gh.expires = None
            self.assertEqual(w.sync()[0], 0)
            out = status()[1]
            self.assertIn("key for someone: expiry not known", out)
            self.assertNotIn("no expiry", out)
            # A date that cannot be read is shown as not known, and no warning fires on a guess.
            w.gh.expires = "next Tuesday-ish"
            self.assertEqual(w.sync()[0], 0)
            rc, out = status()
            self.assertEqual(rc, 0, out)
            self.assertIn("key for someone: expiry not known (GitHub sent 'next Tuesday-ish', which could not be read)", out)
            self.assertNotIn("problem: the key for someone expires", out)

    def test_G6_6_status_says_when_a_newer_release_pins_a_newer_forge(self):
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            binary = mirror.sha256_of(w.p["bin"])
            rc, out = status()
            self.assertIn("newer release: not known (GitHub gave 404", out)
            self.assertEqual(rc, 0, "a release GitHub does not show is a problem")
            w.gh.release = {"tag_name": "v0.3.0", "published_at": "2026-12-01T00:00:00Z",
                            "body": "Pins Gitea 28.9.1 (security fix)\nOther notes."}
            rc, out = status()
            self.assertNotIn("v0.3.0", out, "asked again within the day")
            rc, out = quiet(mirror.main, ["status", "--check-release"])
            self.assertIn(f"newer release of this skill: v0.3.0 (this is {mirror.VERSION}); `self-update` is your choice", out)
            self.assertIn(f"that release pins Gitea 28.9.1 (this forge runs {mirror.GITEA_VERSION}), and names it a SECURITY FIX", out)
            self.assertIn("Nothing was changed.", out)
            self.assertEqual(w.gh.release_calls, [False, False], "the release was asked for more than once a day, or with a key")
            self.assertEqual(mirror.sha256_of(w.p["bin"]), binary)
            w.gh.release["body"] = "Pins Gitea 28.9.2"
            out = quiet(mirror.main, ["status", "--check-release"])[1]
            self.assertIn(f"that release pins Gitea 28.9.2 (this forge runs {mirror.GITEA_VERSION});", out)
            self.assertNotIn("SECURITY", out)

    def test_G6_7_a_next_run_on_the_boot_clock_is_a_next_run(self):
        """Seen on a stock Ubuntu 24.04: systemd kept the next run on the boot clock only, and status said
        'no next run' while one was 2 min away."""
        saved = mirror.system
        mirror.system = lambda: ("linux", "x86_64")
        try:
            with Home() as h:
                h.timer_next, h.timer_mono = "", "%dmin 57.5s" % (time.monotonic() // 60 + 2)
                when, problem = mirror.next_sync()
                self.assertEqual(problem, "")
                self.assertRegex(when, r"\d{4}-\d\d-\d\d \d\d:\d\d")
                h.timer_mono = "infinity"
                when, problem = mirror.next_sync()
                self.assertIn("no next run", problem)
                h.timer_next, h.timer_mono = "Mon 2026-11-23 01:07:28 UTC", ""
                self.assertEqual(mirror.next_sync(), ("Mon 2026-11-23 01:07:28 UTC", ""))
                h.timer_next, h.timer_mono = "", "not a time span"
                self.assertIn("no next run", mirror.next_sync()[1])
        finally:
            mirror.system = saved


def bare_home(root, name, with_commit=False):
    path = os.path.join(root, name + ".git")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", path], check=True)
    if with_commit:
        work = tempfile.mkdtemp(dir=root)
        subprocess.run(["git", "init", "-q", "-b", "main", work], check=True)
        subprocess.run(["git", "-C", work, "-c", "user.email=a@example.invalid", "-c", "user.name=a", "commit", "-q",
                        "--allow-empty", "-m", "already here"], check=True)
        subprocess.run(["git", "-C", work, "push", "-q", path, "main"], check=True)
    return path


def lsremote(path):
    out = subprocess.run(["git", "ls-remote", path], capture_output=True, text=True).stdout
    return {r: s for s, r in (l.split("\t") for l in out.splitlines()) if r.startswith(("refs/heads/", "refs/tags/"))}


class G7(unittest.TestCase):
    def world_with_copies(self, w, lfs=False):
        w.gh.add_repo("someone", "alpha")
        w.gh.commit("someone/alpha", {"a": b"1"})
        w.gh.git("someone/alpha", "tag", "v1")
        w.gh.add_repo("someone", "beta")
        if lfs and mirror.has_git_lfs():
            w.gh.commit("someone/beta", {".gitattributes": b"*.bin filter=lfs diff=lfs merge=lfs -text\n",
                                         "big.bin": w.gh.lfs_file(b"L" * 300_000)})
        else:
            w.gh.commit("someone/beta", {"b": b"1"})
        w.key_set()
        self.assertEqual(w.first_sync()[0], 0)

    def test_G7_1_each_sync_writes_down_the_refs_github_last_showed(self):
        with World(self) as w:
            self.world_with_copies(w)
            st = mirror.load_json(w.p["state"], {})
            for rec in st["repos"].values():
                self.assertEqual(rec["seen_refs"], w.gh_refs(rec["full_name"]))
                self.assertTrue(rec["seen_at"])
            # When the push into the forge fails, what GitHub showed is still written down.
            w.gh.commit("someone/alpha", {"a": b"2"})
            new = w.gh_refs("someone/alpha")
            put_settings(w.p, git_timeout_s=1)
            saved = mirror.push_specs
            mirror.push_specs = lambda cache: ["refs/heads/main:refs/heads/main", "+refs/heads/nope:refs/heads/nope"]
            try:
                self.assertEqual(w.sync()[0], 1)
            finally:
                mirror.push_specs = saved
            st = mirror.load_json(w.p["state"], {})
            alpha = next(r for r in st["repos"].values() if r["full_name"] == "someone/alpha")
            self.assertEqual(alpha["seen_refs"], new)
            self.assertTrue(alpha["problem"])

    def test_G7_2_restore_check_proves_every_copy_from_the_forge_alone(self):
        with World(self) as w:
            self.world_with_copies(w, lfs=True)
            w.gh.stop()                                # GitHub is out of reach
            before = set(os.listdir(w.p["data"]))
            rc, out = quiet(mirror.main, ["restore", "--check"])
            self.assertEqual(rc, 0, out)
            self.assertIn("restore check: 2 copies, 2 whole, 0 difference(s); nothing outside this computer was asked", out)
            self.assertEqual(set(os.listdir(w.p["data"])), before, "the scratch folder was left behind")
            # A copy that no longer holds what GitHub last showed is named, ref by ref.
            cache = os.path.join(w.p["cache"], str(w.gh.repos["someone/alpha"]["id"]) + ".git")
            w.forge_push(cache, "someone--alpha", ":refs/tags/v1")
            rc, out = quiet(mirror.main, ["restore", "--check"])
            self.assertEqual(rc, 1)
            self.assertRegex(out, r"someone/alpha: refs/tags/v1 is missing in the copy, [0-9a-f]{12} when GitHub was last seen")
            self.assertIn("restore check: 2 copies, 1 whole, 1 difference(s)", out)
            # A copy whose objects are gone from the forge cannot pass.
            forge_repo = os.path.join(w.p["forge"], "data", "gitea-repositories", "mirror", "someone--beta.git")
            for d, _, fs in os.walk(os.path.join(forge_repo, "objects")):
                for f in fs:
                    os.remove(os.path.join(d, f))
            rc, out = quiet(mirror.main, ["restore", "--check"])
            self.assertEqual(rc, 1)
            self.assertIn("someone/beta: ", out)
            self.assertIn("2 copies, 0 whole", out)
            self.assertEqual(w.gh.writes(), [])

    def test_G7_3_a_copy_is_restored_to_a_new_empty_home(self):
        with World(self) as w:
            self.world_with_copies(w, lfs=True)
            root = tempfile.mkdtemp(prefix="wsm-home-")
            TEMPS.append(root)
            home = bare_home(root, "alpha")
            rc, out = quiet(mirror.main, ["restore", "--to", home, "someone/alpha"])
            self.assertEqual(rc, 0, out)
            self.assertIn(f"restored someone/alpha: 1 branch(es) and 1 tag(s) to {home}", out)
            self.assertEqual(lsremote(home), w.gh_refs("someone/alpha"))
            # A home that is not empty is refused, and nothing is pushed there.
            full = bare_home(root, "full", with_commit=True)
            before = lsremote(full)
            rc, out = quiet(mirror.main, ["restore", "--to", full, "someone/alpha"])
            self.assertEqual(rc, 1)
            self.assertIn("The new home is not empty, so nothing was pushed.", out)
            self.assertEqual(lsremote(full), before)
            # GitHub is never a home this skill pushes to (INV1): it says how the person can do it.
            n = len(w.gh.log)
            for url in (f"{mirror.github_git()}/someone/new.git", "https://github.com/someone/new.git",
                        "git@github.com:someone/new.git"):
                rc, out = quiet(mirror.main, ["restore", "--to", url, "someone/alpha"])
                self.assertEqual(rc, 2, url)
                self.assertIn("This skill never writes to GitHub", out)
                self.assertIn("git push --mirror", out)
            self.assertEqual(w.gh.log[n:], [], "the refused restore talked to GitHub")
            self.assertEqual(w.gh.writes(), [])
            if mirror.has_git_lfs():
                lhome = bare_home(root, "beta")
                rc, out = quiet(mirror.main, ["restore", "--to", lhome, "someone/beta"])
                self.assertEqual(rc, 0, out)
                self.assertTrue([f for d, _, fs in os.walk(os.path.join(lhome, "lfs", "objects")) for f in fs],
                                "the large file did not reach the new home")

    def test_G7_4_a_new_computer_gets_every_copy_back_from_the_backup(self):
        with World(self) as w:
            self.world_with_copies(w, lfs=True)
            folder = tempfile.mkdtemp(prefix="wsm-bk-")
            TEMPS.append(folder)
            rc, out = quiet(mirror.main, ["backup", folder])
            self.assertEqual(rc, 0, out)
            self.assertIn("readable by you only", out)
            self.assertIn("It holds the forge's own secrets", out)
            files = os.listdir(folder)
            self.assertEqual(len(files), 1)
            backup = os.path.join(folder, files[0])
            self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
            # the forge is back after the backup
            self.assertEqual(http_get(f"http://127.0.0.1:{w.port}/api/healthz")[0], 200)
            # No key of the skill is in the backup.
            with tarfile.open(backup) as tf:
                names = tf.getnames()
                blob = b"".join(tf.extractfile(m).read() for m in tf.getmembers() if m.isfile() and m.name != "gitea-dump.tar.gz")
            self.assertIn("gitea-dump.tar.gz", names)
            self.assertNotIn(w.gh.key.encode(), blob)
            self.assertNotIn(fread(w.p["mirror_key"]).strip().encode(), blob)
            self.assertFalse([n for n in names if "key" in n.lower()], names)
            want = {n: w.gh_refs(f"someone/{n}") for n in ("alpha", "beta")}
            w.gh.stop()                                # the new computer: GitHub out of reach, only the file
            with Home(linger="yes") as h2:
                h2.place_gitea()
                rc, out = h2.setup("--from", backup)
                self.assertEqual(rc, 0, out)
                self.assertIn("2 copies are back", out)
                self.assertIn("make new keys and run `key set` for each owner: someone", out)
                port, _ = h2.start_forge()
                rc, out = quiet(mirror.main, ["restore", "--check"])
                self.assertEqual(rc, 0, out)
                self.assertIn("restore check: 2 copies, 2 whole, 0 difference(s)", out)
                p2 = mirror.paths()
                self.assertNotEqual(p2["data"], w.p["data"])
                home = bare_home(folder, "alpha-again")
                self.assertEqual(quiet(mirror.main, ["restore", "--to", home, "someone/alpha"])[0], 0)
                self.assertEqual(lsremote(home), want["alpha"])
                # Setup --from on a computer that already has a forge changes nothing.
                rc, out = h2.setup("--from", backup)
                self.assertEqual(rc, 2)
                self.assertIn("already has a forge", out)


class G8(unittest.TestCase):
    def test_G8_1_the_forge_answers_on_this_computer_only(self):
        addrs = mirror.own_addresses()
        others = [a for a in addrs if not (a == "::1" or a.startswith("127."))]
        # Something that listens on every address is caught, address by address.
        wide = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        wide.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        wide.bind(("0.0.0.0", 0))
        wide.listen(8)
        try:
            v4 = [a for a in others if ":" not in a]
            if v4:                                     # an address on a bridge that is down answers nothing at all
                caught = mirror.answers_on(addrs, wide.getsockname()[1])
                self.assertTrue(caught, "a listener on every address was not caught on any of them")
                self.assertTrue(set(caught) <= set(v4))
        finally:
            wide.close()
        narrow = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        narrow.bind(("127.0.0.1", 0))
        narrow.listen(8)
        try:
            self.assertEqual(mirror.answers_on(addrs, narrow.getsockname()[1]), [])
        finally:
            narrow.close()
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            rc, out = status()
            self.assertEqual(rc, 0, out)
            self.assertIn(f"network: the forge answers on 127.0.0.1 only (tried {len(others)} other address(es)", out)
            saved = mirror.answers_on
            mirror.answers_on = lambda a, p: ["192.0.2.7"]
            try:
                rc, out = status()
            finally:
                mirror.answers_on = saved
            self.assertEqual(rc, 1)
            self.assertIn("problem: the forge answers on 192.0.2.7, not only on 127.0.0.1", out)

    def test_G8_2_exposing_the_forge_is_left_to_the_person(self):
        src = fread(SCRIPT)
        self.assertNotIn("0.0.0.0", src)
        for tool in ("ngrok", "cloudflared", "tailscale", "frpc", "localtunnel", '"-R"', "serveo"):
            self.assertNotIn(tool, src.lower(), tool)
        self.assertEqual(re.findall(r"HTTP_ADDR = (\S+)", mirror.app_ini(mirror.paths(), 3000)), ["127.0.0.1"])
        with Home() as h:
            p = mirror.paths()
            before = tree(h.tmp)
            rc, out = quiet(mirror.main, ["expose"])
            self.assertEqual(rc, 0)
            self.assertIn("this skill will not change that", out)
            self.assertIn("Anyone who can reach it can try to sign in", out)
            self.assertIn("Nothing was changed.", out)
            self.assertEqual(tree(h.tmp), before)
            self.assertEqual(h.calls, [])

    def test_G8_3_the_person_is_told_the_copy_is_as_private_as_this_computer(self):
        sentence = ("The copy is as private as this computer. Anyone who can read this account's files can read every "
                    "project in it. Disk encryption, and who uses this computer, are yours.")
        self.assertEqual(mirror.PRIVATE_AS_THIS_COMPUTER, sentence)
        self.assertIn(sentence, fread(os.path.join(ROOT, "README.md")))
        with World(self) as w:
            self.assertIn(sentence, quiet(mirror.main, ["setup"])[1])
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            rc, out = w.sync("--announce")
            self.assertIn(sentence, out)
            self.assertEqual(w.forge_get("/user/repos")[1], [], "something was copied before the person was told")


PINS_28_3 = {("linux", "x86_64"): ("linux-amd64", "5f834e8f04986f3b9206cf14743b5a6e6996ceddc238dac6409866ebda2a0686"),
             ("darwin", "x86_64"): ("darwin-amd64", "4420bb44df8047cd4f3bec9162d559c0245dee7ef3b58bc5540d7a1eefd8b94f"),
             ("darwin", "arm64"): ("darwin-arm64", "0aca8f1f1d6a0b31abc2c7e5c40feec18f3e45ab0c7b11deaef259794ce45812")}


@contextlib.contextmanager
def pinned(version, pins):
    """As if this were a release of the skill that pins another Gitea."""
    saved = mirror.GITEA_VERSION, dict(mirror.PINS)
    mirror.GITEA_VERSION = version
    mirror.PINS.clear()
    mirror.PINS.update(pins)
    try:
        yield
    finally:
        mirror.GITEA_VERSION = saved[0]
        mirror.PINS.clear()
        mirror.PINS.update(saved[1])


def forge_version(w):
    return w.forge_get("/version")[1].get("version")


class G9(unittest.TestCase):
    def test_G9_1_a_forge_upgrade_backs_up_first_and_rolls_back_on_failure(self):
        if not real_gitea():
            if os.environ.get("WSM_REQUIRE_GITEA"):
                raise AssertionError("the real pinned Gitea is required here")
            raise unittest.SkipTest("no real pinned Gitea for this system")
        new_bin = fread_bytes(real_gitea())
        with contextlib.ExitStack() as stack:
            with pinned("28.3.0", PINS_28_3):        # the forge was set up by an older release of the skill
                w = stack.enter_context(World(self))
                self.assertEqual(forge_version(w), "28.3.0")
                w.gh.add_repo("someone", "alpha")
                w.gh.commit("someone/alpha", {"a": b"1"})
                w.key_set()
                self.assertEqual(w.first_sync()[0], 0)
            # This release pins 28.4.0. A setup does not move the forge; upgrade-forge does, a backup first.
            rc, out = quiet(mirror.main, ["setup"])
            self.assertEqual(rc, 2)
            self.assertIn("Setup does not move the forge: run upgrade-forge", out)
            site = ServeFile(new_bin)
            stack.callback(site.close)
            os.environ["WSM_GITEA_BASE"] = site.url
            stack.callback(os.environ.pop, "WSM_GITEA_BASE", None)
            ok, _ = mirror.take_lock(w.p)                        # not while a sync runs
            rc, out = quiet(mirror.main, ["upgrade-forge"])
            mirror.drop_lock(w.p)
            self.assertEqual(rc, 3, out)
            self.assertEqual(forge_version(w), "28.3.0")
            rc, out = quiet(mirror.main, ["upgrade-forge"])
            self.assertIn("upgrade-forge: Gitea 28.3.0 to 28.4.0. First, a backup.", out)
            self.assertIn("The forge runs Gitea 28.4.0 now.", out)
            self.assertEqual(rc, 0, out)
            st = mirror.load_json(w.p["state"], {})
            self.assertTrue(os.path.exists(st["upgraded"]["backup"]), "no backup was made first")
            self.assertEqual(forge_version(w), "28.4.0")
            self.assertEqual(quiet(mirror.main, ["restore", "--check"])[0], 0)
            # A newer pin whose binary cannot run: the old binary and the database come back.
            bad = b"#!/bin/sh\nexit 3\n"
            site2 = ServeFile(bad)
            stack.callback(site2.close)
            os.environ["WSM_GITEA_BASE"] = site2.url
            badpins = {k: (v[0], hashlib.sha256(bad).hexdigest()) for k, v in mirror.PINS.items()}
            with pinned("28.4.1", badpins):
                rc, out = quiet(mirror.main, ["upgrade-forge"])
            self.assertEqual(rc, 1, out)
            self.assertIn("The upgrade to Gitea 28.4.1 failed: Gitea's migration failed", out)
            self.assertIn("The old forge, Gitea 28.4.0, is back with its database as it was before.", out)
            self.assertEqual(mirror.load_json(w.p["state"], {})["gitea"], "28.4.0")
            self.assertEqual(forge_version(w), "28.4.0")
            self.assertEqual(quiet(mirror.main, ["restore", "--check"])[0], 0)
            # Never moved back.
            with pinned("28.3.0", PINS_28_3):
                rc, out = quiet(mirror.main, ["upgrade-forge"])
            self.assertEqual(rc, 2)
            self.assertIn("A forge is never moved back", out)

    def test_G9_2_self_update_swaps_whole_and_never_during_a_sync(self):
        with World(self) as w:
            src = fread(SCRIPT)
            new = src.replace(f'VERSION = "{mirror.VERSION}"', 'VERSION = "0.2.0"').replace(
                f'GITEA_VERSION = "{mirror.GITEA_VERSION}"', 'GITEA_VERSION = "28.9.0"').encode()
            self.assertNotEqual(new, src.encode())
            w.gh.contents["scripts/mirror.py"] = new
            target = w.p["script"]
            before = fread_bytes(target)
            binary, gitea_ver = mirror.sha256_of(w.p["bin"]), mirror.load_json(w.p["state"], {})["gitea"]
            w.gh.release = {"tag_name": "v0.2.0", "body": "mirror.py SHA-256 " + "0" * 64}
            rc, out = quiet(mirror.main, ["self-update"])
            self.assertEqual(rc, 1)
            self.assertIn("does not match its checksum. Nothing was changed.", out)
            w.gh.release["body"] = "Notes.\nmirror.py SHA-256 " + hashlib.sha256(new).hexdigest()
            ok, _ = mirror.take_lock(w.p)
            rc, out = quiet(mirror.main, ["self-update"])
            mirror.drop_lock(w.p)
            self.assertEqual(rc, 3)
            self.assertIn("Try again when it is done.", out)
            self.assertEqual(fread_bytes(target), before)
            self.assertFalse(os.path.exists(target + ".new"))
            rc, out = quiet(mirror.main, ["self-update"])
            self.assertEqual(rc, 0, out)
            self.assertIn(f"self-update: {mirror.VERSION} to v0.2.0, replaced whole", out)
            self.assertIn("v0.2.0 pins Gitea 28.9.0: run upgrade-forge when you choose", out)
            self.assertEqual(fread_bytes(target), new)
            ran = subprocess.run([sys.executable, "-I", target, "--version"], capture_output=True, text=True)
            self.assertIn("0.2.0", ran.stdout)
            self.assertEqual((mirror.sha256_of(w.p["bin"]), mirror.load_json(w.p["state"], {})["gitea"]), (binary, gitea_ver),
                             "the self-update changed the forge")
            self.assertEqual(w.gh.release_calls.count(True), 0, "a release or file was asked for with a key")
            self.assertIn("os.replace(tmp, target)", inspect.getsource(mirror.cmd_self_update))

    def test_G9_5_self_update_on_the_latest_release_says_so_with_the_checksum_it_read(self):
        """The owner's ruling on M13 (2026-11-23): before 0.1.1 there is no newer release to move to, so what is
        seen at the 0.1.0 release is the read: self-update on the latest release reads it with no key and says
        there is nothing newer, with the checksum the release names, checked against this very file."""
        own = hashlib.sha256(fread_bytes(SCRIPT)).hexdigest()
        with World(self) as w:
            target = w.p["script"]
            before = fread_bytes(target)
            w.gh.release = {"tag_name": f"v{mirror.VERSION}", "body": "Notes.\nmirror.py SHA-256 " + own}
            rc, out = quiet(mirror.main, ["self-update"])
            self.assertEqual(rc, 0, out)
            self.assertIn(f"This is the latest release ({mirror.VERSION}). Its notes name mirror.py SHA-256 {own}, "
                          "and this file matches it. Nothing was changed.", out)
            w.gh.release["body"] = "mirror.py SHA-256 " + "0" * 64
            rc, out = quiet(mirror.main, ["self-update"])
            self.assertEqual(rc, 1, out)
            self.assertIn(f"the release says mirror.py SHA-256 {'0' * 64}, and this file's is {own}. Nothing was "
                          "changed. To put the released file back, update the plugin with Claude Code's own plugin "
                          "update, then run setup.", out)
            w.gh.release["body"] = "Notes with no checksum."
            rc, out = quiet(mirror.main, ["self-update"])
            self.assertEqual(rc, 0, out)
            self.assertIn("Its notes name no checksum of mirror.py, so this file could not be checked against it.", out)
            if mirror.vt(mirror.VERSION) > (0, 0, 0):            # a release branch: an older release is said so
                w.gh.release = {"tag_name": "v0.0.0", "body": ""}
                rc, out = quiet(mirror.main, ["self-update"])
                self.assertIn(f"This is {mirror.VERSION}, newer than the latest release (v0.0.0).", out)
            self.assertEqual(fread_bytes(target), before)
            self.assertEqual(w.gh.release_calls.count(True), 0, "the release was read with a key")
            self.assertGreater(len(w.gh.release_calls), 0)

    def test_G9_3_uninstall_leaves_only_the_copies_and_deletes_them_only_on_the_persons_word(self):
        with World(self) as w:
            for n in ("alpha", "beta"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            p = w.p
            rc, out = quiet(mirror.main, ["uninstall"])
            self.assertEqual(rc, 0, out)
            for gone in (os.path.dirname(p["bin"]), p["conf"], p["cache"], p["script"], p["keys"], p["mirror_key"],
                         p["settings"], p["password"]):
                self.assertFalse(os.path.exists(gone), gone)
            if mirror.system()[0] == "darwin":
                self.assertFalse(os.path.exists(p["plist"]))
            else:
                self.assertFalse(os.path.exists(p["unit"]))
                self.assertIn(["systemctl", "--user", "disable", "--now", mirror.SERVICE, mirror.SYNC_UNIT + ".timer"],
                              w.home.calls)
            repos = os.path.join(p["forge"], "data", "gitea-repositories", "mirror")
            self.assertEqual(sorted(os.listdir(repos)), ["someone--alpha.git", "someone--beta.git"])
            self.assertIn(f"Kept: the folder of copies, {p['data']} (2 copies), with the record", out)
            saved = sys.stdin

            class AtTerminal(io.StringIO):
                def isatty(self):
                    return True
            try:
                sys.stdin = io.StringIO("2\n")                         # piped in, even the right number: refused
                rc, out = quiet(mirror.main, ["uninstall", "--delete-copies"])
                self.assertEqual(rc, 2, out)
                self.assertIn("in a terminal window of their own", out)
                self.assertTrue(os.path.exists(repos))
                sys.stdin = AtTerminal("1\n")
                rc, out = quiet(mirror.main, ["uninstall", "--delete-copies"])
                self.assertEqual(rc, 1)
                self.assertIn("That is not 2. The copies were not deleted.", out)
                self.assertTrue(os.path.exists(repos))
                sys.stdin = AtTerminal("2\n")
                rc, out = quiet(mirror.main, ["uninstall", "--delete-copies"])
            finally:
                sys.stdin = saved
            self.assertEqual(rc, 0, out)
            self.assertFalse(os.path.exists(p["data"]))


    def test_G9_4_leaving_waits_for_a_sync_ends_with_a_census_and_is_safe_twice(self):
        """Rule 8 (the family's): a person who wants to leave is never refused. A running sync is a wait that says
        what holds it and the one next step; the last thing printed says what is gone and what is kept; run twice,
        it is safe; what setup left on the computer takes itself off without the plugin; and nothing of ours acts
        on the person's Claude Code sessions, so Claude Code's own plugin uninstall leaves nothing of ours running
        there (the README says what does go on running, and the one command that takes it off)."""
        with Home() as h:                                      # nothing installed: twice, and no folder made
            for _ in (1, 2):
                rc, out = quiet(mirror.main, ["uninstall"])
                self.assertEqual(rc, 0, out)
                lines = out.strip().splitlines()
                self.assertIn("Gone now: nothing; the service, the sync timer, the forge's binary, the settings and "
                              "the keys were already gone.", lines)
                self.assertEqual(lines[-1], "Kept: nothing. Nothing of the skill's is left on this computer.")
                self.assertFalse(os.path.exists(mirror.paths()["data"]))
        with World(self) as w:
            for n in ("alpha", "beta"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            p = w.p
            # What setup left takes itself off: the copy the service runs is this very script, whole.
            self.assertEqual(fread(p["script"]), fread(SCRIPT))
            if mirror.system()[0] != "darwin":
                self.assertIn(p["script"], fread(os.path.join(os.path.dirname(p["unit"]), mirror.SYNC_UNIT + ".service")))
            # A sync holds the lock: uninstall waits, says what holds it, and goes on when it ends.
            lock = os.path.join(p["data"], "sync.lock")
            with open(lock, "w") as f:
                f.write(str(os.getpid()))
            threading.Timer(3, os.remove, [lock]).start()
            rc, out = quiet(mirror.main, ["uninstall"])
            self.assertEqual(rc, 0, out)
            self.assertIn(f"Waiting: a sync is running (process {os.getpid()}); uninstall goes on as soon as it ends", out)
            lines = out.strip().splitlines()
            self.assertEqual(lines[-1], f"Kept: the folder of copies, {p['data']} (2 copies), with the record of what "
                                        "the skill did here. It is yours; delete it only when you are sure, with: "
                                        "uninstall --delete-copies")
            self.assertIn("Gone now:", lines)
            # Twice: nothing more goes, the copies and the record stay, and it says so last.
            rc, out = quiet(mirror.main, ["uninstall"])
            self.assertEqual(rc, 0, out)
            self.assertIn("Gone now: nothing;", out)
            self.assertTrue(out.strip().splitlines()[-1].startswith("Kept: the folder of copies"))
            self.assertTrue(os.path.exists(p["state"]))
            # A sync that does not end: the line names what runs and the one next step, and nothing is removed.
            with open(lock, "w") as f:
                f.write(str(os.getpid()))

            class Clock:
                now = time.time()

                def __getattr__(self, name):
                    return getattr(time, name)

                def time(self):
                    return Clock.now

                def sleep(self, s):
                    Clock.now += s
            saved = mirror.time
            mirror.time = Clock()
            try:
                rc, out = quiet(mirror.main, ["uninstall"])
            finally:
                mirror.time = saved
                os.remove(lock)
            self.assertEqual(rc, 3, out)
            self.assertEqual(out.strip().splitlines()[-1],
                             f"Not now: a sync is still running (process {os.getpid()}) after 10 minutes, and nothing "
                             "was removed. Run uninstall again once it has ended.")
            self.assertTrue(os.path.exists(p["forge"]))
        # Nothing of ours acts on the person's Claude Code sessions: no hooks, no status line, no launcher.
        manifest = jload(os.path.join(ROOT, ".claude-plugin", "plugin.json"))
        for k in ("hooks", "statusLine", "mcpServers", "commands", "agents"):
            self.assertNotIn(k, manifest)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "hooks")))
        # Where the README says how to install (a release branch), it says what goes on running after Claude
        # Code's own plugin uninstall, and the one command that takes it off.
        readme = fread(os.path.join(ROOT, "README.md"))
        if "## Taking it off" in readme:
            off = " ".join(readme.split("## Taking it off", 1)[1].split("\n## ", 1)[0].split())
            self.assertIn("Claude Code's own `claude plugin uninstall`", off)
            self.assertIn("keep running", off)
            self.assertIn("python3 -I ~/.local/share/windstorm-mirror/mirror.py uninstall", off)
            self.assertIn("needs nothing else installed", off)

SKILL = os.path.join(ROOT, "skills", "mirror", "SKILL.md")


def skill_front():
    text = fread(SKILL)
    head, sep, body = text[4:].partition("\n---\n")
    assert text.startswith("---\n") and sep, "no front matter"
    front = {}
    for line in head.splitlines():
        m = re.fullmatch(r"([a-z-]+): (.*)", line)
        assert m and "\t" not in line, f"not a plain 'key: value' line: {line[:60]}"
        k, v = m.groups()
        if v.startswith("'"):
            assert v.endswith("'") and "'" not in v[1:-1].replace("''", ""), "a quote inside a single-quoted value"
            v = v[1:-1].replace("''", "'")
        elif v.startswith('"'):
            v = json.loads(v)
        else:
            assert not re.search(r": |#|^[\[{&*!|>%@`]", v), f"a plain value YAML could read otherwise: {k}"
        front[k] = v
    return text, front, body


def section(body, title):
    m = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    assert m, title
    return m.group(1)


class G10(unittest.TestCase):
    def test_G10_1_skill_md_fits_the_agent_skills_format(self):
        text, front, body = skill_front()
        self.assertEqual(front["name"], "mirror")
        self.assertEqual(os.path.basename(os.path.dirname(SKILL)), front["name"])
        self.assertRegex(front["name"], r"^[a-z0-9-]{1,64}$")
        self.assertLessEqual(len(front["description"]), 1024)
        self.assertIn("Use when", front["description"])
        self.assertEqual(front.get("license"), "Apache-2.0")
        self.assertLess(len(text.splitlines()), 500)
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(SKILL), "..", "..", "scripts", "mirror.py")),
                        "the script is not two levels above the skill")

    def test_G10_2_skill_md_gives_the_steps_in_the_fixed_order(self):
        _, _, body = skill_front()
        order = section(body, "The order")
        steps = re.findall(r"^(\d+)\. (.+?)(?=^\d+\. |\Z)", order, re.M | re.S)
        self.assertEqual([int(n) for n, _ in steps], list(range(1, 9)))
        want = ["`M setup --dry-run`", "the keys to make", "`M setup --linger`", "`key set`", "`M sync --announce`",
                "`M sync`", "`M status`", "`M restore --check`"]
        for (n, text), w in zip(steps, want):
            self.assertTrue(text.startswith(w) or text.startswith("**Person**: " + w.strip("`")) or w in text.split(".")[0],
                            f"step {n} is not {w}: {text[:60]}")
        # Every command the skill tells the agent to run is one the script has.
        for cmd in sorted(set(re.findall(r"`M ([a-z-]+)", body))):
            with self.assertRaises(SystemExit) as e:
                quiet(mirror.main, [cmd, "--help"])
            self.assertEqual(e.exception.code, 0, cmd)

    def test_G10_3_every_step_that_is_the_persons_is_named_with_what_to_say(self):
        _, _, body = skill_front()
        steps = section(body, "The person's steps")
        for name in ("Logging in to GitHub.", "Making each key.", "An organisation's approval.", "Lingering.",
                     "The go for the first copy.", "The administrator's command."):
            m = re.search(rf"\*\*{re.escape(name)}\*\*(.*?)(?=\n\n|\Z)", steps, re.S)
            self.assertTrue(m, name)
            self.assertRegex(m.group(1), r'[Ss]ay[^"]*: "[^"]+"', f"{name} does not say what the agent tells the person")
        self.assertIn("You never log in for them", steps)
        self.assertRegex(steps, r"Agree, Accept or\s+Continue, never press it")
        order = section(body, "The order")
        marked = [n for n, text in re.findall(r"^(\d+)\. (.+?)(?=^\d+\. |\Z)", order, re.M | re.S) if "**Person**" in text]
        self.assertEqual(marked, ["1", "2", "3", "4", "5"], "the person's steps in the order are not the ones named")
        # The key is typed in a terminal of the person's own, never behind `!` (what runs there is shown to the agent).
        self.assertIn("in a terminal window of their own", order)
        self.assertNotIn("`! ", body)
        # What the skill says about lingering matches what setup itself prints (G1.6).
        self.assertIn("sudo loginctl enable-linger", steps)
        self.assertIn("sudo loginctl enable-linger", fread(SCRIPT))



    def test_G10_5_what_the_person_must_hear_comes_as_one_numbered_block(self):
        """A cold start (2026-11-23): the smallest model shortened the dry run to one line and dropped a sentence of
        the privacy line. The person's sentences come as one block, "tell the person 1 of N" to "N of N", so a
        line dropped or merged on the way shows, and SKILL.md says to give the block whole before any words of its
        own."""
        tell = re.compile(r"^tell the person (\d+) of (\d+): (.*)$")

        def block(out, where):
            lines = out.splitlines()
            at = [i for i, x in enumerate(lines) if tell.match(x)]
            self.assertTrue(at, f"{where}: no block for the person")
            self.assertEqual(at, list(range(at[0], at[0] + len(at))), f"{where}: the block is broken up")
            got = [tell.match(lines[i]).groups() for i in at]
            self.assertEqual([(a, b) for a, b, _ in got], [(str(i), str(len(at))) for i in range(1, len(at) + 1)],
                             f"{where}: not numbered 1 of N to N of N")
            return [t for _, _, t in got]

        with Home() as h:
            saved = mirror.has_git_lfs
            mirror.has_git_lfs = lambda: False
            try:
                rc, out = h.setup("--dry-run")
            finally:
                mirror.has_git_lfs = saved
            self.assertEqual(rc, 0, out)
            said = block(out, "setup --dry-run")
            plan = mirror.setup_plan(mirror.paths(), {})
            self.assertEqual(said[:len(plan)], [f"Setup would {step}." for step in plan], "a step of setup unsaid")
            self.assertTrue(any(x.startswith("After a restart") for x in said), said)
            self.assertIn(mirror.LFS_MISSING, said)
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            saved = mirror.has_git_lfs
            mirror.has_git_lfs = lambda: True
            try:
                rc, out = w.sync("--announce")
            finally:
                mirror.has_git_lfs = saved
            self.assertEqual(rc, 0, out)
            said = block(out, "sync --announce")
            self.assertEqual(len(said), 7, said)
            self.assertTrue(said[0].startswith("The first copy: 1 repositories from 1 owner(s)"), said)
            self.assertEqual(said[1], "They are: someone/alpha.")
            self.assertIn(f"The copies go into the forge on this computer only, http://127.0.0.1:{w.port}/", said[2])
            self.assertIn("Nothing is ever written to GitHub.", said[2])
            self.assertTrue(said[3].startswith("After a restart"), said)
            self.assertEqual(said[4], mirror.key_end_line("someone", ""))
            self.assertEqual(said[5:], [mirror.NOT_COPIED, mirror.PRIVATE_AS_THIS_COMPUTER])
            self.assertEqual(out.splitlines()[-1], "Nothing was copied. When the person says go, run: sync")
        _, _, body = skill_front()
        self.assertIn("Lines that start `tell the person 1 of N:` to `tell the person N of N:` are for the\nperson.",
                      body)
        self.assertIn("word for word and in that order, before any\nwords of your own", body)
        order = section(body, "The order")
        for step in ("1. `M setup --dry-run`. Give the person its `tell the person` block whole",
                     "5. `M sync --announce`. Give the person its `tell the person` block whole"):
            self.assertIn(step, order)


    def test_G10_6_what_the_person_must_hear_before_the_go_is_in_the_block_they_say_go_to(self):
        """A cold start (2026-11-23, run 2): the smallest model gave setup's block 0 of 3, because it went straight on
        to the next commands, while the announcement's block, the last thing before the go, came 5 of 5 on both
        models. So setup prints no block; what happens after a restart, a missing git lfs and how private the copy
        is are in the announcement's block. Lingering itself is chosen at setup by the person's own yes or no
        (--linger, --no-linger), which the tool holds, so an agent may say that part in its own words."""
        tell = re.compile(r"^tell the person (\d+) of (\d+): (.*)$", re.M)
        with World(self) as w:
            self.assertEqual(tell.findall(w.setup_out), [], "setup printed a block an agent can lose")
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            for lfs, linger, said_no in ((False, "yes", None), (True, "no", "no"), (True, "no", None)):
                saved_lfs, saved_linger = mirror.has_git_lfs, mirror.linger_state
                mirror.has_git_lfs, mirror.linger_state = (lambda v=lfs: v), (lambda v=linger: v)
                st = mirror.load_json(mirror.paths()["state"], {})
                st.pop("linger_said", None)
                if said_no:
                    st["linger_said"] = said_no
                mirror.save_state(mirror.paths(), st)
                try:
                    rc, out = w.sync("--announce")
                finally:
                    mirror.has_git_lfs, mirror.linger_state = saved_lfs, saved_linger
                self.assertEqual(rc, 0, out)
                said = [t for _, _, t in tell.findall(out)]
                self.assertEqual(said[-1], mirror.PRIVATE_AS_THIS_COMPUTER)
                restart = [x for x in said if x.startswith("After a restart")]
                self.assertEqual(len(restart), 1, said)
                if mirror.system()[0] == "darwin":
                    pass                                      # a Mac says its own case (G1.6); one line is enough
                elif linger == "yes":
                    self.assertIn("it starts by itself: lingering is on", restart[0])
                elif said_no:
                    self.assertIn("lingering is off, as you chose", restart[0])
                    self.assertIn("run setup --linger", restart[0])
                else:
                    self.assertIn("administrator", restart[0])
                    self.assertIn("sudo loginctl enable-linger", restart[0])
                self.assertEqual(mirror.LFS_MISSING in said, not lfs, said)
                self.assertEqual(out.splitlines()[-1], "Nothing was copied. When the person says go, run: sync")
        _, _, body = skill_front()
        order = section(body, "The order")
        self.assertIn("what happens after a restart", order)

    def test_G10_9_one_version_in_every_place(self):
        """The family's rule 10: one version, in every place it is written: the manifest, the script, the
        changelog's first heading ('Unreleased' only while nothing is released) and this repository's own catalog
        entry."""
        ver = version()
        self.assertEqual(mirror.VERSION, ver, "the script")
        entry = [p for p in jload(os.path.join(ROOT, ".claude-plugin", "marketplace.json"))["plugins"]
                 if p["name"] == "windstorm-mirror-for-github"]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0].get("version"), ver, "the catalog entry")
        heads = re.findall(r"^## (.+)$", fread(os.path.join(ROOT, "CHANGELOG.md")), re.M)
        self.assertTrue(heads, "the changelog has no heading")
        if ver == "0.0.0":
            self.assertEqual(heads[0], "Unreleased")
        else:
            self.assertRegex(heads[0], "^" + re.escape(ver) + r"(?: \(\d{4}-\d{2}-\d{2}\))?$", "the changelog's heading")
            self.assertNotIn("Unreleased", heads[:1])

    def test_G10_10_public_text_names_roles_never_our_own_labels(self):
        """The family's rule 18: public text names kinds and roles ('another program sharing the login', 'a sister
        skill'), never our own labels for how we work. The words are built here so this file does not hold them."""
        ours = [re.compile(r"\b" + w + r"\b", re.I) for w in ("la" + "nes?", "fl" + "eet", "test-" + "now", "board-" + "say")]
        files = subprocess.run(["git", "-C", ROOT, "ls-files"], capture_output=True, text=True).stdout.split()
        if not files:                                  # a copy with no .git (a test box): every file but caches
            files = [os.path.relpath(os.path.join(d, n), ROOT) for d, ds, ns in os.walk(ROOT)
                     for n in ns if "__pycache__" not in d and not n.endswith(".pyc")]
        self.assertGreater(len(files), 10)
        found = []
        for name in files:
            if name in ("LICENSE", "NOTICE") or not os.path.isfile(os.path.join(ROOT, name)):
                continue
            try:
                text = fread(os.path.join(ROOT, name))
            except UnicodeDecodeError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if any(p.search(line) for p in ours):
                    found.append(f"{name}:{n}")
        self.assertEqual(found, [])

    def test_G10_11_the_cold_starts_are_the_first_evals_as_cases(self):
        """The family's rule 13: the cold-start runs, written as cases, are the first evals. Each case loads in
        skill-creator's layout (what `claude plugin eval` reads), and the evals' README records every run against
        every grader."""
        root = os.path.join(os.path.dirname(SKILL), "evals")
        cases = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
        self.assertIn("mirror-cold-start-to-the-first-copy-on-the-persons-go", cases)
        readme = fread(os.path.join(root, "README.md"))
        for c in cases:
            self.assertRegex(c, r"^[a-z][a-z0-9-]*$")
            self.assertNotIn(c, ("results", "mocks", "node_modules", "files", "true", "false", "null"))
            prompt = fread(os.path.join(root, c, "prompt.md"))
            m = re.match(r"---\n(.*?)\n---\n\n(.+)", prompt, re.S)
            self.assertTrue(m, f"{c}: prompt.md has no settings block and prompt")
            keys = {line.split(":", 1)[0] for line in m.group(1).splitlines()}
            self.assertLessEqual(keys, {"tags", "max_turns", "timeout_seconds", "allowed_tools"}, c)
            self.assertTrue(m.group(2).strip(), c)
            case = fread(os.path.join(root, c, "case.yaml"))
            self.assertIn('schema_version: "1.1"\n', case)
            self.assertIn(f"name: {c}\n", case)
            self.assertIn("expected_outcome: |\n  ", case)
            graders = sorted(os.listdir(os.path.join(root, c, "graders")))
            self.assertTrue(graders, f"{c}: a case with no grader fails to load")
            for g in graders:
                self.assertRegex(g, r"^\d\d-[a-z0-9-]+\.md$")
                body = fread(os.path.join(root, c, "graders", g))
                self.assertTrue(body.startswith("---\ntype: llm\nfocus: trace\nweight: 1\n---\n\n"), g)
                self.assertTrue(body.split("---\n\n", 1)[1].strip(), g)
                self.assertRegex(readme, r"\n\| " + g[:2] + r" [^|]+\|", f"{g}: the record of runs has no row for it")
            # Before a release, the address the person gives is the repository's, never a machine of ours.
            self.assertIn("https://github.com/TheWindstorm/windstorm-mirror-for-github", prompt)

    def test_G10_7_the_order_is_setup_then_keys_and_a_refusal_names_the_next_step(self):
        """Two cold starts (2026-11-23): the smallest model sent the person to key set before setup, twice. Our
        words gave it room: step 2 made the keys before setup and nothing said the paste waits. Now the order says
        it plainly, and key set's refusal says whose step setup is and what to run after it."""
        _, _, body = skill_front()
        order = section(body, "The order")
        step2 = order.split("\n2. ", 1)[1].split("\n3. ", 1)[0]
        self.assertIn("paste it only in step 4, after setup", step2)
        self.assertIn("Do not give them the `key set` line before step 3 has run", " ".join(step2.split()))
        self.assertNotIn("key set <owner>", step2)
        self.assertIn("4. **Person**, only after step 3 has run: `key set` for each owner.", order)
        self.assertLess(order.index("`M setup --linger`"), order.index("key set <owner>"))
        line = KEY_BEFORE_SETUP_SOMEONE
        self.assertIn("setup is your agent's step", line)
        self.assertIn("then run key set someone again", line)
        with Home() as h:
            rc, out = quiet(mirror.main, ["key", "set", "someone"])
            self.assertEqual(rc, 2)
            self.assertEqual(out.strip(), line)

    def test_G10_8_the_agent_reaches_github_through_the_tool_only(self):
        """A cold start (2026-11-23): the smallest model probed GitHub with anonymous git and with SSH. On the VM
        both were refused; on a person's own computer their SSH key or gh login answers, and reaches every
        repository they can, with their right to write. So SKILL.md says the road is the tool only, and why; and no
        line of it hands the agent a command that reaches GitHub another way."""
        _, _, body = skill_front()
        never = " ".join(section(body, "Never").split())
        self.assertIn("Reach GitHub only through `M`, never by yourself, not even to look", never)
        for road in ("`git clone`", "`git ls-remote`", "`ssh git@github.com`", "`gh`", "`curl`", "web fetch"):
            self.assertIn(road, never)
        why = never.split("Reach GitHub only through `M`", 1)[1].split(" - ", 1)[0]
        self.assertIn("Why: the person's own git, SSH key or `gh` login reaches every repository they can reach", why)
        self.assertIn("the one read-only key", why)
        self.assertIn("`M sync --announce`: it reads with that key and copies nothing", why)
        # Outside that one line, no command in SKILL.md goes to GitHub by another road.
        rest = body.replace(section(body, "Never"), "")
        spans = re.findall(r"`([^`\n]+)`", rest)
        other = [c for c in spans if re.match(r"(git|gh|ssh|curl|wget|scp)\b", c.strip())]
        self.assertEqual(other, [])
        # And the tool's own announcement reads GitHub without copying: the road it names is real.
        self.assertIn("G3.7: the announcement never copies", open(mirror.__file__).read())


class G6Words(unittest.TestCase):
    NEXT = re.compile(r"\b(run|again|install|make|ask|try|wait|free|name|check|open an issue)\b|`|python3 -I", re.I)

    def test_G6_8_every_stop_and_needs_you_line_names_what_happened_and_the_next_step(self):
        """A cold start (2026-11-23): restore --check stopped on a git error with no next step, and the agent opened
        the script's source four times to find one. Every line the tool says just before it stops with a failing
        code, and every message it stops with, names the one next step. A line built elsewhere is checked where it
        is built."""
        import ast
        tree = ast.parse(fread(SCRIPT))

        def words(n):
            return " ".join(x.value for x in ast.walk(n) if isinstance(x, ast.Constant) and isinstance(x.value, str))

        funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        lacking, built, seen = [], set(), 0
        for node in ast.walk(tree):
            for field in ("body", "orelse", "finalbody"):
                b = getattr(node, field, None)
                if not isinstance(b, list):
                    continue
                for a, nxt in zip(b, b[1:]):
                    if not (isinstance(a, ast.Expr) and isinstance(a.value, ast.Call)
                            and getattr(a.value.func, "id", None) == "say" and isinstance(nxt, ast.Return)
                            and isinstance(nxt.value, ast.Constant) and nxt.value.value not in (0, None)):
                        continue
                    seen += 1
                    arg, mine = a.value.args[0], set()
                    for x in ast.walk(arg):                   # names and calls the words are built from
                        if isinstance(x, ast.Name) and x.id.isupper():
                            mine.add(x.id)
                        elif isinstance(x, ast.Call) and getattr(x.func, "id", None) in funcs:
                            mine.add(x.func.id)
                        elif (isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute) and x.func.attr == "replace"
                              and isinstance(x.func.value, ast.Name) and x.func.value.id == "why"):
                            mine.add("check_key")             # say(why.replace(key, ...)): the key check's words
                    if isinstance(arg, ast.Name) and not arg.id.isupper():
                        mine.add(f"<{arg.id}>")
                    if words(arg).strip() == "STOPPED:":
                        mine.add("HardStop")                  # says a HardStop's words: every raise is checked
                    built |= mine
                    if mine & {"git_missing_sentence", "check_key", "LINGER_CHOICE", "<far>", "HardStop"}:
                        continue                              # checked below, where the words are built
                    if not self.NEXT.search(words(arg)):
                        lacking.append((a.lineno, words(arg)[:90]))
            if (isinstance(node, ast.Raise) and isinstance(getattr(node, "exc", None), ast.Call)
                    and getattr(node.exc.func, "id", None) in ("SystemExit", "HardStop")):
                seen += 1
                if not self.NEXT.search(words(node.exc)):
                    lacking.append((node.lineno, words(node.exc)[:90]))
        self.assertGreater(seen, 50, "too few stop lines found to call this a proof")
        self.assertEqual(lacking, [])
        # Built elsewhere: each is checked where it is built.
        self.assertTrue(self.NEXT.search(mirror.LINGER_CHOICE))
        self.assertTrue(self.NEXT.search(mirror.git_missing_sentence()))
        self.assertTrue(self.NEXT.search(words(funcs["git_out_of_service_reach"])))
        for r in ast.walk(funcs["check_key"]):
            if isinstance(r, ast.Return) and isinstance(r.value, ast.Tuple) and \
                    isinstance(r.value.elts[0], ast.Constant) and r.value.elts[0].value is None:
                self.assertTrue(self.NEXT.search(words(r.value)), words(r.value))
        data = {"SKILL_REPO", "GITEA_VERSION", "last_line",          # facts inside a line, not its words
                "lock_holder", "UNINSTALL_WAIT", "VERSION"}                     # (which process; how long uninstall waits)
        self.assertEqual(built - data - {"LINGER_CHOICE", "git_missing_sentence", "check_key", "<far>", "HardStop"},
                         set(),
                         "a stop line is built from something this test does not check")

class Invariants(unittest.TestCase):
    def test_INV6_standard_library_only_and_no_other_network(self):
        import ast
        tree_ = ast.parse(fread(SCRIPT))
        mods = {a.name.split(".")[0] for n in ast.walk(tree_) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree_) if isinstance(n, ast.ImportFrom) and n.module}
        if hasattr(sys, "stdlib_module_names"):
            self.assertEqual(mods - set(sys.stdlib_module_names), set(), "a module outside the standard library")
        # Every address it can reach: GitHub (its API and git), Gitea's download site, its own forge on this
        # computer. The plist's DOCTYPE names Apple's DTD and is never fetched.
        allowed = {"http://127.0.0.1", "https://api.github.com", "https://github.com", "https://dl.gitea.com/gitea",
                   "https://github.com/TheWindstorm/windstorm-mirror-for-github", "http://www.apple.com/DTDs/PropertyList-1.0.dtd",
                   "https://git-lfs.com",                     # named to the person (M28), never reached
                   "https://github.com/settings/personal-access-tokens",  # named to the person (M38), never reached
                   "http://127.0.0.1:9"}                  # the dead proxy that shuts every other host off (G7.2)
        found = set(re.findall(r"https?://[A-Za-z0-9.:_/-]*[A-Za-z0-9]", fread(SCRIPT)))
        self.assertEqual(found - allowed, set(), "an address outside the allowed ones")
        self.assertEqual(len(re.findall(r"urlopen\(|build_opener\(", fread(SCRIPT))), 5,
                         "a new way to reach the network: name it here and in the plan")
        # What it runs: git, the forge's binary, the system's service manager, the system's own address list,
        # and itself (to check a self-update before it is swapped in).
        progs = set()
        for n in ast.walk(tree_):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "run" and n.args and isinstance(n.args[0], ast.List):
                first = n.args[0].elts[0] if n.args[0].elts else None
                progs.add(first.value if isinstance(first, ast.Constant) else ast.unparse(first))
        self.assertEqual(progs - {"git", "systemctl", "loginctl", "launchctl", "ip", "ifconfig", "sys.executable",
                                  "running_bin(p)", "binary", "defaults", "fdesetup", "pkcheck"}, set(), progs)


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


def stand_ins_unproven(rows, at):
    """T2: the rows due at version `at` whose test leans on a stand-in and that no real machine has proven."""
    return [r["id"] for r in rows if v(r["ship_in"]) <= at and r.get("stand_in", "none") != "none"
            and not r.get("proven") and not r.get("exception")]

class Promises(unittest.TestCase):
    """PROMISES.json holds what real machines, and the people using them, showed must change, and the version
    each change is due in. A lesson is a gate, not a note: these tests refuse a version while a promise due in it
    is not built, and the version after it until the promise was seen to work on a real machine."""

    def setUp(self):
        self.doc = jload(os.path.join(ROOT, "PROMISES.json"))
        self.rows = self.doc["rows"]
        self.version = v(version())
        self.tests = set(collected_tests())          # tests unittest runs, not text that looks like one
        self.plan = fread(os.path.join(ROOT, "DNA-STRAND-MASTER-PLAN.md"))

    def test_PROMISES_every_row_is_whole_and_its_codon_is_in_the_plan(self):
        ids = [r["id"] for r in self.rows]
        self.assertEqual(len(ids), len(set(ids)), "two rows share an id")
        for r in self.rows:
            for k in ("id", "seen", "promise", "codon", "owner", "first_ship_in", "ship_in"):
                self.assertTrue(r.get(k), f"{r.get('id')}: '{k}' is empty")
            for k in ("test", "stand_in", "proven", "exception", "issue", "moved_why", "moved_by_person"):
                self.assertIn(k, r, f"{r['id']}: the column '{k}' is missing")
            self.assertIn(r["owner"], ("person", "maintainer"), r["id"])
            self.assertIn(f"| {r['codon']} |", self.plan, f"{r['id']}: codon {r['codon']} is not in the plan")

    def test_PROMISES_a_lesson_not_taken_is_written_down_with_its_reason(self):
        for d in self.doc.get("declined", []):
            self.assertTrue(d.get("heard") and d.get("why"), "a declined lesson needs what was heard and why it was not taken")

    def test_PROMISES_a_row_names_a_test_only_once_it_is_built(self):
        """Rule 4 (the family's): a row's test field says one thing only, that this test exists and the runner
        reports it; at any version, a row still to build included. An empty field is a row not yet built (where
        its test will come from is in its issue). The gate (join_reports) counts each row's test as a proof."""
        to_build = set(re.findall(r"`(test_[A-Za-z0-9_]+)` \*\(to build\)\*", self.plan))
        for r in self.rows:
            if r.get("test"):
                self.assertIn(r["test"], self.tests, f"{r['id']} names {r['test']}, which is not a test the runner runs")
                self.assertNotIn(r["test"], to_build, f"{r['id']} names a test the plan marks (to build)")
                self.assertIn(f"`{r['test']}`", self.plan, f"{r['id']}: its test is not named in the plan")

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

    def test_PROMISES_a_row_with_a_stand_in_is_proven_before_its_tag(self):
        """Rule 3 (the family's): a row whose test leans on a stand-in's idea of how GitHub, the service manager,
        a Mac or an older Python answers is seen to hold on a real machine before its version is tagged, not only
        by the version after. A release branch raises the version, so there this refuses every such row due in it
        whose 'proven' is empty; on main (0.0.0) nothing is due yet."""
        for r in self.rows:
            self.assertTrue(r.get("stand_in"), f"{r['id']}: say what its test stands in for, or 'none'")
        made_up = {"id": "X1", "ship_in": "0.1.0", "stand_in": "GitHub", "proven": ""}
        self.assertEqual(stand_ins_unproven([made_up], v("0.1.0")), ["X1"])
        self.assertEqual(stand_ins_unproven([made_up], v("0.0.0")), [])
        self.assertEqual(stand_ins_unproven([dict(made_up, proven="2026-11-23, a stock VM")], v("0.1.0")), [])
        self.assertEqual(stand_ins_unproven([dict(made_up, stand_in="none")], v("0.1.0")), [])
        # A named exception, by the owner's word only, lets the tag go before the real sighting.
        self.assertEqual(stand_ins_unproven([dict(made_up, exception="By the owner's word, ...")], v("0.1.0")), [])
        for r in self.rows:
            if r.get("exception"):
                self.assertTrue(r["exception"].startswith("By the owner's word, "), f"{r['id']}: an exception is the owner's")
        unproven = stand_ins_unproven(self.rows, self.version)
        self.assertEqual(unproven, [], f"{version()} cannot be tagged: these rows lean on a stand-in and no real "
                                       "machine has proven them yet (see each row's stand_in)")

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
        real = set(collected_tests())
        codon_tests = {n for n in real if re.match(r"test_(?:G|INV|T\d)", n)}
        self.assertEqual(to_build & real, set(), "a test marked 'to build' exists: remove the mark")
        if to_build:
            self.assertEqual(version(), "0.0.0", "a proof may be marked 'to build' only while nothing is released")
        self.assertEqual(named - real - to_build, set(), "the plan names tests that do not exist")
        self.assertEqual(codon_tests - named, set(), "tests with no codon in the plan")

    def test_T3_a_commits_checks_run_once(self):
        """M43 (the family's rule 17, 2026-11-23): one stack of pull requests queued 17 runs, each commit twice,
        because the workflow ran on every push and every pull request. It runs on pull requests, and on pushes
        only to main and release-*."""
        wf = fread(os.path.join(ROOT, ".github", "workflows", "tests.yml"))
        on = re.search(r"^on:\n((?:[ #].*\n)+)", wf, re.M)
        self.assertIsNotNone(on, "the workflow's triggers are not a block of their own")
        block = re.sub(r"#.*", "", on.group(1))
        self.assertEqual([l.strip() for l in block.splitlines() if l.strip()],
                         ["pull_request:", "push:", "branches: [main, 'release-*']"])

    def test_the_gate_refuses_every_way_a_test_can_hide(self):
        """M19: one real test, hidden each way a sister skill found, in a throwaway copy of the suite. The gate
        (`--gate-only`) must refuse each one, and accept the copy left as it is."""
        name = "test_G1_6_on_a_mac_it_comes_back_by_itself_only_with_the_systems_own_login"
        src = fread(os.path.abspath(__file__))
        head = f"    def {name}(self):\n"
        start = src.index(head)
        end = src.index("\n\n\n", start) + 1
        body, rest = src[start:end], src[:start] + src[end:]
        indented = "".join("    " + line if line.strip() else line for line in body.splitlines(True))
        skip_inside = head + '        raise unittest.SkipTest("no real pinned Gitea for this system")\n'
        q3 = '"' * 3
        as_text = "\n_OLD = " + q3 + "\n" + body.replace(q3, "'" * 3) + q3 + "\n\n\nclass G2(unittest.TestCase):"
        cases = {
            "left as it is": src,
            "nested in a helper after its return":
                rest.replace("    return hits, read\n", "    return hits, read\n\n" + body, 1),
            "renamed so the runner does not collect it": src.replace(head, head.replace("def test_", "def check_"), 1),
            "under if False": src[:start] + "    if False:\n" + indented + src[end:],
            "skip-marked, any reason": src.replace(head, "    @unittest.skip('later')\n" + head, 1),
            "skip-marked with an honest-sounding reason":
                src.replace(head, "    @unittest.skip('no real pinned Gitea for this system')\n" + head, 1),
            "skipped inside with an honest reason": src.replace(head, skip_inside, 1),
            "shadowed by a later empty one": src[:end] + head + "        pass\n\n" + src[end:],
            "left as text in a string": rest.replace("\nclass G2(unittest.TestCase):", as_text, 1),
        }
        env = {k: val for k, val in os.environ.items() if k != "WSM_TEST_REPORT"}
        for case, text in cases.items():
            self.assertTrue(case == "left as it is" or text != src, f"{case}: the case was not planted")
            with tempfile.TemporaryDirectory(prefix="wsm-gate-") as d:
                for f in ("scripts", "skills", "docs", ".claude-plugin", "DNA-STRAND-MASTER-PLAN.md", "PROMISES.json",
                          "README.md", "CHANGELOG.md"):
                    if os.path.exists(os.path.join(ROOT, f)):
                        os.symlink(os.path.join(ROOT, f), os.path.join(d, f))
                os.makedirs(os.path.join(d, "tests"))
                os.symlink(os.path.join(ROOT, "tests", "fake_github.py"), os.path.join(d, "tests", "fake_github.py"))
                copy = os.path.join(d, "tests", "test_mirror.py")
                with open(copy, "w", encoding="utf-8") as f:
                    f.write(text)
                r = subprocess.run([sys.executable, "-I", copy, "--gate-only", name], capture_output=True, text=True,
                                   env=env, stdin=subprocess.DEVNULL, timeout=300)
                said = r.stdout + r.stderr[-2000:]
                if case == "left as it is":
                    self.assertEqual(r.returncode, 0, said)
                elif case == "skipped inside with an honest reason" and _no_gitea_here():
                    # So it is here (no real Gitea in this account): "not run here", never a pass. In CI the
                    # condition never holds, so there it is refused, and the join needs a pass on some job.
                    self.assertEqual(r.returncode, 0, said)
                    self.assertIn("Not run here (1)", r.stdout, said)
                else:
                    self.assertNotEqual(r.returncode, 0, f"{case}: the gate let it through\n{said}")
                    self.assertIn("GATE:", r.stdout, f"{case}: refused, but not by the gate\n{said}")

    def test_no_codon_is_used_twice_in_the_plan(self):
        import collections
        plan = fread(os.path.join(ROOT, "DNA-STRAND-MASTER-PLAN.md"))
        seen = collections.Counter(re.findall(r"^\| ((?:G|INV)[0-9.]+) \|", plan, re.M))
        self.assertEqual([c for c, n in seen.items() if n > 1], [], "a codon is used for two claims")


RELEASE_ONLY = {"README.md", "CHANGELOG.md", ".claude-plugin/plugin.json"}


def release_branch():
    """The branch's name when it is a release branch (release-X.Y.Z): from CI, or from git here; else ''. A pull
    request into main never gets the exception: there the head is judged by main's own rule."""
    if os.environ.get("GITHUB_BASE_REF") == "main":
        return ""
    name = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME") or subprocess.run(
        ["git", "-C", ROOT, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
    return name if re.fullmatch(r"release-\d+\.\d+\.\d+", name or "") else ""


class Package(unittest.TestCase):
    def test_the_script_and_the_plugin_carry_the_same_version(self):
        self.assertEqual(mirror.VERSION, version())

    def test_nothing_is_offered_for_install_before_0_1_0(self):
        """Until the first release the version stays 0.0.0 and the README says nothing can be installed. The one
        exception is a release branch (release-X.Y.Z), where a cold start is sent before the tag: it may offer
        the install, and it is main plus the README, the changelog and the manifest, nothing else."""
        if v(version()) >= (0, 1, 0):
            return
        readme = fread(os.path.join(ROOT, "README.md"))
        branch = release_branch()
        if not branch:
            self.assertIn("Nothing here can be installed yet", readme)
            return
        base = subprocess.run(["git", "-C", ROOT, "merge-base", "origin/main", "HEAD"], capture_output=True, text=True)
        self.assertEqual(base.returncode, 0, f"{branch}: main is not here to compare with: {base.stderr.strip()}")
        today = subprocess.run(["git", "-C", ROOT, "merge-base", "--is-ancestor", "origin/main", "HEAD"])
        self.assertEqual(today.returncode, 0, f"{branch} does not hold today's main: merge main into it first")
        changed = subprocess.run(["git", "-C", ROOT, "diff", "--name-only", base.stdout.strip(), "HEAD"],
                                 capture_output=True, text=True).stdout.split()
        self.assertTrue(changed, f"{branch} is main itself")
        self.assertEqual(sorted(set(changed) - RELEASE_ONLY), [], f"{branch} differs from main outside {RELEASE_ONLY}")
        self.assertIn("## Set it up", readme)


# ------------------------------------------------------------------ the gate: what counts as a proof (M19)
#
# A proof named in the plan counts only when this runner saw it pass. The suite itself can hide a test six ways
# (nested in a helper, renamed, under `if False:`, skipped, shadowed by a later one of the same name, or left as
# text in a string), so the gate does not trust the file's text, the test's name, or "OK":
#   - shape_problems() reads the file and refuses a name defined twice, two classes of one name, and a test-like
#     def anywhere but directly in a TestCase class;
#   - the runner records each test's outcome; a skip mark (unittest's decorator) is refused whatever its reason;
#   - a skip raised inside a test counts as "not run here" only when its reason is in HONEST_SKIPS and that
#     condition holds on this machine right now; in CI the conditions never hold, so every skip there fails;
#   - `--join` reads every CI job's report and needs each proof to have passed on at least one job.
# The self-test (Strand.test_the_gate_refuses_every_way_a_test_can_hide) plants each case and must see it refused.

def _no_gitea_here():
    return not os.environ.get("WSM_REQUIRE_GITEA") and not real_gitea()


def _no_lfs_here():
    return not os.environ.get("WSM_REQUIRE_GITEA") and not mirror.has_git_lfs()


HONEST_SKIPS = {"no real pinned Gitea for this system": _no_gitea_here, "no git lfs here": _no_lfs_here}


def collected_tests():
    """{name: class} for every test method unittest collects from this module."""
    return {n: c for c in list(globals().values()) if isinstance(c, type) and issubclass(c, unittest.TestCase)
            for n in unittest.defaultTestLoader.getTestCaseNames(c)}


def plan_proofs():
    plan = fread(os.path.join(ROOT, "DNA-STRAND-MASTER-PLAN.md"))
    named = set(re.findall(r"`(test_[A-Za-z0-9_]+)`", plan))
    return named - set(re.findall(r"`(test_[A-Za-z0-9_]+)` \*\(to build\)\*", plan))


def shape_problems(path):
    import ast
    tree_ = ast.parse(fread(path))
    out, classes, direct = [], set(), set()
    for n in tree_.body:
        if isinstance(n, ast.ClassDef):
            if n.name in classes:
                out.append(f"two classes are named {n.name}")
            classes.add(n.name)
            seen = set()
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    direct.add(id(m))
                    if m.name in seen:
                        out.append(f"{n.name}.{m.name} is defined twice: the later one hides the first")
                    seen.add(m.name)
    for node in ast.walk(tree_):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
                and id(node) not in direct):
            out.append(f"{node.name} (line {node.lineno}) is not where the runner can collect it")
    return out


class Recorder(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.outcome = {}

    def _set(self, test, what):
        name = test.id().rsplit(".", 1)[-1]
        if self.outcome.get(name) != "failed":        # a failure, of the test or of any part of it, stays
            self.outcome[name] = what

    def addSuccess(self, test):
        super().addSuccess(test)
        self._set(test, "passed")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        method = getattr(test, getattr(test, "_testMethodName", ""), None)
        marked = getattr(method, "__unittest_skip__", False) or getattr(type(test), "__unittest_skip__", False)
        self._set(test, "skip-marked: " + reason if marked else "skipped: " + reason)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._set(test, "failed")

    def addError(self, test, err):
        super().addError(test, err)
        self._set(test, "failed")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._set(test, "failed")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._set(test, "failed")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._set(test, "failed")


def gate(outcome, proofs):
    """(refused, not_run_here) for the proofs named, from what the runner saw."""
    refused, not_here = [], []
    for name in sorted(proofs):
        o = outcome.get(name)
        if o == "passed":
            continue
        reason = o[len("skipped: "):] if o and o.startswith("skipped: ") else None
        if reason in HONEST_SKIPS and HONEST_SKIPS[reason]():
            not_here.append(name)
            continue
        if reason in HONEST_SKIPS:
            o += " (but that is not so on this machine)"
        refused.append(f"{name}: {o or 'never ran'}")
    return refused, not_here


def row_tests():
    """Every built row's test (rule 4): the gate counts it as a proof like the plan's own."""
    rows = jload(os.path.join(ROOT, "PROMISES.json"))["rows"]
    return {r["test"] for r in rows if r.get("test")}


def join_reports(paths, expect):
    reports = [jload(p) for p in paths]
    proofs = plan_proofs() | row_tests()
    passed = set().union(*(set(r["passed"]) for r in reports)) if reports else set()
    refused = [x for r in reports for x in r["refused"]]
    if len(reports) != expect:
        refused.append(f"{len(reports)} job reports, {expect} expected")
    refused += [f"{n} passed on none of the {len(reports)} jobs" for n in sorted(proofs - passed)]
    for x in refused:
        print("GATE:", x)
    print(f"{len(proofs & passed)} of {len(proofs)} proofs passed on at least one of {len(reports)} jobs")
    return 1 if refused else 0


def main(argv):
    verbosity = 2 if "-v" in argv else 1
    args = [a for a in argv if a != "-v"]
    if args[:1] == ["--join"]:
        expect = int(args[args.index("--expect") + 1]) if "--expect" in args else 0
        files = [a for i, a in enumerate(args[1:], 1) if a != "--expect" and args[i - 1] != "--expect"]
        return join_reports(files, expect)
    me = sys.modules[__name__]
    gate_only = args[1] if args[:1] == ["--gate-only"] and len(args) > 1 else None
    tests = collected_tests()
    if gate_only:
        suite = unittest.TestSuite([tests[gate_only](gate_only)] if gate_only in tests else [])
    elif args:
        suite = unittest.defaultTestLoader.loadTestsFromNames(args, me)
    else:
        suite = unittest.defaultTestLoader.loadTestsFromModule(me)
    result = unittest.TextTestRunner(verbosity=verbosity, resultclass=Recorder, stream=sys.stderr).run(suite)
    if args and not gate_only:
        print("A part of the suite was run, so the gate was not applied.", file=sys.stderr)
        return 0 if result.wasSuccessful() else 1
    problems = shape_problems(os.path.abspath(__file__))
    refused, not_here = gate(result.outcome, {gate_only} if gate_only else plan_proofs() | row_tests())
    refused = problems + refused
    for x in refused:
        print("GATE:", x)
    if not_here:
        print(f"Not run here ({len(not_here)}), each must pass on a CI job: " + ", ".join(not_here))
    report = os.environ.get("WSM_TEST_REPORT")
    if report:
        with open(report, "w", encoding="utf-8") as f:
            json.dump({"system": "/".join(mirror.system()), "python": sys.version.split()[0],
                       "passed": sorted(n for n, o in result.outcome.items() if o == "passed"),
                       "not_run_here": not_here, "refused": refused}, f, indent=1)
    return 0 if result.wasSuccessful() and not refused else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
