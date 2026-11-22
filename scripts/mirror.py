#!/usr/bin/env python3
"""Windstorm Mirror for GitHub: a second, always-current copy of all your GitHub projects on a computer you own.

One script, Python standard library only. It installs stock Gitea at a pinned version as the person's own
service, reachable from this computer only, and (in later genes) keeps a copy of every repository in it.
The plan this is built from: DNA-STRAND-MASTER-PLAN.md. Run with `python3 -I mirror.py <command>`.
"""
import argparse
import base64
import fnmatch
import getpass
import urllib.parse
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request

VERSION = "0.0.0"
USER_AGENT = f"windstorm-mirror-for-github/{VERSION} (+https://github.com/TheWindstorm/windstorm-mirror-for-github)"

# G1.1: the forge is stock Gitea at one pinned version. The checksums were read on 2026-11-22 from Gitea's own
# download site and from its GitHub release, and the two agreed. A new pin comes only in a new release of this
# skill (plan: "How the pin moves").
GITEA_VERSION = "28.4.0"
GITEA_BASE = "https://dl.gitea.com/gitea"
PINS = {
    ("linux", "x86_64"): ("linux-amd64", "154e775bd6bb589fc14cd93a689d8fbc64090aaa3ec537f0e3af298b13c6d386"),
    ("darwin", "x86_64"): ("darwin-amd64", "c9538783c9df22d3527ba53ce2a14646fe9cd9dfa579829c51a9cf147c1f03fd"),
    ("darwin", "arm64"): ("darwin-arm64", "6c098261255badcfd2ae4c532ae5e388b475d80af728788474bf8270335a6d32"),
}

SERVICE = "windstorm-mirror-forge"
LAUNCHD_LABEL = "com.thewindstorm.mirror.forge"
MIRROR_USER = "mirror"
MIRROR_SCOPES = "write:repository,write:user"      # contract C6: both are needed to create a repository
NO_LOGIN_NOTE = "after a restart, the copy stands still until you log in"


# ---------------------------------------------------------------- where things live

def system():
    return platform.system().lower(), platform.machine().lower().replace("amd64", "x86_64").replace("aarch64", "arm64")


def paths():
    """Everything the skill writes, in two folders of the person's own. WSM_HOME moves both (tests)."""
    home = os.path.expanduser("~")
    root = os.environ.get("WSM_HOME")
    if root:
        data, conf = os.path.join(root, "data"), os.path.join(root, "config")
    elif system()[0] == "darwin":
        data = conf = os.path.join(home, "Library", "Application Support", "windstorm-mirror")
        conf = os.path.join(conf, "config")
    else:
        data = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share"), "windstorm-mirror")
        conf = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "windstorm-mirror")
    forge = os.path.join(data, "forge")
    return {
        "data": data, "conf": conf, "forge": forge,
        "bin": os.path.join(data, "bin", f"gitea-{GITEA_VERSION}"),
        "app_ini": os.path.join(forge, "custom", "conf", "app.ini"),
        "script": os.path.join(data, "mirror.py"),
        "state": os.path.join(data, "state.json"),
        "password": os.path.join(conf, "forge-password"),
        "mirror_key": os.path.join(conf, "forge-mirror-key"),
        "keys": os.path.join(conf, "keys"),
        "cache": os.path.join(data, "cache"),
        "settings": os.path.join(conf, "settings.json"),
        "unit": os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
                             "systemd", "user", SERVICE + ".service"),
        "plist": os.path.join(home, "Library", "LaunchAgents", LAUNCHD_LABEL + ".plist"),
    }


