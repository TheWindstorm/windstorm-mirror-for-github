#!/usr/bin/env python3
"""Windstorm Mirror for GitHub: a second, always-current copy of all your GitHub projects on a computer you own.

One script, Python standard library only. It installs stock Gitea at a pinned version as the person's own
service, reachable from this computer only, and (in later genes) keeps a copy of every repository in it.
The plan this is built from: DNA-STRAND-MASTER-PLAN.md. Run with `python3 -I mirror.py <command>`.
"""
# G1.12: the lowest Python this names is checked first, before anything else is read or made. These lines, and the
# whole file, stay readable by an older Python's grammar, so that an older Python reaches them and stops here.
import sys
if sys.version_info < (3, 9):
    sys.stdout.write(
        "Stopped: this is Python %d.%d.%d (%s), and this skill needs Python 3.9 or newer. Nothing was made. Run it "
        "with a python3 of 3.9 or newer (python3 --version says which one runs); its tests pass on 3.9, 3.10 and "
        "3.13.\n" % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable or "python3"))
    sys.exit(3)
import argparse
import base64
import calendar
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
import tarfile
import tempfile
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
        "first_copy_list": os.path.join(data, "first-copy-list.txt"),
        "password": os.path.join(conf, "forge-password"),
        "mirror_key": os.path.join(conf, "forge-mirror-key"),
        "keys": os.path.join(conf, "keys"),
        "cache": os.path.join(data, "cache"),
        "settings": os.path.join(conf, "settings.json"),
        "unit": os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"),
                             "systemd", "user", SERVICE + ".service"),
        "plist": os.path.join(home, "Library", "LaunchAgents", LAUNCHD_LABEL + ".plist"),
    }


SECRETS = set()          # every key and password this process has read; say() never prints one (G6.4)


def secret(value):
    if value and len(value) >= 8:
        SECRETS.add(value)
    return value


def say(msg=""):
    msg = str(msg)
    for s in SECRETS:
        msg = msg.replace(s, "<a key>")
    try:
        print(msg, flush=True)
    except BrokenPipeError:
        # G6.11 (M49): the reader stopped reading (`status | head`). The work goes on to its end; the rest of its
        # words go nowhere, rather than a traceback, or a sync stopped halfway through by a closed pipe.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def tell(lines):
    """G10.5: what the person must hear goes out as one numbered block, "tell the person 1 of N: ..." to "N of N",
    so a line dropped, shortened or merged on its way to the person shows (a cold start saw the smallest model
    shorten the dry run to one line and drop a sentence of the privacy line)."""
    lines = [x for x in lines if x]
    for i, x in enumerate(lines, 1):
        say(f"tell the person {i} of {len(lines)}: {x}")


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
    """Every outside command goes through here, so tests can stand in for the ones a CI machine lacks. A program
    that is not installed answers 127 with a plain sentence, never a traceback (seen: a computer with no git)."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} is not installed on this computer")


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
                         "Intel or Apple Silicon. Nothing was changed. To ask for this system, open an issue at "
                         f"https://github.com/{SKILL_REPO}.")
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


def running_bin(p):
    """The forge's binary is the one the state names, not this script's own pin: a self-update that brings a newer
    pin never changes the forge by itself (G9.1)."""
    st = load_json(p["state"], {})
    ver = st.get("gitea")
    return os.path.join(os.path.dirname(p["bin"]), f"gitea-{ver}") if ver else p["bin"]


def gitea(p, *args):
    return run([running_bin(p), "--config", p["app_ini"], "--work-path", p["forge"], *args],
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
        raise SystemExit(f"Gitea refused to create the account {name}: {last_line(out.stderr)}. Run setup again; if "
                         "it stops here again, Gitea's words above are the reason.")
    m = re.search(r"generated random password is '([^']+)'", out.stdout + out.stderr)
    if not m:
        raise SystemExit(f"Gitea created {name} but did not give its password; nothing was printed. The account has "
                         "no password this skill knows: open an issue with this line.")
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


SYNC_UNIT = "windstorm-mirror-sync"
SYNC_LABEL = "com.thewindstorm.mirror.sync"


def sync_unit_text(p):
    return f"""[Unit]
Description=Windstorm Mirror for GitHub: one sync of the copies

[Service]
Type=oneshot
{env_line()}ExecStart={sys.executable} -I {p['script']} sync --from-timer
Nice=10
"""


def sync_timer_text(minutes):
    return f"""[Unit]
Description=Windstorm Mirror for GitHub: a sync every {minutes} minutes

[Timer]
OnStartupSec=2min
OnUnitActiveSec={minutes}min

[Install]
WantedBy=timers.target
"""


def sync_plist_text(p, minutes):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{SYNC_LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{sys.executable}</string><string>-I</string><string>{p['script']}</string><string>sync</string><string>--from-timer</string></array>
{env_plist()}  <key>StartInterval</key><integer>{minutes * 60}</integer>
  <key>StandardOutPath</key><string>{p['forge']}/log/sync.log</string>
  <key>StandardErrorPath</key><string>{p['forge']}/log/sync.log</string>
</dict>
</plist>
"""


def install_timer(p, minutes):
    if system()[0] == "darwin":
        path = os.path.join(os.path.dirname(p["plist"]), SYNC_LABEL + ".plist")
        write_file(path, sync_plist_text(p, minutes))
        uid = str(os.getuid())
        run(["launchctl", "bootout", f"gui/{uid}/{SYNC_LABEL}"])
        out = run(["launchctl", "bootstrap", f"gui/{uid}", path])
        return out.returncode == 0
    base = os.path.dirname(p["unit"])
    write_file(os.path.join(base, SYNC_UNIT + ".service"), sync_unit_text(p))
    write_file(os.path.join(base, SYNC_UNIT + ".timer"), sync_timer_text(minutes))
    run(["systemctl", "--user", "daemon-reload"])
    return run(["systemctl", "--user", "enable", "--now", SYNC_UNIT + ".timer"]).returncode == 0


def next_sync():
    """(when, problem): the timer's next run as the system reads it. M12: 'on' with no next run is a problem."""
    if system()[0] == "darwin":
        out = run(["launchctl", "print", f"gui/{os.getuid()}/{SYNC_LABEL}"])
        if out.returncode != 0:
            return "", "the sync timer is not loaded"
        m = re.search(r"run interval = (\d+) seconds", out.stdout)
        return (f"every {int(m.group(1)) // 60} min (launchd keeps no next-run time)" if m else "loaded"), ""
    out = run(["systemctl", "--user", "show", SYNC_UNIT + ".timer", "-p", "ActiveState", "-p", "NextElapseUSecRealtime",
               "-p", "NextElapseUSecMonotonic"])
    vals = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    if vals.get("ActiveState") != "active":
        return "", "the sync timer is not running"
    nxt = vals.get("NextElapseUSecRealtime", "").strip()
    if nxt and nxt not in ("n/a", "0"):
        return nxt, ""
    # Seen on a stock Ubuntu 24.04: a next run kept on the boot clock has an empty wall-clock field.
    mono = span_seconds(vals.get("NextElapseUSecMonotonic", ""))
    if mono is not None:
        return time.strftime("%a %Y-%m-%d %H:%M:%S %Z", time.localtime(time.time() + mono - time.monotonic())), ""
    return "", "the sync timer is on but has no next run (after a restart, check it with: systemctl --user list-timers)"


SPAN_UNITS = {"us": 1e-6, "ms": 1e-3, "s": 1, "min": 60, "h": 3600, "d": 86400, "w": 604800}


def span_seconds(text):
    """systemd's time span ('7min 35.596695s', '2h', '0') in seconds; None for empty, 'infinity' or unreadable."""
    text = (text or "").strip()
    if not text or text in ("infinity", "0", "n/a"):
        return None
    parts = re.findall(r"([0-9.]+)\s*(us|ms|min|s|h|d|w)\b", text)
    if not parts or "".join(a + b for a, b in parts) != text.replace(" ", ""):
        return None
    return sum(float(a) * SPAN_UNITS[b] for a, b in parts)


def linger_state():
    """'yes', 'no' or 'unknown' for this account's systemd lingering."""
    out = run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger"])
    if out.returncode != 0:
        return "unknown"
    return "yes" if out.stdout.strip().endswith("=yes") else "no"


def comes_back_by_itself(dry=False):
    """G1.6: (True, '') when the forge starts after a restart with no one logged in, else (False, what to say)."""
    if system()[0] == "darwin":
        # Seen on a Mac (contract C9): when the system logs this account in by itself after a restart, the
        # launch agents come back with no one at the keyboard. FileVault on turns that automatic login off.
        # Two facts only are kept from the automatic-login setting: is it set, and is it this person's own
        # account. It may name another person's account on a shared Mac; that name is never printed or kept.
        auto = run(["defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser"])
        auto_set = auto.returncode == 0 and bool(auto.stdout.strip())
        auto_mine = auto_set and auto.stdout.strip() == getpass.getuser()
        del auto
        vault = run(["fdesetup", "status"])
        if auto_mine and "FileVault is Off" in vault.stdout:
            return True, ""
        why = ("automatic login is set for another account, not yours" if auto_set and not auto_mine
               else "this Mac does not log you in by itself after a restart")
        return False, (f"On a Mac, {NO_LOGIN_NOTE}: a launch agent runs only in a login session, and {why}"
                       + (" (FileVault is on)" if "FileVault is On" in vault.stdout else "") + ".")
    return linger_step(None, dry=dry)


LINGER_MEANS = ("the mirror keeps running when you are logged out and after a restart, because your account's "
                "background services are allowed to start without you")
LINGER_COMMAND = "sudo loginctl enable-linger {user}"


def own_process_for_polkit():
    """pid,pid-start-time,uid: the form pkcheck's manual asks for (the bare pid, and pid,start-time, are racy)."""
    with open(f"/proc/{os.getpid()}/stat", encoding="ascii") as f:
        start = f.read().rsplit(")", 1)[1].split()[19]          # field 22, starttime, counted after the name
    return f"{os.getpid()},{start},{os.getuid()}"


def may_self_linger():
    """'yes', 'no' or 'unknown': asked of this computer, never guessed from its name, may this person switch on
    their own lingering? Seen on a stock Ubuntu 24.04: polkit allows it to any user. pkcheck is read-only, is
    never given --allow-user-interaction (so no password prompt can appear), and has 10 s to answer. Its manual:
    0 allowed; 1 not allowed; 2 only with an authentication, which is never asked for, so not allowed here;
    anything else, a missing pkcheck, or no answer in time: not known."""
    try:
        code = run(["pkcheck", "--action-id", "org.freedesktop.login1.set-self-linger",
                    "--process", own_process_for_polkit()], timeout=10).returncode
    except (subprocess.TimeoutExpired, OSError, IndexError, ValueError):
        return "unknown"
    return {0: "yes", 1: "no", 2: "no"}.get(code, "unknown")


def linger_step(st, dry=False, allow=True):
    """G1.6 on Linux: (True, what to say) when the forge will start after a restart with no one logged in, else
    (False, what to say). Lingering changes how the person's computer behaves after they log out, so it is said
    before setup, switched on only by setup's own run and only if the person did not say no, and recorded in
    the state (st["linger_by_setup"]) so that uninstall switches off only what setup switched on."""
    user = getpass.getuser()
    admin = (f"On this computer {NO_LOGIN_NOTE}. To have it start at boot, the computer's administrator runs one "
             f"command: {LINGER_COMMAND.format(user=user)} ({LINGER_MEANS}).")
    state = linger_state()
    if state == "yes":
        if st is not None and "linger_by_setup" not in st:
            st["linger_by_setup"] = False
        return True, f"Lingering is already on for your account: {LINGER_MEANS}."
    if not allow:
        return False, (f"Lingering stays off, as you chose: the mirror syncs only while you are logged in, and "
                       f"after a restart it waits until you log in. To change that later, run setup --linger.")
    say_no = ("It is your choice: setup --linger is yes; setup --no-linger is no, and then the mirror syncs only "
              "while you are logged in.")
    if dry:
        may = may_self_linger()
        if may == "yes":
            return False, (f"Lingering means {LINGER_MEANS}. This computer lets you do that yourself, so setup "
                           f"can switch it on without asking for a password. {say_no}")
        if may == "unknown":
            return False, (f"Lingering means {LINGER_MEANS}. This computer did not say whether you may switch it on "
                           f"yourself, so setup --linger tries without asking for a password; if the computer "
                           f"refuses, its administrator runs one command: {LINGER_COMMAND.format(user=user)}. "
                           f"{say_no}")
        return False, f"{admin} {say_no}"
    # Never a password prompt the person did not expect: refused means the administrator's one command.
    if run(["loginctl", "--no-ask-password", "enable-linger", user]).returncode == 0 and linger_state() == "yes":
        if st is not None:
            st["linger_by_setup"] = True
        return True, f"Lingering is now on for your account: {LINGER_MEANS}. uninstall switches it off again."
    return False, admin


