#!/usr/bin/env python3
"""Tests for Windstorm Mirror for GitHub. Standard library only: python3 tests/test_mirror.py -v"""
import atexit
import contextlib
import hashlib
import shlex
import signal
import tarfile
import http.server
import inspect
import io
import json
import os
import plistlib
import random
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

GIT_TRAP = "[[[ this home's git config is a trap: a git call of the suite read it instead of GIT_CONFIG_GLOBAL\n"


SESSION_AND_MOVED = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "TMUX", "WSM_HOME", "XDG_DATA_HOME",
                     "XDG_STATE_HOME", "XDG_CACHE_HOME")


def _a_home_of_the_suites_own():
    """T6, rule 17: the suite reads and writes nothing of the account that runs it. Before the skill is imported,
    the test process gets a home of its own and its own XDG_CONFIG_HOME, git's global config at an empty file and
    git's system config off; it loses this account's lines to its session (XDG_RUNTIME_DIR, DBUS_SESSION_BUS_ADDRESS,
    TMUX) and any setting that would put the tool's folders outside that home. The home is the base: git older than 2.32 does not know GIT_CONFIG_GLOBAL and reads
    the home's config instead, which is this empty home's. Where git knows it, the home's own git config is a trap,
    so a git call that drops GIT_CONFIG_GLOBAL fails its test. Home, below, still gives each World test its own."""
    home = tempfile.mkdtemp(prefix="wsm-suite-home-")
    empty = os.path.join(home, "gitconfig-empty")
    open(empty, "w").close()
    os.environ.update(HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"), GIT_CONFIG_GLOBAL=empty,
                      GIT_CONFIG_NOSYSTEM="1")
    for k in SESSION_AND_MOVED:          # the account's session, and any setting that would move the tool's folders
        os.environ.pop(k, None)
    try:
        got = subprocess.run(["git", "version"], capture_output=True, text=True, timeout=30).stdout
        knows = tuple(int(x) for x in re.findall(r"\d+", got)[:2]) >= (2, 32)
    except (OSError, subprocess.TimeoutExpired):
        knows = False
    if knows:
        for f in (os.path.join(home, ".gitconfig"), os.path.join(home, ".config", "git", "config")):
            os.makedirs(os.path.dirname(f), exist_ok=True)
            with open(f, "w") as fh:
                fh.write(GIT_TRAP)
    return home, empty, knows


SUITE_HOME, SUITE_GITCONFIG, GIT_TRAP_SET = _a_home_of_the_suites_own()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "mirror.py")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import mirror  # noqa: E402

_REAL_LOOSE = mirror.loose
_SUITES_OWN = tuple({os.path.realpath(tempfile.gettempdir()), os.path.realpath(SUITE_HOME)})


def _suites_answer(d, accts):
    """Rule 17 (seen 2026-11-24 on a stock runner: its freshly downloaded Python sat in a folder every account could
    write in, and eleven tests that do not test the walk heard of it): a folder outside the suite's own temporary
    folders, such as the folder of the Python that runs the suite, is the machine's, and the suite does not lean on
    its modes; its answer for such a folder is closed. G9.12 plants open folders inside its own, the Python's
    folder included, so the walk's sentences are held on every machine."""
    r = os.path.realpath(d)
    if not any(r == t or r.startswith(t + os.sep) for t in _SUITES_OWN):
        return None
    return _REAL_LOOSE(d, accts)


mirror.loose = _suites_answer


class _PassableAbove:
    """os for the skill, but a folder above `top` reads as one every account may pass (its group's and others' x bits set)."""
    def __init__(self, top):
        self._top = top

    def __getattr__(self, name):
        return getattr(os, name)

    def stat(self, path, *a, **k):
        s_ = os.stat(path, *a, **k)
        r = os.path.realpath(path) if isinstance(path, (str, bytes, os.PathLike)) else None
        if r is not None and r != self._top and self._top.startswith(r.rstrip(os.sep) + os.sep):
            return os.stat_result((s_.st_mode | 0o011,) + tuple(s_)[1:])
        return s_
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
TEMPS.append(SUITE_HOME)


def _no_road_to_github(real):
    """The suite never reaches GitHub itself: a read of github.com from any test fails that test at once."""
    def urlopen(req, *a, **k):
        host = (urllib.parse.urlsplit(getattr(req, "full_url", req)).hostname or "").lower()
        if host == "github.com" or host.endswith(".github.com"):
            raise AssertionError(f"a test reached GitHub itself ({host}); the suite stands GitHub in on loopback")
        return real(req, *a, **k)
    return urlopen


urllib.request.urlopen = _no_road_to_github(urllib.request.urlopen)


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


BOOT_ID = "89c29fe6010d48b2886cf8dc3e19d3ca"     # what %b gave on the probe's guest


def sd_standin_words(value, home):
    """THE SUITE'S STAND-IN for systemd 255 reading one ExecStart= or Environment= value into words, as seen
    2026-11-25 on two stock Ubuntu 24.04 guests (row M148; the planted-home walk proves it on a real machine): words
    part at blanks outside quotes; inside double or single quotes a blank is kept; a backslash takes the character
    after it; %% is %, %h the home, %b the boot id, any other specifier refuses the line (None); a $ is kept as written
    (the bus holds the line before $ is expanded at start). None also for a quote left open."""
    words, cur, quote, inword, i = [], "", None, False, 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            cur, inword, i = cur + value[i + 1], True, i + 2
            continue
        if quote:
            if c == quote:
                quote = None
            else:
                cur += c
        elif c in "\"'":
            quote, inword = c, True
        elif c in " \t":
            if inword:
                words.append(cur)
            cur, inword = "", False
        else:
            cur, inword = cur + c, True
        i += 1
    if quote:
        return None
    if inword:
        words.append(cur)
    out = []
    for w in words:
        r, j = "", 0
        while j < len(w):
            if w[j] != "%":
                r, j = r + w[j], j + 1
                continue
            spec = {"%": "%", "h": home, "b": BOOT_ID}.get(w[j + 1:j + 2])
            if spec is None:
                return None
            r, j = r + spec, j + 2
        out.append(r)
    return out


# G1.24's floor (the owner's ruling, 11-25): setup's one read of the manager's version over the user bus, before its
# first write; a read, asked of nothing else
# busctl's own words with no user bus, RECORDED from the real program, 2026-11-25: on an Ubuntu 24.04 host, systemd
# 255.4, and on a stock Ubuntu 24.04 guest, systemd 255.4-1ubuntu8.18, the same words state by state (research logs
# busctl-nobus-words and busctl-words-guest): with neither variable set (on the guest also after su - from root and
# under sudo -u, which leave both unset); with XDG_RUNTIME_DIR or DBUS_SESSION_BUS_ADDRESS set and no bus where it
# leads. The family's record of five stock systems (systemd 237 to 255) holds the same for 249 and 255. (systemctl's
# words in the first shell are other words: "Failed to connect to bus: No medium found".)
BUSCTL_NO_ADDRESS = ("Failed to set bus address: $DBUS_SESSION_BUS_ADDRESS and $XDG_RUNTIME_DIR not defined (consider "
                     "using --machine=<user>@.host --user to connect to bus of other user)")
BUSCTL_NO_BUS = "Failed to connect to bus: No such file or directory"
FLOOR_READ = ["busctl", "--user", "--json=short", "get-property", mirror.SD_DEST, "/org/freedesktop/systemd1",
              mirror.SD_DEST + ".Manager", "Version"]


def sd_standin_unit(text, home):
    """(ExecStart's data, Environment's data) as busctl --json=short gives them for a unit with this text: a start
    line systemd refuses (a program path holding a double quote, an apostrophe or a backslash, seen: the unit reads bad-setting) gives no
    start line at all, as seen."""
    starts, env = [], []
    for line in text.split("\n"):
        if line.startswith("ExecStart="):
            w = sd_standin_words(line[len("ExecStart="):], home)
            starts = [] if not w or any(c in "\"'\\" for c in w[0]) else [[w[0], w, False, 0, 0, 0, 0, 0, 0, 0]]
        elif line.startswith("Environment="):
            env += sd_standin_words(line[len("Environment="):], home) or []
    return starts, env


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
        self.mac_session = True      # a Mac: is this account logged in at the screen (its gui/<uid> domain there)
        # launchd's jobs, as seen 2026-11-24 on a Mac (macOS 27.2.1): a job booted out while it runs is still there
        # for a moment, and a bootstrap while it is answers 5. teardown_reads: how many `launchctl print` reads of a
        # booted-out job still find it (0: gone at once). print_code: what print answers instead, when set (a read
        # that is not 113 and not a job). A job never loaded or booted out here keeps the old answers.
        self.teardown_reads, self.loaded, self.tearing, self.gone, self.answered = 0, set(), {}, set(), []
        self.print_code = None
        self.domain_code = None      # what `launchctl print gui/<uid>` itself answers, when set (the domain's own read)
        # systemd's user units, as seen 2026-11-24 on a stock Ubuntu 24.04 (M107): a unit `enable --now` or `start`
        # started reads ActiveState=active until it is stopped; user_bus False is a shell with no user bus (after
        # `su -` from root, or XDG_RUNTIME_DIR unset), where every `systemctl --user` answers 1 "Failed to connect to
        # bus: No medium found" and changes nothing; stop_reads: how many reads a stopped unit still reads deactivating.
        self.units_up, self.user_bus, self.stop_reads, self.stopping = set(), True, 0, {}
        self.show_answer = None      # (code, stdout) every `show <unit> -p ActiveState` answers instead, when set
        # G1.24: the user manager's bus (busctl), answering from the unit files on disk as sd_standin_unit reads them.
        # bus_answer: (code, stdout) every busctl call answers instead, when set; bus_starts: the start lines every
        # ExecStart read gives instead, when set (a manager that parsed something other than what was meant).
        self.bus_answer, self.bus_starts, self.bus_objs = None, None, {}
        self.launchd_args = None     # (arguments, environment) every loaded job's print shows instead, when set; or
                                     # {label: (arguments, environment) or None} per job (M150)
        self.launchd_form = None     # a loaded job's whole print block instead, when set (recorded or MADE UP: each
                                     # test names which)
        self.bus_env_extra = []      # Environment entries a person's own drop-in adds to every unit (rule 31 (v.5) (3))
        self.launchd_env_extra = {}  # variables a loaded job's print shows besides its file's own
        self.no_bus_words = "Failed to connect to bus: No medium found"     # systemd 255's; 262 says it otherwise
        # bus_version: what setup's floor read (the manager's Version over the bus) answers: (code, stdout, stderr),
        # or "timeout"; bus_answer "timeout": every other busctl call runs past its limit. bus_kws: each busctl
        # call's keywords, so a test reads its time limit. sd_version: systemctl --version's number.
        self.bus_version = (0, json.dumps({"type": "s", "data": "255.4-1ubuntu8"}) + "\n", "")
        self.bus_kws, self.sd_version = [], "255"
        # bus_env_set: variables a person's own drop-in sets, in the form SEEN on systemd 237 to 255 (the family's
        # rule 31 (v.5) (3), 4:03 PM ET 11-25): the list holds each ONCE, with the drop-in's value. bus_dropins: the
        # unit's DropInPaths.
        self.bus_env_set, self.bus_dropins = {}, []
        # bus_starts_extra: start lines a person's own drop-in adds after the unit's own, in the form SEEN on the same
        # five (rule 31 (v.5) (3), 4:22 PM ET 11-25: beside ours, ours first); bus_type: the Type every unit reads
        # instead of its file's own, when set (a drop-in that sets it).
        self.bus_starts_extra, self.bus_type = [], None
        # XDG_RUNTIME_DIR: a login of the person's own sets it, so a home has one (setup's floor reads it, G1.24)
        self.env = {"WSM_HOME": os.path.join(self.tmp, "wsm"), "HOME": os.path.join(self.tmp, "home"),
                    "XDG_CONFIG_HOME": os.path.join(self.tmp, "home", ".config"),
                    "XDG_RUNTIME_DIR": os.path.join(self.tmp, "run")}
        os.makedirs(self.env["HOME"])
        os.makedirs(self.env["XDG_RUNTIME_DIR"], mode=0o700)
        self.saved_env = {k: os.environ.get(k) for k in self.env}
        self.saved_run = mirror.run
        self.procs = []

    def launchd(self, cmd):
        """launchctl's bootstrap, bootout and print of one job, or None for every other command."""
        dom = f"gui/{os.getuid()}"
        done = lambda rc, out="", err="": subprocess.CompletedProcess(cmd, rc, out, err)
        if cmd == ["launchctl", "print", dom] and self.domain_code is not None:
            return done(self.domain_code, "", "")
        if not self.mac_session and cmd[:2] in (["launchctl", "bootstrap"], ["launchctl", "bootout"],
                                                ["launchctl", "print"]) and cmd[2].startswith(dom):
            # Seen 2026-11-24 over ssh on macOS 13.7.8, a test account with no screen login: print of the domain
            # and of a job, and bootout of a job, each answered 125.
            return done(125, "", "Could not print domain: 125: Domain does not support specified action\n")
        if cmd[:3] == ["launchctl", "bootstrap", dom] and len(cmd) == 4 and self.service_ok:
            label = os.path.basename(cmd[3])[:-len(".plist")]
            if label in self.loaded or label in self.tearing:
                return done(5, "", "Bootstrap failed: 5: Input/output error\n")
            self.loaded.add(label)
            self.gone.discard(label)
            if self.forge_follows and cmd[3] == mirror.paths()["plist"]:
                self.start_forge()
            return done(0)
        if cmd[:2] == ["launchctl", "bootout"] and len(cmd) == 3 and cmd[2].startswith(dom + "/"):
            label = cmd[2][len(dom) + 1:]
            if self.forge_follows and label == mirror.LAUNCHD_LABEL:
                for pr in self.procs:
                    if pr.poll() is None:
                        pr.terminate()
                        pr.wait(30)
            if label not in self.loaded:                 # nothing loaded under it: launchd says so, and has none
                if label not in self.tearing:
                    self.gone.add(label)
                return done(3, "", "Boot-out failed: 3: No such process\n")
            self.loaded.discard(label)
            if self.teardown_reads:
                self.tearing[label] = self.teardown_reads
            else:
                self.gone.add(label)
            return done(0)
        if cmd[:2] == ["launchctl", "print"] and len(cmd) == 3 and cmd[2].startswith(dom + "/"):
            label = cmd[2][len(dom) + 1:]
            if self.print_code is not None:
                return done(self.print_code, "", "")
            if label in self.tearing:
                self.tearing[label] -= 1
                if self.tearing[label] <= 0:
                    del self.tearing[label]
                    self.gone.add(label)
                return done(0, "state = running\n")
            if label in self.gone:
                return done(113, "", f"Bad request.\nCould not find service \"{label}\" in domain for user gui: "
                                     f"{os.getuid()}\n")
        return None

    def launchd_job(self, label):
        """THE SUITE'S STAND-IN for the blocks `launchctl print` gives for a loaded job, built from its file with
        plistlib in the form seen 2026-11-25 on a Mac, macOS 27.2.1, a test account (row M148), and recorded the
        same day on GitHub's macOS 15.7.9, 15.8.2, 26.6.1 and 26.7.2 (tests/launchd-print): one argument to a line
        after two tabs; the inherited and default environment blocks; then the job's own environment block,
        launchd's OSLogRateLimit first (not on 15) and its XPC_SERVICE_NAME last around the file's variables."""
        given = self.launchd_args
        if isinstance(given, dict):         # per label: (arguments, environment), or None for a print with no blocks
            if label in given and given[label] is None:
                return ""                   # (launchd_parsed: could not be read; such a print is MADE UP, not seen)
            given = given.get(label)
        if given is not None:
            args, env = given
        else:
            base = os.path.dirname(mirror.paths()["plist"])
            f = os.path.join(base, label + ".plist")
            if not os.path.exists(f):
                return ""
            try:
                with open(f, "rb") as fh:
                    pl = plistlib.load(fh)
            except Exception:
                # A file that is not a plist (M150: any way the parser stops, expat's not-well-formed among them, which
                # is not a ValueError): the stand-in answers it as it answers no file, with no blocks, and never
                # raises where the tool would answer. What launchctl print gives for a label whose file is not a
                # plist is NOT SEEN BY US.
                return ""
            if not isinstance(pl, dict):                # a file with no job in it (a bare <plist/>): no block
                return ""
            args, env = pl.get("ProgramArguments") or [], pl.get("EnvironmentVariables") or {}
            if not isinstance(args, list) or not all(isinstance(a, str) for a in args) or not isinstance(env, dict):
                return ""                               # M150: arguments that are not a list of words: no block
        own = {"OSLogRateLimit": "64", **env, **self.launchd_env_extra}
        own["XPC_SERVICE_NAME"] = label
        return (f"gui/{os.getuid()}/{label} = {{\n\tactive count = 1\n\ttype = LaunchAgent\n\tstate = running\n\n"
                f"\tprogram = {args[0] if args else ''}\n\targuments = {{\n" + "".join(f"\t\t{a}\n" for a in args)
                + "\t}\n\n\tinherited environment = {\n\t\tSSH_AUTH_SOCK => /var/run/com.apple.launchd.x/Listeners\n\t}\n\n"
                "\tdefault environment = {\n\t\tPATH => /usr/bin:/bin:/usr/sbin:/sbin\n\t}\n\n\tenvironment = {\n"
                + "".join(f"\t\t{k} => {v}\n" for k, v in own.items())
                + "\t}\n\n\tresource coalition = {\n\t\tID = 1\n\t\ttype = resource\n\t}\n}\n")

    def bus(self, cmd):
        """busctl --user --json=short, as systemd 255 answered it on a stock guest (2026-11-25)."""
        done = lambda rc, out="", err="": subprocess.CompletedProcess(cmd, rc, out, err)
        if not os.environ.get("XDG_RUNTIME_DIR") and not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return done(1, "", BUSCTL_NO_ADDRESS + "\n")
        if not self.user_bus:
            return done(1, "", BUSCTL_NO_BUS + "\n")
        if cmd[3:] == ["get-property", mirror.SD_DEST, "/org/freedesktop/systemd1", mirror.SD_DEST + ".Manager",
                       "Version"]:
            if self.bus_version == "timeout":
                raise subprocess.TimeoutExpired(cmd, mirror.BUS_WAIT)
            return done(*self.bus_version)
        if self.bus_answer == "timeout":
            raise subprocess.TimeoutExpired(cmd, mirror.BUS_WAIT)
        if self.bus_answer is not None:
            return done(*self.bus_answer)
        if cmd[3] == "call" and cmd[-3:-1] == ["LoadUnit", "s"]:
            if not re.search(r"\.(service|timer)$", cmd[-1]):      # the bus takes a whole unit name only
                return done(1, "", f"Call failed: Unit name {cmd[-1]} is not valid.\n")
            obj = "/org/freedesktop/systemd1/unit/" + "".join(c if c.isalnum() else "_%02x" % ord(c) for c in cmd[-1])
            self.bus_objs[obj] = cmd[-1]
            return done(0, json.dumps({"type": "o", "data": [obj]}) + "\n")
        if cmd[3] == "get-property" and cmd[5] in self.bus_objs:
            f = os.path.join(os.path.dirname(mirror.paths()["unit"]), self.bus_objs[cmd[5]])
            try:
                text = fread(f) if os.path.exists(f) else ""
            except OSError:
                # M150: a unit file the manager cannot open is answered as one with no start line, never a raise; what
                # systemd answers for a unit file of mode 000 that it has not loaded is NOT SEEN BY US
                text = ""
            starts, env = sd_standin_unit(text, self.env["HOME"])
            if cmd[7] == "ExecStart":
                return done(0, json.dumps({"type": "a(sasbttttuii)",
                                           "data": (starts if self.bus_starts is None else self.bus_starts)
                                           + self.bus_starts_extra}) + "\n")
            if cmd[6:8] == [mirror.SD_DEST + ".Service", "Type"]:
                if self.bus_type == "unreadable":
                    return done(1, "", "Failed to get property Type: made up for this test\n")
                typ = self.bus_type or next((ln[5:] for ln in text.split("\n") if ln.startswith("Type=")), "simple")
                return done(0, json.dumps({"type": "s", "data": typ}) + "\n")
            if cmd[7] == "Environment":
                env = [e for e in env if e.split("=", 1)[0] not in self.bus_env_set]
                env += [f"{k}={v}" for k, v in self.bus_env_set.items()]
                return done(0, json.dumps({"type": "as", "data": env + self.bus_env_extra}) + "\n")
            if cmd[6:8] == [mirror.SD_DEST + ".Unit", "DropInPaths"]:
                return done(0, json.dumps({"type": "as", "data": self.bus_dropins}) + "\n")
        return done(1, "", "Unknown object or property\n")

    def fake_run(self, cmd, **kw):
        if cmd[0] == "busctl":
            self.calls.append(cmd)
            self.bus_kws.append(kw)
            return self.bus(cmd)
        if cmd == ["systemctl", "--version"]:
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, f"systemd {self.sd_version} ({self.sd_version}.4-1ubuntu8)\n"
                                                       "+PAM +AUDIT +SELINUX\n", "")
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
            if cmd[:2] == ["systemctl", "--user"]:
                if not self.user_bus:
                    return subprocess.CompletedProcess(cmd, 1, "", self.no_bus_words + "\n")
                verb = [c for c in cmd[2:] if not c.startswith("-")]
                if verb[:1] in (["enable"], ["start"]) and ("--now" in cmd or verb[0] == "start"):
                    self.units_up |= set(verb[1:])
                elif verb[:1] in (["disable"], ["stop"]) and ("--now" in cmd or verb[0] == "stop"):
                    for u in verb[1:]:
                        if u in self.units_up and self.stop_reads:
                            self.stopping[u] = self.stop_reads
                        self.units_up.discard(u)
                elif verb[:1] == ["show"] and cmd[-2:] == ["-p", "ActiveState"] and len(cmd) == 6:
                    u = cmd[3]
                    if self.show_answer is not None:
                        return subprocess.CompletedProcess(cmd, self.show_answer[0], self.show_answer[1], "")
                    if u in self.stopping:
                        self.stopping[u] -= 1
                        if self.stopping[u] <= 0:
                            del self.stopping[u]
                        return subprocess.CompletedProcess(cmd, 0, "ActiveState=deactivating\n", "")
                    return subprocess.CompletedProcess(cmd, 0, f"ActiveState={'active' if u in self.units_up else 'inactive'}\n",
                                                       "")
            mac = self.launchd(cmd)
            if mac is not None:
                self.answered.append((cmd, mac.returncode))
                return mac
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
                # setup's own start (M105): `enable --now` starts a service that is not running, as systemd does
                # (seen 2026-11-24 on a stock virtual machine: the forge came back after setup run again); a running
                # one is left as it is.
                if cmd == ["systemctl", "--user", "enable", "--now", mirror.SERVICE]:
                    if not any(pr.poll() is None for pr in self.procs):
                        self.start_forge()
                    return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[:3] == ["systemctl", "--user", "show"]:
                if self.timer_next is None:
                    return subprocess.CompletedProcess(cmd, 0, "ActiveState=inactive\nNextElapseUSecRealtime=\n", "")
                return subprocess.CompletedProcess(cmd, 0, f"ActiveState=active\nNextElapseUSecRealtime={self.timer_next}\n"
                                                            f"NextElapseUSecMonotonic={self.timer_mono}\n", "")
            if cmd == ["launchctl", "print", f"gui/{os.getuid()}"]:
                return subprocess.CompletedProcess(cmd, 0 if self.mac_session else 113, "", "" if self.mac_session
                                                   else "Could not find domain for user gui: " + str(os.getuid()))
            if cmd[:2] == ["launchctl", "print"]:
                label = cmd[2].rsplit("/", 1)[-1]
                if self.timer_next is None and label == mirror.SYNC_LABEL:
                    return subprocess.CompletedProcess(cmd, 113, "", "Could not find service")
                return subprocess.CompletedProcess(cmd, 0, "state = waiting\n\trun interval = 900 seconds\n"
                                                   + (self.launchd_form if self.launchd_form is not None
                                                      else self.launchd_job(label)), "")
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
        # G9.12: the second key of the test-only settings, written by the suite in the temporary home it made
        os.makedirs(self.env["WSM_HOME"], mode=0o700, exist_ok=True)        # the person's own folder, theirs alone
        with open(os.path.join(self.env["WSM_HOME"], mirror.SUITE_MARKER), "w") as f:
            f.write("made by tests/test_mirror.py for one test\n")
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
        mirror.claim(p["bin"])          # what the skill's own download records (rule 23)
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
        below_short_lived_ports(p)
        script = p["script"] if os.path.exists(p["script"]) else SCRIPT
        pr = subprocess.Popen([sys.executable, "-I", script, "run-forge"], env=dict(os.environ),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(pr)
        STARTED.append(pr)
        port = int(mirror.ini_value(p["app_ini"], "server", "HTTP_PORT"))
        end = time.monotonic() + seconds
        while True:
            try:
                # the suite's own read of the forge on this computer takes no proxy of the environment (G8.6)
                urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    f"http://127.0.0.1:{port}/api/healthz", timeout=2)
                return port, pr
            except (urllib.error.URLError, OSError):
                if pr.poll() is not None:
                    raise AssertionError("the forge stopped: " + pr.stdout.read()[-400:] + forge_log_tail(p))
                if time.monotonic() > end:
                    raise AssertionError(f"the forge did not answer in {seconds} s" + forge_log_tail(p))
                time.sleep(0.5)


def short_lived_ports_start():
    """Where the system's short-lived ports begin: the ones it hands out for a bind to port 0 and for outgoing
    connections (Linux: ip_local_port_range, 32768 by default; macOS: 49152)."""
    try:
        return int(fread("/proc/sys/net/ipv4/ip_local_port_range").split()[0])
    except (OSError, ValueError, IndexError):
        return 49152


def low_free_port():
    """A free port on 127.0.0.1 below the system's short-lived range, for the suite's forges. Seen on a GitHub runner
    (2026-11-24, release 0.1.0's pull_request run, job test 3.9): setup's port came from that range, and in the
    seconds between run-forge's check and Gitea's bind a listener that is none of the suite's (it answered 407,
    which no stand-in here sends) was handed the same number; Gitea could not bind, and the test waited 90 s on a
    stranger. Below that range the system hands the number to no one, so only a program that asks for it by name
    can take it."""
    first = short_lived_ports_start()
    lo, hi = 20000, min(first, 30000)
    if hi - lo < 200:
        raise AssertionError(f"this system's short-lived ports begin at {first}: there is no room below them, from "
                             f"{lo}, for the suite's forge. Run the suite where they begin at 20200 or above.")
    for cand in random.Random().sample(range(lo, hi), 200):
        if mirror.port_is_free(cand):
            return cand
    raise AssertionError(f"no free port from {lo} to {hi} for the suite's forge (200 tried)")



def below_short_lived_ports(p):
    """The guard: a forge the suite starts listens below the short-lived range. With the tool's own pick made there
    (free_port, G8.7), it finds the port low and changes nothing; a port written some other way is moved as
    restore writes it (HTTP_PORT, ROOT_URL, and the state's port)."""
    port = int(mirror.ini_value(p["app_ini"], "server", "HTTP_PORT") or 0)
    if 20000 <= port < short_lived_ports_start():
        return port
    cand = low_free_port()
    ini = fread(p["app_ini"])
    ini = re.sub(r"^HTTP_PORT\s*=.*$", f"HTTP_PORT = {cand}", ini, flags=re.M)
    ini = re.sub(r"^ROOT_URL\s*=.*$", f"ROOT_URL = http://127.0.0.1:{cand}/", ini, flags=re.M)
    mirror.write_file(p["app_ini"], ini, 0o600)
    st = mirror.load_json(p["state"], {})
    if st.get("port"):
        st["port"] = cand
        mirror.save_state(p, st)
    return cand


MAC_COPY_UNSEEN = ("No copy from GitHub has yet been made on a Mac by a person's own account: on GitHub's macOS "
                   "runners the tests make first copies and run restore --check with the real pinned Gitea, against "
                   "a stand-in for GitHub.")
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


def doors_the_hosts_way(real_system, always=False):
    """A Mac runner standing in for Linux has no /proc/net/tcp (seen on GitHub's Mac runners at 649ee99 for G1.9,
    and at 38590cc for G1.24): setup's door floor and door read then run for real the Mac's own way (netstat), as
    G1.9 does. always: the same for a Linux runner standing in for a Mac (M150: the Mac's road on every leg), whose
    netstat is not a Mac's. Returns the pair to put back."""
    saved = mirror.read_door, mirror.door_floor
    if os.path.exists(mirror.PROC_TCP[0]) and not always:
        return saved

    def the_hosts_way(read):
        def run(*a, **kw):
            was = mirror.system
            mirror.system = real_system
            try:
                return read(*a, **kw)
            finally:
                mirror.system = was
        return run
    mirror.read_door, mirror.door_floor = map(the_hosts_way, saved)
    return saved


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
                self.assertIn(" ".join(mirror.sd_word(w) for w in ("-I", p["script"], "run-forge")), txt)   # G1.24's quoted words
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
                     ["pkcheck", "--action-id"], ["launchctl", "print"])                         # read-only checks
            changed = [c for c in h.calls if c[:2] not in reads and c[:3] != ["systemctl", "--user", "show-environment"]
                       and c not in (FLOOR_READ, ["systemctl", "--version"])]       # G1.24's floor is a read
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

    def test_G1_17_on_a_mac_setup_again_loads_a_running_forge_again(self):
        """Seen 2026-11-24 on a Mac (macOS 27.2.1, a clean test account): setup run while the forge's launch agent was
        loaded and running stopped with "did not load the forge's launch agent (code 5)" and left it unloaded; the
        next setup loaded it; so every second setup broke the forge. A job booted out while it runs is still there
        for a moment. Each load waits until launchd no longer has the job (its print answers 113, the family's rule
        24) before loading it; while it is still there, nothing is loaded over it (a load is never tried again)."""
        saved = mirror.system
        mirror.system = lambda: ("darwin", "arm64")
        try:
            with Home() as h:
                p = mirror.paths()
                os.makedirs(os.path.dirname(p["plist"]), exist_ok=True)
                h.teardown_reads = 3
                for n in range(3):                              # setup, setup again, and once more
                    self.assertEqual(mirror.install_service(p), (True, ""), n)
                    self.assertEqual(mirror.install_timer(p, 15), (True, ""), n)
                    self.assertEqual({mirror.LAUNCHD_LABEL, mirror.SYNC_LABEL} - h.loaded, set(), n)
                self.assertEqual([c for c, rc in h.answered if rc == 5], [], "a load met the job still there")
                self.assertTrue(mirror.service(p, "stop"))
                self.assertNotIn(mirror.LAUNCHD_LABEL, h.tearing, "a stop ends once launchd no longer has the job")
                self.assertTrue(mirror.service(p, "start"))
                self.assertIn(mirror.LAUNCHD_LABEL, h.loaded)
                # The wait is written in the forge's own log folder when it is there.
                os.makedirs(os.path.join(p["forge"], "log"), exist_ok=True)
                self.assertTrue(mirror.service(p, "stop"))
                logged = fread(os.path.join(p["forge"], "log", "launchd.log")).splitlines()
                self.assertRegex(logged[-1], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ " + re.escape(mirror.LAUNCHD_LABEL)
                                 + r": gone after \d+\.\d s, 4 read\(s\), last code 113$")
                self.assertTrue(mirror.service(p, "start"))
                saved_wait, mirror.LAUNCHD_WAIT = mirror.LAUNCHD_WAIT, 2
                try:
                    # A job that never goes: nothing is loaded over it, and the words say only what was read.
                    h.teardown_reads = 10 ** 6
                    n = len(h.calls)
                    t = time.monotonic()
                    ok, why = mirror.install_service(p)
                    self.assertLess(time.monotonic() - t, 10)
                    self.assertFalse(ok)
                    self.assertRegex(why, r"^launchd still had the forge's job \d+ s after it was told to take it off, "
                                          r"so it was not loaded again\. Run setup again in a minute, and if it stops "
                                          r"here again, run status$")
                    self.assertFalse([c for c in h.calls[n:] if c[:2] == ["launchctl", "bootstrap"]], "loaded over it")
                    self.assertFalse(mirror.service(p, "stop"), "a stop is not said done while the job is there")
                    # A read that answers neither 113 nor the job: could not be read, never "gone".
                    h.tearing.clear()
                    h.teardown_reads, h.print_code = 0, 1
                    ok, why = mirror.install_service(p)
                    self.assertFalse(ok)
                    self.assertEqual(why, "whether launchd let go of the forge's job could not be read (launchctl print "
                                          "answered code 1), so it was not loaded again. Run setup again, and if it "
                                          "stops here again, run status")
                finally:
                    mirror.LAUNCHD_WAIT = saved_wait
        finally:
            mirror.system = saved

    def test_G1_17_uninstall_removes_nothing_while_launchd_has_a_job(self):
        """G1.17 and the family's rule 30 (the owner's ruling, 3:03 PM ET 11-24): an error is never an absence. Both
        jobs are told off and read first; while either is not read gone, uninstall removes nothing (a job launchd
        still holds would go on starting a program taken from under it), names each job with what was read, and ends
        with the one step as the whole command line; that line, run as printed once launchd lets go, ends 0."""
        saved, saved_wait = mirror.system, getattr(mirror, "LAUNCHD_WAIT", 30)
        mirror.system = lambda: ("darwin", "arm64")
        try:
            with Home() as h:
                p = mirror.paths()
                os.makedirs(os.path.dirname(p["plist"]), exist_ok=True)
                self.assertEqual(mirror.install_service(p), (True, ""))
                self.assertEqual(mirror.install_timer(p, 15), (True, ""))
                sync = os.path.join(os.path.dirname(p["plist"]), mirror.SYNC_LABEL + ".plist")
                kept = [p["script"], p["bin"], p["mirror_key"], os.path.join(p["keys"], "someone"), p["settings"],
                        p["state"]]
                for x in kept:
                    mirror.write_file(x, "{}\n")
                    mirror.claim(x)
                kept += [p["plist"], sync, os.path.join(p["data"], mirror.MADE_RECORD)]
                mirror.LAUNCHD_WAIT = 1
                for case in ("both", "sync"):                   # neither lets go; then the forge's does, the sync's not
                    h.tearing.clear()
                    if case == "both":
                        h.teardown_reads = 10 ** 6
                    else:
                        h.teardown_reads, h.loaded = 0, {mirror.LAUNCHD_LABEL}
                        h.gone.discard(mirror.SYNC_LABEL)
                        h.tearing = {mirror.SYNC_LABEL: 10 ** 6}
                    rc, out = quiet(mirror.main, ["uninstall"])
                    self.assertEqual(rc, 3, out)
                    self.assertEqual([x for x in kept if not os.path.exists(x)], [], f"{case}: something was removed")
                    self.assertNotIn("Gone now", out)
                    self.assertNotIn("is off this computer", out)
                    line = [l for l in out.splitlines() if l.startswith("Not now: ")]
                    self.assertEqual(len(line), 1, out)
                    step = mirror.own_command(p, "uninstall")
                    self.assertTrue(line[0].endswith(f". Nothing was removed. Run uninstall again in a minute: {step}"))
                    self.assertIn("launchd still had the sync's job 1 s after it was told to take it off", line[0])
                    self.assertIn("launchd still had the forge's job 1 s after it was told to take it off" if
                                  case == "both" else f"the forge's job is off, and its file {p['plist']} stays too",
                                  line[0])
                h.tearing.clear()                              # launchd lets go; the step, exactly as printed
                h.gone |= {mirror.LAUNCHD_LABEL, mirror.SYNC_LABEL}
                h.loaded.clear()
                args = shlex.split(step)
                self.assertEqual(args[:3], ["python3", "-I", p["script"]])
                rc, out = quiet(mirror.main, args[3:])
                self.assertEqual(rc, 0, out)
                self.assertNotIn("Not now", out)
                self.assertFalse(os.path.exists(p["plist"]) or os.path.exists(sync) or os.path.exists(p["script"]))
        finally:
            mirror.system, mirror.LAUNCHD_WAIT = saved, saved_wait

    def test_G1_17_an_account_not_at_the_screen_can_leave_and_the_sync_says_its_own_words(self):
        """The owner's read of 3:03 PM ET 11-24: in an account not logged in at the Mac's screen (seen that day over
        ssh on macOS 13.7.8: `launchctl print gui/<uid>` and the job's own print answered 125), setup has already
        written the forge's file when it says to log in at the screen; uninstall then ends 0 with that file gone,
        with no 30 s wait; any other answer of the domain stays "could not be read". And a sync's job that launchd
        does not let go of reaches the person in its own words with its step; the forge's line is not "did not
        start" when the forge did."""
        saved, saved_wait = mirror.system, getattr(mirror, "LAUNCHD_WAIT", 30)
        mirror.system = lambda: ("darwin", "x86_64")
        try:
            with Home() as h:
                p = mirror.paths()
                os.makedirs(os.path.dirname(p["plist"]), exist_ok=True)
                h.mac_session = False
                self.assertEqual(mirror.install_service(p), (False, mirror.MAC_NO_SESSION))
                self.assertTrue(os.path.exists(p["plist"]), "setup wrote the file before the session read")
                t = time.monotonic()
                rc, out = quiet(mirror.main, ["uninstall"])
                self.assertLess(time.monotonic() - t, 5)
                self.assertEqual(rc, 0, out)
                self.assertFalse(os.path.exists(p["plist"]), out)
                self.assertIn(p["plist"], out.split("Gone now:")[1] if "Gone now:" in out else "", out)
                self.assertNotIn("Not now", out)
                # a domain that answers anything else is never "no domain": with the job loaded and not letting go,
                # never "gone", and uninstall removes nothing (the owner's point (a), 3:23 PM ET 11-24)
                self.assertEqual(mirror.LAUNCHD_NO_SCREEN, 125)
                h.mac_session = True
                h.print_code, mirror.LAUNCHD_WAIT = 1, 1
                self.assertEqual(mirror.launchd_off(p, mirror.LAUNCHD_LABEL)[0], "unread")
                h.print_code = None
                self.assertEqual(mirror.install_service(p), (True, ""))
                for x in (p["script"], p["settings"]):
                    mirror.write_file(x, "{}\n")
                    mirror.claim(x)
                for dom_code in (1, 113):
                    h.domain_code = dom_code
                    h.tearing.clear()
                    h.loaded.add(mirror.LAUNCHD_LABEL)
                    h.teardown_reads = 10 ** 6
                    answer = mirror.launchd_off(p, mirror.LAUNCHD_LABEL)[0]
                    self.assertNotEqual(answer, "gone", f"the domain answered {dom_code}")
                    h.tearing.clear()
                    h.loaded |= {mirror.LAUNCHD_LABEL, mirror.SYNC_LABEL}
                    rc, out = quiet(mirror.main, ["uninstall"])
                    self.assertEqual(rc, 3, f"the domain answered {dom_code}: {out}")
                    self.assertTrue(all(os.path.exists(x) for x in (p["plist"], p["script"], p["settings"])), out)
                h.domain_code, h.teardown_reads = None, 0
                h.tearing.clear()
                h.loaded.clear()
                # the sync's job held by launchd: its own words reach the person, with the step
                self.assertEqual(mirror.install_service(p), (True, ""))
                h.loaded.add(mirror.SYNC_LABEL)
                h.teardown_reads = 10 ** 6
                ok, why = mirror.install_timer(p, 15)
                self.assertFalse(ok)
                self.assertRegex(why, r"^launchd still had the sync's job \d+ s after it was told to take it off, so "
                                      r"it was not loaded again\. Run setup again in a minute, and if it stops here "
                                      r"again, run status$")
        finally:
            mirror.system, mirror.LAUNCHD_WAIT = saved, saved_wait
        for fn in (mirror.cmd_setup, mirror.setup_from):    # the forge's own line stands; the sync's is said after it
            src = inspect.getsource(fn)
            self.assertNotIn('"the sync timer could not be switched on"', src)
            self.assertIn("The sync's timer is not on, so nothing is copied until it is: {timer_why}", src)

    def test_G1_18_uninstall_removes_nothing_while_systemd_runs_a_unit_or_cannot_be_read(self):
        """M107 (seen 2026-11-24 on a stock Ubuntu 24.04, the owner's question of 3:23 PM ET): uninstall run from a
        shell with no user bus (after `su -` from root, and with XDG_RUNTIME_DIR and DBUS_SESSION_BUS_ADDRESS unset)
        said "Gone now" and ended 0 while systemd still ran the forge, from its removed program. Every unit is told
        to stop and read stopped first; while one is not, uninstall removes nothing, names each unit with what was
        read, and ends 3 with the whole command line; with no user bus, where to run it. That line, run as printed
        from a shell that reaches systemd, ends 0."""
        saved, saved_wait = mirror.system, mirror.SYSTEMD_WAIT
        mirror.system = lambda: ("linux", "x86_64")
        try:
            with Home() as h:
                p = mirror.paths()
                os.makedirs(os.path.dirname(p["unit"]), exist_ok=True)
                self.assertTrue(mirror.install_service(p)[0])
                self.assertEqual(mirror.install_timer(p, 15), (True, ""))
                base = os.path.dirname(p["unit"])
                kept = [p["script"], p["bin"], p["mirror_key"], os.path.join(p["keys"], "someone"), p["settings"],
                        p["state"]]
                for x in kept:
                    mirror.write_file(x, "{}\n")
                    mirror.claim(x)
                kept += [p["unit"], os.path.join(base, mirror.SYNC_UNIT + ".service"),
                         os.path.join(base, mirror.SYNC_UNIT + ".timer"), os.path.join(p["data"], mirror.MADE_RECORD)]
                step = mirror.own_command(p, "uninstall")
                # (1) no user bus in this shell: nothing is read, so nothing is removed, and where to run it is said
                h.user_bus = False
                rc, out = quiet(mirror.main, ["uninstall"])
                self.assertEqual(rc, 3, out)
                self.assertEqual([x for x in kept if not os.path.exists(x)], [], "something was removed")
                self.assertNotIn("Gone now", out)
                self.assertFalse([c for c in h.calls if c[:2] == ["loginctl", "disable-linger"]], "linger changed")
                line = [l for l in out.splitlines() if l.startswith("Not now: ")]
                self.assertEqual(line, ["Not now: " + "; ".join(
                    f"whether systemd stopped {w} could not be read (systemctl --user answered code 1: Failed to "
                    "connect to bus: No medium found)" for w in ("the forge's service", "the sync's service",
                                                                 "the sync's timer"))
                    + ". Nothing was removed. This shell cannot reach your account's own systemd (it has no user "
                    "bus), so run uninstall again from a login of your own to this computer, such as ssh or a "
                    f"terminal at the screen: {step}"], out)
                # (1b) systemd 262's words for the same (another skill's log, an Arch test account, 2026-11-24)
                for words in ("Failed to connect to user scope bus via local transport: $DBUS_SESSION_BUS_ADDRESS and "
                              "$XDG_RUNTIME_DIR not defined (consider using --machine=<user>@.host --user to connect "
                              "to bus of other user)",
                              "Failed to connect to user scope bus via local transport: No such file or directory"):
                    h.no_bus_words = words
                    rc, out = quiet(mirror.main, ["uninstall"])
                    self.assertEqual(rc, 3, out)
                    self.assertEqual([x for x in kept if not os.path.exists(x)], [], f"{words}: something was removed")
                    self.assertIn("This shell cannot reach your account's own systemd (it has no user bus), so run "
                                  "uninstall again from a login of your own to this computer, such as ssh or a "
                                  f"terminal at the screen: {step}", out)
                    self.assertNotIn("in a minute", out)
                h.no_bus_words = "Failed to connect to bus: No medium found"
                # (2) a unit systemd does not stop in time: named with what was read; the stopped ones too
                h.user_bus, mirror.SYSTEMD_WAIT, h.stop_reads = True, 1, 10 ** 6
                rc, out = quiet(mirror.main, ["uninstall"])
                self.assertEqual(rc, 3, out)
                self.assertEqual([x for x in kept if not os.path.exists(x)], [], "something was removed")
                self.assertIn("systemd still had the forge's service deactivating 1 s after it was told to stop it", out)
                self.assertIn("the sync's service is stopped, and its file stays too", out)
                self.assertIn(f". Nothing was removed. Run uninstall again in a minute: {step}", out)
                self.assertNotIn("Gone now", out)
                # (2b) answers that name no state are never "stopped" (a parser that took a missing state for
                # "inactive" turned a bus error and an empty answer into "gone", in another skill): exit 0 with
                # nothing, exit 0 with another field, and a non-zero answer without the bus's words
                h.stopping.clear()
                h.stop_reads = 0
                for answer in ((0, ""), (0, "LoadState=loaded\n"), (0, "ActiveState=\n"), (4, "")):
                    h.show_answer = answer
                    h.units_up |= {mirror.SERVICE, mirror.SYNC_UNIT + ".timer"}
                    rc, out = quiet(mirror.main, ["uninstall"])
                    self.assertEqual(rc, 3, f"{answer}: {out}")
                    self.assertEqual([x for x in kept if not os.path.exists(x)], [], f"{answer}: something was removed")
                    self.assertIn(f"whether systemd stopped the forge's service could not be read (systemctl --user "
                                  f"answered code {answer[0]}", out)
                    self.assertIn(f". Nothing was removed. Run uninstall again in a minute: {step}", out)
                    self.assertNotIn("no user bus", out)
                h.show_answer = None
                # (3) systemd stops them; the step, exactly as printed
                h.stopping.clear()
                h.stop_reads = 0
                args = shlex.split(step)
                self.assertEqual(args[:3], ["python3", "-I", p["script"]])
                rc, out = quiet(mirror.main, args[3:])
                self.assertEqual(rc, 0, out)
                self.assertNotIn("Not now", out)
                self.assertFalse(os.path.exists(p["unit"]) or os.path.exists(p["script"]), out)
        finally:
            mirror.system, mirror.SYSTEMD_WAIT = saved, saved_wait

    def test_G1_16_on_a_mac_with_no_login_session_setup_says_so_in_the_persons_words(self):
        """M58 (a Mac test account, 2026-11-23): setup over ssh into an account that is not logged in at the Mac's
        screen could not load the forge's launch agent, and said launchctl's own words ("Bootstrap failed: 125:
        Domain does not support specified action"). It says three things in the person's words: this account is not
        logged in at this Mac's screen; the forge starts once it is; log in there, then run setup again. No line for
        a person carries launchctl's words, and nothing is loaded into a session that is not there."""
        saved = mirror.system
        mirror.system = lambda: ("darwin", "arm64")
        try:
            with Home() as h:
                p = mirror.paths()
                os.makedirs(os.path.dirname(p["plist"]), exist_ok=True)
                h.mac_session = False
                ok, why = mirror.install_service(p)
                self.assertFalse(ok)
                self.assertEqual(why, mirror.MAC_NO_SESSION)
                for said in ("not logged in at this Mac's screen", "it starts once this account is",
                             "Log in to this account at this Mac's screen, then run setup again"):
                    self.assertIn(said, why)
                self.assertFalse([c for c in h.calls if c[:2] == ["launchctl", "bootstrap"]], "nothing loaded")
                h.mac_session, h.service_ok = True, False         # a session, and launchctl refuses anyway
                ok, why = mirror.install_service(p)
                self.assertFalse(ok)
                self.assertIn("this Mac did not load the forge's launch agent (code 1); run setup again", why)
                self.assertNotIn("Failed to connect", why)
                h.service_ok = True
                self.assertEqual(mirror.install_service(p), (True, ""))
                self.assertTrue([c for c in h.calls if c[:2] == ["launchctl", "bootstrap"]])
        finally:
            mirror.system = saved
        for fn in (mirror.cmd_setup, mirror.setup_from):      # both say `why` whole, after the same words
            self.assertIn("did not start: {why}", inspect.getsource(fn))

    def test_G1_16_with_no_session_at_the_screen_the_dry_run_and_status_name_a_step_that_works_there(self):
        """M143 (seen 2026-11-25 on a Mac, macOS 27.2.1, an account reached over ssh only): with no session at the
        Mac's screen, setup said the cause and its step, but status said "run setup again" alone, which cannot start
        the forge there, and the dry run said setup would start it. Only the account's own domain answering 125 (its
        read with no session, seen on macOS 13.7.8 and 27.2.1) changes a line: the dry run says the forge cannot start
        now, why, and the step; status's forge line says the cause and a step that works there, in both states (setup
        not finished: log in, then setup again; both jobs' files in place: the login, then status again), and its
        timer and first-copy lines name no step that cannot work. Any other answer leaves every line as it was."""
        import socket
        saved, door, rel = mirror.system, mirror.read_door, mirror.release_check
        mirror.system = lambda: ("darwin", "arm64")
        mirror.read_door = lambda p, port: ("none", "no program listens on it")    # nothing on the forge's port
        mirror.release_check = lambda st, force=False: []
        try:
            with Home() as h:
                p = mirror.paths()
                s = socket.socket()
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
                s.close()
                for d in (p["data"], p["conf"], os.path.dirname(p["plist"])):
                    os.makedirs(d, mode=0o700, exist_ok=True)
                    os.chmod(d, 0o700)
                mirror.save_state(p, {"port": port})
                with open(p["mirror_key"], "w") as f:
                    f.write("5" * 40 + "\n")           # made up, in the form the forge gives (M156)
                shutil.copyfile(SCRIPT, p["script"])
                with open(p["plist"], "w") as f:                  # setup stopped after writing the forge's file
                    f.write("<plist/>\n")
                sync = os.path.join(os.path.dirname(p["plist"]), mirror.SYNC_LABEL + ".plist")
                setup_line = mirror.own_command(p, "setup")
                today_plan = "install the forge as your own service (a launch agent) and start it"
                today_forge = (f"problem: the forge does not answer on 127.0.0.1:{port}; run setup again, which starts "
                               f"it and keeps every copy: {setup_line}")
                today_go = f"first copy: waiting for your go: in a terminal window of your own, run {mirror.own_command(p, 'go')}"

                def look():
                    mirror.DOOR_SEEN.clear()
                    rc, out = status()
                    return rc, out.splitlines(), mirror.setup_plan(p, {})
                for code in (0, 113, 1, 5, None):              # every answer but 125: each line as it was
                    h.domain_code, h.mac_session = (code, True) if code is not None else (None, True)
                    rc, lines, plan = look()
                    self.assertEqual(rc, 1, lines)
                    self.assertIn(today_forge, lines, code)
                    self.assertTrue([x for x in lines if x.startswith(today_go)], (code, lines))
                    self.assertIn(today_plan, plan, code)
                    self.assertFalse([x for x in lines if "logged in at this Mac's screen" in x], (code, lines))
                h.domain_code, h.mac_session = None, False      # the domain answers 125, as seen
                rc, lines, plan = look()
                self.assertEqual(rc, 1, lines)
                self.assertNotIn(today_plan, plan)
                said = [x for x in plan if x.startswith("install the forge as your own service (a launch agent)")]
                self.assertEqual(len(said), 1, plan)
                for words in ("cannot start now", "this account is not logged in at this Mac's screen",
                              "log in to this account at this Mac's screen, then run setup"):
                    self.assertIn(words, said[0])
                self.assertIn(f"problem: the forge does not answer on 127.0.0.1:{port}: {mirror.MAC_NO_SESSION}: "
                              f"{setup_line}", lines, "setup not finished: log in, then setup again")
                self.assertNotIn(today_forge, lines)
                self.assertIn("problem: the sync timer is not loaded: this account is not logged in at this Mac's "
                              "screen", lines)
                self.assertIn("first copy: waiting for your go, which can be given once the forge answers (see the "
                              "problem above)", lines)
                with open(sync, "w") as f:                       # both files in place: a restart since setup
                    f.write("<plist/>\n")
                rc, lines, plan = look()
                self.assertEqual(rc, 1, lines)
                forge = [x for x in lines if x.startswith(f"problem: the forge does not answer on 127.0.0.1:{port}")]
                self.assertEqual(forge, [f"problem: the forge does not answer on 127.0.0.1:{port}: this account is not "
                                         "logged in at this Mac's screen, and on a Mac the forge runs only in a login "
                                         "session at the screen. Log in to this account at this Mac's screen, then run "
                                         "status again; if the forge still does not answer there, run setup again: "
                                         f"{setup_line}"], lines)
                self.assertFalse([x for x in lines if "run setup again, which starts it" in x], lines)
        finally:
            mirror.system, mirror.read_door, mirror.release_check = saved, door, rel

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
                    self.assertIn(mirror.install_words("git"), out)               # this computer's own way (G1.14)
                    if mirror.os_id() == "ubuntu":
                        self.assertIn("sudo apt update && sudo apt install git", out)
                    elif mirror.os_id() == "mac":
                        self.assertIn("xcode-select --install is Apple's command", out)
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

        # Every install command the tool names was run exactly as printed on a fresh system: it is in the plan's table.
        # (Seen: the plain apt lines fail on fresh Ubuntu and Debian images; brew failed on an older Mac.)
        plan, readme = fread(os.path.join(ROOT, "DNA-STRAND-MASTER-PLAN.md")), fread(os.path.join(ROOT, "README.md"))
        table = plan.split("### Install lines, as seen", 1)[1].split("Lines tried and NOT used", 1)[0]
        cmd = r"(?:sudo apt update && )?sudo (?:apt|dnf) install [^;`)\n\"]+|xcode-select --install|brew install [^;`)\n\"]+"
        words = []
        for here in ("ubuntu", "debian", "fedora", "mac", "arch", ""):
            saved, mirror.os_id = mirror.os_id, (lambda here=here: here)
            try:
                words += [mirror.install_words("git"), mirror.install_words("git-lfs")]
            finally:
                mirror.os_id = saved
        for text, where in (("; ".join(words), "the install lines"),
                            (fread(SCRIPT), "the script"), (readme, "README.md")):
            named = {c.strip().rstrip(".,") for c in re.findall(cmd, text)}
            if where == "the install lines":
                self.assertEqual(len(named), 5, named)
            for line in sorted(named):
                self.assertIn(f"`{line}`", table, f"{where} names a line never seen on a fresh system: {line}")
            self.assertNotIn("brew install", text, where)
            self.assertIsNone(re.search(r"(?<!&& )sudo apt install", text), f"an apt line without apt update in {where}")
        self.assertIn("git lfs's own page is https://git-lfs.com", words)

    def test_G1_14_the_install_words_name_this_computers_way_only(self):
        """A Mac test account (2026-11-23) was told the install lines for Ubuntu, Debian and Fedora as well as a Mac's.
        Every clause in a line the agent passes on is one this person must hear (the family's rule 7): the tool
        reads which system this is (/etc/os-release's ID; a Mac by its kernel) and names only that system's line,
        and only a line run exactly as printed there. A system with no such line, a kin of these included, gets
        the program's own page."""
        d = tempfile.mkdtemp(prefix="wsm-os-")
        TEMPS.append(d)
        rel, saved_rel, saved_sys = os.path.join(d, "os-release"), mirror.OS_RELEASE, mirror.system
        others = ("Ubuntu", "Debian", "Fedora", "Mac", "apt", "dnf", "brew", "xcode")
        cases = {"ubuntu": ("on this computer: sudo apt update && sudo apt install git",
                            "on this computer: sudo apt update && sudo apt install git-lfs"),
                 "debian": ("on this computer: sudo apt update && sudo apt install git",
                            "on this computer: sudo apt update && sudo apt install git-lfs"),
                 "fedora": ("on this computer: sudo dnf install git", "on this computer: sudo dnf install git-lfs"),
                 "linuxmint": ("git's own page is https://git-scm.com/downloads", "git lfs's own page is https://git-lfs.com"),
                 "arch": ("git's own page is https://git-scm.com/downloads", "git lfs's own page is https://git-lfs.com")}
        try:
            mirror.OS_RELEASE, mirror.system = rel, (lambda: ("linux", "x86_64"))
            for here, (git, lfs) in cases.items():
                like = "ubuntu debian" if here == "linuxmint" else ""
                with open(rel, "w", encoding="utf-8") as f:
                    f.write(f'NAME="x"\nID={here}\nID_LIKE="{like}"\nPRETTY_NAME="x"\n')
                self.assertEqual(mirror.os_id(), here)
                self.assertEqual((mirror.install_words("git"), mirror.install_words("git-lfs")), (git, lfs), here)
                said = mirror.lfs_missing() + " " + mirror.git_missing_sentence()
                tool = "apt" if here in ("ubuntu", "debian") else "dnf" if here == "fedora" else None
                for word in others:
                    if word != tool:
                        self.assertNotIn(word, said, f"{here}: names another system's way: {word}")
            os.remove(rel)
            self.assertEqual(mirror.os_id(), "")
            self.assertEqual(mirror.install_words("git-lfs"), "git lfs's own page is https://git-lfs.com")
            mirror.system = lambda: ("darwin", "arm64")
            said = mirror.lfs_missing()
            self.assertIn("git lfs's own page is https://git-lfs.com", said)
            for word in ("Ubuntu", "Debian", "Fedora", "apt", "dnf", "brew", "sudo"):
                self.assertNotIn(word, said)
        finally:
            mirror.OS_RELEASE, mirror.system = saved_rel, saved_sys

    def test_G1_15_a_copy_on_a_mac_is_said_unseen_until_it_is_seen(self):
        """No copy from GitHub has been made on a Mac by a person's account (the owner, reading the Mac's first
        sighting, 2026-11-23: it had 0 copies, and the one before it was stand-in agents). While row M50 has no
        proven line, a README that names the Mac says so in one plain sentence, with what was seen instead."""
        rows = {r["id"]: r for r in json.loads(fread(os.path.join(ROOT, "PROMISES.json")))["rows"]}
        self.assertEqual(rows["M50"]["test"], "test_G1_15_a_copy_on_a_mac_is_said_unseen_until_it_is_seen")
        readme = fread(os.path.join(ROOT, "README.md"))
        if rows["M50"]["proven"] or not re.search(r"\b(?:Macs?|macOS)\b", readme):
            return
        self.assertIn(MAC_COPY_UNSEEN, " ".join(readme.split()))

    def test_G1_10_a_stranger_on_the_forges_port_is_never_said_to_be_the_forge(self):
        """M85 (read 11-24, due 0.1.0 with M102): setup and restore said the forge runs from an answer on its port
        alone. Now another program there is said in its own words, apart from "not answering yet", setup and restore
        answer 1, and the stranger is sent nothing. (Setup moving the kept port, and status: test_G8_7.)"""
        if not real_gitea() and not os.environ.get("WSM_REQUIRE_GITEA"):
            raise unittest.SkipTest("no real pinned Gitea for this system")
        real_free, wait = mirror.free_port, mirror.SETUP_WAIT
        with contextlib.ExitStack() as stack:
            ports = [low_free_port() for _ in range(2)]
            strangers = [Stranger(x) for x in ports]
            for s in strangers:
                stack.callback(s.close)
            picks = iter(ports)
            stack.callback(setattr, mirror, "free_port", real_free)
            stack.callback(setattr, mirror, "SETUP_WAIT", wait)
            # setup: the first port it picks is taken by the time it reads it, and so is the one it moves to
            with Home(linger="yes") as h:
                h.place_gitea()
                mirror.free_port = lambda: next(picks)
                mirror.SETUP_WAIT = 5
                rc, out = h.setup()
                mirror.SETUP_WAIT = wait
                self.assertEqual(rc, 1, out)
                self.assertIn(f"The forge's address changed: another program holds 127.0.0.1:{ports[0]}, so the forge "
                              f"moves to http://127.0.0.1:{ports[1]}/.", out)
                self.assertIn(f"problem: another program answers on the forge's port 127.0.0.1:{ports[1]}, not this "
                              "mirror's own forge", out)
                self.assertNotIn("The forge runs as your own service", out)
                self.assertNotIn("not answering yet", out)
                mirror.free_port = real_free
            # restore: setup --from, with another program on the port it picks
            backup = backup_of_one_copy(self)
            with Home(linger="yes") as h2:
                h2.place_gitea()
                mirror.free_port = lambda: ports[1]
                mirror.SETUP_WAIT = 5
                rc, out = h2.setup("--from", backup)
                mirror.SETUP_WAIT = wait
                self.assertEqual(rc, 1, out)
                self.assertIn("copies are in the restored data; they are not counted in the forge, since nothing was "
                              "sent to its port:", out)
                self.assertIn(f"problem: another program answers on the forge's port 127.0.0.1:{ports[1]}", out)
                self.assertNotIn("copies are back", out)
                self.assertNotIn("The forge runs as your own service", out)
            for s in strangers:
                self.assertEqual(s.close(), [], "a stranger was sent requests")

    def test_G1_10_setup_says_the_forge_runs_only_once_it_answers(self):
        with Home() as h:
            h.place_gitea()
            saved = mirror.wait_forge
            try:
                mirror.wait_forge = lambda p, port, seconds=60, again="status": False
                rc, out = h.setup()
                self.assertIn("not answering yet", out)
                self.assertNotIn("The forge runs as your own service", out)
                mirror.wait_forge = lambda p, port, seconds=60, again="status": True
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
                mirror.wait_forge = lambda p, port, seconds=60, again="status": False
                rc, out = h.setup()
            finally:
                mirror.wait_forge = saved
            p = mirror.paths()
            self.assertIn(f"not answering yet on http://127.0.0.1:{mirror.load_json(p['state'], {})['port']}/ after "
                          f"{mirror.SETUP_WAIT} s", out)
            self.assertIn(f"it answered within {mirror.FIRST_START_SEEN} s", out)
            self.assertIn("run status in 5 minutes", out)
            self.assertIn(os.path.join(p["forge"], "log", "gitea.log"), out)

    def test_G1_9_lingering_is_said_first_asked_of_this_computer_and_undone_only_if_setup_did_it(self):
        saved, saved_proc = mirror.system, mirror.own_process_for_polkit
        saved_doors = mirror.read_door, mirror.door_floor
        mirror.system = lambda: ("linux", "x86_64")
        if not os.path.exists("/proc/self/stat"):        # a Mac runner standing in for Linux has no /proc to read
            mirror.own_process_for_polkit = lambda: f"{os.getpid()},1,{os.getuid()}"
        if not os.path.exists(mirror.PROC_TCP[0]):
            # Seen on GitHub's Mac runners at 649ee99: with no /proc/net/tcp, setup's floor said the door cannot be
            # read and stopped. The door is then read for real the Mac's own way (netstat); lingering stays Linux's.
            def the_mac_way(read):
                def run(*a, **kw):
                    was = mirror.system
                    mirror.system = saved
                    try:
                        return read(*a, **kw)
                    finally:
                        mirror.system = was
                return run
            mirror.read_door, mirror.door_floor = map(the_mac_way, saved_doors)
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
            mirror.read_door, mirror.door_floor = saved_doors

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
        saved, mirror.os_id = mirror.os_id, (lambda: "mac")
        try:
            self.assertEqual("git comes with Apple's developer tools, and xcode-select --install is Apple's "
                             "command for them", mirror.install_words("git"))
        finally:
            mirror.os_id = saved
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
    # G1.24 (M148): every value in a service file is one whole value, and what the manager parsed is read back.
    PLANTED = ["a b", "a  b", " a", "a%b", "a%hb", "a$b", "a${X}b", 'a"b', "a\\b", "ab\\", "a'b", "a;b", "a&b<c>d",
               "é", 'a b%c$d"e\\f']

    @staticmethod
    def held_by_systemd(argv):
        """What systemd's bus holds for a start line meant as argv: the program, then argv, an argument's $ as $$
        (it is expanded at start; seen 2026-11-25 on systemd 255, where the started program got each word whole)."""
        return [argv[0], argv[0]] + [w.replace("$", "$$") for w in argv[1:]]

    def test_G1_24_each_value_in_a_service_file_is_one_whole_value(self):
        """M148 (seen 2026-11-25 on two stock Ubuntu 24.04 guests, systemd 255, and the issue's own case): with the
        folder the skill keeps its things in holding a space, %, $, ", \\ or another planted character, alone and all
        at once, every service file setup writes is read as the words meant: a systemd unit, read by the suite's
        stand-in for systemd's grammar as seen, gives the program and then each argument, and each Environment entry
        whole; a launchd file, read as a property list, gives each argument, WSM_HOME and its log paths whole. The
        0.1.0 head's form of the same start line is not read as meant for a space."""
        for v in self.PLANTED:
            with Home() as h, self.subTest(value=v):
                home = os.path.join(h.tmp, v, "wsm")
                os.environ["WSM_HOME"] = home
                p = mirror.paths()
                argv = [sys.executable, "-I", p["script"], "run-forge"]
                sync = [sys.executable, "-I", p["script"], "sync", "--from-timer"]
                for text, want in ((mirror.unit_text(p), argv), (mirror.sync_unit_text(p), sync)):
                    starts, env = sd_standin_unit(text, h.env["HOME"])
                    self.assertEqual(len(starts), 1, text)
                    self.assertEqual([starts[0][0]] + starts[0][1], self.held_by_systemd(want), text)
                    self.assertEqual(env, [f"WSM_HOME={home}"], text)
                for text, want, log in ((mirror.plist_text(p), argv, "launchd.log"),
                                        (mirror.sync_plist_text(p, 15), sync, "sync.log")):
                    pl = plistlib.loads(text.encode("utf-8"))
                    self.assertEqual(pl["ProgramArguments"], want, text)
                    self.assertEqual(pl["EnvironmentVariables"], {"WSM_HOME": home}, text)
                    self.assertEqual(pl["StandardOutPath"], os.path.join(p["forge"], "log", log), text)
                    self.assertEqual(pl["StandardErrorPath"], os.path.join(p["forge"], "log", log), text)
        meant = [sys.executable, "-I", "/srv/a b/data/mirror.py", "run-forge"]
        starts, _ = sd_standin_unit(f"ExecStart={sys.executable} -I /srv/a b/data/mirror.py run-forge\n", "/home/you")
        self.assertNotEqual([starts[0][0]] + starts[0][1], self.held_by_systemd(meant))

    def test_G1_24_setup_stops_before_its_first_write_on_a_value_no_manager_takes_whole(self):
        """M148: a character that cannot be seen (a control character, a format character, a line or paragraph
        separator) or bytes that are not text in WSM_HOME, and on Linux a dollar sign, a double quote, an apostrophe
        or a backslash in the path of the Python setup runs with (seen 2026-11-25 on systemd 255: no one spelling
        carried every such path whole): setup and its dry run end 2 before anything is written or any service
        manager is asked, never print the value, and name the character and the one next step. On a Mac the same
        Python path is written whole into its file, so setup goes on there."""
        saved_sys, saved_exe = mirror.system, sys.executable
        saved_doors = doors_the_hosts_way(saved_sys)
        try:
            mirror.system = lambda: ("linux", "x86_64")
            for bad in ("a\nb", "a\x7fb", "a\u2028b", "a\u200bb", "a\udcffb"):   # each planted character as an escape
                with Home() as h, self.subTest(home=repr(bad)):
                    os.environ["WSM_HOME"] = os.path.join(h.tmp, bad)
                    before = tree(h.tmp)
                    for args in (("--dry-run",), ("--linger",)):
                        rc, out = h.setup(*args)
                        self.assertEqual(rc, 2, out)
                        self.assertIn("Stopped: the folder this skill keeps its things in holds a character that "
                                      "cannot be seen, or bytes that are not text, so it cannot be written into the "
                                      "service file as one whole value. Nothing was changed. The one next step: set "
                                      "WSM_HOME to a folder of your own whose path has none, and run setup again.", out)
                        self.assertNotIn(bad, out)
                        self.assertIn(mirror.SHOWN_CHANGED, out)
                    self.assertEqual(tree(h.tmp), before)
                    self.assertFalse([c for c in h.calls if c[0] in ("systemctl", "busctl", "launchctl")
                                      and c != FLOOR_READ], h.calls)          # the floor's one read only
            for exe, named in (("/opt/a$b/python3", "a dollar sign"), ('/opt/a"b/python3', "a double quote"),
                               ("/opt/a'b/python3", "an apostrophe"), ("/opt/a\\b/python3", "a backslash"),
                               ("/opt/a$\\b/python3", "a dollar sign and a backslash")):
                with Home() as h, self.subTest(exe=exe):
                    before = tree(h.tmp)
                    sys.executable = exe
                    try:
                        rc, out = h.setup("--linger")
                    finally:
                        sys.executable = saved_exe
                    self.assertEqual(rc, 2, out)
                    it = "them" if " and " in named else "it"
                    self.assertIn(f"Stopped: the path of the Python this runs with holds {named}, and systemd does "
                                  f"not take a program's path holding {it} as one whole value. Nothing was changed. The one next step: run setup with a Python whose "
                                  "path has none (for example /usr/bin/python3).", out)
                    self.assertEqual(tree(h.tmp), before)
                    self.assertFalse([c for c in h.calls if c[0] in ("systemctl", "busctl", "launchctl")
                                      and c != FLOOR_READ], h.calls)          # the floor's one read only
            mirror.system = lambda: ("darwin", "arm64")
            with Home():
                sys.executable = "/opt/a$b\"c'd\\e&f<g/python3"
                self.assertIsNone(mirror.service_value_stop(mirror.paths()))
        finally:
            mirror.system, sys.executable = saved_sys, saved_exe
            mirror.read_door, mirror.door_floor = saved_doors

    def test_G1_24_set_up_is_said_only_when_the_manager_holds_each_word_as_written(self):
        """M148: on Linux setup reads back what systemd parsed for each service, over its bus one word at a time
        (never the line `systemctl show` prints, which joins the words), after daemon-reload and before anything is
        switched on, and switches it on only when every word and every Environment entry is what it wrote. A parse
        that differs and one that could not be read are each said as that, with nothing switched on; the go block's
        restart line and status say the same, each with a step that works where it is said. On a Mac, launchd's
        loaded job is read back the same way and taken off again at once when it is not what setup wrote."""
        saved = mirror.system
        other = [sys.executable, "-I", "/elsewhere/mirror.py", "run-forge"]
        try:
            mirror.system = lambda: ("linux", "x86_64")
            with Home() as h:
                p = mirror.paths()
                os.makedirs(os.path.dirname(p["unit"]), exist_ok=True)
                self.assertEqual(mirror.install_service(p), (True, ""))
                reads = [i for i, c in enumerate(h.calls) if c[0] == "busctl"]
                on = h.calls.index(["systemctl", "--user", "enable", "--now", mirror.SERVICE])
                self.assertTrue(reads and max(reads) < on, h.calls)
                self.assertFalse([c for c in h.calls if c[:3] == ["systemctl", "--user", "show"]])
                self.assertEqual(mirror.install_timer(p, 15), (True, ""))
                self.assertEqual([mirror.service_read_back(p, w) for w in ("forge", "sync")], ["equal", "equal"])
                self.assertFalse(mirror.restart_line(p, {}).startswith("After a restart: systemd"))
                for name, value, answer in (("bus_starts", [[other[0], other, False, 0, 0, 0, 0, 0, 0, 0]], "differs"),
                                            ("bus_starts", [], "differs"),
                                            ("bus_answer", (1, "", "Failed to connect to bus: No medium found\n"),
                                             "unread")):
                    setattr(h, name, value)
                    n = len(h.calls)
                    for which, got in (("forge", mirror.install_service(p)), ("sync", mirror.install_timer(p, 15))):
                        self.assertFalse(got[0])
                        self.assertIn("its start line is not read as setup wrote it" if answer == "differs" else
                                      "as setup wrote it could not be read", got[1])
                        self.assertTrue(got[1].endswith(mirror.read_back_step(which, answer)), got[1])
                        # the owner's ruling, 11-25: never uninstall first; the person's own setting is looked at first
                        self.assertNotIn("uninstall", got[1].split(";")[0])
                    self.assertFalse([c for c in h.calls[n:] if c[:3] == ["systemctl", "--user", "enable"]])
                    self.assertEqual(mirror.service_read_back(p, "forge"), answer)
                    line = mirror.restart_line(p, {})
                    self.assertTrue(line.startswith("After a restart: systemd does not hold the forge's service"
                                                    if answer == "differs" else
                                                    "After a restart: whether systemd holds the forge's service"), line)
                    self.assertTrue(line.endswith(mirror.read_back_step("forge", answer, "status")), line)
                    self.assertNotIn("starts by itself", line)
                    setattr(h, name, None)
            src = inspect.getsource(mirror.cmd_status)
            self.assertIn('read_back_step(which, held, "status")', src)
            mirror.system = lambda: ("darwin", "arm64")
            with Home() as h:
                p = mirror.paths()
                os.makedirs(os.path.dirname(p["plist"]), exist_ok=True)
                self.assertEqual(mirror.install_service(p), (True, ""))
                self.assertEqual(mirror.service_read_back(p, "forge"), "equal")
                h.launchd_args = (other, {"WSM_HOME": os.environ["WSM_HOME"]})
                n = len(h.calls)
                ok, why = mirror.install_service(p)
                self.assertFalse(ok)
                self.assertIn("launchd does not hold the forge's service as setup wrote it: its arguments are not "
                              "read as setup wrote it, so it was taken off again, and launchd reads it gone. Run setup "
                              "again, which writes it again and reads it back; if this line comes back after that, "
                              "run uninstall", why)
                job = ["launchctl", "print", f"gui/{os.getuid()}/{mirror.LAUNCHD_LABEL}"]
                boot = [i for i, c in enumerate(h.calls[n:]) if c[:2] == ["launchctl", "bootstrap"]]
                out = [i for i, c in enumerate(h.calls[n:]) if c == ["launchctl", "bootout", f"gui/{os.getuid()}/{mirror.LAUNCHD_LABEL}"]]
                self.assertTrue(boot and out and max(out) > max(boot), h.calls[n:])
                # G1.17: taken off by the one function, and read gone after it (the owner's ruling, 11-25)
                self.assertTrue([i for i, c in enumerate(h.calls[n:]) if c == job and i > max(out)], h.calls[n:])
                h.launchd_args = None
                h.print_code = 1
                self.assertEqual(mirror.service_read_back(p, "forge"), "unread")
        finally:
            mirror.system = saved

    def test_G1_24_a_persons_own_entry_is_theirs_and_no_line_holds_a_value_the_tool_did_not_write(self):
        """M148, the family's rule 31 (v.5) (3): an environment entry the person set in the skill's service
        themselves (a drop-in of theirs on Linux; a variable in the loaded job on a Mac), holding a made-up secret,
        is named as theirs by its name alone, a warning: setup goes on, the read-back is equal, and no line of status,
        of the restart line or of the warning holds its value. A second entry for WSM_HOME (systemd) or WSM_HOME
        changed (launchd) is a stop, named by the part, and no line holds the planted value. The read-back's meant
        side is what setup wrote: a terminal with another WSM_HOME reads the same answer."""
        import socket
        secret = "madeup-secret-7f3a9c"
        planted = f"http://me:{secret}@proxy.example:8080"
        saved, door, rel = mirror.system, mirror.read_door, mirror.release_check
        mirror.read_door = lambda p, port: ("none", "no program listens on it")
        mirror.release_check = lambda st, force=False: []
        try:
            for name in ("linux", "darwin"):
                mirror.system = lambda name=name: (name, "arm64" if name == "darwin" else "x86_64")
                with Home() as h, self.subTest(system=name):
                    p = mirror.paths()
                    s = socket.socket()
                    s.bind(("127.0.0.1", 0))
                    port = s.getsockname()[1]
                    s.close()
                    for d in (p["data"], p["conf"], os.path.dirname(p["plist"] if name == "darwin" else p["unit"])):
                        os.makedirs(d, mode=0o700, exist_ok=True)
                        os.chmod(d, 0o700)
                    mirror.save_state(p, {"port": port})
                    with open(p["mirror_key"], "w") as f:
                        f.write("5" * 40 + "\n")           # made up, in the form the forge gives (M156)
                    shutil.copyfile(SCRIPT, p["script"])
                    self.assertEqual(mirror.install_service(p), (True, ""))
                    self.assertEqual(mirror.install_timer(p, 15), (True, ""))
                    if name == "darwin":
                        h.launchd_env_extra = {"HTTPS_PROXY": planted}
                    else:
                        h.bus_env_extra = [f"HTTPS_PROXY={planted}"]
                    for which in ("forge", "sync"):
                        self.assertEqual(mirror.service_read(p, which), ("equal", ["HTTPS_PROXY"], []))
                    warn = mirror.theirs_line(p)
                    self.assertEqual(warn, "Your own setting is in the skill's service too, set by you, not by this "
                                           "tool: HTTPS_PROXY. It is left as it is; this tool reads only the values it "
                                           "set itself.")
                    mirror.DOOR_SEEN.clear()
                    rc, out = status()
                    self.assertIn("note: " + warn, out)
                    self.assertNotIn("as setup wrote it", out)
                    self.assertNotIn(secret, out)
                    self.assertNotIn(secret, mirror.restart_line(p, {}))
                    home = os.environ["WSM_HOME"]
                    os.environ["WSM_HOME"] = home + "-another-terminal"
                    try:
                        self.assertEqual(mirror.service_read(p, "forge")[0], "equal")
                    finally:
                        os.environ["WSM_HOME"] = home
                    if name == "linux":
                        # SEEN on systemd 237 to 255 (the family's rule 31 (v.5) (3), 4:03 PM ET 11-25): a drop-in of
                        # the person's own that sets WSM_HOME leaves ONE entry, with their value. A stop; their value
                        # is never said; the step is the manager's own listing, never uninstall or setup again.
                        h.bus_env_extra = [f"HTTPS_PROXY={planted}"]
                        h.bus_env_set = {"WSM_HOME": planted}
                        h.bus_dropins = [os.path.join(os.path.dirname(p["unit"]), mirror.SERVICE + ".service.d",
                                                      "mine.conf")]
                        self.assertEqual(mirror.service_read(p, "forge"),
                                         ("differs", ["HTTPS_PROXY"], ["its WSM_HOME entry"]))
                        line = mirror.restart_line(p, {})
                        self.assertTrue(line.startswith("After a restart: systemd does not hold the forge's service as "
                                                        "setup wrote it: its WSM_HOME entry is not read as setup wrote "
                                                        "it, so it is not set up. 1 drop-in file stands on it"), line)
                        self.assertTrue(line.endswith("systemctl --user cat " + mirror.SERVICE + ".service"), line)
                        mirror.DOOR_SEEN.clear()
                        rc, out = status()
                        for said in (line, out):
                            self.assertNotIn(secret, said)
                            self.assertNotIn("uninstall", said.split("its WSM_HOME entry")[-1].split("\n")[0])
                            self.assertNotIn("setup again", said.split("its WSM_HOME entry")[-1].split("\n")[0])
                        h.bus_env_set, h.bus_dropins = {}, []
                    # Below, on Linux, a SECOND entry for WSM_HOME: the STAND-IN'S OWN FORM, produced by none of the
                    # five systemd versions tried (rule 31 (v.5) (3)); the stop it holds is said by its count.
                    if name == "darwin":
                        h.launchd_env_extra = {"HTTPS_PROXY": planted, "WSM_HOME": planted}
                    else:
                        h.bus_env_extra = [f"HTTPS_PROXY={planted}", f"WSM_HOME={planted}"]
                    entry = "its WSM_HOME entry" if name == "darwin" else "its WSM_HOME entry (2 found where setup wrote one)"
                    self.assertEqual(mirror.service_read(p, "forge"), ("differs", ["HTTPS_PROXY"], [entry]))
                    mgr = "launchd" if name == "darwin" else "systemd"
                    line = mirror.restart_line(p, {})
                    self.assertTrue(line.startswith(f"After a restart: {mgr} does not hold the forge's service as "
                                                    f"setup wrote it: {entry} is not read as setup wrote it, so it is "
                                                    "not set up."), line)
                    mirror.DOOR_SEEN.clear()
                    rc, out = status()
                    self.assertIn(f"{entry} is not read as setup wrote it", out)
                    self.assertNotIn(secret, out + line)
                    if name == "linux":
                        n = len(h.calls)
                        ok, why = mirror.install_service(p)
                        self.assertFalse(ok)
                        self.assertNotIn(secret, why)
                        self.assertFalse([c for c in h.calls[n:] if c[:3] == ["systemctl", "--user", "enable"]])
        finally:
            mirror.system, mirror.read_door, mirror.release_check = saved, door, rel

    def test_G1_24_a_start_line_of_the_persons_own_beside_the_syncs_is_theirs_by_its_count(self):
        """M148, the family's rule 31 (v.5) (3) as corrected 4:22 PM ET 11-25: the sync's unit is one-shot (the
        list holds its start lines in order, seen; that the manager runs them in that order is recorded on five
        systems, the user manager (rule 31 (v.5) (3), 4:52 PM ET), and a line still says only what the tool read). A start line of the person's own beside ours, ours held
        once, as written and first, is theirs: named by its count, never its words (it holds a made-up secret here),
        the listing as the step and as the last words of the line, and the answer stays "set up" (the timer goes on).
        Ours not first, ours twice, ours changed, the manager holding the sync as another type, and any start line of
        theirs on the forge (written simple: seen by no one) are each a stop, its step the listing where a drop-in
        stands. Each line that gives a command ends with it (the same rule, the same hour)."""
        secret = "madeup-secret-5c1e"
        theirs = ["/bin/sh", ["/bin/sh", "-c", f"curl -s https://me:{secret}@hook.example/"], False, 0, 0, 0, 0, 0, 0, 0]
        import socket
        saved, door, rel = mirror.system, mirror.read_door, mirror.release_check
        mirror.system = lambda: ("linux", "x86_64")
        mirror.read_door = lambda p, port: ("none", "no program listens on it")
        mirror.release_check = lambda st, force=False: []
        try:
            with Home() as h:
                p = mirror.paths()
                for d in (p["data"], p["conf"], os.path.dirname(p["unit"])):
                    os.makedirs(d, mode=0o700, exist_ok=True)
                    os.chmod(d, 0o700)
                s = socket.socket()
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
                s.close()
                mirror.save_state(p, {"port": port})
                with open(p["mirror_key"], "w") as f:
                    f.write("5" * 40 + "\n")           # made up, in the form the forge gives (M156)
                shutil.copyfile(SCRIPT, p["script"])
                self.assertEqual(mirror.install_service(p), (True, ""))
                self.assertEqual(mirror.install_timer(p, 15), (True, ""))
                sync = mirror.SYNC_UNIT + ".service"
                ours = sd_standin_unit(fread(os.path.join(os.path.dirname(p["unit"]), sync)), h.env["HOME"])[0]
                self.assertEqual(len(ours), 1)
                h.bus_dropins = [os.path.join(os.path.dirname(p["unit"]), sync + ".d", "mine.conf")]
                h.bus_starts_extra = [theirs]
                self.assertEqual(mirror.service_read(p, "sync"), ("equal", [1], []))
                warn = mirror.theirs_line(p)
                # what was READ, the order in the list; that systemd runs them in that order is not recorded (rule 31
                # (v.5) (3), 4:27 PM ET 11-25), so no line says it
                self.assertEqual(warn, "1 start line of your own is in the sync's service, set by you, not by this "
                                       "tool; it stands after the skill's own in systemd's list, and it is left as it "
                                       "is. To read it: systemctl --user cat " + sync)
                self.assertNotIn("runs", warn)
                n = len(h.calls)
                self.assertEqual(mirror.install_timer(p, 15), (True, ""))       # set up: the timer goes on
                self.assertTrue([c for c in h.calls[n:] if c[:3] == ["systemctl", "--user", "enable"]])
                mirror.DOOR_SEEN.clear()
                rc, out = status()
                self.assertIn("note: " + warn, out)
                self.assertNotIn("the sync's service as setup wrote it", out)   # (the stand-in plants it on the forge too)
                h.bus_starts_extra = [theirs, theirs]
                self.assertTrue(mirror.theirs_line(p).startswith("2 start lines of your own are in the sync's"))
                self.assertNotIn(secret, out + warn + mirror.theirs_line(p))
                changed = [ours[0][0], ours[0][1] + ["--more"]] + ours[0][2:]
                for label, starts, typ in (("ours not first", [theirs] + ours, None),
                                           ("ours twice", ours + [theirs] + ours, None),
                                           ("ours changed", [changed, theirs], None),
                                           ("held as another type", ours + [theirs], "simple")):
                    with self.subTest(case=label):
                        h.bus_starts, h.bus_starts_extra, h.bus_type = starts, [], typ
                        self.assertEqual(mirror.service_read(p, "sync"), ("differs", [], ["its start line"]))
                        ok, why = mirror.install_timer(p, 15)
                        self.assertFalse(ok)
                        self.assertTrue(why.endswith("systemctl --user cat " + sync), why)
                        self.assertNotIn("setup again", why.split("its start line")[-1])
                        self.assertNotIn(secret, why)
                h.bus_starts, h.bus_starts_extra, h.bus_type = None, [theirs], "unreadable"
                self.assertEqual(mirror.service_read(p, "sync"), ("unread", [], []))   # a read that failed: unread
                h.bus_starts, h.bus_type = None, None
                h.bus_starts_extra = [theirs]            # the forge, written simple: theirs there is seen by no one
                h.bus_dropins = [os.path.join(os.path.dirname(p["unit"]), mirror.SERVICE + ".service.d", "mine.conf")]
                self.assertEqual(mirror.service_read(p, "forge"), ("differs", [], ["its start line"]))
                line = mirror.restart_line(p, {})
                self.assertTrue(line.endswith("systemctl --user cat " + mirror.SERVICE + ".service"), line)
                self.assertNotIn(secret, line)
                # A COMMAND STANDS LAST ON ITS LINE: each step line of the cure that gives a command ends with it.
                given = [mirror.theirs_line(p)]
                for n_in in ([], h.bus_dropins):
                    h.bus_dropins = n_in
                    for which in ("forge", "sync"):
                        for answer in ("differs", "unread", "unrecorded"):
                            for where in ("setup", "status"):
                                given.append(mirror.read_back_step(which, answer, where))
                h.user_bus = False
                given.append(mirror.bus_floor(p))
                h.user_bus = True
                cmds = 0
                for g in given:
                    m = re.search(r"systemctl --user cat \S+|" + re.escape(mirror.own_command(p, "setup")), g)
                    if m:
                        cmds += 1
                        self.assertEqual(g[m.end():], "", g)
                self.assertGreaterEqual(cmds, 6)
        finally:
            mirror.system, mirror.read_door, mirror.release_check = saved, door, rel

    def test_G1_24_a_mac_print_as_recorded_on_each_macos_image_is_read_whole(self):
        """M148 (the owner's ruling 3, 11-25): launchctl print's answer for one made-up agent, RECORDED on each macOS
        image GitHub's standard runners offered that day (15.7.9 and 26.6.1 on Intel, 15.8.2 and 26.7.2 on Apple
        Silicon; tests/launchd-print, each tab kept as \\t), is read by the tool whole: its arguments one by one, the
        one with a space whole, and its own variable; with setup's record set to that agent the read-back is equal and
        nothing is the person's own (launchd's own OSLogRateLimit is there on 26 and not on 15; neither is counted).
        One argument other than recorded is a difference."""
        saved = mirror.system
        mirror.system = lambda: ("darwin", "arm64")
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launchd-print")
        forms = sorted(f for f in os.listdir(here) if f.endswith(".txt"))
        self.assertEqual(forms, ["macos-15.7.9-x86_64.txt", "macos-15.8.2-arm64.txt", "macos-26.6.1-x86_64.txt",
                                 "macos-26.7.2-arm64.txt"])
        args = ["/bin/sh", "-c", "sleep 300"]
        try:
            for name in forms:
                with Home() as h, self.subTest(form=name):
                    p = mirror.paths()
                    os.makedirs(p["data"], exist_ok=True)
                    text = re.sub(r"\\([\\t])", lambda m: "\t" if m.group(1) == "t" else "\\",
                                  fread(os.path.join(here, name)))
                    h.launchd_form = text
                    with open(os.path.join(p["data"], mirror.SERVICE_MEANT), "w") as f:
                        json.dump({"forge": {"argv": args, "env": {"TRIAL_VALUE": "made up"}}}, f)
                    got = mirror.launchd_parsed(mirror.LAUNCHD_LABEL)
                    self.assertEqual(got[0], args)
                    self.assertEqual(got[1]["TRIAL_VALUE"], "made up")
                    self.assertEqual(set(got[1]) - {"TRIAL_VALUE"}, set(got[1]) & set(mirror.LAUNCHD_OWN_ENV))
                    self.assertEqual("OSLogRateLimit" in got[1], name.startswith("macos-26"))
                    self.assertEqual(mirror.service_read(p, "forge"), ("equal", [], []))
                    with open(os.path.join(p["data"], mirror.SERVICE_MEANT), "w") as f:
                        json.dump({"forge": {"argv": args[:2] + ["sleep 301"], "env": {"TRIAL_VALUE": "made up"}}}, f)
                    self.assertEqual(mirror.service_read(p, "forge"), ("differs", [], ["its arguments"]))
        finally:
            mirror.system = saved

    def test_G1_24_a_mac_print_in_a_made_up_other_form_is_could_not_be_read(self):
        """M148 (the owner's ruling, 11-25, point 7 (b)): launchd's print form is seen on the macOS versions
        MACOS_PRINT_SEEN names only. A print
        block in another form, MADE UP here (no macOS has been seen printing these: one with no arguments block of
        the seen form, one with the seen arguments block and no environment block where setup set a variable), is
        answered "could not be read" with its step: never "differs", never "not set up", never a road to uninstall;
        the step ends with the command that prints this Mac's version. A job setup wrote with no variable reads equal
        with no environment block (whether launchd prints one for such a job is not seen)."""
        saved = mirror.system
        mirror.system = lambda: ("darwin", "arm64")
        made_up_no_arguments = ("gui/501/x = {\n\tstate = running\n\tProgramArguments = (\n\t\t\"/usr/bin/python3\",\n"
                                "\t)\n\tEnvironmentVariables = (\n\t\tWSM_HOME = \"/made/up\"\n\t)\n}\n")
        try:
            with Home() as h:
                p = mirror.paths()
                os.makedirs(os.path.dirname(p["plist"]), exist_ok=True)
                self.assertEqual(mirror.install_service(p), (True, ""))
                args = mirror.read_meant(p, "forge")[0]
                made_up_no_environment = ("gui/501/x = {\n\tstate = running\n\targuments = {\n"
                                          + "".join(f"\t\t{a}\n" for a in args) + "\t}\n}\n")
                for label, form in (("made up: no arguments block", made_up_no_arguments),
                                    ("made up: no environment block", made_up_no_environment)):
                    with self.subTest(form=label):
                        h.launchd_form = None
                        self.assertEqual(mirror.install_service(p), (True, ""))      # loaded again, read equal
                        h.launchd_form = form
                        self.assertEqual(mirror.service_read(p, "forge"), ("unread", [], []))
                        line = mirror.restart_line(p, {})           # read while it is loaded
                        ok, why = mirror.install_service(p)
                        self.assertFalse(ok)
                        for said, first in ((why, "Run setup again in a minute;"),
                                            (line, "Run status from a terminal window of your own;")):
                            self.assertIn("whether launchd holds the forge's service as setup wrote it could not be "
                                          "read", said)
                            self.assertIn(first + " if this line comes back, launchd's answer on this Mac may be in a "
                                          f"form this tool does not read yet (it was seen on macOS "
                                          f"{mirror.MACOS_PRINT_SEEN})", said)
                            self.assertTrue(said.endswith("which this prints: sw_vers -productVersion"), said)
                            for never in ("not set up", "uninstall", "does not hold"):
                                self.assertNotIn(never, said)
                h.launchd_form = None
                self.assertEqual(mirror.install_service(p), (True, ""))
            with Home() as h:
                os.environ.pop("WSM_HOME", None)            # put back by Home on its way out
                p = mirror.paths()
                os.makedirs(os.path.dirname(p["plist"]), exist_ok=True)
                self.assertEqual(mirror.install_service(p), (True, ""))
                self.assertEqual(mirror.read_meant(p, "forge")[1], {})
                args = mirror.read_meant(p, "forge")[0]
                h.launchd_form = ("gui/501/x = {\n\tstate = running\n\targuments = {\n"
                                  + "".join(f"\t\t{a}\n" for a in args) + "\t}\n}\n")
                self.assertEqual(mirror.service_read(p, "forge"), ("equal", [], []))
        finally:
            mirror.system = saved

    def test_G1_24_setup_stops_before_its_first_write_when_the_read_backs_road_is_not_there(self):
        """M148 (the owner's ruling, 11-25: the read-back's road is a floor, and a floor is checked before the first
        write): on Linux setup reads the manager's own version over the user bus in busctl's JSON form before it
        makes anything. A shell with no user bus, a busctl that does not answer in that form, none at all, or no
        answer within the limit: setup and its dry run end 2, the home is read unchanged, no unit is written and
        systemd is asked nothing else; the line says what was found and the one next step. On a Mac there is no
        such read."""
        saved = mirror.system
        login = "The one next step: run setup from a login of your own to this computer, such as ssh or a terminal at the screen"
        # (what the case sets, its value, the words said, busctl reads, how the line ends: "cmd" = with the command)
        cases = (("no_vars", True, "this shell cannot reach your account's own systemd: it has neither "
                                   "XDG_RUNTIME_DIR nor DBUS_SESSION_BUS_ADDRESS", 0, "cmd"),
                 ("user_bus", False, "this shell cannot reach your account's own systemd: busctl found no bus where "
                                     "XDG_RUNTIME_DIR or DBUS_SESSION_BUS_ADDRESS leads", 1, "cmd"),
                 # RECORDED (the family's record of five stock systems, 2026-11-25): busctl's own refusal of the JSON
                 # form on systemd 237 (Ubuntu 18.04) and 239 (AlmaLinux 8.10), in every shell, with a bus too
                 ("old", ("237", (1, "", "busctl: unrecognized option '--json=short'\n")),
                  "this computer's systemd is 237 whose busctl does not know the JSON form setup reads with (busctl "
                  f"answered that the option is not one it knows); the skill was seen to run on systemd "
                  f"{mirror.OLDEST_SEEN_SYSTEMD},", 1,
                  "set the skill up on a computer whose busctl answers in that form, such as one with systemd "
                  f"{mirror.OLDEST_SEEN_SYSTEMD}, as the README says."),
                 # MADE UP: an answer no one has recorded, on the oldest systemd seen, and on 249, which the same
                 # record shows answering the read: the version alone never names a cause (the owner's ruling, 11-25)
                 ("bus_version", (3, "", "made-up words\n"), "busctl answered exit 3 when setup read your account's "
                                                              "own systemd over its bus", 1, "cmd"),
                 ("old", ("249", (1, "", "made-up words\n")), "busctl answered exit 1 when setup read your "
                                                               "account's own systemd over its bus", 1, "cmd"),
                 # RECORDED (the same family record): systemd 245's busctl, a runtime folder with no bus, its words
                 # between colour escapes although standard error was a file
                 ("bus_version", (1, "", "\x1b[0;1;31mFailed to connect to bus: No such file or directory\x1b[0m\n"),
                  "this shell cannot reach your account's own systemd: busctl found no bus where XDG_RUNTIME_DIR or "
                  "DBUS_SESSION_BUS_ADDRESS leads", 1, "cmd"),
                 ("bus_version", (0, "made-up, not JSON\n", ""), "systemd 255 answered over your account's bus in a "
                                                                 "form setup does not read (busctl's JSON form)", 1,
                  "open an issue with this line."),
                 ("bus_version", (127, "", "busctl is not installed on this computer"), "this computer has no busctl",
                  1, "open an issue with this line."),
                 ("bus_version", "timeout", f"did not answer over its bus within {mirror.BUS_WAIT} s", 1,
                  "run setup again in a minute."))
        saved_doors = doors_the_hosts_way(saved)
        try:
            mirror.system = lambda: ("linux", "x86_64")
            for name, value, said, reads, end in cases:
                for args in (("--dry-run",), ("--linger",)):
                    with Home(linger="yes") as h, self.subTest(case=said, args=args):
                        if name == "no_vars":
                            os.environ.pop("XDG_RUNTIME_DIR", None)
                            saved_dbus = os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
                        elif name == "old":
                            h.sd_version, h.bus_version = value
                        else:
                            setattr(h, name, value)
                        before = tree(h.tmp)
                        try:
                            rc, out = h.setup(*args)
                        finally:
                            if name == "no_vars" and saved_dbus is not None:
                                os.environ["DBUS_SESSION_BUS_ADDRESS"] = saved_dbus
                        line = [x for x in out.splitlines() if x.startswith("Stopped: ")]
                        self.assertEqual(rc, 2, out)
                        self.assertEqual(len(line), 1, out)
                        self.assertIn(said, line[0])
                        self.assertIn("Setup made nothing. The one next step: ", line[0])
                        if end == "cmd":                # the login first; the command stands last on the line
                            self.assertIn(login, line[0])
                            self.assertTrue(line[0].endswith(": " + mirror.own_command(mirror.paths(), "setup")),
                                            line[0])
                            if "answered exit" in said:  # an issue only if the line comes back in that login
                                self.assertIn(login + ", and if this line comes back there, open an issue", line[0])
                        else:
                            self.assertTrue(line[0].endswith(end), line[0])
                        if name in ("no_vars", "user_bus"):
                            self.assertNotIn("issue", line[0])
                        if name == "old" and value[0] == "249":
                            self.assertNotIn("249", line[0])       # nothing blamed on the version it read
                        if (name, value[0] if name == "old" else None) != ("old", "237") and \
                                (name != "bus_version" or value[0] != 0):
                            self.assertNotIn("form", line[0])     # nothing about a form unless JSON came back
                        self.assertEqual(tree(h.tmp), before)
                        self.assertFalse(os.path.exists(mirror.paths()["unit"]))
                        self.assertFalse([c for c in h.calls if c[:2] == ["systemctl", "--user"]], h.calls)
                        self.assertEqual(len([c for c in h.calls if c[0] == "busctl"]), reads, h.calls)
            mirror.system = lambda: ("darwin", "arm64")
            with Home() as h:
                h.user_bus = False
                self.assertIsNone(mirror.bus_floor(mirror.paths()))
                self.assertFalse([c for c in h.calls if c[0] in ("busctl", "systemctl")])
        finally:
            mirror.system = saved
            mirror.read_door, mirror.door_floor = saved_doors

    def test_G1_24_every_read_of_a_service_manager_has_a_limit_and_each_is_read_once(self):
        """M148 (the owner's ruling, 11-25): every read over the manager's bus, and launchd's print, is given
        BUS_WAIT; past it the answer is "could not be read", never a wait without end. Status reads each service once
        and the warning about the person's own entry uses those reads; setup reads that warning once. Setup's own
        record not there is its own answer, "unrecorded", with setup again as the first step and no bus read."""
        seen = []

        def slow(cmd, **kw):
            seen.append((cmd[0], kw.get("timeout")))
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        saved_run, saved = mirror.run, mirror.system
        try:
            mirror.run = slow
            mirror.system = lambda: ("linux", "x86_64")
            self.assertIsNone(mirror.systemd_parsed(mirror.SERVICE + ".service"))
            self.assertIsNone(mirror.launchd_parsed(mirror.LAUNCHD_LABEL))
            self.assertEqual(mirror.systemd_version(), "")
            was = os.environ.get("XDG_RUNTIME_DIR")
            os.environ["XDG_RUNTIME_DIR"] = "/run/user/made-up"          # as a login sets it
            try:
                self.assertIn(f"within {mirror.BUS_WAIT} s", mirror.bus_floor(None))
            finally:
                if was is None:
                    os.environ.pop("XDG_RUNTIME_DIR")
                else:
                    os.environ["XDG_RUNTIME_DIR"] = was
        finally:
            mirror.run = saved_run
        self.assertEqual({t for _, t in seen}, {mirror.BUS_WAIT}, seen)
        self.assertEqual({c for c, _ in seen}, {"busctl", "launchctl", "systemctl"}, seen)
        try:
            with Home() as h:
                p = mirror.paths()
                os.makedirs(os.path.dirname(p["unit"]), exist_ok=True)
                self.assertEqual(mirror.install_service(p), (True, ""))
                self.assertEqual(mirror.install_timer(p, 15), (True, ""))
                self.assertTrue(h.bus_kws and all(k.get("timeout") == mirror.BUS_WAIT for k in h.bus_kws), h.bus_kws)
                reads = {w: mirror.service_read(p, w) for w in ("forge", "sync")}
                n = len(h.calls)
                self.assertEqual(mirror.theirs_line(p, reads), "")
                self.assertEqual(len(h.calls), n, "the warning read the services again")
                h.bus_answer = "timeout"
                self.assertEqual(mirror.service_read(p, "forge")[0], "unread")
                self.assertIn("could not be read", mirror.restart_line(p, {}))
                h.bus_answer = None
                os.remove(os.path.join(p["data"], mirror.SERVICE_MEANT))
                n = len(h.calls)
                self.assertEqual(mirror.service_read(p, "forge"), ("unrecorded", [], []))
                self.assertFalse([c for c in h.calls[n:] if c[0] == "busctl"], "a missing record is not a bus read")
                line = mirror.restart_line(p, {})
                self.assertIn("what setup wrote for the forge's service could not be read from setup's own record, "
                              "so it is not set up. Run setup again, which writes that record again", line)
                self.assertNotIn("uninstall", line.split(";")[0])
                step = mirror.read_back_step("forge", "differs")
                self.assertTrue(step.startswith("A setting of your own that changes it comes off first, then run setup "
                                                "again; if this line comes back after that, run uninstall"), step)
                self.assertTrue(step.endswith("systemctl --user cat " + mirror.SERVICE + ".service"), step)
            status_src, setup_src = inspect.getsource(mirror.cmd_status), inspect.getsource(mirror.cmd_setup)
            self.assertIn("theirs_line(p, reads)", status_src)
            self.assertEqual(status_src.count("service_read("), 1)
            self.assertEqual(setup_src.count("theirs_line("), 1)
        finally:
            mirror.system = saved


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
        self.home = Home().__enter__()       # the suite's own home: the stand-in GitHub is taken only there (G9.12)
        self.addCleanup(self.home.__exit__, None, None, None)
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

    def test_G5_5_github_out_of_reach_is_said_so_never_a_code_or_a_no(self):
        """A real machine with its road to GitHub cut (2026-11-23) was told "GitHub answered 0". No answer is said
        as "could not be reached"; and in key set, no answer or GitHub's own failure (5xx) is never "this key
        cannot see": nothing is kept, and the step is to run key set again."""
        api = os.environ["WSM_GITHUB_API"]
        os.environ["WSM_GITHUB_API"] = f"http://127.0.0.1:{mirror.free_port()}"      # nothing listens there
        try:
            repos, why, _ = mirror.list_owner("someone", "user", self.gh.key)
            self.assertIsNone(repos)
            self.assertEqual(why, "the listing of someone failed at page 1 (GitHub could not be reached)")
            rc, out = self.key_set("acme", self.gh.key)
            self.assertNotEqual(rc, 0)
            self.assertIn("GitHub could not be reached; nothing was kept. Run key set again in a few minutes.", out)
        finally:
            os.environ["WSM_GITHUB_API"] = api
        real = mirror.gh_get
        for step in ("/orgs/", "/repos?"):
            for code in (0, 502):
                def gh_get(key, path, step=step, code=code):
                    return (code, {}, {"message": "x"}) if step in path else real(key, path)
                mirror.gh_get = gh_get
                try:
                    rc, out = self.key_set("acme", self.gh.key)
                finally:
                    mirror.gh_get = real
                self.assertNotEqual(rc, 0, out)
                self.assertNotIn("cannot", out, f"{step} answered {code}: said as a no")
                self.assertNotIn("answered 0", out)
                self.assertIn("nothing was kept. Run key set again in a few minutes.", out)
                self.assertFalse(os.path.exists(mirror.key_path(mirror.paths(), "acme")))

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


class AtTerminal(io.StringIO):
    """Standing in for the person's own terminal: what is typed there."""
    def isatty(self):
        return True


def disk_holds(where, name):
    """Whether the disk under the folder `where` can hold a file of that name at all (a Mac's refuses bytes that
    are not UTF-8): asked of the disk, never guessed by the system's name."""
    d = tempfile.mkdtemp(dir=where)
    try:
        open(os.path.join(d, name), "w").close()
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(d)


def disk_folds(where, a, b):
    """Whether the disk under the folder `where` makes the names a and b one file (a Mac's folds case and the two
    spellings of an accented letter): asked of the disk, never guessed by the system's name."""
    d = tempfile.mkdtemp(dir=where)
    try:
        open(os.path.join(d, a), "w").close()
        return os.path.exists(os.path.join(d, b))
    finally:
        shutil.rmtree(d)


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

    def go(self, answer="\n"):
        """G3.13: the person's own go, typed at a terminal of their own (a stand-in terminal)."""
        saved = sys.stdin
        sys.stdin = AtTerminal(answer)
        try:
            return quiet(mirror.main, ["go"])
        finally:
            sys.stdin = saved

    def first_sync(self):
        self.tc.assertEqual(self.sync("--announce")[0], 0)
        rc, out = self.go()
        self.tc.assertEqual(rc, 0, out)
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
            with Home():       # the suite's own home: WSM_PAGE_SIZE is read only there (G9.12)
                for val, want in ((None, 100), ("3", 3), ("1", 1), ("100", 100), ("0", 100), ("101", 100),
                                  ("x", 100), ("-5", 100)):
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
                w.go()
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
                if holds:                       # the fetch, and the read of GitHub's own list before it (M123)
                    self.assertTrue("fetch" in cmd or "ls-remote" in cmd,
                                    f"the key was given to a process that does not read GitHub: {cmd[:6]}")
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
            rc, out = w.go()
            self.assertEqual(rc, 0, out)
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
            self.assertIn("Nothing was copied. The go for the first copy is the person's own, given in a terminal window "
                          "of their own", out)
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
            self.assertIn("first copy: waiting for your go: in a terminal window of your own, run python3 -I ",
                          quiet(mirror.main, ["status"])[1])
            rc, out = w.sync()                                   # the agent's sync, with no go of the person's
            self.assertEqual(rc, 2, out)
            self.assertEqual(w.forge_get("/user/repos")[1], [], "a sync copied before the person's own go")
            self.assertEqual(w.go()[0], 0)
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

    def test_G3_14_the_fetch_stays_on_the_road_the_read_only_key_travels(self):
        """M74 (seen 2026-11-24, a sighting in the suite): a person's git config with the common line
        url."git@github.com:".insteadOf "https://github.com/" sent the skill's fetch over ssh, where the read-only key
        does not travel and the person's own ssh key, with the whole account's rights, would; the sync broke and said
        only "the fetch from GitHub failed: and the repository exists." The skill's own git now holds its road to the
        project's full address: with either common form (an ssh:// address, and the git@host: form) the fetch, the
        large files' fetch included, stays on HTTPS with the read-only key and ssh is never run; a line for the
        project's whole address, which the hold cannot outrank, stops that one copy with a sentence that says why and
        the one step that mends it; an address git cannot read stops it the same way; no transport but the scheme of
        the address the skill set is allowed; every git call that names GitHub's address goes through one function;
        a failed fetch is said by git's first fatal line."""
        lfs = mirror.has_git_lfs()
        if not lfs and os.environ.get("WSM_REQUIRE_GITEA"):
            raise AssertionError("git lfs is needed for this test in CI")
        data = os.urandom(150_000)
        bin_ = tempfile.mkdtemp(prefix="wsm-ssh-")
        self.addCleanup(shutil.rmtree, bin_, True)
        ran = os.path.join(bin_, "ran")
        with open(os.path.join(bin_, "ssh"), "w") as f:
            f.write(f"#!/bin/sh\necho \"$*\" >> {ran}\necho 'ssh: connect to host 127.0.0.1 port 1: Connection refused' >&2\nexit 255\n")
        os.chmod(os.path.join(bin_, "ssh"), 0o755)
        path = os.environ["PATH"]
        os.environ["PATH"] = bin_ + os.pathsep + path
        try:
            with World(self) as w:
                w.gh.add_repo("someone", "alpha")
                w.gh.commit("someone/alpha", {"a": b"1"})
                if lfs:
                    w.gh.add_repo("someone", "big")
                    w.gh.commit("someone/big", {".gitattributes": b"*.bin filter=lfs diff=lfs merge=lfs -text\n",
                                                 "blob.bin": w.gh.lfs_file(data)})
                w.key_set()
                base = os.environ["WSM_GITHUB_GIT"].rstrip("/")
                host = urllib.parse.urlsplit(base).hostname
                gc = os.path.join(os.environ["HOME"], ".gitconfig")
                for n, form in enumerate((f'[url "ssh://git@{host}:1/"]\n\tinsteadOf = {base}/\n',
                                          f'[url "git@{host}:"]\n\tinsteadOf = {base}/\n')):
                    with open(gc, "w") as f:
                        f.write(form)
                    if n:
                        w.gh.commit("someone/alpha", {"n": str(n).encode()})
                    rc, out = w.sync() if n else w.first_sync()
                    self.assertEqual(rc, 0, out)
                    self.assertEqual(w.forge_refs("someone--alpha"), w.gh_refs("someone/alpha"), form)
                    self.assertFalse(os.path.exists(ran), f"the fetch went over ssh with {form}")
                if lfs:
                    key = fread(w.p["mirror_key"]).strip()
                    req = urllib.request.Request(f"http://127.0.0.1:{w.port}/api/v1/repos/mirror/someone--big/media/blob.bin",
                                                 headers={"Authorization": f"token {key}"})
                    self.assertEqual(urllib.request.urlopen(req, timeout=10).read(), data)
                # A line for the project's whole address: that copy stops, said with the one step that mends it.
                w.gh.commit("someone/alpha", {"b": b"2"})
                with open(gc, "w") as f:
                    f.write(f'[url "ssh://git@{host}:1/someone/alpha.git"]\n\tinsteadOf = {base}/someone/alpha.git\n')
                rc, out = w.sync()
                self.assertNotEqual(rc, 0, out)
                self.assertIn(mirror.REWRITTEN.format(where=f"ssh://{host}"), out)
                self.assertFalse(os.path.exists(ran), "the fetch went over ssh")
                self.assertNotEqual(w.forge_refs("someone--alpha"), w.gh_refs("someone/alpha"))
                listed = subprocess.run(["git", "config", "--global", "--get-regexp", "insteadof"], capture_output=True,
                                        text=True, env=dict(os.environ, GIT_CONFIG_GLOBAL=gc))
                self.assertIn(f"{base}/someone/alpha.git", listed.stdout)    # the step names the line to take out
                os.remove(gc)
                rc, out = w.sync()
                self.assertEqual(rc, 0, out)
                self.assertEqual(w.forge_refs("someone--alpha"), w.gh_refs("someone/alpha"))
                # An address git cannot read: no fetch, said with the step that shows the setting it stops at.
                w.gh.commit("someone/alpha", {"c": b"3"})
                with open(gc, "w") as f:
                    f.write("[url\n")
                rc, out = w.sync()
                self.assertNotEqual(rc, 0, out)
                self.assertIn("the address git would use for GitHub could not be read (fatal: bad config line 1", out)
                self.assertIn("This command shows the git setting it stops at; with that line mended, the next sync refreshes "
                              "it:\n    git config --global --list", out)
                shown = subprocess.run(["git", "config", "--global", "--list"], capture_output=True, text=True,
                                       env=dict(os.environ, GIT_CONFIG_GLOBAL=gc))
                self.assertIn(f"bad config line 1 in file {gc}", shown.stderr)
                os.remove(gc)
                rc, out = w.sync()
                self.assertEqual(rc, 0, out)
                self.assertEqual(w.forge_refs("someone--alpha"), w.gh_refs("someone/alpha"))
                self.assertFalse(os.path.exists(ran), "ssh ran")
                # No transport but the scheme of the address the skill set (the stand-in's; GitHub's is https).
                r = mirror.github_call(None, base + "/someone/alpha.git", "ls-remote", f"ssh://git@{host}:1/someone/alpha.git")
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("transport 'ssh' not allowed", r.stderr)
                self.assertFalse(os.path.exists(ran), "ssh ran")
        finally:
            os.environ["PATH"] = path
        self.assertEqual(mirror.road("https://github.com/a/b.git")[1::2],
                         ["url.https://github.com/a/b.git.insteadOf=https://github.com/a/b.git", "protocol.allow=never",
                          "protocol.https.allow=always"])
        # Every git call that names GitHub's address goes through github_call, and the key only there.
        src = fread(SCRIPT)
        body = re.search(r"\ndef github_call\(.*?(?=\n\S)", src, re.S).group(0)
        rest = src.replace(body, "").replace("def github_env(", "")
        self.assertEqual(re.findall(r"github_env\(", rest), [], "the key handed to git around github_call")
        calls = re.findall(r"(?<!def )\bgit\((?:[^()]|\([^()]*\))*\)", rest)
        self.assertEqual([c for c in calls if '"github"' in c or "github_git()" in c or "via=" in c], [],
                         "a git call names GitHub's address around github_call")
        self.assertEqual(mirror.where_to("ssh://git@127.0.0.1:1/someone/alpha.git"), "ssh://127.0.0.1")
        self.assertEqual(mirror.where_to("git@github.com:someone/alpha.git"), "an ssh address on github.com")
        self.assertEqual(mirror.where_to("https://x-access-token:secret@example.com/a.git"), "https://example.com")
        self.assertEqual(mirror.git_reason("fatal: Could not read from remote repository.\n\nPlease make sure you have "
                                           "the correct access rights\nand the repository exists.\n"),
                         "fatal: Could not read from remote repository.")

    def test_G3_13_the_person_hears_it_in_their_own_terminal_and_the_go_is_their_enter(self):
        """M61, the family's rule 7 (2026-11-23: a small model reworded even a one-line block, dropped facts and
        invented a step): what the person must hear before the first copy is also printed by a command they run
        themselves, in their own terminal, before anything changes, and the copy goes on only after they press Enter
        there. The same facts on both roads; nothing piped in, typed otherwise or left unanswered is a go; a yes in
        a conversation (the agent's sync alone) copies nothing."""
        with World(self) as w:
            for n in ("alpha", "beta"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"a": b"1"})
            helped = io.StringIO()
            with contextlib.redirect_stdout(helped), self.assertRaises(SystemExit):
                mirror.main(["--help"])
            self.assertIn("shows you what will be copied and takes your go for the first copy",
                          " ".join(helped.getvalue().split()), "go's help line does not say what it does")
            rc, out = w.key_set()
            self.assertEqual(rc, 0, out)
            self.assertIn("the go for the first copy is yours, here in this window: run python3 -I ", out)
            self.assertIn(" go. It shows what will be copied and waits for your Enter.", out)
            rc, relayed = w.sync("--announce")
            self.assertEqual(rc, 0, relayed)
            agent_block = [x.split(": ", 1)[1] for x in relayed.splitlines() if x.startswith("tell the person ")]
            state = lambda: mirror.load_json(w.p["state"], {})
            saved = sys.stdin
            try:
                sys.stdin = io.StringIO("\n")                      # piped in, even an Enter: refused
                rc, out = quiet(mirror.main, ["go"])
            finally:
                sys.stdin = saved
            self.assertEqual(rc, 2, out)
            self.assertIn("in a terminal window of their own", out)
            self.assertNotIn("person_go", state())
            for typed in ("yes\n", "no\n", " \n", ""):              # anything but Enter alone, or nothing at all
                rc, out = w.go(typed)
                self.assertEqual(rc, 1, (typed, out))
                self.assertIn("No go was given. Nothing was copied or changed.", out)
                self.assertNotIn("person_go", state(), typed)
            rc, out = w.sync()                                   # the agent's sync on a yes in a conversation
            self.assertEqual(rc, 2, out)
            self.assertIn("it has not been given for this list", out)
            self.assertEqual(w.forge_get("/user/repos")[1], [])
            rc, out = w.go()
            self.assertEqual(rc, 0, out)
            self.assertNotIn("tell the person", out)              # said to the person, not for relaying
            lines = out.splitlines()
            block = [x.split(": ", 1)[1] for x in lines if re.match(r"^\d+ of \d+: ", x)]
            self.assertEqual(block, agent_block, "the person's own terminal and the agent's block say different things")
            for fact in (mirror.PRIVATE_AS_THIS_COMPUTER, mirror.NOT_COPIED, "The end date of the key for someone",
                         "After a restart", "They are: someone/alpha, someone/beta."):
                self.assertTrue(any(fact in x for x in block), fact)
            ask = next(i for i, x in enumerate(lines) if x.startswith("Press Enter to give your go"))
            self.assertGreater(ask, max(i for i, x in enumerate(lines) if re.match(r"^\d+ of \d+: ", x)))
            self.assertTrue(lines[-1].startswith("Your go is kept, for these 2 repositories only."), out)
            self.assertEqual(w.forge_get("/user/repos")[1], [], "go copied something itself")
            self.assertEqual(state()["person_go"]["list"], ["someone/alpha", "someone/beta"])
            self.assertNotIn("go", state())
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertEqual(sorted(r["name"] for r in w.forge_get("/user/repos")[1]), ["someone--alpha", "someone--beta"])

    def test_G3_15_nothing_one_project_holds_stops_the_sync(self):
        """M120: bytes that are not UTF-8 in one project (a .gitattributes, a branch's or a tag's name) never end the
        sync: under LANG=en_US.UTF-8 and with no LANG, in a fresh process writing to a pipe, the project is copied or
        named and the projects after it are copied; and a fault the tool did not foresee in one project names that
        project in plain words while the others are copied."""
        drive = ("import importlib.util, sys\n"
                 "s = importlib.util.spec_from_file_location('mirror', sys.argv[1]); m = importlib.util.module_from_spec(s)\n"
                 "s.loader.exec_module(m)\n"
                 "sys.stderr.write('stdout errors: ' + sys.stdout.errors + '\\n')\n"
                 "if sys.argv[2] == 'no-lfs':\n"
                 "    m.has_git_lfs = lambda: False\n"
                 "sys.exit(m.main(sys.argv[3:]))\n")

        def sync_in(w, lang, lfs, *cmd, locpath=None):
            env = {k: v for k, v in os.environ.items() if k not in ("LANG", "LC_ALL", "LC_CTYPE", "LANGUAGE", "LOCPATH")}
            if lang:
                env["LANG"] = lang
            if locpath:
                env["LOCPATH"] = locpath
            r = subprocess.run([sys.executable, "-I", "-c", drive, w.p["script"], lfs, *(cmd or ["sync"])], env=env,
                               capture_output=True, timeout=300)
            out, err = r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
            if lang:                               # the print trap is real only where the language makes it strict
                self.assertIn("stdout errors: strict", err, f"{lang} is not a language on this computer")
            self.assertNotIn("Traceback", err + out, err + out)
            self.assertNotRegex(out, r"\b\w+Error\b", "a Python name in the tool's words")
            return r.returncode, out

        def raw_refs(cmd):
            out = subprocess.run(cmd, capture_output=True, check=True).stdout
            return {r: s for s, r in (line.split(b"\t" if b"\t" in line else b" ", 1) for line in out.split(b"\n")
                                      if line) if not r.endswith(b"^{}") and r != b"HEAD"}

        def gh_raw(w, name):
            return raw_refs(["git", "--git-dir", os.path.join(w.gh.root, "someone", name + ".git"), "for-each-ref",
                             "--format=%(objectname) %(refname)"])

        def forge_raw(w, name):
            key = fread(w.p["mirror_key"]).strip()
            hdr = "Authorization: Basic " + __import__("base64").b64encode(f"mirror:{key}".encode()).decode()
            return raw_refs(["git", "-c", f"http.http://127.0.0.1:{w.port}/.extraheader={hdr}", "ls-remote",
                             f"http://127.0.0.1:{w.port}/mirror/someone--{name}.git"])

        def three(w, first):
            for n in ("aa", "bb", "cc"):
                w.gh.add_repo("someone", n)
            first(w)
            w.gh.commit("someone/bb", {".gitattributes": b"\xf6*.dat -text\r\n", "a": b"1"})     # Latin-1, CR LF, no LFS
            w.gh.commit("someone/cc", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.sync("--announce")[0], 0)
            rc, out = w.go()
            self.assertEqual(rc, 0, out)

        def attrs(w):
            w.gh.commit("someone/aa", {".gitattributes": b"# M\xf6\xdfe\r*.bin filter=lfs diff=lfs merge=lfs -text\r\n",
                                       "a": b"1"})

        # Every character Python's split() and strip() with no argument take for a blank, that git takes in a ref's
        # name (rule 31 (vi)): each in the middle of a branch's name and at the end of a tag's. The same world is also
        # run with "_" in the same places (git takes no control character there), so a name cut or trimmed shows as
        # a difference in the count.
        blanks = [chr(i) for i in range(0x110000) if len(("a" + chr(i) + "b").split()) > 1
                  and subprocess.run(["git", "check-ref-format", "refs/heads/a" + chr(i) + "b"]).returncode == 0]
        self.assertEqual(len(blanks), 19, [hex(ord(c)) for c in blanks])

        def names(w, twin=None):
            w.gh.commit("someone/aa", {"a": b"1"})
            w.gh.commit("someone/aa", {"b": b"2"}, branch="side")
            w.gh.plant("someone/aa", "refs/heads/a\udcffb", "side")
            w.gh.plant("someone/aa", "refs/tags/t\udcc3x", "main")
            w.gh.git("someone/aa", "update-ref", "refs/heads/c" + (twin or "\u0085") + "d" + (twin or "\u2028") + "e",
                     "main")                                                    # UTF-8 that splitlines breaks
            w.gh.commit("someone/bb", {"z": b"0"})       # bb: the same names, in a project nothing else is wrong in
            for c in blanks:
                for n, at in (("aa", "side"), ("bb", "main")):
                    w.gh.git(f"someone/{n}", "update-ref", f"refs/heads/w{ord(c):04x}-a{twin or c}b", at)
                    w.gh.git(f"someone/{n}", "tag", f"v{ord(c):04x}{twin or c}", "main")

        def counted(w, lang, twin=None):
            """Part 2's world: N branches and tags on GitHub end as N - 2 on the forge, the 2 named; never one more,
            never one fewer unnamed."""
            three(w, lambda w: names(w, twin))
            rc, out = sync_in(w, lang, "lfs")
            if not disk_holds(os.path.dirname(w.p["state"]), "a\udcffb"):
                # A disk that cannot hold such a name as a file (seen: a Mac's) cannot fetch it either: the project
                # is a named problem in git's words, never the left-out road, and the projects after it are copied.
                self.assertEqual(rc, 1, out)
                self.assertIn("problem: someone/aa: the fetch from GitHub failed: ", out)
                self.assertNotIn("not UTF-8 text", out)
                for n in ("bb", "cc"):
                    self.assertEqual(forge_raw(w, n), gh_raw(w, n), f"{n} ({lang})")
                rc, said = sync_in(w, lang, "lfs", "status")
                self.assertIn("\ncopies: 3 on GitHub, 2 copied, 1 behind, 0 unreadable\n", said)
                return None
            self.assertEqual(rc, 0, out)
            self.assertIn("problem: someone/aa: " + mirror.NOT_TEXT.format(n=2, names="a?b, tag t?x; " + mirror.SHOWN_CHANGED), out)
            gh = gh_raw(w, "aa")
            self.assertEqual(len(gh), 5 + 2 * len(blanks))
            want = {r: s for r, s in gh.items() if r not in (b"refs/heads/a\xffb", b"refs/tags/t\xc3x")}
            got = forge_raw(w, "aa")
            self.assertEqual(len(got), len(gh) - 2,
                             f"{lang} {'twin' if twin else 'blanks'}: {sorted(set(got) ^ set(want))}")
            self.assertEqual(got, want, lang)
            self.assertEqual(len(gh_raw(w, "bb")), 1 + 2 * len(blanks))
            self.assertEqual(forge_raw(w, "bb"), gh_raw(w, "bb"), lang)
            # what the tool wrote down as GitHub's (status's "current" and the left-out names are read from it)
            recs = {r["full_name"]: r for r in mirror.load_json(w.p["state"], {})["repos"].values()}
            for n, key in (("aa", "seen_refs"), ("bb", "refs")):
                wrote = {r.encode("utf-8", "surrogateescape"): v.encode() for r, v in recs[f"someone/{n}"][key].items()}
                self.assertEqual(wrote, gh_raw(w, n), f"{n}'s {key} ({lang})")
            rc, said = sync_in(w, lang, "lfs", "status")
            self.assertEqual(rc, 1, said)
            self.assertIn("\ncopies: 3 on GitHub, 3 copied, 1 behind, 0 unreadable\n", said)
            return said

        for lang in ("en_US.UTF-8", None):
            # 1. A .gitattributes whose bytes are not UTF-8, on a computer without git lfs: decided on its bytes.
            with World(self) as w:
                three(w, attrs)
                rc, out = sync_in(w, lang, "no-lfs")
                self.assertEqual(rc, 0, out)
                self.assertIn(f"problem: someone/aa: {mirror.LFS_NOT_IN_COPY}", out, "an LFS line read as no LFS")
                self.assertNotIn(f"someone/bb: {mirror.LFS_NOT_IN_COPY}", out)
                for n in ("aa", "bb", "cc"):
                    self.assertEqual(forge_raw(w, n), gh_raw(w, n), f"{n} ({lang})")
            # 2. A branch's and a tag's name that are not UTF-8 (the forge cannot hold them): every other branch
            # and tag is copied, the two are named in the sync's words and in status, and the projects after it
            # are copied whole.
            with World(self) as w:
                out = counted(w, lang)
                if out is None:
                    continue
                self.assertIn(b"refs/heads/c\xc2\x85d\xe2\x80\xa8e", forge_raw(w, "aa"))
                self.assertIn(b"refs/tags/v3000\xe3\x80\x80", forge_raw(w, "aa"))     # a tag ending in a blank
                for n in ("bb", "cc"):
                    self.assertEqual(forge_raw(w, n), gh_raw(w, n), f"{n} ({lang})")
                self.assertIn(mirror.NOT_TEXT.format(n=2, names="a?b, tag t?x; " + mirror.SHOWN_CHANGED), out)   # status
        with World(self) as w:                                  # 2, the twin: "_" in every place a blank was
            counted(w, "en_US.UTF-8", twin="_")
        # 3. A .gitattributes that cannot be read is never taken as no LFS: the project is named, the rest copied.
        with World(self) as w:
            three(w, attrs)
            saved, lfs = mirror.git, mirror.has_git_lfs

            def unread(cache, *args, **kw):
                if args[:2] == ("cat-file", "blob") and args[2].endswith(":.gitattributes"):
                    return subprocess.CompletedProcess(args, 128, "", "fatal: a read that failed")
                return saved(cache, *args, **kw)
            mirror.git, mirror.has_git_lfs = unread, lambda: False
            try:
                rc, out = w.sync()
            finally:
                mirror.git, mirror.has_git_lfs = saved, lfs
            self.assertEqual(rc, 1, out)
            for n in ("aa", "bb"):
                self.assertIn(f"someone/{n}: {mirror.LFS_UNREAD}", out)
            self.assertNotIn(mirror.LFS_NOT_IN_COPY, out)
            self.assertEqual(forge_raw(w, "cc"), gh_raw(w, "cc"))
        # 4. A fault the tool did not foresee, in the first project only: named in plain words; the others copied.
        with World(self) as w:
            three(w, lambda w: w.gh.commit("someone/aa", {"a": b"1"}))
            saved = mirror.place_copy

            def fault(forge, rec, full, gid, report):
                if full == "someone/aa":
                    raise KeyError("id")
                return saved(forge, rec, full, gid, report)
            mirror.place_copy = fault
            try:
                rc, out = w.sync()
            finally:
                mirror.place_copy = saved
            self.assertEqual(rc, 1, out)
            self.assertIn(f"someone/aa: {mirror.UNFORESEEN}", out)
            self.assertNotIn("KeyError", out)
            for n in ("bb", "cc"):
                self.assertEqual(forge_raw(w, n), gh_raw(w, n), n)
        # 5. The forge refuses a push for a reason the tool did not foresee (Gitea 28.4.0's own words, seen when it
        # refused a name): the project is named with the forge's words, and the others are copied.
        with World(self) as w:
            three(w, lambda w: w.gh.commit("someone/aa", {"a": b"1"}))
            saved = mirror.forge_call
            gitea_said = ("remote: \nremote: error:        \nremote: error: Internal Server Error (no message for end "
                          "users)        \nremote: error:        \nTo http://127.0.0.1:1/mirror/someone--aa.git\n"
                          " ! [remote rejected] main -> main (pre-receive hook declined)\n"
                          "error: failed to push some refs to 'http://127.0.0.1:1/mirror/someone--aa.git'\n")

            def refused(forge, cache, *args, **kw):
                if args[:1] == ("push",) and "someone--aa" in fread(os.path.join(cache, "config")):
                    return subprocess.CompletedProcess(args, 1, "", gitea_said)
                return saved(forge, cache, *args, **kw)
            mirror.forge_call = refused
            try:
                rc, out = w.sync()
            finally:
                mirror.forge_call = saved
            self.assertEqual(rc, 1, out)
            self.assertIn("someone/aa: the push into the forge failed, so nothing in its copy was moved (the push is "
                          "all or nothing): Internal Server Error (no message for end users); pre-receive hook declined",
                          out)
            for n in ("bb", "cc"):
                self.assertEqual(forge_raw(w, n), gh_raw(w, n), n)
        # 6. restore --to with a .gitattributes that cannot be read: never taken as no LFS; nothing is pushed.
        with World(self) as w:
            three(w, attrs)
            self.assertEqual(w.sync()[0], 0)
            root = tempfile.mkdtemp(prefix="wsm-home-")
            TEMPS.append(root)
            home = bare_home(root, "bb")
            saved = mirror.git

            def unread(cache, *args, **kw):
                if args[:2] == ("cat-file", "blob") and args[2].endswith(":.gitattributes"):
                    return subprocess.CompletedProcess(args, 128, "", "fatal: a read that failed")
                return saved(cache, *args, **kw)
            mirror.git = unread
            try:
                rc, out = quiet(mirror.main, ["restore", "--to", home, "someone/bb"])
            finally:
                mirror.git = saved
            self.assertEqual(rc, 1, out)
            self.assertIn("Whether someone/bb keeps files with Git LFS could not be read from its .gitattributes, so "
                          "nothing was pushed.", out)
            self.assertEqual(lsremote(home), {})
        # 7. A tag at a file (a blob), an annotated one, a tag at a tree and a tag of a tag (git's own project has a
        # tag at a file): by its kind it has no top .gitattributes, which is a no, never "could not be read"; with
        # git lfs and without, the project is copied whole and restore --to brings it all back.
        def odd_tags(w):
            w.gh.commit("someone/aa", {"a": b"1"})
            blob = w.gh.git("someone/aa", "rev-parse", "main:a").strip()
            tree = w.gh.git("someone/aa", "rev-parse", "main^{tree}").strip()
            me = ["-c", "user.name=a", "-c", "user.email=a@example.invalid"]
            w.gh.git("someone/aa", "tag", "a-key-file", blob)
            w.gh.git("someone/aa", *me, "tag", "-a", "-m", "a file", "an-annotated-file", blob)
            w.gh.git("someone/aa", "tag", "a-tree", tree)
            w.gh.git("someone/aa", *me, "tag", "-a", "-m", "of a tag", "a-tag-of-a-tag", "an-annotated-file")

        for lfs in ("no-lfs", "lfs"):
            with World(self) as w:
                three(w, odd_tags)
                rc, out = sync_in(w, "en_US.UTF-8", lfs)
                self.assertEqual(rc, 0, f"{lfs}: {out}")
                self.assertEqual(len(gh_raw(w, "aa")), 5)
                for n in ("aa", "bb", "cc"):
                    self.assertEqual(forge_raw(w, n), gh_raw(w, n), f"{n} ({lfs})")
                root = tempfile.mkdtemp(prefix="wsm-home-")
                TEMPS.append(root)
                home = bare_home(root, "aa")
                rc, out = sync_in(w, "en_US.UTF-8", lfs, "restore", "--to", home, "someone/aa")
                self.assertEqual(rc, 0, f"{lfs}: {out}")
                self.assertEqual(raw_refs(["git", "ls-remote", home]), gh_raw(w, "aa"), lfs)
        # 8. What a left-out name costs, against a control with a plain name in the same place, three syncs: the copy
        # is refreshed and said with its limit at every sync and in status; the sync is good, its full pass is one,
        # the others are unchanged the next time, and no line says "not refreshed" or "cannot refresh" of it.
        for label, name in (("planted", "refs/heads/a\udcffb"), ("control", "refs/heads/a_b")):
            with World(self) as w:
                three(w, lambda w: (w.gh.commit("someone/aa", {"a": b"1"}), w.gh.plant("someone/aa", name, "main")))
                self.assertIn(name.encode("utf-8", "surrogateescape"), gh_raw(w, "aa"))        # the premise
                time.sleep(1.1)                        # pushed_at has whole seconds: a push in a fetch's own second is fetched again
                limit = "problem: someone/aa: " + mirror.NOT_TEXT.format(n=1, names="a?b; " + mirror.SHOWN_CHANGED)
                if label == "planted" and not disk_holds(os.path.dirname(w.p["state"]), "a\udcffb"):
                    # A disk that cannot hold the name (seen: a Mac's): a named problem at every sync, in git's
                    # words, never the left-out road, and the others copied (M123's guard, M124's cure).
                    for i in (1, 2):
                        rc, out = sync_in(w, "en_US.UTF-8", "lfs")
                        self.assertEqual(rc, 1, out)
                        self.assertIn("problem: someone/aa: the fetch from GitHub failed: ", out)
                        self.assertNotIn(limit, out)
                        for n in ("bb", "cc"):
                            self.assertEqual(forge_raw(w, n), gh_raw(w, n), n)
                    continue
                for i in (1, 2, 3):
                    rc, out = sync_in(w, "en_US.UTF-8", "lfs")
                    self.assertEqual(rc, 0, f"{label} {i}: {out}")
                    line = next(x for x in out.split("\n") if x.startswith("sync: "))
                    self.assertIn("3 copied now, 0 unchanged" if i == 1 else "0 copied now, 3 unchanged", line, label)
                    self.assertEqual(", a full pass" in line, i == 1, line)
                    self.assertEqual(limit in out, label == "planted", out)
                    self.assertEqual("1 copy without branch or tag names the forge cannot hold" in line,
                                     label == "planted", line)
                    self.assertNotRegex(out, r"not refreshed|cannot refresh", out)
                    st = mirror.load_json(w.p["state"], {})
                    for k in ("first_copy_done", "last_good_sync", "last_full_pass"):
                        self.assertTrue(st.get(k), f"{label} {i}: {k}")
                rc, out = sync_in(w, "en_US.UTF-8", "lfs", "status")
                self.assertNotRegex(out, r"not refreshed|cannot refresh|last good sync: none yet", out)
                self.assertIn(f"\ncopies: 3 on GitHub, 3 copied, {1 if label == 'planted' else 0} behind, 0 unreadable", out)
                self.assertEqual(f"problem: someone/aa: {mirror.NOT_TEXT.format(n=1, names='a?b; ' + mirror.SHOWN_CHANGED)}"
                                 in out, label == "planted", out)
                for n in ("bb", "cc"):
                    self.assertEqual(forge_raw(w, n), gh_raw(w, n), f"{n} ({label})")
                if label == "planted":
                    time.sleep(1.1)                    # pushed_at has whole seconds
                    w.gh.plant("someone/aa", name)
                    rc, out = sync_in(w, "en_US.UTF-8", "lfs")
                    self.assertEqual(rc, 0, out)
                    self.assertNotIn("not UTF-8 text", out)
                    self.assertEqual(forge_raw(w, "aa"), gh_raw(w, "aa"))
                    rc, out = sync_in(w, "en_US.UTF-8", "lfs", "status")
                    self.assertIn("\ncopies: 3 on GitHub, 3 copied, 0 behind, 0 unreadable", out)
        # 9. A program's words with a carriage return, shown to a person: cut there too, never handed to the screen.
        out = mirror.run(["sh", "-c", "printf 'fatal: bad\\rsudo id #\\n' >&2; exit 1"])
        self.assertEqual(out.stderr, "fatal: bad\rsudo id #\n")                       # the premise: the CR was read
        self.assertEqual(mirror.git_reason(out.stderr), "fatal: bad")
        self.assertEqual(mirror.last_line(out.stderr), "sudo id #")
        out = mirror.run(["sh", "-c", "printf 'remote: error: no\\rsudo id #\\n ! [remote rejected] main -> main (x\\ry)\\n' >&2;"
                                      " exit 1"])
        self.assertIn("\r", out.stderr)
        self.assertEqual(mirror.forge_refusal(out.stderr), "no")
        for f in (mirror.git_reason, mirror.last_line, mirror.forge_refusal):
            self.assertNotIn("\r", f(out.stderr), f.__name__)
        self.assertEqual(mirror.lines_of("a\rb\nc"), ["a\rb", "c"])                     # a record keeps its CR
        # 10. A language that is not UTF-8 (en_US.ISO-8859-1, made into a folder of the test's own): a branch and a
        # tag named with an e-acute go back to git as the bytes read, and are copied, with git lfs and without; a
        # status in the other language reads what was written down as the same names.
        loc = tempfile.mkdtemp(prefix="wsm-loc-")
        TEMPS.append(loc)
        made = shutil.which("localedef") and subprocess.run(
            ["localedef", "-i", "en_US", "-f", "ISO-8859-1", os.path.join(loc, "en_US.ISO-8859-1")],
            capture_output=True).returncode in (0, 1)
        probe = subprocess.run([sys.executable, "-I", "-c", "import sys; print(sys.getfilesystemencoding())"],
                               env=dict({k: v for k, v in os.environ.items() if not k.startswith(("LC_", "LANG"))},
                                        LANG="en_US.ISO-8859-1", LOCPATH=loc), capture_output=True, text=True).stdout
        if probe.strip() != "iso8859-1":
            if sys.platform.startswith("linux") and os.environ.get("WSM_REQUIRE_GITEA"):
                raise AssertionError(f"a Latin-1 language could not be made here (localedef: {made}): {probe!r}")
            print(f"\nG3.15: part 10 is not run here (no Latin-1 language for Python: {probe.strip()!r}); a Linux CI "
                  "job runs it", file=sys.stderr)
            return

        def latin(w):
            for n, e in (("aa", "é"), ("bb", "e")):
                w.gh.commit(f"someone/{n}", {"a": b"1"})
                w.gh.git(f"someone/{n}", "update-ref", f"refs/heads/caf{e}", "main")
                w.gh.git(f"someone/{n}", "tag", f"v{e}", "main")

        for lfs in ("lfs", "no-lfs"):
            with World(self) as w:
                three(w, latin)
                self.assertIn(b"refs/heads/caf\xc3\xa9", gh_raw(w, "aa"))                 # the premise
                rc, out = sync_in(w, "en_US.ISO-8859-1", lfs, locpath=loc)
                self.assertEqual(rc, 0, f"{lfs}: {out}")
                for n in ("aa", "bb", "cc"):
                    self.assertEqual(forge_raw(w, n), gh_raw(w, n), f"{n} ({lfs})")
                    self.assertEqual(len(forge_raw(w, n)), 1 if n == "cc" else 3, n)
                for lang, lp in (("en_US.UTF-8", None), ("en_US.ISO-8859-1", loc)):
                    rc, out = sync_in(w, lang, lfs, "status", locpath=lp)
                    self.assertIn("\ncopies: 3 on GitHub, 3 copied, 0 behind, 0 unreadable", out, f"{lfs} {lang}")
                rc, out = sync_in(w, "en_US.UTF-8", lfs)
                self.assertEqual(rc, 0, f"{lfs}: {out}")
                rc, out = sync_in(w, "en_US.ISO-8859-1", lfs, "restore", "--check", locpath=loc)
                self.assertEqual(rc, 0, f"{lfs}: {out}")

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
            self.assertEqual(w.go()[0], 0)                       # the person's go, for a and b

            def refused(why, counts):
                rc, out = w.sync()
                self.assertEqual(rc, 2, f"{why}: {out}")
                self.assertIn(f"Needs you: the first copy would not be what the person was told ({counts} than the "
                              "last announcement). Nothing was copied.", out)
                self.assertEqual(w.forge_get("/user/repos")[1], [], f"{why}: copied after a change")
                self.assertNotIn("go", mirror.load_json(w.p["state"], {}), f"{why}: a go recorded")
                self.assertTrue(out.rstrip().endswith("When it says their go is kept, run: sync"), out)
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
            # The person's go was for the old list: the agent's sync alone copies nothing.
            rc, out = w.sync()
            self.assertEqual(rc, 2, out)
            self.assertIn("it has not been given for this list", out)
            self.assertEqual(w.forge_get("/user/repos")[1], [])
            # The go to the list as it was last said copies exactly that list.
            self.assertEqual(w.go()[0], 0)
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
    def test_G3_19_a_settings_file_that_cannot_be_read_is_never_taken_as_empty(self):
        """M151 (G3.19; seen 2026-11-25 and 2026-11-26 in stand-in trials in made homes): config and key set wrote over
        a settings file they could not read; sync, left-out and go said "No GitHub key is kept yet" with the key
        there; status said nothing. Every form, before every command that reads it: never taken as empty, never
        written over, GitHub never asked; the stop names the file, where the read stopped and one step, and the
        step taken as printed ends with the include list chosen again, the key kept again, and a sync whole."""
        with World(self) as w:
            for n in ("a", "b"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"f": b"1"})
            self.assertEqual(w.key_set()[0], 0)
            self.assertEqual(quiet(mirror.main, ["config", "include", "someone/a"])[0], 0)
            self.assertEqual(w.first_sync()[0], 0)
            p = w.p
            kept_file_trial(self, w, p["settings"], b'{"owners": ["someone"]}\n')
            plant_kept(p["settings"], "cut short", fread_bytes(p["settings"]), None)
            rc, out = run_kept_cmd(w, ["sync"])
            self.assertEqual(rc, 2, out)
            step = out.split("The one next step: ", 1)[1]
            inc, ks = mirror.own_command(p, "config", "include"), mirror.own_command(p, "key", "set", "someone")
            self.assertIn(f"move that file out of {os.path.dirname(p['settings'])}", step)
            self.assertIn(inc, step)
            self.assertIn(ks, step)
            kept = os.path.join(w.home.tmp, "kept-by-the-person")
            os.makedirs(kept)
            os.rename(p["settings"], os.path.join(kept, "settings.json"))         # the step, as printed
            self.assertEqual(run_printed(w, inc + " someone/a")[0], 0)
            rc, out = run_printed(w, ks, stdin=w.gh.key + "\n")
            self.assertEqual(rc, 0, out)
            w.gh.commit("someone/a", {"f": b"2"})
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertEqual(w.forge_refs("someone--a").get("refs/heads/main"),
                             w.gh.git("someone/a", "rev-parse", "main").strip(), out)
            self.assertEqual(quiet(mirror.main, ["status"])[0], 0)

    def test_G3_16_a_copy_is_whole_only_as_githubs_own_list_shows_it(self):
        """M123: a project is copied, and its copy whole, only when every branch and tag in GitHub's own list is in
        the copy at the commit GitHub lists. Seen on a Mac (2026-11-24): a disk that folds case kept Feature at
        feature's commit and lost feature, and the sync, status and restore --check all called the copy whole. Where
        a name is not as listed, the project is a named problem, the forge keeps what it held, and the exit is 1;
        a list that could not be read is said so and nothing is called wrong; a name that moved or went on GitHub
        during the fetch is no problem; and where the disk keeps names apart the guard says nothing."""
        drive = ("import importlib.util, json, os, subprocess, sys\n"
                 "s = importlib.util.spec_from_file_location('mirror', sys.argv[1]); m = importlib.util.module_from_spec(s)\n"
                 "s.loader.exec_module(m)\n"
                 "inj = json.loads(os.environ.get('WSM_INJ') or '{}')\n"
                 "n = {'ls': 0}\n"
                 "real, real_forge = m.github_call, m.forge_call\n"
                 "def g(*a, **k):\n"
                 "    mine = 'ls-remote' in a and a[1].endswith('/someone/aa.git')\n"
                 "    if 'ls-remote' in a:\n"
                 "        open(os.environ['WSM_INJ_COUNT'], 'a').write(a[1].rsplit('/', 1)[-1] + '\\n')\n"
                 "    if mine:\n"
                 "        n['ls'] += 1\n"
                 "        if n['ls'] in inj.get('unread', []):\n"
                 "            return subprocess.CompletedProcess(a, 128, '', 'fatal: unable to access (planted)')\n"
                 "    out = real(*a, **k)\n"
                 "    if mine and n['ls'] == 1 and inj.get('move'):\n"
                 "        bare, ref, sha = inj['move']\n"
                 "        subprocess.run(['git', '--git-dir', bare, 'update-ref'] + (['-d', ref] if not sha else [ref, sha]), check=True)\n"
                 "    if 'fetch' in a and 'github' in a and inj.get('fold'):\n"
                 "        cache = a[0]\n"
                 "        lines = subprocess.run(['git', '--git-dir', cache, 'for-each-ref', '--format=%(refname) %(objectname)',\n"
                 "                                'refs/heads', 'refs/tags'], capture_output=True, check=True).stdout.split(b'\\n')\n"
                 "        groups = {}\n"
                 "        for x in lines:\n"
                 "            if x:\n"
                 "                r, sha = x.rsplit(b' ', 1); groups.setdefault(r.lower(), []).append((r, sha))\n"
                 "        for grp in groups.values():\n"
                 "            if len(grp) > 1:\n"
                 "                grp.sort()\n"
                 "                subprocess.run([b'git', b'--git-dir', os.fsencode(cache), b'update-ref', grp[0][0], grp[-1][1]], check=True)\n"
                 "                for r, _ in grp[1:]:\n"
                 "                    subprocess.run([b'git', b'--git-dir', os.fsencode(cache), b'update-ref', b'-d', r], check=True)\n"
                 "    return out\n"
                 "def f(forge, cache, *a, **k):\n"
                 "    out = real_forge(forge, cache, *a, **k)\n"
                 "    if a[:1] == ('push',) and inj.get('forge_drop'):\n"
                 "        real_forge(forge, cache, 'push', '--quiet', 'forge', ':' + inj['forge_drop'])\n"
                 "    return out\n"
                 "m.github_call, m.forge_call = g, f\n"
                 "sys.exit(m.main(sys.argv[2:]))\n")

        def run_(w, *cmd, project="aa", **inj):
            count = os.path.join(w.gh_root, "listings")
            open(count, "w").close()
            env = dict(os.environ, WSM_INJ=json.dumps(inj), WSM_INJ_COUNT=count)
            r = subprocess.run([sys.executable, "-I", "-c", drive, w.p["script"], *(cmd or ["sync"])], env=env,
                               capture_output=True, timeout=300)
            out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
            self.assertNotIn("Traceback", out, out)
            return r.returncode, out, open(count).read().split().count(project + ".git")

        def raw(cmd):
            out = subprocess.run(cmd, capture_output=True)
            if out.returncode != 0:
                return None
            return {r: s for s, r in (x.split(b"\t" if b"\t" in x else b" ", 1) for x in out.stdout.split(b"\n") if x)
                    if not r.endswith(b"^{}") and r != b"HEAD" and not r.startswith(b"refs/heads/kept/")}

        def gh_raw(w, n):
            return raw(["git", "--git-dir", os.path.join(w.gh.root, "someone", n + ".git"), "for-each-ref",
                        "--format=%(objectname) %(refname)", "refs/heads", "refs/tags"])

        def forge_raw(w, n):
            key = fread(w.p["mirror_key"]).strip()
            hdr = "Authorization: Basic " + __import__("base64").b64encode(f"mirror:{key}".encode()).decode()
            return raw(["git", "-c", f"http.http://127.0.0.1:{w.port}/.extraheader={hdr}", "ls-remote", "--heads",
                        "--tags", f"http://127.0.0.1:{w.port}/mirror/someone--{n}.git"])

        def folds(a, b):
            return disk_folds(os.path.dirname(w.p["state"]), a, b)

        def holds(name):
            return disk_holds(os.path.dirname(w.p["state"]), name)

        def world(w, pairs=()):
            for n in ("aa", "bb", "cc"):
                w.gh.add_repo("someone", n)
            c = [w.gh.commit("someone/aa", {"a": str(i).encode()}) for i in (1, 2, 3)]
            for i, ref in enumerate(pairs):
                w.gh.plant("someone/aa", ref, c[i % 2])
            w.gh.commit("someone/bb", {"a": b"1"})
            w.gh.commit("someone/cc", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.sync("--announce")[0], 0)
            rc, out = w.go()
            self.assertEqual(rc, 0, out)
            for ref in pairs:                                       # the premise: GitHub holds every name, apart
                self.assertIn(os.fsencode(ref), gh_raw(w, "aa"))
            return c

        case = ["refs/heads/Feature", "refs/heads/feature", "refs/tags/V1", "refs/tags/v1"]
        case_line = mirror.NOT_AS_LISTED.format(n=4, names="Feature: at another commit, feature: missing, "
                                                "tag V1: at another commit, tag v1: missing")

        def not_moved(w, rc, out, before, listings, n=2):
            self.assertEqual(rc, 1, out)
            self.assertIn("problem: someone/aa: " + case_line, out)
            self.assertEqual(listings, n, out)
            self.assertEqual(forge_raw(w, "aa"), before, "the forge was given a set GitHub does not list")
            for x in ("bb", "cc"):
                self.assertEqual(forge_raw(w, x), gh_raw(w, x), x)

        # 1. A wrong cache on a first copy (made here as a disk that folds case makes it): a named problem at every
        # sync and in status, the forge given nothing of it, restore --check not whole; right again once it is right.
        with World(self) as w:
            world(w, case)
            for i in (1, 2):
                rc, out, ls = run_(w, fold=True)
                not_moved(w, rc, out, None, ls)
            rc, out, _ = run_(w, "status")
            self.assertIn(case_line, out)
            self.assertIn("\ncopies: 3 on GitHub, 2 copied, 1 behind", out)
            rc, out, _ = run_(w, "restore", "--check")
            self.assertNotIn("3 whole", out)
            if not folds("Feature", "feature"):
                rc, out, ls = run_(w)
                self.assertEqual(rc, 0, out)
                self.assertNotIn(case_line, out)
                self.assertEqual(forge_raw(w, "aa"), gh_raw(w, "aa"))
                rc, out, _ = run_(w, "restore", "--check")
                self.assertIn("3 copies, 3 whole, 0 difference(s)", out)
        # 2. The same after a good copy: what the forge held before stays byte for byte, and what GitHub showed is
        # written down from GitHub's own list, so restore --check names what the copy lacks.
        with World(self) as w:
            world(w)
            rc, out, ls = run_(w)
            self.assertEqual((rc, ls), (0, 1), out)
            before = forge_raw(w, "aa")
            gid = str(w.gh.repos["someone/aa"]["id"])
            self.assertEqual({os.fsencode(r): s.encode() for r, s in mirror.load_json(w.p["state"], {})["repos"][gid]["seen_refs"].items()},
                             gh_raw(w, "aa"))
            time.sleep(1.1)                                         # pushed_at has whole seconds
            for i, ref in enumerate(case):
                w.gh.plant("someone/aa", ref, "main~" + str(i % 2))
            rc, out, ls = run_(w, fold=True)
            not_moved(w, rc, out, before, ls)
            rc, out, _ = run_(w, "restore", "--check")
            self.assertIn("someone/aa: refs/heads/feature is missing in the copy", out)
        # 3. The three worlds a Mac's disk was seen to fold or refuse, with nothing planted in the tool: where the disk
        # keeps the names apart, every name is at its own commit and the guard says nothing; where it folds case, the
        # project is the named problem; where it folds two spellings or cannot hold a name, the fetch says so.
        for label, pairs, a, b in (("case", case, "Feature", "feature"),
                                   ("spelling", ["refs/heads/caf\u00e9", "refs/heads/cafe\u0301"], "caf\u00e9", "cafe\u0301"),
                                   ("not UTF-8", ["refs/heads/a\udcffb", "refs/heads/a_b"], None, None)):
            with World(self) as w:
                world(w, pairs)
                folded = folds(a, b) if a else not holds("a\udcffb")
                rc, out, ls = run_(w)
                if not folded:
                    self.assertEqual((rc, ls), (0, 1), f"{label}: {out}")
                    self.assertNotIn("would not be what GitHub lists", out)
                    held = forge_raw(w, "aa")
                    for ref in pairs:
                        want = gh_raw(w, "aa")[os.fsencode(ref)]
                        self.assertEqual(held.get(os.fsencode(ref)), None if mirror.not_text(ref) else want, label)
                elif label == "case":
                    not_moved(w, rc, out, None, ls)
                else:
                    self.assertEqual(rc, 1, out)
                    self.assertIn("problem: someone/aa: the fetch from GitHub failed: ", out)
                    self.assertIsNone(forge_raw(w, "aa"), label)
        # 4. A name that moved, and one that went, on GitHub between the first list and the fetch: no problem, a
        # second list read once, and the copy as GitHub holds it after; beside a control that reads one list.
        for label, change in (("moved", lambda w, bare: [bare, "refs/heads/side", w.gh.git("someone/aa", "rev-parse", "main~1").strip()]),
                              ("went", lambda w, bare: [bare, "refs/heads/side", ""]),
                              ("control", None)):
            with World(self) as w:
                world(w)
                rc, out, ls = run_(w)
                self.assertEqual((rc, ls), (0, 1), out)
                time.sleep(1.1)
                w.gh.commit("someone/aa", {"b": b"1"})
                w.gh.git("someone/aa", "branch", "side", "main")
                bare = os.path.join(w.gh.root, "someone", "aa.git")
                rc, out, ls = run_(w, **({"move": change(w, bare)} if change else {}))
                self.assertEqual((rc, ls), (0, 2 if change else 1), f"{label}: {out}")
                self.assertEqual(forge_raw(w, "aa"), gh_raw(w, "aa"), label)
                self.assertEqual(b"refs/heads/side" in forge_raw(w, "aa"), label != "went", label)
        # 5. A list that could not be read (both tries), the first and then the second: said so, nothing called wrong,
        # the forge given nothing, the other projects copied; beside a control with the same wrong cache and both
        # lists read.
        for label, inj in (("first", {"unread": [1, 2]}), ("second", {"unread": [2, 3], "fold": True}),
                           ("control", {"fold": True})):
            with World(self) as w:
                world(w, case)
                rc, out, ls = run_(w, **inj)
                self.assertEqual((rc, ls), (1, {"first": 2, "second": 3, "control": 2}[label]), out)
                self.assertEqual("problem: someone/aa: " + mirror.LISTING_UNREAD.format(why="fatal: unable to access (planted)")
                                 in out, label != "control", out)
                self.assertEqual("would not be what GitHub lists" in out, label == "control", out)
                self.assertIsNone(forge_raw(w, "aa"), label)
                for x in ("bb", "cc"):
                    self.assertEqual(forge_raw(w, x), gh_raw(w, x), x)
        # 6. What the list does not hold is not compared (the fetch asks for branches and tags only): a note on
        # GitHub is no problem; and a project with no branch and no tag is a list read with none, never unread.
        with World(self) as w:
            world(w)
            w.gh.git("someone/aa", "update-ref", "refs/notes/commits", "main")
            w.gh.add_repo("someone", "dd")
            self.assertIn(b"refs/notes/commits", raw(["git", "--git-dir", os.path.join(w.gh.root, "someone", "aa.git"),
                                                      "for-each-ref", "--format=%(objectname) %(refname)"]))
            self.assertEqual(gh_raw(w, "dd"), {})
            w.key_set()
            self.assertEqual(w.sync("--announce")[0], 0)
            self.assertEqual(w.go()[0], 0)
            rc, out, ls = run_(w)
            self.assertEqual((rc, ls), (0, 1), out)
            time.sleep(1.1)
            w.gh.commit("someone/aa", {"b": b"2"})
            w.gh.touch("someone/dd")
            rc, out, ls = run_(w, project="dd")
            self.assertEqual((rc, ls), (0, 1), out)
            self.assertNotIn("could not be read", out)
            self.assertEqual(forge_raw(w, "aa"), gh_raw(w, "aa"))
        # 7. A project skipped as unchanged reads no list.
        with World(self) as w:
            world(w)
            time.sleep(1.1)                                         # a push in a fetch's own second is fetched again
            self.assertEqual(run_(w)[0], 0)
            rc, out, ls = run_(w)
            self.assertEqual((rc, ls), (0, 0), out)
            self.assertIn("0 copied now, 3 unchanged", out)
        # 8. The copy is the forge, read back after the push: a branch gone from it then is a named problem.
        with World(self) as w:
            world(w)
            w.gh.git("someone/aa", "branch", "side", "main")
            rc, out, ls = run_(w, forge_drop="refs/heads/side")
            self.assertEqual(rc, 1, out)
            self.assertIn("problem: someone/aa: " + mirror.FORGE_NOT_AS_LISTED.format(n=1, names="side: missing"), out)
            rc, out, ls = run_(w)
            self.assertEqual(rc, 0, out)
            self.assertEqual(forge_raw(w, "aa"), gh_raw(w, "aa"))
        # 9. A GitHub branch named kept/...: the read-back leaves refs/heads/kept/ out on both sides, so the first copy
        # is whole and not called wrong (the name is checked in the cache against GitHub's list). Its later syncs are
        # row M127's, due 0.1.1.
        with World(self) as w:
            for n in ("aa", "bb", "cc"):
                w.gh.add_repo("someone", n)
                w.gh.commit("someone/" + n, {"a": b"1"})
            w.gh.git("someone/aa", "branch", "kept/2020-01-01/old", "main")
            w.key_set()
            self.assertEqual(w.sync("--announce")[0], 0)
            rc, out = w.go()
            self.assertEqual(rc, 0, out)
            rc, out = w.sync()                                       # the first copy
            self.assertEqual(rc, 0, out)
            self.assertNotIn("not what GitHub lists", out)
            self.assertEqual(forge_raw(w, "aa"), gh_raw(w, "aa"))
            self.assertEqual(w.forge_refs("someone--aa").get("refs/heads/kept/2020-01-01/old"),
                             w.gh_refs("someone/aa")["refs/heads/main"])
            st = mirror.load_json(w.p["state"], {})["repos"]
            aa = next(r for r in st.values() if r.get("full_name") == "someone/aa")
            self.assertEqual(aa.get("problem"), "", aa)
            self.assertIn("refs/heads/kept/2020-01-01/old", aa["refs"])


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

    def test_G2_7_a_fine_grained_key_for_one_project_of_an_organisation_is_set_copied_and_proved(self):
        """Row M99: the one road 0.1.0 gives a person, with a key as GitHub's docs describe it: fine-grained
        (github_pat_ and 82 more, no X-OAuth-Scopes header), read-only, an organisation as its resource owner, one
        project selected, an end date in its header. key set keeps it; the announcement names that project and the
        key's end; the first copy follows the person's go; restore --check proves it from the forge alone; the key
        is in its own file and nowhere else. This is the stand-in's idea of GitHub's answers: the row is seen
        against the real GitHub, with a real key of this kind, before its version is tagged."""
        pat = "github_pat_" + "11ABCDEFG0" + "Zz9_" * 18
        self.assertEqual(len(pat), len("github_pat_") + 82)
        self.assertTrue(mirror.KEY_SHAPE.search(pat), "the key scan does not know a fine-grained key's shape")
        end = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(time.time() + 30 * 86400))
        tell = re.compile(r"^tell the person \d+ of \d+: (.*)$", re.M)
        with World(self) as w:
            w.gh.key, w.gh.scopes, w.gh.expires = pat, None, end
            w.gh.add_repo("theorg", "proof", owner_type="Organization")
            w.gh.commit("theorg/proof", {"a": b"1"})
            quiet(mirror.main, ["config", "include", "theorg/proof"])
            rc, out = w.key_set("theorg")
            self.assertEqual(rc, 0, out)
            self.assertIn("The key for theorg (organisation) is kept", out)
            rc, out = w.sync("--announce")
            self.assertEqual(rc, 0, out)
            said = tell.findall(out)
            self.assertTrue(any(x.startswith("The first copy: 1 repositories from 1 owner(s)") for x in said), said)
            self.assertTrue(any("They are: theorg/proof." in x for x in said), said)
            self.assertIn(f"The key for theorg ends {end}; the mirror warns you two weeks before.", said)
            self.assertEqual(w.go()[0], 0)
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertEqual(w.forge_refs("theorg--proof"), w.gh_refs("theorg/proof"))
            rc, out = quiet(mirror.main, ["restore", "--check"])
            self.assertEqual(rc, 0, out)
            self.assertIn("restore check: 1 copies, 1 whole, 0 difference(s), 0 could not be read", out)
            rc, out = status()
            self.assertEqual(rc, 0, out)
            self.assertIn(f"key for theorg: expires {end}", out)
            self.assertRegex(out, r"key scan: \d+ file\(s\) read, no key found")
            hits, read = files_holding(w.home.tmp, pat)
            self.assertGreater(read, 0, "the scan read no files")
            self.assertEqual(hits, [mirror.key_path(w.p, "theorg")], "the key was written somewhere else")
            self.assertEqual(w.gh.writes(), [], "something was written to GitHub")


def put_settings(p, **kw):
    s = mirror.load_json(p["settings"], {})
    s.update(kw)
    mirror.write_file(p["settings"], json.dumps(s), 0o600)


def git_calls(gh, full=None):
    return [x for x in gh.log if ".git/" in x[1] and (full is None or x[1].startswith(f"/{full}.git/"))]


# Rule 6 (M151, M154, M157): each damaged form of a kept JSON file, planted alone; "cannot be opened" only where the
# account is not root (root reads mode 000), and a folder by the file's name stands in for it there too.
KEPT_FORMS = ("cannot be opened", "0 bytes", "blank space", "cut short", "not JSON", "not text", "a JSON list",
              "a folder", "an entry of the wrong kind")


def plant_kept(path, form, whole, wrong):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.chmod(path, 0o600)
        os.remove(path)
    if form == "a folder":
        os.makedirs(path)
        return
    data = {"cannot be opened": whole, "0 bytes": b"", "blank space": b" \n\n", "cut short": whole[:len(whole) // 2],
            "not JSON": b"owners = someone\n", "not text": b"\xff\xfe{\x00", "a JSON list": b'["someone"]\n',
            "an entry of the wrong kind": wrong}[form]
    with open(path, "wb") as f:
        f.write(data)
    if form == "cannot be opened":
        os.chmod(path, 0)


def kept_mark(path):
    """What a planted file is, read without opening it: its kind, mode, size and time."""
    st = os.lstat(path)
    return stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_size, st.st_mtime_ns


def run_kept_cmd(w, cmd):
    saved = sys.stdin
    sys.stdin = AtTerminal("\n") if cmd == ["go"] else io.StringIO(w.gh.key + "\n" if cmd[:2] == ["key", "set"] else "")
    try:
        return quiet(mirror.main, cmd)
    finally:
        sys.stdin = saved


def run_printed(w, line, stdin=""):
    """A command as the tool printed it, run as a person pastes it: its words after `python3 -I <the copy>`."""
    argv = shlex.split(line)
    assert argv[:2] == ["python3", "-I"] and os.path.exists(argv[2]), line
    saved = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        return quiet(mirror.main, argv[3:])
    finally:
        sys.stdin = saved


KEPT_COMMANDS = (["status"], ["left-out"], ["restore", "--check"], ["sync"], ["config", "include", "someone/a"],
                 ["key", "set", "someone"], ["go"], ["setup"])


def made_home_tree(h, p):
    """M158: the whole made home, each path with its bytes' SHA-256 (or "dir", or "link"), its service files and
    the folders around them aside."""
    svc = os.path.dirname(mirror.service_files(p)[0])
    out = {}
    for d, ds, fs in os.walk(h.tmp):
        for x in ds + fs:
            q = os.path.join(d, x)
            if q == svc or mirror.inside(q, svc) or mirror.inside(svc, q):
                continue
            if os.path.islink(q):
                out[q] = "link"
            elif os.path.isdir(q):
                out[q] = "dir"
            else:
                try:
                    out[q] = mirror.sha256_of(q)
                except OSError as e:
                    out[q] = f"could not be read ({type(e).__name__}) {stat.S_IMODE(os.lstat(q).st_mode):o}"
    return out


def kept_file_trial(tc, w, path, wrong):
    """Every form before every command that reads the file: never a traceback, never taken as empty or written
    over, GitHub never asked, and the stop (status: a problem line) names the file, where the read stopped and one
    step."""
    whole = fread_bytes(path)
    forms = [f for f in KEPT_FORMS if f != "cannot be opened" or os.geteuid() != 0]
    for form in forms:
        for cmd in KEPT_COMMANDS:
            with tc.subTest(form=form, command=" ".join(cmd)):
                plant_kept(path, form, whole, wrong)
                mark, n = kept_mark(path), len(w.gh.log)
                rc, out = run_kept_cmd(w, cmd)
                tc.assertEqual(kept_mark(path), mark, f"the file was changed: {out}")
                tc.assertEqual(len(w.gh.log), n, "GitHub was asked")
                tc.assertNotIn("No GitHub key is kept yet", out)
                tc.assertNotIn("Run setup first", out)
                tc.assertNotIn("Setup comes first", out)
                head = "problem: " if cmd == ["status"] else "Stopped: "
                tc.assertIn(f"{head}{path} could not be read: ", out)
                tc.assertEqual(rc, 1 if cmd == ["status"] else 2, out)
                tc.assertIn("The one next step: move that file out of", out)
                why = out.split("could not be read: ", 1)[1].split("\n")[0]
                for planted in ("owners = someone", '["someone"]', "\\xff", "\ufffd"):
                    tc.assertNotIn(planted, why, "a value of the file was printed")
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.chmod(path, 0o600)
        os.remove(path)
    with open(path, "wb") as f:
        f.write(whole)


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
                self.assertIn(" ".join(mirror.sd_word(w) for w in (p["script"], "sync", "--from-timer")), unit)   # G1.24
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
            # The first read of GitHub is its list of branches and tags (M123): that is the slow one, said with why.
            slow = mirror.LISTING_UNREAD.format(why="it took longer than 2 s, and so did the retry")
            self.assertIn("problem: someone/alpha: " + slow, out)
            self.assertIn("2 on GitHub, 1 copied now", out)
            self.assertEqual(len([x for x in git_calls(w.gh, "someone/alpha") if "info/refs" in x[1]]), 2,
                             "the slow fetch was not tried exactly twice")
            self.assertEqual(w.forge_refs("someone--beta"), w.gh_refs("someone/beta"))
            st = mirror.load_json(w.p["state"], {})
            alpha = next(r for r in st["repos"].values() if r["full_name"] == "someone/alpha")
            self.assertEqual(alpha["problem"], "cannot refresh: " + slow)
            # When it is quick again, the next sync copies it.
            w.gh.slow.clear()
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertEqual(w.forge_refs("someone--alpha"), w.gh_refs("someone/alpha"))
        # The list quick and the fetch slow: the fetch's own words, tried exactly twice, and the rest copied.
        with World(self) as w:
            for n in ("alpha", "beta"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"a": b"1"})
            w.key_set()
            put_settings(w.p, git_timeout_s=2)
            w.gh.slow_fetch["someone/alpha"] = 5
            rc, out = w.first_sync()
            self.assertEqual(rc, 1, out)
            slow = "the fetch from GitHub took longer than 2 s, and so did the retry"
            self.assertIn("problem: someone/alpha: " + slow, out)
            self.assertNotIn("could not be read", out)
            self.assertIn("2 on GitHub, 1 copied now", out)
            self.assertEqual(w.gh.fetches.count("someone/alpha"), 2, "the slow fetch was not tried exactly twice")
            self.assertEqual(w.forge_refs("someone--beta"), w.gh_refs("someone/beta"))
            st = mirror.load_json(w.p["state"], {})
            alpha = next(r for r in st["repos"].values() if r["full_name"] == "someone/alpha")
            self.assertEqual(alpha["problem"], "cannot refresh: " + slow)

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
            self.assertEqual(rc, 0, out)               # G4.8: below the floor the sync waits for the reset, and says
            st = mirror.load_json(w.p["state"], {})    # what it is, never "spent" (a real machine was told so, M9)
            m = re.search(r"GitHub's allowance for your keys is down to (\d+), under the floor of 500 this skill keeps, "
                          r"so the sync waits until (.+?) and asks GitHub nothing before it", out)
            self.assertTrue(m, out)
            self.assertLess(int(m.group(1)), 500)     # what GitHub said was left (the stand-in counts down per call)
            self.assertEqual(m.group(2), mirror.local_time(st["github_wait_until"]))
            self.assertNotIn("spent", out)
            self.assertNotIn("spent", quiet(mirror.main, ["status"])[1])
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
            st["release_check"] = {}                   # its daily read is due: still, nothing is asked in the wait
            mirror.save_state(w.p, st)
            asked = len(w.gh.log)
            out = quiet(mirror.main, ["status"])[1]
            self.assertEqual(len(w.gh.log), asked, "status asked GitHub during the wait")
            self.assertIn("newer release: not asked while the sync waits for GitHub's reset", out)
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


    def test_G4_10_two_writes_of_one_kept_file_at_once_never_tear_it(self):
        """M161 (seen 2026-11-26 in a stand-in trial in a made home, the two writes forced into one order inside one
        process): with one temporary name for every writer, a second write run whole at the first one's rename took
        that name away, and the first ended in a traceback; the record's second write, run inside the first,
        left a record that could not be read. Each write is made under its own temporary name and put in place
        whole: no traceback, and the file holds one writer's bytes, whole. Both writers (write_file, the record's
        write_made), each forced order (the second run whole right after the first made its temporary file, and
        at the first's rename)."""
        with Home():
            p = mirror.paths()
            mirror.write_file(os.path.join(p["data"], "first-note"), "x\n")
            rf = os.path.join(p["data"], mirror.MADE_RECORD)
            base, why = mirror.read_made(p)
            self.assertIsNotNone(base, why)
            writers = {
                "settings": (p["settings"], lambda tag: mirror.write_file(
                    p["settings"], json.dumps({"include": [tag]}, indent=1) + "\n", 0o600),
                    lambda tag: json.dumps({"include": [tag]}, indent=1) + "\n"),
                "record": (rf, lambda tag: mirror.write_made(p, dict(base, **{os.path.join(p["data"], tag): "file"})),
                           lambda tag: json.dumps({"written_by": "windstorm-mirror", "made": dict(
                               base, **{os.path.join(p["data"], tag): "file"})}, indent=1)),
                # two setups at once (the owner's Q4, 11-26): each puts the skill's own copy in place
                "the installed copy": (p["script"], lambda tag: mirror.copy_whole(srcs[tag], p["script"]),
                                       lambda tag: f"# the copy of {tag}\n"),
            }
            srcs = {}
            for tag in ("first-writer", "second-writer"):
                srcs[tag] = os.path.join(mirror.paths()["data"], "..", f"source-{tag}.py")
                with open(srcs[tag], "w") as fh:
                    fh.write(f"# the copy of {tag}\n")
            for name, (f, write, text_of) in writers.items():
                for at in ("made", "rename"):
                    with self.subTest(file=name, second_writer_at=at):
                        real_mk, real_rep, box = tempfile.mkstemp, os.replace, {"n": 0}

                        def second():
                            box["n"] += 1
                            tempfile.mkstemp, os.replace = real_mk, real_rep
                            write("second-writer")

                        def mk(*a, **kw):
                            got = real_mk(*a, **kw)
                            if at == "made" and box["n"] == 0:
                                second()
                            return got

                        def rep(src, dst):
                            if at == "rename" and dst == f and box["n"] == 0:
                                second()
                            return real_rep(src, dst)
                        tempfile.mkstemp, os.replace = mk, rep
                        try:
                            write("first-writer")        # no traceback: an error fails the test here
                        finally:
                            tempfile.mkstemp, os.replace = real_mk, real_rep
                        self.assertEqual(box["n"], 1, "the second writer was not run inside the first")
                        self.assertEqual(fread(f), text_of("first-writer"),
                                         "the file does not hold one writer's bytes, whole (the first renamed last)")
                        left = [x for x in os.listdir(os.path.dirname(f)) if x.startswith(os.path.basename(f) + ".")]
                        self.assertEqual(left, [], "a temporary file was left beside it")
                        if name == "record":
                            self.assertIsNotNone(mirror.read_made(p)[0])

    def test_G4_11_an_entry_noted_in_the_record_is_never_lost_to_another_commands_write(self):
        """M162 (seen 2026-11-26 in a stand-in trial in a made home): claim() read the record, then wrote it, with no
        lock; a second command's claim run whole between the two was written over, and its folder was then not the
        skill's to setup. The record is read and written under one lock: another process's claim, started while
        this one is between its read and its write, waits, and both entries are kept."""
        with Home():
            p = mirror.paths()
            mirror.write_file(os.path.join(p["data"], "first-note"), "x\n")
            ours, theirs = os.path.join(p["data"], "noted-here"), os.path.join(p["data"], "noted-by-another")
            for f in (ours, theirs):
                with open(f, "w") as fh:
                    fh.write("x\n")
            code = (f"import sys; sys.path.insert(0, {os.path.dirname(SCRIPT)!r}); import mirror; "
                    f"mirror.claim({theirs!r})")
            real, box = mirror.read_made, {}

            def read_then_another(p_):
                got = real(p_)
                if "pr" not in box:
                    box["pr"] = subprocess.Popen([sys.executable, "-I", "-c", code], env=dict(os.environ))
                    try:
                        box["pr"].wait(3)
                        box["ended_between"] = True
                    except subprocess.TimeoutExpired:
                        box["ended_between"] = False
                return got
            mirror.read_made = read_then_another
            try:
                mirror.claim(ours)
            finally:
                mirror.read_made = real
            self.assertEqual(box["pr"].wait(30), 0)
            self.assertFalse(box["ended_between"], "the other command's claim ran whole between this one's read and "
                             "its write")
            rec, why = mirror.read_made(p)
            self.assertIsNotNone(rec, why)
            self.assertEqual((rec.get(ours), rec.get(theirs)), ("file", "file"), "an entry was lost")
            self.assertEqual(mirror.not_ours(p), [])

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

    def test_G5_6_a_tag_deleted_on_github_leaves_the_copy_its_commit_kept(self):
        """M125 (seen 2026-11-25 in a reviewer's world): a tag deleted on GitHub stayed in the copy for ever, because
        the fetch from the forge followed tags and wrote the forge's tag back into the cache."""
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            one = w.gh.commit("someone/alpha", {"a": b"1"})
            w.gh.commit("someone/alpha", {"a": b"2"})
            w.gh.git("someone/alpha", "tag", "t1", one)
            w.gh.git("someone/alpha", "tag", "t2")
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            self.assertEqual(w.forge_refs("someone--alpha")["refs/tags/t1"], one)       # the premise: it was copied
            day = time.strftime("%Y-%m-%d", time.gmtime())
            w.gh.git("someone/alpha", "tag", "-d", "t1")
            tags = lambda refs: {r: v for r, v in refs.items() if r.startswith("refs/tags/")}
            want = tags(w.gh_refs("someone/alpha"))           # read once: a read of the stand-in moves its pushed_at
            w.gh.repos["someone/alpha"]["pushed_at"] = "2002-01-01T00:00:00Z"   # GitHub's time moved (not to the second)
            fetched = lambda: len(git_calls(w.gh, "someone/alpha"))     # its git calls to GitHub (a skip makes none)
            # The sync after the delete, then one with nothing changed on GitHub but its time (so the project is
            # fetched and pushed again, not skipped as unchanged), then one skipped as unchanged.
            for step in ("after the delete", "nothing changed", "unchanged"):
                if step == "nothing changed":
                    w.gh.repos["someone/alpha"]["pushed_at"] = "2001-01-01T00:00:00Z"
                was = fetched()
                rc, out = w.sync()
                self.assertEqual(rc, 0, f"{step}: {out}")
                self.assertEqual(fetched() == was, step == "unchanged", f"{step}: fetched again or not")
                refs = w.forge_refs("someone--alpha")
                self.assertEqual(tags(refs), want, step)
                self.assertEqual(sorted(r for r in refs if "/kept/" in r), [f"refs/heads/kept/{day}/tags/t1"], step)
                self.assertEqual(refs[f"refs/heads/kept/{day}/tags/t1"], one, step)
                said = f"someone/alpha: tags/t1 was deleted on GitHub; its old commit is kept as kept/{day}/tags/t1"
                (self.assertIn if step == "after the delete" else self.assertNotIn)(said, out)
            rc, out = status()
            self.assertIn("\ncopies: 1 on GitHub, 1 copied, 0 behind", out)
            rc, out = quiet(mirror.main, ["restore", "--check"])
            self.assertEqual(rc, 0, out)
            self.assertIn("1 copies, 1 whole, 0 difference(s)", out)

    def test_G5_7_an_annotated_tag_moved_or_deleted_keeps_its_commit(self):
        """M126 (seen 2026-11-25 in a reviewer's world): an annotated tag moved on GitHub ended its project's refresh
        for good, because the tag object, not its commit, was pushed to a kept branch, which git refuses."""
        with World(self) as w:
            full = "someone/alpha"
            w.gh.add_repo("someone", "alpha")
            one = w.gh.commit(full, {"a": b"1"})
            two = w.gh.commit(full, {"a": b"2"})
            blob = w.gh.git(full, "rev-parse", "main:a").strip()
            me = ["-c", "user.name=a", "-c", "user.email=a@example.invalid"]
            for name, at in (("ta", one), ("tam", one), ("tfile", blob)):
                w.gh.git(full, *me, "tag", "-a", "-m", f"the tag {name}", name, at)
            w.gh.git(full, *me, "tag", "-a", "-m", "a tag of a tag", "ttag", "ta")
            w.gh.git(full, "tag", "plain", one)                                     # a plain tag that stays
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            tags = lambda refs: {r: v for r, v in refs.items() if r.startswith("refs/tags/")}
            self.assertEqual(tags(w.forge_refs("someone--alpha")), tags(w.gh_refs(full)))   # the premise
            day = time.strftime("%Y-%m-%d", time.gmtime())
            # On GitHub: an annotated tag deleted, one moved, a tag of a tag deleted, a tag at a file deleted.
            w.gh.git(full, "tag", "-d", "ta")
            w.gh.git(full, *me, "tag", "-a", "-f", "-m", "the tag tam, moved", "tam", two)
            w.gh.git(full, "tag", "-d", "ttag")
            w.gh.git(full, "tag", "-d", "tfile")
            want = tags(w.gh_refs(full))                      # read once: a read of the stand-in moves its pushed_at
            w.gh.repos[full]["pushed_at"] = "2002-01-01T00:00:00Z"              # GitHub's time moved (not to the second)
            fetched = lambda: len(git_calls(w.gh, "someone/alpha"))     # its git calls to GitHub (a skip makes none)
            for step in ("after the change", "nothing changed", "unchanged"):
                if step == "nothing changed":
                    w.gh.repos[full]["pushed_at"] = "2001-01-01T00:00:00Z"
                was = fetched()
                rc, out = w.sync()
                self.assertEqual(rc, 0, f"{step}: {out}")
                self.assertEqual(fetched() == was, step == "unchanged", f"{step}: fetched again or not")
                refs = w.forge_refs("someone--alpha")
                self.assertEqual(tags(refs), want, step)
                kept = {r: v for r, v in refs.items() if "/kept/" in r}
                self.assertEqual(kept, {f"refs/heads/kept/{day}/tags/{n}": one for n in ("ta", "tam", "ttag")}, step)
                lines = [f"someone/alpha: tags/{n} was {what} on GitHub; its old commit is kept as kept/{day}/tags/{n}"
                         for n, what in (("ta", "deleted"), ("tam", "moved"), ("ttag", "deleted"))]
                lines.append(mirror.TAG_NOT_KEPT.format(full=full, short="tags/tfile", what="deleted", at="a file"))
                for line in lines:
                    (self.assertIn if step == "after the change" else self.assertNotIn)(line, out)
            rc, out = status()
            self.assertIn("\ncopies: 1 on GitHub, 1 copied, 0 behind", out)
            rc, out = quiet(mirror.main, ["restore", "--check"])
            self.assertEqual(rc, 0, out)
            self.assertIn("1 copies, 1 whole, 0 difference(s)", out)
            # tam moved again: a read of its kind that failed is never taken for a no (G5.4), so the copy is not moved
            # on that pass; the next pass keeps its commit under a name of its own.
            w.gh.git(full, *me, "tag", "-a", "-f", "-m", "the tag tam, moved back", "tam", one)
            w.gh.repos[full]["pushed_at"] = "2003-01-01T00:00:00Z"
            saved, before = mirror.git, w.forge_refs("someone--alpha")

            def unread(cache, *args, **kw):
                if args[:2] == ("cat-file", "-t") and "^" not in args[2]:      # keep_before_moving's read only
                    return subprocess.CompletedProcess(args, 128, "", "fatal: a read that failed")
                return saved(cache, *args, **kw)
            mirror.git = unread
            try:
                rc, out = w.sync()
            finally:
                mirror.git = saved
            self.assertEqual(rc, 1, out)
            self.assertIn("problem: someone/alpha: an old commit could not be kept, so the copy was not moved", out)
            self.assertEqual(w.forge_refs("someone--alpha"), before, "the copy moved on a read that failed")
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertIn(f"someone/alpha: tags/tam was moved on GitHub; its old commit is kept as kept/{day}/tags/tam-2", out)
            refs = w.forge_refs("someone--alpha")
            self.assertEqual(tags(refs), tags(w.gh_refs(full)))
            self.assertEqual(refs[f"refs/heads/kept/{day}/tags/tam-2"], two)

    def test_G5_9_a_github_branch_named_kept_is_said_in_plain_words(self):
        """M129 (a reviewer's world 16, 2026-11-25): with a branch named kept/... on GitHub, every sync after something
        changed there ended on git's own "dst ref ... receives from more than one src", with no next step, and the
        copy's main stayed behind GitHub's. Now the sync says what it is, before anything is pushed, and its one next
        step, run, gives a sync that is good again. The cure is row M127, due 0.1.1."""
        with World(self) as w:
            for n, b in (("alpha", "kept/foo"), ("beta", "kept/2020-01-01/old"), ("gamma", None)):
                w.gh.add_repo("someone", n)
                one = w.gh.commit(f"someone/{n}", {"a": b"1"})
                if b:
                    w.gh.git(f"someone/{n}", "branch", b, one)
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)                       # this version copies such a project once
            before = {n: w.forge_refs(f"someone--{n}") for n in ("alpha", "beta")}
            for n in ("alpha", "beta", "gamma"):                         # then something changes on GitHub
                w.gh.commit(f"someone/{n}", {"a": b"2"})
                w.gh.repos[f"someone/{n}"]["pushed_at"] = "2002-01-01T00:00:00Z"
            for _ in (1, 2):                                             # every sync after that, not by timing
                rc, out = w.sync()
                self.assertEqual(rc, 1, out)
                for n, b in (("alpha", "kept/foo"), ("beta", "kept/2020-01-01/old")):
                    self.assertIn(f"problem: someone/{n}: " + mirror.KEPT_CLASH.format(n=1, names=b), out)
                    self.assertEqual(w.forge_refs(f"someone--{n}"), before[n], "the copy moved")
                self.assertNotIn("receives from more than one src", out)
                self.assertEqual(w.forge_refs("someone--gamma")["refs/heads/main"],
                                 w.gh_refs("someone/gamma")["refs/heads/main"])     # the control follows
            rc, out = status()
            self.assertIn("cannot refresh: " + mirror.KEPT_CLASH.format(n=1, names="kept/foo"), out)
            # The one next step, taken: the branch gets another name on GitHub, and the next sync is good.
            w.gh.git("someone/alpha", "branch", "-m", "kept/foo", "foo")
            w.gh.repos["someone/alpha"]["pushed_at"] = "2003-01-01T00:00:00Z"
            rc, out = w.sync()
            self.assertNotIn("problem: someone/alpha", out)
            refs, gh = w.forge_refs("someone--alpha"), w.gh_refs("someone/alpha")
            self.assertEqual({r: v for r, v in refs.items() if not r.startswith("refs/heads/kept/")}, gh)


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
            b_good = next(r for r in st["repos"].values() if r["full_name"] == "someone/b")["last_good"]
            self.assertTrue(b_good, st)              # b had a good refresh, so its line says since when first (G6.2)
            self.assertTrue(any(x.startswith(f"problem: someone/b: not refreshed since {b_good}; cannot refresh: GitHub's "
                                             "list of its branches and tags could not be read (") for x in problems), out)
            self.assertIn("problem: someone/c: the copy's branches and tags differ from what GitHub last showed", problems)

    def test_G6_2_a_refused_key_reads_as_not_refreshed_since_the_last_good_one(self):
        """The skill's name promises a current copy (the maintainer's condition for 0.1.0, 2026-11-23): when a refresh
        fails because GitHub refuses the key, expired, revoked or narrowed, status and the sync say the copy is not
        refreshed since its last good refresh, and why; never current, and the last good sync stays where it was.
        GitHub refuses with a 401 (a key that stopped) or a 403 that is not the allowance; both are taken here."""
        refusals = {"a key changed on GitHub (401)": None,
                    "a 403 that is not the allowance": (403, {}, {"message": "Resource not accessible by personal access token"}),
                    "a 401 for an expired key": (401, {}, {"message": "Bad credentials"})}
        for why, answer in refusals.items():
            with World(self) as w:
                w.gh.add_repo("someone", "alpha")
                w.gh.commit("someone/alpha", {"a": b"1"})
                w.key_set()
                self.assertEqual(w.first_sync()[0], 0, why)
                rc, out = status()
                self.assertEqual(rc, 0, out)
                self.assertIn("copies: 1 on GitHub, 1 copied, 0 behind, 0 unreadable", out)
                st = mirror.load_json(w.p["state"], {})
                good, good_sync = next(iter(st["repos"].values()))["last_good"], st["last_good_sync"]
                time.sleep(1.1)
                real = mirror.gh_get
                if answer is None:
                    w.gh.key = "ghkey-" + "d4" * 10                      # the key stops working on GitHub
                else:
                    mirror.gh_get = lambda key, path, a=answer: a
                try:
                    rc, out = w.sync()
                    self.assertEqual(rc, 1, (why, out))
                    self.assertRegex(out, r"problem: someone: (GitHub refused the key for someone \((401)\)|the listing of "
                                          r"someone failed at page 1 \(GitHub answered 403\))", why)
                    self.assertIn("sync: 0 on GitHub, 0 copied now, 0 unchanged, 1 problem(s)", out)
                    rc, out = status()
                finally:
                    mirror.gh_get = real
                self.assertEqual(rc, 1, (why, out))
                self.assertIn("copies: 1 on GitHub, 1 copied, 1 behind, 0 unreadable", out, why)
                line = [x for x in out.splitlines() if x.startswith("problem: someone/alpha: ")]
                self.assertEqual(len(line), 1, (why, out))
                self.assertTrue(line[0].startswith(f"problem: someone/alpha: not refreshed since {good}; cannot refresh: "),
                                (why, line[0]))
                self.assertIn(f"last good sync: {good_sync}", out, why)
                self.assertNotIn("0 behind", out, why)
                st = mirror.load_json(w.p["state"], {})
                self.assertEqual(st["last_good_sync"], good_sync, why)
                self.assertEqual(next(iter(st["repos"].values()))["last_good"], good, why)
                self.assertFalse(mirror.copy_state(next(iter(st["repos"].values())), w.forge_refs("someone--alpha"), 10 ** 9)[0])

    def test_G6_2_a_reason_that_ends_in_a_command_ends_its_status_line_with_it(self):
        """The family's rule 31 (2026-11-24, found on reading the strings commit): status says a copy that stopped
        for a git setting with the time of its last good refresh; the command its reason names stands alone on the
        line after it and ends that line, so a shell given the line to its end runs nothing after the command. Both
        reasons: a line for the project's whole address, and a setting git cannot read."""
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            good = next(iter(mirror.load_json(w.p["state"], {})["repos"].values()))["last_good"]
            base = os.environ["WSM_GITHUB_GIT"].rstrip("/")
            host = urllib.parse.urlsplit(base).hostname
            gc = os.path.join(os.environ["HOME"], ".gitconfig")
            for n, (form, cmd) in enumerate((
                    (f'[url "ssh://git@{host}:1/someone/alpha.git"]\n\tinsteadOf = {base}/someone/alpha.git\n',
                     "git config --global --get-regexp insteadof"),
                    ("[url\n", "git config --global --list"))):
                w.gh.commit("someone/alpha", {"n": str(n).encode()})
                with open(gc, "w") as f:
                    f.write(form)
                try:
                    rc, out = w.sync()
                    self.assertNotEqual(rc, 0, out)
                finally:
                    os.remove(gc)             # status prints the reason the sync stored (a setting git cannot read
                rc, out = status()            # would stop status's own read of the forge, a line with no command)
                self.assertEqual(rc, 1, out)
                lines = out.splitlines()
                at = [i for i, x in enumerate(lines) if x.startswith("problem: someone/alpha: ")]
                self.assertEqual(len(at), 1, out)
                self.assertTrue(lines[at[0]].startswith(f"problem: someone/alpha: not refreshed since {good}; "), out)
                self.assertTrue(lines[at[0]].endswith(":"), lines[at[0]])
                self.assertEqual(lines[at[0] + 1], "    " + cmd, out)

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

    def test_G6_12_no_printed_line_carries_a_backtick_or_a_dollar_parenthesis(self):
        """The family's rule 31 (2026-11-24): a line the person is asked to report can reach a shell inside double
        quotes, where a backticked command, or one in a dollar-parenthesis, is run and its answer lands in the text
        that is sent. So no string the tool can print carries either; a whole command stands alone on a line of
        its own. Read over every string constant in the script (docstrings aside), f-strings' parts too."""
        import ast
        tree = ast.parse(fread(SCRIPT))
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        found = sorted((n.lineno, n.value[:90]) for n in ast.walk(tree)
                       if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
                       and ("`" in n.value or "$(" in n.value))
        self.assertEqual(found, [], f"{len(found)} printed string(s) carry a backtick or a dollar-parenthesis")

    def test_G6_12_a_printed_command_runs_as_printed_where_the_path_holds_a_space(self):
        """M60 (a Mac test account, 2026-11-23): the tool printed `python3 -I /Users/.../Application Support/
        windstorm-mirror/mirror.py uninstall --delete-copies`, which a shell splits at the space. Every command the
        tool prints with its own copy's path runs as printed: the path is quoted."""
        with Home() as h:
            root = os.path.join(h.tmp, "Application Support")
            os.environ["WSM_HOME"] = root
            p = mirror.paths()
            self.assertIn(" ", p["script"])
            os.makedirs(os.path.dirname(p["script"]), exist_ok=True)     # the skill's own copy is there (M144)
            shutil.copyfile(SCRIPT, p["script"])
            for args in (("uninstall",), ("uninstall", "--delete-copies"), ("status",)):
                argv = shlex.split(mirror.own_command(p, *args))
                self.assertEqual(argv, ["python3", "-I", p["script"], *args])
            saved = sys.stdin
            sys.stdin = io.StringIO("")
            try:
                rc, out = quiet(mirror.main, ["uninstall", "--delete-copies"])
            finally:
                sys.stdin = saved
            self.assertEqual(rc, 2, out)
            cmd = out.split("in a terminal window of their own, they run ", 1)[1].split(" and type the number")[0]
            self.assertEqual(shlex.split(cmd), ["python3", "-I", p["script"], "uninstall", "--delete-copies"])
            self.assertIn("command=own_command(p, \"uninstall\")", inspect.getsource(mirror.cmd_status))
            self.assertIn("own_command(paths(), a.cmd)", inspect.getsource(mirror.main))
            self.assertNotIn("{script}", mirror.PLUGIN_GONE)

    def test_G6_12_every_printed_command_names_a_copy_of_the_tool_that_is_there(self):
        """M144 (seen 2026-11-25 on a Mac, macOS 27.2.1): after uninstall had taken the skill's own copy away,
        `uninstall --delete-copies` with no terminal printed a line for the person that named that copy, so it could
        not run. Every command line the tool prints is tried in the state it was printed in, with the skill's own copy
        there and with it gone: the file it names is there when it is printed. While the own copy is there, every
        line names it, as before; once it is gone, the line names the copy that runs, and run as printed in a
        terminal it asks for the number of copies; the Kept line names a way with no copy of the tool left."""
        printed = []
        real_say = mirror.say

        def say(msg=""):
            for m in re.finditer(r"python3 -I ('[^']*'|[^ ]+)", str(msg)):
                path = shlex.split(m.group(1))[0]
                printed.append((path, os.path.exists(path), str(msg)[:100]))
            return real_say(msg)
        saved = sys.stdin
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            mirror.say = say
            try:
                sys.stdin = io.StringIO("")                           # no terminal, as an agent runs it
                for argv in (["status"], ["go"], ["uninstall", "--delete-copies"]):
                    mirror.DOOR_SEEN.clear()
                    quiet(mirror.main, argv)
                self.assertGreaterEqual(len(printed), 3, printed)
                self.assertEqual({x[0] for x in printed}, {w.p["script"]}, "the own copy, as before")
                self.assertTrue(all(x[1] for x in printed), printed)
                del printed[:]
                rc, out = quiet(mirror.main, ["uninstall"])
                self.assertEqual(rc, 0, out)
                self.assertFalse(os.path.exists(w.p["script"]))
                kept = [x for x in out.splitlines() if x.startswith("Kept: the folder of copies")]
                rc, out = quiet(mirror.main, ["uninstall", "--delete-copies"])
                self.assertEqual(rc, 2, out)
                self.assertTrue(printed, out)
                self.assertEqual([x for x in printed if not x[1]], [], "a printed line names a file that is not there")
                self.assertEqual(len(kept), 1, kept)
                self.assertIn("once no copy of this skill is left on this computer, as you delete any folder of yours",
                              kept[0])
                cmd = out.split("in a terminal window of their own, they run ", 1)[1].split(" and type the number")[0]
                argv = shlex.split(cmd)
                self.assertEqual(argv[:3], ["python3", "-I", os.path.abspath(mirror.__file__)])
                self.assertEqual(argv[3:], ["uninstall", "--delete-copies"])
                sys.stdin = AtTerminal("7\n")                        # run as printed, at a terminal (not 0)
                rc, out = quiet(mirror.main, argv[3:])
                self.assertEqual(rc, 1, out)
                self.assertIn("The copies were not deleted.", out)
                self.assertTrue(os.path.exists(w.p["data"]))
            finally:
                mirror.say = real_say
                sys.stdin = saved
        # The maintainer's finding on this row's first commit (2026-11-25): uninstall run from the skill's OWN copy, the
        # copy every printed line names, reads that copy gone once it has taken it, and the copy that runs is that same
        # path, so no copy of the tool was read there. The Kept line said "no copy of this skill is left on this
        # computer", which the tool had not read (a clone or the plugin's folder can still be there). It says what was
        # read and no more: the own copy is gone now, and another copy only as an "if". "Run from the own copy" is stood
        # in for by one thing: mirror.__file__, the path the tool reads as the copy that runs, is set to the own copy's
        # path before uninstall; the tree's own scripts/mirror.py stays on the disk.
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            running = mirror.__file__
            mirror.__file__ = w.p["script"]
            try:
                rc, out = quiet(mirror.main, ["uninstall"])
            finally:
                mirror.__file__ = running
            self.assertEqual(rc, 0, out)
            self.assertFalse(os.path.exists(w.p["script"]))
            self.assertTrue(os.path.exists(running), "the tree's own copy stays on the disk, unread by uninstall")
            kept = [x for x in out.splitlines() if x.startswith("Kept: the folder of copies")]
            self.assertEqual(len(kept), 1, out)
            self.assertNotIn("no copy of this skill is left", kept[0])
            self.assertIn("delete it only when you are sure, as you delete any folder of yours; the skill's own copy is "
                          "gone now, and if another copy of this skill is on this computer, its uninstall "
                          "--delete-copies does it, in a terminal window of your own. Setup run again later", kept[0])

    def test_G6_11_a_reader_that_stops_reading_never_gets_a_traceback(self):
        """A real machine (2026-11-23): `status | head -3` ended in a Python traceback (a broken pipe). The tool is
        run with its output going to a pipe nobody reads: it ends with its own code, says nothing on stderr, and
        does the whole of its work (setup --dry-run reads this computer to its end and changes nothing). Its own
        code is the one the same run gives with a reader: where setup's floors stop it (G8.7, G1.24: a shell with
        no user bus), that is 2 both ways; past them, 0."""
        home = tempfile.mkdtemp(prefix="wsm-pipe-")
        TEMPS.append(home)
        read = subprocess.run([sys.executable, "-I", SCRIPT, "setup", "--dry-run"], capture_output=True, text=True,
                              timeout=120, env=dict(os.environ, WSM_HOME=home))
        self.assertIn(read.returncode, (0, 2), read.stdout + read.stderr)
        if read.returncode == 2:            # 2 only from a floor's own line, read here as the tool reads it
            saved_home = os.environ.get("WSM_HOME")
            os.environ["WSM_HOME"] = home
            try:
                p = mirror.paths()
                p["script"] = SCRIPT
                floors = {mirror.bus_floor(p)} | {(mirror.door_floor() or "") + " Setup made nothing."}
            finally:
                if saved_home is None:
                    os.environ.pop("WSM_HOME")
                else:
                    os.environ["WSM_HOME"] = saved_home
            self.assertIn(read.stdout.strip().splitlines()[-1], floors, read.stdout)
        self.assertEqual(read.stderr, "")
        self.assertEqual(os.listdir(home), [])
        r, w = os.pipe()
        os.close(r)                                   # the reader is gone before the first word
        try:
            pr = subprocess.run([sys.executable, "-I", SCRIPT, "setup", "--dry-run"], stdout=w, stderr=subprocess.PIPE,
                                text=True, timeout=120, env=dict(os.environ, WSM_HOME=home))
        finally:
            os.close(w)
        self.assertEqual(pr.returncode, read.returncode, pr.stderr)
        self.assertEqual(pr.stderr, "")
        self.assertEqual(os.listdir(home), [])

    def test_G3_12_the_announcement_asks_the_forge_first(self):
        """M59 (a Mac test account, 2026-11-23): with the forge's service not started, `sync --announce` announced a
        first copy that could not then be made. Nothing is announced that cannot then be done: the announcement asks
        the forge first, as the sync does; when it does not answer, it says so with its next step, asks GitHub
        nothing and records nothing; once the forge answers, the announcement is made as before."""
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            pr = w.home.procs[-1]
            pr.terminate()
            pr.wait(60)
            asked = len(w.gh.log)
            rc, out = w.sync("--announce")
            self.assertEqual(rc, 1, out)
            self.assertIn(f"problem: the forge does not answer on 127.0.0.1:{w.port}, so the first copy is not "
                          "announced: it could not be made now. Nothing was changed, and GitHub was not asked.", out)
            self.assertIn("run status", out)
            self.assertNotIn("tell the person", out)
            self.assertEqual(len(w.gh.log), asked, "GitHub was asked")
            st = mirror.load_json(w.p["state"], {})
            self.assertFalse(st.get("announced") or st.get("announced_list"), "an announcement was recorded")
            w.home.start_forge()
            rc, out = w.sync("--announce")
            self.assertEqual(rc, 0, out)
            self.assertIn("tell the person 1 of", out)

    def test_G6_10_status_ends_the_forge_does_not_answer_line_with_its_step_taken_as_printed(self):
        """M105 (the family's rule 7; seen 2026-11-24 on a stock virtual machine, the service's python3 taken away):
        status's problem line for a forge that does not answer ends with its one step, setup again by its own command
        line. The step is taken exactly as printed, with no reset between: then status answers 0, the forge answers
        and every copy is kept."""
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            w.home.forge_follows = True
            pr = w.home.procs[-1]
            pr.terminate()
            pr.wait(60)
            rc, out = status()
            self.assertEqual(rc, 1, out)
            step = mirror.own_command(w.p, "setup")
            self.assertIn(f"problem: the forge does not answer on 127.0.0.1:{w.port}; run setup again, which starts it "
                          f"and keeps every copy: {step}", out.splitlines())
            argv = shlex.split(step)
            self.assertEqual(argv[:3], ["python3", "-I", w.p["script"]], "the step names the skill's own copy")
            rc, said = quiet(mirror.main, argv[3:])
            self.assertEqual(rc, 0, said)
            mirror.DOOR_SEEN.clear()                # status is a process of its own; setup's door read is not reused
            rc, out = status()
            self.assertEqual(rc, 0, out)
            self.assertNotIn("does not answer", out)
            self.assertIn("next sync: " + ("every 15 min" if mirror.system()[0] == "darwin" else w.home.timer_next), out)
            self.assertEqual(w.forge_refs("someone--alpha"), w.gh_refs("someone/alpha"), "every copy kept")

    def test_G6_10_next_sync_is_no_promise_while_the_forge_does_not_answer(self):
        """M105 (seen 2026-11-24 on a stock virtual machine: with the forge's service failing, status still said
        "next sync: <time>" as if a sync would run there): while the forge does not answer, the line says the time a
        sync is tried and that it cannot work until the forge answers; once it answers, the line is the time alone."""
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            when = "every 15 min (launchd keeps no next-run time)" if mirror.system()[0] == "darwin" else w.home.timer_next
            def next_sync(out):
                lines = [x for x in out.splitlines() if x.startswith("next sync: ")]
                self.assertEqual(len(lines), 1, out)
                return lines[0]
            up = next_sync(status()[1])
            self.assertTrue(up.startswith(f"next sync: {when}") and "cannot work" not in up, up)
            pr = w.home.procs[-1]
            pr.terminate()
            pr.wait(60)
            mirror.DOOR_SEEN.clear()
            rc, out = status()
            self.assertEqual(rc, 1, out)
            down = next_sync(out)
            # The time is still to come: "will be tried at" (the owner's word, 11-24); a Mac's line names no time.
            self.assertEqual(down, f"next sync: {'' if mirror.system()[0] == 'darwin' else 'will be tried at '}{when}; "
                                   "it cannot work until the forge answers")

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
            self.assertEqual(w.go()[0], 0)
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
            self.assertIn(f"problem: the key for someone expires {near}, in 5 day(s): make a new one, then run key set someone", out)
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
            self.assertIn(f"newer release of this skill: v0.3.0 (this is {mirror.VERSION}); self-update is your choice", out)
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


    def test_G6_21_a_kept_json_file_has_three_answers_and_never_a_value_in_its_words(self):
        """M154 (G6.21; seen 2026-11-26 in a stand-in trial in a made home): a settings or state file whose top was a
        JSON list ended five commands in a traceback. One reader, three answers: read, not there, could not be read;
        the why names the error's class or where the parser stopped, never a byte or a value; a top that is not an
        object, or an entry of a kind the tool never writes there, is "could not be read"."""
        with Home() as h:
            f = os.path.join(h.tmp, "kept.json")
            self.assertEqual(mirror.kept_read(f), ("not there", None))
            whole = b'{"owners": {"someone": {"kind": "user"}}, "include": ["someone/a"]}\n'
            for form in KEPT_FORMS:
                if form == "cannot be opened" and os.geteuid() == 0:
                    continue
                with self.subTest(form=form):
                    plant_kept(f, form, whole, b'{"owners": ["someone"]}\n')
                    answer, why = mirror.kept_read(f)
                    if form == "an entry of the wrong kind":
                        self.assertEqual(answer, "read")
                        with self.assertRaises(mirror.KeptUnread) as cm:
                            mirror.load_kept(f, mirror.settings_wrong)
                        self.assertEqual(cm.exception.why, "its 'owners' entry is not of the kind the tool writes there")
                        continue
                    self.assertEqual(answer, "could not be read", form)
                    want = {"cannot be opened": "it could not be opened (PermissionError)",
                            "a folder": "it could not be opened (IsADirectoryError)", "0 bytes": "it is empty (0 bytes)",
                            "blank space": "it holds only blank space", "not text": "it is not text (UnicodeDecodeError)",
                            "a JSON list": "its top is a JSON list, where the tool keeps an object"}.get(form)
                    if want:
                        self.assertEqual(why, want)
                    else:
                        self.assertTrue(why.startswith("it is not well-formed JSON (cut short, or changed by hand; the "
                                                       "read stopped at line "), why)
                    for planted in ("owners", "someone", "\\x"):
                        self.assertNotIn(planted, why)
                    with self.assertRaises(mirror.KeptUnread):
                        mirror.load_kept(f)
            plant_kept(f, "a folder", whole, None)
            shutil.rmtree(f)
            with open(f, "wb") as fh:
                fh.write(whole)
            self.assertEqual(mirror.load_kept(f, mirror.settings_wrong)["include"], ["someone/a"])
            with open(f, "wb") as fh:
                fh.write(b'{"repos": ["someone/a"]}')
            with self.assertRaises(mirror.KeptUnread):
                mirror.load_kept(f, mirror.state_wrong)

    def test_G6_22_a_key_file_that_cannot_be_read_is_said_and_the_other_owners_go_on(self):
        """M155 (G6.22; seen 2026-11-26 in stand-in trials in made homes): an owner's key file that could not be
        opened, was not text, or was a folder ended left-out, sync and go in a traceback (status too when not text),
        and key set with a folder there ended in a traceback and left a file of its own beside it. Each form: status
        says it in a problem line; sync and left-out say that owner and go on with the other (an organisation);
        key set, on the person's own typed key, replaces the file and says it could not be read, or, for a folder,
        stops with its step; never a traceback, never a byte of it."""
        with World(self) as w:
            w.gh.add_repo("someone", "a")
            w.gh.commit("someone/a", {"f": b"1"})
            w.gh.add_repo("team", "c", owner_type="Organization")
            w.gh.commit("team/c", {"f": b"1"})
            self.assertEqual(w.key_set("someone")[0], 0)
            self.assertEqual(w.key_set("team")[0], 0)
            self.assertEqual(w.first_sync()[0], 0)
            p = w.p
            kp = mirror.key_path(p, "someone")
            whole = fread_bytes(kp)
            forms = [f for f in ("cannot be opened", "not text", "a folder", "0 bytes", "blank space", "two lines")
                     if f != "cannot be opened" or os.geteuid() != 0]
            for i, form in enumerate(forms):
                with self.subTest(form=form):
                    def plant():
                        if form == "two lines":
                            plant_kept(kp, "0 bytes", whole, None)
                            with open(kp, "wb") as fh:
                                fh.write(b"ab12\ncd34\n")
                        else:
                            plant_kept(kp, form, whole, None)
                    plant()
                    mark = kept_mark(kp)
                    w.gh.commit("team/c", {"f": str(i + 2).encode()})
                    new_c = w.gh.git("team/c", "rev-parse", "main").strip()
                    line = f"someone: its key file {kp} could not be read: "
                    for cmd in (["status"], ["left-out"], ["sync"], ["go"]):
                        rc, out = run_kept_cmd(w, cmd)
                        self.assertNotIn("Traceback", out)
                        if cmd != ["go"]:                # after the first copy, go reads no key: it ends whole
                            self.assertIn(line, out, (cmd, out))
                        self.assertEqual(kept_mark(kp), mark)
                        self.assertNotIn("ab12", out)
                    self.assertEqual(w.forge_refs("team--c").get("refs/heads/main"), new_c)   # the other went on
                    rc, out = run_kept_cmd(w, ["key", "set", "someone"])
                    if form == "a folder":
                        self.assertEqual(rc, 2, out)
                        self.assertIn(f"Stopped: {kp} is a folder", out)
                        self.assertTrue(os.path.isdir(kp))
                        self.assertEqual([x for x in os.listdir(os.path.dirname(kp)) if x.startswith("someone.key.")], [])
                        shutil.rmtree(kp)
                        rc, out = run_kept_cmd(w, ["key", "set", "someone"])
                        self.assertEqual(rc, 0, out)
                    else:
                        self.assertEqual(rc, 0, out)
                        self.assertIn("The key file it replaced could not be read (", out)
                    self.assertEqual(fread_bytes(kp), whole)
                    self.assertEqual(run_kept_cmd(w, ["status"])[0], 0)

    def test_G6_23_a_forge_key_that_cannot_be_read_is_said_as_that_and_setup_makes_it_whole(self):
        """M156 (G6.23; seen 2026-11-26 in stand-in trials in made homes): the mirror account's key file at 0 bytes or
        cut short made status, sync and restore --check say "the forge does not answer" (it answered), and setup
        ended 0 and left it so; one that could not be opened, was not text or was a folder ended them in a
        traceback. Each form: status says the file in a problem line with its step; sync and restore --check stop
        naming it; the step taken as printed (setup) gives the account a new key and keeps every copy; for a folder,
        setup stops with its own step and changes nothing."""
        with World(self) as w:
            w.gh.add_repo("someone", "a")
            w.gh.commit("someone/a", {"f": b"1"})
            self.assertEqual(w.key_set()[0], 0)
            self.assertEqual(w.first_sync()[0], 0)
            p = w.p
            mk = p["mirror_key"]
            whole = fread_bytes(mk)
            forms = [f for f in ("cannot be opened", "not text", "a folder", "0 bytes", "cut short", "two lines")
                     if f != "cannot be opened" or os.geteuid() != 0]
            for i, form in enumerate(forms):
                with self.subTest(form=form):
                    if form == "two lines":
                        plant_kept(mk, "0 bytes", whole, None)
                        with open(mk, "wb") as fh:
                            fh.write(whole.strip()[:20] + b"\n" + whole.strip()[20:] + b"\n")
                    else:
                        plant_kept(mk, form, whole, None)
                    mark = kept_mark(mk)
                    rc, out = run_kept_cmd(w, ["status"])
                    self.assertEqual(rc, 1, out)
                    self.assertIn(f"problem: {mk} could not be read: ", out)
                    self.assertIn(f"run setup again: {mirror.own_command(p, 'setup')}", out)
                    for cmd in (["sync"], ["restore", "--check"]):
                        rc, out = run_kept_cmd(w, cmd)
                        self.assertEqual(rc, 2, (cmd, out))
                        self.assertIn(f"Stopped: {mk} could not be read: ", out)
                        self.assertNotIn("does not answer", out)
                        self.assertNotIn(whole.strip()[:20].decode(), out)
                    self.assertEqual(kept_mark(mk), mark)
                    rc, out = w.home.setup()                                     # the step, as printed
                    if form == "a folder":
                        self.assertEqual(rc, 2, out)
                        self.assertIn(f"Stopped: {mk} is a folder", out)
                        self.assertTrue(os.path.isdir(mk))
                        shutil.rmtree(mk)
                        rc, out = w.home.setup()
                    else:
                        self.assertIn(f"key file {mk} could not be read: ", out)
                    self.assertEqual(rc, 0, out)
                    self.assertEqual(mirror.key_file_read(mk, mirror.FORGE_KEY_FORM)[0], "read")
                    w.port = int(mirror.ini_value(p["app_ini"], "server", "HTTP_PORT"))
                    w.gh.commit("someone/a", {"f": str(i + 2).encode()})
                    rc, out = w.sync()
                    self.assertEqual(rc, 0, out)
                    self.assertEqual(w.forge_refs("someone--a").get("refs/heads/main"),
                                     w.gh.git("someone/a", "rev-parse", "main").strip())
                    self.assertEqual(len(w.forge_get("/user/repos?limit=50")[1]), 1)    # kept, never made again
                    whole = fread_bytes(mk)

    def test_G6_26_a_forge_settings_file_that_cannot_be_read_has_a_step_that_ends_with_the_forge_up(self):
        """M159 (G6.26; seen 2026-11-26 in stand-in trials in made homes, Gitea 28.4.0): with the forge's settings file
        damaged, status said nothing while the forge ran, and after its next start said "the forge does not answer;
        run setup again", and setup stopped on Gitea's own startup line with "Run setup again": the step looped. Each
        form, the forge stopped (as after a restart) and still running: status names the file with its step; sync
        and setup stop and change nothing; the step taken as printed (the file moved out of its folder, then setup)
        writes a new one, keeps the forge's address, starts the forge with it, and a sync after brings a change.
        Tried on the pinned Gitea only (28.4.0, 2026-11-26); a forge version that writes SECRET_KEY into that file is not tried."""
        with World(self) as w:
            w.gh.add_repo("someone", "a")
            w.gh.commit("someone/a", {"f": b"1"})
            self.assertEqual(w.key_set()[0], 0)
            self.assertEqual(w.first_sync()[0], 0)
            p, h = w.p, w.home
            ini = p["app_ini"]
            port = int(mirror.ini_value(ini, "server", "HTTP_PORT"))
            forms = [f for f in ("cannot be opened", "0 bytes", "cut short", "not text", "a folder")
                     if f != "cannot be opened" or os.geteuid() != 0]
            n = 0
            for road in ("the forge stopped", "the forge running"):
                for form in forms:
                    n += 1
                    with self.subTest(road=road, form=form):
                        whole = fread_bytes(ini)
                        if road == "the forge stopped":
                            for pr in h.procs:
                                if pr.poll() is None:
                                    pr.terminate()
                                    pr.wait(30)
                        plant_kept(ini, form, whole, None)
                        mark = kept_mark(ini)
                        rc, out = run_kept_cmd(w, ["status"])
                        self.assertEqual(rc, 1, out)
                        self.assertIn(f"problem: {ini} could not be read: ", out)
                        self.assertIn("it is the forge's own settings as they were, yours to keep", out)
                        self.assertIn(f"the forge keeps its address, http://127.0.0.1:{port}/", out)
                        self.assertIn(mirror.own_command(p, "setup"), out)
                        for cmd in (["sync"], ["setup"]):
                            rc, out = run_kept_cmd(w, cmd)
                            self.assertEqual(rc, 2, (cmd, out))
                            self.assertIn(f"Stopped: {ini} could not be read: ", out)
                            self.assertNotIn("Traceback", out)
                        self.assertEqual(kept_mark(ini), mark)
                        kept = os.path.join(h.tmp, f"kept-{n}")
                        os.makedirs(kept)
                        os.rename(ini, os.path.join(kept, "app.ini"))           # the step, as printed
                        rc, out = h.setup()
                        self.assertEqual(rc, 0, out)
                        self.assertIn(f"keeps its address, http://127.0.0.1:{port}/", out)
                        self.assertEqual(mirror.forge_ini_read(p), ("read", None))
                        self.assertEqual(kept_mark(os.path.join(kept, "app.ini")), mark)
                        w.gh.commit("someone/a", {"f": str(n + 1).encode()})
                        rc, out = w.sync()
                        self.assertEqual(rc, 0, out)
                        self.assertEqual(w.forge_refs("someone--a").get("refs/heads/main"),
                                         w.gh.git("someone/a", "rev-parse", "main").strip(), out)
                        self.assertEqual(len(w.forge_get("/user/repos?limit=50")[1]), 1)
                        self.assertEqual(run_kept_cmd(w, ["status"])[0], 0)
                        if os.path.isdir(os.path.join(kept, "app.ini")):
                            shutil.rmtree(kept)
                        else:
                            os.chmod(os.path.join(kept, "app.ini"), 0o600)

    def test_G6_26_the_forge_starts_again_only_as_its_own_service_never_under_a_sync_and_every_copy_stays(self):
        """M159 (G6.26, the owner's four limits on the start again, 11-26). The forge's settings file moved out of
        its folder by the person (the stop's step), the forge still running from it as it was: (3) while a sync holds
        its lock, setup stops with the one step (run setup again when the sync has ended), writes no settings file
        and stops nothing; (2) where systemd shows the forge's service running another copy than this home's, setup
        stops nothing and says so; (4) after the start again: one sync bringing a change into the forge and restore
        --check both fine, the forge's key and the person's password both answer, both accounts there, and every
        copy's refs as before. Tried on the pinned Gitea only (28.4.0): it writes three values of its own into that
        file at its first start (LFS_JWT_SECRET, INTERNAL_TOKEN, JWT_SECRET); a forge version that writes a fourth
        value is not tried, and tokens of two kinds the skill does not use stop working."""
        with World(self) as w:
            for n in ("a", "b"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"f": b"1"})
            self.assertEqual(w.key_set()[0], 0)
            self.assertEqual(w.first_sync()[0], 0)
            p, h = w.p, w.home
            refs = {r["name"]: w.forge_refs(r["name"]) for r in w.forge_get("/user/repos?limit=50")[1]}
            person = mirror.load_state(p)["person"]
            kept = os.path.join(h.tmp, "kept-by-the-person")
            os.makedirs(kept)
            os.rename(p["app_ini"], os.path.join(kept, "app.ini"))        # the step, as printed
            stops = lambda n: [c for c in h.calls[n:] if c == ["systemctl", "--user", "stop", mirror.SERVICE]]
            # (3) a sync holds the lock
            sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            try:
                with open(os.path.join(p["data"], "sync.lock"), "w") as f:
                    f.write(str(sleeper.pid))
                n = len(h.calls)
                rc, out = h.setup()
                self.assertEqual(rc, 3, out)
                self.assertIn(f"Not now: a sync is running (process {sleeper.pid})", out)
                self.assertIn(f"The one next step: run setup again when the sync has ended: "
                              f"{mirror.own_command(p, 'setup')}", out)
                self.assertFalse(os.path.exists(p["app_ini"]))
                self.assertEqual(stops(n), [])
                self.assertEqual(fread(os.path.join(p["data"], "sync.lock")), str(sleeper.pid))
            finally:
                sleeper.kill()
                sleeper.wait()
            os.remove(os.path.join(p["data"], "sync.lock"))
            # (2) systemd shows another copy for the forge's service just before the stop (Linux's road)
            if mirror.system()[0] != "darwin":
                real = mirror.service_read
                mirror.service_read = lambda p_, which, _r=real: ("equal", None, None) if which == "forge" else _r(p_, which)
                h.bus_starts = [["/usr/bin/python3", ["/usr/bin/python3", "-I", "/srv/another-home/mirror.py",
                                                      "run-forge"], False, 0, 0, 0, 0, 0, 0, 0]]
                try:
                    n = len(h.calls)
                    rc, out = h.setup()
                finally:
                    mirror.service_read, h.bus_starts = real, None
                self.assertNotEqual(rc, 0, out)
                self.assertIn("is not this home's own copy, so setup stops nothing", out)
                self.assertEqual(stops(n), [])
                os.remove(p["app_ini"])                                     # as it was, for the road itself
            # the road: setup writes the file and starts the forge's own service again
            n = len(h.calls)
            rc, out = h.setup()
            self.assertEqual(rc, 0, out)
            self.assertFalse(os.path.exists(os.path.join(p["data"], "sync.lock")))     # setup let its lock go
            if mirror.system()[0] != "darwin":
                self.assertEqual(len(stops(n)), 1, out)
            w.port = int(mirror.ini_value(p["app_ini"], "server", "HTTP_PORT"))
            end = time.time() + 60
            while time.time() < end and quiet(mirror.main, ["status"])[0] != 0:
                time.sleep(1)
            # (4) what a person needs to be true after it
            self.assertEqual({r["name"]: w.forge_refs(r["name"]) for r in w.forge_get("/user/repos?limit=50")[1]}, refs)
            code, _, body = http_get(f"http://127.0.0.1:{w.port}/api/v1/user", fread(p["mirror_key"]).strip())
            self.assertEqual((code, json.loads(body)["login"]), (200, "mirror"))
            req = urllib.request.Request(f"http://127.0.0.1:{w.port}/api/v1/user", headers={
                "Authorization": "Basic " + __import__("base64").b64encode(f"{person}:{fread(p['password']).strip()}".encode()).decode()})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as r:
                self.assertEqual((r.status, json.loads(r.read())["login"]), (200, person))
            new = w.gh.commit("someone/a", {"f": b"2"})
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertEqual(w.forge_refs("someone--a").get("refs/heads/main"), new)
            self.assertEqual(w.forge_refs("someone--b"), refs["someone--b"])
            rc, out = quiet(mirror.main, ["restore", "--check"])
            self.assertEqual(rc, 0, out)

    def test_G6_24_a_state_file_that_cannot_be_read_is_never_set_up_first(self):
        """M157 (G6.24; seen 2026-11-26 in stand-in trials in made homes): status, sync, restore --check and go said
        "Run setup first." and key set "Setup comes first", and setup ended 0 and wrote a new state file over one
        it could not read. Every form, before every command that reads it: never written over, GitHub never asked;
        the stop names the file, where the read stopped and one step, with what follows said plainly. The step
        taken as printed (0.1.0's road, row M165 for its rebuild): setup, the go again, a sync: every copy kept, each
        made again beside its old one, nothing deleted."""
        with World(self) as w:
            for n in ("a", "b"):
                w.gh.add_repo("someone", n)
                w.gh.commit(f"someone/{n}", {"f": b"1"})
            self.assertEqual(w.key_set()[0], 0)
            self.assertEqual(w.first_sync()[0], 0)
            p = w.p
            kept_file_trial(self, w, p["state"], b'{"repos": ["someone/a"]}\n')
            before = sorted(r["name"] for r in w.forge_get("/user/repos?limit=50")[1])
            plant_kept(p["state"], "0 bytes", b"", None)
            rc, out = run_kept_cmd(w, ["sync"])
            self.assertEqual(rc, 2, out)
            step = out.split("The one next step: ", 1)[1]
            self.assertIn(f"move that file out of {os.path.dirname(p['state'])}", step)
            self.assertIn(mirror.own_command(p, "setup"), step)
            for said in ("the go for the first copy is asked again", "beside its old one", "with its kept history",
                         "doubles", "Nothing is deleted"):
                self.assertIn(said, step)
            kept = os.path.join(w.home.tmp, "kept-by-the-person")
            os.makedirs(kept)
            os.rename(p["state"], os.path.join(kept, "state.json"))              # the step, as printed
            rc, out = w.home.setup()
            self.assertEqual(rc, 0, out)
            w.port = int(mirror.ini_value(p["app_ini"], "server", "HTTP_PORT"))
            self.assertEqual(w.first_sync()[0], 0)
            after = sorted(r["name"] for r in w.forge_get("/user/repos?limit=50")[1])
            self.assertTrue(set(before) <= set(after), (before, after))       # nothing deleted
            self.assertEqual(len(after), 2 * len(before), after)              # each made again beside its old one
            self.assertEqual(quiet(mirror.main, ["status"])[0], 0)

    def test_G6_25_a_record_that_cannot_be_read_is_said_as_that_and_the_way_back_goes_on_from_the_folder(self):
        """M158 (G6.25; seen 2026-11-26 in stand-in trials in made homes): with the skill's record of what it made in
        a form that could not be read, or not there, setup said the skill's own service files were "not as the skill
        wrote it", uninstall named the skill's own files as "Not the skill's" and made a new record, and the way back
        it named ended at a second home. Each form planted in the record, then status, setup, uninstall
        --delete-copies (typed at a terminal) and uninstall: said as that; nothing in the skill's folders deleted,
        written or made (the whole made home, paths and bytes, its service files aside); each kept key named; the
        need-you block printed last. Then the step as printed (setup --use-this-folder): status 0, and one sync
        moves no copy aside and makes none twice; the keys byte for byte; the set-aside record its own kind."""
        forms = [f for f in ("cannot be opened", "0 bytes", "not there", "cut short", "not text", "a JSON list")
                 if f != "cannot be opened" or os.geteuid() != 0]
        for form in forms:
            with self.subTest(form=form), World(self) as w:
                for n in ("a", "b"):
                    w.gh.add_repo("someone", n)
                    w.gh.commit(f"someone/{n}", {"f": b"1"})
                self.assertEqual(w.key_set()[0], 0)
                self.assertEqual(w.first_sync()[0], 0)
                p, h = w.p, w.home
                made = os.path.join(p["data"], mirror.MADE_RECORD)
                whole = fread_bytes(made)
                copies = {r["name"]: w.forge_refs(r["name"]) for r in w.forge_get("/user/repos?limit=50")[1]}
                keys = {k: fread_bytes(k) for k in (os.path.join(p["keys"], "someone.key"), p["mirror_key"],
                                                    p["password"], p["settings"])}
                if form == "not there":
                    os.remove(made)
                else:
                    plant_kept(made, form, whole, None)
                said = "is not there" if form == "not there" else "could not be read: "
                rc, out = quiet(mirror.main, ["status"])
                self.assertEqual(rc, 1, out)
                self.assertIn(f"problem: {made} {said}", out)
                self.assertNotIn("not as the skill wrote it", out)
                rc, out = h.setup()
                self.assertEqual(rc, 2, out)
                self.assertIn(f"Stopped: {made} {said}", out)
                self.assertNotIn("not as the skill wrote it", out)
                self.assertNotIn("WSM_HOME", out.replace("WSM_HOME is set", ""))
                lines = [x for x in out.strip().split("\n") if x.strip()]
                step = mirror.own_command(p, "setup", "--use-this-folder")
                self.assertEqual(lines[-2], f"tell the person 1 of 2: {mirror.USE_FOLDER_STEP}{step}")
                self.assertTrue(lines[-1].startswith("tell the person 2 of 2: "), lines[-1])
                for pr in h.procs:                        # the forge stopped, so the whole made home holds still
                    if pr.poll() is None:
                        pr.terminate()
                        pr.wait(30)
                before = made_home_tree(h, p)
                saved = sys.stdin
                sys.stdin = AtTerminal("2\n")
                try:
                    rc, out = quiet(mirror.main, ["uninstall", "--delete-copies"])
                finally:
                    sys.stdin = saved
                self.assertEqual(rc, 1, out)
                self.assertNotIn("type the number", out)
                self.assertIn(f"The copies were not deleted: {made} {said}", out)
                self.assertIn(f"it is yours now", out)
                self.assertEqual(out.count("--use-this-folder"), 1, out)
                self.assertEqual([x for x in out.strip().split("\n") if x.strip()][-1], mirror.USE_FOLDER_STEP + step)
                self.assertEqual(made_home_tree(h, p), before)
                rc, out = quiet(mirror.main, ["uninstall"])
                self.assertEqual(rc, 0, out)
                self.assertEqual(made_home_tree(h, p), before)
                self.assertIn(f"Not taken: {made} {said}", out)
                for k in (os.path.join(p["keys"], "someone.key"), p["mirror_key"], p["password"]):
                    self.assertIn(f"({k})", out.split("kept because that record cannot show them")[1].split("\n")[0])
                self.assertNotIn("Not the skill's", out)
                self.assertNotIn("not as the skill wrote it", out)
                self.assertNotIn("goes on from it", out)
                self.assertEqual([x for x in out.strip().split("\n") if x.strip()][-1], mirror.USE_FOLDER_STEP + step)
                self.assertEqual(os.path.lexists(made), form != "not there")     # no new record by uninstall
                rc, out = run_printed(w, step + " --linger")                     # the step, as printed
                self.assertEqual(rc, 0, out)
                self.assertIn("Taken on again, on your word (--use-this-folder)", out)
                w.port = int(mirror.ini_value(p["app_ini"], "server", "HTTP_PORT"))
                end = time.time() + 60                       # the stand-in's service starts the forge on its own
                rc, out = quiet(mirror.main, ["status"])
                while rc != 0 and time.time() < end and "the forge does not answer" in out:
                    time.sleep(1)
                    rc, out = quiet(mirror.main, ["status"])
                self.assertEqual(rc, 0, out)
                self.assertEqual(w.sync()[0], 0)
                now = {r["name"]: w.forge_refs(r["name"]) for r in w.forge_get("/user/repos?limit=50")[1]}
                self.assertEqual(now, copies)                 # none moved aside, none made twice, each as it was
                self.assertEqual({k: fread_bytes(k) for k in keys}, keys)
                rec = mirror.read_made(p)[0]
                aside = [k for k, v in rec.items() if v == "set-aside record"]
                self.assertEqual(len(aside), 0 if form == "not there" else 1, rec)
                for k in aside:
                    self.assertTrue(os.path.basename(k).startswith(mirror.MADE_RECORD + ".could-not-be-read-"))
                    self.assertEqual(kept_mark(k)[2], len(whole) if form in ("cannot be opened",) else kept_mark(k)[2])
                    os.chmod(k, 0o600)

    def test_G6_25_the_way_back_takes_on_only_a_folder_that_holds_a_mirror(self):
        """M158 (G6.25), by paths: a folder that holds a mirror (the forge's settings file planted) with no record
        stops setup with the need-you block; a folder setup never made (a person's own file, no record) is never
        taken on, with or without the option, and its step stays a folder of its own. With a whole record, or in
        folders that held nothing, the option changes nothing and says so. The stop and SKILL.md both say the option
        is the person's own word."""
        skill = fread(SKILL)
        self.assertIn("Never type `setup --use-this-folder` unless the person asks for it in their own words", skill)
        with Home(linger="yes") as h:
            p = mirror.paths()
            self.assertTrue(mirror.home_fresh(p))
            mine = os.path.join(os.environ["WSM_HOME"], "a-letter-of-the-persons.txt")
            os.makedirs(os.path.dirname(mine), exist_ok=True)
            with open(mine, "w") as f:
                f.write("mine\n")
            self.assertFalse(mirror.home_fresh(p))
            for args in ((), ("--use-this-folder",)):
                rc, out = h.setup(*args)
                self.assertEqual(rc, 2, out)
                self.assertIn("set WSM_HOME to a folder that does not exist yet", out)
                self.assertNotIn("Taken on again", out)
                self.assertNotIn(mirror.USE_FOLDER_STEP, out)
                self.assertFalse(os.path.lexists(os.path.join(p["data"], mirror.MADE_RECORD)))
            self.assertEqual(fread(mine), "mine\n")
            os.makedirs(os.path.dirname(p["app_ini"]))                    # now it holds a mirror, by its paths
            with open(p["app_ini"], "w") as f:
                f.write(mirror.app_ini(p, 3000))
            rc, out = h.setup()
            self.assertEqual(rc, 2, out)
            self.assertIn(f"{os.path.join(p['data'], mirror.MADE_RECORD)} is not there, and {p['data']} holds a mirror",
                          out)
            self.assertTrue(out.strip().split("\n")[-2].startswith(f"tell the person 1 of 2: {mirror.USE_FOLDER_STEP}"))
            self.assertFalse(os.path.lexists(os.path.join(p["data"], mirror.MADE_RECORD)))
        with World(self) as w:
            rec = mirror.read_made(w.p)[0]
            rc, out = w.home.setup("--use-this-folder")
            self.assertEqual(rc, 0, out)
            self.assertIn("--use-this-folder changes nothing here: the record of what the skill made", out)
            self.assertEqual(set(mirror.read_made(w.p)[0]), set(rec))

    def test_G6_25_the_way_back_takes_only_the_skills_own_names_and_forms(self):
        """M158 (G6.25, the owner's ruling 11-26: never by a name alone): with the record not there, setup
        --use-this-folder takes on the forge's program only under the pinned name and with the pin's SHA-256, and a
        key file only under a name the skill gives, mode 0600, one line; it names each file it took and each it did
        not, with the two counts, never a byte of a key. A file of the person's own (a .key under a name the skill
        does not give, a file named like the forge's program with other bytes) stays through the way back and
        through uninstall --delete-copies, named both times. A file at the place the forge's program goes, with
        other bytes, stops setup before any write."""
        with World(self) as w:
            w.gh.add_repo("someone", "a")
            w.gh.commit("someone/a", {"f": b"1"})
            self.assertEqual(w.key_set()[0], 0)
            self.assertEqual(w.first_sync()[0], 0)
            p, h = w.p, w.home
            made = os.path.join(p["data"], mirror.MADE_RECORD)
            os.remove(made)
            held = os.path.join(h.tmp, "the-pinned-program")
            os.rename(p["bin"], held)                               # (the forge runs it: moved, never written)
            with open(p["bin"], "wb") as f:                        # the program's own place, other bytes
                f.write(b"not the pinned program\n")
            rc, out = h.setup("--use-this-folder")
            self.assertEqual(rc, 2, out)
            self.assertIn(p["bin"], out.split("already holds things the skill did not make: ", 1)[1])
            self.assertFalse(os.path.lexists(made))
            self.assertEqual(fread_bytes(p["bin"]), b"not the pinned program\n")
            os.replace(held, p["bin"])
            mine = {os.path.join(p["keys"], "a-note.key"): b"the person's own\n",
                    os.path.join(os.path.dirname(p["bin"]), "gitea-28.3.0"): b"the person's own program\n",
                    os.path.join(p["data"], "notes.txt"): b"the person's own\n"}
            for x, data in mine.items():
                with open(x, "wb") as f:
                    f.write(data)
                os.chmod(x, 0o600)
            rc, out = h.setup("--use-this-folder")
            self.assertEqual(rc, 0, out)
            took = out.split("Taken on as the skill's (", 1)[1].split("Not taken (", 1)
            n_took, took_lines = int(took[0].split(")", 1)[0]), [x.strip() for x in took[0].split("\n")[1:] if x.strip()]
            n_left = int(took[1].split(")", 1)[0])
            left_lines = [x.strip() for x in took[1].split("\n")[1:] if x.startswith("  ")]
            self.assertEqual(len(took_lines), n_took)
            self.assertEqual(sorted(left_lines), sorted(mine))
            self.assertEqual(n_left, len(mine))
            for x in (p["bin"], key_path := mirror.key_path(p, "someone"), p["mirror_key"], p["password"]):
                self.assertIn(x, took_lines)
            for x in mine:
                self.assertNotIn(x, took_lines)
            for k in (key_path, p["mirror_key"], p["password"]):
                self.assertNotIn(fread(k).strip(), out)
            for pr in h.procs:
                if pr.poll() is None:
                    pr.terminate()
                    pr.wait(30)
            saved = sys.stdin
            sys.stdin = AtTerminal("1\n")
            try:
                rc, out = quiet(mirror.main, ["uninstall", "--delete-copies"])
            finally:
                sys.stdin = saved
            self.assertEqual(rc, 0, out)
            kept = out.split("Not the skill's, so left where they are", 1)[1]
            for x, data in mine.items():
                self.assertEqual(fread_bytes(x), data)
                self.assertIn(f"  {x}\n", kept)
            self.assertFalse(os.path.lexists(p["bin"]))
            self.assertFalse(os.path.lexists(key_path))
            left = {q for q in made_home_tree(h, p) if not os.path.isdir(q)}
            self.assertEqual(left - set(mine), {os.path.join(os.environ["WSM_HOME"], mirror.SUITE_MARKER)}
                             | ({made} if os.path.lexists(made) else set()))

    def test_G6_25_a_service_file_not_as_setup_writes_it_is_never_written_over_whatever_the_kept_files_say(self):
        """The owner's Q1 (11-26), the meant file's road: with the meant file not readable (0 bytes, cut short, not
        there) and a person's own line in one service file (the forge's unit, or the sync's timer), setup stops and
        writes neither the meant file nor any service file, and switches nothing; with the record of what the skill
        made unreadable as well, the same, and so with --use-this-folder too (no new record is written)."""
        if mirror.system()[0] == "darwin":
            self.skipTest("the systemd road; the Mac's service files are its own test's (G1.24)")
        for record in ("read", "0 bytes"):
            with World(self) as w:
                p, h = w.p, w.home
                base = os.path.dirname(p["unit"])
                files = {"the forge's unit": p["unit"], "the sync's timer": os.path.join(base, mirror.SYNC_UNIT + ".timer")}
                meant = os.path.join(p["data"], mirror.SERVICE_MEANT)
                made = os.path.join(p["data"], mirror.MADE_RECORD)
                whole_meant, whole_made = fread_bytes(meant), fread_bytes(made)
                for which, f in files.items():
                    for form in ("0 bytes", "cut short", "not there"):
                        with self.subTest(record=record, file=which, meant=form):
                            text = fread(f)
                            with open(f, "w") as fh:
                                fh.write(text + "# a line of the person's own\n")
                            if form == "not there":
                                os.remove(meant)
                            else:
                                plant_kept(meant, form, whole_meant, None)
                            if record != "read":
                                plant_kept(made, record, whole_made, None)
                            sums = {x: mirror.sha256_of(x) for x in mirror.service_files(p)}
                            marks = {x: (kept_mark(x) if os.path.lexists(x) else None) for x in (meant, made)}
                            for args in ((), ("--use-this-folder",)) if record != "read" else ((),):
                                n = len(h.calls)
                                rc, out = h.setup(*args)
                                self.assertEqual(rc, 2, out)
                                self.assertEqual({x: mirror.sha256_of(x) for x in mirror.service_files(p)}, sums)
                                self.assertEqual({x: (kept_mark(x) if os.path.lexists(x) else None)
                                                  for x in (meant, made)}, marks)
                                self.assertEqual([c for c in h.calls[n:] if c[:2] == ["systemctl", "--user"] and any(
                                    v in c for v in ("enable", "start", "stop", "restart", "disable"))], [])
                                if args:
                                    self.assertIn(f"{f}, and not what setup writes there now", out)
                                    self.assertNotIn("Taken on again", out)
                            with open(f, "w") as fh:
                                fh.write(text)
                            with open(meant, "wb") as fh:
                                fh.write(whole_meant)
                            with open(made, "wb") as fh:
                                fh.write(whole_made)

    def test_G6_19_a_name_in_the_older_sentences_is_never_printed_in_a_form_a_shell_runs(self):
        """M134 (family rule 31(B), the maintainer's word for 0.1.0, 2026-11-25): a name on GitHub is chosen by
        whoever can push there. In every line of sync, status and restore --check, a branch's or a tag's name with a
        backtick or a dollar-parenthesis is shown changed, and the line says so; the names themselves are stored,
        kept and pushed as they are, and two names that show alike stay two."""
        forms = lambda out: [l for l in out.splitlines() if "`" in l or "$(" in l]
        note = f" ({mirror.SHOWN_CHANGED})"
        with World(self) as w:
            full = "someone/alpha"
            w.gh.add_repo("someone", "alpha")
            one = w.gh.commit(full, {"a": b"1"})
            two = w.gh.commit(full, {"a": b"2"})
            blob = w.gh.git(full, "rev-parse", "main:a").strip()
            me = ["-c", "user.name=a", "-c", "user.email=a@example.invalid"]
            for b in ("d`x`", "d$(x)", "c$(q)", "plain"):
                w.gh.git(full, "branch", b, one)
            w.gh.git(full, "tag", "t$(y)", one)
            w.gh.git(full, *me, "tag", "-a", "-m", "a note", "t`z`", one)
            w.gh.git(full, "tag", "f$(b)", blob)
            odd = disk_holds(os.path.dirname(w.p["state"]), "n`\udcff")   # a Mac's disk cannot hold it
            if odd:
                w.gh.plant(full, "refs/heads/n`\udcff", "main")
            w.key_set()
            rc, out = w.first_sync()
            self.assertEqual(rc, 0, out)
            said = out
            if odd:                                   # M120: left out, named at every sync and in status
                limit = "problem: someone/alpha: " + mirror.NOT_TEXT.format(n=1, names="n??; " + mirror.SHOWN_CHANGED)
                self.assertIn(limit, out)
            # GitHub moves and deletes them: each kept line shows the name changed, and says so.
            for b in ("d`x`", "d$(x)"):
                w.gh.git(full, "update-ref", "-d", "refs/heads/" + b)
            w.gh.git(full, "update-ref", "refs/tags/t$(y)", two)
            w.gh.git(full, "update-ref", "-d", "refs/tags/t`z`")
            w.gh.git(full, "update-ref", "-d", "refs/tags/f$(b)")
            w.gh.repos[full]["pushed_at"] = "2002-01-01T00:00:00Z"
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            said += out
            day = time.strftime("%Y-%m-%d", time.gmtime())
            for was, what in (("d?x?", "deleted"), ("d??x)", "deleted"), ("tags/t??y)", "moved"),
                              ("tags/t?z?", "deleted")):
                self.assertIn(f"{full}: {was} was {what} on GitHub; its old commit is kept as kept/{day}/{was}{note}",
                              out)
            self.assertIn(mirror.TAG_NOT_KEPT.format(full=full, short="tags/f??b)", what="deleted", at="a file")
                          + note, out)
            # Only the printed form changed: the forge holds every name as it is, each kept ref by its own name.
            held = w.forge_refs("someone--alpha")
            for r in ("refs/heads/kept/%s/d`x`", "refs/heads/kept/%s/d$(x)", "refs/heads/kept/%s/tags/t$(y)",
                      "refs/heads/kept/%s/tags/t`z`"):
                self.assertIn(r % day, held)
            out = subprocess.run(["git", "--git-dir", os.path.join(w.gh.root, "someone", "alpha.git"), "for-each-ref",
                                  "--format=%(refname)"], capture_output=True, check=True).stdout
            gh = {os.fsdecode(r) for r in out.split(b"\n") if r}
            gh = {r for r in gh if not mirror.written_not_text(r)}
            self.assertEqual({r for r in held if not r.startswith("refs/heads/kept/")}, gh)
            rc, out = quiet(mirror.main, ["status"])
            said += out
            if odd:
                self.assertIn(limit, out)
            # restore --check's difference line: a name taken out of the copy behind the tool's back.
            folder = os.path.join(w.p["forge"], "data", "gitea-repositories", "mirror", "someone--alpha.git")
            subprocess.run(["git", "--git-dir", folder, "update-ref", "-d", "refs/heads/c$(q)"], check=True)
            rc, out = quiet(mirror.main, ["restore", "--check"])
            self.assertEqual(rc, 1, out)
            said += out
            self.assertRegex(out, re.escape(f"{full}: refs/heads/c??q) is missing in the copy, ") + r"[0-9a-f]{12}"
                             + re.escape(" when GitHub was last seen" + note))
            # Two lines that need a world the guard alone makes (NOT_AS_LISTED, FORGE_NOT_AS_LISTED), read by their
            # one builder; two names that show alike stay two.
            for words in (mirror.NOT_AS_LISTED, mirror.FORGE_NOT_AS_LISTED):
                line = mirror.not_as_listed_line([("refs/heads/a`b", "missing"), ("refs/heads/a$(b", "missing"),
                                                  ("refs/tags/v`1", "at another commit")], words)
                self.assertEqual(line, words.format(n=3, names="a?b: missing, a??b: missing, tag v?1: at another "
                                                    "commit; " + mirror.SHOWN_CHANGED))
                said += line + "\n"
            self.assertEqual(mirror.shown_names(["x`1", "x\udcff1"]), "x?1, x?1; " + mirror.SHOWN_CHANGED)
            self.assertEqual(forms(said), [], said)
            # Inside double quotes a shell reads a dollar and a question mark as the last exit code: a shown name
            # never holds that pair, so it never shows a digit that was never in it.
            self.assertNotIn("$" + "?", said)
            self.assertNotIn(mirror.SHOWN_CHANGED, mirror.shown_names(["plain", "x;y"]))
        # git's and the forge's own words (M134's second part, the maintainer's word 11-25): they quote a name that
        # somebody else chose. Each is read on the printed line (what the sync's service writes to its log) and in
        # status, which reads the problem the sync wrote down; only the two forms change, the rest letter for letter.
        bad = "e`x`$(y)"
        for road in ("git", "forge"):
            with World(self) as w:
                full = "someone/beta"
                w.gh.add_repo("someone", "beta")
                one = w.gh.commit(full, {"a": b"1"})
                w.gh.git(full, "branch", bad, one)
                w.key_set()
                rc, out = w.first_sync()
                self.assertEqual(rc, 0, out)
                self.assertIn("refs/heads/" + bad, w.forge_refs("someone--beta"))          # the premise
                w.gh.git(full, "update-ref", "refs/heads/" + bad, w.gh.commit(full, {"a": b"2"}))   # it moves too
                w.gh.repos[full]["pushed_at"] = "2002-01-01T00:00:00Z"
                folder = os.path.join(w.p["forge"], "data", "gitea-repositories", "mirror", "someone--beta.git")
                saved = mirror.forge_call
                if road == "git":
                    # The name taken out of the cache between the listing and the push: git itself says
                    # "src refspec <the name> does not match any".
                    def gone(forge, cache, *args, **kw):
                        if args[:1] == ("push",):
                            subprocess.run(["git", "--git-dir", cache, "update-ref", "-d", "refs/heads/" + bad],
                                           check=True)
                        return saved(forge, cache, *args, **kw)
                    mirror.forge_call = gone
                    words = "error: src refspec refs/heads/e?x???y) does not match any; " + mirror.SHOWN_CHANGED
                else:
                    # The forge refuses the push and names the ref in its own words: a hook in the copy's own folder,
                    # run by Gitea's pre-receive and relayed by git as "remote: error: ...".
                    hooks = os.path.join(folder, "hooks", "pre-receive.d")
                    self.assertTrue(os.path.isdir(hooks), os.listdir(os.path.join(folder, "hooks")))
                    hook = os.path.join(hooks, "zz-refuse")
                    with open(hook, "w") as f:
                        f.write("#!/bin/sh\nwhile read o n r; do case \"$r\" in *e?x?*) echo \"error: refusing $r\" >&2"
                                ";; esac; done\nexit 1\n")
                    os.chmod(hook, 0o755)
                    words = ("refusing refs/heads/e?x???y); " + mirror.SHOWN_CHANGED + "; pre-receive hook declined")
                try:
                    rc, out = w.sync()
                finally:
                    mirror.forge_call = saved
                self.assertEqual(rc, 1, out)
                self.assertIn(f"{full}: the push into the forge failed, so nothing in its copy was moved (the push is "
                              f"all or nothing): {words}", out)
                said = out
                rc, out = quiet(mirror.main, ["status"])
                self.assertIn(words, out, road)
                said += out
                self.assertEqual(forms(said), [], (road, said))
                self.assertIn("refs/heads/" + bad, w.forge_refs("someone--beta"))          # the name, as it is


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


def git_all_refs(path):
    """Every ref a bare folder holds, as git holds it: {name: commit or tag object}."""
    out = subprocess.run(["git", "--git-dir", path, "for-each-ref", "--format=%(objectname) %(refname)"],
                         capture_output=True, text=True, check=True).stdout
    return dict(reversed(l.split(" ", 1)) for l in out.splitlines())


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
            self.assertIn("restore check: 2 copies, 2 whole, 0 difference(s), 0 could not be read; nothing outside this computer was asked", out)
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

    def test_G7_5_restore_check_says_when_the_forge_does_not_answer(self):
        """A real machine (2026-11-23): with the forge's service stopped, restore --check printed a raw connection
        error and "0 whole, 1 difference(s)", with no next step. A forge that does not answer is said as that, with
        its next step and a failing code, and is never counted as a difference in a copy."""
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            pr = w.home.procs[-1]
            pr.terminate()
            pr.wait(60)
            rc, out = quiet(mirror.main, ["restore", "--check"])
            self.assertEqual(rc, 1, out)
            self.assertIn(f"problem: the forge does not answer on 127.0.0.1:{w.port}, so no copy could be checked", out)
            self.assertIn("run status", out)
            for word in ("difference", "whole", "urlopen", "Errno"):
                self.assertNotIn(word, out)
            w.home.start_forge()
            rc, out = quiet(mirror.main, ["restore", "--check"])
            self.assertEqual(rc, 0, out)
            self.assertIn("1 whole, 0 difference(s), 0 could not be read", out)
            # One copy that cannot be read, with the forge answering: a third number, never a difference, and no
            # raw text from the library.
            real = mirror.Forge.call

            def call(forge, method, path, body=None):
                return (0, {"message": "<urlopen error [Errno 111] Connection refused>"}) if "/repositories/" in path \
                    else real(forge, method, path, body)
            mirror.Forge.call = call
            try:
                rc, out = quiet(mirror.main, ["restore", "--check"])
            finally:
                mirror.Forge.call = real
            self.assertEqual(rc, 1, out)
            self.assertIn("someone/alpha: its copy could not be read: the forge could not be read (it did not answer)", out)
            self.assertIn("0 whole, 0 difference(s), 1 could not be read", out)
            self.assertNotIn("urlopen", out)

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
                self.assertIn(" push --mirror ", out)                       # the line itself: G7.8
            self.assertEqual(w.gh.log[n:], [], "the refused restore talked to GitHub")
            self.assertEqual(w.gh.writes(), [])
            if mirror.has_git_lfs():
                lhome = bare_home(root, "beta")
                rc, out = quiet(mirror.main, ["restore", "--to", lhome, "someone/beta"])
                self.assertEqual(rc, 0, out)
                self.assertTrue([f for d, _, fs in os.walk(os.path.join(lhome, "lfs", "objects")) for f in fs],
                                "the large file did not reach the new home")

    def test_G7_7_a_restore_never_leaves_a_name_out_unsaid(self):
        """M128 (seen 2026-11-25 on 0.1.0's head with the guard and the tag cures): restore --to of a project whose
        GitHub branches include kept/... exited 0 and left them out of the new home, without a word. Now each is
        named, and each gets one whole line a person runs as it is printed (rule 31: a name we did not write never
        reaches a shell raw); a name this tool does not print as it is (a character no one can see, a backtick, a
        dollar-parenthesis: rule 31(B)) gets no line, the text says so, and no printed line carries either form."""
        with World(self) as w:
            plain = [f"kept/{x}" for x in ("a", "b", "c", "d", "e", "f")] + ["kept/2020-01-01/old"]
            semi = "kept/x;id>m3"                                                 # runs a command if typed raw
            hidden = {"kept/$(id>m1)": "kept/??id>m1)", "kept/`id>m2`": "kept/?id>m2?",   # rule 31(B): never printed
                      "kept/zero\u200bwidth": "kept/zero?width"}                  # a character no one can see
            names = plain + [semi] + sorted(hidden)
            for n in ("alpha", "beta"):
                w.gh.add_repo("someone", n)
                one = w.gh.commit(f"someone/{n}", {"a": b"1"})
                w.gh.git(f"someone/{n}", "tag", "v1", one)
                for b in names if n == "alpha" else ():
                    w.gh.git("someone/alpha", "branch", b, one)
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            held = w.forge_refs("someone--alpha")
            self.assertEqual(sorted(r for r in held if r.startswith("refs/heads/kept/")),
                             sorted("refs/heads/" + b for b in names))           # the premise: the copy holds them
            root = tempfile.mkdtemp(prefix="wsm-home-")
            TEMPS.append(root)
            home = bare_home(root, "alpha")
            rc, out = quiet(mirror.main, ["restore", "--to", home, "someone/alpha"])
            self.assertEqual(rc, 1, out)
            self.assertIn(f"restored someone/alpha: 1 branch(es) and 1 tag(s) to {home}", out)
            shown = [hidden.get(b, b) for b in sorted(names)]
            said = mirror.RESTORE_LEFT_OUT.format(n=11, names=", ".join(shown[:5]) + " and 6 more; "
                                                  + mirror.SHOWN_CHANGED)
            self.assertIn(said, out)
            # Rule 31(B): no line restore --to prints carries a backtick or a dollar-parenthesis.
            self.assertFalse([l for l in out.splitlines() if "`" in l or "$(" in l], out)
            self.assertEqual(lsremote(home), {r: v for r, v in w.gh_refs("someone/alpha").items()
                                              if not r.startswith("refs/heads/kept/")})
            # One line per name that can be shown, every one of them (not only the first five), none for the other.
            lines = [l for l in out.splitlines() if l.startswith("  git ") and " push " in l]
            self.assertEqual(len(lines), 8, out)
            self.assertFalse([l for l in lines if "\u200b" in l], "a line for a name no one can read")
            self.assertEqual(len([l for l in lines if shlex.quote("refs/heads/" + semi) in l]), 1, out)
            folder = os.path.join(w.p["forge"], "data", "gitea-repositories", "mirror", "someone--alpha.git")
            self.assertTrue(os.path.isdir(folder), "stock Gitea keeps the copy elsewhere")
            self.assertIn(mirror.RESTORE_HIDDEN.format(n=3) + f"\n  git --git-dir {shlex.quote(folder)} for-each-ref "
                          "refs/heads/kept/\n", out)
            listing = [l for l in out.splitlines() if l.startswith("  git ") and " for-each-ref " in l]
            self.assertEqual(len(listing), 1, out)
            listed = subprocess.run(["bash", "-c", listing[0].strip()],
                                    capture_output=True, text=True, cwd=root, stdin=subprocess.DEVNULL)
            for b in hidden:
                self.assertIn("refs/heads/" + b, listed.stdout)                  # that line lists each, as git holds it
            self.assertIn("Run each git line above as it stands, in a terminal", out)
            # Every line run through a real shell exactly as printed, in a folder of its own, with nothing added
            # (the text gives the person nothing to supply): no command inside a name runs, and the names arrive.
            # (The listing line above was run in root, not here.)
            here = tempfile.mkdtemp(prefix="wsm-shell-")
            TEMPS.append(here)
            for l in lines:
                r = subprocess.run(["bash", "-c", l.strip()], cwd=here, capture_output=True, text=True,
                                   env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), stdin=subprocess.DEVNULL)
                self.assertEqual(r.returncode, 0, (l, r.stderr))
            self.assertEqual(os.listdir(here), [], "a command inside a name ran")
            self.assertEqual(lsremote(home), {r: v for r, v in w.gh_refs("someone/alpha").items()
                                              if r[len("refs/heads/"):] not in hidden})
            # The road without the copy's folder ("open an issue with these lines"): the same left-out sentence in
            # the shown form, no git line, and still no line with either form.
            home = bare_home(root, "alpha-again")
            real = mirror.forge_folder
            mirror.forge_folder = lambda p, forge, name: ""
            try:
                rc, out = quiet(mirror.main, ["restore", "--to", home, "someone/alpha"])
            finally:
                mirror.forge_folder = real
            self.assertEqual(rc, 1, out)
            self.assertIn(said, out)
            self.assertIn("open an issue with these lines", out)
            self.assertNotIn("  git ", out)
            self.assertFalse([l for l in out.splitlines() if "`" in l or "$(" in l], out)
            # The control, a project without such a name: exit 0 and no such line.
            home = bare_home(root, "beta")
            rc, out = quiet(mirror.main, ["restore", "--to", home, "someone/beta"])
            self.assertEqual(rc, 0, out)
            self.assertNotIn("not in the new home", out)
            self.assertNotIn("  git ", out)
            self.assertEqual(lsremote(home), w.gh_refs("someone/beta"))

    def test_G7_9_restore_says_what_the_new_home_lacks_from_githubs_last_list(self):
        """M135 (a reviewer's world 18, 2026-11-25): GitHub held a branch the copy did not (a name that is not text),
        and restore --to said "restored ... 3 branch(es)" and exited 0, with no word of it. Now restore --to reads
        GitHub's list as the sync last read it, as restore --check does, and says what the new home lacks of it: a
        count, the names through the shown form, why where the record knows why; exit 1. A record without that list
        is said as not read, never as none missing. What restore pushes does not change."""
        with World(self) as w:
            full = "someone/aa"
            w.gh.add_repo("someone", "aa")
            w.gh.add_repo("someone", "bb")
            one = w.gh.commit(full, {"a": b"1"})
            w.gh.git(full, "tag", "v1", one)
            for b in ("b1", "c`x`", "d`y", "d\u200by"):           # the last two show alike: they stay two
                w.gh.git(full, "branch", b, one)
            w.gh.commit("someone/bb", {"a": b"1"})
            odd = disk_holds(os.path.dirname(w.p["state"]), "a\udcffb")   # a Mac's disk cannot hold it
            if odd:
                w.gh.plant(full, "refs/heads/a\udcffb", "main")
            w.key_set()
            rc, out = w.first_sync()
            self.assertEqual(rc, 0, out)
            # Taken out of the copy behind the tool's back (the record does not know why), and kept by GitHub.
            folder = os.path.join(w.p["forge"], "data", "gitea-repositories", "mirror", "someone--aa.git")
            for b in ("b1", "c`x`", "d`y", "d\u200by"):
                subprocess.run(["git", "--git-dir", folder, "update-ref", "-d", "refs/heads/" + b], check=True)
            root = tempfile.mkdtemp(prefix="wsm-home-")
            TEMPS.append(root)
            home = bare_home(root, "aa")
            n = len(w.gh.log)
            rc, out = quiet(mirror.main, ["restore", "--to", home, full])
            self.assertEqual(rc, 1, out)
            self.assertIn(f"restored {full}: 1 branch(es) and 1 tag(s) to {home}; the kept refs stay in your forge\n",
                          out)
            names = (["a?b"] if odd else []) + ["b1", "c?x?", "d?y", "d?y"]
            self.assertIn(mirror.RESTORE_LACKS.format(
                n=len(names), names=", ".join(names) + "; " + mirror.SHOWN_CHANGED,
                why="; 1 of them are not UTF-8 text, which the forge cannot hold" if odd else ""), out)
            self.assertFalse([l for l in out.splitlines() if "`" in l or "$(" in l], out)
            self.assertEqual(set(lsremote(home)), {"refs/heads/main", "refs/tags/v1"})   # what it pushes: as before
            self.assertEqual(w.gh.log[n:], [], "restore asked GitHub")
            # The control: a project the copy holds whole is restored with exit 0 and no such line.
            home = bare_home(root, "bb")
            rc, out = quiet(mirror.main, ["restore", "--to", home, "someone/bb"])
            self.assertEqual(rc, 0, out)
            self.assertNotIn("but ", out)
            # A record without GitHub's list: said as not read, never as none missing; the exit is not 0.
            st = mirror.load_json(w.p["state"], {})
            rec = next(r for r in st["repos"].values() if r["full_name"] == "someone/bb")
            rec.pop("seen_refs", None)
            rec.pop("refs", None)
            mirror.save_state(w.p, st)
            home = bare_home(root, "bb-again")
            rc, out = quiet(mirror.main, ["restore", "--to", home, "someone/bb"])
            self.assertEqual(rc, 1, out)
            self.assertIn(mirror.RESTORE_LIST_UNREAD.format(full="someone/bb"), out)
            self.assertEqual(set(lsremote(home)), {"refs/heads/main"})

    def test_G7_8_the_line_to_put_a_copy_back_on_github_runs_as_printed(self):
        """M132 (a reviewer's read, 2026-11-25): restore --to refuses GitHub and said how to do it yourself with a
        clone over the forge's address, which asks for a login the text never names, and "cd <that folder>". Now it
        gives one line that runs as printed: git pushes the copy as a mirror from the copy's own folder in the forge
        (on this computer, no login there) to the new, empty GitHub repository, each part through shlex.quote. It
        sends what the old pair would have sent. Run here against a local bare folder standing in for the new home,
        through git's own insteadOf, with no call to GitHub."""
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            one = w.gh.commit("someone/alpha", {"a": b"1"})
            w.gh.git("someone/alpha", "tag", "v1", one)
            w.gh.git("someone/alpha", "-c", "user.email=a@example.invalid", "-c", "user.name=a",
                     "tag", "-a", "-m", "a note", "v2", one)
            w.gh.commit("someone/alpha", {"a": b"2"})
            w.gh.git("someone/alpha", "branch", "topic", one)
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            w.gh.git("someone/alpha", "update-ref", "refs/heads/main", one)   # main moved back: its old commit kept
            w.gh.repos["someone/alpha"]["pushed_at"] = "2002-01-01T00:00:00Z"
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            folder = os.path.join(w.p["forge"], "data", "gitea-repositories", "mirror", "someone--alpha.git")
            held = git_all_refs(folder)
            self.assertTrue([r for r in held if r.startswith("refs/heads/kept/")], held)   # the premise: kept refs
            url = "https://github.com/someone/new.git"
            n = len(w.gh.log)
            rc, out = quiet(mirror.main, ["restore", "--to", url, "someone/alpha"])
            self.assertEqual(rc, 2, out)
            self.assertIn("This skill never writes to GitHub", out)
            self.assertIn("make a new, empty repository there", out)
            lines = [l for l in out.splitlines() if l.startswith("  ")]
            self.assertEqual(lines, [f"  git --git-dir {shlex.quote(folder)} push --mirror {shlex.quote(url)}"], out)
            self.assertNotIn("<", out)                                 # nothing for the person to fill in
            self.assertNotIn("clone", out)
            # Run as printed, in an empty folder, nothing added but git's own address rewrite to the stand-in home.
            root = tempfile.mkdtemp(prefix="wsm-home-")
            TEMPS.append(root)
            home = bare_home(root, "new")
            here = tempfile.mkdtemp(prefix="wsm-shell-")
            TEMPS.append(here)
            road = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="1",
                        GIT_CONFIG_KEY_0=f"url.{home}.insteadOf", GIT_CONFIG_VALUE_0=url)
            r = subprocess.run(["bash", "-c", lines[0].strip()], cwd=here, capture_output=True, text=True, env=road,
                               stdin=subprocess.DEVNULL)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(os.listdir(here), [])
            self.assertEqual(git_all_refs(folder), held, "the line changed the copy")
            self.assertEqual(git_all_refs(home), held)
            # What the old pair would have sent: a mirror clone over the forge's address (with the tool's own login
            # here, which a person did not have), then a mirror push into a second empty home.
            key = fread(w.p["mirror_key"]).strip()
            hdr = "Authorization: Basic " + __import__("base64").b64encode(f"mirror:{key}".encode()).decode()
            old = os.path.join(root, "old.git")
            subprocess.run(["git", "-c", f"http.http://127.0.0.1:{w.port}/.extraheader={hdr}", "clone", "-q",
                            "--mirror", f"http://127.0.0.1:{w.port}/mirror/someone--alpha.git", old],
                           check=True, capture_output=True, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
            home2 = bare_home(root, "new2")
            subprocess.run(["git", "--git-dir", old, "push", "-q", "--mirror", home2], check=True, capture_output=True)
            self.assertEqual(git_all_refs(home), git_all_refs(home2))
            self.assertEqual(w.gh.log[n:], [], "the refused restore talked to GitHub")
            self.assertEqual(w.gh.writes(), [])
            # No copy, or a folder this tool does not print as it is: no line, and what to do.
            for repo, words in (("someone/nothere", "could not be found"),):
                rc, out = quiet(mirror.main, ["restore", "--to", url, repo])
                self.assertEqual(rc, 2, out)
                self.assertIn(words, out)
                self.assertNotIn("  git ", out)
            real = mirror.forge_folder
            mirror.forge_folder = lambda p, forge, name: folder + "$(id>m4)"
            try:
                rc, out = quiet(mirror.main, ["restore", "--to", url, "someone/alpha"])
            finally:
                mirror.forge_folder = real
            self.assertEqual(rc, 2, out)
            self.assertIn("does not print as it is", out)
            self.assertNotIn("  git ", out)
            # The --to value (a reviewer's world 20): typed by a person or their agent, and passed on. Each line is
            # read both ways: a git line run as printed, and every line relayed inside double quotes, as an agent
            # passes a tool's line on; a command inside the value never runs, and no line carries either form.
            def relayed(out):
                here = tempfile.mkdtemp(prefix="wsm-relay-")
                TEMPS.append(here)
                for l in out.splitlines():
                    if l.startswith("  git "):
                        subprocess.run(["bash", "-c", l.strip()], cwd=here, capture_output=True,
                                       env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), stdin=subprocess.DEVNULL)
                    subprocess.run(["bash", "-c", 'printf "%s\\n" "' + l.replace('"', '\\"') + '"'], cwd=here,
                                   capture_output=True, stdin=subprocess.DEVNULL)
                return os.listdir(here)
            forms = lambda out: [l for l in out.splitlines() if "`" in l or "$(" in l or "$" + "?" in l]
            for bad in ("https://github.com/someone/n$(touch m1).git", "https://github.com/someone/n`touch m2`.git",
                        "git@github.com:someone/n$(touch m3).git"):
                rc, out = quiet(mirror.main, ["restore", "--to", bad, "someone/alpha"])
                self.assertEqual(rc, 2, out)
                self.assertIn("This skill never writes to GitHub", out)
                self.assertIn("The address given holds a character this tool does not print as it is", out)
                self.assertIn(mirror.SHOWN_CHANGED, out)
                self.assertNotIn("  git ", out)
                self.assertEqual(forms(out), [], out)
                self.assertEqual(relayed(out), [], (bad, out))
            # A new home on this computer whose path holds such a character: the restore itself works (a person's
            # folder name is theirs), and the line that names it shows it changed and says so.
            w.gh.add_repo("someone", "beta")
            w.gh.commit("someone/beta", {"a": b"1"})
            w.gh.git("someone/alpha", "branch", "kept/x", one)            # M128's lines, with such a home
            w.gh.repos["someone/alpha"]["pushed_at"] = "2003-01-01T00:00:00Z"
            self.assertEqual(w.sync()[0], 0)
            odd = os.path.join(root, "h$(touch m4)`touch m5`")
            os.makedirs(odd)
            for repo, rc_want in (("someone/beta", 0), ("someone/alpha", 1)):
                home = bare_home(odd, repo.split("/")[1])
                rc, out = quiet(mirror.main, ["restore", "--to", home, repo])
                self.assertEqual(rc, rc_want, out)
                self.assertIn(f"restored {repo}: ", out)
                self.assertIn(f" to {mirror.shown_as_is(home)}; the kept refs stay in your forge ({mirror.SHOWN_CHANGED})",
                              out)
                self.assertEqual({r: v for r, v in lsremote(home).items() if not r.endswith("^{}")},
                                 {r: v for r, v in w.gh_refs(repo).items() if not r.startswith("refs/heads/kept/")})
                self.assertEqual(forms(out), [], out)
                self.assertEqual(relayed(out), [], (repo, out))
                if repo == "someone/alpha":
                    self.assertIn(mirror.RESTORE_TO_HIDDEN + f"\n  git --git-dir {shlex.quote(folder)} for-each-ref "
                                  "refs/heads/kept/\n", out)
                    self.assertFalse([l for l in out.splitlines() if l.startswith("  git ") and " push " in l], out)
            self.assertNotIn("$(", out)

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
                h2.auto_login = mirror.getpass.getuser()   # a Mac that logs this account in by itself, stood in
                h2.place_gitea()
                rc, out = restore_with_the_forge_answering(h2, backup)
                self.assertEqual(rc, 0, out)
                self.assertIn("2 copies are back, on http://127.0.0.1:", out)
                self.assertIn("counted in the forge itself", out)
                self.assertIn("The forge runs as your own service.", out)
                self.assertIn("After a restart it starts by itself.", out)
                self.assertNotIn(mirror.NO_LOGIN_NOTE, out)
                self.assertIn("make new keys and run key set for each owner: someone", out)
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
            # The other answer of the same read (the Mac job, 2026-11-24): a Mac with no automatic login, or Linux
            # with lingering refused, is told the copy stands still until the person logs in, never that it starts.
            with Home(linger="no", enable_linger_ok=False) as h3:
                h3.place_gitea()
                rc, out = restore_with_the_forge_answering(h3, backup)
                self.assertEqual(rc, 0, out)
                self.assertIn(mirror.NO_LOGIN_NOTE, out)
                self.assertNotIn("After a restart it starts by itself.", out)

    def test_G7_4_restore_says_back_and_runs_only_from_what_the_forge_answered(self):
        """M88 (rule 24): restore's lines are read from the forge at that moment. Each thing is taken away in turn
        while the restored record stays: the forge that does not answer, a record naming more copies than the forge
        holds, and a service that could not be installed."""
        with World(self) as w:
            self.world_with_copies(w)
            folder = tempfile.mkdtemp(prefix="wsm-bk-")
            TEMPS.append(folder)
            self.assertEqual(quiet(mirror.main, ["backup", folder])[0], 0)
            backup = os.path.join(folder, os.listdir(folder)[0])
            w.gh.stop()
            # 1. The data is in place and the forge does not answer: never "back", never "runs", not 0.
            with Home(linger="yes") as h2:
                h2.place_gitea()
                rc, out = h2.setup("--from", backup)
                self.assertEqual(rc, 1, out)
                self.assertIn("2 copies are in the restored data; the forge has not answered, so they are not counted "
                              "there yet.", out)
                self.assertNotIn("are back", out)
                self.assertNotIn("The forge runs as your own service.", out)
                self.assertIn("it was not answering yet", out)
            # 2. The record names a copy the forge does not hold: both numbers and "Run status", not 0.
            more = os.path.join(folder, "more-in-the-record.tar")
            work = tempfile.mkdtemp(prefix="wsm-bk-more-")
            TEMPS.append(work)
            with tarfile.open(backup) as tf:
                tf.extractall(work, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
            stp = os.path.join(work, "skill", "state.json")
            st = json.loads(fread(stp))
            st["repos"]["999999"] = {"full_name": "someone/gone-from-the-forge", "forge_id": 987654}
            with open(stp, "w", encoding="utf-8") as fh:
                json.dump(st, fh)
            with tarfile.open(more, "w") as tf:
                for x in sorted(os.listdir(work)):
                    tf.add(os.path.join(work, x), arcname=x)
            with Home(linger="yes") as h3:
                h3.place_gitea()
                rc, out = restore_with_the_forge_answering(h3, more)
                self.assertEqual(rc, 1, out)
                self.assertIn("the forge holds 2 of the 3 copies in the restored record", out)
                self.assertIn("Run status: it names each copy that is not there.", out)
                self.assertNotIn("are back", out)
                self.assertIn("The forge runs as your own service.", out)
            # 3. The service could not be installed: no "starts by itself" after it, in restore and in setup.
            with Home(linger="yes", service_ok=False) as h4:
                h4.place_gitea()
                rc, out = h4.setup("--from", backup)
                self.assertEqual(rc, 1, out)
                self.assertIn("The forge's service did not start", out)
                self.assertNotIn("After a restart it starts by itself.", out)
                self.assertNotIn("are back", out)
            with Home(linger="yes", service_ok=False) as h5:
                h5.place_gitea()
                rc, out = h5.setup()
                self.assertEqual(rc, 1, out)
                self.assertIn("The forge's service did not start", out)
                self.assertNotIn("After a restart it starts by itself.", out)


def restore_with_the_forge_answering(home, backup):
    """setup --from with the stand-in service start followed by the real forge, and restore given time to read it:
    the road where the forge answers, as on a person's computer."""
    real, wait = mirror.install_service, mirror.SETUP_WAIT

    def install_and_start(p, **kw):
        r = real(p, **kw)
        if r[0]:
            home.start_forge()
        return r
    mirror.install_service, mirror.SETUP_WAIT = install_and_start, 30
    try:
        return home.setup("--from", backup)
    finally:
        mirror.install_service, mirror.SETUP_WAIT = real, wait


STRANGER = r'''
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def answer(self):
        print("REQ", self.command, self.path.split("?")[0], "auth=%d" % ("Authorization" in self.headers), flush=True)
        body = b'{"version": "28.4.0"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = answer
import errno, time
start = time.monotonic()
refused = 0
while True:
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H)
        break
    except OSError as e:
        if e.errno != errno.EADDRINUSE or time.monotonic() - start > 90:
            print("NOT LISTENING after %.1f s, %d refusal(s) of Address already in use" % (time.monotonic() - start,
                  refused), flush=True)
            raise
        refused += 1
        time.sleep(0.5)
print("LISTENING after %.1f s, %d refusal(s) of Address already in use" % (time.monotonic() - start, refused),
      flush=True)
srv.serve_forever()
'''


class Stranger:
    """G8.7: another program on the forge's port, answering every request as a forge would (200, a version) and
    writing down each one it is sent, and whether it carried an Authorization header. as_account: the account it runs
    as (another account, by sudo without a password), or None for this account's own. Its bind waits up to 90 s on
    "Address already in use": seen on GitHub's Mac runners at 649ee99, where the forge's port, free to this account,
    was refused to the stranger; a Mac keeps another account off a port this account's closed connections still
    hold (TIME_WAIT), where Linux does not (our reading; the next Mac leg is its test). It prints how long it waited
    and how many refusals; the test writes that into the run's log whenever it was refused at all."""

    def __init__(self, port, as_account=None):
        py = "/usr/bin/python3" if as_account and os.path.exists("/usr/bin/python3") else sys.executable
        cmd = [py, "-I", "-c", STRANGER, str(port)]
        if as_account:
            cmd = ["sudo", "-n", "-u", as_account, *cmd]
        self.pr = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd="/")
        first = self.pr.stdout.readline()
        who = as_account or "this account"
        if not first.startswith("LISTENING"):
            self.pr.kill()
            raise AssertionError(f"the stranger ({who}) did not listen on {port}: "
                                 f"{first}{self.pr.stdout.read()[-300:]}")
        self.refusals = int(first.split(", ")[1].split()[0])
        if self.refusals:                       # the run's log says the wait, so a Mac leg can be read for it
            print(f"\nG8.7: the stranger ({who}) on 127.0.0.1:{port}: {first.strip()}", file=sys.stderr, flush=True)
        self.seen = []

    def close(self):
        if self.pr.poll() is None:
            self.pr.terminate()
            try:
                self.pr.wait(10)
            except subprocess.TimeoutExpired:
                self.pr.kill()
                self.pr.wait(10)
        self.seen = [x for x in self.pr.stdout.read().splitlines() if x.startswith("REQ ")]
        return self.seen


def stop_forge(h):
    """The forge stops (its service told to stop), as a person's computer might do it, and the port is free."""
    for pr in h.procs:
        if pr.poll() is None:
            pr.terminate()
            pr.wait(30)
    port = int(mirror.ini_value(mirror.paths()["app_ini"], "server", "HTTP_PORT"))
    end = time.monotonic() + 30
    while not mirror.port_is_free(port):
        if time.monotonic() > end:
            raise AssertionError(f"the forge did not let go of {port}")
        time.sleep(0.2)
    mirror.DOOR_SEEN.clear()
    return port


def backup_of_one_copy(tc):
    """A backup file of a forge holding one copy (Gitea's dump needs a copy to hold), made by the skill's own backup."""
    with World(tc) as w:
        w.gh.add_repo("someone", "alpha")
        w.gh.commit("someone/alpha", {"a": b"1"})
        tc.assertEqual(w.key_set()[0], 0)
        tc.assertEqual(w.first_sync()[0], 0)
        folder = tempfile.mkdtemp(prefix="wsm-bk-")
        TEMPS.append(folder)
        w.home.forge_follows = True
        rc, out = quiet(mirror.main, ["backup", folder])
        tc.assertEqual(rc, 0, out)
        return os.path.join(folder, os.listdir(folder)[0])


def said(out):
    """What a command said, without the line every command in the suite starts with (WSM_HOME is set: ...)."""
    return "\n".join(x for x in out.strip().splitlines() if not x.startswith("WSM_HOME is set:"))


def other_account():
    """The account another program runs as for the second half of G8.7's proof: WSM_OTHER_ACCOUNT (CI names
    'nobody', and its runners lend sudo without a password). Where it is not named, that half is not run here and the
    test says so; on CI it must be named."""
    name = os.environ.get("WSM_OTHER_ACCOUNT")
    if not name:
        if os.environ.get("CI"):
            raise AssertionError("CI runs G8.7 with another account: WSM_OTHER_ACCOUNT is not set")
        return None
    out = subprocess.run(["sudo", "-n", "-u", name, "true"], capture_output=True, text=True)
    if out.returncode != 0:
        raise AssertionError(f"WSM_OTHER_ACCOUNT={name}, and sudo -n -u {name} does not run: {out.stderr[-200:]}")
    return name


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
            # The count is of the addresses status itself read: on a computer whose addresses come and go (a VPN, a
            # virtual machine's bridge), a count read earlier by the test can differ by then (seen 2026-11-23).
            read, real = [], mirror.own_addresses
            mirror.own_addresses = lambda: read.append(real()) or read[-1]
            try:
                rc, out = status()
            finally:
                mirror.own_addresses = real
            self.assertEqual(rc, 0, out)
            self.assertTrue(read, "status did not read this computer's addresses")
            now = [a for a in read[-1] if not (a == "::1" or a.startswith("127."))]
            self.assertIn(f"network: the forge answers on 127.0.0.1 only (tried {len(now)} other address(es)", out)
            saved = mirror.answers_on
            mirror.answers_on = lambda a, p: ["192.0.2.7"]
            try:
                rc, out = status()
            finally:
                mirror.answers_on = saved
            self.assertEqual(rc, 1)
            self.assertIn("problem: the forge answers on 192.0.2.7, not only on 127.0.0.1", out)

    def test_G8_4_the_forge_never_writes_what_may_log_in_to_this_computer(self):
        """M64 (seen 2026-11-23 with the real pinned Gitea): stock Gitea writes the account's ~/.ssh/authorized_keys
        when it starts, even with SSH off: an empty one made where there was none, the person's own rewritten. With
        the forge's settings it is never touched, and nothing is made in ~/.ssh."""
        for had_one in (True, False):
            with Home(linger="yes") as h:
                ssh = os.path.join(os.environ["HOME"], ".ssh")
                ak = os.path.join(ssh, "authorized_keys")
                if had_one:
                    os.makedirs(ssh, mode=0o700)
                    with open(ak, "w") as f:
                        f.write("ssh-ed25519 AAAAC3NzaPERSONSOWNKEY person@laptop\n")
                    os.chmod(ak, 0o600)
                    before = os.stat(ak)
                h.place_gitea()
                rc, out = h.setup()
                self.assertEqual(rc, 0, out)
                h.start_forge()
                time.sleep(2)                                     # past the forge's own start-up writes
                if had_one:
                    after = os.stat(ak)
                    self.assertEqual((after.st_ino, after.st_mtime_ns, after.st_size),
                                     (before.st_ino, before.st_mtime_ns, before.st_size),
                                     "the forge rewrote the person's authorized_keys")
                    self.assertEqual(fread(ak), "ssh-ed25519 AAAAC3NzaPERSONSOWNKEY person@laptop\n")
                else:
                    self.assertFalse(os.path.exists(ssh), "the forge made ~/.ssh in the person's home")
                ini = fread(mirror.paths()["app_ini"])
                for line in ("SSH_CREATE_AUTHORIZED_KEYS_FILE = false", "SSH_CREATE_AUTHORIZED_PRINCIPALS_FILE = false",
                             f"SSH_ROOT_PATH = {mirror.paths()['forge']}/ssh"):
                    self.assertIn(line, ini)

    def test_G8_6_the_skills_own_roads_take_no_proxy_and_run_no_hook_of_the_persons(self):
        """M76 (seen 2026-11-24, a lived-in sighting in the suite): with http.proxy at a dead address in the person's
        git config, the skill's git sent the push to its own forge on 127.0.0.1 to that proxy and the sync stopped,
        0 copied; with http_proxy in the environment, the skill's calls to its own forge went the same way and the
        sync stopped at its first step; with core.hooksPath set, the person's hooks ran inside the skill's own git.
        Now, with both proxies dead from before the forge starts (as from a person's shell), GitHub's stand-in
        exempted as a working proxy would carry GitHub, marking hooks in core.hooksPath and in init.templateDir,
        and a project with large files: the sync copies everything, status says nothing is wrong, and no hook of the
        person's ran. Every road to the forge goes through forge_call (git and git-lfs) or forge_opener (urllib)."""
        lfs = mirror.has_git_lfs()
        if not lfs and os.environ.get("WSM_REQUIRE_GITEA"):
            raise AssertionError("git lfs is needed for this test in CI")
        data = os.urandom(120_000)
        dead = "http://127.0.0.1:9"
        proxies = {k: dead for k in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
        proxies.update(no_proxy="localhost", NO_PROXY="localhost")
        saved = {k: os.environ.get(k) for k in proxies}
        os.environ.update(proxies)                       # from before the forge starts, as from a person's shell
        urllib.request.install_opener(None)              # urllib builds its default opener, proxies and all, anew
        try:
            with World(self) as w:
                home = os.environ["HOME"]
                marks = os.path.join(home, "marks")
                os.makedirs(marks)
                hooks, template = os.path.join(home, "my-hooks"), os.path.join(home, "my-template")
                for d in (hooks, os.path.join(template, "hooks")):
                    os.makedirs(d)
                    for h in ("pre-push", "reference-transaction", "post-update", "pre-receive", "post-checkout"):
                        with open(os.path.join(d, h), "w") as f:
                            f.write(f"#!/bin/sh\necho \"$0\" >> {marks}/hook\ncat >/dev/null 2>&1\nexit 0\n")
                        os.chmod(os.path.join(d, h), 0o755)
                for k in ("WSM_GITHUB_API", "WSM_GITHUB_GIT"):            # GitHub's stand-in, carried
                    os.environ[k] = os.environ[k].replace("127.0.0.1", "localhost")
                gh = os.environ["WSM_GITHUB_GIT"].rstrip("/")
                with open(os.path.join(home, ".gitconfig"), "w") as f:
                    f.write(f'[http]\n\tproxy = {dead}\n[http "{gh}/"]\n\tproxy = \n[core]\n\thooksPath = {hooks}\n'
                            f'[init]\n\ttemplateDir = {template}\n')
                w.gh.add_repo("someone", "alpha")
                w.gh.commit("someone/alpha", {"a": b"1"})
                if lfs:
                    w.gh.add_repo("someone", "big")
                    w.gh.commit("someone/big", {".gitattributes": b"*.bin filter=lfs diff=lfs merge=lfs -text\n",
                                                 "blob.bin": w.gh.lfs_file(data)})
                w.key_set()
                rc, out = w.first_sync()
                self.assertEqual(rc, 0, out)
                w.gh.commit("someone/alpha", {"b": b"2"})
                rc, out = w.sync()
                self.assertEqual(rc, 0, out)
                rc, st = quiet(mirror.main, ["status"])
                self.assertEqual(rc, 0, st)
                rc, chk = quiet(mirror.main, ["restore", "--check"])
                self.assertEqual(rc, 0, chk)
                # The older way of global hooks alone: init.templateDir, which git copies into each new cache.
                with open(os.path.join(home, ".gitconfig"), "w") as f:
                    f.write(f'[http]\n\tproxy = {dead}\n[http "{gh}/"]\n\tproxy = \n[init]\n\ttemplateDir = {template}\n')
                w.gh.add_repo("someone", "gamma")
                w.gh.commit("someone/gamma", {"g": b"1"})
                rc, out = w.sync()
                self.assertEqual(rc, 0, out)
                self.assertTrue(any(os.path.exists(os.path.join(c, "hooks", "pre-push"))
                                    for c in [os.path.join(w.p["cache"], x) for x in os.listdir(w.p["cache"])]),
                                "git copied no hook from the template: the case did not happen")
                self.assertFalse(os.path.exists(os.path.join(marks, "hook")),
                                 "a hook of the person's ran: " + (fread(os.path.join(marks, "hook"))
                                                                   if os.path.exists(os.path.join(marks, "hook")) else ""))
                for k, v in saved.items():                       # the suite's own reads take no proxy
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                os.remove(os.path.join(home, ".gitconfig"))
                self.assertEqual(w.forge_refs("someone--alpha"), w.gh_refs("someone/alpha"))
                self.assertEqual(w.forge_refs("someone--gamma"), w.gh_refs("someone/gamma"))
                if lfs:
                    key = fread(w.p["mirror_key"]).strip()
                    req = urllib.request.Request(f"http://127.0.0.1:{w.port}/api/v1/repos/mirror/someone--big/media/blob.bin",
                                                 headers={"Authorization": f"token {key}"})
                    no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    self.assertEqual(no_proxy.open(req, timeout=10).read(), data)
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            urllib.request.install_opener(None)          # no later test inherits an opener built with the dead proxies
        # Every road to the forge goes through forge_call or forge_opener; every git runs with hooks off.
        src = fread(SCRIPT)
        body = re.search(r"\ndef forge_call\(.*?(?=\n\S)", src, re.S).group(0)
        rest = src.replace(body, "")
        self.assertEqual(re.findall(r"git_env\(\)", rest.replace("def git_env(self)", "")), [],
                         "the forge's key handed to git around forge_call")
        calls = re.findall(r"(?<!def )\bgit\((?:[^()]|\([^()]*\))*\)", rest)
        self.assertEqual([c for c in calls if '"forge"' in c or '"origin"' in c or "forge.base" in c], [],
                         "a git call names the forge's address around forge_call")
        self.assertEqual(re.findall(r"urllib\.request\.(?:urlopen|build_opener)\([^)]*(?:127\.0\.0\.1|self\.base)", src), [],
                         "a call to the forge around forge_opener")
        self.assertIn('"-c", f"core.hooksPath={os.devnull}"', inspect.getsource(mirror.git))

    def test_G8_5_the_forge_never_writes_or_follows_a_git_config_of_the_persons(self):
        """M73 (seen 2026-11-24 with the real pinned Gitea, by T6's guard): the forge was handed the whole
        environment, and stock Gitea's git took an inherited GIT_CONFIG_GLOBAL over its own git home and wrote its
        user.name and gc lines into that file. With git's settings of the person's set (GIT_CONFIG_GLOBAL, and a
        GIT_CONFIG_COUNT pair), setup and the forge's start leave the person's file as it was, and the forge's own
        settings go in its own folder."""
        with Home(linger="yes") as h:
            mine = os.path.join(os.environ["HOME"], "my-gitconfig")
            with open(mine, "w") as f:
                f.write("[user]\n\tname = The Person\n")
            before = os.stat(mine)
            was = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                                                  "GIT_CONFIG_VALUE_0")}
            os.environ.update(GIT_CONFIG_GLOBAL=mine, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="user.email",
                              GIT_CONFIG_VALUE_0="person@example.invalid")
            try:
                h.place_gitea()
                rc, out = h.setup()
                self.assertEqual(rc, 0, out)
                h.start_forge()
                time.sleep(2)                                     # past the forge's own start-up writes
            finally:
                for k, v in was.items():
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            after = os.stat(mine)
            self.assertEqual(fread(mine), "[user]\n\tname = The Person\n", "the forge wrote the person's git config")
            self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
            forge = mirror.paths()["forge"]
            own = [os.path.join(r, f) for r, _, fs in os.walk(forge) for f in fs if f == ".gitconfig"]
            self.assertTrue(any("name = Gitea" in fread(x) for x in own), f"the forge's own git settings: {own}")
            self.assertFalse(any("person@example.invalid" in fread(x) for x in own), "the forge followed the person's")
        # Git's other global file: the person's XDG_CONFIG_HOME holding git/config, and no GIT_CONFIG_GLOBAL set.
        with Home(linger="yes") as h:
            xdg = os.path.join(os.environ["HOME"], "my-xdg")
            mine = os.path.join(xdg, "git", "config")
            os.makedirs(os.path.dirname(mine))
            with open(mine, "w") as f:
                f.write("[user]\n\temail = person@example.invalid\n")
            before = os.stat(mine)
            was = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME", "GIT_CONFIG_COUNT")}
            os.environ.pop("GIT_CONFIG_GLOBAL")
            try:
                h.place_gitea()
                rc, out = h.setup()
                self.assertEqual(rc, 0, out)
                os.environ["XDG_CONFIG_HOME"] = xdg             # set for the forge's start, as a person's shell may
                os.environ["GIT_CONFIG_COUNT"] = "0"
                _, pr = h.start_forge()
                time.sleep(2)
                if os.path.exists(f"/proc/{pr.pid}/environ"):    # Linux: what the running forge was handed
                    names = [x.split(b"=", 1)[0].decode() for x in fread_bytes(f"/proc/{pr.pid}/environ").split(b"\0") if x]
                    self.assertEqual([n for n in names if n.upper().startswith("GIT_") or n == "XDG_CONFIG_HOME"], [],
                                     "the forge's start hands on git's settings")
                    self.assertIn("GITEA_WORK_DIR", names)
            finally:
                for k, v in was.items():
                    os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            after = os.stat(mine)
            self.assertEqual(fread(mine), "[user]\n\temail = person@example.invalid\n", "the forge wrote git's XDG file")
            self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))
            forge = mirror.paths()["forge"]
            own = [os.path.join(r, f) for r, _, fs in os.walk(forge) for f in fs if f == ".gitconfig"]
            self.assertTrue(any("name = Gitea" in fread(x) for x in own), f"the forge's own git settings: {own}")
        # Each of the forge's two roads holds by itself: the forge commands setup runs hand the forge none of git's
        # settings either (the service's start is the road above).
        seen = []
        real_run = mirror.run
        mirror.run = lambda cmd, **kw: seen.append(kw.get("env")) or subprocess.CompletedProcess(cmd, 0, "", "")
        os.environ["GIT_CONFIG_GLOBAL_PROBE"] = "x"
        try:
            with Home() as h:
                os.environ["GIT_DIR"] = os.path.join(h.tmp, "not-the-forges")
                try:
                    mirror.gitea(mirror.paths(), "--version")
                finally:
                    os.environ.pop("GIT_DIR")
                self.assertIn("XDG_CONFIG_HOME", os.environ)
        finally:
            mirror.run = real_run
            os.environ.pop("GIT_CONFIG_GLOBAL_PROBE")
        self.assertEqual(len(seen), 1)
        self.assertEqual(sorted(k for k in seen[0] if k.upper().startswith("GIT_") or k == "XDG_CONFIG_HOME"), [],
                         "gitea() hands on git's settings")
        self.assertIn("GITEA_WORK_DIR", seen[0])

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


    def test_G8_7_a_mac_process_of_the_account_nobody_is_another_programs(self):
        """Seen 2026-11-24 in GitHub's Mac runner (M102's stranger, run as `nobody`): `ps -o uid=` prints that account
        as -2 on macOS (read on another Mac too: `id -u nobody` 4294967294, ps -2), and the door read said "could not
        be read" of a port another program held. A uid with its sign is read as a uid, another account's; anything
        that is not a number stays "could not be read"."""
        saved = mirror.subprocess.run
        answers = {}
        def fake(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, answers["uid" if "uid=" in cmd else "args"], "")
        mirror.subprocess.run = fake
        try:
            answers.update(uid="   -2 /usr/bin/python3\n", args="python3 stranger.py\n")
            self.assertEqual(mirror.mac_proc(4242), (-2, "/usr/bin/python3", "python3 stranger.py"))
            for bad in ("   - /usr/bin/python3\n", "  x2 /usr/bin/python3\n", "   -2\n", ""):
                answers["uid"] = bad
                with self.assertRaises(mirror.DoorUnread, msg=bad):
                    mirror.mac_proc(4242)
        finally:
            mirror.subprocess.run = saved

    def test_G8_7_nothing_with_the_key_goes_to_a_port_that_is_not_the_forges_own(self):
        """M102 (the owner's ruling, 11-24): every road reads who holds the forge's port first; another program there,
        of this account or of another, gets nothing: no request at all, so no key and no copy. sync and status each
        say it in one line with one next step, the sync's own record says it, and setup moves the forge to a free
        port only while its own forge is stopped, saying the new address."""
        if not real_gitea() and not os.environ.get("WSM_REQUIRE_GITEA"):
            raise unittest.SkipTest("no real pinned Gitea for this system")
        other = other_account()
        with contextlib.ExitStack() as stack:
            w = stack.enter_context(World(self))
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            self.assertEqual(w.key_set()[0], 0)
            self.assertEqual(w.first_sync()[0], 0)
            self.assertEqual(mirror.read_door(w.p, w.port), ("mine", "this mirror's own forge holds it"))
            backups = tempfile.mkdtemp(prefix="wsm-bk-")
            TEMPS.append(backups)
            rounds = [(None, "a program of this account that is not this mirror's forge holds it")]
            if other:
                rounds.append((other, "a program of another account holds it"))
            else:
                print("\nG8.7: the other account's half is not run here (WSM_OTHER_ACCOUNT unset); CI runs it",
                      file=sys.stderr)
            for who, why in rounds:
                w.home.forge_follows = False
                port = stop_forge(w.home)
                s = Stranger(port, who)
                stack.callback(s.close)
                self.assertEqual(mirror.read_door(w.p, port), ("other", why))
                line = (f"problem: another program answers on the forge's port 127.0.0.1:{port}, not this mirror's own "
                        f"forge ({why}), so nothing was sent to it: no key, no copy. Run setup: it moves the forge "
                        "to a free port and starts it again; every copy stays.")
                rc, out = w.sync()
                self.assertEqual((rc, said(out)), (1, line))
                self.assertEqual(mirror.load_json(w.p["state"], {})["last_sync"]["stopped"],
                                 "another program holds the forge's port")
                rc, out = quiet(mirror.main, ["status"])
                self.assertEqual(rc, 1)
                self.assertEqual([x for x in out.splitlines() if x.startswith("problem:")], [line], out)
                self.assertIn("last sync: ", out)
                self.assertIn("stopped: another program holds the forge's port", out)
                for cmd in (["restore", "--check"], ["restore", "--to", "/nowhere/new.git", "someone/alpha"]):
                    rc, out = quiet(mirror.main, cmd)
                    self.assertEqual((rc, said(out)), (1, line), cmd)
                with pinned("28.4.1", mirror.PINS):
                    rc, out = quiet(mirror.main, ["upgrade-forge"])
                self.assertEqual((rc, said(out)), (1, line + " Nothing was upgraded."))
                self.assertEqual(mirror.load_json(w.p["state"], {})["gitea"], mirror.GITEA_VERSION)
                rc, out = quiet(mirror.main, ["backup", backups])        # the forge is stopped: a backup of it is whole
                self.assertEqual(rc, 1, out)                              # its service cannot start on a held port
                self.assertIn("backup: ", out)
                self.assertEqual(said(out).splitlines()[-1], line)
                self.assertEqual(w.key_set()[0], 0)
                self.assertEqual(s.close(), [], f"the stranger was sent requests ({who or 'this account'})")
                # setup: moves the forge only now that its own is stopped, says the new address, and starts it there
                s = Stranger(port, who)
                stack.callback(s.close)
                w.home.forge_follows = True
                rc, out = w.home.setup()
                self.assertEqual(rc, 0, out)
                new = mirror.load_json(w.p["state"], {})["port"]
                self.assertNotEqual(new, port)
                self.assertTrue(mirror.PORT_FLOOR <= new < mirror.short_lived_start(), new)
                self.assertIn(f"The forge's address changed: another program holds 127.0.0.1:{port}, so the forge "
                              f"moves to http://127.0.0.1:{new}/. Every copy stays.", out)
                self.assertNotIn("problem", out)
                self.assertEqual(mirror.ini_value(w.p["app_ini"], "server", "HTTP_PORT"), str(new))
                self.assertEqual(mirror.ini_value(w.p["app_ini"], "server", "ROOT_URL"), f"http://127.0.0.1:{new}/")
                w.port, _ = w.home.start_forge()          # the service's start, as World stands in for it
                self.assertEqual(w.port, new)
                self.assertEqual(mirror.read_door(w.p, new)[0], "mine")
                self.assertEqual(w.sync()[0], 0)
                self.assertEqual(quiet(mirror.main, ["restore", "--check"])[0], 0)
                self.assertEqual(s.close(), [], f"the stranger was sent requests ({who or 'this account'})")

    def test_G8_7_setup_never_starts_a_second_forge_on_one_folder(self):
        """The owner's condition 5: the port moves only on a certain "other", after the service is stopped and no
        process of this account runs the forge's binary on this folder; otherwise setup stops and says so."""
        with World(self) as w:
            w.home.forge_follows = False                  # the service manager does not stop it: it runs on
            ini = fread(w.p["app_ini"])
            real = mirror.read_door
            try:
                mirror.read_door = lambda p, port: ("other", "a program of another account holds it")
                rc, out = w.home.setup()
            finally:
                mirror.read_door = real
            self.assertEqual(rc, 1, out)
            self.assertIn(f"problem: another program holds the forge's port 127.0.0.1:{w.port}, and this mirror's "
                          "own forge may still run on its folder, so setup did not move it: two forges on one folder "
                          "would harm the copies. Nothing else was changed. Run setup again in a minute.", out)
            self.assertEqual(fread(w.p["app_ini"]), ini)
            self.assertEqual(mirror.load_json(w.p["state"], {})["port"], w.port)
            self.assertTrue(mirror.own_forge_running(w.p))
            mirror.DOOR_SEEN.clear()
            self.assertEqual(mirror.read_door(w.p, w.port)[0], "mine")
            # an unread door is not "other": setup moves nothing and says the read's own line
            try:
                def unread(p, port):
                    raise mirror.DoorUnread(False, "a process holding it could not be read")
                mirror.read_door = unread
                rc, out = w.home.setup()
            finally:
                mirror.read_door = real
            self.assertEqual(rc, 1, out)
            self.assertIn(f"problem: who holds the forge's port 127.0.0.1:{w.port} could not be read this time (a "
                          "process holding it could not be read), so nothing was sent to it: no key, no copy. Run "
                          "setup again.", out)
            self.assertEqual(fread(w.p["app_ini"]), ini)

    def test_G8_7_a_door_that_cannot_be_read_stops_every_road_and_setup_makes_nothing(self):
        """The owner's conditions b and 4: each read fails in turn, and none is taken for "other" or for "mine";
        a computer that cannot say makes setup stop as its first act, having made nothing."""
        mac = mirror.system()[0] == "darwin"
        bad_head = os.path.join(tempfile.mkdtemp(prefix="wsm-door-"), "tcp")
        TEMPS.append(os.path.dirname(bad_head))
        with open(bad_head, "w") as f:
            f.write("  sl  local_address rem_address   st something_new uid inode\n")
        # setup, as its first act, on a computer whose table this release cannot read
        with Home() as h:
            saved = mirror.PROC_TCP, mirror.NETSTAT
            try:
                if mac:
                    mirror.NETSTAT = [sys.executable, "-I", "-c", "print('Proto Recv-Q Local Address (state) pid')"]
                else:
                    mirror.PROC_TCP = (bad_head,)
                rc, out = h.setup()
            finally:
                mirror.PROC_TCP, mirror.NETSTAT = saved
            self.assertEqual(rc, 2, out)
            self.assertRegex(said(out), r"^problem: who holds the forge's port 127\.0\.0\.1:\d+ cannot be read on "
                             r"this computer \((?:netstat names|\S+ has) columns this release does not know\), so "
                             r"nothing was sent to it: no key, no copy\.")
            self.assertTrue(said(out).endswith("\"What it runs\" says where it can. Setup made nothing."), out)
            self.assertEqual(os.listdir(h.env["WSM_HOME"]), [mirror.SUITE_MARKER], "setup made something")
            self.assertEqual(os.listdir(h.env["HOME"]), [])
        # each read of this platform failing in turn, on a set-up mirror: sync, status and restore say it, send nothing
        with World(self) as w:
            opened = []
            real_opener = mirror.forge_opener
            mirror.forge_opener = lambda: opened.append(1) or real_opener()
            faults = {
                "the table is not there": ("PROC_TCP", ("/nonexistent/tcp",), True),
                "the table cannot be read": ("PROC_TCP", (os.path.dirname(bad_head),), False),
                "the table's columns are new": ("PROC_TCP", (bad_head,), True),
                "a process holding it is gone": ("linux_proc", None, False),
                "its holder is not found": ("linux_holders", None, False),
            } if not mac else {
                "netstat is not there": ("NETSTAT", ["/nonexistent/netstat"], True),
                "netstat fails": ("NETSTAT", [sys.executable, "-I", "-c", "import sys; sys.exit(1)"], False),
                "netstat's columns are new": ("NETSTAT", [sys.executable, "-I", "-c", "print('Proto Local pid')"], True),
                "ps cannot read the process": ("mac_proc", None, False),
            }
            try:
                for name, (attr, val, never) in faults.items():
                    saved = getattr(mirror, attr)
                    if val is None:
                        def fail(*a, **k):
                            if attr == "linux_holders":
                                return {}
                            raise mirror.DoorUnread(False, "a process holding it could not be read")
                        val = fail
                    setattr(mirror, attr, val)
                    mirror.DOOR_SEEN.clear()
                    try:
                        with self.assertRaises(mirror.DoorUnread, msg=name) as e:
                            mirror.read_door(w.p, w.port)
                        self.assertEqual(e.exception.never, never, name)
                        word = "cannot be read on this computer" if never else "could not be read this time"
                        rc, out = w.sync()
                        self.assertEqual(rc, 1, (name, out))
                        self.assertEqual(len(said(out).splitlines()), 1, (name, out))
                        self.assertIn(f"problem: who holds the forge's port 127.0.0.1:{w.port} {word}", out)
                        self.assertTrue(said(out).endswith("\"What it runs\" says where it can." if never else
                                                             "Run sync again."), (name, out))
                        self.assertEqual(mirror.load_json(w.p["state"], {})["last_sync"]["stopped"],
                                         "who holds the forge's port could not be read")
                        rc, out = quiet(mirror.main, ["status"])
                        self.assertEqual(rc, 1, name)
                        self.assertIn(f"problem: who holds the forge's port 127.0.0.1:{w.port} {word}", out)
                        self.assertNotIn("does not answer", out)
                        rc, out = quiet(mirror.main, ["restore", "--check"])
                        self.assertEqual(rc, 1, name)
                        self.assertIn(word, out)
                        self.assertEqual(opened, [], f"{name}: a request went to the forge's port")
                    finally:
                        setattr(mirror, attr, saved)
            finally:
                mirror.forge_opener = real_opener
            mirror.DOOR_SEEN.clear()
            self.assertEqual(mirror.read_door(w.p, w.port)[0], "mine")
            self.assertNotIn("forge's port", w.sync()[1])         # past the door: the read works again

    def test_G8_7_the_listener_tables_are_read_by_their_headers(self):
        """The two reads, Linux's tables and the Mac's netstat, each from its header; rows read as the kernel and
        netstat write them (the Mac's as read on a real Mac, 11-24); anything else is unread, never mine."""
        d = tempfile.mkdtemp(prefix="wsm-door-")
        TEMPS.append(d)
        me = os.getuid()
        tcp, tcp6 = os.path.join(d, "tcp"), os.path.join(d, "tcp6")
        with open(tcp, "w") as f:
            f.write("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
                    f"   0: 0100007F:5208 00000000:0000 0A 00000000:00000000 00:00000000 00000000  {me}        0 4242 1\n"
                    "   1: 0100007F:5208 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  999        0 4243 1\n"
                    "   2: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 17 1\n")
        with open(tcp6, "w") as f:
            f.write("  sl  local_address                         remote_address                        st tx_queue "
                    "rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
                    "   0: 00000000000000000000000000000000:5208 00000000000000000000000000000000:0000 0A "
                    "00000000:00000000 00:00000000 00000000   999        0 5151 1\n")
        self.assertEqual(mirror.linux_listeners(0x5208, (tcp,)), [(me, 4242)])
        self.assertEqual(mirror.linux_listeners(0x5208, (tcp, tcp6)), [(me, 4242), (999, 5151)])
        self.assertEqual(mirror.linux_listeners(22, (tcp,)), [(0, 17)])
        self.assertEqual(mirror.linux_listeners(1, (tcp, os.path.join(d, "no-tcp6"))), [])
        with open(tcp, "a") as f:
            f.write("   3: garbled\n")
        with self.assertRaises(mirror.DoorUnread) as e:
            mirror.linux_listeners(1, (tcp,))
        self.assertTrue(e.exception.never)
        head = ("Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)          rxbytes      "
                "txbytes  rhiwat  shiwat          process:pid    state  options           gencnt    flags   flags1 "
                "usecnt rtncnt fltrs")
        rows = [
            "tcp4       0      0  127.0.0.1.21000        *.*                    LISTEN                 0            0 "
            " 131072  131072        gitea-28.4.0:812 00000 00000006 0000000000a1b2c3 00000000 00000800      1      0 000000",
            "tcp46      0      0  *.21000                *.*                    LISTEN                 0            0 "
            " 131072  131072     Google Chrome H:913 00000 00000006 0000000000a1b2c4 00000000 00000800      1      0 000000",
            "tcp4       0      0  127.0.0.1.21000        127.0.0.1.50000        ESTABLISHED            0            0 "
            " 131072  131072        gitea-28.4.0:812 00000 00000006 0000000000a1b2c5 00000000 00000800      1      0 000000",
            "tcp4       0      0  127.0.0.1.2100         *.*                    LISTEN                 0            0 "
            " 131072  131072            other:77 00000 00000006 0000000000a1b2c6 00000000 00000800      1      0 000000",
        ]
        text = "Active Internet connections (including servers)\n" + head + "\n" + "\n".join(rows) + "\n"
        self.assertEqual(mirror.mac_listeners(21000, text), [812, 913])
        self.assertEqual(mirror.mac_listeners(2100, text), [77])
        self.assertEqual(mirror.mac_listeners(9, text), [])
        # macOS 13.7.8 Intel, read 11-24: a plain pid column, then epid
        head13 = ("Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)      rhiwat  shiwat    "
                  "pid   epid state  options           gencnt    flags   flags1 usscnt rtncnt fltrs")
        rows13 = [
            "tcp4       0      0  127.0.0.1.21000        *.*                    LISTEN       131072  131072    812      0 "
            "00000 00000006 0000000000a1b2c3 00000000 00000800      1      0 000000",
            "tcp46      0      0  *.21000                *.*                    LISTEN       131072  131072    913      0 "
            "00000 00000006 0000000000a1b2c4 00000000 00000800      1      0 000000",
            "tcp4       0      0  127.0.0.1.2100         *.*                    LISTEN       131072  131072     77      0 "
            "00000 00000006 0000000000a1b2c6 00000000 00000800      1      0 000000",
        ]
        text13 = "Active Internet connections (including servers)\n" + head13 + "\n" + "\n".join(rows13) + "\n"
        self.assertEqual(mirror.mac_listeners(21000, text13), [812, 913])
        self.assertEqual(mirror.mac_listeners(2100, text13), [77])
        for changed in (head.replace("process:pid", "pid"), head + " newcolumn", "nothing like a header",
                        head13.replace("epid", "xpid")):
            with self.assertRaises(mirror.DoorUnread) as e:
                mirror.mac_listeners(21000, text.replace(head, changed) if "xpid" not in changed else
                                     text13.replace(head13, changed))
            self.assertTrue(e.exception.never, changed)
        # our forge: our pinned binary (any version beside it), on our settings file; nothing else
        with Home():
            p = mirror.paths()
            b = os.path.dirname(p["bin"])
            ours = [os.path.join(b, "gitea-28.4.0"), "--config", p["app_ini"], "--work-path", p["forge"], "web"]
            self.assertTrue(mirror.our_forge(p, ours[0], ours))
            self.assertTrue(mirror.our_forge(p, os.path.join(b, "gitea-28.3.0"), ours))
            self.assertTrue(mirror.our_forge(p, ours[0], " ".join(ours)))
            self.assertFalse(mirror.our_forge(p, os.path.join(b, "gitea-28.4.0.download"), ours))
            self.assertFalse(mirror.our_forge(p, "/usr/bin/gitea-28.4.0", ours))
            self.assertFalse(mirror.our_forge(p, ours[0], [*ours[:2], p["app_ini"] + ".other", *ours[3:]]))
            self.assertFalse(mirror.our_forge(p, sys.executable, ours))

    def test_G8_7_every_road_to_the_forge_reads_the_door_and_every_caller_is_named(self):
        """The owner's condition c: one place. Every request to the forge's port goes through Forge.call, forge_call
        or wait_forge (and wait_stopped for a stop), each reads the door first; the commands that reach the port are
        named here, so a new road fails this test until it is named; key set and uninstall send the forge nothing."""
        import ast
        src = fread(SCRIPT)
        tree_ = ast.parse(src)
        funcs, cls = {}, {}
        for node in ast.walk(tree_):
            if isinstance(node, ast.ClassDef):
                for f in node.body:
                    if isinstance(f, ast.FunctionDef):
                        cls[f] = node.name
        for node in ast.walk(tree_):
            if isinstance(node, ast.FunctionDef):
                funcs[(cls[node] + "." if node in cls else "") + node.name] = node

        def called(fn):
            out = set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Call):
                    f = n.func
                    out.add(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None)
            return out - {None}

        def first_call(fn):
            for st in fn.body:
                if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
                    continue
                return st
        # 1. the only openers of a connection to the forge
        openers = {k for k, f in funcs.items() if "forge_opener" in called(f)}
        self.assertEqual(openers, {"Forge.call", "wait_forge"})
        self.assertEqual({k for k, f in funcs.items() if {"create_connection", "urlopen"} & called(f)},
                         {"gh_get", "gh_public", "fetch_forge"},
                         "a connection made outside the forge's one opener, GitHub's two roads and the pinned download")
        self.assertNotIn("def port_answers", src)
        # 2. each road reads the door before it sends, and sends only on "mine" (the owner's ruling F1: on "none"
        # nothing is sent either): its first act is `if door(...) != 'mine': return ...`
        for name in ("Forge.call", "forge_call"):
            st = first_call(funcs[name])
            self.assertIsInstance(st, ast.If, name)
            self.assertRegex(ast.unparse(st.test), r"^door\(.*\) != 'mine'$", name)
            self.assertEqual([type(x) for x in st.body], [ast.Return], name)
            self.assertEqual(st.orelse, [], name)
        self.assertIn("door", called(funcs["wait_forge"]))
        self.assertIn("door", called(funcs["wait_stopped"]))
        wf = ast.unparse(funcs["wait_forge"])
        self.assertLess(wf.index("door("), wf.index("forge_opener()"))
        # 3. every command, and what of the forge's roads it reaches
        roads = {"Forge", "forge_call", "wait_forge", "wait_stopped"}
        reach = {}
        for k, f in funcs.items():
            reach[k] = called(f)
        names = {k.split(".")[-1]: k for k in funcs}

        def reaches(start, seen=None):
            seen = seen if seen is not None else set()
            for c in reach.get(start, ()):
                if c in roads:
                    return True
                k = names.get(c)
                if k and k not in seen:
                    seen.add(k)
                    if reaches(k, seen):
                        return True
            return False
        cmds = {k for k in funcs if k.startswith("cmd_")}
        to_forge = {k for k in cmds if reaches(k)}
        self.assertEqual(to_forge, {"cmd_sync", "cmd_go", "cmd_status", "cmd_restore", "cmd_setup", "cmd_backup",
                                    "cmd_upgrade_forge"},
                         "a command reaches the forge's port that this test does not name")
        self.assertNotIn("cmd_key_set", to_forge)
        self.assertNotIn("cmd_uninstall", to_forge)
        # the commands named read the door before their first request (sync, restore, setup and upgrade-forge read
        # it fresh as an act of their own; status, backup, left-out and go through the roads above)
        for k in ("sync", "cmd_restore", "cmd_upgrade_forge", "cmd_setup"):
            self.assertIn("door", called(funcs[k]) | ({"door_floor"} & called(funcs[k])), k)
        self.assertEqual(ast.unparse(funcs["cmd_setup"].body[1]), "floor = door_floor()",
                         "setup's first act is not the floor")


    def test_G8_7_our_own_forge_reads_mine_on_every_road_that_starts_it(self):
        """The owner's condition ii: setup, restore and the service's own restart after a failure each start the
        forge, and on each the door reads it as ours, with every read on the way "mine" or "none" (upgrade-forge,
        at each step: test_G9_1). Never "other" for our own forge."""
        if not real_gitea() and not os.environ.get("WSM_REQUIRE_GITEA"):
            raise unittest.SkipTest("no real pinned Gitea for this system")
        kinds, real_read = [], mirror.read_door

        def record(p, port):
            try:
                k = real_read(p, port)
            except mirror.DoorUnread as e:
                kinds.append("unread: " + str(e))
                raise
            kinds.append(k[0])
            return k
        real_install, wait = mirror.install_service, mirror.SETUP_WAIT
        mirror.read_door = record
        try:
            with contextlib.ExitStack() as stack:
                h = stack.enter_context(Home(linger="yes"))
                h.place_gitea()

                def install_and_start(p, **kw):
                    r = real_install(p, **kw)
                    if r[0]:
                        h.start_forge()
                    return r
                mirror.install_service, mirror.SETUP_WAIT = install_and_start, 30
                rc, out = h.setup()                                     # setup
                mirror.install_service, mirror.SETUP_WAIT = real_install, wait
                port = mirror.load_json(mirror.paths()["state"], {})["port"]
                self.assertEqual(rc, 0, out)
                self.assertIn(f"The forge runs as your own service on http://127.0.0.1:{port}/", out)
                self.assertIn("mine", kinds)
                self.assertEqual(set(kinds) - {"mine", "none"}, set(), kinds)
                # the service's own restart after a failure: the forge killed, run-forge started again
                kinds.clear()
                for pr in h.procs:
                    if pr.poll() is None:
                        pr.kill()
                        pr.wait(30)
                mirror.DOOR_SEEN.clear()
                self.assertEqual(mirror.read_door(mirror.paths(), port)[0], "none")
                h.start_forge()
                mirror.DOOR_SEEN.clear()
                self.assertEqual(mirror.read_door(mirror.paths(), port)[0], "mine")
            # restore: a backup, and setup --from on a new home
            backup = backup_of_one_copy(self)
            kinds.clear()
            with Home(linger="yes") as h2:
                h2.auto_login = mirror.getpass.getuser()
                h2.place_gitea()
                rc, out = restore_with_the_forge_answering(h2, backup)
                self.assertEqual(rc, 0, out)
                self.assertIn("counted in the forge itself", out)
                self.assertIn("mine", kinds)
                self.assertEqual(set(kinds) - {"mine", "none"}, set(), kinds)
        finally:
            mirror.read_door, mirror.install_service, mirror.SETUP_WAIT = real_read, real_install, wait

    def test_G8_7_on_no_listener_nothing_is_sent_even_to_one_that_comes_after_the_read(self):
        """The owner's ruling F1 (11-24): "nothing listens" is exactly when another program can take the number, so on
        "none" nothing is sent: the API call answers as no answer (0, said "it did not answer") from the read itself,
        and git is not run at all. A listener that appears between the read and the call receives 0 requests; the
        same listener, read as "mine", is sent both, so the 0 is the door's doing and not the listener's."""
        port = mirror.free_port()
        f = object.__new__(mirror.Forge)
        f.p, f.port, f.base = {"app_ini": "/nowhere/app.ini"}, port, f"http://127.0.0.1:{port}"
        f.key, f.user = "forge-key-" + "b2" * 10, "mirror"
        url = f.base + "/mirror/alpha.git"
        real = mirror.read_door
        s = Stranger(port)                                 # it came after the read below was made
        h = Home().__enter__()                             # a person's own home: git reads its settings there
        try:
            mirror.DOOR_SEEN.clear()
            mirror.read_door = lambda p, port: ("none", "nothing listens")
            code, body = f.call("GET", "/version")
            self.assertEqual((code, body), (0, {"message": f"nothing listens on 127.0.0.1:{port}"}))
            self.assertEqual(mirror.Forge.why(code, body), "it did not answer")
            out = mirror.forge_call(f, None, "ls-remote", url)
            self.assertEqual(out.returncode, 128)
            self.assertIn(f"nothing listens on 127.0.0.1:{port}", out.stderr)
            self.assertEqual(s.close(), [], "a request reached a listener the door read as none")
            s = Stranger(port)
            mirror.DOOR_SEEN.clear()
            mirror.read_door = lambda p, port: ("mine", "this mirror's own forge holds it")
            self.assertEqual(f.call("GET", "/version")[0], 200)
            mirror.forge_call(f, None, "ls-remote", url)
            seen = s.close()                               # git may ask again by the older road (its own choice)
            self.assertEqual(seen[:2], ["REQ GET /api/v1/version auth=1", "REQ GET /mirror/alpha.git/info/refs auth=1"])
            self.assertTrue(all(x.endswith(" auth=1") for x in seen), seen)
        finally:
            mirror.read_door = real
            mirror.DOOR_SEEN.clear()
            s.close()
            h.__exit__(None, None, None)

    def test_G8_7_a_stranger_beside_our_running_forge_is_not_taken_for_stopped(self):
        """The owner's ruling F2 (11-24): read_door says "other" as soon as ONE listener on the number is not our
        forge, at any address, so a stranger on ::1 while our forge still runs on 127.0.0.1 reads "other". The stop
        wait takes that for "stopped" only when no process of this account runs this forge on this folder; while
        ours runs, backup dumps nothing and upgrade-forge never says "the forge stopped". A real listener on ::1,
        opened as the service is told to stop, beside the real forge that does not stop."""
        real_read, real_own = mirror.read_door, mirror.own_forge_running
        try:                                   # the wait's three answers on "other", by what our own processes read
            mirror.read_door = lambda p, port: ("other", "a program of another account holds it")
            for ours, kind in ((True, "running"), (None, "unread"), (False, "other")):
                mirror.own_forge_running = lambda p, ours=ours: ours
                mirror.DOOR_SEEN.clear()
                self.assertEqual(mirror.wait_stopped({"app_ini": "/nowhere/app.ini"}, 20001, seconds=1), kind, ours)
        finally:
            mirror.read_door, mirror.own_forge_running = real_read, real_own
            mirror.DOOR_SEEN.clear()
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            self.assertEqual(w.key_set()[0], 0)
            self.assertEqual(w.first_sync()[0], 0)
            beside, stops, at = [], [], [1]
            real_service, waits, real_fetch = mirror.service, mirror.wait_stopped.__defaults__, mirror.fetch_forge

            def service(p, verb):
                if verb == "stop":
                    stops.append(verb)
                    # at the chosen stop: our forge runs on (it is told to stop and does not), and a stranger
                    # takes the number on ::1; at any other stop the forge stops as told
                    w.home.forge_follows = len(stops) != at[0]
                    if len(stops) == at[0]:
                        s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                        s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                        s6.bind(("::1", w.port))
                        s6.listen(1)
                        beside.append(s6)
                return real_service(p, verb)
            backups = tempfile.mkdtemp(prefix="wsm-bk-")
            TEMPS.append(backups)
            try:
                mirror.service, mirror.wait_stopped.__defaults__ = service, (3,)
                rc, out = quiet(mirror.main, ["backup", backups])
                self.assertEqual(rc, 1, out)
                self.assertIn("The forge did not stop, so no backup was made (a backup of a running forge can be "
                              "torn). Run backup again; if the forge still does not stop, run status.", out)
                self.assertEqual(os.listdir(backups), [])
                self.assertTrue(beside)
                mirror.DOOR_SEEN.clear()
                self.assertEqual(mirror.read_door(w.p, w.port)[0], "other")     # the stranger is there, beside ours
                self.assertTrue(mirror.own_forge_running(w.p))
                beside.pop().close()
                mirror.DOOR_SEEN.clear()
                # upgrade-forge: its backup's stop goes as told (the backup is made), and at its own stop the
                # stranger comes beside our forge that runs on. The new binary is not what is read here.
                stops[:], at[0] = [], 2
                mirror.fetch_forge = lambda *a, **k: None
                with pinned("28.4.1", mirror.PINS):
                    rc, out = quiet(mirror.main, ["upgrade-forge"])
                self.assertEqual(rc, 1, out)
                self.assertEqual((len(stops), len(beside)), (2, 1), out)
                self.assertIn("The forge did not stop, so it was not upgraded. Run upgrade-forge again; if the forge "
                              "still does not stop, run status.", out)
                self.assertNotIn("the forge stopped", out)
                self.assertNotIn("migrat", out.lower())
                self.assertEqual(mirror.load_json(w.p["state"], {})["gitea"], mirror.GITEA_VERSION)
            finally:
                mirror.service, mirror.wait_stopped.__defaults__ = real_service, waits
                mirror.fetch_forge = real_fetch
                w.home.forge_follows = True
                for s6 in beside:
                    s6.close()
                mirror.DOOR_SEEN.clear()
            self.assertEqual(mirror.read_door(w.p, w.port)[0], "mine")
            self.assertEqual(w.sync()[0], 0)

    def test_G8_7_setups_floor_says_its_unread_by_kind(self):
        """The owner's ruling F3 (11-24): the floor's line follows the kind of unread. A read that is not there, or
        names columns this release does not know: "cannot be read on this computer". A read that did not answer
        this time (a netstat that failed or timed out once, a table that could not be read once): "could not be read
        this time", and the next step is setup again, not that this computer cannot run the mirror."""
        never = (r"^problem: who holds the forge's port 127\.0\.0\.1:\d+ cannot be read on this computer \(%s\), so "
                 r"nothing was sent to it: no key, no copy\. The mirror needs that read to run; the README's section "
                 r"\"What it runs\" says where it can\.$")
        again = (r"^problem: who holds the forge's port 127\.0\.0\.1:\d+ could not be read this time \(%s\), so "
                 r"nothing was sent to it: no key, no copy\. Run setup again\.")
        real_system, real_netstat, real_run, real_ll = mirror.system, mirror.NETSTAT, subprocess.run, mirror.linux_listeners

        def times_out(cmd, *a, **k):
            if cmd == mirror.NETSTAT:
                raise subprocess.TimeoutExpired(cmd, k.get("timeout"))
            return real_run(cmd, *a, **k)

        def once(port, files=None):
            raise mirror.DoorUnread(False, "/proc/net/tcp could not be read")
        cases = [("darwin", [sys.executable, "-I", "-c", "import sys; sys.exit(1)"], None, None,
                  again % "netstat did not answer"),
                 ("darwin", [sys.executable, "-I", "-c", "pass"], times_out, None, again % "netstat did not answer"),
                 ("darwin", ["/nowhere/netstat"], None, None, never % "netstat is not there"),
                 ("darwin", [sys.executable, "-I", "-c", "print('Proto Recv-Q Local Address (state) pid')"], None,
                  None, never % "netstat names columns this release does not know"),
                 ("linux", real_netstat, None, once, again % "/proc/net/tcp could not be read")]
        for os_name, netstat, run, ll, want in cases:
            try:
                mirror.system = lambda os_name=os_name: (os_name, "x86_64")
                mirror.NETSTAT = netstat
                if run:
                    mirror.subprocess.run = run
                if ll:
                    mirror.linux_listeners = ll
                line = mirror.door_floor()
            finally:
                mirror.system, mirror.NETSTAT, mirror.linux_listeners = real_system, real_netstat, real_ll
                mirror.subprocess.run = real_run
            self.assertRegex(line or "", want + ("" if want.endswith("$") else "$"), (os_name, netstat))
        with Home() as h:                              # and through setup itself: made nothing, run setup again
            try:
                mirror.linux_listeners = once
                mirror.system = lambda: ("linux", "x86_64")
                rc, out = h.setup()
            finally:
                mirror.system, mirror.linux_listeners = real_system, real_ll
            self.assertEqual(rc, 2, out)
            self.assertRegex(said(out), (again % "/proc/net/tcp could not be read") + r" Setup made nothing\.$")
            self.assertFalse(os.path.exists(mirror.paths()["app_ini"]))

    def test_G8_7_a_forge_that_closed_its_port_and_lives_on_is_not_stopped(self):
        """The owner's ruling F4 (11-24): "stopped" needs both, no listener on the port AND no process of this account
        running this forge on this folder: a forge that has closed its listener can still be writing its database
        while it ends, and a dump or a migration then reads a torn one. True keeps the wait (it ends "running"); a
        read of our own processes that fails ends it "unread". The real case: a real Gitea of ours on this folder,
        our own binary on our own settings file, that no longer listens on the forge's port (started on another
        port, as a forge that closed its listener and lives on); backup then dumps nothing."""
        real_read, real_own = mirror.read_door, mirror.own_forge_running
        try:                                   # the wait's three answers on "none", by what our own processes read
            mirror.read_door = lambda p, port: ("none", "nothing listens")
            for ours, kind in ((True, "running"), (None, "unread"), (False, "stopped")):
                mirror.own_forge_running = lambda p, ours=ours: ours
                mirror.DOOR_SEEN.clear()
                self.assertEqual(mirror.wait_stopped({"app_ini": "/nowhere/app.ini"}, 20001, seconds=1), kind, ours)
        finally:
            mirror.read_door, mirror.own_forge_running = real_read, real_own
            mirror.DOOR_SEEN.clear()
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            self.assertEqual(w.key_set()[0], 0)
            self.assertEqual(w.first_sync()[0], 0)
            w.home.forge_follows = False
            port = stop_forge(w.home)
            ini = fread(w.p["app_ini"])
            other = mirror.free_port()
            lives = subprocess.Popen([mirror.running_bin(w.p), "--config", w.p["app_ini"], "--work-path", w.p["forge"],
                                      "web", "--port", str(other)], env=mirror.forge_env(w.p),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            STARTED.append(lives)
            backups = tempfile.mkdtemp(prefix="wsm-bk-")
            TEMPS.append(backups)
            waits = mirror.wait_stopped.__defaults__
            try:
                end = time.monotonic() + mirror.FIRST_START_WAIT
                while True:
                    try:
                        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                            f"http://127.0.0.1:{other}/api/healthz", timeout=2)
                        break
                    except (urllib.error.URLError, OSError):
                        self.assertIsNone(lives.poll(), "the second Gitea of ours stopped")
                        self.assertLess(time.monotonic(), end, "the second Gitea of ours did not answer")
                        time.sleep(0.5)
                mirror.DOOR_SEEN.clear()
                self.assertEqual(mirror.read_door(w.p, port)[0], "none")        # nothing on the forge's own port
                self.assertTrue(mirror.own_forge_running(w.p))                  # and ours lives on, on this folder
                mirror.wait_stopped.__defaults__ = (3,)
                rc, out = quiet(mirror.main, ["backup", backups])
                self.assertEqual(rc, 1, out)
                self.assertIn("The forge did not stop, so no backup was made (a backup of a running forge can be "
                              "torn). Run backup again; if the forge still does not stop, run status.", out)
                self.assertEqual(os.listdir(backups), [])
            finally:
                mirror.wait_stopped.__defaults__ = waits
                lives.terminate()
                lives.wait(30)
            with open(w.p["app_ini"], "w") as f:      # Gitea's own `web --port` writes LOCAL_ROOT_URL there
                f.write(ini)                           # (seen 11-24): the stand-in's mark, not the tool's, undone
            # once ours has ended, the same backup is made: the wait's "stopped" is the process's end, not the port's
            mirror.DOOR_SEEN.clear()
            self.assertFalse(mirror.own_forge_running(w.p))
            w.home.forge_follows = True
            rc, out = quiet(mirror.main, ["backup", backups])
            self.assertEqual(rc, 0, out)
            self.assertEqual(len(os.listdir(backups)), 1, out)
            self.assertEqual(w.sync()[0], 0)

    def test_G8_7_status_calls_what_answers_the_forge_only_when_the_door_read_it(self):
        """The owner's ruling, 11-24 (the rehearsal's two lines): status says only what was read. The forge line by
        the door's answer: other "(not asked: another program answers on its port)", unread "(not asked: who holds
        its port could not be read)", none "(not answering)". The network line and its problem line call what
        answers "the forge" only when the door read mine and the forge answered; otherwise the subject is "a
        program". Read in four states: the forge down; another program on every address; the door unread; the
        forge up (the words as before)."""
        def lines(out, head):
            return [x for x in out.splitlines() if x.startswith(head)]
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            self.assertEqual(w.key_set()[0], 0)
            self.assertEqual(w.first_sync()[0], 0)
            read, caught, real_addrs, real_ans = [], [], mirror.own_addresses, mirror.answers_on

            def addrs():
                read.append(real_addrs())
                return read[-1]

            def ans(a, p):
                caught.append(real_ans(a, p))
                return caught[-1]

            def others():
                return len([a for a in read[-1] if not (a == "::1" or a.startswith("127."))])
            mirror.own_addresses, mirror.answers_on = addrs, ans
            wide = None
            try:
                w.home.forge_follows = False
                port = stop_forge(w.home)
                rc, out = status()                                   # 1. the forge down
                self.assertEqual(rc, 1, out)
                self.assertEqual(lines(out, "forge: "), [f"forge: Gitea (not answering) on http://127.0.0.1:{port}/ "
                                                         f"(pinned {mirror.GITEA_VERSION})"])
                self.assertEqual(caught[-1], [])
                self.assertEqual(lines(out, "network: "), [
                    "network: the forge was not reached, so where it answers was not checked (nothing answers on this "
                    f"port at the {others()} other address(es) of this computer)"])
                self.assertNotIn("the forge answers on", out)
                wide = socket.socket(socket.AF_INET, socket.SOCK_STREAM)       # 2. another program, every address
                wide.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                wide.bind(("0.0.0.0", port))
                wide.listen(8)
                mirror.DOOR_SEEN.clear()
                self.assertEqual(mirror.read_door(w.p, port)[0], "other")
                rc, out = status()
                self.assertEqual(rc, 1, out)
                self.assertEqual(lines(out, "forge: "), [
                    f"forge: Gitea (not asked: another program answers on its port) on http://127.0.0.1:{port}/ "
                    f"(pinned {mirror.GITEA_VERSION})"])
                v4 = [a for a in read[-1] if ":" not in a and not a.startswith("127.")]
                if v4:                                 # an address on a bridge that is down answers nothing at all
                    self.assertTrue(caught[-1], "a listener on every address was not caught on any of them")
                for a in caught[-1]:
                    self.assertIn(f"problem: a program answers on port {port} at {a}, and it is not this mirror's "
                                  "forge as far as this computer says", out)
                self.assertEqual(lines(out, "network: "), [
                    f"network: the forge was not reached; a program answers on this port at {', '.join(caught[-1])}"
                    if caught[-1] else "network: the forge was not reached, so where it answers was not checked "
                    f"(nothing answers on this port at the {others()} other address(es) of this computer)"])
                self.assertNotIn("the forge answers on", out)
                real_read = mirror.read_door                                   # 3. the door unread

                def unread(p, port):
                    raise mirror.DoorUnread(False, "a process holding it could not be read")
                mirror.read_door = unread
                mirror.DOOR_SEEN.clear()
                try:
                    rc, out = status()
                finally:
                    mirror.read_door = real_read
                self.assertEqual(rc, 1, out)
                self.assertEqual(lines(out, "forge: "), [
                    f"forge: Gitea (not asked: who holds its port could not be read) on http://127.0.0.1:{port}/ "
                    f"(pinned {mirror.GITEA_VERSION})"])
                for a in caught[-1]:
                    self.assertIn(f"problem: a program answers on port {port} at {a}; whether it is this mirror's "
                                  "forge could not be read", out)
                self.assertNotIn("the forge answers on", out)
                wide.close()
                wide = None
                mirror.DOOR_SEEN.clear()
                w.port, _ = w.home.start_forge()                               # 4. the forge up: words as before
                w.home.forge_follows = True
                rc, out = status()
                self.assertEqual(rc, 0, out)
                self.assertEqual(lines(out, "forge: "), [f"forge: Gitea {mirror.GITEA_VERSION} on http://127.0.0.1:"
                                                         f"{w.port}/ (pinned {mirror.GITEA_VERSION})"])
                self.assertEqual(lines(out, "network: "), [
                    f"network: the forge answers on 127.0.0.1 only (tried {others()} other address(es) of this "
                    "computer)"])
            finally:
                mirror.own_addresses, mirror.answers_on = real_addrs, real_ans
                if wide:
                    wide.close()
                mirror.DOOR_SEEN.clear()


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
    def test_G9_12_a_test_only_setting_needs_the_suites_marker_and_never_sends_a_key_off_this_computer(self):
        """Rule 27: WSM_GITHUB_API, WSM_GITHUB_GIT, WSM_PAGE_SIZE and WSM_GITEA_BASE need two keys, the setting and
        the marker the suite writes in the home it made. The setting alone is found and ignored, said first, and the
        command goes on against GitHub itself; with both, a GitHub address that is not this computer's loopback
        stops the command having done nothing. No key is ever sent to a host that is not GitHub's API or loopback."""
        vals = {"WSM_GITHUB_API": "http://127.0.0.1:9", "WSM_GITHUB_GIT": "http://localhost:9",
                "WSM_PAGE_SIZE": "3", "WSM_GITEA_BASE": "http://[::1]:9/gitea"}
        saved, real_open, sent = {k: os.environ.get(k) for k in vals}, urllib.request.urlopen, []

        def no_road(req, *a, **k):          # every read the tool would make, recorded; none leaves this test
            sent.append(urllib.parse.urlsplit(getattr(req, "full_url", req)).netloc)
            raise urllib.error.URLError("no road in this test")
        urllib.request.urlopen = no_road
        try:
            with Home():
                marker = os.path.join(os.environ["WSM_HOME"], mirror.SUITE_MARKER)
                os.environ.update(vals)
                # The control: with both keys, each setting is taken (the suite leans on this).
                self.assertEqual((mirror.github_api(), mirror.github_git(), mirror.page_size(),
                                  mirror.test_setting("WSM_GITEA_BASE")),
                                 (vals["WSM_GITHUB_API"], vals["WSM_GITHUB_GIT"], 3, vals["WSM_GITEA_BASE"]))
                rc, out = quiet(mirror.main, ["status"])
                self.assertNotIn("ignored", out)
                self.assertTrue(out.startswith(f"WSM_HOME is set: this skill's folders are under "
                                               f"{os.environ['WSM_HOME']}."), out)
                # The setting alone: nothing is replaced; said first; the command goes on.
                os.remove(marker)
                self.assertEqual((mirror.github_api(), mirror.github_git(), mirror.page_size(),
                                  mirror.test_setting("WSM_GITEA_BASE")),
                                 (mirror.GITHUB_API, mirror.GITHUB_GIT, 100, None))
                rc, out = quiet(mirror.main, ["status"])
                lines = out.splitlines()
                self.assertTrue(lines[0].startswith("WSM_HOME is set:"), out)
                for i, name in enumerate(vals, 1):
                    self.assertTrue(lines[i].startswith(f"{name} was found and ignored"), out)
                self.assertIn("Run setup first.", out)
                self.assertEqual([n for n in vals if n in os.environ], [], "handed on to what the tool starts")
                sent.clear()
                mirror.gh_get("k", "/user")
                mirror.gh_public("/repos/x/y/releases/latest")      # self-update's read
                self.assertEqual(sent, ["api.github.com", "api.github.com"])
                # Both keys, a GitHub address that is not loopback: stopped first, nothing done, no key sent there.
                with open(marker, "w"):
                    pass
                for bad in ("https://example.com", "http://10.0.0.1:3000", "https://127.0.0.1.example.com",
                            "http://localhost.example.com:80", "ftp://127.0.0.1", "https://api.github.com.example.com"):
                    for name in ("WSM_GITHUB_API", "WSM_GITHUB_GIT"):
                        os.environ[name] = bad
                        before = sorted(os.listdir(os.environ["WSM_HOME"]))
                        for cmd in (["setup", "--linger"], ["self-update"], ["status"]):
                            sent.clear()
                            rc, out = quiet(mirror.main, cmd)
                            self.assertEqual(rc, 2, (bad, name, cmd, out))
                            self.assertIn(f"Stopped: {name} names an address that is not this computer's own", out)
                            self.assertEqual((sent, sorted(os.listdir(os.environ["WSM_HOME"]))), ([], before))
                        self.assertIn(mirror.github_api(), (mirror.GITHUB_API, vals["WSM_GITHUB_API"]))
                        self.assertIn(mirror.github_git(), (mirror.GITHUB_GIT, vals["WSM_GITHUB_GIT"]))
                        sent.clear()
                        mirror.gh_get("k", "/user")
                        self.assertNotIn(urllib.parse.urlsplit(bad).netloc, sent)
                        os.environ[name] = vals[name]
                # A next page on another host than GitHub's API is never asked with the key.
                sent.clear()
                self.assertEqual(mirror.gh_get("k", "https://example.com/user/repos?page=2")[0], 0)
                os.environ.pop("WSM_GITHUB_API")
                self.assertEqual(mirror.gh_get("k", "https://example.com/user/repos?page=2")[0], 0)
                self.assertEqual(sent, [])
                mirror.gh_get("k", "https://api.github.com/user/repos?page=2")
                self.assertEqual(sent, ["api.github.com"])
        finally:
            urllib.request.urlopen = real_open
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        for f in (SKILL, os.path.join(ROOT, "README.md")):
            for name in vals:
                self.assertNotIn(name, fread(f))

    def test_G9_12_every_setting_the_tool_reads_is_on_one_of_the_three_lists(self):
        """Rule 27's gate: every environment read in the released code is a person's setting (named in the README),
        a fact from the host, the skill's own marker, or a two-key test setting read only through test_setting."""
        src = fread(os.path.join(ROOT, "scripts", "mirror.py"))
        read = set(re.findall(r'(?:os\.environ(?:\.get)?\s*[\(\[]|os\.getenv\()\s*"([A-Za-z0-9_]+)"', src))
        lists = set(mirror.PERSON_SETTINGS) | set(mirror.HOST_FACTS) | set(mirror.OWN_MARKERS)
        self.assertEqual(sorted(read - lists), [], "read, and on none of the lists")
        self.assertEqual(sorted(set(re.findall(r'test_setting\("([A-Z0-9_]+)"\)', src)) - set(mirror.TEST_SETTINGS)), [])
        self.assertEqual(sorted(read & set(mirror.TEST_SETTINGS)), [], "a test setting read without its second key")
        self.assertNotIn("getenv(", src.replace('os.getenv("', ""), "a read this test cannot name")
        self.assertEqual(len(re.findall(r"os\.environ(?:\.get)?\s*[\(\[]\s*(?!\")", src)),
                         len(re.findall(r"os\.environ\.get\(name\)", src)), "a read by a name this test cannot see")
        readme = fread(os.path.join(ROOT, "README.md"))
        for name in mirror.PERSON_SETTINGS:
            self.assertIn(f"`{name}`", readme)
        # A moved folder is said by the command it changed.
        with Home():
            os.environ["XDG_DATA_HOME"] = os.path.join(os.environ["HOME"], "elsewhere")
            os.environ.pop("WSM_HOME")
            try:
                rc, out = quiet(mirror.main, ["status"])
            finally:
                os.environ.pop("XDG_DATA_HOME")
            if mirror.system()[0] == "linux":
                self.assertTrue(out.startswith("XDG_DATA_HOME is set: this skill's data folders are under "), out)

    def test_G9_12_only_what_the_skill_made_is_written_over_or_taken_away(self):
        """Rule 23: setup refuses the home folder and a folder holding a person's things. Uninstall and the delete
        take only what the record holds, by name, read when they run (a person's file may arrive after setup); rule
        8: a person's file never stops the leave. It goes to its end (the service off, its files and everything on
        the record gone), the person's things and the folders above them stay and are named as theirs, it exits 0,
        and its last line says the mirror is off and what stayed is named. The census never says "Kept: nothing"
        over them."""
        stand = {"data/notes.txt": "my notes\n", "data/mirror.py": "print('mine')\n", "data/cache/photo.jpg": "J\n",
                 "data/bin/tool.sh": "echo mine\n", "data/forge/readme.txt": "mine\n", "config/app.conf": "x=1\n",
                 "config/settings.json": '{"mine": true}\n', "config/keys/ssh-note.txt": "note\n"}

        def plant(root, only=None):
            for rel, txt in stand.items():
                if only is None or rel in only:
                    f = os.path.join(root, rel)
                    os.makedirs(os.path.dirname(f), exist_ok=True)
                    with open(f, "w") as fh:
                        fh.write(txt)

        def kept(root, only=None):
            for rel, txt in stand.items():
                if only is None or rel in only:
                    self.assertEqual(fread(os.path.join(root, rel)), txt, rel)

        def listing(root):
            return sorted(os.path.relpath(os.path.join(d, x), root) for d, ds, fs in os.walk(root) for x in ds + fs)

        def uninstall(*args, typed=None):
            saved = sys.stdin
            sys.stdin = AtTerminal(typed or "")
            try:
                return quiet(mirror.main, ["uninstall", *args])
            finally:
                sys.stdin = saved

        # Setup refuses the home folder, and a folder that holds things it did not make; nothing changes.
        for where in ("", "theirs"):
            h = Home()
            root = os.path.join(h.env["HOME"], where) if where else h.env["HOME"]
            h.env["WSM_HOME"] = root
            plant(root)
            with h:
                before = listing(root)
                rc, out = h.setup()
                self.assertEqual(rc, 2, out)
                self.assertIn("is your home folder" if not where else "already holds things the skill did not make",
                              out)
                self.assertIn("Nothing was changed.", out)
                self.assertEqual(listing(root), before)
                kept(root)
        # Fine at setup; a person's files arrive later, in every place the skill uses.
        with World(self) as w:
            root = os.environ["WSM_HOME"]
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            self.assertEqual(w.key_set()[0], 0)
            self.assertEqual(w.first_sync()[0], 0)
            self.assertEqual(mirror.not_ours(w.p), [])
            # (Two of the stand-ins bear the skill's own file names; those setup refuses above. Here, the rest.)
            later = set(stand) - {"data/mirror.py", "config/settings.json"}
            plant(root, later)
            darwin = mirror.system()[0] == "darwin"               # a launch agent on a Mac, a user unit on Linux
            unit = w.p["plist" if darwin else "unit"]
            self.assertTrue(os.path.exists(unit))
            rc, out = uninstall()
            # Rule 8: the leave goes to its end for everything that is the skill's; what it did not make stays,
            # named as the person's, with each folder that stayed for it; exit 0; the last line says so.
            self.assertEqual(rc, 0, out)
            self.assertIn(["launchctl", "bootout", f"gui/{os.getuid()}/{mirror.LAUNCHD_LABEL}"] if darwin else
                          ["systemctl", "--user", "disable", "--now", mirror.SERVICE, mirror.SYNC_UNIT + ".timer"],
                          w.home.calls)
            self.assertFalse(os.path.exists(unit), "the unit goes")
            self.assertFalse(os.path.exists(w.p["bin"]), "the forge's binary goes")
            self.assertFalse(os.path.exists(os.path.join(root, "config", "keys", "someone.key")), "the skill's key goes")
            kept(root, later)
            self.assertIn("Not the skill's, so left where they are", out)
            theirs = out.split("Not the skill's, so left where they are")[1]
            for rel in later:
                self.assertIn(os.path.join(root, rel), theirs)
                self.assertNotIn(os.path.join(root, rel), out.split("Not the skill's")[0])
            self.assertIn("Folders of the skill's that stayed because they hold those:", out)
            self.assertIn(f"  {os.path.join(root, 'config', 'keys')}: holds ssh-note.txt", out)
            self.assertIn(f"  {os.path.join(root, 'data', 'bin')}: holds tool.sh", out)
            self.assertNotIn("Kept: nothing", out)
            self.assertNotIn("run uninstall again", out)
            self.assertEqual(out.strip().splitlines()[-1],
                             "The mirror is off this computer. What stayed, and why, is named above; it is yours, to "
                             "keep or delete after you look at it.")
            self.assertTrue(os.path.isdir(w.p["forge"]), "the copies stay without --delete-copies")
            self.assertIn("Kept: the folder of copies", out)
            # Run again: safe, and still true.
            rc, out = uninstall()
            self.assertEqual(rc, 0, out)
            kept(root, later)
            # The delete, with the person's files still there (one at the forge's own top): only what the record
            # holds goes; theirs and the folders above them stay, and are named.
            rc, out = uninstall("--delete-copies", typed="1\n")
            self.assertEqual(rc, 0, out)
            kept(root, later)
            self.assertFalse(os.path.exists(os.path.join(w.p["forge"], "data")), "the forge's own folders go")
            # The record stays while folders of the skill's stand (they hold the person's things), and is said.
            self.assertTrue(os.path.exists(os.path.join(w.p["data"], mirror.MADE_RECORD)), out)
            self.assertIn(f"Kept: {w.p['data']}, the record of what the skill did here.", out)
            self.assertIn(f"  {w.p['forge']}: the 1 copies (the forge's own folders in it)", out)
            self.assertIn(f"  {w.p['forge']}: holds readme.txt", out)
            self.assertNotIn("Kept: nothing", out)
            self.assertNotIn("Kept: the folder of copies", out)
            self.assertTrue(out.strip().endswith("it is yours, to keep or delete after you look at it."), out)
            for rel in later:
                os.remove(os.path.join(root, rel))
            self.assertEqual(uninstall()[0], 0)
        # A file arriving between the check and the removal (the check stood in to see nothing): the removal
        # itself still takes only what the record holds, never a whole folder holding something else.
        with World(self) as w2:
            root2 = os.environ["WSM_HOME"]
            w2.gh.add_repo("someone", "alpha")
            w2.gh.commit("someone/alpha", {"a": b"1"})
            self.assertEqual(w2.key_set()[0], 0)
            self.assertEqual(w2.first_sync()[0], 0)
            plant(root2, later)
            saved_check, mirror.not_ours = mirror.not_ours, lambda p: []
            try:
                rc, out = uninstall()
                kept(root2, later)
                rc, out = uninstall("--delete-copies", typed="1\n")
            finally:
                mirror.not_ours = saved_check
            kept(root2, later)
            self.assertFalse(os.path.exists(os.path.join(root2, "config", "keys", "someone.key")))
            for rel in later:
                os.remove(os.path.join(root2, rel))
            rc, out = uninstall("--delete-copies", typed="1\n")
            self.assertEqual(rc, 0, out)
            self.assertFalse(os.path.exists(w2.p["data"]), out)
            self.assertIn("Kept: nothing", out)
            self.assertEqual(listing(root2), [mirror.SUITE_MARKER])
        # No line the tool prints, the README or the skill's text deletes a folder recursively or by a pattern.
        for f in (os.path.join(ROOT, "scripts", "mirror.py"), os.path.join(ROOT, "README.md"), SKILL):
            self.assertIsNone(re.search(r"\brm\s+-[a-zA-Z]*[rR]|(?<![.\w])rmdir\s|\bfind\b[^\n]*-delete|\brm\s+[^\n]*\*",
                                        fread(f)), f)

    def test_G9_12_nothing_of_the_skill_runs_with_admin_rights(self):
        """The family's rule 28: no command the tool runs is sudo, doas, pkexec or su, and the README says the
        skill needs no administrator rights (it prints sudo only in an install line the person reads and runs)."""
        src = fread(os.path.join(ROOT, "scripts", "mirror.py"))
        self.assertIsNone(re.search(r'\[\s*"(sudo|doas|pkexec|su|runas)"', src))
        self.assertIsNone(re.search(r'(subprocess\.\w+|run)\(\s*f?"(sudo|doas|pkexec|su) ', src))
        self.assertIn("needs no administrator rights", fread(os.path.join(ROOT, "README.md")))

    def test_G9_12_setup_and_uninstall_do_nothing_against_another_homes_install(self):
        """WSM_HOME is a person's setting. Setup and uninstall refuse it when this computer's service for the skill
        runs another home's copy: nothing changed, the home that install uses said, and the one next step."""
        def service_text(root):
            was = os.environ.get("WSM_HOME")
            os.environ.pop("WSM_HOME", None) if root is None else os.environ.__setitem__("WSM_HOME", root)
            try:
                q = mirror.paths()
                return mirror.plist_text(q) if mirror.system()[0] == "darwin" else mirror.unit_text(q)
            finally:
                os.environ["WSM_HOME"] = was
        # M150 (G9.15): both roads on every leg, the system stood in for (a Mac's road ran only on the Mac legs, and
        # a stand-in that raised there was how a launch agent file that is not well-formed went unseen on Linux)
        saved = mirror.system
        saved_doors = doors_the_hosts_way(saved, always=True)
        try:
            for name in (("linux", "x86_64"), ("darwin", "arm64")):
                mirror.system = lambda name=name: name
                with Home() as h, self.subTest(system=name[0]):
                    p = mirror.paths()
                    f = p["plist"] if mirror.system()[0] == "darwin" else p["unit"]
                    os.makedirs(os.path.dirname(f), exist_ok=True)
                    other = os.path.join(h.tmp, "other home")
                    for q in (mirror.paths(other), mirror.paths(None)):         # each service's copy is there, as it runs (M144)
                        os.makedirs(os.path.dirname(q["script"]), exist_ok=True)
                        shutil.copyfile(SCRIPT, q["script"])
                    for text, want in ((service_text(other), f"WSM_HOME={shlex.quote(other)} python3 -I "
                                        f"{shlex.quote(mirror.paths(other)['script'])}"),
                                       (service_text(None), "unset WSM_HOME, then run python3 -I "
                                        f"{shlex.quote(mirror.paths(None)['script'])}"),
                                       ("[Service]\n", "could not be read")):
                        with open(f, "w") as fh:
                            fh.write(text)
                        for cmd in (["setup", "--linger"], ["setup", "--dry-run"], ["uninstall"]):
                            rc, out = quiet(mirror.main, cmd)
                            if text == "[Service]\n" and mirror.system()[0] == "darwin":
                                # M150/M152: not a property list, so the tool's own reader could not read it either:
                                # the step is the move, never "read that file"; uninstall, with launchd's answer for
                                # the job unread too, is a Not now that names the same step (a reviewer's world C)
                                self.assertNotIn("read that file", out)
                                self.assertIn(f"out of {os.path.dirname(f)}", out)
                                if cmd[0] == "uninstall":
                                    self.assertEqual(rc, 3, (cmd, out))
                                    self.assertIn("Nothing was removed and nothing was taken off.", out)
                                    self.assertEqual(fread(f), text)
                                    continue
                            if text == "[Service]\n" and mirror.system()[0] != "darwin":
                                # M153 (G9.17): on Linux the unit opens and systemd shows no start line: it runs
                                # nothing, so it is no other install; the file is not as the skill wrote it (rule 23):
                                # setup stops with the move, uninstall names it, leaves it, and goes to its end
                                self.assertNotIn("read that file", out)
                                self.assertIn("not as the skill wrote it", out)
                                self.assertEqual(fread(f), text)
                                if cmd[0] == "uninstall":
                                    self.assertEqual(rc, 0, (cmd, out))
                                    self.assertTrue(out.strip().endswith("it is yours, to keep or delete after you "
                                                                         "look at it."), out)
                                else:
                                    self.assertEqual(rc, 2, (cmd, out))
                                    self.assertIn("move it out of that folder", out)
                                    self.assertIn("Nothing was changed.", out)
                                continue
                            self.assertEqual(rc, 2, (cmd, out))
                            self.assertIn("Nothing was changed.", out)
                            self.assertIn(want, out)
                            self.assertEqual(fread(f), text)
                            self.assertFalse(os.path.exists(p["data"]), "nothing made")
                    # The control: the service of this home's own copy, as the skill wrote it, is no reason to stop.
                    with open(f, "w") as fh:
                        fh.write(service_text(os.environ["WSM_HOME"]))
                    mirror.claim(f)                                       # what setup records when it writes it
                    rc, out = quiet(mirror.main, ["setup", "--dry-run"])
                    self.assertNotIn("uses another home", out)
                    self.assertEqual(rc, 0, out)
                    # Rule 23: the same file changed since (by the person, or by something else) is not written over by
                    # setup, nor removed by uninstall: it is named and left. Setup stops there; uninstall goes to its end
                    # (rule 8: a person who wants to leave is never refused because of the state of their own files).
                    with open(f, "a") as fh:     # M150: a change that stays a well-formed property list on a Mac
                        fh.write("<!-- the person's own line -->\n" if mirror.system()[0] == "darwin" else
                                 "# the person's own line\n")
                    changed = fread(f)
                    for cmd in (["setup", "--dry-run"], ["uninstall"]):
                        rc, out = quiet(mirror.main, cmd)
                        if cmd[0] == "setup":
                            self.assertNotEqual(rc, 0, (cmd, out))
                        else:
                            self.assertEqual(rc, 0, (cmd, out))
                            self.assertIn("It is switched off; look at it, and keep or delete it as you choose.", out)
                            self.assertTrue(out.strip().endswith("it is yours, to keep or delete after you look at it."), out)
                        self.assertIn("not as the skill wrote it", out)
                        self.assertIn(f, out)
                        self.assertEqual(fread(f), changed)
                        self.assertNotIn("Kept: nothing", out)
                    os.remove(f)
                    # Rule 28: a folder of the skill's that other accounts can write in, or that another account owns, is
                    # refused before anything is written there.
                    os.makedirs(p["data"], exist_ok=True)
                    mode = os.stat(p["data"]).st_mode
                    try:
                        os.chmod(p["data"], 0o777)
                        rc, out = quiet(mirror.main, ["setup", "--dry-run"])
                        self.assertEqual(rc, 2, out)
                        self.assertIn(f"Stopped: {p['data']} is not yours alone (other accounts can write in it)", out)
                    finally:
                        os.chmod(p["data"], mode & 0o777)
        finally:
            mirror.system = saved
            mirror.read_door, mirror.door_floor = saved_doors
        # Rule 28: whatever umask the tool is started with (000 in some containers and job runners), it makes
        # nothing other accounts can write, so its own next run never refuses a folder it made itself.
        def everything(h):
            return {os.path.join(d, y) for top in (os.environ["WSM_HOME"], h.env["HOME"])
                    for d, ds, fs in os.walk(top) for y in ds + fs}
        with Home() as h:
            h.place_gitea()                                       # the suite's stand-in download, as the person's
            before = everything(h)
            saved = os.umask(0)
            try:
                for _ in (1, 2):
                    rc, out = h.setup()
                    self.assertEqual(rc, 0, out)
            finally:
                os.umask(saved)
            made = sorted(everything(h) - before)
            self.assertTrue(made)
            for x in made:
                if not os.path.islink(x):
                    self.assertEqual(os.lstat(x).st_mode & 0o022, 0, (x, oct(os.lstat(x).st_mode)))
            # A file that holds a secret is made 0600 by name, before a byte is written into it, whatever the umask,
            # and a .tmp an earlier run left (here 0666) is never written into.
            opened, real_fdopen = [], os.fdopen

            def seen_fdopen(fd, *a, **k):
                opened.append(os.fstat(fd).st_mode & 0o777)
                return real_fdopen(fd, *a, **k)
            saved = os.umask(0)
            try:
                for write in (mirror.write_private, lambda f, t: mirror.write_file(f, t, 0o600)):
                    f = os.path.join(mirror.paths()["conf"], "a-secret")
                    with open(f + ".tmp", "w") as fh:
                        fh.write("left by an earlier run\n")
                    os.chmod(f + ".tmp", 0o666)
                    opened.clear()
                    os.fdopen = seen_fdopen
                    try:
                        write(f, "s3cret\n")
                    finally:
                        os.fdopen = real_fdopen
                    # the file's own write first; the record's note of it after, where the record gains an entry
                    # (M161: put_whole too), also 0600
                    self.assertIn(opened, ([0o600], [0o600, 0o600]))
                    self.assertEqual(os.stat(f).st_mode & 0o777, 0o600)
                    # M161: this write's own temporary name; the earlier run's file is never written into, and no
                    # temporary file of this write's is left
                    self.assertEqual(fread(f + ".tmp"), "left by an earlier run\n")
                    self.assertEqual([x for x in os.listdir(os.path.dirname(f)) if x.startswith("a-secret.")],
                                     ["a-secret.tmp"])
                    os.remove(f + ".tmp")
                    os.remove(f)
            finally:
                os.umask(saved)
        # Rule 28: a file is only as closed as the folders above it. Every folder from the home down to what the
        # skill runs is read: one of the skill's own, opened since, setup closes; one of the person's that another
        # account can write in is never changed, only named with the one command, by setup, its dry run, the go's
        # block and status, until it is closed; one that cannot be read is said as that.
        with Home() as h, contextlib.ExitStack() as stack:
            os.environ["WSM_GITHUB_API"] = "http://127.0.0.1:9"     # status reads the latest release: not here
            stack.callback(os.environ.pop, "WSM_GITHUB_API", None)
            # This computer's other accounts, stood in: none but a stranger in no group of these folders. The home
            # is passable (755), so a folder in it that others can write in is reachable.
            saved_accounts, mirror.accounts = mirror.accounts, lambda: []
            stack.callback(setattr, mirror, "accounts", saved_accounts)
            # The folders above the suite's own are the machine's (rule 17): every account may pass them, as /tmp
            # lets it on Linux. A Mac's temporary folder is the account's alone (700), so there nobody could reach a
            # folder below it and the walk rightly names none (seen on a stock macOS runner); stood in here.
            stack.callback(setattr, mirror, "os", mirror.os)
            mirror.os = _PassableAbove(os.path.realpath(h.tmp))
            os.chmod(h.tmp, 0o755)
            os.chmod(h.env["HOME"], 0o755)
            h.place_gitea()
            darwin = mirror.system()[0] == "darwin"
            service_dir = os.path.dirname(mirror.paths()["plist" if darwin else "unit"])
            theirs = os.path.dirname(service_dir) if darwin else os.path.dirname(os.path.dirname(service_dir))
            os.makedirs(theirs, exist_ok=True)
            os.chmod(theirs, 0o777)                               # the person's, there before setup, open to all
            want = (f"{theirs} is open: every account on this computer can write in it, so another account could "
                    "replace the skill's service or program in it, or below it.")
            rc, out = h.setup()
            self.assertEqual(rc, 0, out)
            self.assertIn(want, out)
            self.assertIn(f"The one command that closes it: chmod go-w {shlex.quote(theirs)}", out)
            self.assertEqual(os.stat(theirs).st_mode & 0o777, 0o777, "the person's folder is never changed")
            for cmd in (["setup", "--dry-run"], ["status"]):
                rc, out = quiet(mirror.main, cmd)
                self.assertIn(want, out, cmd)
            self.assertIn(service_dir, mirror.load_json(mirror.paths()["state"], {}).get("made_folders") or [])
            os.chmod(service_dir, 0o777)                         # a folder setup made, opened since
            rc, out = h.setup()
            self.assertEqual(rc, 0, out)
            self.assertEqual(os.stat(service_dir).st_mode & 0o022, 0, "setup closes a folder of its own")
            self.assertNotIn(f"{service_dir} is open", out)
            # What runs includes what decides what runs: the walk starts from the forge's settings (app.ini), the
            # tool's settings and state, and the Python the service's files name, too. The forge's settings folder,
            # inside the forge the skill made, opened since: named as the skill's own, and setup closes it.
            above = mirror.folders_above(mirror.paths())
            for f in (mirror.paths()["app_ini"], mirror.paths()["settings"], mirror.paths()["state"], sys.executable):
                self.assertIn(os.path.dirname(os.path.abspath(f)), above, f)
            ini_dir = os.path.dirname(mirror.paths()["app_ini"])
            os.chmod(ini_dir, 0o777)
            rc, out = quiet(mirror.main, ["status"])
            self.assertIn(f"{ini_dir} is a folder of the skill's own that others may write in: setup closes it "
                          "(chmod go-w).", out)
            rc, out = h.setup()
            self.assertEqual(rc, 0, out)
            self.assertEqual(os.stat(ini_dir).st_mode & 0o022, 0, "setup closes the forge's settings folder")
            # The Python the service names, in a folder every account can write in (seen on a stock runner, where a
            # freshly downloaded Python sat in one): planted here, so the sentence is held on every machine, and the
            # one command it prints closes it.
            pybin = os.path.join(h.tmp, "python-download", "bin")
            os.makedirs(pybin)
            with open(os.path.join(pybin, "python3"), "w") as f:
                f.write("")
            os.chmod(pybin, 0o777)
            real_py = sys.executable
            sys.executable = os.path.join(pybin, "python3")
            try:
                rc, out = quiet(mirror.main, ["status"])
                self.assertIn(f"{pybin} is open: every account on this computer can write in it", out)
                self.assertIn(f"chmod go-w {shlex.quote(pybin)}", out)
                subprocess.run(["chmod", "go-w", pybin], check=True)     # the command as printed
                rc, out = quiet(mirror.main, ["status"])
                self.assertNotIn(f"{pybin} is", out)
            finally:
                sys.executable = real_py
            os.chmod(theirs, 0o755)                               # the person runs the one command
            rc, out = quiet(mirror.main, ["status"])
            self.assertNotIn(" is open: ", out)
            # 775 in the person's own group with nobody else in it: the ordinary Linux home, not named.
            os.chmod(theirs, 0o775)
            rc, out = quiet(mirror.main, ["status"])
            self.assertNotIn(theirs + " is", out)
            # The same group with a second member, the home passable: named, with that account.
            mirror.accounts = lambda: [("someone-else", 99999, frozenset({os.stat(theirs).st_gid}))]
            rc, out = quiet(mirror.main, ["status"])
            self.assertIn(f"{theirs} is open: another account on this computer can write in it (someone-else), so "
                          "another account could replace the skill's service or program in it, or below it.", out)
            # A home at 700 keeps everyone out: nothing under it is named, even open to all.
            os.chmod(theirs, 0o777)
            os.chmod(h.env["HOME"], 0o700)
            rc, out = quiet(mirror.main, ["status"])
            self.assertNotIn(theirs + " is", out)
            os.chmod(h.env["HOME"], 0o755)
            # Who is in the group could not be read: said as that, never taken as nobody.
            os.chmod(theirs, 0o775)
            mirror.accounts = lambda: None
            rc, out = quiet(mirror.main, ["status"])
            self.assertIn(f"{theirs} is writable by group {mirror.group_name(os.stat(theirs).st_gid)}; who is in "
                          "that group could not be read", out)
            mirror.accounts = lambda: []
            os.chmod(theirs, 0o755)
            real_stat = os.stat

            def unreadable(path, *a, **k):
                if path == theirs:
                    raise PermissionError(13, "Permission denied")
                return real_stat(path, *a, **k)
            os.stat = unreadable
            try:
                lines = mirror.open_folders(mirror.paths())
            finally:
                os.stat = real_stat
            self.assertIn(f"{theirs} could not be read (Permission denied), so whether another account can write in "
                          "it is not known; the skill's service or program is in it, or below it.", lines)
            # An account is its number, not its name: a second name with the person's number (and one with the
            # administrator's) is the person (or the administrator), not another account, so a passable home and a
            # folder open to the person's group name nothing. Read through accounts() itself, the list stood in.
            import pwd
            me = pwd.getpwuid(os.getuid())
            mirror.accounts = saved_accounts
            saved_getpwall = pwd.getpwall
            pwd.getpwall = lambda: [pwd.struct_passwd(("my-second-name", "x", os.getuid(), me.pw_gid, "", me.pw_dir,
                                                       "/bin/sh")),
                                    pwd.struct_passwd(("toor", "x", 0, 0, "", "/root", "/bin/sh"))]
            try:
                os.chmod(theirs, 0o775)
                self.assertEqual(mirror.accounts(), [])
                self.assertFalse([x for x in mirror.open_folders(mirror.paths()) if x.startswith(theirs + " ")])
            finally:
                pwd.getpwall = saved_getpwall
                os.chmod(theirs, 0o755)
            # On a Mac, the account list carries group 4294967294 (nobody's, -2 to the system's call, which takes a
            # signed number and refuses a larger one): read as the folder reads it, never a crash (seen on macOS 13:
            # setup and its dry run stopped with OverflowError there). The call stood in as the Mac's.
            real_getgrouplist = os.getgrouplist

            def macs(name, gid):
                if gid > 2 ** 31 - 1:
                    raise OverflowError("Python int too large to convert to C int")
                return [gid % 2 ** 64, 12]
            mirror.accounts = saved_accounts
            pwd.getpwall = lambda: [pwd.struct_passwd(("nobody", "*", 4294967294, 4294967294, "", "/var/empty",
                                                       "/usr/bin/false"))]
            os.getgrouplist = macs
            try:
                self.assertEqual(mirror.accounts(), [("nobody", 4294967294, frozenset({4294967294, 12}))])
                rc, out = quiet(mirror.main, ["setup", "--dry-run"])
                self.assertNotIn("Traceback", out)
            finally:
                os.getgrouplist = real_getgrouplist
                pwd.getpwall = saved_getpwall
            # One account whose groups cannot be read is said for that account, and every other one is still
            # worked out: never the whole list lost, never an error out of setup or status.
            def one_unreadable(name, gid):
                if name == "groups-unread":
                    raise OSError(5, "Input/output error")
                return [gid]
            pwd.getpwall = lambda: [pwd.struct_passwd(("groups-unread", "*", 99997, 99997, "", "/", "/bin/sh")),
                                    pwd.struct_passwd(("plain", "*", 99996, 99996, "", "/", "/bin/sh"))]
            os.getgrouplist = one_unreadable
            try:
                self.assertEqual(mirror.accounts(), [("groups-unread", 99997, None),
                                                     ("plain", 99996, frozenset({99996}))])
                os.chmod(theirs, 0o775)
                rc, out = quiet(mirror.main, ["status"])
                self.assertIn(f"{theirs}: whether another account can write in it is not fully known: the groups of 1 "
                              "account on this computer could not be read; the skill's service or program is in it, "
                              f"or below it. If it is not only yours: chmod go-w {shlex.quote(theirs)}", out)
                os.chmod(theirs, 0o777)
                rc, out = quiet(mirror.main, ["status"])
                self.assertIn(f"{theirs} is open: every account on this computer can write in it", out)
                os.chmod(theirs, 0o755)
                rc, out = quiet(mirror.main, ["status"])
                self.assertNotIn(theirs + ":", out)
                self.assertNotIn(theirs + " is", out)
            finally:
                os.getgrouplist = real_getgrouplist
                pwd.getpwall = saved_getpwall
                os.chmod(theirs, 0o755)
            # A folder that is not the person's gets no command (the person cannot change it): one another account
            # owns and can reach is named with that account; one of the administrator's, open to all, with root.
            def owned_by(uid):
                def fake(path, *a, **k):
                    s = real_stat(path, *a, **k)
                    if path != theirs:
                        return s
                    f = list(s[:10])
                    f[4] = uid
                    return os.stat_result(f)
                return fake
            mirror.accounts = lambda: [("the-other", 99998, frozenset())]
            for uid, mode, want in (
                    (99998, 0o755, f"{theirs} belongs to the account the-other, which could replace the skill's service "
                     "or program in it, or below it. You cannot change a folder that is not yours: only the computer's "
                     "administrator can hand it back to you. The skill leaves it as it is."),
                    (0, 0o777, f"{theirs} is open: every account on this computer can write in it, so another account "
                     "could replace the skill's service or program in it, or below it. It belongs to root, not to you: "
                     "only the computer's administrator can change it. The skill leaves it as it is.")):
                os.chmod(theirs, mode)
                os.stat = owned_by(uid)
                try:
                    lines = mirror.open_folders(mirror.paths())
                finally:
                    os.stat = real_stat
                self.assertIn(want, lines)
                self.assertEqual([x for x in lines if x.startswith(theirs + " ") and "chmod" in x], [], uid)
            os.chmod(theirs, 0o755)

    def test_G9_15_a_launch_agent_file_that_cannot_be_read_never_ends_a_command_in_a_traceback(self):
        """M150 (G9.15), the family's rule 6: a launch agent file at the skill's path that is not a well-formed
        property list (cut short, or changed by hand) ended setup and uninstall in a traceback (seen on the suite's
        Mac legs and in a stand-in trial). The one reader answers each planted form "could not be read", with where
        the read stopped and never a byte of the file; with launchd holding no job, setup stops with nothing changed
        and the one step a person can take, which is taken here; uninstall names the file, leaves it byte for byte
        and goes on, removing only what it can show to be this home's (each world planted); status gives one
        problem line. The Mac's road runs on every leg, the system stood in for."""
        mark = "zq-planted-bytes-7c1e"                         # a byte of the file must never be said
        saved, rel = mirror.system, mirror.release_check
        door = doors_the_hosts_way(saved, always=True)
        mirror.system = lambda: ("darwin", "arm64")
        mirror.read_door = lambda p, port: ("none", "no program listens on it")
        mirror.release_check = lambda st, force=False: []
        try:
            with Home() as h:
                p = mirror.paths()
                whole = mirror.plist_text(p).replace(mirror.LAUNCHD_LABEL, mirror.LAUNCHD_LABEL + "." + mark, 1)
                whole = whole.encode("utf-8")
                binary = plistlib.dumps({"Label": mark, "ProgramArguments": ["python3", mark]},
                                        fmt=plistlib.FMT_BINARY)
                forms = [
                    ("0 bytes", b""),
                    ("cut short in its head", whole[:len(whole) // 5]),
                    ("cut short in its arguments", whole[:whole.index(b"<array>") + 20]),
                    ("cut short at its end", whole[:-4]),
                    ("an unescaped ampersand", whole.replace(mark.encode(), mark.encode() + b" & more", 1)),
                    ("not XML", f"[Service]\nExecStart=/bin/true {mark}\n".encode()),
                    ("a binary list's header cut short", binary[:12]),
                    ("a top that is an array", plistlib.dumps([mark])),
                    ("ProgramArguments as a word", plistlib.dumps({"Label": mark, "ProgramArguments": "python3"})),
                    ("an argument that is a number", plistlib.dumps({"Label": mark,
                                                                     "ProgramArguments": ["python3", 3]})),
                ]
            # The commands, each planted form at the skill's own path, launchd holding no job of either label.
            for name, data in forms:
                with Home() as h, self.subTest(form=name):
                    p = mirror.paths()
                    la = os.path.dirname(p["plist"])
                    os.makedirs(la)
                    h.gone |= {mirror.LAUNCHD_LABEL, mirror.SYNC_LABEL}
                    with open(p["plist"], "wb") as fh:
                        fh.write(data)
                    for cmd in (["setup", "--dry-run"], ["setup"]):
                        rc, out = quiet(mirror.main, cmd)
                        self.assertEqual(rc, 2, (cmd, out))
                        self.assertIn(f"a launch agent file at the skill's path ({p['plist']}), and it could not be "
                                      "read: ", out)
                        self.assertIn("cannot tell whose it is", out)
                        self.assertIn("Nothing was changed.", out)
                        self.assertIn(f"The one next step: move that file out of {la}", out)
                        self.assertNotIn(mark, out)
                        self.assertEqual(fread_bytes(p["plist"]), data)
                        self.assertEqual(sorted(os.listdir(la)), [os.path.basename(p["plist"])])
                        self.assertFalse(os.path.exists(p["data"]), "nothing made")
                    # the one step, taken as printed: the file moved out of that folder, setup no longer stops on it
                    os.rename(p["plist"], os.path.join(h.tmp, "kept.plist"))
                    rc, out = quiet(mirror.main, ["setup", "--dry-run"])
                    self.assertNotIn("could not be read", out)
                    self.assertEqual(rc, 0, out)
                    os.rename(os.path.join(h.tmp, "kept.plist"), p["plist"])
                    # uninstall: named, left byte for byte, and it goes on to its end (rule 8)
                    n = len(h.calls)
                    rc, out = quiet(mirror.main, ["uninstall"])
                    self.assertEqual(rc, 0, out)
                    self.assertIn(f"Not taken, and left byte for byte: {p['plist']}. It could not be read: ", out)
                    self.assertIn(f"It is still in {la}, the folder launchd reads at a login", out)
                    self.assertNotIn(mark, out)
                    self.assertTrue(out.strip().endswith("it is yours, to keep or delete after you look at it."), out)
                    self.assertEqual(fread_bytes(p["plist"]), data)
                    self.assertFalse([c for c in h.calls[n:] if c[:2] == ["launchctl", "bootout"]], out)
            # The worlds for the limit: the forge's file cannot be read, and the sync's file
            # is (i) this home's own bytes, (ii) another home's, (iii) cannot be read either. Setup changes nothing in
            # any; uninstall removes the sync's file only when it is this home's own, and takes off a loaded job only
            # when launchd shows it running this home's copy.
            for world in ("own", "another home's", "unreadable"):
                with Home() as h, self.subTest(world=world):
                    p = mirror.paths()
                    la = os.path.dirname(p["plist"])
                    os.makedirs(la)
                    sp = os.path.join(la, mirror.SYNC_LABEL + ".plist")
                    if world == "own":                             # written by the tool, so recorded as its own
                        mirror.write_file(sp, mirror.sync_plist_text(p, 15))
                    else:                                          # put there by something else
                        with open(sp, "w") as fh:
                            fh.write(mirror.sync_plist_text(mirror.paths(os.path.join(h.tmp, "other")), 15)
                                     if world == "another home's" else "<plist><dict>")
                    with open(p["plist"], "wb") as fh:
                        fh.write(forms[4][1])
                    planted = {x: fread_bytes(x) for x in (p["plist"], sp)}
                    h.gone |= {mirror.LAUNCHD_LABEL}               # launchd holds no forge job; the sync's as below
                    if world == "unreadable":
                        h.gone |= {mirror.SYNC_LABEL}
                    rc, out = quiet(mirror.main, ["setup"])
                    self.assertEqual(rc, 2, out)
                    self.assertEqual({x: fread_bytes(x) for x in planted}, planted)
                    n = len(h.calls)
                    rc, out = quiet(mirror.main, ["uninstall"])
                    self.assertEqual(rc, 0, out)
                    offs = [c for c in h.calls[n:] if c[:2] == ["launchctl", "bootout"]]
                    self.assertEqual(fread_bytes(p["plist"]), planted[p["plist"]])
                    if world == "own":                             # its loaded job runs this home's copy: taken off
                        self.assertFalse(os.path.exists(sp), out)
                        self.assertEqual(offs, [["launchctl", "bootout", f"gui/{os.getuid()}/{mirror.SYNC_LABEL}"]])
                    else:
                        self.assertEqual(fread_bytes(sp), planted[sp], out)
                        self.assertEqual(offs, [], out)
                        self.assertIn(sp, out)
                        if world == "another home's":
                            self.assertIn("which runs another copy than this home's", out)
                    self.assertTrue(out.strip().endswith("it is yours, to keep or delete after you look at it."), out)
            # A reviewer's world C (11-26): under the limit, a job launchd could not answer for (no arguments
            # block, a code neither 0 nor 113, a timeout) is "could not be read": never "loaded" and left while the
            # rest goes. Uninstall is a Not now: nothing taken off, nothing removed (this home's installed copy
            # stays), exit 3, and the step that ends it, which is taken here.
            with Home() as h:
                p = mirror.paths()
                la = os.path.dirname(p["plist"])
                os.makedirs(la)
                sp = os.path.join(la, mirror.SYNC_LABEL + ".plist")
                mirror.write_file(sp, mirror.sync_plist_text(p, 15))
                os.makedirs(os.path.dirname(p["script"]), exist_ok=True)
                mirror.write_file(p["script"], fread(SCRIPT))
                with open(p["plist"], "wb") as fh:
                    fh.write(forms[4][1])
                h.gone |= {mirror.LAUNCHD_LABEL}
                h.launchd_args = {mirror.SYNC_LABEL: None}
                planted = {x: fread_bytes(x) for x in (p["plist"], sp, p["script"])}
                n = len(h.calls)
                rc, out = quiet(mirror.main, ["uninstall"])
                self.assertEqual(rc, 3, out)
                self.assertIn("\nNot now: whether launchd holds the sync's job", out)
                self.assertIn("Nothing was removed and nothing was taken off.", out)
                self.assertIn(f"The one next step: move {p['plist']} out of {la}", out)
                self.assertNotIn("loaded in launchd", out)
                self.assertNotIn(mark, out)
                self.assertEqual({x: fread_bytes(x) for x in planted}, planted)
                self.assertFalse([c for c in h.calls[n:] if c[:2] == ["launchctl", "bootout"]], out)
                os.rename(p["plist"], os.path.join(h.tmp, "kept.plist"))      # the step, as printed
                rc, out = quiet(mirror.main, ["uninstall"])
                self.assertEqual(rc, 0, out)
                self.assertNotIn("Not now", out)
            # status: the same reader, one problem line, no traceback
            with Home() as h:
                p = mirror.paths()
                os.makedirs(os.path.dirname(p["plist"]))
                for d in (p["data"], p["conf"]):
                    os.makedirs(d, mode=0o700, exist_ok=True)
                mirror.save_state(p, {"port": 1})
                with open(p["mirror_key"], "w") as fh:
                    fh.write("5" * 40 + "\n")           # made up, in the form the forge gives (M156)
                with open(p["plist"], "wb") as fh:
                    fh.write(forms[4][1])
                h.gone |= {mirror.LAUNCHD_LABEL, mirror.SYNC_LABEL}
                mirror.DOOR_SEEN.clear()
                rc, out = status()
                said = [x for x in out.split("\n") if p["plist"] in x]
                self.assertEqual(len(said), 1, out)
                self.assertTrue(said[0].startswith(f"problem: the forge's launch agent file {p['plist']} could not be "
                                                   "read: it is not a well-formed property list (the read stopped at "
                                                   "line "), said[0])
                self.assertIn("move that file out of", said[0])
                self.assertNotIn(mark, out)
                self.assertEqual(fread_bytes(p["plist"]), forms[4][1])
            # The one reader, each form alone: "could not be read", where it stopped, never a byte of the file
            with Home() as h:
                p = mirror.paths()
                f = os.path.join(h.tmp, "planted.plist")
                for name, data in forms:
                    with open(f, "wb") as fh:
                        fh.write(data)
                    answer, why = mirror.plist_job_args(f)
                    self.assertEqual(answer, "could not be read", (name, why))
                    self.assertNotIn(mark, why, name)
                    if name == "an unescaped ampersand":
                        self.assertTrue(why.startswith("it is not a well-formed property list (the read stopped at "
                                                       "line "), why)
                self.assertEqual(mirror.plist_job_args(os.path.join(h.tmp, "nothing.plist")), ("not there", None))
                os.remove(f)
                os.makedirs(f)                                     # a file that cannot be opened: a folder by its name
                self.assertEqual(mirror.plist_job_args(f), ("could not be read", "it could not be opened "
                                                            "(IsADirectoryError)"))
                os.rmdir(f)
                if os.geteuid() != 0:
                    with open(f, "wb") as fh:
                        fh.write(whole)
                    os.chmod(f, 0)
                    try:
                        self.assertEqual(mirror.plist_job_args(f), ("could not be read", "it could not be opened "
                                                                    "(PermissionError)"))
                    finally:
                        os.chmod(f, 0o600)
                with open(f, "wb") as fh:                          # the control: the file as setup writes it, read
                    fh.write(mirror.plist_text(p).encode("utf-8"))
                self.assertEqual(mirror.plist_job_args(f), ("read", mirror.forge_argv(p)))
        finally:
            mirror.system, mirror.release_check = saved, rel
            mirror.read_door, mirror.door_floor = door

    def test_G9_15_a_service_file_that_cannot_be_opened_is_never_this_homes_and_never_a_traceback(self):
        """M150 (G9.15), a reviewer's trial (11-26: 16 tracebacks of 16 in a made home): a service file of mode 000
        ended setup --dry-run and uninstall in a PermissionError, on both roads, the forge's file and the sync's, this
        home's own bytes or not, uninstall's after the jobs were told off and before any of its last lines. "Is this
        service file as this home wrote it" has three answers at every reader; the third is never this home's (never
        written over, never removed) and never "not there": setup and its dry run stop with nothing changed, the
        file, the error's class and a step, which is taken here; uninstall names it, leaves it byte for byte and goes
        to its end, saying what it switched off; status gives one problem line. Both roads, the system stood in for;
        the service manager holding this home's jobs (loaded before the file became unreadable) or holding none."""
        saved, rel = mirror.system, mirror.release_check
        saved_doors = doors_the_hosts_way(saved, always=True)
        real_sha = mirror.sha256_of
        mirror.release_check = lambda st, force=False: []
        try:
            for name in (("linux", "x86_64"), ("darwin", "arm64")):
                mirror.system = lambda name=name: name
                mac = name[0] == "darwin"
                for which in range(3 if not mac else 2):
                    for held in ("held", "none"):
                        for own in (True, False):
                            with Home() as h, self.subTest(system=name[0], file=which, manager=held, own=own):
                                p = mirror.paths()
                                files = mirror.service_files(p)
                                texts = ([mirror.plist_text(p), mirror.sync_plist_text(p, 15)] if mac else
                                         [mirror.unit_text(p), mirror.sync_unit_text(p), mirror.sync_timer_text(15)])
                                for x, text in zip(files, texts):           # as setup writes them, and records them
                                    mirror.write_file(x, text)
                                for d in (p["data"], p["conf"]):
                                    os.makedirs(d, mode=0o700, exist_ok=True)
                                mirror.save_state(p, {"port": 1})
                                mirror.write_file(p["mirror_key"], "5" * 40 + "\n", 0o600)
                                f = files[which]
                                if not own:                                 # bytes this home did not write
                                    os.remove(f)
                                    with open(f, "w") as fh:
                                        fh.write(texts[which] + ("<!-- another -->\n" if mac else "# another\n"))
                                whole = fread_bytes(f)
                                if mac:
                                    if held == "held":                      # loaded at setup, before the chmod
                                        h.launchd_args = {mirror.LAUNCHD_LABEL: (mirror.forge_argv(p), {}),
                                                          mirror.SYNC_LABEL: (mirror.sync_argv(p), {})}
                                    else:
                                        h.gone |= {mirror.LAUNCHD_LABEL, mirror.SYNC_LABEL}
                                elif held == "held":
                                    h.bus_starts = [["/usr/bin/python3", mirror.forge_argv(p), False, 0, 0, 0, 0, 0,
                                                     0, 0]]
                                os.chmod(f, 0)
                                if os.access(f, os.R_OK):                   # root reads mode 000: the read's fault stood in
                                    def sha(path, real=real_sha, f=f):
                                        if path == f:
                                            raise PermissionError(13, "Permission denied")
                                        return real(path)
                                    mirror.sha256_of = sha
                                try:
                                    for cmd in (["setup", "--dry-run"], ["setup", "--linger"]):
                                        rc, out = quiet(mirror.main, cmd)
                                        self.assertEqual(rc, 2, (cmd, out))
                                        self.assertIn(f, out)
                                        self.assertIn("could not be read: it could not be opened (PermissionError)", out)
                                        self.assertIn("Nothing was changed.", out)
                                        self.assertIn(f"out of {os.path.dirname(f)}", out)
                                        self.assertNotIn("read that file", out)
                                        self.assertEqual(os.stat(f).st_mode & 0o777, 0)
                                    # the step, as printed: the file moved out of that folder, setup no longer stops on it
                                    os.rename(f, os.path.join(h.tmp, "kept"))
                                    rc, out = quiet(mirror.main, ["setup", "--dry-run"])
                                    self.assertNotIn("could not be read", out)
                                    self.assertNotIn("Traceback", out)
                                    os.rename(os.path.join(h.tmp, "kept"), f)
                                    rc, out = status()
                                    said = [x for x in out.split("\n") if f in x]
                                    self.assertEqual(len(said), 1, out)
                                    self.assertTrue(said[0].startswith("problem: "), said[0])
                                    self.assertIn("could not be opened (PermissionError)", said[0])
                                    self.assertIn(f"move that file out of {os.path.dirname(f)}", said[0])
                                    n = len(h.calls)
                                    rc, out = quiet(mirror.main, ["uninstall"])
                                    self.assertEqual(rc, 0, out)
                                    self.assertIn(f"{f} (it could not be read: it could not be opened (PermissionError)"
                                                  if (which or held == "held") else f"Not taken, and left byte for "
                                                  f"byte: {f}. It could not be read: it could not be opened "
                                                  "(PermissionError)", out)
                                    self.assertTrue(out.strip().endswith("it is yours, to keep or delete after you look "
                                                                         "at it."), out)
                                    self.assertTrue(os.path.lexists(f), out)
                                    self.assertEqual(os.stat(f).st_mode & 0o777, 0)
                                    for x in files:                        # the others: this home's, so taken
                                        if x != f:
                                            self.assertFalse(os.path.lexists(x), (x, out))
                                    offs = [c for c in h.calls[n:] if c[:2] in (["launchctl", "bootout"],
                                                                                ["systemctl", "--user"])]
                                    if held == "none" and (mac or which == 0):
                                        # M163 (G9.18): with the forge's unit unread, Linux switches off only what
                                        # systemd shows is this home's, as the Mac's road does
                                        if which == 0:
                                            self.assertNotIn("Switched off first, and read off before anything was "
                                                             "removed: the forge's", out)
                                    else:
                                        self.assertIn("Switched off first, and read off before anything was "
                                                      "removed: the forge's", out)
                                        self.assertTrue(offs, out)
                                finally:
                                    mirror.sha256_of = real_sha
                                    os.chmod(f, 0o600)
                                self.assertEqual(fread_bytes(f), whole)
        finally:
            mirror.system, mirror.release_check = saved, rel
            mirror.read_door, mirror.door_floor = saved_doors
            mirror.sha256_of = real_sha

    def test_G9_16_no_login_at_the_macs_screen_never_keeps_a_person_from_leaving(self):
        """M152 (G9.16; seen 2026-11-26 in a stand-in trial in a made home, not on a Mac): with no login at the Mac's
        screen, launchctl answers 125 for the account's domain and for each job (seen 2026-11-24 over ssh on macOS
        13.7.8). G9.12's read of the forge's job took that for "could not be read", so uninstall stopped (exit 2) with
        "read that file, and run uninstall with the copy it names": over this home's own file with one line a person
        added (D), every time; over a file that is not well-formed (B), naming a file the tool itself could not read.
        That answer is read as launchd_off reads it: nothing of this account is loaded, and the file's own words decide.
        Each world with and without a login at the screen."""
        saved = mirror.system
        saved_doors = doors_the_hosts_way(saved, always=True)
        mirror.system = lambda: ("darwin", "arm64")
        bad = None
        try:
            for screen in (True, False):
                for world in ("D: this home's file, a line added", "B: not well-formed"):
                    with Home() as h, self.subTest(screen=screen, world=world):
                        h.mac_session = screen
                        p = mirror.paths()
                        la = os.path.dirname(p["plist"])
                        sp = os.path.join(la, mirror.SYNC_LABEL + ".plist")
                        mirror.write_file(p["plist"], mirror.plist_text(p))
                        mirror.write_file(sp, mirror.sync_plist_text(p, 15))
                        if world.startswith("D"):
                            with open(p["plist"], "a") as fh:
                                fh.write("<!-- the person's own line -->\n")
                        else:
                            text = mirror.plist_text(p)
                            bad = text.replace("</string>", " & more</string>", 1)
                            with open(p["plist"], "w") as fh:
                                fh.write(bad)
                        if screen:                                 # logged in: launchd holds no forge job now
                            h.gone |= {mirror.LAUNCHD_LABEL}
                        planted = fread_bytes(p["plist"])
                        rc, out = quiet(mirror.main, ["setup", "--dry-run"])
                        self.assertEqual(rc, 2, out)
                        self.assertNotIn("read that file", out)
                        self.assertIn(f"out of {la}" if world.startswith("B") else "move it out of that folder", out)
                        for _ in range(2):                         # twice running: never the same stop again
                            rc, out = quiet(mirror.main, ["uninstall"])
                            self.assertEqual(rc, 0, out)
                            self.assertNotIn("read that file", out)
                            self.assertIn(p["plist"], out)
                            self.assertEqual(fread_bytes(p["plist"]), planted)
                            self.assertTrue(out.strip().endswith("it is yours, to keep or delete after you look at "
                                                                 "it."), out)
                            if world.startswith("D"):
                                self.assertIn("not as the skill wrote it", out)
                            else:
                                self.assertIn(f"Not taken, and left byte for byte: {p['plist']}. It could not be "
                                              "read: it is not a well-formed property list", out)
                        self.assertFalse(os.path.exists(sp), out)
        finally:
            mirror.system = saved
            mirror.read_door, mirror.door_floor = saved_doors

    def test_G9_17_a_unit_that_names_no_start_line_never_keeps_a_person_from_leaving(self):
        """M153 (G9.17; seen 2026-11-26 in a stand-in trial in a made home, systemd stood in for): on Linux, the
        forge's unit at the skill's path opens but names no start line: the line [Service] alone (L1), a unit the
        person masked, a link to /dev/null as systemctl mask leaves it (L2), a file the person emptied (L3). Setup's
        dry run and uninstall stopped (exit 2) with "read that file, and run uninstall with the copy it names": the
        file names no copy, so the step led back to the stop. A unit systemd was read for that shows no start line
        runs nothing, so it is no other install. NOT SEEN BY US: what systemd itself answers for a masked unit (the
        stand-in reads the path's text, which for /dev/null is nothing). L4 (this home's own unit with one comment
        line) is the control."""
        saved = mirror.system
        saved_doors = doors_the_hosts_way(saved, always=True)
        mirror.system = lambda: ("linux", "x86_64")
        try:
            for world in ("L1 [Service]", "L2 masked", "L3 emptied", "L4 a comment line"):
                with Home() as h, self.subTest(world=world):
                    p = mirror.paths()
                    files = mirror.service_files(p)
                    for f, text in zip(files, [mirror.unit_text(p), mirror.sync_unit_text(p),
                                               mirror.sync_timer_text(15)]):
                        mirror.write_file(f, text)           # as setup writes them, and records them
                    os.makedirs(os.path.dirname(p["script"]), exist_ok=True)
                    mirror.write_file(p["script"], "# the installed copy\n")
                    forge = files[0]
                    text = mirror.unit_text(p)
                    os.remove(forge)
                    if world.startswith("L2"):
                        os.symlink("/dev/null", forge)
                    else:
                        with open(forge, "w") as fh:
                            fh.write({"L1": "[Service]\n", "L3": "", "L4": text + "# the person's own line\n"}[world[:2]])
                    planted = os.readlink(forge) if world.startswith("L2") else fread_bytes(forge)
                    rc, out = quiet(mirror.main, ["setup", "--dry-run"])
                    self.assertEqual(rc, 2, out)
                    self.assertNotIn("read that file", out)
                    self.assertIn("move it out of that folder", out)
                    for _ in range(2):                         # twice running: never the same stop again
                        rc, out = quiet(mirror.main, ["uninstall"])
                        self.assertEqual(rc, 0, out)
                        self.assertNotIn("read that file", out)
                        self.assertIn(forge, out)
                        self.assertIn("not as the skill wrote it", out)
                        self.assertEqual(os.readlink(forge) if world.startswith("L2") else fread_bytes(forge), planted)
                        self.assertTrue(out.strip().endswith("it is yours, to keep or delete after you look at it."),
                                        out)
                    self.assertFalse(os.path.exists(files[1]), out)   # the sync's own unit, this home's: taken
                    self.assertFalse(os.path.exists(p["script"]), out)
        finally:
            mirror.system = saved
            mirror.read_door, mirror.door_floor = saved_doors

    def test_G9_18_under_the_limit_uninstall_switches_off_only_what_systemd_shows_is_this_homes(self):
        """M163 (G9.18; seen 2026-11-26 in a stand-in trial in a made home, systemd stood in for): on Linux, while the
        forge's unit at the skill's path could not be opened, uninstall told off every unit, the sync's timer of
        another home with them, without showing any to be this home's. Now, as the Mac's road does: a unit is told
        off only when systemd's own start line for it names this home's copy. Q: the sync's service names another
        home's copy, its timer running: named and left running, its two files byte for byte. P (the control): it
        names this home's copy: told off. U: systemd could not answer: "Not now", exit 3, nothing told off, and the
        step taken as printed (the forge's unit moved out of that folder) lets uninstall go on. NOT SEEN BY US: what
        real systemd holds for a unit whose file is mode 000 (the stand-in reads the file, and answers no start line)."""
        saved, rel = mirror.system, mirror.release_check
        saved_doors = doors_the_hosts_way(saved, always=True)
        mirror.system, mirror.release_check = (lambda: ("linux", "x86_64")), (lambda st, force=False: [])
        other = "/srv/another-home"
        try:
            for world in ("Q another home's sync", "P this home's sync", "U systemd does not answer"):
                with Home() as h, self.subTest(world=world):
                    p = mirror.paths()
                    forge, sync_unit, timer = mirror.service_files(p)
                    text = mirror.sync_unit_text(p)
                    if world.startswith("Q"):
                        self.assertIn(os.environ["WSM_HOME"], text)
                        text = text.replace(os.environ["WSM_HOME"], other)
                    os.makedirs(os.path.dirname(forge), exist_ok=True)
                    with open(sync_unit, "w") as fh:
                        fh.write(text)
                    with open(timer, "w") as fh:
                        fh.write(mirror.sync_timer_text(15))
                    mirror.write_file(forge, mirror.unit_text(p))
                    os.makedirs(os.path.dirname(p["script"]), exist_ok=True)
                    mirror.write_file(p["script"], "# the installed copy\n")
                    before = {f: fread_bytes(f) for f in (sync_unit, timer)}
                    os.chmod(forge, 0)
                    real_sum = mirror.sha256_of
                    if os.geteuid() == 0:            # root reads mode 000: there (only) the read's fault is stood in
                        mirror.sha256_of = lambda f, _r=real_sum: (_ for _ in ()).throw(PermissionError(13, "denied")) \
                            if f == forge else _r(f)
                    h.units_up |= {mirror.SERVICE, mirror.SYNC_UNIT + ".timer"}
                    if world.startswith("U"):
                        h.bus_answer = (1, "")
                    try:
                        n = len(h.calls)
                        rc, out = quiet(mirror.main, ["uninstall"])
                        offs = [c for c in h.calls[n:] if c[:2] == ["systemctl", "--user"] and
                                any(v in c for v in ("disable", "stop", "kill"))]
                        if world.startswith("U"):
                            self.assertEqual(rc, 3, out)
                            self.assertIn("Not now", out)
                            self.assertEqual(offs, [], out)
                            self.assertIn(f"move {forge} out of {os.path.dirname(forge)}", out)
                            self.assertTrue(os.path.exists(p["script"]), out)
                            os.chmod(forge, 0o600)
                            kept = os.path.join(h.tmp, "kept-by-the-person")
                            os.makedirs(kept)
                            os.rename(forge, os.path.join(kept, os.path.basename(forge)))   # the step, as printed
                            h.bus_answer = None
                            rc, out = quiet(mirror.main, ["uninstall"])
                            self.assertEqual(rc, 0, out)
                            continue
                        self.assertEqual(rc, 0, out)
                        timer_off = any(mirror.SYNC_UNIT + ".timer" in c for c in offs)
                        if world.startswith("Q"):
                            self.assertFalse(timer_off, (offs, out))
                            self.assertIn(mirror.SYNC_UNIT + ".timer", h.units_up)
                            self.assertIn("runs another copy than this home's", out)
                            self.assertEqual({f: fread_bytes(f) for f in before}, before)
                            self.assertNotIn("The mirror is off this computer", out)
                        else:
                            self.assertTrue(timer_off, (offs, out))
                            self.assertNotIn(mirror.SYNC_UNIT + ".timer", h.units_up)
                        self.assertFalse(os.path.exists(p["script"]), out)
                    finally:
                        mirror.sha256_of = real_sum
                        if os.path.exists(forge):
                            os.chmod(forge, 0o600)
        finally:
            mirror.system, mirror.release_check = saved, rel
            mirror.read_door, mirror.door_floor = saved_doors

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
            kinds, real_read = [], mirror.read_door

            def record(p, port):                 # G8.7, the owner's condition ii: each step's read of the door
                try:
                    k = real_read(p, port)
                except mirror.DoorUnread as e:
                    kinds.append("unread: " + str(e))
                    raise
                kinds.append(k[0])
                return k
            mirror.read_door = record
            stack.callback(setattr, mirror, "read_door", real_read)
            rc, out = quiet(mirror.main, ["upgrade-forge"])
            mirror.read_door = real_read
            self.assertEqual(set(kinds) - {"mine", "none"}, set(), kinds)       # never "other" for our own forge
            self.assertEqual((kinds[0], kinds[-1]), ("mine", "mine"), kinds)     # 28.3.0 before, 28.4.0 after
            self.assertIn("none", kinds)                                          # stopped between
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
        """The maintainer's ruling on M13 (2026-11-23): before 0.1.1 there is no newer release to move to, so what is
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

    def test_G9_9_uninstall_takes_away_the_folders_setup_made_once_they_are_empty(self):
        """M65 (seen 2026-11-23 on a fresh stock machine, the family's rule 23): setup made ~/.config,
        ~/.config/systemd and ~/.config/systemd/user, which a stock home does not have, and uninstall left them. The
        dry run names them, and the links systemctl makes, first; setup records them before it makes anything;
        uninstall takes each away again once it is empty, and the census names one that is not. A record that could
        not be read takes none of them and is never written over (rule 6)."""
        mac = mirror.system()[0] == "darwin"
        with Home(linger="yes") as h:
            home = os.environ["HOME"]
            p = mirror.paths()
            svc = os.path.dirname(p["plist"] if mac else p["unit"])
            top = os.path.join(home, "Library" if mac else ".config")
            chain, d = [], svc
            while d != home:
                chain.append(d)
                d = os.path.dirname(d)
            h.place_gitea()
            rc, out = quiet(mirror.main, ["setup", "--dry-run"])
            self.assertEqual(rc, 0, out)
            self.assertIn("make the folders ", out)
            for d in chain:
                self.assertIn(d, out)
            if not mac:
                self.assertIn(f"systemctl links them in {svc}/default.target.wants and {svc}/timers.target.wants, and "
                              "uninstall takes the units and those links away", out)
            self.assertFalse(os.path.exists(top), "the dry run made a folder")
            rc, out = h.setup()
            self.assertEqual(rc, 0, out)
            made = mirror.load_json(p["state"], {})["made_folders"]
            for d in chain:
                self.assertIn(d, made)
            others = os.path.join(top, "another-programs-file")
            with open(others, "w") as f:
                f.write("not the skill's\n")
            rc, out = quiet(mirror.main, ["uninstall"])
            self.assertEqual(rc, 0, out)
            for d in chain[:-1]:
                self.assertFalse(os.path.exists(d), f"{d}: setup made it and uninstall left it, empty")
                self.assertIn(f"  {d}: an empty folder setup made", out)
            self.assertTrue(os.path.exists(others), "uninstall touched another program's file")
            self.assertIn(f"  {top}: a folder setup made, left because other files are in it now. It is yours.", out)
        with Home(linger="yes") as h:
            p = mirror.paths()
            svc = os.path.dirname(p["plist"] if mac else p["unit"])
            h.place_gitea()
            self.assertEqual(h.setup()[0], 0)
            with open(p["state"], "w") as f:
                f.write("{not a record")
            rc, out = quiet(mirror.main, ["uninstall"])
            self.assertTrue(os.path.isdir(svc), "a folder setup made was taken on a record that could not be read")
            self.assertIn(f"the record of them, {p['state']}, could not be read, so none was removed.", out)
            with open(p["state"]) as f:
                self.assertEqual(f.read(), "{not a record", "the record that could not be read was written over")

    def test_G9_8_the_census_names_everything_still_here_and_whose_it_is(self):
        """M63, the family's rule 8 (2026-11-23: an agent invents where the tool is silent): the census names
        everything still on this computer because of the skill, kept on purpose or simply left, with where it is and
        whose it is to delete: the folder the skill was downloaded into (a copy of its repository, or Claude Code's
        plugin folder and entry), and the service's journal lines. After uninstall, no file of the skill's is left
        in the person's home outside the folder it names as kept."""
        with World(self) as w:
            w.gh.add_repo("someone", "alpha")
            w.gh.commit("someone/alpha", {"a": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            p = w.p
            self.assertEqual(mirror.load_json(p["state"], {})["setup_from"], ROOT)
            rc, out = quiet(mirror.main, ["uninstall"])
            self.assertEqual(rc, 0, out)
            lines = out.strip().splitlines()
            self.assertIn(f"  {ROOT}: the folder the skill was downloaded into (a copy of its repository). It is yours; "
                          "delete it like any folder when you want. Nothing runs from it now.", lines)
            if mirror.system()[0] != "darwin":
                self.assertEqual(lines[-1], "  Any lines of the service in this computer's journal (journalctl --user): "
                                            "the system ages them out by itself.")
            self.assertNotIn("Nothing of the skill's is left", out)
            left = [os.path.join(d, f) for d, _, fs in os.walk(w.home.tmp) for f in fs]
            self.assertEqual([x for x in left if not x.startswith(p["data"] + os.sep)
                              and os.path.basename(x) != mirror.SUITE_MARKER], [],
                             "a file of the skill's is left that the census does not name as kept")
            # A plugin: Claude Code's folder and entry are Claude Code's, with its own command where it was seen. They
            # are named by Claude Code's own list (G9.14, the family's rule 29 (e)), never by a version folder.
            st = mirror.load_json(p["state"], {})
            reg = os.path.join(w.home.tmp, "cc", "plugins", "installed_plugins.json")
            os.makedirs(os.path.dirname(reg))
            st.pop("setup_from", None)
            st["plugin_registry"], st["plugin_catalog"] = reg, "thewindstorm"
            mirror.save_state(p, st)
            ours = {"scope": "user", "version": "0.1.0",
                    "installPath": os.path.join(os.path.dirname(reg), "cache", "thewindstorm", mirror.PLUGIN, "0.1.0")}
            whose = ("They are Claude Code's. Claude Code's own plugin uninstall takes this plugin off Claude Code's "
                     "list at once; its folder in Claude Code's cache stays, and Claude Code's documentation says it "
                     "removes that folder 14 days later. Nothing runs from them now."
                     + (f" Its command for this plugin:\n      claude plugin uninstall {mirror.PLUGIN}@thewindstorm"
                        if mirror.system()[0] != "darwin" else ""))
            with open(reg, "w") as f:
                json.dump({"version": 2, "plugins": {mirror.PLUGIN + "@thewindstorm": [ours]}}, f)
            out = quiet(mirror.main, ["uninstall"])[1]
            self.assertIn("\n  Claude Code's folder for this skill as a plugin, and its entry in Claude Code. " + whose + "\n",
                          out)
            if mirror.system()[0] != "darwin":
                # The family's rule 31 (iv): the catalog's name is Claude Code's list's, not the tool's, so the one
                # command that carries it is quoted for the shell, alone on its line.
                with open(reg, "w") as f:
                    json.dump({"version": 2, "plugins": {mirror.PLUGIN + "@my catalog": [ours]}}, f)
                out = quiet(mirror.main, ["uninstall"])[1]
                self.assertIn(f"Its command for this plugin:\n      claude plugin uninstall '{mirror.PLUGIN}@my catalog'\n",
                              out)
            with open(reg, "w") as f:
                f.write("{not a list")
            out = quiet(mirror.main, ["uninstall"])[1]
            self.assertIn("\n  Claude Code's folder for this skill as a plugin, and its entry in Claude Code, if Claude Code "
                          f"still has them (its list of plugins, {reg}, could not be read). " + whose + "\n", out)
            with open(reg, "w") as f:
                json.dump({"version": 2, "plugins": {}}, f)
            out = quiet(mirror.main, ["uninstall"])[1]
            self.assertNotIn("Claude Code's folder for this skill as a plugin", out, "Claude Code's list names it nowhere")
            # M104: a record an earlier build wrote keeps the plugin's version folder as setup_from (with or without
            # the list's path beside it). It is read without error, the list and the catalog are read from that path,
            # and the path itself is never said, there or gone.
            ver = os.path.join(os.path.dirname(reg), "cache", "thewindstorm", mirror.PLUGIN, "0.0.9")
            with open(reg, "w") as f:
                json.dump({"version": 2, "plugins": {mirror.PLUGIN + "@thewindstorm": [ours]}}, f)
            for there in (True, False):
                if there:
                    os.makedirs(os.path.join(ver, "scripts"))
                else:
                    shutil.rmtree(ver)
                for keep_list in (True, False):
                    old_st = {k: v for k, v in st.items() if k not in ("plugin_registry", "plugin_catalog")}
                    old_st["setup_from"] = ver
                    if keep_list:
                        old_st["plugin_registry"] = reg
                    mirror.save_state(p, old_st)
                    rc, out = quiet(mirror.main, ["uninstall"])
                    self.assertEqual(rc, 0, out)
                    self.assertNotIn(ver, out, f"a version folder said (there={there}, list kept={keep_list})")
                    self.assertIn("\n  Claude Code's folder for this skill as a plugin, and its entry in Claude Code. " + whose
                                  + "\n", out, f"there={there}, list kept={keep_list}")
                    self.assertNotIn("the folder the skill was downloaded into (a copy", out)
            st.pop("plugin_registry")
            st.pop("plugin_catalog")
            mirror.save_state(p, st)
            # A script run on its own (from a downloads folder, say) is named as that one file, never the folder
            # around it: a census must never call a person's home "the folder the skill was downloaded into".
            lone = os.path.join(w.home.tmp, "home", "Downloads", "mirror.py")
            os.makedirs(os.path.dirname(lone))
            shutil.copyfile(SCRIPT, lone)
            self.assertEqual(mirror.downloaded_from(lone), ("file", lone))
            self.assertEqual(mirror.downloaded_from(SCRIPT), ("folder", ROOT))
            os.makedirs(os.path.join(w.home.tmp, "home", "scripts"))
            self.assertEqual(mirror.downloaded_from(os.path.join(w.home.tmp, "home", "scripts", "mirror.py"))[0], "file")
            st = mirror.load_json(p["state"], {})
            st.pop("setup_from", None)
            st["setup_from_file"] = lone
            mirror.save_state(p, st)
            out = quiet(mirror.main, ["uninstall"])[1]
            self.assertIn(f"  {lone}: the copy of the script setup was run from. It is yours; delete it when you want.", out)
            self.assertNotIn("the folder the skill was downloaded into", out)
            # The record deleted with the copies: what was downloaded is still named, with no place to guess at.
            # The forge is stopped first: the stand-in service manager does not stop it on uninstall's disable --now
            # (systemd does), and a folder removed under a running forge races its writes (seen once in CI).
            stop_forge(w.home)
            self.assertEqual([pr.poll() is None for pr in w.home.procs if pr.poll() is None], [])
            shutil.rmtree(p["data"])
            out = quiet(mirror.main, ["uninstall"])[1]
            self.assertIn("The folder the skill was downloaded into, if it is still there (a copy of its repository, or "
                          "Claude Code's plugin folder): no record of where it is is left on this computer.", out)
            self.assertIn("Kept: nothing; there is no folder of copies and no record of the skill's on this computer "
                          "now.", out)

    def test_G9_7_the_agent_never_deletes_or_offers_to_delete_what_uninstall_kept(self):
        """M62, the family's rule 8 (2026-11-23: a small model offered, unasked, to delete what an uninstall kept):
        the skill's text tells the agent never to delete it, and never to offer to, unless the person asks in their
        own words; even then, the deletion is the person's own command at a terminal of their own."""
        never = " ".join(fread(SKILL).split("## Never", 1)[1].split())
        self.assertIn("Never delete, and never offer to delete, the folder of copies or the record that uninstall "
                      "kept, unless the person asks for it in their own words in this conversation; even then, it is "
                      "their own command (`uninstall --delete-copies`, in a terminal window of their own).", never)

    def test_G9_14_nothing_setup_writes_names_a_plugin_cache_version_folder(self):
        """The family's rule 29 (a) and (e): Claude Code keeps an installed plugin in <its folder>/plugins/cache/
        <catalog>/<plugin>/<version>/, makes a new folder at each update and removes the old one later. Setup run
        from such a folder copies the script to the skill's own place and runs it from there: no unit, timer, job
        file, setting or state value it writes names a path inside the version folder. The plugin is known by
        Claude Code's own list (<its folder>/plugins/installed_plugins.json) and its name alone."""
        cc = tempfile.mkdtemp(prefix="wsm-cc-")
        TEMPS.append(cc)
        ver = os.path.join(cc, "plugins", "cache", "thewindstorm", mirror.PLUGIN, "0.1.0")
        here = os.path.join(ver, "scripts", "mirror.py")
        os.makedirs(os.path.dirname(here))
        shutil.copyfile(SCRIPT, here)
        os.makedirs(os.path.join(ver, "skills", "mirror"))
        shutil.copyfile(os.path.join(ROOT, "skills", "mirror", "SKILL.md"), os.path.join(ver, "skills", "mirror", "SKILL.md"))
        reg = os.path.join(cc, "plugins", "installed_plugins.json")
        self.assertEqual(mirror.plugin_registry_for(here), reg)
        self.assertEqual(mirror.downloaded_from(here), ("folder", ver), "the stand-in is not shaped as the plugin's folder")

        def values(x, at="state"):
            if isinstance(x, dict):
                for k, v in x.items():
                    yield from values(v, f"{at}.{k}")
            elif isinstance(x, list):
                for i, v in enumerate(x):
                    yield from values(v, f"{at}[{i}]")
            else:
                yield at, str(x)
        with Home(linger="yes") as h:
            h.place_gitea()
            p = mirror.paths()
            saved = mirror.__file__
            mirror.__file__ = here
            try:
                rc, out = h.setup()
            finally:
                mirror.__file__ = saved
            self.assertEqual(rc, 0, out)
            self.assertTrue(os.path.isfile(p["script"]) and not p["script"].startswith(ver + os.sep), p["script"])
            st = mirror.load_json(p["state"], {})
            self.assertEqual((st.get("plugin_registry"), st.get("plugin_catalog")), (reg, "thewindstorm"),
                             "the plugin is known by Claude Code's own list and its names")
            self.assertEqual([(k, v) for k, v in values(st) if ver in v], [],
                             "a state value names the plugin's version folder")
            read = []
            for d, _, fs in os.walk(h.tmp):
                for f in fs:
                    path = os.path.join(d, f)
                    if path == p["bin"] or os.path.islink(path):
                        continue
                    with open(path, "rb") as fh:
                        if ver.encode() in fh.read():
                            read.append(path)
            self.assertEqual(read, [], "a file setup wrote names the plugin's version folder")
            for f in [x for x in mirror.service_files(p) if not x.endswith(".timer")]:     # a timer names no program
                with open(f, encoding="utf-8") as fh:
                    self.assertIn(p["script"], fh.read(), f"{f} does not run the skill's own copy")

    def test_G9_14_a_list_that_is_not_on_this_computer_is_could_not_be_read_never_removed(self):
        """M104 (the family's rules 6 and 29 (e)): a record whose list of plugins names a file that is not on this
        computer (a record restored from another computer, say) answers "could not be read", never "names it
        nowhere": status and sync never call the plugin removed from it, and uninstall's line names the list's path
        as the thing that could not be read."""
        gone = os.path.join(tempfile.gettempdir(), "wsm-no-such-" + os.urandom(4).hex(), "plugins", "installed_plugins.json")
        self.assertFalse(os.path.exists(os.path.dirname(os.path.dirname(gone))))
        st = {"plugin_registry": gone, "plugin_catalog": "thewindstorm"}
        self.assertEqual(mirror.plugin_read(st), (None, None))
        self.assertIsNone(mirror.plugin_listed(st))
        self.assertFalse(mirror.plugin_removed(st), "a list that is not there was taken for one that names it nowhere")
        with Home():
            p = mirror.paths()
            lines = mirror.still_here(p, st)
            self.assertIn("Claude Code's folder for this skill as a plugin, and its entry in Claude Code, if Claude Code "
                          f"still has them (its list of plugins, {gone}, could not be read). They are Claude Code's. "
                          "Claude Code's own plugin uninstall takes this plugin off Claude Code's list at once; its "
                          "folder in Claude Code's cache stays, and Claude Code's documentation says it removes that "
                          "folder 14 days later. Nothing runs from them now."
                          + (f" Its command for this plugin:\n      claude plugin uninstall {mirror.PLUGIN}@thewindstorm"
                             if mirror.system()[0] != "darwin" else ""), lines)

    def test_G9_6_after_claude_codes_own_uninstall_the_mirror_says_so_and_nothing_runs_from_a_set_aside_folder(self):
        """M55 (the family's rule 8, 2026-11-23): a person may leave by Claude Code's own `plugin uninstall` and never run
        ours. Claude Code takes the plugin's key out of its list of installed plugins and marks the plugin's folders
        `.orphaned_at` (seen 2026-11-24, Claude Code 2.1.331: no key with an empty list is left). The mirror is the
        thing the person set up, so it goes on, and status and each sync say the plugin was removed, with the one
        command that takes the mirror off; a list that could not be read is never 'removed'.
        Nothing runs from a set-aside folder."""
        cc = tempfile.mkdtemp(prefix="wsm-cc-")
        TEMPS.append(cc)
        ver = os.path.join(cc, "plugins", "cache", "thewindstorm", mirror.PLUGIN, "0.0.0")
        here = os.path.join(ver, "scripts", "mirror.py")
        reg = os.path.join(cc, "plugins", "installed_plugins.json")
        self.assertEqual(mirror.plugin_registry_for(here), reg)
        self.assertIsNone(mirror.plugin_registry_for(os.path.join(ROOT, "scripts", "mirror.py")))
        self.assertIsNone(mirror.plugin_registry_for(os.path.join(cc, "plugins", "cache", "c", "other", "1", "mirror.py")))
        row = {"scope": "user", "installPath": ver, "version": "0.0.0"}
        os.makedirs(os.path.dirname(reg))

        def registry(x):
            with open(reg, "w", encoding="utf-8") as f:
                f.write(x if isinstance(x, str) else json.dumps(x))
        registry({"version": 2, "plugins": {mirror.PLUGIN + "@thewindstorm": [row]}})
        st = {"plugin_registry": reg}
        self.assertFalse(mirror.plugin_removed(st), "read, and it names the plugin")
        for gone in ({"other@x": [row]}, {}):
            registry({"version": 2, "plugins": gone})
            self.assertTrue(mirror.plugin_removed(st), gone)
        # The family's rule 29 (e), as seen 2026-11-24: a key whose list is empty, or a row with no installPath, was
        # never seen; it is "could not be read", never "removed" and never "installed".
        key = mirror.PLUGIN + "@thewindstorm"
        for odd in ({key: []}, {key: [{"scope": "user", "version": "0.0.0"}]}, {key: [row, {"scope": "user"}]},
                    {key: [{"scope": "user", "installPath": ""}]}, {key: row}, {key: ["x"]}):
            registry({"version": 2, "plugins": odd})
            self.assertEqual(mirror.plugin_read(st), (None, None), f"never seen, so could not be read: {odd}")
        for unread in ("", "{not json", json.dumps({"version": 2}), json.dumps({"version": 2, "plugins": []})):
            registry(unread)
            self.assertFalse(mirror.plugin_removed(st), f"could not be read, never removed: {unread!r}")
        os.remove(reg)
        self.assertFalse(mirror.plugin_removed(st), "no list at all is not 'removed'")
        self.assertFalse(mirror.plugin_removed({}), "never set up from Claude Code's plugin")

        os.makedirs(os.path.dirname(here))
        shutil.copyfile(mirror.__file__, here)
        with open(os.path.join(ver, ".orphaned_at"), "w") as f:
            f.write("1763930000000")
        home = os.path.join(cc, "home")
        env = dict(os.environ, HOME=home, WSM_HOME=os.path.join(cc, "wsm"), XDG_CONFIG_HOME=os.path.join(home, ".config"))
        for cmd in (["status"], ["sync"], ["setup"], ["uninstall"]):
            r = subprocess.run([sys.executable, "-I", here, *cmd], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            self.assertIn(f"in a folder Claude Code set aside when its plugin was removed or updated ({ver})", r.stdout)
            self.assertIn("Nothing was done. Nothing of the skill's is set up on this computer; to use it, install "
                          "the plugin again and run setup.", r.stdout)
            self.assertFalse(os.path.exists(env["WSM_HOME"]), "nothing made")
        own = os.path.join(env["WSM_HOME"], "data", "mirror.py")
        os.makedirs(os.path.dirname(own))
        shutil.copyfile(mirror.__file__, own)
        r = subprocess.run([sys.executable, "-I", here, "uninstall"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn(f"The skill set up on this computer is {own}; run it there: python3 -I {own} uninstall",
                      r.stdout)
        self.assertTrue(os.path.exists(own), "nothing removed")
        os.remove(os.path.join(ver, ".orphaned_at"))
        r = subprocess.run([sys.executable, "-I", here, "status"], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotIn("set aside", r.stdout)

        with World(self) as w:
            w.gh.add_repo("someone", "a")
            w.gh.commit("someone/a", {"f": b"1"})
            w.key_set()
            self.assertEqual(w.first_sync()[0], 0)
            st = mirror.load_json(w.p["state"], {})
            st["plugin_registry"] = reg
            mirror.save_state(w.p, st)
            said = "plugin: " + mirror.PLUGIN_GONE.format(command=mirror.own_command(w.p, "uninstall"))
            self.assertIn("To take it off: python3 -I " + w.p["script"] + " uninstall", said)
            registry({"version": 2, "plugins": {mirror.PLUGIN + "@thewindstorm": [row]}})
            rc, out = status()
            self.assertEqual(rc, 0, out)
            self.assertNotIn("plugin:", out)
            registry({"version": 2, "plugins": {mirror.PLUGIN + "@thewindstorm": []}})
            rc, out = status()
            self.assertNotIn("plugin:", out, "a key with an empty list is could not be read, never removed")
            registry({"version": 2, "plugins": {}})
            rc, out = status()
            self.assertEqual(rc, 0, out)
            self.assertIn(said, out.splitlines())
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertEqual(out.splitlines()[-1], said[len("plugin: "):])
            registry("{not json")
            rc, out = status()
            self.assertNotIn("plugin:", out)

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
                self.assertIn("Kept: nothing; there is no folder of copies and no record of the skill's on this "
                              "computer now.", lines)
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
            # M144: the skill's own copy is gone now, so the way names the copy that runs, and the way with none left
            running = shlex.quote(os.path.abspath(mirror.__file__))
            kept = lines.index(f"Kept: the folder of copies, {p['data']} (2 copies), with the record of what the skill "
                               f"did here. It is yours; delete it only when you are sure, with: python3 -I {running} "
                               "uninstall --delete-copies, in a terminal window of your own (it asks you to type the "
                               "number of copies), or, once no copy of this skill is left on this computer, as you "
                               "delete any folder of yours. Setup run again later finds this folder and goes on from it: "
                               "the mirror account gets a new key, and your Gitea account and its password are left as "
                               "they are.")
            self.assertEqual(lines[kept + 1], "Still on this computer, and not uninstall's to take:")
            self.assertTrue(all(x.startswith("  ") for x in lines[kept + 2:]), lines[kept:])
            self.assertIn("Gone now:", lines)
            # Twice: nothing more goes, the copies and the record stay, and it says so last.
            rc, out = quiet(mirror.main, ["uninstall"])
            self.assertEqual(rc, 0, out)
            self.assertIn("Gone now: nothing;", out)
            self.assertTrue(any(x.startswith("Kept: the folder of copies") for x in out.strip().splitlines()))
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

    def test_G9_13_the_way_back_in_uninstall_then_setup_and_the_tool_works(self):
        """M84 (G9.13): after an uninstall that kept the copies, setup again makes the tool work. The kept record still
        names the mirror account, and its key went with uninstall: setup gives it a new key (a new name, since Gitea
        refuses one the account holds), leaves the person's Gitea account and password as they are and says so, and
        status and a sync then work. Seen on a real machine 2026-11-24: setup answered 0, then every command said
        "Run setup first." for ever."""
        with World(self) as w:
            G7.world_with_copies(self, w)
            p = w.p
            rc, out = quiet(mirror.main, ["uninstall"])
            self.assertEqual(rc, 0, out)
            self.assertIn("Setup run again later finds this folder and goes on from it", out)
            self.assertFalse(os.path.exists(p["mirror_key"]))
            self.assertEqual(mirror.load_json(p["state"], {}).get("mirror_user"), mirror.MIRROR_USER)
            rc, out = w.home.setup("--dry-run")
            self.assertIn("a new key of its own", out)
            self.assertIn("as it is, from before: setup does not change it or its password", out)
            w.home.place_gitea()
            rc, out = w.home.setup()
            self.assertEqual(rc, 0, out)
            self.assertTrue(os.path.exists(p["mirror_key"]), out)
            self.assertIn("is there from before: setup did not change it or its password", out)
            self.assertIn("there from before; it has a new key", out)
            self.assertFalse(os.path.exists(p["password"]), "setup wrote a password the person's account does not have")
            self.assertEqual(mirror.set_up_misses(p, mirror.load_json(p["state"], {})), "")
            rc, out = quiet(mirror.main, ["status"])
            self.assertNotIn("Run setup first", out)
            self.assertIn("copies: 2 on GitHub, 2 copied", out)
            w.key_set()
            rc, out = w.sync()
            self.assertEqual(rc, 0, out)
            self.assertNotIn("Run setup first", out)
            code, repos = w.forge_get("/repos/search?limit=50")
            self.assertEqual(code, 200)
            self.assertEqual(len(repos["data"]), 2, "the new key reads the copies kept from before")

    def test_G9_13_from_the_cut_shape_setup_makes_the_key_or_does_not_say_it_is_set_up(self):
        """M84 (G9.13): from the shape a cut uninstall leaves (the settings and keys gone, the record kept), setup makes
        the key; and setup never says it is set up, nor answers 0, unless the commands' own check (the port and the
        key) passes: a key that did not land stops it before anything is started."""
        with Home() as h:
            p = h.place_gitea()
            rc, out = h.setup()
            self.assertEqual(rc, 0, out)
            shutil.rmtree(p["conf"])                    # the cut shape: settings and keys gone, the record kept
            self.assertTrue(mirror.load_json(p["state"], {}).get("mirror_user"))
            rc, out = h.setup()
            self.assertEqual(rc, 0, out)
            self.assertEqual(mirror.set_up_misses(p, mirror.load_json(p["state"], {})), "")
            os.remove(p["mirror_key"])
            saved = mirror.write_private
            mirror.write_private = lambda path, text: None if path == p["mirror_key"] else saved(path, text)
            try:
                n = len(h.calls)
                rc, out = h.setup()
            finally:
                mirror.write_private = saved
            self.assertEqual(rc, 1, out)
            self.assertIn("Setup did not finish: the mirror account's key is not in place", out)
            self.assertNotIn("The forge runs as your own service", out)
            started = [c for c in h.calls[n:] if c[0] in ("systemctl", "launchctl")
                       and {"start", "enable", "daemon-reload", "bootstrap", "load"} & set(c)]
            self.assertFalse(started, "setup installed or started a service after its own check failed")

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
            self.assertIn(mirror.lfs_missing(), said)
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
            self.assertTrue(out.splitlines()[-1].endswith("which shows them this same block and waits for their Enter. "
                                                      "When it says their go is kept, run: sync"), out)
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
                self.assertEqual(mirror.lfs_missing() in said, not lfs, said)
                self.assertTrue(out.splitlines()[-1].endswith("which shows them this same block and waits for their Enter. "
                                                      "When it says their go is kept, run: sync"), out)
        _, _, body = skill_front()
        order = section(body, "The order")
        self.assertIn("what happens after a restart", order)

    def test_G10_9_one_version_in_every_place(self):
        """The family's rule 10: one version, in every place it is written: the manifest, the script, the
        changelog's first heading ('Unreleased' only while nothing is released), this repository's own catalog
        entry, and the README's own status line and every clone line (rule 15, at the tag; G10.14)."""
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
        self.assertIsNone(readme_release_wrong(fread(os.path.join(ROOT, "README.md")), ver), "the README")

    def test_G10_14_the_tagged_readme_states_the_release_and_clones_the_tag(self):
        """M67 (the family's rule 15, at the tag, 2026-11-23): the README that is tagged states the release and its
        clone lines name the tag, never the branch, so a person cloning from it gets what was tagged. Seen to refuse
        each way it can be wrong."""
        good = "Status: 0.1.0 (2026-11-25)\n\n```bash\ngit clone --branch v0.1.0 https://github.com/TheWindstorm/x\n```\n"
        self.assertIsNone(readme_release_wrong(good, "0.1.0"))
        self.assertIsNone(readme_release_wrong("Nothing here can be installed yet.\ngit clone https://e/x\n", "0.0.0"))
        wrong = {"no status": (good.replace("Status: 0.1.0 (2026-11-25)", "Released."), "0.1.0"),
                 "an older status": (good.replace("Status: 0.1.0", "Status: 0.0.9"), "0.1.0"),
                 "two status lines": ("Status: 0.1.0\n" + good, "0.1.0"),
                 "a status before any release": (good, "0.0.0"),
                 "a clone of main": (good.replace("--branch v0.1.0 ", ""), "0.1.0"),
                 "a clone of the release branch": (good.replace("v0.1.0", "release-0.1.0"), "0.1.0"),
                 "a clone of another tag": (good.replace("--branch v0.1.0", "--branch v0.1.01"), "0.1.0"),
                 "a second clone line of main": (good + "git clone https://github.com/TheWindstorm/x\n", "0.1.0"),
                 "no clone line": ("Status: 0.1.0\n", "0.1.0"),
                 "a link to the release branch": (good + "See https://github.com/TheWindstorm/x/tree/release-0.1.0\n",
                                                  "0.1.0")}
        for why, (readme, ver) in wrong.items():
            self.assertIsNotNone(readme_release_wrong(readme, ver), why)
        self.assertIsNone(readme_release_wrong(fread(os.path.join(ROOT, "README.md")), version()), "this README")

    def test_G10_10_public_text_names_roles_never_our_own_labels(self):
        """The family's rule 18: public text names kinds and roles ('another program sharing the login', 'a sister
        skill'), never our own labels for how we work. The words are built here so this file does not hold them."""
        ours = [re.compile(r"\b" + w + r"\b", re.I) for w in ("la" + "nes?", "fl" + "eet", "test-" + "now", "board-" + "say",
                                                           "bo" + "ss", "(?:his|her) (?:words?|rulings?)", "ve" + "ron",
                                                           "her" + "dr", "(?:windy|kit) ?0")]
        ours += [re.compile(r"\b" + w + r"\b") for w in ("Gr" + "ant", "Ru" + "ss", "Al" + "ex", "Bi" + "ll")]   # names
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

    def test_G10_12_a_ruling_says_whose_word_it_was_by_role(self):
        """M56 (the family's rule 18 and principle 9, 2026-11-23): a ruling says whose word it really was, by role.
        The owner is the person whose project this is; the maintainer is who rules day to day. An exception begins
        with one of the two and nothing else lets a version be tagged before its row is proven; the promise file says
        who may move each."""
        rows = jload(os.path.join(ROOT, "PROMISES.json"))
        made_up = {"id": "X1", "ship_in": "0.1.0", "stand_in": "GitHub", "proven": ""}
        for named in ("By the maintainer's word, 2026-11-23: why, and when.", "By the owner's word, 2026-11-23: why."):
            self.assertEqual(stand_ins_unproven([dict(made_up, exception=named)], v("0.1.0")), [], named)
        for not_named in ("By the owner's word", "By h" + "is word, why.", "By a la" + "ne's word, why.", "why, and when.", " "):
            self.assertEqual(stand_ins_unproven([dict(made_up, exception=not_named)], v("0.1.0")), ["X1"], not_named)
        for r in rows["rows"]:
            for k in ("exception", "moved_why"):
                if r.get(k) and r[k].startswith("By "):
                    self.assertTrue(exception_named(r[k]), f"{r['id']} {k}: say whose word it was, by role")
        for k in ("M13", "M42"):
            r = next(x for x in rows["rows"] if x["id"] == k)
            self.assertTrue((r["moved_why"] or r["exception"]).startswith("By the maintainer's word, "), k)
        rule = next(x for x in rows["how_to_use"] if x.startswith("'exception'"))
        for w in WHOSE_WORD:
            self.assertIn(f'"{w}"', rule)
        self.assertIn("an owner's exception is moved only by the owner, a maintainer's by the maintainer or the owner", rule)

    def test_G10_13_our_own_catalog_entry_is_pinned_once_a_version_is_released(self):
        """M57 (the family's rule 16, corrected 2026-11-23): this repository's own catalog file stays, and its entry
        never hands out unreleased code under a released label. `./` stands only while no version older than this one
        is released; then an https url with ref v<a released version no newer than this one> and a full sha. The one
        commit between a tag and its pin is the release step's to hold: the pin needs the tag's sha. The sha is the
        commit the ref points to (`git rev-parse <ref>^{commit}`), checked against the tags in this clone."""
        url = lambda ref, sha="a" * 40, u="https://github.com/TheWindstorm/windstorm-mirror-for-github.git": \
            {"source": {"source": "url", "url": u, "ref": ref, "sha": sha}}
        ok = [({"source": "./"}, "0.0.0", ["Unreleased"]),
              ({"source": "./"}, "0.1.0", ["0.1.0 (2026-11-24)"]),
              (url("v0.1.0"), "0.1.0", ["0.1.0 (2026-11-24)"]),
              (url("v0.1.0"), "0.1.1", ["0.1.1 (2026-12-01)", "0.1.0 (2026-11-24)"])]
        wrong = [({"source": "./"}, "0.1.1", ["0.1.1 (2026-12-01)", "0.1.0 (2026-11-24)"]),
                 (url("v0.1.1"), "0.1.0", ["0.1.0 (2026-11-24)"]),
                 (url("v0.1.1"), "0.1.0", ["0.1.1 (2026-12-01)", "0.1.0 (2026-11-24)"]),
                 (url("v0.0.9"), "0.1.0", ["0.1.0 (2026-11-24)"]),
                 (url("0.1.0"), "0.1.0", ["0.1.0 (2026-11-24)"]),
                 (url("v0.1.0", sha="abc123"), "0.1.0", ["0.1.0 (2026-11-24)"]),
                 (url("v0.1.0", u="http://github.com/TheWindstorm/windstorm-mirror-for-github.git"), "0.1.0", ["0.1.0"]),
                 ({"source": "https://github.com/TheWindstorm/windstorm-mirror-for-github.git"}, "0.1.0", ["0.1.0"])]
        for e, ver, heads in ok:
            self.assertIsNone(catalog_entry_wrong(e, ver, heads), (e, ver))
        for e, ver, heads in wrong:
            self.assertIsNotNone(catalog_entry_wrong(e, ver, heads), (e, ver))
        entry = [p for p in jload(os.path.join(ROOT, ".claude-plugin", "marketplace.json"))["plugins"]
                 if p["name"] == "windstorm-mirror-for-github"][0]
        heads = re.findall(r"^## (.+)$", fread(os.path.join(ROOT, "CHANGELOG.md")), re.M)
        self.assertIsNone(catalog_entry_wrong(entry, version(), heads, git_commit_of(ROOT)))

    def test_G10_13_the_pinned_sha_is_the_commit_its_ref_points_to(self):
        """M57, rule 16 (seen 2026-11-23 on Claude Code 2.1.323 and 2.1.330): an annotated tag's own id as the sha is
        refused, and a ref that disagrees with its sha is installed by the sha without a word. In a real clone with
        an annotated tag: the tag's commit passes; the tag's own id, another commit, and a tag not in the clone are
        each refused."""
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
                       GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
            g = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True, text=True,
                                          env=env).stdout.strip()
            g("init", "-q"); g("commit", "-q", "--allow-empty", "-m", "one"); first = g("rev-parse", "HEAD")
            g("commit", "-q", "--allow-empty", "-m", "two"); g("tag", "-a", "v0.1.0", "-m", "0.1.0")
            commit, tag_id = g("rev-parse", "v0.1.0^{commit}"), g("rev-parse", "v0.1.0")
            self.assertNotEqual(commit, tag_id)
            heads = ["0.1.0 (2026-11-24)"]
            pin = lambda sha, ref="v0.1.0": {"source": {"source": "url", "ref": ref, "sha": sha,
                                                        "url": "https://github.com/TheWindstorm/windstorm-mirror-for-github.git"}}
            self.assertIsNone(catalog_entry_wrong(pin(commit), "0.1.0", heads, git_commit_of(d)))
            for sha in (tag_id, first):
                why = catalog_entry_wrong(pin(sha), "0.1.0", heads, git_commit_of(d))
                self.assertIsNotNone(why, sha); self.assertIn(commit, why)
            g("tag", "-d", "v0.1.0")
            self.assertIn("not in this clone", catalog_entry_wrong(pin(commit), "0.1.0", heads, git_commit_of(d)) or "")

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
            self.assertEqual("\n".join(x for x in out.strip().splitlines() if not x.startswith("WSM_HOME is set:")),
                             line)

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
        # G8.7: a DoorShut's words, and setup's floor, are door_line's: checked where they are built, below
        door_names = {h.name for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler) and h.name
                      and getattr(h.type, "id", None) == "DoorShut"} | {"floor", "shut"}
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
                    if any(isinstance(x, ast.Name) and x.id in door_names for x in ast.walk(arg)):
                        mine.add("door_line")
                    elif isinstance(arg, ast.Name) and not arg.id.isupper():
                        mine.add(f"<{arg.id}>")
                    if words(arg).strip() == "STOPPED:":
                        mine.add("HardStop")                  # says a HardStop's words: every raise is checked
                    built |= mine
                    if mine & {"git_missing_sentence", "check_key", "LINGER_CHOICE", "<far>", "HardStop", "<other>",
                               "door_line", "<bus>"}:
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
        for kind, never in (("other", False), ("unread", True), ("unread", False)):    # G8.7, each of its three
            line = mirror.door_line(kind, 21000, "why", never)
            self.assertTrue(re.search(r"\. (Run setup|Run status again|The mirror needs that read to run; the README)",
                                      line), line)
        for fn in ("other_install_stop", "home_is_not_ours", "bus_floor",  # G9.12, G1.24: each stop it returns
                   "kept_step"):                                           # the three answers (M151, M154-M159)
            for r in ast.walk(funcs[fn]):
                if isinstance(r, ast.Return) and r.value is not None and words(r.value):
                    self.assertTrue(self.NEXT.search(words(r.value)), (fn, words(r.value)[:90]))
        self.assertIn("kept_step", {x.func.id for x in ast.walk(funcs["kept_stop_words"])     # its step: kept_step's
                                    if isinstance(x, ast.Call) and isinstance(x.func, ast.Name)})
        for r in ast.walk(funcs["check_key"]):
            if isinstance(r, ast.Return) and isinstance(r.value, ast.Tuple) and \
                    isinstance(r.value.elts[0], ast.Constant) and r.value.elts[0].value is None:
                self.assertTrue(self.NEXT.search(words(r.value)), words(r.value))
        data = {"SKILL_REPO", "GITEA_VERSION", "last_line", "lines_of", "shown_lines",  # facts in a line (git's words)
                "lock_holder", "UNINSTALL_WAIT", "VERSION",                     # (which process; how long uninstall waits)
                "own_command",                                                   # (the command to run, as it runs)
                "SHOWN_CHANGED",                                                 # (what a ? stands for, M132)
                "paths", "MIRROR_USER"}                                          # (a folder; the forge account's name)
        self.assertEqual(built - data - {"LINGER_CHOICE", "git_missing_sentence", "check_key", "<far>", "HardStop",
                                         "<other>", "door_line", "<bus>", "kept_step", "kept_stop_words"},
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
                   "https://git-scm.com/downloads",           # named to the person (M51), never reached
                   "https://github.com/settings/personal-access-tokens",  # named to the person (M38), never reached
                   "http://127.0.0.1:9"}                  # the dead proxy that shuts every other host off (G7.2)
        found = set(re.findall(r"https?://[A-Za-z0-9.:_/-]*[A-Za-z0-9]", fread(SCRIPT)))
        self.assertEqual(found - allowed, set(), "an address outside the allowed ones")
        # Four: GitHub's API, its releases, Gitea's download site, and the one opener for the skill's own forge (G8.6).
        self.assertEqual(len(re.findall(r"urlopen\(|build_opener\(", fread(SCRIPT))), 4,
                         "a new way to reach the network: name it here and in the plan")
        # What it runs: git, the forge's binary, the system's service manager, the system's own address list,
        # and itself (to check a self-update before it is swapped in).
        progs = set()
        for n in ast.walk(tree_):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "run" and n.args and isinstance(n.args[0], ast.List):
                first = n.args[0].elts[0] if n.args[0].elts else None
                progs.add(first.value if isinstance(first, ast.Constant) else ast.unparse(first))
        self.assertEqual(progs - {"git", "systemctl", "busctl", "loginctl", "launchctl", "ip", "ifconfig", "sys.executable",
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
            and not r.get("proven") and not exception_named(r.get("exception"))]


WHOSE_WORD = ("By the maintainer's word, ", "By the owner's word, ")


def git_commit_of(root):
    """The commit a ref points to in the clone at `root` (an annotated tag peeled to its commit), or None."""
    def commit_of(ref):
        r = subprocess.run(["git", "-C", root, "rev-parse", "--verify", "-q", ref + "^{commit}"],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    return commit_of


def rows_not_here(texts, ids):
    """Each row number (M and digits, standing alone) that a text names and `ids` does not hold: [(number, where)]."""
    out = []
    for name in sorted(texts):
        for m in re.finditer(r"(?<![A-Za-z0-9_])M(\d+)(?![A-Za-z0-9_])", texts[name]):
            if "M" + m.group(1) not in ids:
                out.append(("M" + m.group(1), f"{name}:{texts[name].count(chr(10), 0, m.start()) + 1}"))
    return out


def to_build_wrong(plan, rows, ver):
    """Each proof the plan marks *(to build)* that may not stand at version `ver` (the maintainer's ruling,
    2026-11-23): before the first release any may; after it, only one whose codon has a row due in a later version
    than `ver`, a row not marked built (no test named). Returns [(test, why)]."""
    wrong = []
    for line in plan.splitlines():
        m = re.match(r"\| (G\d+\.\d+|T\d+) \|", line)            # a T codon (the suite's own) as a G codon
        for t in re.findall(r"`(test_[A-Za-z0-9_]+)` \*\(to build\)\*", line):
            if ver == "0.0.0":
                continue
            if not m:
                wrong.append((t, "not on a codon's line")); continue
            mine = [r for r in rows if m.group(1) in [c.strip() for c in r.get("codon", "").split(",")]]
            if not mine:
                wrong.append((t, f"{m.group(1)} has no row")); continue
            for r in mine:
                if r.get("test"):
                    wrong.append((t, f"{r['id']} is marked built"))
                elif v(r.get("ship_in") or "0.0.0") <= v(ver):
                    wrong.append((t, f"{r['id']} is due in {r.get('ship_in')}, not after {ver}"))
    return wrong


def readme_release_wrong(readme, ver):
    """Why the README does not match version `ver`, or None (the family's rule 15, at the tag; G10.14, M67). Once a
    version is released the README states it on one line of its own, 'Status: X.Y.Z', and every `git clone` line
    names that version's tag with --branch, never a release branch; before the first release it claims no status."""
    status = re.findall(r"^Status: (\S+)", readme, re.M)
    clones = [l.strip() for l in readme.splitlines() if "git clone" in l and not l.lstrip().startswith(("#", ">"))]
    if ver == "0.0.0":
        return f"a status line before any release: {status}" if status else None
    if status != [f"{ver}"] and not (len(status) == 1 and re.fullmatch(re.escape(ver) + r",?", status[0])):
        return f"the status line says {status or 'nothing'}, not {ver}"
    if not clones:
        return "no clone line"
    for l in clones:
        if f"--branch v{ver} " not in l + " " or re.search(r"release-\d", l):
            return f"a clone line that does not name the tag v{ver}: {l}"
    if re.search(r"/tree/release-\d|--branch release-\d", readme):
        return "the README names a release branch"
    return None


def catalog_entry_wrong(entry, ver, heads, commit_of=None):
    """M57 (the family's rule 16): why this repository's own catalog entry is wrong at version `ver`, given the
    changelog's headings, or None. `./` hands out whatever main holds, so it stands only while no version older
    than this one is released (0.0.0, and the release commit itself, before the tag exists to pin to). Otherwise
    the entry is an https url pinned to a released version no newer than this one: ref v<that version>, and a sha
    that is the commit that ref points to (`commit_of`): Claude Code refuses an annotated tag's own id, and installs
    by the sha, without a word, when the ref says something else."""
    released = [h.split(" ")[0] for h in heads if re.match(r"^\d+\.\d+\.\d+(?: \(\d{4}-\d{2}-\d{2}\))?$", h)]
    src = entry.get("source")
    if src == "./":
        older = [x for x in released if v(x) < v(ver)]
        return f"'./' after {older[0]} was released: pin the entry to a release" if older else None
    if not isinstance(src, dict) or src.get("source") != "url" or not str(src.get("url", "")).startswith("https://"):
        return "the entry is neither './' nor an https url source"
    ref, sha = str(src.get("ref", "")), str(src.get("sha", ""))
    if not (ref.startswith("v") and ref[1:] in released and v(ref[1:]) <= v(ver)):
        return f"ref {ref!r} is not a released version no newer than {ver}"
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return "the sha is not a full commit id"
    if commit_of is not None:
        c = commit_of(ref)
        if c is None:
            return f"the tag {ref} is not in this clone, so the sha cannot be checked against it: git fetch --tags"
        if c != sha:
            return (f"the sha is not the commit {ref} points to ({c}): an annotated tag's own id is refused, and a "
                    "sha that disagrees with its ref is installed by the sha without a word")
    return None


def exception_named(text):
    """M56: an exception counts only when it says whose word it was, by role, and something after it."""
    return any((text or "").startswith(w) and len(text) > len(w) for w in WHOSE_WORD)

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
        # A named exception, by the maintainer's word or the owner's (M56), lets the tag go before the real sighting.
        self.assertEqual(stand_ins_unproven([dict(made_up, exception="By the maintainer's word, ...")], v("0.1.0")), [])
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
    def test_T4_mac_jobs_are_shared_so_a_pull_request_runs_one(self):
        """M54 (the family's rule 17, 2026-11-23): GitHub gives the account a few Mac runners at a time for all five
        repositories together; one afternoon had 34 runs queued and none running. A pull request runs ONE Mac job
        (the floor's Python); the full Mac matrix runs on main and release-*; the gate expects the reports of the
        event it runs on; a newer push to a pull request cancels its older runs, never a run on main or a release
        branch."""
        wf = fread(os.path.join(ROOT, ".github", "workflows", "tests.yml"))
        pr = "github.event_name == 'pull_request'"
        jobs = re.split(r"^  (?=[a-z-]+:\n)", wf.split("\njobs:\n", 1)[1], flags=re.M)
        job = {j.split(":", 1)[0]: j for j in jobs if j.strip()}
        macs = lambda j: re.findall(r'\{os: (macos-[0-9]+), python: "([0-9.]+)"\}', job[j])
        self.assertEqual(macs("test"), [("macos-26", "3.9")], "a pull request runs one Mac job, the floor's Python")
        self.assertNotIn("if:", job["test"].split("steps:")[0])
        self.assertEqual(macs("test-mac"), [("macos-26", "3.13")])
        self.assertIn("    if: github.event_name != 'pull_request'\n", job["test-mac"])
        self.assertEqual(job["test"].split("steps:", 1)[1], job["test-mac"].split("steps:", 1)[1], "the same steps")
        self.assertIn("needs: [test, test-mac]", job["gate"])
        self.assertIn(f"--expect ${{{{ {pr} && 4 || 5 }}}}", wf)
        self.assertIn(f"group: ${{{{ {pr} && format('pr-{{0}}', github.event.pull_request.number) || "
                      "format('run-{0}', github.run_id) }}", wf)
        self.assertIn(f"cancel-in-progress: ${{{{ {pr} }}}}", wf)

    def test_T5_a_proof_to_build_stands_after_a_release_only_for_a_row_due_later(self):
        """The maintainer's ruling, 2026-11-23 (rules 1 and 4): once a version is released, the plan may still mark
        a proof *(to build)*, but only where its codon's row is due in a later version and not yet built; so nothing
        planned for this version stays unbuilt. Seen to refuse: no row, a row due now or earlier, a row marked built;
        and a mark not on a codon's line."""
        plan = "| G2.1 | the skill lists the keys | `test_G2_1_x` *(to build)* |\n"
        row = {"id": "X1", "codon": "G2.1", "ship_in": "0.1.1", "test": ""}
        self.assertEqual(to_build_wrong(plan, [row], "0.1.0"), [])
        self.assertEqual(to_build_wrong(plan, [], "0.0.0"), [])
        self.assertEqual(to_build_wrong(plan, [dict(row, codon="G1.1, G2.1")], "0.1.0"), [])
        wrong = {"no row": ([], "0.1.0"),
                 "a row of another codon": ([dict(row, codon="G2.10")], "0.1.0"),
                 "a row due now": ([dict(row, ship_in="0.1.0")], "0.1.0"),
                 "a row due earlier": ([dict(row, ship_in="0.1.0")], "0.1.1"),
                 "a row marked built": ([dict(row, test="test_G2_1_x")], "0.1.0"),
                 "one of two rows due now": ([row, dict(row, id="X2", ship_in="0.1.0")], "0.1.0")}
        for why, (rows, ver) in wrong.items():
            self.assertNotEqual(to_build_wrong(plan, rows, ver), [], why)
        self.assertNotEqual(to_build_wrong("text `test_G2_1_x` *(to build)*\n", [row], "0.1.0"), [], "off a codon's line")

    def test_T6_the_suite_reads_nothing_of_the_home_of_the_account_that_runs_it(self):
        """M72, rule 17 (seen 2026-11-24 in a run with the home at a trap: two tests ran git outside Home and read
        the git config of the account running the suite, which can name hooks, signing or address rewrites). The
        test process runs in a home of its own, with git's global config at an empty file and its system config off;
        where git knows GIT_CONFIG_GLOBAL (2.32 and newer), a git call that drops it reads the home's trap and fails,
        and so would the two calls the trap run found."""
        import pwd
        own = pwd.getpwuid(os.getuid()).pw_dir
        self.assertEqual(os.environ["HOME"], SUITE_HOME)
        self.assertNotEqual(os.path.realpath(SUITE_HOME), os.path.realpath(own))
        self.assertEqual((os.environ["GIT_CONFIG_GLOBAL"], os.environ["GIT_CONFIG_NOSYSTEM"]), (SUITE_GITCONFIG, "1"))
        self.assertEqual(os.environ["XDG_CONFIG_HOME"], os.path.join(SUITE_HOME, ".config"))
        self.assertEqual(fread(SUITE_GITCONFIG), "")
        self.assertEqual([k for k in SESSION_AND_MOVED if k in os.environ], [])
        self.assertTrue(mirror.paths()["data"].startswith(SUITE_HOME + os.sep), mirror.paths()["data"])
        read = subprocess.run(["git", "config", "--global", "--list"], capture_output=True, text=True)
        self.assertEqual((read.stdout, read.stderr), ("", ""), "git reads a global config that is not the empty one")
        d = tempfile.mkdtemp(prefix="wsm-t6-")
        self.addCleanup(shutil.rmtree, d, True)
        subprocess.run(["git", "-C", d, "init", "-q"], check=True)
        subprocess.run(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
                        "--allow-empty", "-m", "one"], check=True)
        self.assertIsNotNone(git_commit_of(d)("HEAD"))
        if not GIT_TRAP_SET:
            return        # git older than 2.32 reads the home's config, and the home is the suite's own and empty
        saved = os.environ.pop("GIT_CONFIG_GLOBAL")
        try:
            dropped = subprocess.run(["git", "config", "--global", "--list"], capture_output=True, text=True)
            self.assertNotEqual(dropped.returncode, 0, "a git call that drops GIT_CONFIG_GLOBAL does not fail")
            self.assertIn("bad config", dropped.stderr)
            self.assertIsNone(git_commit_of(d)("HEAD"), "git_commit_of read the home's config and went on")
            self.assertFalse(mirror.has_git_lfs(), "has_git_lfs read the home's config and went on")
        finally:
            os.environ["GIT_CONFIG_GLOBAL"] = saved

    def test_every_proof_named_in_the_plan_is_a_real_test_and_every_test_has_a_codon(self):
        plan = fread(os.path.join(ROOT, "DNA-STRAND-MASTER-PLAN.md"))
        named = set(re.findall(r"`(test_[A-Za-z0-9_]+)`", plan))
        to_build = set(re.findall(r"`(test_[A-Za-z0-9_]+)` \*\(to build\)\*", plan))
        real = set(collected_tests())
        codon_tests = {n for n in real if re.match(r"test_(?:G|INV|T\d)", n)}
        self.assertEqual(to_build & real, set(), "a test marked 'to build' exists: remove the mark")
        self.assertEqual(to_build_wrong(plan, jload(os.path.join(ROOT, "PROMISES.json"))["rows"], version()), [],
                         "a proof marked 'to build' after the first release needs its codon's row due later")
        self.assertEqual(named - real - to_build, set(), "the plan names tests that do not exist")
        self.assertEqual(codon_tests - named, set(), "tests with no codon in the plan")

    def test_T8_every_row_number_a_text_names_is_a_row(self):
        """The family's rule 34 (2026-11-25, M130): every row number this tree's README, plan, rows, scripts or tests
        name is a row in this same tree's PROMISES.json (seen: M124 and M127 named here, and their rows only on the
        rows branch). Seen red first on a planted number, here, every run."""
        ids = {r["id"] for r in jload(os.path.join(ROOT, "PROMISES.json"))["rows"]}
        planted = "M" + str(10 ** 6)                          # built here, so this file names no such row
        self.assertEqual(rows_not_here({"planted.md": f"as row {planted} says; rows M93-M98"}, {"M93", "M98"}),
                         [(planted, "planted.md:1")], "the reader misses a planted number")
        texts = {}
        for top, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for f in files:
                path = os.path.join(top, f)
                try:
                    with open(path, encoding="utf-8") as fh:
                        texts[os.path.relpath(path, ROOT)] = fh.read()
                except (UnicodeDecodeError, OSError):
                    continue                                  # not a text (an image, a binary)
        self.assertIn("README.md", texts)
        self.assertIn(os.path.join("scripts", "mirror.py"), texts)
        self.assertEqual(rows_not_here(texts, ids), [], "a text names a row this tree's PROMISES.json does not hold")

    def test_T9_the_rule_3_gate_is_a_job_of_its_own_and_hides_nothing_else(self):
        """M131 (a reviewer's read of CI, 2026-11-25): with the rule 3 gate red by design in every leg, a second red
        in a leg (G9.8, once) stood behind it. The legs run the whole suite but that one test, and a job of its own
        runs it; the flag sets apart that test only, and takes nothing else."""
        me = sys.modules[__name__]
        ids = lambda suite: {t.id().rsplit(".", 1)[-1] for t in iter_tests(suite)}
        whole = ids(unittest.defaultTestLoader.loadTestsFromModule(me))
        apart = ids(without(unittest.defaultTestLoader.loadTestsFromModule(me), RULE3_GATE))
        self.assertIn(RULE3_GATE, whole)
        self.assertEqual(whole - apart, {RULE3_GATE}, "the flag sets apart another test, or none")
        self.assertIn(RULE3_GATE, plan_proofs() | row_tests())
        # The flag takes nothing else: no name, no --gate-only beside it.
        for extra in (["G1"], ["--gate-only", "test_T3_a_commits_checks_run_once"]):
            r = subprocess.run([sys.executable, "-I", os.path.abspath(__file__), APART_FLAG, *extra],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120,
                               env={k: v for k, v in os.environ.items() if k != "WSM_TEST_REPORT"})
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr[-800:])
        # The join leaves it out only when every report says it was set apart.
        with tempfile.TemporaryDirectory(prefix="wsm-join-") as d:
            proofs = sorted((plan_proofs() | row_tests()) - {RULE3_GATE})
            def report(n, apart_):
                path = os.path.join(d, f"r{n}.json")
                with open(path, "w") as f:
                    json.dump({"passed": proofs, "refused": [], "apart": apart_}, f)
                return path
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(join_reports([report(1, [RULE3_GATE]), report(2, [RULE3_GATE])], 2), 0)
                self.assertEqual(join_reports([report(3, [RULE3_GATE]), report(4, [])], 2), 1)
            self.assertIn(f"GATE: {RULE3_GATE} passed on none", out.getvalue())
        # The workflow: every leg sets it apart, one job of its own runs it, the join waits on the legs.
        wf = fread(os.path.join(ROOT, ".github", "workflows", "tests.yml"))
        jobs = re.split(r"^  (?=[a-z0-9-]+:\n)", wf.split("\njobs:\n", 1)[1], flags=re.M)
        job = {j.split(":", 1)[0]: j for j in jobs if j.strip()}
        for leg in ("test", "test-mac"):
            self.assertIn(f"run: python -I tests/test_mirror.py -v {APART_FLAG}\n", job[leg])
        self.assertIn(f"run: python -I tests/test_mirror.py -v --gate-only {RULE3_GATE}\n", job["rule-3-gate"])
        self.assertNotIn("needs:", job["rule-3-gate"])
        self.assertEqual(wf.count(APART_FLAG), 2)

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


# T9 (M131): the family's rule 3 gate runs in a job of its own, so the legs' colour says the suite and that job's
# colour says the proofs on real machines. Only this one test can be set apart, and only with this flag.
RULE3_GATE = "test_PROMISES_a_row_with_a_stand_in_is_proven_before_its_tag"
APART_FLAG = "--without-rule3-gate"


def join_reports(paths, expect):
    reports = [jload(p) for p in paths]
    proofs = plan_proofs() | row_tests()
    if reports and all(r.get("apart") == [RULE3_GATE] for r in reports):
        proofs -= {RULE3_GATE}                        # judged in its own job, never by the legs
    passed = set().union(*(set(r["passed"]) for r in reports)) if reports else set()
    refused = [x for r in reports for x in r["refused"]]
    if len(reports) != expect:
        refused.append(f"{len(reports)} job reports, {expect} expected")
    refused += [f"{n} passed on none of the {len(reports)} jobs" for n in sorted(proofs - passed)]
    for x in refused:
        print("GATE:", x)
    print(f"{len(proofs & passed)} of {len(proofs)} proofs passed on at least one of {len(reports)} jobs")
    return 1 if refused else 0


def iter_tests(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from iter_tests(t)
        else:
            yield t


def without(suite, name):
    """The suite with every test of that one name taken out (none when name is None)."""
    out = unittest.TestSuite()
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            out.addTest(without(t, name))
        elif name is None or t.id().rsplit(".", 1)[-1] != name:
            out.addTest(t)
    return out


def main(argv):
    verbosity = 2 if "-v" in argv else 1
    args = [a for a in argv if a != "-v"]
    if args[:1] == ["--join"]:
        expect = int(args[args.index("--expect") + 1]) if "--expect" in args else 0
        files = [a for i, a in enumerate(args[1:], 1) if a != "--expect" and args[i - 1] != "--expect"]
        return join_reports(files, expect)
    me = sys.modules[__name__]
    apart = APART_FLAG in args
    if apart and args != [APART_FLAG]:
        print(f"{APART_FLAG} runs the whole suite but one test, and takes nothing else.", file=sys.stderr)
        return 2
    args = [] if apart else args
    gate_only = args[1] if args[:1] == ["--gate-only"] and len(args) > 1 else None
    tests = collected_tests()
    if gate_only:
        suite = unittest.TestSuite([tests[gate_only](gate_only)] if gate_only in tests else [])
    elif args:
        suite = unittest.defaultTestLoader.loadTestsFromNames(args, me)
    else:
        suite = without(unittest.defaultTestLoader.loadTestsFromModule(me), RULE3_GATE if apart else None)
    result = unittest.TextTestRunner(verbosity=verbosity, resultclass=Recorder, stream=sys.stderr).run(suite)
    if args and not gate_only:
        print("A part of the suite was run, so the gate was not applied.", file=sys.stderr)
        return 0 if result.wasSuccessful() else 1
    problems = shape_problems(os.path.abspath(__file__))
    refused, not_here = gate(result.outcome, {gate_only} if gate_only else
                             (plan_proofs() | row_tests()) - ({RULE3_GATE} if apart else set()))
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
                       "not_run_here": not_here, "refused": refused, "apart": [RULE3_GATE] if apart else []},
                      f, indent=1)
    return 0 if result.wasSuccessful() and not refused else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