def say(msg=""):
    print(msg, flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_private(path, text):
    """A file only the person can read: made 0600 before anything is written into it."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def write_file(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def run(cmd, **kw):
    """Every outside command goes through here, so tests can stand in for the ones a CI machine lacks."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


# ---------------------------------------------------------------- G1.1 the pinned binary

def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def pin_for_this_system():
    pin = PINS.get(system())
    if not pin:
        raise SystemExit(f"This system ({'/'.join(system())}) is not one 0.1 supports: Linux on x86-64, macOS on "
                         "Intel or Apple Silicon. Nothing was changed.")
    return pin


def fetch_forge(p, base=None):
    """Download the pinned Gitea and check its SHA-256 before it is ever run. A mismatch deletes the file."""
    suffix, want = pin_for_this_system()
    if os.path.exists(p["bin"]) and sha256_of(p["bin"]) == want:
        return False
    url = f"{base or GITEA_BASE}/{GITEA_VERSION}/gitea-{GITEA_VERSION}-{suffix}"
    os.makedirs(os.path.dirname(p["bin"]), exist_ok=True)
    part = p["bin"] + ".download"
    try:
        # A named User-Agent: Gitea's download site answered 403 to Python's default one (2026-11-22).
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f)
        except (urllib.error.URLError, OSError) as e:
            raise SystemExit(f"Gitea {GITEA_VERSION} could not be downloaded from {url}: {e}. Nothing was run.")
        got = sha256_of(part)
        if got != want:
            raise SystemExit(f"The downloaded Gitea {GITEA_VERSION} does not match its pinned checksum "
                             f"(expected {want[:16]}..., got {got[:16]}...). It was deleted and not run.")
        os.chmod(part, 0o755)
        os.replace(part, p["bin"])
    finally:
        if os.path.exists(part):
            os.remove(part)
    return True


# ---------------------------------------------------------------- G1.3 the forge's settings

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_is_free(port):
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def app_ini(p, port):
    f = p["forge"]
    return f"""; Written by Windstorm Mirror for GitHub. Stock Gitea {GITEA_VERSION}, for this computer only.
APP_NAME = Windstorm Mirror
RUN_MODE = prod
WORK_PATH = {f}

[server]
PROTOCOL = http
HTTP_ADDR = 127.0.0.1
HTTP_PORT = {port}
DOMAIN = 127.0.0.1
ROOT_URL = http://127.0.0.1:{port}/
DISABLE_SSH = true
START_SSH_SERVER = false
LFS_START_SERVER = true
OFFLINE_MODE = true

[database]
DB_TYPE = sqlite3
PATH = {f}/data/gitea.db

[security]
INSTALL_LOCK = true

[service]
DISABLE_REGISTRATION = true
REQUIRE_SIGNIN_VIEW = true
DEFAULT_ALLOW_CREATE_ORGANIZATION = false
ENABLE_NOTIFY_MAIL = false

[repository]
DEFAULT_PRIVATE = private
DISABLE_MIGRATIONS = true

[mirror]
ENABLED = false

[actions]
ENABLED = false

[cron.update_checker]
ENABLED = false

[log]
MODE = file
LEVEL = Info
ROOT_PATH = {f}/log
"""


def ini_value(path, section, key):
    """Read one setting by its name; a config file is never printed whole (G6.4)."""
    cur = ""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if s.startswith("["):
                    cur = s.strip("[]")
                elif "=" in s and not s.startswith((";", "#")) and cur == section:
                    k, _, v = s.partition("=")
                    if k.strip() == key:
                        return v.strip()
    except OSError:
        pass
    return None


def gitea(p, *args):
    return run([p["bin"], "--config", p["app_ini"], "--work-path", p["forge"], *args],
               env=dict(os.environ, GITEA_WORK_DIR=p["forge"]))


# ---------------------------------------------------------------- G1.4 the two accounts

def person_name():
    n = re.sub(r"[^a-z0-9-]", "-", getpass.getuser().lower()).strip("-")[:30]
    return n if n and n not in ("admin", MIRROR_USER, "api", "user", "explore") else "owner"


def forge_users(p):
    out = gitea(p, "admin", "user", "list")
    names = []
    for line in out.stdout.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 2:
            names.append(cols[1])
    return names


def create_user(p, name, admin):
    """The password is made by Gitea and read from its answer: never on a command line, never printed."""
    args = ["admin", "user", "create", "--username", name, "--email", f"{name}@localhost.invalid",
            "--random-password", "--must-change-password=false"]
    if admin:
        args.append("--admin")
    out = gitea(p, *args)
    if out.returncode != 0:
        raise SystemExit(f"Gitea refused to create the account {name}: {last_line(out.stderr)}")
    m = re.search(r"generated random password is '([^']+)'", out.stdout + out.stderr)
    if not m:
        raise SystemExit(f"Gitea created {name} but did not give its password; nothing was printed.")
    return m.group(1)


def last_line(text):
    lines = [x for x in (text or "").strip().splitlines() if x.strip()]
    return redact(lines[-1]) if lines else ""


def redact(text):
    return re.sub(r"(?i)(token|password|key)([=: '\"]+)\S+", r"\1\2<hidden>", text)


# ---------------------------------------------------------------- G1.2 and G1.6 the service