LINGER_CHOICE = ("Needs you: lingering is the person's choice. Ask them, then run setup again with --linger (yes) or "
                 "--no-linger (no). Nothing was changed.")


def linger_unchosen(args, st):
    """G1.9: on Linux, with lingering off, setup changes nothing until the person has said yes or no to it."""
    if system()[0] == "darwin" or getattr(args, "linger", False) or getattr(args, "no_linger", False):
        return False
    return st.get("linger_said") != "no" and linger_state() != "yes"


SERVICE_PATHS = {"linux": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                 "darwin": "/usr/bin:/bin:/usr/sbin:/sbin"}


def service_path():
    """G1.8: the PATH the forge's service and the sync timer run with: on Linux the person's own service
    manager's (read-only), on a Mac launchd's own for a launch agent."""
    kind = system()[0]
    if kind == "linux":
        try:
            out = run(["systemctl", "--user", "show-environment"], timeout=10)
            for line in (out.stdout or "").splitlines() if out.returncode == 0 else []:
                if line.startswith("PATH="):
                    return line[5:]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return SERVICE_PATHS.get(kind, SERVICE_PATHS["linux"])



# G1.8 and G1.14 (M51, the family's rule 7): every install line here was run exactly as printed, in a terminal,
# its prompts answered by hand, on a fresh image of that system; the plan's "Install lines, as seen" table says
# where and when, and a test holds that no line is named that is not in it. Only this computer's own way is said:
# a person on Fedora has no use for the apt line. An apt line alone fails on a fresh Ubuntu or Debian image (stale
# or no package lists), so it starts with apt update. A system not named here (a kin of these too: its line was
# never run there) gets the program's own page, not a guess.
OS_RELEASE = "/etc/os-release"
INSTALL_SEEN = {
    ("ubuntu", "git"): "sudo apt update && sudo apt install git",
    ("debian", "git"): "sudo apt update && sudo apt install git",
    ("fedora", "git"): "sudo dnf install git",
    ("ubuntu", "git-lfs"): "sudo apt update && sudo apt install git-lfs",
    ("debian", "git-lfs"): "sudo apt update && sudo apt install git-lfs",
    ("fedora", "git-lfs"): "sudo dnf install git-lfs",
}
OWN_PAGE = {"git": "https://git-scm.com/downloads", "git-lfs": "https://git-lfs.com"}


def os_id():
    """'mac', or the ID line of /etc/os-release ('ubuntu', 'debian', 'fedora', ...), or '' when it cannot be read."""
    if system()[0] == "darwin":
        return "mac"
    try:
        with open(OS_RELEASE, encoding="utf-8") as f:
            m = re.search(r'^ID="?([A-Za-z0-9._-]+)"?\s*$', f.read(), re.M)
    except OSError:
        return ""
    return m.group(1).lower() if m else ""


def install_words(tool):
    """How to install git or git-lfs on this computer, said only as far as it was seen (G1.14)."""
    here = os_id()
    if here == "mac" and tool == "git":
        # G1.13: a Mac is written as what was seen; what this shows on a Mac without the tools, nobody has.
        return "git comes with Apple's developer tools, and xcode-select --install is Apple's command for them"
    line = INSTALL_SEEN.get((here, tool))
    name = "git lfs" if tool == "git-lfs" else "git"
    return f"on this computer: {line}" if line else f"{name}'s own page is {OWN_PAGE[tool]}"

def git_out_of_service_reach():
    """The sentence when git is here only in a folder the service does not look in, else ''."""
    here = shutil.which("git")
    where = service_path()
    if not here or shutil.which("git", path=where):
        return ""
    return (f"git is at {here}, in a folder the mirror's service does not look in ({where}), so the forge and the "
            f"sync would not find it. Install git for the whole computer ({install_words('git')}); that is the person's "
            f"step, or their administrator's. Nothing was changed.")


def git_missing_sentence():
    return (f"git is not installed on this computer. The skill and the forge both need it. Install it ({install_words('git')}), "
            "then run setup again.")


LFS_NOT_IN_COPY = ("its large files are NOT in the copy, because git lfs was not installed when it was copied; "
                   "install git lfs, and the next sync copies them")
def lfs_missing():
    return ("git lfs is not installed here, so projects with large files are copied without them until it is "
            f"({install_words('git-lfs')}; setup and sync find it by themselves once it is installed).")


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


def spent_until(code, headers, floor=0, note=None):
    """G4.8: the time (epoch) until which GitHub's allowance for this key is spent, else None. GitHub says it two ways:
    a 403 or 429 with no calls remaining (back at X-RateLimit-Reset), or with Retry-After (back that many seconds
    on). Below the skill's own floor the sync waits too, until the same reset; that is not "spent", so `note` gets
    below_floor = [left, floor] for the line to say what it is (M9: a real machine was told "spent" with 4755 left)."""
    h = {k.lower(): str(v) for k, v in (headers or {}).items()}
    reset = int(h["x-ratelimit-reset"]) if h.get("x-ratelimit-reset", "").isdigit() else int(time.time()) + 3600
    if code in (403, 429):
        if h.get("retry-after", "").isdigit():
            return int(time.time()) + int(h["retry-after"])
        if h.get("x-ratelimit-remaining") == "0":
            return reset
    left = h.get("x-ratelimit-remaining", "")
    if floor and left.isdigit() and int(left) < floor:
        if note is not None:
            note["below_floor"] = [int(left), floor]
        return reset
    return None


def stamp_of(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def key_path(p, owner):
    return os.path.join(p["keys"], owner.lower() + ".key")


def read_key_from_stdin():
    """A key comes in on standard input, never on a command line. At a terminal it is typed without echo."""
    if sys.stdin.isatty():
        return getpass.getpass("Paste the key (it is not shown): ").strip()
    return sys.stdin.readline().strip()


def gh_unread(code):
    """G5.5 (M52): a read of GitHub that got no answer is said as that, never as an answer of 0 (a real machine with
    its road cut was told "GitHub answered 0")."""
    return "GitHub could not be reached" if not code else f"GitHub did not answer as expected ({code})"


def check_key(key, owner):
    """(kind, message): kind is 'user' or 'org' when the key reaches that owner's repositories, else None. No
    answer, or GitHub's own failure (5xx), is "could not be read", never "this key cannot see" (G5.4, G5.5)."""
    code, _, body = gh_get(key, "/user")
    if code == 401:
        return None, "GitHub refused this key (401). Make a new one; nothing was kept."
    if code != 200:
        return None, gh_unread(code) + "; nothing was kept. Run key set again in a few minutes."
    if (body or {}).get("login", "").lower() == owner.lower():
        kind, listing = "user", "/user/repos?affiliation=owner&per_page=1"
    else:
        code, _, _ = gh_get(key, f"/orgs/{urllib.parse.quote(owner)}")
        if not code or code >= 500:
            return None, (f"{gh_unread(code)} when asked about '{owner}'; nothing was kept. Run key set again in "
                          "a few minutes.")
        if code != 200:
            return None, (f"This key cannot see '{owner}' (GitHub answered {code}). A key belongs to one owner: "
                          "make one for this owner. Nothing was kept.")
        kind, listing = "org", f"/orgs/{urllib.parse.quote(owner)}/repos?per_page=1"
    code, _, _ = gh_get(key, listing)
    if not code or code >= 500:
        return None, (f"{gh_unread(code)} when asked for the repositories of '{owner}'; nothing was kept. Run key "
                      "set again in a few minutes.")
    if code != 200:
        return None, (f"This key cannot list the repositories of '{owner}' (GitHub answered {code}). Nothing was "
                      "kept. Check the key's resource owner and repository access on GitHub's key page, then run key "
                      "set again.")
    return kind, ""


def cmd_key_set(args):
    p = paths()
    owner = args.owner
    if not OWNER_RE.match(owner):
        say(f"'{owner}' is not a GitHub account or organisation name.")
        return 2
    if not load_json(p["state"], {}).get("port"):
        # G2.6: setup comes first, so every key lands where uninstall and the key scan already look. Nothing is read.
        # G10.7: it names the one next step and whose it is.
        say(f"Setup comes first, and it has not run on this computer: setup is your agent's step (or run setup "
            f"yourself), then run key set {owner} again. No key was read, and nothing was kept.")
        return 2
    key = secret(read_key_from_stdin())
    if not key or any(c.isspace() for c in key) or len(key) > 255:
        say(f"No key was read (or it was not one line). Nothing was kept. Run key set {owner} again in a terminal "
            "window of your own, and paste the key when it asks.")
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
KEPT_DAYS = 90
DEFAULTS = {"interval_min": 15, "full_pass_hours": 24, "git_timeout_s": 600, "api_floor": 500, "disk_floor_mb": 2048}


def setting(s, name):
    v = s.get(name, DEFAULTS[name])
    low = 1 if name in ("interval_min", "git_timeout_s", "full_pass_hours") else 0
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= low else DEFAULTS[name]
PRIVATE_AS_THIS_COMPUTER = ("The copy is as private as this computer. Anyone who can read this account's files can "
                            "read every project in it. Disk encryption, and who uses this computer, are yours.")
NOT_COPIED = "Not copied in 0.1: GitHub's pull-request refs, releases, issues and wikis."
FETCH_SPEC = ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
# What the copy holds now, read back from the forge before each push: the copy's branches and tags, and the kept refs.
FORGE_SPEC = ["+refs/heads/*:refs/forge/heads/*", "+refs/tags/*:refs/forge/tags/*", "+refs/heads/kept/*:refs/kept/*"]
NO_PUSH = "no-push://the-skill-never-writes-to-github"
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


def page_size():
    """G3.10: GitHub's largest page, 100. WSM_PAGE_SIZE (1 to 100) is read for tests and labs only: it makes the
    pages small, so that paging can be seen on a real owner with few repositories. Anything else is 100."""
    v = os.environ.get("WSM_PAGE_SIZE", "")
    return int(v) if v.isdigit() and 1 <= int(v) <= 100 else 100


def list_owner(owner, kind, key, floor=0, info=None):
    """Every repository of one owner, page by page to the last page. (repos, None), or (None, why) when any page
    failed: half a list is never acted on as if it were whole."""
    n = page_size()
    url = (f"/user/repos?affiliation=owner&per_page={n}" if kind == "user"
           else f"/orgs/{urllib.parse.quote(owner)}/repos?type=all&per_page={n}")
    repos, page, calls = [], 0, 0
    while url:
        page += 1
        code, headers, body = gh_get(key, url)
        calls += 1
        if info is not None and page == 1:
            info["expires"] = headers.get("github-authentication-token-expiration") or headers.get(
                "Github-Authentication-Token-Expiration") or ""
        until = spent_until(code, headers, floor if next_link(headers) else 0, info)
        if until and info is not None:
            info["spent_until"] = until
            return None, "GitHub's allowance is spent", calls
        if code == 401:
            return None, (f"GitHub refused the key for {owner} (401): the key stopped working (it may have expired, "
                          f"been narrowed or lost an organisation's approval). Make a new one and run key set {owner}"), calls
        if code != 200 or not isinstance(body, list):
            return None, (f"the listing of {owner} failed at page {page} ("
                          + (f"GitHub answered {code})" if code else "GitHub could not be reached)")), calls
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


class CouldNotRead(RuntimeError):
    """G5.4: a read that failed. It is a third answer, never taken for "not there"."""


class Forge:
    """The person's own forge, through its API, as the mirror account."""

    def __init__(self, p, st):
        self.base = f"http://127.0.0.1:{st['port']}"
        with open(p["mirror_key"], encoding="utf-8") as f:
            self.key = secret(f.read().strip())
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
        """The copy with this forge id; None only when the forge says it is not there (404); CouldNotRead when
        the read failed (G5.4)."""
        code, body = self.call("GET", f"/repositories/{rid}")
        if code == 404:
            return None
        if code != 200 or not isinstance(body, dict):
            raise CouldNotRead(f"the forge could not be read ({self.why(code, body)})")
        return body

    def by_name(self, name):
        """The copy under this name; None only when the forge says there is none: a 404, or a redirect left by a
        rename (the name now points elsewhere; redirects are never followed, contract C7)."""
        code, body = self.call("GET", f"/repos/{self.user}/{urllib.parse.quote(name)}")
        if code == 404 or code in (301, 302, 307, 308):
            return None
        if code != 200 or not isinstance(body, dict):
            raise CouldNotRead(f"the forge could not be read ({self.why(code, body)})")
        return body if body.get("name", "").lower() == name.lower() else None

    @staticmethod
    def why(code, body):
        # M53: no raw text from the library in a line for a person; no answer is said as that
        return f"it answered {code}" if code else "it did not answer"

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
                           "and the sync stopped. Nothing else was copied. Open an issue with this line; the "
                           "timer tries again at its next run, and stops the same way if the forge does the same.")
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


def twice(step):
    """Run one git step with its time limit; on a failure or a timeout, once more. None when the last try timed out."""
    out = None
    for _ in (1, 2):
        try:
            out = step()
        except subprocess.TimeoutExpired:
            out = None
        if out is not None and out.returncode == 0:
            return out
    return out


def refs_under(cache, *prefixes):
    out = git(cache, "for-each-ref", "--format=%(objectname) %(refname)", *prefixes)
    return {r: s for s, r in (line.split(" ", 1) for line in out.stdout.splitlines() if line.strip())}


def keep_before_moving(cache, report, full):
    """G5.1: before a branch or tag of the copy moves backwards, is rewritten or is deleted, keep its old commit
    under kept/<date>/<name> (a tag under kept/<date>/tags/<name>). Returns the new kept refs, or None on a failure."""
    now = refs_under(cache, "refs/heads", "refs/tags")
    held = refs_under(cache, "refs/forge/heads", "refs/forge/tags")
    day = time.strftime("%Y-%m-%d", time.gmtime())
    made = []
    for ref, old in sorted(held.items()):
        name = ref[len("refs/forge/"):]                       # heads/<b> or tags/<t>
        if name.startswith("heads/kept/"):
            continue
        new = now.get("refs/" + name)
        if new == old:
            continue
        if new and name.startswith("heads/") and git(cache, "merge-base", "--is-ancestor", old, new).returncode == 0:
            continue                                           # moved forward: nothing is lost
        short = name[len("heads/"):] if name.startswith("heads/") else name
        dest, n = f"refs/kept/{day}/{short}", 1
        while True:
            have = git(cache, "rev-parse", "--verify", "-q", dest).stdout.strip()
            if not have or have == old:
                break
            n += 1
            dest = f"refs/kept/{day}/{short}-{n}"
        if git(cache, "update-ref", dest, old).returncode != 0:
            return None
        what = "deleted" if not new else ("moved" if name.startswith("tags/") else "rewritten or moved back")
        report.append(f"{full}: {short} was {what} on GitHub; its old commit is kept as kept/{dest[len('refs/kept/'):]}")
        made.append(dest)
    return made


def drop_old_kept(cache, days):
    """G5.2: kept refs older than the kept period are dropped. With the period left at its default, a kept ref
    that is the only ref reaching its commit stays."""
    chosen = isinstance(days, (int, float)) and not isinstance(days, bool) and days >= 0
    limit = time.time() - (days if chosen else KEPT_DAYS) * 86400
    dropped = []
    for ref, sha in sorted(refs_under(cache, "refs/kept").items()):
        m = re.match(r"refs/kept/(\d{4}-\d{2}-\d{2})/", ref)
        if not m or calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d")) + 86400 > limit:
            continue
        if not chosen:
            others = git(cache, "for-each-ref", "--contains", sha, "--format=%(refname)", "refs/heads", "refs/tags",
                         "refs/kept").stdout.split()
            if not [r for r in others if r != ref]:
                continue                                       # the only way to its commit: it stays
        if git(cache, "update-ref", "-d", ref, sha).returncode == 0:
            dropped.append(ref)
    return dropped


def push_specs(cache):
    """What one atomic push sends: every branch and tag as GitHub has it, every kept ref, and a delete for each
    branch, tag or kept ref the copy holds that is gone here (each one already kept, or dropped by G5.2)."""
    now = refs_under(cache, "refs/heads", "refs/tags")
    kept = refs_under(cache, "refs/kept")
    held = refs_under(cache, "refs/forge/heads", "refs/forge/tags")
    specs = [f"+{r}:{r}" for r in sorted(now)] + [f"+{r}:refs/heads/kept/{r[len('refs/kept/'):]}" for r in sorted(kept)]
    for ref in sorted(held):
        there = "refs/" + ref[len("refs/forge/"):]
        if there.startswith("refs/heads/kept/"):
            if "refs/kept/" + there[len("refs/heads/kept/"):] not in kept:
                specs.append(":" + there)
        elif there not in now:
            specs.append(":" + there)
    return specs


def copy_one(p, forge, key, rec, gh, report, limit=GIT_TIMEOUT, kept_days=None):
    """Fetch one repository from GitHub (key in memory only) into its cache, then push it into the forge."""
    gid = str(gh["id"])
    full = gh["full_name"]
    cache = os.path.join(p["cache"], gid + ".git")
    if not os.path.exists(cache):
        os.makedirs(p["cache"], mode=0o700, exist_ok=True)
        if git(None, "init", "-q", "--bare", cache).returncode != 0:
            return "the local cache could not be made"
    git(cache, "config", "remote.github.url", f"{github_git()}/{full}.git")
    git(cache, "config", "remote.github.pushurl", NO_PUSH)       # G5.3: a push to GitHub cannot even start
    for i, spec in enumerate(FETCH_SPEC):
        git(cache, "config", "--replace-all" if i == 0 else "--add", "remote.github.fetch", spec)
    fetched_from = now_utc()
    out = twice(lambda: git(cache, "fetch", "--prune", "--quiet", "github", env=github_env(key), timeout=limit))
    if out is None:
        return f"the fetch from GitHub took longer than {limit} s, and so did the retry"
    if out.returncode != 0:
        return "the fetch from GitHub failed: " + last_line(out.stderr).replace(key, "<the key>")
    rec.update(seen_refs=refs_of(cache), seen_at=now_utc())      # G7.1: what GitHub showed, written down
    lfs = None
    if not refs_of(cache):
        pass                                   # an empty repository has no large files to fetch or push
    elif has_git_lfs():
        out = twice(lambda: git(cache, "lfs", "fetch", "--all", "github", env=github_env(key), timeout=limit))
        if out is None:
            return f"the large files took longer than {limit} s to fetch, and so did the retry"
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
    for i, spec in enumerate(FORGE_SPEC):
        git(cache, "config", "--replace-all" if i == 0 else "--add", "remote.forge.fetch", spec)
    out = twice(lambda: git(cache, "fetch", "--quiet", "--prune", "forge", env=forge.git_env(), timeout=limit))
    if out is None or out.returncode != 0:
        return "what the copy holds now could not be read from the forge" + (
            "" if out is None else ": " + last_line(out.stderr))
    kept = keep_before_moving(cache, report, full)
    if kept is None:
        return "an old commit could not be kept, so the copy was not moved"
    dropped = drop_old_kept(cache, kept_days)
    if lfs:
        out = twice(lambda: git(cache, "lfs", "push", "--all", "forge", env=forge.git_env(), timeout=limit))
        if out is None:
            return f"the large files took longer than {limit} s to push, and so did the retry"
        if out.returncode != 0:
            return "the large files could not be pushed into the forge: " + last_line(out.stderr)
    specs = push_specs(cache)
    if specs:
        out = twice(lambda: git(cache, "push", "--quiet", "--atomic", "forge", *specs, env=forge.git_env(),
                                timeout=limit))
        if out is None:
            return f"the push into the forge took longer than {limit} s, and so did the retry"
        if out.returncode != 0:
            return "the push into the forge failed: " + last_line(out.stderr)
    if dropped:
        report.append(f"{full}: {len(dropped)} kept ref(s) older than the kept period were dropped")
    rec.update(full_name=full, forge_name=cur["name"] if cur else copy_name(full), refs=refs_of(cache),
               last_good=now_utc(), fetched_from=fetched_from, pushed_at=gh.get("pushed_at"), problem="",
               lfs_missing=(lfs is False))
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


UNINSTALL_WAIT = 600     # seconds uninstall waits for a running sync to end (rule 8: a lock is a wait)


def lock_holder(p):
    """The process that holds the sync lock, as its number, or 'unknown'."""
    try:
        with open(os.path.join(p["data"], "sync.lock"), encoding="utf-8") as f:
            return int(f.read().strip() or 0) or "unknown"
    except (OSError, ValueError):
        return "unknown"