def unit_text(p):
    return f"""[Unit]
Description=Windstorm Mirror for GitHub: the forge (stock Gitea {GITEA_VERSION}, this computer only)

[Service]
Type=simple
{env_line()}ExecStart={sys.executable} -I {p['script']} run-forge
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def env_line():
    """The service runs with the same home setup used (WSM_HOME, when setup ran with one)."""
    h = os.environ.get("WSM_HOME")
    return f'Environment="WSM_HOME={h}"\n' if h else ""


def env_plist():
    h = os.environ.get("WSM_HOME")
    if not h:
        return ""
    return f"  <key>EnvironmentVariables</key><dict><key>WSM_HOME</key><string>{h}</string></dict>\n"


def plist_text(p):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{sys.executable}</string><string>-I</string><string>{p['script']}</string><string>run-forge</string></array>
{env_plist()}  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>{p['forge']}/log/launchd.log</string>
  <key>StandardErrorPath</key><string>{p['forge']}/log/launchd.log</string>
</dict>
</plist>
"""


def linger_state():
    """'yes', 'no' or 'unknown' for this account's systemd lingering."""
    out = run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger"])
    if out.returncode != 0:
        return "unknown"
    return "yes" if out.stdout.strip().endswith("=yes") else "no"


def comes_back_by_itself(dry=False):
    """G1.6: (True, '') when the forge starts after a restart with no one logged in, else (False, what to say)."""
    if system()[0] == "darwin":
        return False, (f"On a Mac, {NO_LOGIN_NOTE}: a launch agent runs only in a login session, and with "
                       "FileVault on nothing runs before someone logs in.")
    state = linger_state()
    if state == "yes":
        return True, ""
    if not dry:
        if run(["loginctl", "enable-linger", getpass.getuser()]).returncode == 0 and linger_state() == "yes":
            return True, ""
    return False, (f"On this computer {NO_LOGIN_NOTE}. To have it start at boot, the computer's administrator "
                   f"runs one command: sudo loginctl enable-linger {getpass.getuser()}")


def install_service(p):
    if system()[0] == "darwin":
        write_file(p["plist"], plist_text(p))
        uid = str(os.getuid())
        run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"])
        out = run(["launchctl", "bootstrap", f"gui/{uid}", p["plist"]])
        return out.returncode == 0, last_line(out.stderr)
    write_file(p["unit"], unit_text(p))
    run(["systemctl", "--user", "daemon-reload"])
    out = run(["systemctl", "--user", "enable", "--now", SERVICE])
    return out.returncode == 0, last_line(out.stderr)


# ---------------------------------------------------------------- G2 the GitHub keys

GITHUB_API = "https://api.github.com"
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def github_api():
    return os.environ.get("WSM_GITHUB_API") or GITHUB_API


def gh_get(key, path):
    """One read from GitHub's REST API with one owner's key. Returns (status, headers, body). Never writes."""
    url = path if path.startswith("http") else github_api() + path
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT,
                                               "Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"null")
        except ValueError:
            body = None
        return e.code, dict(e.headers), body
    except (urllib.error.URLError, OSError) as e:
        return 0, {}, {"message": str(e)}


def key_path(p, owner):
    return os.path.join(p["keys"], owner.lower() + ".key")


def read_key_from_stdin():
    """A key comes in on standard input, never on a command line. At a terminal it is typed without echo."""
    if sys.stdin.isatty():
        return getpass.getpass("Paste the key (it is not shown): ").strip()
    return sys.stdin.readline().strip()


def check_key(key, owner):
    """(kind, message): kind is 'user' or 'org' when the key reaches that owner's repositories, else None."""
    code, _, body = gh_get(key, "/user")
    if code == 401:
        return None, "GitHub refused this key (401). Make a new one; nothing was kept."
    if code != 200:
        return None, f"GitHub did not answer as expected ({code}); nothing was kept."
    if (body or {}).get("login", "").lower() == owner.lower():
        kind, listing = "user", "/user/repos?affiliation=owner&per_page=1"
    else:
        code, _, _ = gh_get(key, f"/orgs/{urllib.parse.quote(owner)}")
        if code != 200:
            return None, (f"This key cannot see '{owner}' (GitHub answered {code}). A key belongs to one owner: "
                          "make one for this owner. Nothing was kept.")
        kind, listing = "org", f"/orgs/{urllib.parse.quote(owner)}/repos?per_page=1"
    code, _, _ = gh_get(key, listing)
    if code != 200:
        return None, f"This key cannot list the repositories of '{owner}' (GitHub answered {code}). Nothing was kept."
    return kind, ""