def take_lock(p):
    """(True, note) when this sync may run; (False, why) when another one is running."""
    path = os.path.join(p["data"], "sync.lock")
    os.makedirs(p["data"], mode=0o700, exist_ok=True)
    note = ""
    for _ in (1, 2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return True, note
        except FileExistsError:
            try:
                with open(path, encoding="utf-8") as f:
                    pid = int(f.read().strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and pid_alive(pid):
                return False, f"another sync is running (process {pid}); this one did not start"
            note = f"a lock left by a sync that is gone (process {pid or 'unknown'}) was taken over"
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
    return False, "the lock could not be taken"


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def drop_lock(p):
    path = os.path.join(p["data"], "sync.lock")
    try:
        with open(path, encoding="utf-8") as f:
            mine = f.read().strip() == str(os.getpid())
        if mine:
            os.remove(path)
    except OSError:
        pass


def cmd_sync(args):
    p = paths()
    ok, note = take_lock(p)
    if not ok:
        say(f"{note}. It ends by itself; wait for it, or run sync again when it has ended.")
        return 3
    try:
        if note:
            say(note)
        return sync(p, args)
    finally:
        drop_lock(p)


def save_state(p, st):
    write_file(p["state"], json.dumps(st, indent=1) + "\n", 0o600)


def local_time(stamp):
    """A recorded UTC time, said in this computer's own time zone, the way the person reads a clock."""
    t = epoch_of(stamp)
    return time.strftime("%a %Y-%m-%d %H:%M %Z", time.localtime(t)) if t else str(stamp)


def epoch_of(stamp):
    try:
        return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return 0



ANNOUNCE_NAMES = 20


def first_copy_names(picked):
    return sorted((r["full_name"] for _, _, r in picked.values()), key=str.lower)


KEY_PAGE = "https://github.com/settings/personal-access-tokens"


def key_end_epoch(exp):
    """G6.9: the key's end date as GitHub sent it, or None when there is none or it could not be read. The header
    has not been seen on a real key yet (contract C3): only a date that reads cleanly counts; nothing is worked out,
    and nothing absent is called "no end date" until GitHub's way of saying that has been seen."""
    try:
        return calendar.timegm(time.strptime(exp.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S")) if exp else None
    except ValueError:
        return None


def key_end_line(owner, exp):
    """G6.9: what the person hears about a key's end before the go: a date and the warning, or that no warning
    can be promised and where the date can be read."""
    if key_end_epoch(exp) is not None:
        return f"The key for {owner} ends {exp}; the mirror warns you two weeks before."
    return (f"The end date of the key for {owner} could not be read, so no warning before it ends can be promised; "
            f"its end date is on GitHub's key page, {KEY_PAGE}.")


def restart_line(st):
    """G10.6: what happens after a restart, read now and changing nothing, for the block the person says go to."""
    if system()[0] == "darwin":
        back, note = comes_back_by_itself()
        return "After a restart it starts by itself." if back else f"After a restart: {note}"
    if linger_state() == "yes":
        return f"After a restart it starts by itself: lingering is on for your account, so {LINGER_MEANS}."
    if st.get("linger_said") == "no":
        return ("After a restart it waits until you log in: lingering is off, as you chose, so the mirror syncs only "
                "while you are logged in. To change that, run setup --linger.")
    return (f"After a restart: lingering is off, so {NO_LOGIN_NOTE}. The computer's administrator can switch it on "
            f"with one command: {LINGER_COMMAND.format(user=getpass.getuser())} ({LINGER_MEANS}).")


def announce_first_copy(p, st, picked, complete):
    """G3.7, G3.9: what the person says yes to, named: how many, which (the first ANNOUNCE_NAMES by name, the whole
    list in a file of the person's own), how big, where they go, what is not copied, how private. It is kept, and the
    go copies exactly this list."""
    names = first_copy_names(picked)
    size = sum(int(r.get("size") or 0) for _, _, r in picked.values())
    write_file(p["first_copy_list"], "".join(n + "\n" for n in names), 0o600)
    more = len(names) - ANNOUNCE_NAMES
    tell([f"The first copy: {len(names)} repositories from {len(complete)} owner(s), "
          f"{size / 1024:.1f} MB as GitHub reports it (GitHub can show 0 for a new repository).",
          ("They are: " + ", ".join(names[:ANNOUNCE_NAMES])
           + (f", and {more} more; the whole list is in {p['first_copy_list']}." if more > 0 else ".")) if names
          else "There are none to copy yet.",
          f"The copies go into the forge on this computer only, http://127.0.0.1:{st['port']}/, as private "
          f"repositories of its '{MIRROR_USER}' account. Nothing is ever written to GitHub.",
          restart_line(st), lfs_missing() if not has_git_lfs() else "",
          *[key_end_line(o, (st.get("keys") or {}).get(o.lower(), {}).get("expires") or "") for o in sorted(complete, key=str.lower)],
          NOT_COPIED, PRIVATE_AS_THIS_COMPUTER])
    st["announced"] = st.get("announced") or now_utc()
    st["announced_list"] = names
    save_state(p, st)


def left_out_line(skipped):
    """G3.11: what the person left out is a count; its names come only when they ask (left-out). A plain sync put
    the names of every project left out into every agent's transcript."""
    inc = sum(1 for _, why in skipped if why == "not in include")
    parts = ([f"{inc} by your include list"] if inc else []) + (
        [f"{len(skipped) - inc} by your exclude patterns"] if len(skipped) > inc else [])
    return f"left out: {' and '.join(parts)}; their names are not shown here (to see them, run: left-out)."


def cmd_left_out(args):
    """G3.11: the names of what the include list and the exclude patterns leave out, asked for by the person. It
    reads GitHub's lists with the kept keys, and copies and changes nothing."""
    p = paths()
    st, s = load_json(p["state"], {}), load_json(p["settings"], {})
    owners = s.get("owners") or {}
    if not owners:
        say("No GitHub key is kept yet. Run: key set <your GitHub account> (and one per organisation).")
        return 2
    if epoch_of(st.get("github_wait_until")) > time.time():
        say(spent_line(st))
        return 0
    names, problems = [], []
    for owner, meta in owners.items():
        kp = key_path(p, owner)
        if not os.path.exists(kp):
            problems.append(f"{owner}: no key is kept for it; run key set {owner}")
            continue
        with open(kp, encoding="utf-8") as f:
            key = secret(f.read().strip())
        repos, why, _ = list_owner(owner, meta.get("kind", "user"), key, setting(s, "api_floor"), {})
        if repos is None:
            problems.append(f"{owner}: {why}")
            continue
        for r in repos:
            ok, why = chosen(r["full_name"], s)
            if not ok:
                names.append((r["full_name"], why))
    for full, why in sorted(names, key=lambda x: x[0].lower()):
        say(f"left out: {full} ({why})")
    for x in problems:
        say(f"problem: {x}")
    say(f"left-out: {len(names)} left out{' (the list is not whole: see the problem above)' if problems else ''}. "
        "Nothing was copied or changed.")
    return 1 if problems else 0


def spent_line(st):
    """G4.8: "spent" only when GitHub said so; under the skill's own floor, how much is left and the floor."""
    when, low = local_time(st["github_wait_until"]), st.get("github_wait_floor")
    head = (f"GitHub's allowance for your keys is down to {low[0]}, under the floor of {low[1]} this skill keeps, so "
            f"the sync waits until {when}" if low else
            f"GitHub's allowance for your keys is spent until {when}. The sync waits until then")
    return head + (" and asks GitHub nothing before it; every copy is as it was. Nothing to do: the timer starts the "
                   "next sync by itself.")


def sync(p, args):
    st = load_json(p["state"], {})
    s = load_json(p["settings"], {})
    owners = s.get("owners") or {}
    if getattr(args, "from_timer", False) and not st.get("go"):
        say("waiting for the person's go: nothing is copied until they say go and a sync is run on their word")
        return 0
    if not st.get("port") or not os.path.exists(p["mirror_key"]):
        say("Run setup first.")
        return 2
    wait = epoch_of(st.get("github_wait_until"))
    if wait > time.time():
        say(spent_line(st))
        return 0
    if not owners:
        say("No GitHub key is kept yet. Run: key set <your GitHub account> (and one per organisation).")
        return 2
    floor_mb = setting(s, "disk_floor_mb")
    free_mb = shutil.disk_usage(p["data"]).free // (1024 * 1024)
    if free_mb < floor_mb:
        why = f"only {free_mb} MB free on this disk, below the floor of {floor_mb} MB"
        st["last_sync"] = {"at": now_utc(), "calls": 0, "copied": 0, "problems": 1, "stopped": why}
        save_state(p, st)
        say(f"The sync did not start: {why}. Every copy is as it was. Free some space on this disk; the next sync "
            "starts by itself once there is enough.")
        return 1
    state = st.setdefault("repos", {})
    found, problems, report, calls = {}, [], [], 0
    complete = set()
    for owner, meta in owners.items():
        kp = key_path(p, owner)
        if not os.path.exists(kp):
            problems.append(f"{owner}: no key is kept for it")
            continue
        with open(kp, encoding="utf-8") as f:
            key = secret(f.read().strip())
        info = {}
        repos, why, n = list_owner(owner, meta.get("kind", "user"), key, setting(s, "api_floor"), info)
        if info.get("spent_until"):
            # G4.8: one line for the whole sync, never a failed project; nothing more is asked of GitHub until the
            # reset, by this process or any later one (the time is kept in the state, so it survives a restart).
            st["github_wait_until"] = stamp_of(max(info["spent_until"], epoch_of(st.get("github_wait_until"))))
            st["github_wait_floor"] = info.get("below_floor")
            st["last_sync"] = {"at": now_utc(), "calls": calls + n, "copied": 0, "problems": 0,
                               "stopped": "under the floor of GitHub's allowance this skill keeps"
                               if info.get("below_floor") else "GitHub's allowance is spent"}
            save_state(p, st)
            say(spent_line(st))
            return 0
        if repos is not None:
            st.setdefault("keys", {})[owner.lower()] = {"expires": info.get("expires", ""), "seen": now_utc()}
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
        (picked.__setitem__(gid, (owner, key, r)) if ok else skipped.append((r["full_name"], why)))
    if s.get("include") and not picked and found:
        say("The include list matches no repository GitHub shows; the sync stops rather than copy everything. Check "
            "the names (owner/name, as GitHub shows them) with config include, or clear the list with config "
            "include --clear.")
        return 2
    if args.announce:
        # G3.7: the announcement never copies, the first time or any time after; only a plain sync is the go.
        if st.get("go"):
            say(f"The first copy was made on {local_time(st['go'])}, on the person's go; there is nothing to "
                "announce, and nothing was copied now. The timer keeps the copies current.")
            return 0
        if len(complete) < len(owners):
            say("The first copy is not announced: the list of projects could not be read for "
                + "; ".join(problems) + ". Nothing was changed. Run sync --announce again when GitHub answers.")
            return 1
        announce_first_copy(p, st, picked, complete)
        say("Nothing was copied. When the person says go, run: sync")
        return 0
    if not st.get("announced"):
        say("Before the first copy, run: sync --announce (it says what will be copied, and how big it is).")
        return 2
    if not st.get("go"):
        # G3.9: the go covers exactly what the person was last told. If the list changed since (the include list,
        # a key for another owner, a project that appeared or went), nothing is copied: it is said again, and the
        # person's go is asked again. Only for the first copy; afterwards new projects join by themselves.
        names = first_copy_names(picked)
        if names != st.get("announced_list"):
            if len(complete) < len(owners):
                say("The first copy did not start: the list of projects could not be read for " + "; ".join(problems)
                    + ". Nothing was changed. Run sync again when GitHub answers.")
                return 1
            told = set(st.get("announced_list") or [])
            more, fewer = len([n for n in names if n not in told]), len([n for n in told if n not in names])
            say(f"Needs you: the first copy would not be what the person was told ({more} more and {fewer} fewer "
                "than the last announcement). Nothing was copied. It is said again here:")
            announce_first_copy(p, st, picked, complete)
            say("Give the person the block above; when they say go to this list, run: sync")
            return 2
    forge = Forge(p, st)
    # G6.10 (M48): a forge that does not answer is said by the sync itself, even when nothing changed on GitHub, so
    # a sync never reads as clean while no copy could be read or moved. Nothing is copied, changed or used up: the
    # go is recorded only by a sync that reached the forge.
    if forge.call("GET", "/version")[0] != 200:
        st["last_sync"] = {"at": now_utc(), "calls": calls, "copied": 0, "problems": 1,
                           "stopped": "the forge does not answer"}
        save_state(p, st)
        say(f"problem: the forge does not answer on 127.0.0.1:{st['port']}, so no copy was read, copied or changed. It "
            "is your own service on this computer: run status; if it still does not answer, run setup, which starts "
            "it again and keeps every copy.")
        return 1
    st["go"] = st.get("go") or now_utc()
    done, unchanged, started = 0, 0, time.time()
    full_due = time.time() - epoch_of(st.get("last_full_pass")) >= setting(s, "full_pass_hours") * 3600
    limit = setting(s, "git_timeout_s")
    order = sorted(picked.items(), key=lambda x: x[1][2]["full_name"].lower())
    lfs_here = has_git_lfs()
    for i, (gid, (owner, key, r)) in enumerate(order):
        if i == 3 and len(order) > 3 and not st.get("first_copy_done"):
            per = (time.time() - started) / 3
            say(f"first copy: 3 of {len(order)} done; about {max(1, round(per * (len(order) - 3) / 60))} min left at this pace")
        rec = state.setdefault(gid, {})
        if (not full_due and rec.get("forge_id") and not rec.get("problem") and rec.get("last_good")
                and not rec.get("copying_since") and not (rec.get("lfs_missing") and lfs_here)
                and rec.get("full_name") == r["full_name"] and r.get("pushed_at")
                and rec.get("pushed_at") == r["pushed_at"] and r["pushed_at"] < rec.get("fetched_from", "")):
            unchanged += 1
            continue
        rec.update(owner=owner, full_name=r["full_name"], gone_since="", copying_since=now_utc())
        save_state(p, st)
        try:
            why = copy_one(p, forge, key, rec, r, report, limit, s.get("kept_days"))
        except HardStop as e:
            rec["problem"] = "cannot refresh: the sync stopped"
            rec.pop("copying_since", None)
            st["last_sync"] = {"at": now_utc(), "calls": calls, "copied": done, "problems": 1, "stopped": str(e)}
            save_state(p, st)
            say(f"STOPPED: {e}")
            return 1
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            why = str(e)
        rec.pop("copying_since", None)
        if why:
            rec["problem"] = "cannot refresh: " + why
            problems.append(f"{r['full_name']}: {why}")
        else:
            done += 1
        save_state(p, st)
    for gid, rec in state.items():
        if gid not in found and rec.get("owner", "").lower() in complete and not rec.get("gone_since"):
            rec["gone_since"] = now_utc()
            report.append(f"{rec.get('full_name')}: gone from GitHub (or no longer visible to its key); its copy is kept")
    st["last_sync"] = {"at": now_utc(), "calls": calls, "copied": done, "unchanged": unchanged,
                       "problems": len(problems), "full_pass": full_due}
    if full_due and not problems:
        st["last_full_pass"] = now_utc()
    if not problems:
        st["first_copy_done"] = st.get("first_copy_done") or now_utc()
        st["last_good_sync"] = now_utc()
    save_state(p, st)
    for line in report + [f"problem: {x}" for x in problems]:
        say(line)
    if skipped:
        say(left_out_line(skipped))
    lfs_missing = sorted(rec["full_name"] for rec in state.values() if rec.get("lfs_missing") and not rec.get("gone_since"))
    for name in lfs_missing:
        say(f"problem: {name}: {LFS_NOT_IN_COPY}")
    say(f"sync: {len(found)} on GitHub, {done} copied now, {unchanged} unchanged, {len(problems)} problem(s), "
        f"{calls} GitHub API call(s)" + (", a full pass" if full_due else "")
        + (f", {len(lfs_missing)} cop{'y' if len(lfs_missing) == 1 else 'ies'} without its large files"
           if lfs_missing else ""))
    say(NOT_COPIED)
    if plugin_removed(st):
        say(PLUGIN_GONE.format(script=p["script"]))
    return 0 if not problems else 1


# ---------------------------------------------------------------- Claude Code's own uninstall (rule 8)

PLUGIN = "windstorm-mirror-for-github"
PLUGIN_GONE = ("Claude Code's plugin for this skill was removed; the mirror goes on as your own service on this "
               "computer and keeps every copy current. To take it off: python3 -I {script} uninstall")


def plugin_registry_for(here):
    """Claude Code keeps an installed plugin in <its folder>/plugins/cache/<catalog>/<plugin>/<version>/, and its
    list of installed plugins in <its folder>/plugins/installed_plugins.json. None when `here` is not in such a
    folder (a clone, or the copy setup made)."""
    parts = os.path.abspath(here).split(os.sep)
    for i in range(len(parts) - 3, 0, -1):
        if parts[i] == "cache" and parts[i - 1] == "plugins" and parts[i + 2] == PLUGIN:
            return os.sep.join(parts[:i] + ["installed_plugins.json"])
    return None


def plugin_removed(st):
    """G9.6: True only when Claude Code's list of installed plugins, the one setup was run from, was READ and names
    this plugin nowhere. A list that could not be read is never taken for 'removed' (a failed read is not a no)."""
    reg = st.get("plugin_registry")
    if not reg:
        return False
    try:
        with open(reg, encoding="utf-8") as f:
            rows = json.load(f)["plugins"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(rows, dict):
        return False
    return not any(k.split("@")[0] == PLUGIN and v for k, v in rows.items())


def set_aside_folder(here):
    """G9.6: the folder Claude Code marked `.orphaned_at` (the plugin was removed, or updated to a newer version)
    when `here` is inside one; None otherwise. Nothing runs from such a folder."""
    d = os.path.dirname(os.path.abspath(here))
    for _ in range(2):
        if os.path.exists(os.path.join(d, ".orphaned_at")):
            return d
        d = os.path.dirname(d)
    return None


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
    if getattr(args, "from_file", None):
        return setup_from(p, args)
    have = load_json(p["state"], {}).get("gitea")
    if have and have != GITEA_VERSION:
        say(f"This forge runs Gitea {have}, and this release of the skill pins {GITEA_VERSION}. Setup does not move "
            "the forge: run upgrade-forge, which makes a backup first. Nothing was changed.")
        return 2
    st = load_json(p["state"], {})
    allow = not getattr(args, "no_linger", False)
    if not shutil.which("git"):
        say(git_missing_sentence() + " Nothing was changed.")
        return 2
    far = git_out_of_service_reach()
    if far:
        say(far)
        return 2
    plan = setup_plan(p, st)
    if args.dry_run:
        back, note = (comes_back_by_itself(dry=True) if system()[0] == "darwin"
                      else linger_step(None, dry=True, allow=allow))
        say("setup --dry-run: nothing is changed.")
        tell([f"Setup would {step}." for step in plan]
             + [f"After a restart: {note}" if note else "After a restart it starts by itself.",
                lfs_missing() if not has_git_lfs() else ""])
        return 0
    if linger_unchosen(args, st):
        tell([f"After a restart: {linger_step(None, dry=True)[1]}"])
        say(LINGER_CHOICE)
        return 2
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
        raise SystemExit(f"Gitea could not prepare its database: {last_line(out.stderr)}. Run setup again; if it "
                         "stops here again, Gitea's words above are the reason.")
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
            raise SystemExit("Gitea did not give the mirror account its key; nothing was printed. Run setup again.")
        write_private(p["mirror_key"], out.stdout.strip() + "\n")
        st["mirror_user"] = MIRROR_USER
        say(f"The '{MIRROR_USER}' account that owns the copies is made; its key is in {p['mirror_key']}.")
    here = os.path.abspath(__file__)
    if os.path.abspath(p["script"]) != here:
        shutil.copyfile(here, p["script"] + ".tmp")
        os.replace(p["script"] + ".tmp", p["script"])
    if plugin_registry_for(here):
        st["plugin_registry"] = plugin_registry_for(here)
    st.update(version=VERSION, gitea=GITEA_VERSION)
    save_state(p, st)
    ok, why = install_service(p)
    if ok and not install_timer(p, int(setting(load_json(p["settings"], {}), "interval_min"))):
        ok, why = False, "the sync timer could not be switched on"
    if not ok:
        say(f"The forge's service did not start: {why}")
    elif wait_forge(st["port"], seconds=SETUP_WAIT):
        say(f"The forge runs as your own service on http://127.0.0.1:{st['port']}/ (this computer only).")
    else:
        say(not_answering_yet(p, st["port"], SETUP_WAIT))
    if system()[0] == "darwin":
        back, note = comes_back_by_itself()
    else:
        back, note = linger_step(st, allow=allow)
        if not allow:
            st["linger_said"] = "no"
        elif getattr(args, "linger", False):
            st.pop("linger_said", None)
        save_state(p, st)
    # G10.6: no block here. An agent goes straight on after setup and its block was lost in a cold start; what the
    # person must hear before the go is in the announcement's block, the one they say go to.
    for line in ("After a restart it starts by itself." if back else "", note, lfs_missing() if not has_git_lfs() else "",
                 PRIVATE_AS_THIS_COMPUTER):
        if line:
            say(line)
    return 0 if ok else 1


SKILL_REPO = "TheWindstorm/windstorm-mirror-for-github"
KEY_SHAPE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")


def copy_state(rec, forge_refs, max_age):
    """(current, why): G6.1 and G6.2. A copy is current only when its last good refresh is recent, nothing
    stopped it, and the forge holds exactly the branches and tags GitHub last showed. There is no pardon."""
    if rec.get("copying_since"):
        return False, f"a copy was stopped part-way at {rec['copying_since']}; the next sync does it again"
    if rec.get("problem"):
        return False, rec["problem"]
    if not rec.get("last_good"):
        return False, "never copied yet"
    if time.time() - epoch_of(rec["last_good"]) > max_age:
        return False, f"last good refresh {rec['last_good']}, older than twice the sync interval"
    held = {r: s for r, s in (forge_refs or {}).items() if not r.startswith("refs/heads/kept/")}
    if held != (rec.get("refs") or {}):
        return False, "the copy's branches and tags differ from what GitHub last showed"
    return True, ""


def forge_refs_of(forge, name):
    out = git(None, "ls-remote", f"{forge.base}/{forge.user}/{name}.git", env=forge.git_env(), timeout=60)
    if out.returncode != 0:
        return None
    return {r: s for s, r in (line.split("\t", 1) for line in out.stdout.splitlines() if "\t" in line)
            if r.startswith(("refs/heads/", "refs/tags/")) and not r.endswith("^{}")}


def key_scan(p, st):
    """G6.3: every repository config in the forge and the skill's own files, for anything shaped like a key or
    equal to one the skill holds. Returns (files read, [paths where one was found]). The key files are skipped."""
    known = set(SECRETS)
    for path in [p["mirror_key"]] + [os.path.join(p["keys"], f) for f in (os.listdir(p["keys"]) if os.path.isdir(p["keys"]) else [])]:
        try:
            with open(path, encoding="utf-8") as f:
                known.add(secret(f.read().strip()))
        except OSError:
            pass
    known.discard("")
    skip = {os.path.realpath(p["mirror_key"]), os.path.realpath(p["password"])}
    if os.path.isdir(p["keys"]):
        skip |= {os.path.realpath(os.path.join(p["keys"], f)) for f in os.listdir(p["keys"])}
    files = []
    for root in (p["conf"], p["data"]):
        for d, dirs, names in os.walk(root):
            rel = os.path.relpath(d, p["data"])
            dirs[:] = [x for x in dirs if x not in ("objects", "lfs", "refs", "hooks", "logs", "info", "queues",
                                                    "indexers", "sessions", "avatars", "repo-avatars", "attachments",
                                                    "packages", "tmp", "bin")]
            for n in names:
                f = os.path.join(d, n)
                if os.path.realpath(f) in skip or n.endswith((".pack", ".idx", ".rev", ".db", ".db-journal")):
                    continue
                if d.endswith(".git") and n not in ("config", "description", "FETCH_HEAD", "packed-refs"):
                    continue
                files.append(f)
    found = []
    for f in files:
        try:
            with open(f, "rb") as fh:
                text = fh.read(2_000_000).decode("utf-8", "replace")
        except OSError:
            continue
        if KEY_SHAPE.search(text) or any(k in text for k in known):
            found.append(f)
    return len(files), found


def vt(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def gh_public(path, accept="application/vnd.github+json"):
    """One read of GitHub's API with no key at all (the skill's own releases). Returns (status, bytes)."""
    req = urllib.request.Request(github_api() + path, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, OSError):
        return 0, b""


def release_check(st, force=False):
    """G6.6: once a day, one call with no key for this skill's latest release. Returns lines to show."""
    rc = st.get("release_check") or {}
    due = force or time.time() - epoch_of(rc.get("at")) >= 86400
    if due and not force and epoch_of(st.get("github_wait_until")) > time.time():
        # G4.8 (M25): while GitHub's allowance waits for its reset, nothing at all is asked of GitHub, not even this
        # read with no key (a real machine saw status make it during the wait).
        return ["newer release: not asked while the sync waits for GitHub's reset; it is asked after it"]
    if due:
        try:
            code, raw = gh_public(f"/repos/{SKILL_REPO}/releases/latest")
            if code != 200:
                raise urllib.error.HTTPError("", code or 0, "no answer", None, None)
            body = json.loads(raw or b"null") or {}
            m = re.search(r"Pins Gitea (\d+\.\d+\.\d+)([^\n]*)", body.get("body") or "")
            rc = {"at": now_utc(), "tag": body.get("tag_name", ""), "gitea": m.group(1) if m else "",
                  "security": bool(m and "security fix" in m.group(2).lower())}
        except (urllib.error.URLError, OSError, ValueError) as e:
            rc = {"at": now_utc(), "error": getattr(e, "code", None) or "no answer"}
        st["release_check"] = rc
    if rc.get("error"):
        return [f"newer release: not known (GitHub gave {rc['error']}; it is asked again tomorrow)"]
    lines = []
    if rc.get("tag") and vt(rc["tag"]) > vt(VERSION):
        lines.append(f"newer release of this skill: {rc['tag']} (this is {VERSION}); `self-update` is your choice")
    if rc.get("gitea") and vt(rc["gitea"]) > vt(GITEA_VERSION):
        lines.append(f"that release pins Gitea {rc['gitea']} (this forge runs {GITEA_VERSION})"
                     + (", and names it a SECURITY FIX" if rc.get("security") else "")
                     + "; `self-update` then `upgrade-forge` is your choice. Nothing was changed.")
    return lines or [f"newer release: none (latest {rc.get('tag') or 'none'})"]


def own_addresses():
    """Every address of this computer's own network interfaces, as the system lists them (no other host is asked)."""
    found = set()
    for cmd in (["ip", "-o", "addr", "show"], ["ifconfig", "-a"]):
        try:
            out = run(cmd, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0:
            found |= set(re.findall(r"\binet6?\s+(?:addr:)?([0-9a-fA-F:.]+[0-9a-fA-F])", out.stdout))
            break
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(info[4][0])
    except OSError:
        pass
    return sorted(a.split("%")[0] for a in found if a)


def answers_on(addresses, port):
    """The addresses, other than this computer's loopback, on which something answers at this port."""
    open_on = []
    for a in addresses:
        if a == "::1" or a.startswith("127."):
            continue
        fam = socket.AF_INET6 if ":" in a else socket.AF_INET
        if fam == socket.AF_INET6 and a.lower().startswith("fe80"):
            continue                               # link-local needs a scope to be tried
        try:
            with socket.socket(fam, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((a, int(port))) == 0:
                    open_on.append(a)
        except OSError:
            pass
    return open_on


EXPOSE_TEXT = """The forge listens on 127.0.0.1 only: no other computer can reach it, and this skill will not change that.

Opening it to other computers is your own choice and your own step. What it would mean:
- Anyone who can reach it can try to sign in. Its accounts and every copy are then guarded by passwords and keys
  alone, and a forge open to the internet is found and tried within hours.
- Gitea's updates become urgent: a security fix matters the day it is out (`status` says when one is pinned).
- The usual roads are a private network of your own devices (a VPN), or a reverse proxy with TLS in front of
  it. Both are outside this skill, and `status` reports the forge as reachable from outside each time it runs.
Nothing was changed."""


def cmd_expose(args):
    """G8.2: the skill does not open the forge; it says what opening it would mean."""
    say(EXPOSE_TEXT)
    return 0


def cmd_status(args):
    """G6: one line per problem, then the count of copies, then the forge, the keys, the disk and the syncs."""
    p = paths()
    st = load_json(p["state"], {})
    s = load_json(p["settings"], {})
    if not st.get("port") or not os.path.exists(p["mirror_key"]):
        say("Run setup first.")
        return 2
    forge = Forge(p, st)
    problems, notes = [], []
    if not shutil.which("git"):
        problems.append("git is not installed on this computer, so no copy can refresh until it is")
    code, ver = forge.call("GET", "/version")
    up = code == 200
    if not up:
        problems.append(f"the forge does not answer on 127.0.0.1:{st['port']}")
    elif (ver or {}).get("version") != GITEA_VERSION:
        problems.append(f"the forge runs Gitea {(ver or {}).get('version')}, and this release pins {GITEA_VERSION}"
                        + ("; `upgrade-forge` moves it, with a backup first" if vt((ver or {}).get("version")) < vt(GITEA_VERSION) else ""))
    max_age = 2 * setting(s, "interval_min") * 60
    on_gh = copied = behind = unreadable = no_lfs = 0
    for gid, rec in sorted((st.get("repos") or {}).items(), key=lambda x: (x[1].get("full_name") or "").lower()):
        name = rec.get("full_name") or gid
        if rec.get("gone_since"):
            notes.append(f"note: {name} is gone from GitHub since {rec['gone_since']}; its copy is kept")
            continue
        on_gh += 1
        if rec.get("last_good"):
            copied += 1
        try:
            cur = forge.by_id(rec["forge_id"]) if up and rec.get("forge_id") else None
        except CouldNotRead:
            cur = None
        refs = forge_refs_of(forge, cur["name"]) if cur else None
        if rec.get("last_good") and refs is None:
            unreadable += 1
            problems.append(f"{name}: its copy could not be read from the forge")
            continue
        ok, why = copy_state(rec, refs, max_age)
        if not ok:
            behind += 1
            problems.append(f"{name}: {why}")
        if rec.get("lfs_missing"):
            no_lfs += 1
            problems.append(f"{name}: {LFS_NOT_IN_COPY}")
    for owner, k in sorted((st.get("keys") or {}).items()):
        exp = k.get("expires") or ""
        when = key_end_epoch(exp)
        if when is None:
            notes.append(f"key for {owner}: expiry not known" + (f" (GitHub sent '{exp[:40]}', which could not be read)"
                                                                if exp else "") + "; no warning before it ends can be promised")
            continue
        left = (when - time.time()) / 86400
        if left < 14:
            problems.append(f"the key for {owner} expires {exp}" + (" (it has expired)" if left < 0 else
                            f", in {int(left)} day(s): make a new one, then `key set {owner}`"))
        notes.append(f"key for {owner}: expires {exp}")
    free_mb = shutil.disk_usage(p["data"]).free // (1024 * 1024)
    floor_mb = setting(s, "disk_floor_mb")
    if free_mb < floor_mb:
        problems.append(f"only {free_mb} MB free on this disk, below the floor of {floor_mb} MB: syncs do not start")
    addrs = own_addresses()
    outside = answers_on(addrs, st["port"])
    for a in outside:
        problems.append(f"the forge answers on {a}, not only on 127.0.0.1: other computers may reach it")
    when, tproblem = next_sync()
    if tproblem:
        problems.append(tproblem)
    n, found = key_scan(p, st)
    if n == 0:
        problems.append("the key scan read no files, so it proves nothing")
    for f in found:
        problems.append(f"something shaped like a key, or a key itself, is in {f}")
    rel = release_check(st, force=getattr(args, "check_release", False))
    save_state(p, st)
    if epoch_of(st.get("github_wait_until")) > time.time():
        notes.append(f"github: {spent_line(st)}")
    if plugin_removed(st):
        notes.append("plugin: " + PLUGIN_GONE.format(script=p["script"]))
    for x in problems:
        say(f"problem: {x}")
    say(f"copies: {on_gh} on GitHub, {copied} copied, {behind} behind, {unreadable} unreadable"
        + (f", {no_lfs} without its large files" if no_lfs else ""))
    say(f"first copy: made on {local_time(st['go'])}, on your go" if st.get("go") else
        "first copy: waiting for your go (sync --announce says what will be copied; a sync on your word starts it)")
    for x in notes:
        say(x)
    say(f"forge: Gitea {(ver or {}).get('version') if up else '(not answering)'} on http://127.0.0.1:{st['port']}/ "
        f"(pinned {GITEA_VERSION})")
    say(f"disk: {free_mb} MB free (floor {floor_mb} MB)")
    others = [a for a in addrs if not (a == "::1" or a.startswith("127."))]
    say(f"network: the forge answers on 127.0.0.1 only (tried {len(others)} other address(es) of this computer)"
        if not outside else f"network: the forge answers on {', '.join(outside)} too")
    last = st.get("last_sync") or {}
    say(f"last good sync: {st.get('last_good_sync') or 'none yet'}")
    if last:
        say(f"last sync: {last.get('at')}, {last.get('copied', 0)} copied, {last.get('unchanged', 0)} unchanged, "
            f"{last.get('problems', 0)} problem(s), {last.get('calls', 0)} GitHub API call(s)"
            + (f"; stopped: {last['stopped']}" if last.get("stopped") else ""))
    say(f"next sync: {when}" if not tproblem else "next sync: none (see the problem above)")
    say(f"key scan: {n} file(s) read, " + (f"{len(found)} with a key in them" if found else "no key found"))
    for x in rel:
        say(x)
    return 1 if problems else 0


# ---------------------------------------------------------------- G7 restore, and the backup

ONLY_THIS_COMPUTER = {"http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
                      "all_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
                      "HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9",
                      "no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1,localhost"}


def scratch(p, what):
    os.makedirs(p["data"], mode=0o700, exist_ok=True)
    return tempfile.mkdtemp(prefix=f"{what}-", dir=p["data"])


def clone_copy(forge, name, into):
    """A bare clone of one copy from the forge, with every host but this computer shut off (G7.2)."""
    env = dict(forge.git_env(), **ONLY_THIS_COMPUTER)
    out = git(None, "clone", "--quiet", "--bare", f"{forge.base}/{forge.user}/{name}.git", into, env=env)
    if out.returncode != 0:
        return "the copy could not be cloned from the forge: " + last_line(out.stderr)
    if uses_lfs(into):
        if not has_git_lfs():
            return "it holds large files, and git lfs is not installed here to check them"
        out = git(into, "lfs", "fetch", "--all", "origin", env=env)
        if out.returncode != 0:
            return "its large files could not be fetched from the forge: " + last_line(out.stderr)
        out = git(into, "lfs", "fsck", env=env)
        if out.returncode != 0:
            return "its large files are damaged: " + last_line(out.stdout + out.stderr)
    return ""


def cmd_restore(args):
    p = paths()
    st = load_json(p["state"], {})
    if not st.get("port") or not os.path.exists(p["mirror_key"]):
        say("Run setup first.")
        return 2
    forge = Forge(p, st)
    if args.to:
        return restore_to(p, st, forge, args.to, args.repository)
    if not args.check:
        say("Say what to do: restore --check, or restore --to <url> <owner/name>.")
        return 2
    # G7.5 (M53): a forge that does not answer is said as that, with its next step; it is not a finding about any
    # copy (a real machine was told "1 difference(s)" and a raw connection error, with no step).
    if forge.call("GET", "/version")[0] != 200:
        say(f"problem: the forge does not answer on 127.0.0.1:{st['port']}, so no copy could be checked; this says "
            "nothing about the copies themselves. It is your own service on this computer: run status; if it still "
            "does not answer, run setup, which starts it again and keeps every copy, then run restore --check again.")
        return 1
    work = scratch(p, "restore-check")
    diffs, unread, whole, total = [], [], 0, 0
    try:
        for gid, rec in sorted((st.get("repos") or {}).items(), key=lambda x: (x[1].get("full_name") or "").lower()):
            if not rec.get("forge_id"):
                continue
            total += 1
            name = rec.get("full_name") or gid
            try:
                cur = forge.by_id(rec["forge_id"])
            except CouldNotRead as e:
                unread.append(f"{name}: its copy could not be read: {e}")   # rule 6: not a difference
                continue
            if not cur:
                diffs.append(f"{name}: its copy is not in the forge")
                continue
            if rec.get("lfs_missing"):
                diffs.append(f"{name}: {LFS_NOT_IN_COPY}")
                continue
            into = os.path.join(work, gid + ".git")
            why = clone_copy(forge, cur["name"], into)
            if why:
                diffs.append(f"{name}: {why}")
                continue
            mine = 0
            have = {r: s for r, s in refs_under(into, "refs/heads", "refs/tags").items() if not r.startswith("refs/heads/kept/")}
            seen = rec.get("seen_refs") or rec.get("refs") or {}
            for ref in sorted(set(have) | set(seen)):
                if have.get(ref) != seen.get(ref):
                    mine += 1
                    diffs.append(f"{name}: {ref} is {(have.get(ref) or 'missing')[:12]} in the copy, "
                                 f"{(seen.get(ref) or 'missing')[:12]} when GitHub was last seen")
            out = git(into, "fsck", "--no-progress", "--no-dangling")
            if out.returncode != 0:
                mine += 1
                diffs.append(f"{name}: git fsck found damage: {last_line(out.stdout + out.stderr)}")
            if not mine:
                whole += 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for d in diffs + unread:
        say(d)
    say(f"restore check: {total} copies, {whole} whole, {len(diffs)} difference(s), {len(unread)} could not be read; "
        "nothing outside this computer was asked" + ("; run restore --check again when the forge answers" if unread else ""))
    return 0 if not diffs and not unread and total else 1


def github_hosts():
    hosts = {"github.com"}
    host = urllib.parse.urlsplit(github_git()).hostname
    if host:
        hosts.add(host.lower())
    return hosts


def restore_to(p, st, forge, url, repository):
    """G7.3: one copy, every branch and tag, into a new home the person names; never GitHub (INV1)."""
    if not repository:
        say("Name the repository: restore --to <url> <owner/name>.")
        return 2
    low = url.lower()
    host = (urllib.parse.urlsplit(url).hostname or re.sub(r"^[^@]*@([^:/]+).*$", r"\1", url)).lower()
    if any(h == host or host.endswith("." + h) for h in github_hosts()) or "github.com" in low:
        say("This skill never writes to GitHub, so it will not restore there. To put this project back on GitHub "
            "yourself, clone it from your forge and push it with your own login:")
        say(f"  git clone --mirror http://127.0.0.1:{st['port']}/{forge.user}/{copy_name(repository)}.git")
        say("  cd <that folder> and run: git push --mirror <the new, empty GitHub repository>")
        return 2
    rec = next((r for r in (st.get("repos") or {}).values()
                if (r.get("full_name") or "").lower() == repository.lower()
                or (r.get("forge_name") or "").lower() == repository.lower()), None)
    try:
        cur = forge.by_id(rec["forge_id"]) if rec and rec.get("forge_id") else None
    except CouldNotRead as e:
        say(f"The copy of {repository} could not be read: {e}. Nothing was pushed; try again when the forge answers.")
        return 1
    if not cur:
        say(f"There is no copy of {repository} in the forge. Run status to see the copies it holds, and name one "
            "of them as owner/name.")
        return 2
    # The person's own git login for the new home; the skill's keys are not used there.
    out = run(["git", "ls-remote", url], env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), timeout=120)
    if out.returncode != 0:
        say(f"The new home could not be reached: {last_line(out.stderr)}. Nothing was pushed. Check the address, "
            "and that your own git login reaches it (git ls-remote <the address>), then run restore --to again.")
        return 1
    if out.stdout.strip():
        say("The new home is not empty, so nothing was pushed. Make a new, empty repository and name that.")
        return 1
    work = scratch(p, "restore-to")
    try:
        into = os.path.join(work, "copy.git")
        why = clone_copy(forge, cur["name"], into)
        if why:
            say(f"{repository}: {why}. Nothing was pushed. Run restore --check to see what the copy needs.")
            return 1
        mine = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        run(["git", "--git-dir", into, "remote", "add", "home", url], env=mine)
        if uses_lfs(into) and has_git_lfs():
            # Large files first, to the new home by its own name (git lfs would otherwise ask the forge).
            out = run(["git", "--git-dir", into, "lfs", "push", "--all", "home"], env=mine, timeout=3600)
            if out.returncode != 0:
                say("The large files could not be put in the new home, so nothing else was pushed: "
                    + " / ".join(out.stderr.strip().splitlines()[-3:])
                    + ". Check that the new home takes large files, then run restore --to again.")
                return 1
        specs = ["refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*", "^refs/heads/kept/*"]
        out = run(["git", "--git-dir", into, "-c", "core.hooksPath=/dev/null", "push", "--quiet", "--atomic", "home",
                   *specs], env=mine, timeout=3600)
        if out.returncode != 0:
            say("The push to the new home failed: " + " / ".join(out.stderr.strip().splitlines()[-4:])
                + ". Run restore --to again into a new, empty repository; if it fails the same way, git's words are "
                  "the reason.")
            return 1
        sent = {r: s for r, s in refs_under(into, "refs/heads", "refs/tags").items() if not r.startswith("refs/heads/kept/")}
        back = run(["git", "ls-remote", url], env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), timeout=120)
        there = {r: s for s, r in (l.split("\t", 1) for l in back.stdout.splitlines() if "\t" in l)
                 if r.startswith(("refs/heads/", "refs/tags/")) and not r.endswith("^{}")}
        if there != sent:
            say("The new home does not hold what was sent; do not rely on it. Run restore --to again into a new, "
                "empty repository.")
            return 1
        heads = sum(1 for r in sent if r.startswith("refs/heads/"))
        say(f"restored {repository}: {heads} branch(es) and {len(sent) - heads} tag(s) to {url}; "
            "the kept refs stay in your forge")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def service(p, verb):
    """Stop or start the forge's own service."""
    if system()[0] == "darwin":
        uid = str(os.getuid())
        cmd = (["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"] if verb == "stop"
               else ["launchctl", "bootstrap", f"gui/{uid}", p["plist"]])
    else:
        cmd = ["systemctl", "--user", verb, SERVICE]
    return run(cmd).returncode == 0


def port_answers(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
            return True
    except OSError:
        return False


def safe_extract(archive, into):
    with tarfile.open(archive) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(into, filter="data")
            return
        root = os.path.realpath(into)
        for m in tf.getmembers():
            dest = os.path.realpath(os.path.join(into, m.name))
            if not dest.startswith(root + os.sep) or m.issym() or m.islnk() or m.isdev():
                raise SystemExit(f"The backup holds an entry that points outside it ({m.name}); nothing was restored. "
                                 "Name a backup made by this skill's own backup command.")
        tf.extractall(into)


def cmd_backup(args):
    """G7.4: the forge's own dump, plus the skill's state and settings (never its keys), in one file only the
    person can read."""
    p = paths()
    st = load_json(p["state"], {})
    if not st.get("port"):
        say("Run setup first.")
        return 2
    lock_ok, why = take_lock(p)
    if not lock_ok:
        say(f"No backup was made: {why}. Run backup again when it has ended.")
        return 3
    try:
        return make_backup(p, st, args.folder)
    finally:
        drop_lock(p)


def make_backup(p, st, folder):
    folder = os.path.abspath(os.path.expanduser(folder))
    os.makedirs(folder, mode=0o700, exist_ok=True)
    work = scratch(p, "backup")
    try:
        service(p, "stop")
        for _ in range(60):
            if not port_answers(st["port"]):
                break
            time.sleep(0.5)
        else:
            say("The forge did not stop, so no backup was made (a backup of a running forge can be torn). Run "
                "backup again; if the forge still does not stop, run status.")
            return 1
        dump = os.path.join(work, "gitea-dump.tar.gz")
        out = gitea(p, "dump", "--type", "tar.gz", "--file", dump, "--tempdir", work)
        if out.returncode != 0 or not os.path.exists(dump):
            say(f"Gitea's dump failed: {last_line(out.stderr)}. No backup was made. Run backup again; if it fails "
                "the same way, Gitea's words are the reason.")
            return 1
    finally:
        started = service(p, "start")
    try:
        skill = os.path.join(work, "skill")
        os.makedirs(skill)
        shutil.copyfile(p["state"], os.path.join(skill, "state.json"))
        if os.path.exists(p["settings"]):
            shutil.copyfile(p["settings"], os.path.join(skill, "settings.json"))
        write_file(os.path.join(skill, "VERSION"), VERSION + "\n")
        name = os.path.join(folder, f"windstorm-mirror-backup-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.tar.gz")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w:gz") as tf:
            tf.add(dump, arcname="gitea-dump.tar.gz")
            tf.add(skill, arcname="skill")
        os.chmod(name, 0o600)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    n = sum(1 for r in (st.get("repos") or {}).values() if r.get("forge_id"))
    say(f"backup: {name} ({os.path.getsize(name) // 1024} KB, {n} copies), readable by you only.")
    say("It holds the forge's own secrets, so keep it as private as the forge. Your GitHub keys are not in it.")
    if not started:
        say("problem: the forge's service did not start again after the backup; run setup to start it")
        return 1
    st["last_backup"] = name
    return 0


def setup_from(p, args):
    """G7.4: a new computer with only the backup file and the skill gets back every copy."""
    if load_json(p["state"], {}).get("port"):
        say("This computer already has a forge of this skill; setup --from is for a new one. Nothing was changed. "
            "Run setup --from on the new computer; this one keeps its own forge.")
        return 2
    if linger_unchosen(args, {}):
        tell([f"After a restart: {linger_step(None, dry=True)[1]}"])
        say(LINGER_CHOICE)
        return 2
    src = os.path.abspath(os.path.expanduser(args.from_file))
    os.makedirs(p["data"], mode=0o700, exist_ok=True)
    os.makedirs(p["conf"], mode=0o700, exist_ok=True)
    fetch_forge(p, os.environ.get("WSM_GITEA_BASE"))
    work = scratch(p, "setup-from")
    try:
        safe_extract(src, work)
        old_st = load_json(os.path.join(work, "skill", "state.json"), None)
        if not old_st or not os.path.exists(os.path.join(work, "gitea-dump.tar.gz")):
            say("That file is not a backup of this skill. Nothing was changed. Name the file this skill's own "
                "backup command made.")
            return 2
        dump = os.path.join(work, "dump")
        os.makedirs(dump)
        safe_extract(os.path.join(work, "gitea-dump.tar.gz"), dump)
        f = p["forge"]
        os.makedirs(f, mode=0o700, exist_ok=True)
        if os.path.isdir(os.path.join(dump, "custom")):
            shutil.copytree(os.path.join(dump, "custom"), os.path.join(f, "custom"), dirs_exist_ok=True)
        shutil.copytree(os.path.join(dump, "data"), os.path.join(f, "data"), dirs_exist_ok=True)
        if os.path.isdir(os.path.join(dump, "repos")):
            shutil.copytree(os.path.join(dump, "repos"), os.path.join(f, "data", "gitea-repositories"), dirs_exist_ok=True)
        os.makedirs(os.path.join(f, "log"), exist_ok=True)
        with open(os.path.join(dump, "app.ini"), encoding="utf-8") as fh:
            ini = fh.read()
        old = (re.search(r"^WORK_PATH\s*=\s*(.+)$", ini, re.M) or [None, ""])[1].strip()
        port = free_port()
        if old:
            ini = ini.replace(old, f)
        ini = re.sub(r"^HTTP_PORT\s*=.*$", f"HTTP_PORT = {port}", ini, flags=re.M)
        ini = re.sub(r"^ROOT_URL\s*=.*$", f"ROOT_URL = http://127.0.0.1:{port}/", ini, flags=re.M)
        ini = re.sub(r"^HTTP_ADDR\s*=.*$", "HTTP_ADDR = 127.0.0.1", ini, flags=re.M)
        os.makedirs(os.path.dirname(p["app_ini"]), exist_ok=True)
        write_file(p["app_ini"], ini, 0o600)
        settings = os.path.join(work, "skill", "settings.json")
        if os.path.exists(settings):
            write_file(p["settings"], json.dumps(load_json(settings, {}), indent=1) + "\n", 0o600)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for step in (["migrate"], ["admin", "regenerate", "hooks"]):
        out = gitea(p, *step)
        if out.returncode != 0:
            raise SystemExit(f"Gitea could not {' '.join(step)}: {last_line(out.stderr)}. The forge's data is in "
                             "place, and the forge is not ready: open an issue with this line.")
    user = old_st.get("mirror_user", MIRROR_USER)
    out = gitea(p, "admin", "user", "generate-access-token", "--username", user,
                "--token-name", "windstorm-mirror-" + time.strftime("%Y%m%d%H%M%S", time.gmtime()),
                "--scopes", MIRROR_SCOPES, "--raw")
    if out.returncode != 0 or not out.stdout.strip():
        raise SystemExit("Gitea did not give the mirror account a new key; nothing was printed. The forge's data "
                         "is in place, and the sync cannot reach it: open an issue with this line.")
    write_private(p["mirror_key"], secret(out.stdout.strip()) + "\n")
    st = dict(old_st, port=port, version=VERSION, gitea=GITEA_VERSION, restored_from=os.path.basename(src),
              restored_at=now_utc())
    st.pop("keys", None)
    save_state(p, st)
    here = os.path.abspath(__file__)
    if os.path.abspath(p["script"]) != here:
        shutil.copyfile(here, p["script"] + ".tmp")
        os.replace(p["script"] + ".tmp", p["script"])
    ok, why = install_service(p)
    if ok and not install_timer(p, int(setting(load_json(p["settings"], {}), "interval_min"))):
        ok, why = False, "the sync timer could not be switched on"
    n = sum(1 for r in (st.get("repos") or {}).values() if r.get("forge_id"))
    owners = ", ".join(sorted((load_json(p["settings"], {}).get("owners") or {}))) or "(none)"
    say(f"Restored from {os.path.basename(src)}: {n} copies are back, on http://127.0.0.1:{port}/ (this computer only).")
    say(f"Your GitHub keys are not in a backup. To keep the copies current, make new keys and run `key set` for "
        f"each owner: {owners}. Until then, `status` shows them behind.")
    say("Your Gitea password is the one it had on the old computer.")
    say(f"The forge runs as your own service." if ok else f"The forge's service did not start: {why}")
    if system()[0] == "darwin":
        back, note = comes_back_by_itself()
    else:
        st = load_json(p["state"], {})
        st.pop("linger_by_setup", None)              # the old computer's record means nothing here
        back, note = linger_step(st, allow=not getattr(args, "no_linger", False))
        save_state(p, st)
    if back:
        say("After a restart it starts by itself.")
    if note:
        say(note)
    return 0 if ok else 1


# ---------------------------------------------------------------- G9 upgrades and uninstall

# G1.9: the slowest first answer measured: 25 s, after a restart, on a stock virtual machine with one processor,
# 512 MB and a disk held to 30 MB/s and 80 operations a second (an old laptop's disk). Every wait for a forge's
# first answer is more than three times that, and a forge not answering by then is said with its log's place.
FIRST_START_SEEN = 25
FIRST_START_WAIT = 90
SETUP_WAIT = FIRST_START_WAIT   # seconds setup waits for the new forge to answer before it says so


def not_answering_yet(p, port, waited):
    """G1.9: what 'not answering yet' means, and when the person should look again."""
    return (f"The forge's service is started, and it was not answering yet on http://127.0.0.1:{port}/ after "
            f"{waited} s. On the slowest computer it was tried on, it answered within {FIRST_START_SEEN} s, so "
            f"something is holding it up: run `status` in 5 minutes. If it still does not answer then, the forge's "
            f"own words are the last lines of {os.path.join(p['forge'], 'log', 'gitea.log')}.")


def wait_forge(port, up=True, seconds=FIRST_START_WAIT):
    for _ in range(seconds * 2):
        if port_answers(port) == up:
            if not up:
                return True
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/healthz", timeout=2):
                    return True
            except (urllib.error.URLError, OSError):
                pass
        time.sleep(0.5)
    return False


def cmd_upgrade_forge(args):
    """G9.1: only to the version this release pins; a backup first; the binary checksum-checked; started and
    checked; on any failure the old binary and the database as it was are put back."""
    p = paths()
    st = load_json(p["state"], {})
    if not st.get("port"):
        say("Run setup first.")
        return 2
    cur, new = st.get("gitea") or GITEA_VERSION, GITEA_VERSION
    if vt(cur) == vt(new):
        say(f"The forge runs Gitea {cur}, the version this release pins. Nothing to do.")
        return 0
    if vt(cur) > vt(new):
        say(f"The forge runs Gitea {cur}, newer than the {new} this release pins. A forge is never moved back. "
            "Nothing was changed. Run self-update for the newest release of this skill.")
        return 2
    ok, why = take_lock(p)
    if not ok:
        say(f"No upgrade: {why}. Run upgrade-forge again when it has ended.")
        return 3
    rollback = os.path.join(p["data"], "upgrade-rollback")
    try:
        say(f"upgrade-forge: Gitea {cur} to {new}. First, a backup.")
        if make_backup(p, st, os.path.join(p["data"], "backups")) != 0:
            say("The backup failed, so the forge was not touched. Run upgrade-forge again once the backup's "
                "reason above is dealt with; it makes the backup first.")
            return 1
        save_state(p, st)
        try:
            fetch_forge(p, os.environ.get("WSM_GITEA_BASE"))
        except SystemExit as e:
            say(f"{e} The forge was not touched. Run upgrade-forge again.")
            return 1
        service(p, "stop")
        if not wait_forge(st["port"], up=False, seconds=30):
            service(p, "start")
            say("The forge did not stop, so it was not upgraded. Run upgrade-forge again; if the forge still does "
                "not stop, run status.")
            return 1
        shutil.rmtree(rollback, ignore_errors=True)
        os.makedirs(rollback, mode=0o700)
        db = os.path.join(p["forge"], "data", "gitea.db")
        shutil.copy2(db, os.path.join(rollback, "gitea.db"))
        shutil.copy2(p["app_ini"], os.path.join(rollback, "app.ini"))
        st["gitea"] = new
        save_state(p, st)
        why = ""
        out = gitea(p, "migrate")
        if out.returncode != 0:
            why = "Gitea's migration failed: " + last_line(out.stderr or out.stdout)
        else:
            service(p, "start")
            if not wait_forge(st["port"], up=True, seconds=FIRST_START_WAIT):
                why = "the new forge did not answer within 90 s"
            else:
                code, ver = Forge(p, st).call("GET", "/version")
                if code != 200 or (ver or {}).get("version") != new:
                    why = f"the new forge says it is {(ver or {}).get('version')}, not {new}"
        if why:
            service(p, "stop")
            wait_forge(st["port"], up=False, seconds=30)
            shutil.copy2(os.path.join(rollback, "gitea.db"), db)
            shutil.copy2(os.path.join(rollback, "app.ini"), p["app_ini"])
            st["gitea"] = cur
            save_state(p, st)
            service(p, "start")
            back = wait_forge(st["port"], up=True, seconds=FIRST_START_WAIT)
            say(f"The upgrade to Gitea {new} failed: {why}. "
                + (f"The old forge, Gitea {cur}, is back with its database as it was before."
                   if back else f"The old forge, Gitea {cur}, was put back but does not answer yet; run status.")
                + f" The backup is at {st.get('last_backup')}.")
            return 1
        st["upgraded"] = {"from": cur, "to": new, "at": now_utc(), "backup": st.get("last_backup")}
        save_state(p, st)
        old_bin = os.path.join(os.path.dirname(p["bin"]), f"gitea-{cur}")
        say(f"The forge runs Gitea {new} now. The backup made first is at {st.get('last_backup')}; "
            f"the old binary is kept at {old_bin} until uninstall.")
    finally:
        shutil.rmtree(rollback, ignore_errors=True)
        drop_lock(p)
    return cmd_status(args)


def cmd_self_update(args):
    """G9.2: the skill's own file, replaced whole in one rename, checked first, and never while a sync runs."""
    p = paths()
    code, raw = gh_public(f"/repos/{SKILL_REPO}/releases/latest")
    if code != 200:
        say(f"GitHub did not show the latest release ({code or 'no answer'}). Nothing was changed. Run "
            "self-update again later.")
        return 1
    rel = json.loads(raw or b"null") or {}
    tag = rel.get("tag_name", "")
    m = re.search(r"mirror\.py SHA-256 ([0-9a-f]{64})", rel.get("body") or "")
    if vt(tag) < vt(VERSION):
        say(f"This is {VERSION}, newer than the latest release ({tag}). Nothing was changed.")
        return 0
    if vt(tag) == vt(VERSION):
        # G9.5: on the latest release, the read is said with the checksum it read, checked against this very file.
        with open(os.path.abspath(__file__), "rb") as f:
            own = hashlib.sha256(f.read()).hexdigest()
        if not m:
            say(f"This is the latest release ({VERSION}). Its notes name no checksum of mirror.py, so this file "
                "could not be checked against it. Nothing was changed.")
            return 0
        if m.group(1) == own:
            say(f"This is the latest release ({VERSION}). Its notes name mirror.py SHA-256 {m.group(1)}, and this "
                "file matches it. Nothing was changed.")
            return 0
        say(f"This is the latest release ({VERSION}) by its number, but this file is not the one it names: the "
            f"release says mirror.py SHA-256 {m.group(1)}, and this file's is {own}. Nothing was changed. To put "
            "the released file back, update the plugin with Claude Code's own plugin update, then run setup.")
        return 1
    if not m:
        say(f"Release {tag} does not name the checksum of mirror.py, so it is not installed. Nothing was changed.")
        return 1
    code, data = gh_public(f"/repos/{SKILL_REPO}/contents/scripts/mirror.py?ref={urllib.parse.quote(tag)}",
                           accept="application/vnd.github.raw")
    if code != 200 or hashlib.sha256(data).hexdigest() != m.group(1):
        say(f"The mirror.py of {tag} did not arrive whole or does not match its checksum. Nothing was changed. "
            "Run self-update again; if it fails the same way, do not install it by hand: open an issue with this "
            "line.")
        return 1
    target = p["script"] if os.path.exists(p["script"]) else os.path.abspath(__file__)
    tmp = target + ".new"
    with open(tmp, "wb") as f:
        f.write(data)
    out = run([sys.executable, "-I", tmp, "--version"], timeout=60)
    if out.returncode != 0 or vt(out.stdout) != vt(tag):
        os.remove(tmp)
        say(f"The new mirror.py did not run as {tag}. Nothing was changed.")
        return 1
    ok, why = take_lock(p)
    if not ok:
        os.remove(tmp)
        say(f"No update: {why}. Try again when it is done.")
        return 3
    try:
        os.chmod(tmp, os.stat(target).st_mode & 0o777 if os.path.exists(target) else 0o644)
        os.replace(tmp, target)
    finally:
        drop_lock(p)
    say(f"self-update: {VERSION} to {tag}, replaced whole at {target}. The forge was not changed.")
    pin = re.search(r'^GITEA_VERSION = "([\d.]+)"', data.decode("utf-8", "replace"), re.M)
    if pin and vt(pin.group(1)) > vt(load_json(p["state"], {}).get("gitea") or GITEA_VERSION):
        say(f"{tag} pins Gitea {pin.group(1)}: run upgrade-forge when you choose (it makes a backup first).")
    return 0


def cmd_uninstall(args):
    """G9.3: everything but the folder of copies goes; that folder goes only on the person's typed word, typed
    at a terminal (it cannot be undone, so nothing piped in counts)."""
    p = paths()
    if args.delete_copies and not sys.stdin.isatty():
        say("Needs you: deleting the copies cannot be undone, so the person runs this themselves, in a terminal "
            f"window of their own: python3 -I {p['script']} uninstall --delete-copies. Nothing was changed.")
        return 2
    st = load_json(p["state"], {})
    # Rule 8: a sync that is running is a wait, said with what holds it, never a refusal of a person who wants to
    # leave. With no folder of the skill's left, nothing can be running, and no folder is made again.
    if os.path.exists(p["data"]):
        ok, why = take_lock(p)
        if not ok:
            holder = lock_holder(p)
            say(f"Waiting: a sync is running (process {holder}); uninstall goes on as soon as it ends, for up to "
                f"{UNINSTALL_WAIT // 60} minutes.")
            end = time.time() + UNINSTALL_WAIT
            while not ok and time.time() < end:
                time.sleep(2)
                ok, why = take_lock(p)
        if not ok:
            say(f"Not now: a sync is still running (process {lock_holder(p)}) after {UNINSTALL_WAIT // 60} minutes, "
                "and nothing was removed. Run uninstall again once it has ended.")
            return 3
    removed, linger_said = [], ""
    try:
        uid = str(os.getuid())
        if system()[0] == "darwin":
            for label, plist in ((LAUNCHD_LABEL, p["plist"]),
                                 (SYNC_LABEL, os.path.join(os.path.dirname(p["plist"]), SYNC_LABEL + ".plist"))):
                run(["launchctl", "bootout", f"gui/{uid}/{label}"])
                if os.path.exists(plist):
                    os.remove(plist)
                    removed.append(plist)
        else:
            run(["systemctl", "--user", "disable", "--now", SERVICE, SYNC_UNIT + ".timer"])
            base = os.path.dirname(p["unit"])
            for f in (p["unit"], os.path.join(base, SYNC_UNIT + ".service"), os.path.join(base, SYNC_UNIT + ".timer")):
                if os.path.exists(f):
                    os.remove(f)
                    removed.append(f)
            run(["systemctl", "--user", "daemon-reload"])
            if st.get("linger_by_setup"):
                if run(["loginctl", "disable-linger", getpass.getuser()]).returncode == 0:
                    linger_said = "Lingering was switched on by setup, and is switched off again."
                else:
                    linger_said = ("Lingering was switched on by setup, and could not be switched off: run "
                                   f"loginctl disable-linger {getpass.getuser()}")
            elif st.get("linger_by_setup") is False:
                linger_said = "Lingering was already on before setup, so it is left on."
            st.pop("linger_by_setup", None)
            if os.path.exists(p["state"]):
                save_state(p, st)
        for d in (os.path.dirname(p["bin"]), p["cache"], p["conf"]):
            if os.path.exists(d):
                shutil.rmtree(d)
                removed.append(d)
        for f in (p["script"],):
            if os.path.exists(f):
                os.remove(f)
                removed.append(f)
    finally:
        if os.path.exists(p["data"]):
            drop_lock(p)
    n = len([r for r in (st.get("repos") or {}).values() if r.get("forge_id")])
    rc = 0
    if args.delete_copies and os.path.exists(p["forge"]):
        say(f"To delete the folder of copies ({n} copies, {p['data']}), type the number of copies it holds:")
        answer = input("> ").strip()
        if answer == str(n):
            shutil.rmtree(p["data"])
            removed.append(f"{p['data']}: the folder of copies ({n} copies) and the record of what the skill did here")
        else:
            say(f"That is not {n}. The copies were not deleted. If you mean to delete them, run uninstall "
                f"--delete-copies again and type {n}.")
            rc = 1
    uninstall_census(p, removed, linger_said, n)
    return rc


def uninstall_census(p, removed, linger_said, n):
    """Rule 8: the last thing uninstall prints says what is gone now and what is kept. Run twice, it says so."""
    say("uninstall: what is gone, and what is kept.")
    if removed:
        say("Gone now:")
        for r in removed:
            say(f"  {r}")
    else:
        say("Gone now: nothing; the service, the sync timer, the forge's binary, the settings and the keys were "
            "already gone.")
    if linger_said:
        say(linger_said)
    if os.path.exists(p["forge"]):
        say(f"Kept: the folder of copies, {p['data']} ({n} copies), with the record of what the skill did here. It "
            "is yours; delete it only when you are sure, with: uninstall --delete-copies")
    elif os.path.exists(p["data"]):
        say(f"Kept: {p['data']}, the record of what the skill did here. It is yours.")
    else:
        say("Kept: nothing. Nothing of the skill's is left on this computer.")


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
    binary = running_bin(p)
    os.execve(binary, [binary, "--config", p["app_ini"], "--work-path", p["forge"], "web"], env)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mirror.py", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"Windstorm Mirror for GitHub {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="install the forge as your own service")
    s.add_argument("--dry-run", action="store_true", help="say what setup would do and change nothing")
    s.add_argument("--from", dest="from_file", metavar="BACKUP", help="on a new computer: bring every copy back from a backup")
    s.add_argument("--linger", action="store_true",
                   help="on Linux: the person said yes to lingering, so the mirror runs after a restart with no one "
                        "logged in")
    s.add_argument("--no-linger", action="store_true",
                   help="on Linux: do not switch on lingering; the mirror then syncs only while you are logged in")
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
    y.add_argument("--from-timer", action="store_true", help=argparse.SUPPRESS)
    lo = sub.add_parser("left-out", help="the names of the projects your include list and exclude patterns leave out")
    lo.set_defaults(fn=cmd_left_out)
    st_ = sub.add_parser("status", help="what is copied, what is behind, and when the next sync runs")
    st_.add_argument("--check-release", action="store_true", help="ask GitHub for the latest release now")
    st_.set_defaults(fn=cmd_status)
    y.set_defaults(fn=cmd_sync)
    rs = sub.add_parser("restore", help="prove every copy from the forge alone, or put one in a new home")
    rs.add_argument("--check", action="store_true", help="clone every copy from the forge and check it against GitHub's last refs")
    rs.add_argument("--to", metavar="URL", help="a new, empty home for one copy (never GitHub)")
    rs.add_argument("repository", nargs="?", help="owner/name of the copy to put in the new home")
    rs.set_defaults(fn=cmd_restore)
    e = sub.add_parser("expose", help="what opening the forge to other computers would mean (changes nothing)")
    e.set_defaults(fn=cmd_expose)
    b = sub.add_parser("backup", help="the forge's own backup plus the skill's settings, readable by you only")
    b.add_argument("folder")
    b.set_defaults(fn=cmd_backup)
    u = sub.add_parser("upgrade-forge", help="move the forge to the Gitea this release pins (a backup first)")
    u.set_defaults(fn=cmd_upgrade_forge, check_release=False)
    su = sub.add_parser("self-update", help="replace this skill's own file with the latest release, whole")
    su.set_defaults(fn=cmd_self_update)
    un = sub.add_parser("uninstall", help="remove everything but the folder of copies")
    un.add_argument("--delete-copies", action="store_true", help="also delete the copies, after you type their number")
    un.set_defaults(fn=cmd_uninstall)
    r = sub.add_parser("run-forge", help="(used by the service) start the forge")
    r.set_defaults(fn=cmd_run_forge)
    a = ap.parse_args(argv)
    aside = set_aside_folder(__file__)
    if aside:
        own = paths()["script"]
        say(f"Stopped: this copy of the skill is in a folder Claude Code set aside when its plugin was removed or "
            f"updated ({aside}), and nothing runs from there. Nothing was done. "
            + (f"The skill set up on this computer is {own}: python3 -I {own} {a.cmd}" if os.path.exists(own) else
               "Nothing of the skill's is set up on this computer."))
        return 3
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