def cmd_key_set(args):
    p = paths()
    owner = args.owner
    if not OWNER_RE.match(owner):
        say(f"'{owner}' is not a GitHub account or organisation name.")
        return 2
    key = read_key_from_stdin()
    if not key or any(c.isspace() for c in key) or len(key) > 255:
        say("No key was read from standard input (or it was not one line). Nothing was kept.")
        return 2
    kind, why = check_key(key, owner)
    if not kind:
        say(why.replace(key, "<the key>"))
        return 1
    write_private(key_path(p, owner), key + "\n")
    s = load_json(p["settings"], {})
    s.setdefault("owners", {})[owner] = {"kind": kind}
    write_file(p["settings"], json.dumps(s, indent=1) + "\n", 0o600)
    say(f"The key for {owner} ({'your account' if kind == 'user' else 'organisation'}) is kept in "
        f"{key_path(p, owner)}, readable by you only. It is used for {owner}'s repositories and nothing else.")
    return 0


# ---------------------------------------------------------------- G3 find every project, and copy it

GITHUB_GIT = "https://github.com"
NOT_COPIED = "Not copied in 0.1: GitHub's pull-request refs, releases, issues and wikis."
FETCH_SPEC = ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
GIT_TIMEOUT = 600


def github_git():
    return os.environ.get("WSM_GITHUB_GIT") or GITHUB_GIT


def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def next_link(headers):
    for part in (headers.get("Link") or headers.get("link") or "").split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None


def list_owner(owner, kind, key):
    """Every repository of one owner, page by page to the last page. (repos, None), or (None, why) when any page
    failed: half a list is never acted on as if it were whole."""
    url = ("/user/repos?affiliation=owner&per_page=100" if kind == "user"
           else f"/orgs/{urllib.parse.quote(owner)}/repos?type=all&per_page=100")
    repos, page, calls = [], 0, 0
    while url:
        page += 1
        code, headers, body = gh_get(key, url)
        calls += 1
        if code == 401:
            return None, f"GitHub refused the key for {owner} (401)", calls
        if code != 200 or not isinstance(body, list):
            return None, f"the listing of {owner} failed at page {page} (GitHub answered {code})", calls
        repos.extend(r for r in body if r.get("owner", {}).get("login", "").lower() == owner.lower())
        url = next_link(headers)
    return repos, None, calls


def chosen(full, settings):
    inc, exc = settings.get("include") or [], settings.get("exclude") or []
    if inc and not any(fnmatch.fnmatchcase(full.lower(), x.lower()) for x in inc):
        return False, "not in include"
    for x in exc:
        if fnmatch.fnmatchcase(full.lower(), x.lower()):
            return False, f"left out by '{x}'"
    return True, ""


def copy_name(full):
    """The copy's name in the forge. GitHub names cannot hold '--' in an owner, so owner--name is one-to-one."""
    owner, name = full.split("/", 1)
    return f"{owner}--{name}"


class HardStop(Exception):
    """Something that must stop the whole sync at once, said in one line."""


class Forge:
    """The person's own forge, through its API, as the mirror account."""

    def __init__(self, p, st):
        self.base = f"http://127.0.0.1:{st['port']}"
        self.key = open(p["mirror_key"], encoding="utf-8").read().strip()
        self.user = st.get("mirror_user", MIRROR_USER)

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + "/api/v1" + path, data=data, method=method,
                                     headers={"Authorization": f"token {self.key}", "Content-Type": "application/json",
                                              "User-Agent": USER_AGENT})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, None
        except (urllib.error.URLError, OSError) as e:
            return 0, {"message": str(e)}

    def by_id(self, rid):
        code, body = self.call("GET", f"/repositories/{rid}")
        return body if code == 200 else None

    def by_name(self, name):
        code, body = self.call("GET", f"/repos/{self.user}/{urllib.parse.quote(name)}")
        return body if code == 200 and body and body.get("name", "").lower() == name.lower() else None

    def rename(self, rid, cur, new):
        code, body = self.call("PATCH", f"/repos/{self.user}/{urllib.parse.quote(cur)}", {"name": new})
        return code == 200 and body.get("id") == rid

    def create(self, name, about):
        """M17: a repository made through the API is public unless 'private' is sent. The answer is checked, and
        a copy that comes back not private is deleted at once and the whole sync stops."""
        code, body = self.call("POST", "/user/repos", {"name": name, "private": True, "description": about})
        if code != 201:
            raise RuntimeError(f"the forge refused to create {name} ({code})")
        if body.get("private") is not True:
            dcode, _ = self.call("DELETE", f"/repos/{self.user}/{urllib.parse.quote(name)}")
            raise HardStop(f"the forge made the copy {name} without 'private'; it was deleted at once "
                           f"({'deleted' if dcode == 204 else f'delete answered {dcode}: delete it by hand'}), "
                           "and the sync stopped. Nothing else was copied.")
        self.call("PATCH", f"/repos/{self.user}/{urllib.parse.quote(name)}", {"has_actions": False})
        return body

    def git_env(self):
        hdr = "Authorization: Basic " + base64.b64encode(f"{self.user}:{self.key}".encode()).decode()
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{self.base}/.extraheader", "GIT_CONFIG_VALUE_0": hdr}


def github_env(key):
    """G3.4: the key travels only in the environment of the one git process that fetches from GitHub."""
    hdr = "Authorization: Basic " + base64.b64encode(f"x-access-token:{key}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{github_git()}/.extraheader", "GIT_CONFIG_VALUE_0": hdr}


def git(cache, *args, env=None, timeout=GIT_TIMEOUT):
    e = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    for k in [k for k in e if k.startswith("GIT_CONFIG_")]:
        e.pop(k)
    e.update(env or {})
    cmd = ["git", "-c", "http.followRedirects=false", "-c", "credential.helper="]
    if cache:
        cmd += ["--git-dir", cache]
    return run(cmd + list(args), env=e, timeout=timeout)


def has_git_lfs():
    try:
        return run(["git", "lfs", "version"], timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def uses_lfs(cache):
    """Does any branch or tag tip say, in its top .gitattributes, that files are kept with Git LFS?"""
    for ref in git(cache, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags").stdout.split():
        if "filter=lfs" in git(cache, "show", f"{ref}:.gitattributes").stdout:
            return True
    return False


def refs_of(cache):
    out = git(cache, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags")
    return dict(reversed(line.split(" ", 1)) for line in out.stdout.splitlines() if line.strip())


def place_copy(forge, rec, full, gid, report):
    """Find this GitHub repository's copy by the forge id kept for it, follow a rename, and move aside any other
    copy that sits under the name it should have. Returns the copy's current forge record."""
    want = copy_name(full)
    cur = forge.by_id(rec["forge_id"]) if rec.get("forge_id") else None
    if cur and cur.get("name") != want:
        other = forge.by_name(want)
        if other and other["id"] != cur["id"]:
            aside = f"{want}--was-{gid_of(other)}"
            forge.rename(other["id"], other["name"], aside)
            report.append(f"{want}: another copy held this name; it was moved aside to {aside}")
        if forge.rename(cur["id"], cur["name"], want):
            report.append(f"{full}: renamed on GitHub; its copy {cur['name']} is now {want}")
            cur = forge.by_id(cur["id"])
    if not cur:
        other = forge.by_name(want)
        if other:
            aside = f"{want}--was-{gid_of(other)}"
            forge.rename(other["id"], other["name"], aside)
            report.append(f"{want}: an older copy held this name; it was moved aside to {aside}")
        cur = forge.create(want, f"Copy of github.com/{full} (GitHub id {gid}), kept by Windstorm Mirror for GitHub")
        rec["forge_id"] = cur["id"]
    return cur


def gid_of(forge_repo):
    m = re.search(r"GitHub id (\d+)", forge_repo.get("description") or "")
    return m.group(1) if m else f"forge{forge_repo['id']}"


def copy_one(p, forge, key, rec, gh, report):
    """Fetch one repository from GitHub (key in memory only) into its cache, then push it into the forge."""
    gid = str(gh["id"])
    full = gh["full_name"]
    cache = os.path.join(p["cache"], gid + ".git")
    if not os.path.exists(cache):
        os.makedirs(p["cache"], mode=0o700, exist_ok=True)
        if git(None, "init", "-q", "--bare", cache).returncode != 0:
            return "the local cache could not be made"
    git(cache, "config", "remote.github.url", f"{github_git()}/{full}.git")
    for i, spec in enumerate(FETCH_SPEC):
        git(cache, "config", "--replace-all" if i == 0 else "--add", "remote.github.fetch", spec)
    try:
        out = git(cache, "fetch", "--prune", "--quiet", "github", env=github_env(key))
    except subprocess.TimeoutExpired:
        return "the fetch from GitHub took too long"
    if out.returncode != 0:
        return "the fetch from GitHub failed: " + last_line(out.stderr).replace(key, "<the key>")
    lfs = None
    if not refs_of(cache):
        pass                                   # an empty repository has no large files to fetch or push
    elif has_git_lfs():
        out = git(cache, "lfs", "fetch", "--all", "github", env=github_env(key))
        if out.returncode != 0:
            return "the large files could not be fetched: " + last_line(out.stderr).replace(key, "<the key>")
        lfs = True
    elif uses_lfs(cache):
        lfs = False
    cur = place_copy(forge, rec, full, gid, report)
    # Read the copy's name by its id again just before the push, and never follow a redirect (contract C7).
    check = forge.by_id(rec["forge_id"])
    if not check or check.get("name") != copy_name(full):
        return "the copy's name could not be confirmed before the push"
    url = f"{forge.base}/{forge.user}/{check['name']}.git"
    git(cache, "config", "remote.forge.url", url)
    out = git(cache, "push", "--quiet", "--prune", "forge", *FETCH_SPEC, env=forge.git_env())
    if out.returncode != 0 and "No refs in common and none specified" not in out.stderr:
        empty = not refs_of(cache)
        if not empty:
            return "the push into the forge failed: " + last_line(out.stderr)
    if lfs:
        out = git(cache, "lfs", "push", "--all", "forge", env=forge.git_env())
        if out.returncode != 0:
            return "the large files could not be pushed into the forge: " + last_line(out.stderr)
    rec.update(full_name=full, forge_name=cur["name"] if cur else copy_name(full), refs=refs_of(cache),
               last_good=now_utc(), pushed_at=gh.get("pushed_at"), problem="", lfs_missing=(lfs is False))
    return ""


def cmd_config(args):
    p = paths()
    s = load_json(p["settings"], {})
    if args.clear:
        s.pop(args.what, None)
    else:
        s[args.what] = list(args.patterns)
    write_file(p["settings"], json.dumps(s, indent=1) + "\n", 0o600)
    say(f"{args.what}: {', '.join(s.get(args.what) or []) or '(none)'}")
    return 0


def cmd_sync(args):
    p = paths()
    st = load_json(p["state"], {})
    s = load_json(p["settings"], {})
    owners = s.get("owners") or {}
    if not st.get("port") or not os.path.exists(p["mirror_key"]):
        say("Run setup first.")
        return 2
    if not owners:
        say("No GitHub key is kept yet. Run: key set <your GitHub account> (and one per organisation).")
        return 2
    state = st.setdefault("repos", {})
    found, problems, report, calls = {}, [], [], 0
    complete = set()
    for owner, meta in owners.items():
        kp = key_path(p, owner)
        if not os.path.exists(kp):
            problems.append(f"{owner}: no key is kept for it")
            continue
        key = open(kp, encoding="utf-8").read().strip()
        repos, why, n = list_owner(owner, meta.get("kind", "user"), key)
        calls += n
        if repos is None:
            problems.append(f"{owner}: {why}; nothing was changed for {owner}")
            for rec in state.values():
                if rec.get("owner", "").lower() == owner.lower():
                    rec["problem"] = "cannot refresh: " + why
            continue
        complete.add(owner.lower())
        for r in repos:
            found[str(r["id"])] = (owner, key, r)
    picked, skipped = {}, []
    for gid, (owner, key, r) in found.items():
        ok, why = chosen(r["full_name"], s)
        (picked.__setitem__(gid, (owner, key, r)) if ok else skipped.append(f"{r['full_name']} ({why})"))
    if s.get("include") and not picked and found:
        say("The include list matches no repository GitHub shows; the sync stops rather than copy everything.")
        return 2
    if not st.get("announced") and not args.announce:
        say("Before the first copy, run: sync --announce (it says what will be copied, and how big it is).")
        return 2
    if args.announce and not st.get("announced"):
        size = sum(int(r.get("size") or 0) for _, _, r in picked.values())
        say(f"The first copy: {len(picked)} repositories from {len(complete)} owner(s), "
            f"{size / 1024:.1f} MB as GitHub reports it (GitHub can show 0 for a new repository).")
        say(NOT_COPIED)
        say("The copy is as private as this computer. Anyone who can read this account's files can read every "
            "project in it. Disk encryption, and who uses this computer, are yours.")
        st["announced"] = now_utc()
        write_file(p["state"], json.dumps(st, indent=1) + "\n", 0o600)
        say("When the person says go, run: sync")
        return 0
    forge = Forge(p, st)
    done, started = 0, time.time()
    order = sorted(picked.items(), key=lambda x: x[1][2]["full_name"].lower())
    for i, (gid, (owner, key, r)) in enumerate(order):
        if i == 3 and len(order) > 3 and not st.get("first_copy_done"):
            per = (time.time() - started) / 3
            say(f"first copy: 3 of {len(order)} done; about {max(1, round(per * (len(order) - 3) / 60))} min left at this pace")
        rec = state.setdefault(gid, {})
        rec.update(owner=owner, full_name=r["full_name"], gone_since="")
        try:
            why = copy_one(p, forge, key, rec, r, report)
        except HardStop as e:
            rec["problem"] = "cannot refresh: the sync stopped"
            st["last_sync"] = {"at": now_utc(), "calls": calls, "copied": done, "problems": 1, "stopped": str(e)}
            write_file(p["state"], json.dumps(st, indent=1) + "\n", 0o600)
            say(f"STOPPED: {e}")
            return 1
        except RuntimeError as e:
            why = str(e)
        if why:
            rec["problem"] = "cannot refresh: " + why
            problems.append(f"{r['full_name']}: {why}")
        else:
            done += 1
    for gid, rec in state.items():
        if gid not in found and rec.get("owner", "").lower() in complete and not rec.get("gone_since"):
            rec["gone_since"] = now_utc()
            report.append(f"{rec.get('full_name')}: gone from GitHub (or no longer visible to its key); its copy is kept")
    st["last_sync"] = {"at": now_utc(), "calls": calls, "copied": done, "problems": len(problems)}
    if not problems:
        st["first_copy_done"] = st.get("first_copy_done") or now_utc()
    write_file(p["state"], json.dumps(st, indent=1) + "\n", 0o600)
    for line in report + [f"problem: {x}" for x in problems]:
        say(line)
    for x in skipped:
        say(f"left out: {x}")
    lfs_missing = [rec["full_name"] for rec in state.values() if rec.get("lfs_missing")]
    if lfs_missing:
        say("Large files NOT copied (git lfs is not installed here): " + ", ".join(sorted(lfs_missing)))
    say(f"sync: {len(found)} on GitHub, {done} copied now, {len(problems)} problem(s), {calls} GitHub API call(s)")
    say(NOT_COPIED)
    return 0 if not problems else 1


# ---------------------------------------------------------------- commands

def setup_plan(p, st):
    plan = []
    if not (os.path.exists(p["bin"]) and sha256_of(p["bin"]) == pin_for_this_system()[1]):
        plan.append(f"download Gitea {GITEA_VERSION} for {'/'.join(system())} from {GITEA_BASE} and check its "
                    "pinned SHA-256 before running it")
    if not os.path.exists(p["app_ini"]):
        plan.append("write the forge's settings: 127.0.0.1 only, a free port, SSH off, sign-up closed, sign-in "
                    "needed to see anything, Actions off, Gitea's own mirrors and migrations off, no update check")
    plan.append("prepare the forge's database (gitea migrate)")
    if not st.get("person"):
        plan.append(f"make your Gitea account '{person_name()}' (administrator) and save its first password in "
                    f"{p['password']}, readable by you only")
    if not st.get("mirror_user"):
        plan.append(f"make the '{MIRROR_USER}' account that owns the copies, with a key of its own in "
                    f"{p['mirror_key']}, readable by you only")
    plan.append(f"copy this script to {p['script']}")
    plan.append("install the forge as your own service (" +
                ("a launch agent" if system()[0] == "darwin" else "a systemd user unit") + ") and start it")
    return plan


def cmd_setup(args):
    p = paths()
    st = load_json(p["state"], {})
    plan = setup_plan(p, st)
    back, note = comes_back_by_itself(dry=True)
    if args.dry_run:
        say("setup --dry-run: nothing is changed. Setup would:")
        for i, step in enumerate(plan, 1):
            say(f"  {i}. {step}")
        if not back:
            say(f"Note: {note}")
        return 0
    os.makedirs(p["data"], mode=0o700, exist_ok=True)
    os.makedirs(p["conf"], mode=0o700, exist_ok=True)
    if fetch_forge(p, os.environ.get("WSM_GITEA_BASE")):
        say(f"Gitea {GITEA_VERSION} downloaded; checksum matches the pin.")
    else:
        say(f"Gitea {GITEA_VERSION} already here; checksum matches the pin.")
    if not os.path.exists(p["app_ini"]):
        port = free_port()
        write_file(p["app_ini"], app_ini(p, port), 0o600)
        st["port"] = port
    st.setdefault("port", int(ini_value(p["app_ini"], "server", "HTTP_PORT") or 0))
    for d in ("data", "log", "custom/conf"):
        os.makedirs(os.path.join(p["forge"], d), exist_ok=True)
    out = gitea(p, "migrate")
    if out.returncode != 0:
        raise SystemExit(f"Gitea could not prepare its database: {last_line(out.stderr)}")
    users = forge_users(p)
    if not st.get("person"):
        name = person_name()
        if name not in users:
            write_private(p["password"], create_user(p, name, admin=True) + "\n")
        st["person"] = name
        say(f"Your Gitea account '{name}' is made. Its first password is in {p['password']} (readable by you only).")
    if not st.get("mirror_user"):
        if MIRROR_USER not in users:
            create_user(p, MIRROR_USER, admin=False)
        out = gitea(p, "admin", "user", "generate-access-token", "--username", MIRROR_USER,
                    "--token-name", "windstorm-mirror", "--scopes", MIRROR_SCOPES, "--raw")
        if out.returncode != 0 or not out.stdout.strip():
            raise SystemExit("Gitea did not give the mirror account its key; nothing was printed.")
        write_private(p["mirror_key"], out.stdout.strip() + "\n")
        st["mirror_user"] = MIRROR_USER
        say(f"The '{MIRROR_USER}' account that owns the copies is made; its key is in {p['mirror_key']}.")
    here = os.path.abspath(__file__)
    if os.path.abspath(p["script"]) != here:
        shutil.copyfile(here, p["script"] + ".tmp")
        os.replace(p["script"] + ".tmp", p["script"])
    st.update(version=VERSION, gitea=GITEA_VERSION)
    write_file(p["state"], json.dumps(st, indent=1) + "\n", 0o600)
    ok, why = install_service(p)
    say(f"The forge runs as your own service on http://127.0.0.1:{st['port']}/ (this computer only)."
        if ok else f"The forge's service did not start: {why}")
    back, note = comes_back_by_itself()
    say("After a restart it starts by itself." if back else note)
    return 0 if ok else 1


def cmd_run_forge(args):
    """What the service runs: check the port is still ours to take, then become Gitea."""
    p = paths()
    if not os.path.exists(p["app_ini"]):
        say(f"The forge cannot start: its settings are not at {p['app_ini']}. Run setup.")
        return 3
    port = int(ini_value(p["app_ini"], "server", "HTTP_PORT") or 0)
    if not port or not port_is_free(port):
        say(f"The forge cannot start: port {port} on 127.0.0.1 is taken by another program. "
            "Run setup again to pick a free one.")
        return 3
    env = dict(os.environ, GITEA_WORK_DIR=p["forge"])
    os.execve(p["bin"], [p["bin"], "--config", p["app_ini"], "--work-path", p["forge"], "web"], env)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mirror.py", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"Windstorm Mirror for GitHub {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="install the forge as your own service")
    s.add_argument("--dry-run", action="store_true", help="say what setup would do and change nothing")
    s.set_defaults(fn=cmd_setup)
    k = sub.add_parser("key", help="the GitHub keys, one per owner")
    ks = k.add_subparsers(dest="key_cmd", required=True)
    kset = ks.add_parser("set", help="keep one owner's read-only key, read from standard input")
    kset.add_argument("owner", help="your GitHub account, or one organisation")
    kset.set_defaults(fn=cmd_key_set)
    c = sub.add_parser("config", help="choose what is copied: include or exclude patterns of owner/name")
    c.add_argument("what", choices=["include", "exclude"])
    c.add_argument("patterns", nargs="*")
    c.add_argument("--clear", action="store_true")
    c.set_defaults(fn=cmd_config)
    y = sub.add_parser("sync", help="copy every chosen repository from GitHub into the forge")
    y.add_argument("--announce", action="store_true", help="before the first copy: say what will be copied")
    y.set_defaults(fn=cmd_sync)
    r = sub.add_parser("run-forge", help="(used by the service) start the forge")
    r.set_defaults(fn=cmd_run_forge)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
