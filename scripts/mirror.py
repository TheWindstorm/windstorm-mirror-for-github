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
import contextlib
import fcntl
import base64
import calendar
import fnmatch
import plistlib
import xml.parsers.expat
import shlex
import unicodedata
import getpass
import urllib.parse
import hashlib
import random
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

VERSION = "0.1.0"
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


def paths(root="from WSM_HOME"):
    """Everything the skill writes, in two folders of the person's own. WSM_HOME moves both (the person's setting,
    named in the README; the suite gives each test a home of its own with it). paths(None): where they are with no
    WSM_HOME."""
    home = os.path.expanduser("~")
    if root == "from WSM_HOME":
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
    # M120: a character this terminal's language cannot write (the bytes of a name that are not UTF-8, as run()
    # reads them) is written as a backslash form; the print never raises, whatever the language.
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    msg = msg.encode(enc, "backslashreplace").decode(enc)
    try:
        print(msg, flush=True)
    except BrokenPipeError:
        # G6.11 (M49): the reader stopped reading (`status | head`). The work goes on to its end; the rest of its
        # words go nowhere, rather than a traceback, or a sync stopped halfway through by a closed pipe.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def tell(lines, to_the_person=False):
    """G10.5: what the person must hear goes out as one numbered block, "tell the person 1 of N: ..." to "N of N",
    so a line dropped, shortened or merged on its way to the person shows (a cold start saw the smallest model
    shorten the dry run to one line and drop a sentence of the privacy line). G3.13: in the person's own terminal
    the same lines go out numbered "1 of N: ...", to them directly."""
    lines = [x for x in lines if x]
    for i, x in enumerate(lines, 1):
        say(f"{'' if to_the_person else 'tell the person '}{i} of {len(lines)}: {x}")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class KeptUnread(Exception):
    """A file the tool keeps that is there and could not be read (its third answer): the command stops before it
    changes anything, naming the file, where the read stopped, and one step a person can take."""

    def __init__(self, path, why):
        # why None: a file the command needs that is not there (the system said so); said as that, never as a read
        self.said = "is not there" if why is None else f"could not be read: {why}"
        super().__init__(f"{path} {self.said}")
        self.path, self.why = path, why


def json_kind(v):
    return {list: "list", str: "string", bool: "true or false", type(None): "null"}.get(type(v), "number")


def blocked_folder(path):
    """The first folder on a path's road that this account may not enter (search), or None. Named in a failed read's
    why, so a folder at mode 000, or one another account owns (left by a run with sudo), is said as that."""
    road, d = [], os.path.dirname(os.path.abspath(path))
    while True:
        road.append(d)
        up = os.path.dirname(d)
        if up == d:
            break
        d = up
    for d in reversed(road):
        if not os.path.isdir(d):
            return None
        if not os.access(d, os.X_OK):
            return d
    return None


def opened_why(path, e):
    """Why a kept file could not be opened: the error's class, and the folder that could not be entered when that is
    the cause; never a byte of the file."""
    why = f"it could not be opened ({type(e).__name__})"
    d = blocked_folder(path) if isinstance(e, PermissionError) else None
    return why + (f": the folder {d} could not be entered" if d else "")


def open_kept(path):
    """A kept file's bytes with its three answers: ('read', the bytes); ('not there', None) only when the system
    itself said the name is not there (the open's own FileNotFoundError, and no link by that name); else ('could not
    be read', why). Any other error (a folder that cannot be entered, a name that is a folder) is never said as an
    absence."""
    try:
        with open(path, "rb") as f:
            return "read", f.read()
    except FileNotFoundError:
        try:
            os.lstat(path)
        except FileNotFoundError:
            return "not there", None
        except OSError as e:
            return "could not be read", opened_why(path, e)
        return "could not be read", "it is a link to a file that is not there"
    except OSError as e:
        return "could not be read", opened_why(path, e)


def kept_read(path):
    """M151, M154, M157 (G3.19, G6.21, G6.24): a kept JSON file's three answers: ('read', the object), ('not
    there', None), or ('could not be read', why). Never taken as empty, never written over on that answer. The why
    names the error's class or where the parser stopped (line, column), never a byte or a value of the file."""
    answer, data = open_kept(path)
    if answer != "read":
        return answer, data
    if not data.strip():
        return "could not be read", "it is empty (0 bytes)" if not data else "it holds only blank space"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "could not be read", "it is not text (UnicodeDecodeError)"
    try:
        v = json.loads(text)
    except ValueError as e:
        return "could not be read", ("it is not well-formed JSON (cut short, or changed by hand; the read stopped at "
                                     f"line {getattr(e, 'lineno', '?')}, column {getattr(e, 'colno', '?')})")
    if not isinstance(v, dict):
        return "could not be read", f"its top is a JSON {json_kind(v)}, where the tool keeps an object"
    return "read", v


def load_kept(path, check=None):
    """The object a kept JSON file holds; {} when it is not there; KeptUnread when it could not be read, or when
    `check` finds an entry of a kind the tool never writes there (its name only, never its value)."""
    answer, got = kept_read(path)
    if answer == "not there":
        return {}
    if answer == "could not be read":
        raise KeptUnread(path, got)
    wrong = check(got) if check else None
    if wrong:
        raise KeptUnread(path, f"its {wrong} is not of the kind the tool writes there")
    return got


def settings_wrong(s):
    owners = s.get("owners")
    if owners is not None and not (isinstance(owners, dict) and all(isinstance(v, dict) for v in owners.values())):
        return "'owners' entry"
    for k in ("include", "exclude"):
        v = s.get(k)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            return f"'{k}' entry"
    return None


def state_wrong(st):
    for k in ("repos",):
        v = st.get(k)
        if v is not None and not (isinstance(v, dict) and all(isinstance(x, dict) for x in v.values())):
            return f"'{k}' entry"
    return None


def load_settings(p):
    return load_kept(p["settings"], settings_wrong)


def load_state(p):
    return load_kept(p["state"], state_wrong)


def write_private(path, text):
    """A file only the person can read: made 0600 before anything is written into it."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    write_file(path, text, 0o600)


def put_whole(path, text, mode):
    """M161 (G4.10): written whole under a temporary name of this write's own, then put in place, so two writes of
    one file at once never meet in one temporary file: the file holds one writer's bytes, whole. Whatever the umask, the
    temporary file is made new (never one left by an earlier run) with its own mode, before a byte is written, so no
    other account can read a key in it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def copy_whole(src, path, mode=0o644):
    """M161 (G4.10): the skill's own copy put in place as put_whole does it: copied under a temporary name of this
    copy's own, then renamed whole, so two setups at once never meet in one temporary file; noted in the record."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        os.close(fd)
        shutil.copyfile(src, tmp)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    claim(path)


def write_file(path, text, mode=0o644):
    """Written whole, then put in place (put_whole), and noted in the skill's record."""
    put_whole(path, text, mode)
    claim(path)


# ---------------------------------------------------------------- G9.12 / rule 23: only what the skill wrote
# A folder a setting names (WSM_HOME, XDG_*) may hold a person's things, and so may a folder of the skill's own name.
# The skill keeps a record of every file and folder it writes in its own folders, as it writes it, and setup,
# uninstall and the delete read the folders against that record at the moment they run: an entry the record does not
# hold stops setup, and is named and left by uninstall and the delete, which go on for the rest (rule 8). Trees a
# program the skill runs writes into (Gitea's forge, git's cache repositories) are recorded whole and known by their
# exact top-level names; a folder goes only when it is empty.
MADE_RECORD = "windstorm-mirror-made.json"
FORGE_NAMES = {"custom", "data", "log"}
GIT_NAMES = {"HEAD", "config", "description", "hooks", "info", "objects", "refs", "packed-refs", "FETCH_HEAD", "lfs",
             "logs", "shallow", "branches", "ORIG_HEAD", "gc.log", "gc.pid", "index"}
_made_cache = {}


def own_roots(p):
    """The folders that are the skill's own places: its data and settings folders, and WSM_HOME around them."""
    roots = [os.path.abspath(p["data"]), os.path.abspath(p["conf"])]
    h = os.environ.get("WSM_HOME")
    if h:
        roots.append(os.path.abspath(h))
    return roots


def inside(path, folder):
    return (path + os.sep).startswith(folder + os.sep) and path != folder


# M158 (G6.25): read once as a command starts, before it writes anything: whether the skill's own folders hold
# nothing yet. Only then may a command start a record where none is; else only setup --use-this-folder makes one.
_made_start = {"data": None, "fresh": None, "use": False}


def home_fresh(p):
    """The skill's own folders hold nothing yet (each is not there, or holds only another of them and the suite's
    marker), and there is no record."""
    roots = own_roots(p)
    if os.path.lexists(os.path.join(p["data"], MADE_RECORD)):
        return False
    for r in roots:
        try:
            names = os.listdir(r)
        except FileNotFoundError:
            continue
        except OSError:
            return False
        for n in names:
            e = os.path.join(r, n)
            if n == SUITE_MARKER or (e in roots and os.path.isdir(e) and not os.path.islink(e)):
                continue
            return False
    return True


def read_made(p):
    """(record, None) or (None, why): what the skill wrote in its own folders, path to kind ("file", "dir",
    "tree:<forge|git|other>", "service:<SHA-256>", or "set-aside record"). M158 (G6.25): the file has three answers;
    "not there" is an empty record only in folders that held nothing as the command started (or once setup
    --use-this-folder made a new one); a record that could not be read is never taken as empty, and its why names
    where the read stopped, never a byte of it."""
    f = os.path.join(p["data"], MADE_RECORD)
    answer, got = kept_read(f)
    if answer == "not there":
        if (_made_start["fresh"] is False and not _made_start["use"]
                and _made_start["data"] == os.path.abspath(p["data"])):
            return None, f"{f} is not there"
        return {}, None
    if answer != "read":
        return None, f"{f} could not be read: {got}"
    made = got.get("made") or {}
    if got.get("written_by") != "windstorm-mirror" or not isinstance(made, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in made.items()):
        return None, f"{f} could not be read: it is not the record the skill writes (its 'written_by' or 'made' entry)"
    return dict(made), None


def holds_mirror(p):
    """M158: by paths, the skill's data folder holds a mirror: the forge's own settings file, or its data folder
    (where the copies are)."""
    return os.path.lexists(p["app_ini"]) or os.path.isdir(os.path.join(p["forge"], "data"))


CACHE_REPO = re.compile(r"[0-9]+\.git")


def service_expected(p):
    """Each service file this home's setup writes, and the text it writes there now (None for one whose text needs a
    setting that could not be read)."""
    try:
        minutes = int(setting(load_settings(p), "interval_min"))
    except (KeptUnread, TypeError, ValueError):
        minutes = None
    if system()[0] == "darwin":
        return {p["plist"]: plist_text(p), os.path.join(os.path.dirname(p["plist"]), SYNC_LABEL + ".plist"):
                sync_plist_text(p, minutes) if minutes else None}
    base = os.path.dirname(p["unit"])
    return {p["unit"]: unit_text(p), os.path.join(base, SYNC_UNIT + ".service"): sync_unit_text(p),
            os.path.join(base, SYNC_UNIT + ".timer"): sync_timer_text(minutes) if minutes else None}


def service_as_written(p, f):
    """True when the service file is byte for byte what this home's setup writes there now (so it runs this home's
    own copy, and is the skill's), False when it is not, None when its bytes could not be read."""
    want = service_expected(p).get(f)
    if want is None or os.path.islink(f) or not os.path.isfile(f):
        return False
    try:
        with open(f, "rb") as fh:
            return fh.read() == want.encode("utf-8")
    except OSError:
        return None


def made_by_place(p):
    """M158 (G6.25), on the person's own word (setup --use-this-folder): a record built anew from what stands at the
    skill's own places in the form the skill writes there: a file by its path (a plain file, never a link), a folder
    a real one, the forge's tree and each cache's git tree by their top-level names, and a service file only while
    it is byte for byte what setup writes there now. Anything else is left off it, and so is named, never taken.
    Never by a name alone (the owner's ruling, 11-26): the forge's program only under the pinned version's name and
    with the pin's own SHA-256; a key file only under a name the skill gives (each owner's in the settings, the
    'mirror' account's key, the password), mode 0600, one line of text (and the forge's key in the form the forge
    gives); none of it read out or printed."""
    rec = {}

    def key_(x, form=None):
        try:
            mode = stat.S_IMODE(os.lstat(x).st_mode)
        except OSError:
            return
        if (os.path.isfile(x) and not os.path.islink(x) and mode == 0o600
                and key_file_read(x, form)[0] == "read"):
            rec[os.path.abspath(x)] = "file"

    def file_(x):
        if os.path.isfile(x) and not os.path.islink(x):
            rec[os.path.abspath(x)] = "file"

    def dir_(x):
        ok = os.path.isdir(x) and not os.path.islink(x)
        if ok:
            rec[os.path.abspath(x)] = "dir"
        return ok

    def names(x):
        try:
            return sorted(os.listdir(x))
        except OSError:
            return []
    if dir_(p["data"]):
        for x in (p["script"], p["state"], p["first_copy_list"], os.path.join(p["data"], SERVICE_MEANT),
                  os.path.join(p["data"], "sync.lock")):
            file_(x)
        if dir_(os.path.dirname(p["bin"])) and os.path.isfile(p["bin"]) and not os.path.islink(p["bin"]):
            try:
                if sha256_of(p["bin"]) == pin_for_this_system()[1]:
                    file_(p["bin"])
            except (OSError, SystemExit):
                pass
        if os.path.isdir(p["forge"]) and not os.path.islink(p["forge"]) and set(names(p["forge"])) & FORGE_NAMES:
            rec[os.path.abspath(p["forge"])] = "tree:forge"
        if dir_(p["cache"]):
            for n in names(p["cache"]):
                c = os.path.join(p["cache"], n)
                inner = names(c)
                if (CACHE_REPO.fullmatch(n) and os.path.isdir(c) and not os.path.islink(c) and "HEAD" in inner
                        and all(x in GIT_NAMES or x.endswith(".lock") for x in inner)):
                    rec[os.path.abspath(c)] = "tree:git"
        for n in ("backups", "upgrade-rollback"):
            c = os.path.join(p["data"], n)
            if os.path.isdir(c) and not os.path.islink(c):
                rec[os.path.abspath(c)] = "tree:other"
    if dir_(p["conf"]):
        file_(p["settings"])
        key_(p["mirror_key"], FORGE_KEY_FORM)
        key_(p["password"])
        if dir_(p["keys"]):
            try:
                owners = load_settings(p).get("owners") or {}
            except KeptUnread:
                owners = {}
            for owner in owners:
                key_(key_path(p, owner))
    for f in service_files(p):
        if service_as_written(p, f):
            rec[f] = "service:" + sha256_of(f)
    return rec


def setup_writes(p):
    """Every place in the skill's own folders that setup, sync, key set or config writes a file of its own at."""
    owners = []
    with contextlib.suppress(KeptUnread):
        owners = list(load_settings(p).get("owners") or {})
    return {os.path.abspath(x) for x in (
        p["bin"], p["script"], p["state"], p["first_copy_list"], os.path.join(p["data"], SERVICE_MEANT),
        os.path.join(p["data"], "sync.lock"), os.path.join(p["data"], "backups"),
        os.path.join(p["data"], "upgrade-rollback"), p["settings"], p["mirror_key"], p["password"],
        os.path.join(p["data"], MADE_RECORD), *[key_path(p, o) for o in owners])}


def writes_there(p, x):
    """An entry at a place the skill writes: one of setup_writes, a cache in the form the sync names one, or the
    forge's own folders (a name Gitea writes there)."""
    x = os.path.abspath(x.split(" (could not be read", 1)[0])
    return (x in setup_writes(p) or (os.path.dirname(x) == os.path.abspath(p["cache"])
                                     and CACHE_REPO.fullmatch(os.path.basename(x)) is not None)
            or (os.path.dirname(x) == os.path.abspath(p["forge"]) and os.path.basename(x) in FORGE_NAMES)
            or x in (os.path.abspath(p["data"]), os.path.abspath(p["conf"]), os.path.abspath(p["keys"]),
                     os.path.abspath(p["cache"]), os.path.abspath(p["forge"]), os.path.dirname(os.path.abspath(p["bin"]))))


def take_folder_on(p, lost, rec):
    """setup --use-this-folder on a folder that holds a mirror whose record is lost, once every check before setup's
    first write has passed against `rec` (made_by_place's): the record that could not be read is set aside under a
    name of its own (kept, never deleted or written over), and `rec` is written as the new record, with the
    set-aside one on it as its own kind. Says what it did."""
    f = os.path.join(p["data"], MADE_RECORD)
    with made_lock(p):
        aside = None
        if os.path.lexists(f):
            aside = f + ".could-not-be-read-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            os.rename(f, aside)
            rec[aside] = "set-aside record"
        _made_start["use"] = True
        write_made(p, rec)
    say(f"Taken on again, on your word (--use-this-folder): {lost}. "
        + (f"It is set aside, as it was, as {aside}. " if aside else "")
        + "A new record holds what setup can show by its place and its form to be the skill's own here. Nothing was "
        "deleted or written over.")
    took = sorted(k for k, v in rec.items() if v != "dir" and v != "set-aside record")
    left = not_ours(p, rec)
    say(f"Taken on as the skill's ({len(took)}), each named here, and each goes with uninstall as the skill's own:")
    for k in took:
        say(f"  {k}" + (" (the forge's own folder, with the copies)" if rec[k] == "tree:forge" else
                        " (a cache of one copy)" if rec[k] == "tree:git" else ""))
    say(f"Not taken ({len(left)}): " + ("none." if not left else "they stay where they are, as yours:"))
    for k in left:
        say(f"  {k}")


def service_files(p):
    """The service manager's files the skill writes, outside its own folders: each is recorded with its SHA-256, and
    is written over or removed only while it is still exactly as the skill wrote it."""
    if system()[0] == "darwin":
        return [p["plist"], os.path.join(os.path.dirname(p["plist"]), SYNC_LABEL + ".plist")]
    base = os.path.dirname(p["unit"])
    return [p["unit"], os.path.join(base, SYNC_UNIT + ".service"), os.path.join(base, SYNC_UNIT + ".timer")]


def service_file_state(p, rec, f):
    """M150 (a reviewer's trial, 11-26): is this service file exactly as this home wrote it (its recorded SHA-256):
    ('yes', None), ('no', None), or ('could not be read', why) when its bytes could not be read (mode 000, an error
    in the read). The third is never this home's (never written over, never removed) and never "not there"."""
    if os.path.islink(f) or not os.path.isfile(f):
        return "no", None
    try:
        got = sha256_of(f)
    except OSError as e:
        return "could not be read", f"it could not be opened ({type(e).__name__})"
    return ("yes" if rec is not None and rec.get(f) == "service:" + got else "no"), None


def service_file_ours(p, rec, f):
    return service_file_state(p, rec, f)[0] == "yes"


def write_made(p, rec):
    f = os.path.join(p["data"], MADE_RECORD)
    os.makedirs(p["data"], mode=0o700, exist_ok=True)
    put_whole(f, json.dumps({"written_by": "windstorm-mirror", "made": rec}, indent=1), 0o600)


_made_held = [0]


@contextlib.contextmanager
def made_lock(p, make=True):
    """M162 (G4.11): the record is read and written under one lock, so an entry one command notes is never lost to
    another's write (a key set while the first sync runs, the day a person sets up). The lock is the data folder's
    own (flock on the folder itself: no file is added); it is held once per process (a claim inside a write takes
    no second lock), and only while the record is read and written. With no data folder and make False, there is
    no record to keep, and nothing is locked."""
    if _made_held[0]:
        _made_held[0] += 1
        try:
            yield
        finally:
            _made_held[0] -= 1
        return
    if make:
        os.makedirs(p["data"], mode=0o700, exist_ok=True)
    try:
        fd = os.open(p["data"], os.O_RDONLY)
    except FileNotFoundError:
        if make:
            raise
        yield
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _made_held[0] = 1
        try:
            yield
        finally:
            _made_held[0] = 0
    finally:
        os.close(fd)


def claim(path, kind="file"):
    """Note in the record that the skill wrote `path` (and made each folder between it and its own folders), or,
    for a service file, its SHA-256. Anything else outside the skill's own folders (a backup the person asked for)
    is not recorded."""
    p = paths()
    path = os.path.abspath(path)
    roots = own_roots(p)
    if path in service_files(p):
        with made_lock(p):
            rec, why = read_made(p)
            if rec is not None:
                rec[path] = "service:" + sha256_of(path)
                write_made(p, rec)
        return
    if not any(inside(path, r) for r in roots) or os.path.basename(path) == MADE_RECORD:
        return
    with made_lock(p):
        claim_noted(p, path, kind, roots)


def claim_noted(p, path, kind, roots):
    """claim's own read and write of the record, under made_lock."""
    rec, why = read_made(p)
    if rec is None:
        return                       # never written over: an unreadable record stays as it is, and is said
    want = {path: kind}
    d = os.path.dirname(path)
    while any(inside(d, r) for r in roots):
        want.setdefault(d, "dir")
        d = os.path.dirname(d)
    if all(rec.get(k) == v or (v == "dir" and k in rec) for k, v in want.items()):
        return
    for k, v in want.items():
        if not (v == "dir" and k in rec):
            rec[k] = v
    write_made(p, rec)


def unclaim(p, gone):
    with made_lock(p, make=False):
        rec, why = read_made(p)
        if rec is None or not any(g in rec for g in gone):
            return
        for g in gone:
            rec.pop(g, None)
        if os.path.exists(p["data"]):
            write_made(p, rec)


def not_ours(p, rec=None):
    """Every entry in the skill's own folders that its record does not hold (at most 50), or [why] when the record
    cannot be read. Checked at the moment a command runs: a person's file may have arrived since setup. `rec`: a
    record to read the folders against (setup --use-this-folder --dry-run's own, never written)."""
    why = None
    if rec is None:
        rec, why = read_made(p)
    if rec is None:
        return [why]
    found = []
    record_file = os.path.join(os.path.abspath(p["data"]), MADE_RECORD)

    def look(folder):
        try:
            names = sorted(os.listdir(folder))
        except FileNotFoundError:
            return
        except OSError as e:
            found.append(f"{folder} (could not be read: {e})")
            return
        for n in names:
            e = os.path.join(folder, n)
            kind = rec.get(e)
            if e == record_file or e in roots or (n == SUITE_MARKER and folder == wsm):
                if e in roots and e != folder:
                    continue          # an own folder inside another (a Mac's config inside data) is looked at on its own
                continue
            if kind is None:
                found.append(e)
            elif kind == "dir" and os.path.isdir(e) and not os.path.islink(e):
                look(e)
            elif kind in ("tree:forge", "tree:git") and os.path.isdir(e):
                allowed = FORGE_NAMES if kind == "tree:forge" else GIT_NAMES
                found.extend(os.path.join(e, x) for x in sorted(os.listdir(e))
                             if x not in allowed and not x.endswith(".lock"))
            if len(found) >= 50:
                return
    roots = own_roots(p)
    wsm = os.path.abspath(os.environ.get("WSM_HOME") or "") if os.environ.get("WSM_HOME") else None
    for r in roots:
        look(r)
    return sorted(set(found))[:50]


STRANGER = ("", -1, frozenset())     # an account in no group of any folder: only the "others" bits let it in


def accounts():
    """Rule 28: every account on this computer but the person's own and the administrator's, as (name, uid, its
    group ids); None when the list cannot be read (rule 6: then that is said, never taken as nobody)."""
    try:
        import pwd
        rows = pwd.getpwall()
    except Exception:
        return None
    me, out = os.getuid(), []
    for u in rows:
        if u.pw_uid in (me, 0):
            continue
        try:
            out.append((u.pw_name, u.pw_uid, groups_of(u.pw_name, u.pw_gid)))
        except Exception:           # this one account's groups could not be read: said for it, the rest go on
            out.append((u.pw_name, u.pw_uid, None))
    return out


def groups_of(name, gid):
    """An account's group numbers, as a folder's group reads. On a Mac, the system's call takes a signed number, so
    the group of nobody (4294967294 in the account list, -2 there) is passed as -2, and what comes back is read as
    the folder reads it (seen on macOS 13: the call answered 2**64 - 2 for it)."""
    try:
        got = os.getgrouplist(name, gid)
    except OverflowError:
        got = os.getgrouplist(name, gid - 2 ** 32)
    return frozenset(g % 2 ** 32 for g in got) | {gid}


def may(acct, s_, bit):
    """What the system asks: the owner's bits for the owner, the group's for a member of the group, else the others'.
    None when it turns on groups that could not be read for this account (its group's bits and the others' differ)."""
    name, uid, groups = acct
    if uid == s_.st_uid:
        return bool(s_.st_mode >> 6 & bit)
    if groups is None:
        g, o = bool(s_.st_mode >> 3 & bit), bool(s_.st_mode & bit)
        return g if g == o else None
    if s_.st_gid in groups:
        return bool(s_.st_mode >> 3 & bit)
    return bool(s_.st_mode & bit)


def account_name(uid, accts=()):
    """The name of the account with this number: from the accounts read, else the system's list, else the number."""
    for a in accts or []:
        if a[1] == uid:
            return a[0]
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return f"number {uid}"


def group_name(gid):
    try:
        import grp
        return grp.getgrgid(gid).gr_name
    except (ImportError, KeyError):
        return str(gid)


def loose(path, accts=()):
    """Rule 28 (when a person's folder is named): why another account can really reach this folder and write in it,
    "could not be read (...)", or None. Reach: every folder above it lets that account pass. Write: its bits for
    that account, or it owns the folder. A 775 folder in the person's own group with nobody else in it is not
    named; accts None (the list of accounts could not be read) is said as that when a group could write."""
    up = []
    d = os.path.dirname(os.path.abspath(path))
    while True:
        up.append(d)
        if os.path.dirname(d) == d:
            break
        d = os.path.dirname(d)
    try:
        s_ = os.stat(path)
        chain = [os.stat(x) for x in up]
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"could not be read ({e.strerror or e})"
    pool = [STRANGER] + list(accts or [])
    if s_.st_uid not in (os.getuid(), 0):
        owner = next((a for a in pool if a[1] == s_.st_uid), ("", s_.st_uid, frozenset()))
        if all(may(owner, c, 1) for c in chain):
            return "another account owns it"
    def can(a):                 # True, False, or None (it turns on groups that could not be read)
        said = [may(a, c, 1) for c in chain] + [may(a, s_, 2)]
        return False if False in said else None if None in said else True
    writers = [a for a in pool if can(a) is True]
    unsure = [a for a in pool if can(a) is None]
    if STRANGER in writers:
        return "every account on this computer can write in it"
    if writers:
        return ("another account on this computer can write in it ("
                + ", ".join(sorted(a[0] for a in writers)[:3]) + (", and more" if len(writers) > 3 else "") + ")")
    if accts is None and s_.st_mode & 0o020:
        return f"writable by group {group_name(s_.st_gid)}; who is in that group could not be read"
    if unsure:
        return (f"not fully known: the groups of {len(unsure)} account{'s' if len(unsure) > 1 else ''} on this "
                "computer could not be read")
    return None


def folders_above(p):
    """Rule 28 (a file is only as closed as the folders above it): every folder from the home (or from the skill's
    own root, for what is not under the home) down to each thing of the skill's that runs, and to what decides what
    runs (what runs includes what decides what runs): the service's files, the installed copy of the tool, the
    forge's program and the state that picks it, the forge's settings (app.ini), the tool's settings, and the Python
    the service's files name (as named, and the file it leads to)."""
    home = os.path.abspath(os.path.expanduser("~"))
    out = []
    py = [sys.executable, os.path.realpath(sys.executable)] if sys.executable else []
    for f in service_files(p) + [p["script"], p["bin"], p["state"], p["app_ini"], p["settings"]] + py:
        d = os.path.dirname(os.path.abspath(f))
        top = home if (d == home or inside(d, home)) else next(
            (r for r in sorted(own_roots(p), key=len) if d == r or inside(d, r)), d)
        chain = [d]
        while chain[-1] != top and os.path.dirname(chain[-1]) != chain[-1]:
            chain.append(os.path.dirname(chain[-1]))
        out += [c for c in reversed(chain) if c not in out]
    return out


def open_folders(p, close=False):
    """Rule 28: each folder above what the skill runs that another account can write in, as a line for the person.
    The skill's own (one of its roots, on its record, or made by setup) setup closes (`close`); a folder it cannot
    know it made is never changed (rule 23), only named, with what that means and, when it is the person's, the one
    command that closes it; one that is not theirs, with whose it is and no command."""
    rec, why = read_made(p)
    own = set(own_roots(p)) | set(rec or {}) | set(load_json(p["state"], {}).get("made_folders") or [])
    lines, accts = [], accounts()
    for d in folders_above(p):
        try:
            mine = d in own and os.stat(d).st_uid == os.getuid() and os.stat(d).st_mode & 0o022
        except OSError:
            mine = False
        if mine:                    # the skill's own: closed for group and others, without asking who is there
            if close:
                os.chmod(d, os.stat(d).st_mode & 0o7755)
            else:
                lines.append(f"{d} is a folder of the skill's own that others may write in: setup closes it "
                             "(chmod go-w).")
            continue
        why = loose(d, accts)
        if not why:
            continue
        try:
            uid = os.stat(d).st_uid
        except OSError:
            uid = os.getuid()
        # Rule 28 (how it is worked out): a folder that is not the person's gets no command, since the person
        # cannot change it and a printed command that fails is a false next step; its line says whose it is.
        who = account_name(uid, accts) if uid != os.getuid() else ""
        not_yours = (f" It belongs to {who}, not to you: only the computer's administrator can change it. The skill "
                     "leaves it as it is.")
        if why.startswith("could not be read"):
            lines.append(f"{d} {why}, so whether another account can write in it is not known; the skill's service "
                         "or program is in it, or below it.")
        elif why == "another account owns it":
            lines.append(f"{d} belongs to the account {who}, which could replace the skill's service or program in "
                         "it, or below it. You cannot change a folder that is not yours: only the computer's "
                         "administrator can hand it back to you. The skill leaves it as it is.")
        elif why.startswith("not fully known"):
            lines.append(f"{d}: whether another account can write in it is {why}; the skill's service or program is "
                         "in it, or below it."
                         + (not_yours if who else f" If it is not only yours: chmod go-w {shlex.quote(d)}"))
        elif why.startswith("writable by group"):
            lines.append(f"{d} is {why}, so whether another account can write in it is not known; the skill's "
                         "service or program is in it, or below it."
                         + (not_yours if who else f" If it is not only yours: chmod g-w {shlex.quote(d)}"))
        else:
            lines.append(f"{d} is open: {why}, so another account could replace the skill's service or program in it, "
                         "or below it."
                         + (not_yours if who else " It is yours, and the skill leaves it as it is. The one command "
                            f"that closes it: chmod go-w {shlex.quote(d)}"))
    return lines


UNSEEN = ("Cc", "Cf", "Cs", "Zl", "Zp")       # the family's rule 31 (vi): cannot be seen, or not text


def service_value_stop(p):
    """G1.24 (M148): a value the service files carry that no service manager can be handed as one whole value (a
    character that cannot be seen, a line or paragraph separator, or bytes that are not text): setup stops before
    its first write. None, or the sentence that stops it. The value itself is not printed: it holds such a character."""
    for what, value, step in (
            ("the path of the Python this runs with", sys.executable,
             "run setup with a Python whose path has none (for example /usr/bin/python3)"),
            ("the folder this skill keeps its things in", p["data"],
             "set WSM_HOME to a folder of your own whose path has none, and run setup again"),
            ("WSM_HOME", os.environ.get("WSM_HOME") or "",
             "set WSM_HOME to a folder of your own whose path has none, and run setup again")):
        if what.startswith("the path of the Python") and not value:
            return ("Stopped: this Python does not say where its own program is, so the service could not be told "
                    "what to run. Nothing was changed. The one next step: run setup with /usr/bin/python3.")
        if any(unicodedata.category(c) in UNSEEN for c in value):
            return (f"Stopped: {what} holds a character that cannot be seen, or bytes that are not text, so it "
                    "cannot be written into the service file as one whole value. Nothing was changed. The one next "
                    f"step: {step}.")
        if what.startswith("the path of the Python") and system()[0] != "darwin" and any(c in SD_PATH_REFUSED for c in value):
            found = [SD_PATH_NAMES[c] for c in SD_PATH_REFUSED if c in value]     # each by its name (rule 31 v.5)
            return (f"Stopped: the path of the Python this runs with holds {' and '.join(found)}, and systemd does "
                    f"not take a program's path holding {'it' if len(found) == 1 else 'them'} as one whole value. "
                    f"Nothing was changed. The one next step: {step}.")
    return None


USE_FOLDER_STEP = "If this folder is your mirror and you want to go on with it, the one next step is yours to take: "


def lost_record_stop(p, lost):
    """M158 (G6.25): setup's stop on a folder that holds a mirror whose record is lost: the record named with why,
    then the need-you block, printed last, with the one step that is the person's own."""
    return (f"Stopped: {lost}, and {p['data']} holds a mirror (the forge's own files, and the copies in them). So "
            "setup cannot tell which files there are the skill's own, and changes nothing: nothing was copied, written "
            "or taken away, and the copies, the forge's settings and your keys are still there as they were.\n"
            "Needs you: going on with this folder is the person's own word.\n"
            f"tell the person 1 of 2: {USE_FOLDER_STEP}{own_command(p, 'setup', '--use-this-folder')}\n"
            "tell the person 2 of 2: It keeps that record, set aside under a name of its own, and makes a new one of "
            "what setup can show by its place and its form to be the skill's own; anything else in these folders "
            "stays where it is, off that record, and is named. Nothing is deleted or written over.")


def home_is_not_ours(p, use=False, dry=False):
    """Setup's word on the skill's own folders before it writes anything: None, or the sentence that stops it.
    `use`: the person's --use-this-folder (M158)."""
    _take_on_pending.clear()
    stop = service_value_stop(p)
    if stop:
        return stop
    h = os.environ.get("WSM_HOME")
    if h and os.path.realpath(h) == os.path.realpath(os.path.expanduser("~")):
        return (f"Stopped: WSM_HOME is your home folder ({h}). The skill makes its folders only in a folder of its "
                "own, so nothing of yours is ever written over or taken away. Nothing was changed. The one next "
                "step: set WSM_HOME to a folder that does not exist yet, or is empty, and run setup again.")
    for d in own_roots(p) + [os.path.dirname(p["bin"])]:
        try:
            s_ = os.stat(d)
        except FileNotFoundError:
            continue
        # Others may not write there; a group may only when it is the person's own (a user's private group).
        loose = s_.st_mode & 0o002 or (s_.st_mode & 0o020 and s_.st_gid != os.getgid())
        if s_.st_uid != os.getuid() or loose:
            return (f"Stopped: {d} is not yours alone ("
                    + ("another account owns it" if s_.st_uid != os.getuid() else "other accounts can write in it")
                    + "), and the forge's program, your keys and the copies would live there, where someone else "
                    "could change them. Nothing was changed. The one next step: "
                    + ("set WSM_HOME to a folder of your own that does not exist yet" if h else
                       f"make {d} yours alone (run chmod go-w {shlex.quote(d)} as its owner)") + ", and run setup again.")
    lost = read_made(p)[1]
    rec, taken_on = None, False
    if lost and holds_mirror(p):
        if not use:
            return lost_record_stop(p, lost)
        rec = made_by_place(p)        # written only once every check below has passed
        if dry:
            say(f"With --use-this-folder, setup would set {os.path.join(p['data'], MADE_RECORD)} aside (kept, as it "
                f"is) and make a new record of {len(rec)} entries, of what it can show by its place and its form to "
                "be the skill's own; nothing would be deleted or written over.")
        taken_on = True
    elif use:
        here = os.path.lexists(os.path.join(p["data"], MADE_RECORD))
        say("--use-this-folder changes nothing here: " + (
            f"the record of what the skill made, {os.path.join(p['data'], MADE_RECORD)}, was read whole, so setup goes "
            "on from it as it always does." if not lost and here else
            "the skill's folders held nothing yet, so there is nothing of a mirror's to go on with; setup goes on as "
            "it would without it." if not lost else
            f"{p['data']} holds no mirror (no forge settings file at {p['app_ini']}, and no forge data folder), so "
            "there is nothing of a mirror's to go on with; setup goes on as it would without it."))
    record_unread = []
    if rec is None:
        rec, why = read_made(p)
        if rec is None:
            # No mirror here (holds_mirror is False), so nothing of the skill's is lost with the record: each entry
            # in its folders is named by its path, as one the skill did not make, and a record that is there but
            # could not be read is named with them (it is moved out with them; nothing is deleted or written over)
            rec = {}
            if not why.endswith(" is not there"):
                record_unread = [why.replace(" could not be read: ", " (could not be read: ", 1) + ")"]
    states = {f: service_file_state(p, rec, f) for f in service_files(p) if os.path.lexists(f)}
    unread = [(f, w) for f, (a, w) in states.items() if a == "could not be read"]
    if unread:
        return ("Stopped: this computer already has " + "; and ".join(f"{f}, and it could not be read: {w}"
                                                                      for f, w in unread)
                + ". So setup cannot tell whether " + ("it is" if len(unread) == 1 else "they are") + " as the "
                "skill wrote " + ("it" if len(unread) == 1 else "them") + ", and does not write over "
                + ("it" if len(unread) == 1 else "them") + ". Nothing was changed. The one next step: move "
                + ("that file" if len(unread) == 1 else "those files") + f" out of {os.path.dirname(unread[0][0])} "
                f"(into a folder of your own, to keep it), then run {own_command(p, 'setup')}")
    changed = [f for f, (a, _) in states.items() if a == "no"]
    if changed:
        return ("Stopped: this computer already has " + ", ".join(changed) + (
                    ", and not what setup writes there now, so setup cannot show it is the skill's own (the record "
                    "of what the skill wrote was not read)" if taken_on else
                    ", and not as the skill wrote it (it was changed since, or something else wrote it)")
                + ", so setup does not write over it. Nothing was changed. The one next step: move it out of that "
                "folder, and run setup again.")
    others = record_unread + not_ours(p, rec)
    if taken_on:
        # M158 (the owner's ruling, 11-26): on the way back, what setup does not take on stays where it is and is
        # named as the person's; only an entry at a place setup itself writes stops it (never written over)
        others = [x for x in others if writes_there(p, x)]
    if others:
        where = h or p["data"]
        # M158: a folder that holds a mirror is never left for a second home: what is in it is moved out of it
        second = h and not holds_mirror(p)
        return (f"Stopped: {where} already holds things the skill did not make: " + ", ".join(others[:8])
                + (f" and {len(others) - 8} more" if len(others) > 8 else "") + ". The skill writes only into "
                "folders that are its own, so nothing of yours is written over or taken away. Nothing was changed. "
                "The one next step: " + ("set WSM_HOME to a folder that does not exist yet, or is empty"
                                         if second else "move those out of it (into a folder of your own, to keep "
                                         "them)") + ", and run setup again.")
    if taken_on and not dry:
        _take_on_pending[:] = [(p, lost, rec)]     # taken on at setup's first write, after its last stop (walk 11-26)
    return None


_take_on_pending = []


def take_on_now():
    """setup --use-this-folder's take-on, made only once no stop of setup's is left before its first write: a stop
    after it (lingering not chosen, a sync holding the lock) would else say "Nothing was changed" over a record
    already set aside and written anew (seen 2026-11-26 on a stock Ubuntu 24.04 guest)."""
    while _take_on_pending:
        take_folder_on(*_take_on_pending.pop())


def run(cmd, **kw):
    """Every outside command goes through here, so tests can stand in for the ones a CI machine lacks. A program
    that is not installed answers 127 with a plain sentence, never a traceback (seen: a computer with no git).
    M120: its words are read as bytes and decoded by text_of with the codec Python itself encodes every argument and
    environment value with, so a name read from git and handed back to git is the same bytes under any language."""
    try:
        out = subprocess.run(cmd, capture_output=True, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} is not installed on this computer")
    return subprocess.CompletedProcess(out.args, out.returncode, text_of(out.stdout), text_of(out.stderr))


def lines_of(text):
    """A program's lines, split at the line feed only (M120): a ref's name may hold U+0085, U+2028 or U+2029, where
    Python's splitlines also breaks."""
    return [x for x in (text or "").split("\n") if x.strip()]


def text_of(data):
    """A program's bytes as text, never raising (M120), and with every byte kept: a carriage return stays one, so
    a line ends only where the program wrote a line feed (family rule 31 (vi)). Decoded as os.fsdecode does, which is
    the codec subprocess encodes each argument with (UTF-8 with stand-in characters on a UTF-8 language and on a Mac;
    the language's own on another, seen: ISO-8859-1), so what is read goes back as the same bytes by construction."""
    if data is None:
        return None
    return os.fsdecode(data)


def program_lines(text):
    """A program's words cut into lines at a carriage return too (M120), as they came: read, never shown."""
    return [x for x in re.split(r"\r\n|\r|\n", text or "") if x.strip()]


def shown_lines(text):
    """A program's words as lines for a PERSON (M120, the owner's ruling 11-24): cut at a carriage return too, so
    words after one never reach the screen over the words before it. Records are read with lines_of. M134 (family
    rule 31(B), the owner's ruling 11-25): git and the forge quote a name somebody else chose in their own words, so
    each line goes through shown_line; nothing decides by these lines."""
    return [shown_line(x) for x in program_lines(text)]


def shown_line(line):
    """One line of a program's words for a person: a backtick and each of the two characters of a dollar-parenthesis
    are put as a ?, every other character stays as it came (so the words can be searched for), and a line where one
    was put says so. No line is dropped for it."""
    out = line.replace(BACKTICK, "?").replace(DOLLAR_PAREN, "??")
    return line if out == line else out.rstrip() + "; " + SHOWN_CHANGED


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
        claim(p["bin"])
    finally:
        if os.path.exists(part):
            os.remove(part)
    return True


# ---------------------------------------------------------------- G1.3 the forge's settings

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
; G8.4 (M64): stock Gitea writes the account's ~/.ssh/authorized_keys when it starts, even with SSH off (seen
; 2026-11-23: an empty file made where there was none, and the person's own file rewritten). Never: what may log in
; to this computer is not the forge's to touch, and any SSH file of its own stays in its own folder.
SSH_CREATE_AUTHORIZED_KEYS_FILE = false
SSH_CREATE_AUTHORIZED_PRINCIPALS_FILE = false
SSH_ROOT_PATH = {f}/ssh
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


# M159 (G6.26): what the forge itself needs from its settings file to start as an installed forge, by section and
# name (Gitea 28.4.0 stopped at its start, in a stand-in trial, 2026-11-26, without [security] INSTALL_LOCK).
FORGE_INI_NEEDS = (("server", "HTTP_PORT"), ("database", "DB_TYPE"), ("database", "PATH"), ("security", "INSTALL_LOCK"))


def forge_ini_read(p):
    """M159 (G6.26): the forge's settings file's three answers: ('read', None), ('not there', None) or ('could not
    be read', why); the why names the error's class or the setting that is missing, never a value of the file."""
    f = p["app_ini"]
    answer, data = open_kept(f)
    if answer != "read":
        return answer, data
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "could not be read", "it is not text (UnicodeDecodeError)"
    if not data.strip():
        return "could not be read", "it is empty (0 bytes)" if not data else "it holds only blank space"
    for sec, key in FORGE_INI_NEEDS:
        if not ini_value(f, sec, key):
            return "could not be read", (f"it holds no {key} in its [{sec}] part, which the forge needs to start (cut "
                                         "short, or changed by hand)")
    if not ini_value(f, "server", "HTTP_PORT").isdigit():
        return "could not be read", "its HTTP_PORT in its [server] part is not a number"
    return "read", None


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
    except (OSError, UnicodeDecodeError):
        pass
    return None


def running_bin(p):
    """The forge's binary is the one the state names, not this script's own pin: a self-update that brings a newer
    pin never changes the forge by itself (G9.1)."""
    st = load_json(p["state"], {})
    ver = st.get("gitea")
    return os.path.join(os.path.dirname(p["bin"]), f"gitea-{ver}") if ver else p["bin"]


def forge_env(p):
    """What the forge runs with: this environment without any of git's own settings (GIT_CONFIG_GLOBAL, GIT_DIR and
    the rest) and without XDG_CONFIG_HOME (git's other global file is $XDG_CONFIG_HOME/git/config), so the forge's
    git keeps its settings in the forge's own git home and never writes them into a git config of the person's, nor
    follows one (seen: an inherited GIT_CONFIG_GLOBAL took the forge's user.name)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_") and k != "XDG_CONFIG_HOME"}
    env["GITEA_WORK_DIR"] = p["forge"]
    return env


def gitea(p, *args):
    return run([running_bin(p), "--config", p["app_ini"], "--work-path", p["forge"], *args], env=forge_env(p))


# ---------------------------------------------------------------- G1.4 the two accounts

def set_up_misses(p, st):
    """M84 (G1.4): what the commands need of setup, the one check they all make and setup's own last act makes: the
    forge's port in the state and the mirror account's key on disk. What is missing, or "" when it is set up."""
    miss = ([] if st.get("port") else ["the forge's port"]) + ([] if os.path.exists(p["mirror_key"])
                                                                 else ["the mirror account's key"])
    return " and ".join(miss)


def person_name():
    n = re.sub(r"[^a-z0-9-]", "-", getpass.getuser().lower()).strip("-")[:30]
    return n if n and n not in ("admin", MIRROR_USER, "api", "user", "explore") else "owner"


def forge_users(p):
    out = gitea(p, "admin", "user", "list")
    names = []
    for line in lines_of(out.stdout)[1:]:
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
    lines = shown_lines(text)
    return redact(lines[-1].rstrip()) if lines else ""


def redact(text):
    return re.sub(r"(?i)(token|password|key)([=: '\"]+)\S+", r"\1\2<hidden>", text)


# ---------------------------------------------------------------- G1.2 and G1.6 the service

def unit_text(p):
    return f"""[Unit]
Description=Windstorm Mirror for GitHub: the forge (stock Gitea {GITEA_VERSION}, this computer only)

[Service]
Type=simple
{env_line()}ExecStart={sd_line(forge_argv(p))}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


# G1.24 (M148): every value in a service file is written as that manager's grammar takes one whole value, and what
# the manager parsed is read back word for word before anything is started (service_read_back).

def forge_argv(p):
    return [sys.executable, "-I", p["script"], "run-forge"]


def sync_argv(p):
    return [sys.executable, "-I", p["script"], "sync", "--from-timer"]


def service_env():
    """What the services carry in their environment: WSM_HOME, when setup ran with one."""
    h = os.environ.get("WSM_HOME")
    return [f"WSM_HOME={h}"] if h else []


# G1.24: what setup wrote into each service file, the read-back's "meant" side
SERVICE_MEANT = "service-meant.json"
# set by launchd itself in a loaded job's environment block, never by the person (seen 2026-11-25 on a Mac, macOS
# 27.2.1, a test account: `launchctl print` showed these two beside the job file's own variables)
LAUNCHD_OWN_ENV = ("OSLogRateLimit", "XPC_SERVICE_NAME")


def record_meant(p, which, argv):
    """Before setup writes a service file: what it writes there, kept in the skill's own folder, so the read-back
    compares with what setup wrote and never with what a later terminal's environment says (the family's rule 31
    (v.5) (3))."""
    path = os.path.join(p["data"], SERVICE_MEANT)
    held = load_json(path, {})
    held = held if isinstance(held, dict) else {}
    held[which] = {"argv": list(argv), "env": dict(e.split("=", 1) for e in service_env())}
    write_file(path, json.dumps(held, indent=1, ensure_ascii=False) + "\n", 0o600)


def read_meant(p, which):
    """(argv, {name: value}) setup wrote for the forge's ('forge') or the sync's ('sync') service, or None."""
    got = load_json(os.path.join(p["data"], SERVICE_MEANT), {})
    m = got.get(which) if isinstance(got, dict) else None
    try:
        argv, env = list(m["argv"]), dict(m["env"])
    except (TypeError, KeyError, ValueError):
        return None
    if not all(isinstance(w, str) for w in argv) or not all(isinstance(k, str) and isinstance(v, str)
                                                            for k, v in env.items()):
        return None
    return argv, env


# Seen 2026-11-25 on two stock Ubuntu 24.04 guests (systemd 255): an argument written as sd_word reached the started
# program whole for a space, two spaces, a leading space, %, %h, $, ${X}, ", \, a trailing \, ', ;, & < > and a
# non-ASCII letter, each alone and all at once, and the bus held each $ as $$ (expanded at start, not at parse). The
# program's own path takes no ", ' or \ (the unit is refused), and no one spelling carried every such path holding a $
# whole; SD_PATH_REFUSED is refused by setup before its first write, so the path's word needs only the percent sign
# doubled.
SD_PATH_REFUSED = "$\"'\\"
SD_PATH_NAMES = {"$": "a dollar sign", '"': "a double quote", "'": "an apostrophe", "\\": "a backslash"}


def sd_word(s):
    """One argument of a systemd start line: in double quotes, with the backslash, the double quote, the percent
    sign and the dollar sign each in systemd's own form for them."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def sd_line(argv):
    """The start line: the program's path (it holds none of SD_PATH_REFUSED), then each argument as sd_word."""
    return " ".join(['"' + argv[0].replace("%", "%%") + '"'] + [sd_word(w) for w in argv[1:]])


def sd_parsed_form(argv):
    """What systemd's bus holds for argv written by sd_line: the program, then argv, each argument's $ as $$."""
    return [argv[0], argv[0]] + [w.replace("$", "$$") for w in argv[1:]]


def env_line():
    """The service runs with the same home setup used. One Environment= word, quoted: systemd takes the backslash,
    the double quote and the percent sign there in its own forms, and no dollar sign."""
    return "".join('Environment="' + e.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"\n'
                   for e in service_env())


def xml_text(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def plist_strings(words):
    return "".join(f"<string>{xml_text(w)}</string>" for w in words)


def env_plist():
    env = service_env()
    if not env:
        return ""
    return ("  <key>EnvironmentVariables</key><dict>"
            + "".join(f"<key>{xml_text(k)}</key><string>{xml_text(v)}</string>" for k, v in (e.split("=", 1) for e in env))
            + "</dict>\n")


def plist_text(p):
    log = xml_text(os.path.join(p["forge"], "log", "launchd.log"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>{plist_strings(forge_argv(p))}</array>
{env_plist()}  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
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
{env_line()}ExecStart={sd_line(sync_argv(p))}
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
    log = xml_text(os.path.join(p["forge"], "log", "sync.log"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{SYNC_LABEL}</string>
  <key>ProgramArguments</key>
  <array>{plist_strings(sync_argv(p))}</array>
{env_plist()}  <key>StartInterval</key><integer>{minutes * 60}</integer>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


SD_DEST = "org.freedesktop.systemd1"
# seconds each read of a service manager is given; past it, it "could not be read" (the owner's ruling, 11-25)
BUS_WAIT = 10


def bus_json(*args):
    """One answer of the user manager over its bus (busctl's JSON form), its data, or None when it could not be read
    (an error is never an absence), within BUS_WAIT."""
    try:
        out = run(["busctl", "--user", "--json=short", *args], timeout=BUS_WAIT)
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)["data"]
    except (ValueError, KeyError, TypeError):
        return None


def systemd_version():
    """systemd's own version number as `systemctl --version` prints it on its first line (no bus needed), or ''."""
    try:
        out = run(["systemctl", "--version"], timeout=BUS_WAIT)
    except subprocess.TimeoutExpired:
        return ""
    m = re.match(r"systemd (\d+)\b", (out.stdout or "").split("\n")[0]) if out.returncode == 0 else None
    return m.group(1) if m else ""


BUS_ROAD = "and setup reads back over that bus what systemd takes from each service file it writes"


# The oldest systemd the skill was seen to run on: a stock Ubuntu 24.04 guest's, 2026-11-25 (setup, its read-back over
# the bus, a restart). An older one may hold everything setup needs; it was not seen.
OLDEST_SEEN_SYSTEMD = 255
# busctl's own refusal of the JSON form setup reads with, RECORDED on stock systemd 237 (Ubuntu 18.04) and 239
# (AlmaLinux 8.10), in every shell, with a bus too (the family's record of five systems, 2026-11-25): "busctl:
# unrecognized option '--json=short'". 245 and 249 answered the read in that form, as 255 does.
NO_JSON_FORM = re.compile(r"unrecognized option\W+--json")


def bus_floor(p):
    """G1.24 (the owner's ruling, 11-25: the read-back's road is a floor, and a floor is checked before the first
    write): on Linux, one harmless read over the user bus in busctl's JSON form, the manager's own version. None when
    it answers in that form; otherwise the line that stops setup and its dry run, with what was found and the one
    next step. What the tool can read itself decides first: a shell with neither XDG_RUNTIME_DIR nor
    DBUS_SESSION_BUS_ADDRESS has no road to the user bus (busctl said so in its own words there, seen on Ubuntu
    24.04, systemd 255.4, 2026-11-25). Then busctl's own answers as recorded: no bus (NO_USER_BUS), and its refusal
    of the JSON form (NO_JSON_FORM, an older systemd's). Any answer no one recorded says what was read, busctl's
    exit code, on every version, and nothing about a form unless JSON came back that could not be parsed: no line
    names a cause the tool did not read (the owner's ruling, 11-25)."""
    if system()[0] == "darwin":
        return None
    if not os.environ.get("XDG_RUNTIME_DIR") and not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return ("Stopped: this shell cannot reach your account's own systemd: it has neither XDG_RUNTIME_DIR nor "
                f"DBUS_SESSION_BUS_ADDRESS, the two that lead to its bus, {BUS_ROAD}. Setup made nothing. The one "
                "next step: run setup from a login of your own to this computer, such as ssh or a terminal at the "
                f"screen, which sets them: {own_command(p, 'setup')}")
    try:
        out = run(["busctl", "--user", "--json=short", "get-property", SD_DEST, "/org/freedesktop/systemd1",
                   SD_DEST + ".Manager", "Version"], timeout=BUS_WAIT)
    except subprocess.TimeoutExpired:
        return (f"Stopped: your account's own systemd did not answer over its bus within {BUS_WAIT} s, {BUS_ROAD}. "
                "Setup made nothing. The one next step: run setup again in a minute.")
    if out.returncode == 0:
        try:
            if isinstance(json.loads(out.stdout)["data"], str):
                return None
        except (ValueError, KeyError, TypeError):
            pass
        ver = systemd_version()
        return (f"Stopped: {'systemd ' + ver if ver else 'this computer' + chr(39) + 's systemd'} answered over your "
                f"account's bus in a form setup does not read (busctl's JSON form), {BUS_ROAD}. Setup made nothing. "
                "The one next step: open an issue with this line.")
    if NO_USER_BUS.search(out.stderr or "") or NO_USER_BUS.search(out.stdout or ""):
        return ("Stopped: this shell cannot reach your account's own systemd: busctl found no bus where XDG_RUNTIME_DIR "
                f"or DBUS_SESSION_BUS_ADDRESS leads, {BUS_ROAD}. Setup made nothing. The one next step: run setup "
                f"from a login of your own to this computer, such as ssh or a terminal at the screen: "
                f"{own_command(p, 'setup')}")
    if out.returncode == 127:
        return (f"Stopped: this computer has no busctl, systemd's own tool for its bus, {BUS_ROAD}. Setup made "
                "nothing. The one next step: open an issue with this line.")
    if NO_JSON_FORM.search(out.stderr or ""):
        ver = systemd_version()
        return (f"Stopped: {'this computer' + chr(39) + 's systemd is ' + ver if ver else 'this computer has a systemd'}"
                f" whose busctl does not know the JSON form setup reads with (busctl answered that the option is not "
                f"one it knows); the skill was seen to run on systemd {OLDEST_SEEN_SYSTEMD}, {BUS_ROAD}. Setup made "
                f"nothing. The one next step: set the skill up on a computer whose busctl answers in that form, such "
                f"as one with systemd {OLDEST_SEEN_SYSTEMD}, as the README says.")
    return (f"Stopped: busctl answered exit {out.returncode} when setup read your account's own systemd over its bus, "
            f"{BUS_ROAD}. Setup made nothing. The one next step: run setup from a login of your own to this computer, "
            "such as ssh or a terminal at the screen, and if this line comes back there, open an issue with this "
            f"line: {own_command(p, 'setup')}")


def dropin_count(which):
    """How many drop-in files stand on the job's unit, as the user manager lists them over its bus (DropInPaths):
    a number, or None when that could not be read. Only the count is kept: the files are the person's."""
    unit = (SERVICE if which == "forge" else SYNC_UNIT) + ".service"
    obj = bus_json("call", SD_DEST, "/org/freedesktop/systemd1", SD_DEST + ".Manager", "LoadUnit", "s", unit)
    if not (isinstance(obj, list) and len(obj) == 1 and isinstance(obj[0], str)):
        return None
    got = bus_json("get-property", SD_DEST, obj[0], SD_DEST + ".Unit", "DropInPaths")
    return len(got) if isinstance(got, list) else None


def unit_type(which):
    """The job's unit type as the user manager holds it (a drop-in may set it too): "oneshot", "simple", ..., or None
    when that could not be read."""
    unit = (SERVICE if which == "forge" else SYNC_UNIT) + ".service"
    obj = bus_json("call", SD_DEST, "/org/freedesktop/systemd1", SD_DEST + ".Manager", "LoadUnit", "s", unit)
    if not (isinstance(obj, list) and len(obj) == 1 and isinstance(obj[0], str)):
        return None
    got = bus_json("get-property", SD_DEST, obj[0], SD_DEST + ".Service", "Type")
    return got if isinstance(got, str) else None


def systemd_parsed(unit):
    """G1.24: ([each start line's words], [each Environment entry]) as the user manager holds them for unit, read
    over its bus one word at a time, never from the line `systemctl show` prints (it joins the words with spaces, so
    one word and two read the same); None when that could not be read."""
    obj = bus_json("call", SD_DEST, "/org/freedesktop/systemd1", SD_DEST + ".Manager", "LoadUnit", "s", unit)
    if not (isinstance(obj, list) and len(obj) == 1 and isinstance(obj[0], str)):
        return None
    ex = bus_json("get-property", SD_DEST, obj[0], SD_DEST + ".Service", "ExecStart")
    env = bus_json("get-property", SD_DEST, obj[0], SD_DEST + ".Service", "Environment")
    try:
        starts = [[e[0]] + list(e[1]) for e in ex]          # the program, then its argument list
        if not all(isinstance(w, str) for s in starts for w in s) or not all(isinstance(e, str) for e in env):
            return None
    except (TypeError, IndexError):
        return None
    return starts, list(env)


# The macOS versions launchctl print's form for a job was seen on (row M148): 27.2.1 on a Mac, a test account, and
# 15.7.9, 15.8.2, 26.6.1 and 26.7.2 on GitHub's standard runners with a made-up job, 2026-11-25 (tests/launchd-print).
MACOS_PRINT_SEEN = "15.7.9, 15.8.2, 26.6.1, 26.7.2 and 27.2.1"


def launchd_parsed(label):
    """G1.24: (the loaded job's arguments, one by one, {each environment name: its value}) as launchd holds them, read from
    `launchctl print` (seen 2026-11-25 on macOS MACOS_PRINT_SEEN, the same form: one argument to a line after two tabs inside
    "arguments = {", as written, spaces, &, <, a quote and a backslash alike; each variable as "NAME => value" inside
    "environment = {", beside the "inherited environment" and "default environment" blocks, which are not the
    job's); None when that could not be read, and "absent" when launchd has no such job. A print with no arguments
    block of that form (another macOS's) is None too: could not be read, never "differs" (the owner's ruling, 11-25).
    The environment is None when no block of that form stands (seen only for a job that sets variables: whether
    launchd prints one for a job that sets none is not seen), and service_read decides."""
    try:
        out = run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], timeout=BUS_WAIT)
    except subprocess.TimeoutExpired:
        return None
    if out.returncode == 113:                      # the family's rule 24: no such job
        return "absent"
    if out.returncode != 0:
        return None
    lines, args, env, block = out.stdout.split("\n"), None, None, None
    for line in lines:
        if line in ("\targuments = {", "\tenvironment = {"):
            block = line[1:].split(" ")[0]
            if block == "arguments":
                args = []
            elif env is None:
                env = {}
            continue
        if block and line == "\t}":
            block = None
            continue
        if block == "arguments" and line.startswith("\t\t"):
            args.append(line[2:])
        elif block == "environment" and line.startswith("\t\t") and " => " in line:
            k, v = line[2:].split(" => ", 1)
            env[k] = v
    if args is None:
        return None
    return args, env


def service_read(p, which):
    """G1.24: (answer, the names of the person's own environment entries, the parts that differ by name: "its start
    line" or "its arguments", "its WSM_HOME entry") for the forge's ('forge') or the sync's
    ('sync') job. answer: 'equal' when what the service manager parsed is, word for word, what setup wrote (its
    record, never this terminal's environment): the start line, and for each variable setup set exactly one entry
    with its value; 'differs'; 'unread' when what the manager holds could not be read; 'unrecorded' when setup's own
    record of what it wrote is not there or not readable (the owner's ruling, 11-25: not a bus that did not answer);
    'absent' (a Mac) when launchd holds no such job. An entry for a variable setup did not set is the person's own
    (a drop-in of theirs):
    never a fault of ours, and only its name is kept (rule 31 (v.5) (3): it may hold a password). So is a start line
    of theirs on a one-shot unit beside ours, where ours is held once, as written, and first (the same rule, 4:22 PM
    ET 11-25): the list holds them in order, ours first, and that the manager runs them in that order is recorded
    on five systems, the user manager (4:52 PM ET); a line still says only what the tool read. It is kept by its
    count alone, a number at the end of the names (a start line can hold a secret as a value can)."""
    meant = read_meant(p, which)
    if system()[0] == "darwin":
        got = launchd_parsed(LAUNCHD_LABEL if which == "forge" else SYNC_LABEL)
        if got in (None, "absent"):
            return ("unread" if got is None else "absent"), [], []
        if meant is None:
            return "unrecorded", [], []
        args, env = got
        if env is None:                 # no environment block of the seen form: another form where setup set some
            if meant[1]:
                return "unread", [], []
            env = {}
        theirs = sorted(k for k in env if k not in meant[1] and k not in LAUNCHD_OWN_ENV)
        parts = (["its arguments"] if args != meant[0] else []) + [
            f"its {k} entry" for k, v in meant[1].items() if env.get(k) != v]
        return ("differs" if parts else "equal"), theirs, parts
    if meant is None:
        return "unrecorded", [], []
    got = systemd_parsed((SERVICE if which == "forge" else SYNC_UNIT) + ".service")
    if got is None:
        return "unread", [], []
    starts, entries = got
    pairs = [e.split("=", 1) if "=" in e else [e, None] for e in entries]
    theirs, parts, ours = sorted({k for k, _ in pairs if k not in meant[1]}), [], sd_parsed_form(meant[0])
    if starts != [ours]:
        # Ours not held, held changed, held more than once or not first is a stop; a start line of theirs on a unit of
        # another type is seen by no one, so it is a stop too (rule 31 (v.5) (3), its LIMIT): the forge's is written
        # simple, and only the sync's, written one-shot and held so by the manager, may carry theirs.
        typ = unit_type(which) if which == "sync" and starts[:1] == [ours] and starts.count(ours) == 1 else ""
        if typ is None:                             # the type could not be read: a read that failed, never a fault
            return "unread", [], []
        if typ == "oneshot":
            theirs.append(len(starts) - 1)
        else:
            parts.append("its start line")
    for name, want in meant[1].items():
        held = [v for k, v in pairs if k == name]
        # the count where setup wrote one entry, and no more (never which one wins). More than one was produced by
        # none of the five systemd versions the family tried (rule 31 (v.5) (3), 4:03 PM ET 11-25): a stop kept for
        # a form not seen, said by its count; no promise leans on it.
        if held != [want]:
            parts.append(f"its {name} entry" if len(held) == 1 else f"its {name} entry ({len(held)} found where "
                         "setup wrote one)")
    return ("differs" if parts else "equal"), theirs, parts


def service_read_back(p, which):
    """G1.24: service_read's answer alone."""
    return service_read(p, which)[0]


def theirs_line(p, reads=None):
    """The family's rule 31 (v.5) (3): environment entries the person set in the skill's services themselves, named
    (never their values) as theirs, a warning: '' when there are none or nothing could be read. reads: service_read's
    answers already read, by job, so nothing is read twice (the owner's ruling, 11-25)."""
    names, starts = [], {}
    for which in ("forge", "sync"):
        for n in (reads[which] if reads else service_read(p, which))[1]:
            if isinstance(n, int):
                starts[which] = n
            elif n not in names:
                names.append(n)
    if not names and not starts:
        return ""
    said = []
    if names:
        said.append(f"Your own setting is in the skill's service too, set by you, not by this tool: "
                    f"{shown_names(names)}. It is left as it is; this tool reads only the values it set itself.")
    for which, n in starts.items():               # rule 31 (v.5) (3): by its count, never its words; the listing
        unit = (SERVICE if which == "forge" else SYNC_UNIT) + ".service"
        it, is_ = ("them", "they are") if n != 1 else ("it", "it is")
        said.append(f"{n} start line{'s' if n != 1 else ''} of your own {'are' if n != 1 else 'is'} in the {which}'s "
                    f"service, set by you, not by this tool; {'they stand' if n != 1 else 'it stands'} after the "
                    f"skill's own in systemd's list, and {is_} left as {is_}. To read {it}: systemctl --user cat {unit}")
    return " ".join(said)          # a command, where one is given, stands last (rule 31 (v.5) (3))


COMES_BACK = ("if this line comes back after that, run uninstall, which takes its files away, and open an issue with "
              "this line")


def read_back_step(which, answer, where="setup"):
    """G1.24 (the owner's ruling, 11-25: nobody is sent to uninstall over their own setting, or over the moment's):
    the one next step after read_back_words. First what may be the person's own or the moment's; uninstall and an
    issue only if the line comes back after that."""
    if answer == "unrecorded":
        return "Run setup again, which writes that record again and reads the service back; " + COMES_BACK
    if answer == "unread":
        if system()[0] == "darwin":
            # Seen on the macOS versions in MACOS_PRINT_SEEN only (the owner's rulings, 11-25: the README names
            # them): another macOS may print the job in a form not read here, so this answer may come back; never a
            # road to uninstall.
            first = ("Run status from a terminal window of your own" if where == "status" else
                     "Run setup again in a minute")
            return (f"{first}; if this line comes back, launchd's answer on this Mac may be in a form this tool does "
                    f"not read yet (it was seen on macOS {MACOS_PRINT_SEEN}): open an issue with this line and this "
                    "Mac's version, "
                    "which this prints: sw_vers -productVersion")
        if where == "status":
            return ("Run status from a terminal window of your own; if this line comes back there, open an issue "
                    "with this line")
        return ("Run setup again (setup twice is one setup: what was done is read back, never done twice); "
                + COMES_BACK)
    if system()[0] == "darwin":
        return "Run setup again, which writes it again and reads it back; " + COMES_BACK
    unit = (SERVICE if which == "forge" else SYNC_UNIT) + ".service"
    n = dropin_count(which)
    if n:
        # The family's rule 31 (v.5) (3), 4:03 PM ET 11-25: a drop-in that sets a variable the tool set, or a start
        # line, is the person's own; what it holds is never said. Setup again would write the same file under the
        # same drop-in, so the step is the manager's own listing, never uninstall or setup again.
        return (f"{n} drop-in file{'s' if n != 1 else ''} stand{'s' if n == 1 else ''} on it beside the skill's own "
                "file: a setting there is not this tool's, so it is left as it is. The one next step: read the "
                "manager's own listing of it and its drop-ins, and take off by your own hand the setting that changes "
                f"it: systemctl --user cat {unit}")
    return ("A setting of your own that changes it comes off first, then run setup again; " + COMES_BACK
            + f". Every file systemd read for it, yours too: systemctl --user cat {unit}")


def not_set_up(answer):
    """What status and the restart line say was done about a read-back that is not equal: a parse that could not be
    read is never called not set up (the owner's ruling, 11-25), only not said to be."""
    return "it is not said to be set up" if answer == "unread" else "it is not set up"


def read_back_words(which, answer, then, step=None, parts=()):
    """What setup and status say when the read-back is not 'equal' (the family's rule 31 (v.5): THAT a value is not
    read as setup wrote it, never what it is read as instead, and no value the tool did not write: the parts are
    named, never their values). then: what was done about it ("it was not started"); step: the one next step, for
    where it is said (read_back_step's for setup when not given)."""
    step = step if step is not None else read_back_step(which, answer)
    job = "the forge's service" if which == "forge" else "the sync's service"
    mgr = "launchd" if system()[0] == "darwin" else "systemd"
    if answer == "differs":
        named = " and ".join(parts) if parts else "a value in it"
        verb = "are" if len(parts) > 1 or list(parts) == ["its arguments"] else "is"
        return (f"{mgr} does not hold {job} as setup wrote it: {named} {verb} not read as setup wrote it, so "
                f"{then}. {step}")
    if answer == "unrecorded":
        return f"what setup wrote for {job} could not be read from setup's own record, so {then}. {step}"
    return f"whether {mgr} holds {job} as setup wrote it could not be read, so {then}. {step}"


def launchd_checked(p, which, label, out):
    """G1.24 on a Mac: a job launchd loaded is read back; one that is not what setup wrote is taken off again at
    once. (True, '') or (False, the words)."""
    back, _, parts = service_read(p, which)
    if back == "equal":
        return True, ""
    back = back if back in ("differs", "unrecorded") else "unread"
    answer, waited, code = launchd_off(p, label)        # G1.17: off by the one function, and read gone
    if answer == "gone":
        return False, read_back_words(which, back, "it was taken off again, and launchd reads it gone", parts=parts)
    seen = (f"launchd still had it {waited:.0f} s later" if answer == "there" else
            f"whether launchd let go of it could not be read (launchctl print answered code {code})")
    again = "Run setup again in a minute, which takes it off first and reads it gone; " + COMES_BACK
    return False, read_back_words(which, back, f"it was told to come off again, and {seen}", again, parts)


def install_timer(p, minutes):
    if system()[0] == "darwin":
        path = os.path.join(os.path.dirname(p["plist"]), SYNC_LABEL + ".plist")
        record_meant(p, "sync", sync_argv(p))
        write_file(path, sync_plist_text(p, minutes))
        out, words = launchd_load(p, SYNC_LABEL, path)
        if out is None:
            return False, words
        if out.returncode != 0:
            return False, (f"this Mac did not load the sync's launch agent (code {out.returncode}); run setup again, "
                           "and if it stops here again, run status")
        return launchd_checked(p, "sync", SYNC_LABEL, out)
    base = os.path.dirname(p["unit"])
    record_meant(p, "sync", sync_argv(p))
    write_file(os.path.join(base, SYNC_UNIT + ".service"), sync_unit_text(p))
    write_file(os.path.join(base, SYNC_UNIT + ".timer"), sync_timer_text(minutes))
    run(["systemctl", "--user", "daemon-reload"])
    back, _, parts = service_read(p, "sync")           # G1.24: read back before anything is switched on
    if back != "equal":
        return False, read_back_words("sync", back, "its timer was not switched on", parts=parts)
    if run(["systemctl", "--user", "enable", "--now", SYNC_UNIT + ".timer"]).returncode == 0:
        return True, ""
    return False, "systemd did not switch the sync's timer on; run setup again, and if it stops here again, run status"


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
    vals = dict(line.split("=", 1) for line in lines_of(out.stdout) if "=" in line)
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
             f"command: {LINGER_COMMAND.format(user=shlex.quote(user))} ({LINGER_MEANS}).")
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
                           f"refuses, its administrator runs one command: {LINGER_COMMAND.format(user=shlex.quote(user))}. "
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
            for line in lines_of(out.stdout) if out.returncode == 0 else []:
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
LFS_UNREAD = ("whether it keeps files with Git LFS could not be read from its .gitattributes, so its copy was not "
              "refreshed; the next sync tries again")
NOT_TEXT = ("{n} branch or tag name(s) on GitHub are not UTF-8 text, which the forge cannot hold, so they are not in "
            "the copy ({names}); every other branch and tag is copied")
NOT_AS_LISTED = ("{n} branch or tag name(s) in the copy would not be what GitHub lists ({names}), so the copy was not "
                 "moved; it stays as it was until a sync shows every name at GitHub's commit")
FORGE_NOT_AS_LISTED = ("after the push, {n} branch or tag name(s) in the copy in the forge are not what GitHub lists "
                       "({names}), so it is not called whole; the next sync tries again")
KEPT_CLASH = ("GitHub holds {n} branch(es) named kept/... ({names}), the name this version gives the copy's own kept "
              "refs, so it copies such a project once and cannot refresh it; nothing in its copy was changed. To have "
              "it refreshed again, give that branch another name on GitHub; the copy as it was stays in your forge")
RESTORE_HIDDEN = ("{n} of those name(s) hold a character this tool does not print as it is (each such character is put "
                  "as a ?), so no line is given for them here; this line lists every kept/ name the copy holds, as git "
                  "holds it:")
RESTORE_LACKS = ("but {n} branch or tag name(s) GitHub held when the sync last read its list are not in the new home, "
                 "because the copy in your forge does not hold them ({names}){why}. Run restore --check, which names "
                 "each difference; a sync brings over what has changed on GitHub since, where the forge can hold it.")
RESTORE_LIST_UNREAD = ("but what GitHub held when the sync last read its list could not be read from this computer's record "
                       "of {full}, so whether the new home lacks a branch or tag GitHub held is not known. Run sync, then "
                       "restore --check, which compares the copy with that list.")
RESTORE_TO_HIDDEN = ("The new home's address holds a character this tool does not print as it is, so no line is given "
                     "to bring them over here; this line lists every kept/ name the copy holds, as git holds it:")
RESTORE_LEFT_OUT = ("but {n} GitHub branch(es) named kept/..., which the copy in your forge holds, are not in the new "
                    "home ({names}): such a branch shares its name with the copy's own kept refs, so restore --to does "
                    "not bring it back yet. To bring them over yourself:")
TAG_NOT_KEPT = ("{full}: {short} was {what} on GitHub; it pointed at {at}, not a commit, so nothing of it could be "
                "kept (a kept ref is a branch, and a branch holds only a commit)")
LISTING_UNREAD = ("GitHub's list of its branches and tags could not be read ({why}), so its copy was not refreshed and is "
                  "not called whole; nothing in it is called wrong, and the next sync tries again")
UNFORESEEN = ("the tool met something in this project it was not built for, so its copy was not refreshed; every other "
              "project went on, and the next sync tries again")


def lfs_missing():
    return ("git lfs is not installed here, so projects with large files are copied without them until it is "
            f"({install_words('git-lfs')}; setup and sync find it by themselves once it is installed).")


MAC_NO_SESSION = ("this account is not logged in at this Mac's screen, and on a Mac the forge starts only in a "
                  "login session at the screen; it starts once this account is. Log in to this account at this "
                  "Mac's screen, then run setup again")


LAUNCHD_WAIT = 30      # seconds launchd is given to let go of a job it was told to take off
# What `launchctl print gui/<uid>` answers in an account that is not logged in at the Mac's screen: seen 2026-11-24
# over ssh on macOS 13.7.8 (x86_64), a test account with no desktop process; the job's own print and bootout
# answered 125 there too. Only this code is read as "no domain, so nothing of this account is loaded".
LAUNCHD_NO_SCREEN = 125


def mac_no_screen():
    """M143 (G1.16): True only on a Mac whose account's own domain answers LAUNCHD_NO_SCREEN to `launchctl print
    gui/<uid>` (seen with no session at the screen: 2026-11-24 on macOS 13.7.8, 2026-11-25 on macOS 27.2.1). Any
    other answer, or none, is False, so every line built on it stays as it was; off a Mac nothing is read."""
    return system()[0] == "darwin" and run(["launchctl", "print", f"gui/{os.getuid()}"]).returncode == LAUNCHD_NO_SCREEN


def forge_no_screen(p):
    """M143 (G1.16): the cause and the one step for a forge that does not answer while this account has no session
    at the Mac's screen, a step that works there. Setup not finished (the sync's job file not written): log in at
    the screen, then setup again. Both jobs' files in place (a restart, or a logout, since setup): the login is the
    step, and status read after it says what is still owed; that the jobs load by themselves at the login is not
    said, because it has not been seen."""
    sync = os.path.join(os.path.dirname(p["plist"]), SYNC_LABEL + ".plist")
    if os.path.exists(p["plist"]) and os.path.exists(sync):
        return ("this account is not logged in at this Mac's screen, and on a Mac the forge runs only in a login "
                "session at the screen. Log in to this account at this Mac's screen, then run status again; if the "
                f"forge still does not answer there, run setup again: {own_command(p, 'setup')}")
    return f"{MAC_NO_SESSION}: {own_command(p, 'setup')}"


def launchd_off(p, label):
    """G1.17 (seen 2026-11-24 on a Mac, macOS 27.2.1: setup run while the forge ran answered "code 5" and left it
    unloaded): take the job off and read it gone. 'gone' when `launchctl print` answers 113 (the family's rule 24),
    'there' when it still names the job at LAUNCHD_WAIT, 'unread' when it answered neither; with the seconds waited
    and the last code. The wait is written in the forge's own log folder, launchd.log, when that folder is there.
    The account's domain is read first: answered LAUNCHD_NO_SCREEN, the job is gone with no wait (0 reads)."""
    job = f"gui/{os.getuid()}/{label}"
    start, reads = time.monotonic(), 0
    if run(["launchctl", "print", f"gui/{os.getuid()}"]).returncode == LAUNCHD_NO_SCREEN:
        answer, code = "gone", LAUNCHD_NO_SCREEN        # no domain at the screen: nothing of this account is loaded
    else:
        run(["launchctl", "bootout", job])
        answer = None
    while answer is None:
        out = run(["launchctl", "print", job])
        reads += 1
        code = out.returncode
        answer = "gone" if code == 113 else ("there" if code == 0 and out.stdout.strip() else "unread")
        if answer != "gone" and time.monotonic() - start < LAUNCHD_WAIT:
            answer = None
            time.sleep(0.2)
    waited = time.monotonic() - start
    log = os.path.join(p["forge"], "log")
    if os.path.isdir(log):
        try:
            with open(os.path.join(log, "launchd.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {label}: {answer} after "
                        f"{waited:.1f} s, {reads} read(s), last code {code}"
                        f"{' (no domain at the screen)' if reads == 0 else ''}\n")
        except OSError:
            pass
    return answer, waited, code


def launchd_not_off(label_words, answer, waited, code, again):
    """The words for a job launchd did not let go of: the true state and the one next step."""
    if answer == "there":
        return (f"launchd still had {label_words} {waited:.0f} s after it was told to take it off, so it was not "
                f"loaded again. Run {again} again in a minute, and if it stops here again, run status")
    return (f"whether launchd let go of {label_words} could not be read (launchctl print answered code {code}), so "
            f"it was not loaded again. Run {again} again, and if it stops here again, run status")


def launchd_load(p, label, plist):
    """Off and read gone first (above), then loaded; (CompletedProcess of the load, or None, and the words when it
    was not loaded because launchd did not let go of the old job)."""
    answer, waited, code = launchd_off(p, label)
    if answer != "gone":
        words = "the forge's job" if label == LAUNCHD_LABEL else "the sync's job"
        return None, launchd_not_off(words, answer, waited, code, "setup")
    return run(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist]), ""


SYSTEMD_WAIT = 30      # seconds systemd is given to stop a unit uninstall told it to stop
# systemctl --user's words in a shell with no user bus: "Failed to connect to bus: No medium found" (systemd 255, a
# stock Ubuntu 24.04, 2026-11-24) and "Failed to connect to user scope bus via local transport: ..." (systemd 262,
# another skill's log the same day). busctl's, recorded 2026-11-25 on Ubuntu 24.04, systemd 255.4, with
# XDG_RUNTIME_DIR or DBUS_SESSION_BUS_ADDRESS set and no bus where it leads: "Failed to connect to bus: No such file
# or directory", and "...: Connection refused" for an address that is not one. "Failed to connect to", then "bus" in
# the same line. (With neither set, busctl says "Failed to set bus address: ...", read by bus_floor from the two
# variables themselves, not from words.)
NO_USER_BUS = re.compile(r"Failed to connect to\b[^:\n]*\bbus\b")
SYSTEMD_UNITS = ((None, "the forge's service"), (".service", "the sync's service"), (".timer", "the sync's timer"))
SYSTEMD_REFUSED = []    # (unit, systemctl's first line) for each unit whose disable systemd refused at uninstall


def systemd_off(p, only=None):
    """G1.18 (M107, seen 2026-11-24 on a stock Ubuntu 24.04: uninstall run from a shell with no user bus, after
    `su -` from root and with XDG_RUNTIME_DIR unset, said "Gone now" and ended 0 while systemd still ran the forge
    from its removed program): tell systemd to stop the skill's units, then read each stopped. Per unit (its words,
    'stopped' | 'running' | 'unread', seconds waited, what systemctl answered): 'stopped' when its ActiveState reads
    inactive or failed; 'running' when it still reads active (or on its way) at SYSTEMD_WAIT; 'unread' at once when
    systemctl --user does not answer (no user bus in this shell) or answers no state. M163: `only`, when given, is
    the units (SYSTEMD_UNITS' suffixes) uninstall showed to be this home's; no other is told off or read."""
    pick = [(x, w) for x, w in SYSTEMD_UNITS if only is None or x in only]
    told = [SERVICE if x is None else SYNC_UNIT + x for x, _ in pick if x != ".service"]
    del SYSTEMD_REFUSED[:]
    if told:
        off = run(["systemctl", "--user", "disable", "--now", *told])
        if off.returncode != 0:
            # Seen 2026-11-26 on systemd 255 (a stock Ubuntu 24.04 guest, the forge's unit file at mode 000 while
            # systemd held it loaded): disable refuses the whole call ("Failed to disable unit: Unit file ... does
            # not exist.", code 1) and stops nothing, the timer in the same call too; stop needs no file. So each
            # unit is told off alone, and one whose disable is refused is stopped, its refusal kept in its own words.
            for u in told:
                one = run(["systemctl", "--user", "disable", "--now", u]) if len(told) > 1 else off
                if one.returncode != 0:
                    said = [x.strip() for x in shown_lines(one.stderr or one.stdout)]
                    SYSTEMD_REFUSED.append((u, said[0] if said else f"code {one.returncode}"))
                    run(["systemctl", "--user", "stop", u])
    seen = []
    for suffix, words in pick:
        unit = SERVICE if suffix is None else SYNC_UNIT + suffix
        start = time.monotonic()
        while True:
            out = run(["systemctl", "--user", "show", unit, "-p", "ActiveState"])
            state = dict(x.split("=", 1) for x in lines_of(out.stdout) if "=" in x).get("ActiveState", "").strip()
            if out.returncode != 0 or not state:
                said = [x.strip() for x in shown_lines(out.stderr or out.stdout)]
                seen.append((words, "unread", time.monotonic() - start,
                             f"code {out.returncode}" + (f": {said[0]}" if said else "")))
                break
            if state in ("inactive", "failed"):
                seen.append((words, "stopped", time.monotonic() - start, state))
                break
            if time.monotonic() - start >= SYSTEMD_WAIT:
                seen.append((words, "running", time.monotonic() - start, state))
                break
            time.sleep(0.2)
    return seen


_setup_held = [False]           # M159: setup holds the sync's lock (main lets it go when setup ends)


def install_service(p, restart=False):
    record_meant(p, "forge", forge_argv(p))
    if system()[0] == "darwin":
        if restart:             # M159 limit (2): the loaded job is read as this home's own before it is replaced
            got = launchd_held(LAUNCHD_LABEL)
            if got is None or (got != "absent" and not job_is_ours(p, LAUNCHD_LABEL, got[0])):
                return False, (f"its settings file {p['app_ini']} is written new, and the forge was not started "
                               f"again with it: what launchd holds for the forge's job ({LAUNCHD_LABEL}) "
                               + ("could not be read" if got is None else "is not this home's own copy")
                               + ", so setup takes nothing off. Run setup again; if it stops here again, run status")
        write_file(p["plist"], plist_text(p))
        uid = str(os.getuid())
        # G1.16 (M58): a launch agent loads only into a login session at the screen. Over ssh into an account that
        # is not logged in there, nothing can start: said in the person's words, never launchctl's.
        if run(["launchctl", "print", f"gui/{uid}"]).returncode != 0:
            return False, MAC_NO_SESSION
        out, words = launchd_load(p, LAUNCHD_LABEL, p["plist"])         # G1.17: setup again, while the forge runs
        if out is None:
            return False, words
        if out.returncode != 0:
            return False, (f"this Mac did not load the forge's launch agent (code {out.returncode}); run setup "
                           "again, and if it stops here again, run status")
        return launchd_checked(p, "forge", LAUNCHD_LABEL, out)
    write_file(p["unit"], unit_text(p))
    run(["systemctl", "--user", "daemon-reload"])
    back, _, parts = service_read(p, "forge")          # G1.24: read back before anything is started
    if back != "equal":
        return False, read_back_words("forge", back, "it was not started", parts=parts)
    if restart:                 # M159: a forge still running from the settings file as it was reads the new one
        # the owner's limit (2), 11-26: only the skill's own forge service, read as this home's (M163's read) just
        # before; any other is never stopped, and that is said
        held = systemd_parsed(SERVICE + ".service")
        if held is None or not unit_is_ours(p, "forge", held[0]):
            return False, (f"its settings file {p['app_ini']} is written new, and the forge was not started again "
                           "with it: what systemd runs for the forge's service ("
                           + (f"{SERVICE}.service) could not be read" if held is None else
                              f"{SERVICE}.service) is not this home's own copy") + ", so setup stops nothing. Run "
                           "setup again; if it stops here again, run status")
        run(["systemctl", "--user", "stop", SERVICE])
    out = run(["systemctl", "--user", "enable", "--now", SERVICE])
    return out.returncode == 0, last_line(out.stderr)


# ---------------------------------------------------------------- G2 the GitHub keys

GITHUB_API = "https://api.github.com"
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


# G9.12 (the family's rule 27): four settings exist only for the suite. Each needs TWO keys: the setting, and the marker
# the suite writes in the temporary home it made (WSM_HOME). The setting alone is found and ignored: nothing is
# replaced and the command goes on against GitHub itself. Even with both keys, the two GitHub addresses are taken
# only on this computer's loopback, so no setting ever sends a key to another host.
# The family's rule 27 keeps three lists of what the tool reads from its environment; a test fails on any other name.
PERSON_SETTINGS = ("WSM_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME")     # named in the README; said when they move a folder
HOST_FACTS = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")            # bus_floor: whether each is set, never its value
OWN_MARKERS = ()                                                       # none
TEST_SETTINGS = ("WSM_GITHUB_API", "WSM_GITHUB_GIT", "WSM_PAGE_SIZE", "WSM_GITEA_BASE")
TEST_SETTING_DEFAULT = {"WSM_GITHUB_API": "GitHub itself", "WSM_GITHUB_GIT": "GitHub itself",
                        "WSM_PAGE_SIZE": "GitHub's largest page", "WSM_GITEA_BASE": "Gitea's own download site"}
SUITE_MARKER = "windstorm-mirror-suite-home"
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def suite_home():
    h = os.environ.get("WSM_HOME")
    return bool(h) and os.path.isfile(os.path.join(h, SUITE_MARKER))


def on_loopback(url):
    try:
        u = urllib.parse.urlsplit(url)
        return u.scheme in ("http", "https") and (u.hostname or "").lower() in LOOPBACK
    except ValueError:
        return False


def test_setting(name):
    """The value of a test-only setting when both keys are there (and, for an address, it is loopback); else None."""
    v = os.environ.get(name)
    if not v or not suite_home():
        return None
    if name in ("WSM_GITHUB_API", "WSM_GITHUB_GIT") and not on_loopback(v):
        return None
    return v


def settings_lines():
    """What each command says first about the settings it reads: (lines, stop). WSM_HOME is a person's setting and
    is named; a test-only setting without the suite's marker is found and ignored; a GitHub address that is not
    this computer's loopback, with the marker there, stops the command before it does anything."""
    lines, stop = [], False
    h = os.environ.get("WSM_HOME")
    if h:
        shown, note = shown_one(h)
        lines.append(f"WSM_HOME is set: this skill's folders are under {shown}{note}.")
    elif system()[0] == "linux":
        home = os.path.expanduser("~")
        for name, usual, what in (("XDG_DATA_HOME", os.path.join(home, ".local", "share"), "data"),
                                  ("XDG_CONFIG_HOME", os.path.join(home, ".config"), "settings and service")):
            v = os.environ.get(name)
            if v and os.path.abspath(v) != usual:
                shown, note = shown_one(v)
                lines.append(f"{name} is set: this skill's {what} folders are under {shown}{note}, not {usual}.")
    for name in TEST_SETTINGS:
        v = os.environ.get(name)
        if not v:
            continue
        if not suite_home():
            lines.append(f"{name} was found and ignored: it is read only inside the test suite's own home; this "
                         f"command goes on with {TEST_SETTING_DEFAULT[name]}, and it is not handed on.")
            os.environ.pop(name)        # nothing it starts (git, the forge) is handed it either
        elif name in ("WSM_GITHUB_API", "WSM_GITHUB_GIT") and not on_loopback(v):
            lines.append(f"Stopped: {name} names an address that is not this computer's own (127.0.0.1, localhost "
                         "or ::1), and no key is ever sent there. Nothing was done. Unset it and run the command "
                         "again.")
            stop = True
    return lines, stop


def github_api():
    return test_setting("WSM_GITHUB_API") or GITHUB_API


def gh_get(key, path):
    """One read from GitHub's REST API with one owner's key. Returns (status, headers, body). Never writes."""
    api = github_api()
    url = path if path.startswith("http") else api + path
    if urllib.parse.urlsplit(url)[:2] != urllib.parse.urlsplit(api)[:2]:
        return 0, {}, {"message": f"not asked: {urllib.parse.urlsplit(url).netloc} is not GitHub's API, and a key "
                                  "goes only there"}
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


# M156 (G6.23): the form of the key the pinned Gitea gives the mirror account (seen 2026-11-26 on Gitea 28.4.0 in a
# made home: 40 lowercase hexadecimal characters, one line); a kept key of another form was cut short or changed.
FORGE_KEY_FORM = re.compile(r"^[0-9a-f]{40}$")


def key_file_read(path, form=None):
    """M155, M156 (G6.22, G6.23): a kept key file's three answers: ('read', the key), ('not there', None), or ('could
    not be read', why). The why names the error's class or what is wrong with its form, never a byte of it."""
    answer, data = open_kept(path)
    if answer != "read":
        return answer, data
    try:
        key = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        return "could not be read", "it is not text (UnicodeDecodeError)"
    if not key:
        return "could not be read", "it is empty"
    if any(c.isspace() for c in key):
        return "could not be read", "it is not one line"
    if form is not None and not form.match(key):
        return "could not be read", "it is not a key of the form the forge gives (cut short, or changed by hand)"
    return "read", secret(key)


def key_unread_line(p, owner, kp, why):
    return (f"{owner}: its key file {kp} could not be read: {why}, so nothing of {owner}'s was asked of GitHub. The "
            f"one next step: run key set {owner} again, pasting the key when it asks: "
            f"{own_command(p, 'key', 'set', owner)}")


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
    if not load_state(p).get("port"):
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
    kp = key_path(p, owner)
    if os.path.isdir(kp) and not os.path.islink(kp):
        # M155 (seen 2026-11-26 in a stand-in trial: a traceback, and a file of its own left beside it): a folder
        # where the key file belongs is never written into or removed; it is said, with its step
        say(f"Stopped: {kp} is a folder, where the key file for {owner} belongs, so the key cannot be kept there. "
            f"Nothing was changed. The one next step: move that folder out of {os.path.dirname(kp)} (into a folder "
            f"of your own, to keep it), then run key set {owner} again: {own_command(p, 'key', 'set', owner)}")
        return 2
    old_answer, old_why = key_file_read(kp)
    write_private(kp, key + "\n")
    if old_answer == "could not be read":       # M155: the person's own typed key replaces it, and that is said
        say(f"The key file it replaced could not be read ({old_why}); it holds the key you typed now.")
    s = load_settings(p)
    s.setdefault("owners", {})[owner] = {"kind": kind}
    write_file(p["settings"], json.dumps(s, indent=1) + "\n", 0o600)
    say(f"The key for {owner} ({'your account' if kind == 'user' else 'organisation'}) is kept in "
        f"{key_path(p, owner)}, readable by you only. It is used for {owner}'s repositories and nothing else.")
    if not load_state(p).get("go"):
        say("Once a key is kept for every owner to copy, the go for the first copy is yours, here in this window: run "
            f"{own_command(p, 'go')}. It shows what will be copied and waits for your Enter.")
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


ADDRESS_UNREAD = ("the address git would use for GitHub could not be read ({why}), so nothing was fetched and this copy "
                  "was not refreshed. This command shows the git setting it stops at; with that line mended, the "
                  "next sync refreshes it:\n    git config --global --list")
REWRITTEN = ("your git settings send GitHub's address for this repository to {where} (a url.<...>.insteadOf line in "
             "your git config, for this repository's whole address); the mirror reads GitHub only over HTTPS with its "
             "read-only key, so this copy was not refreshed. This command lists those lines; with that one taken out, "
             "the next sync refreshes it:\n    git config --global --get-regexp insteadof")


def github_git():
    return test_setting("WSM_GITHUB_GIT") or GITHUB_GIT


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
    v = test_setting("WSM_PAGE_SIZE") or ""
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


# ---------------------------------------------------------------- G8.7 the forge's door (M102)
# Nothing that carries the forge's key or a copy goes to 127.0.0.1:<port> until this computer has said who holds
# that port. Three answers, and a fourth for no listener at all: "mine" (every listener on the port is of this
# account AND is this mirror's own forge: our binary, started on our settings file), "other" (certainly not: another
# account's, or a program of this account that is not our forge), "unread" (the computer did not say: a table not
# there or with columns this release does not know, a process gone between two reads). Only "mine" is sent to; an
# "other" is never said on a guess, since setup acts on it. Every road reaches the port through Forge.call,
# forge_call or wait_forge, and each reads the door first (the suite lists every caller).
# On "none" nothing is sent either (the owner's ruling F1): no listener is exactly when another program can take the
# number, so a request then could only fail or reach a stranger; it is answered as no answer, from the read itself.
# THE EDGE, written down and not solved (the owner's ruling, 11-24): one case only. Our own forge, read as "mine",
# dies while a call is under way (a push makes more than one connection, and a read is reused for up to
# DOOR_FRESH_S seconds), and another program takes its number in that moment. The port is picked below the system's
# short-lived range, so the system never hands it out by itself; only a program that asks for it by name can.

DOOR_FRESH_S = 2.0
DOOR_SEEN = {}
PROC_TCP = ("/proc/net/tcp", "/proc/net/tcp6")
LINUX_TCP_HEADS = ("sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode",
                   "sl local_address remote_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode")
# netstat -anv -p tcp on macOS, by its header: only the headers read on a real Mac are known; any other = unread.
# Read 2026-11-24 on macOS 27.2.1 arm64 (process:pid) and on macOS 13.7.8 Intel (pid, then epid).
MAC_NETSTAT_HEADS = (
    "Proto Recv-Q Send-Q Local Address Foreign Address (state) rxbytes txbytes rhiwat shiwat process:pid state "
    "options gencnt flags flags1 usecnt rtncnt fltrs",
    "Proto Recv-Q Send-Q Local Address Foreign Address (state) rhiwat shiwat pid epid state options gencnt flags "
    "flags1 usscnt rtncnt fltrs",
)
NETSTAT = ["netstat", "-anv", "-p", "tcp"]


class DoorUnread(Exception):
    """Who holds the port could not be read. never=True: not on this computer at all (the read itself is not
    there); never=False: not this time."""

    def __init__(self, never, why):
        super().__init__(why)
        self.never = never


class DoorShut(HardStop):
    """The forge's port is held by another program, or who holds it could not be read: nothing was sent."""

    def __init__(self, kind, line, never=False):
        super().__init__(line)
        self.kind, self.never = kind, never


def linux_listeners(port, files=None):
    """[(uid, inode)] of every TCP listener on this port, from the kernel's own tables."""
    rows = []
    for f in files or PROC_TCP:
        try:
            with open(f, encoding="ascii") as fh:
                text = fh.read()
        except FileNotFoundError:
            if f.endswith("6"):
                continue                        # a computer without IPv6 has no tcp6 table
            raise DoorUnread(True, f"{f} is not there")
        except (OSError, UnicodeError):
            raise DoorUnread(False, f"{f} could not be read")
        lines = lines_of(text)
        if not lines or " ".join(lines[0].split()) not in LINUX_TCP_HEADS:
            raise DoorUnread(True, f"{f} has columns this release does not know")
        for line in lines[1:]:
            c = line.split()
            try:
                lport, uid, inode = int(c[1].rsplit(":", 1)[1], 16), int(c[7]), int(c[9])
            except (IndexError, ValueError):
                raise DoorUnread(True, f"{f} has a row this release does not know")
            if c[3] == "0A" and lport == port:
                rows.append((uid, inode))
    return rows


def linux_proc(pid):
    """(uid, exe, args) of one process; DoorUnread when it could not be read (gone, or not ours to read)."""
    try:
        uid = os.stat(f"/proc/{pid}").st_uid
        exe = os.readlink(f"/proc/{pid}/exe")
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            args = fh.read().decode("utf-8", "replace").split("\0")
    except OSError:
        raise DoorUnread(False, "a process holding it could not be read")
    return uid, exe[:-len(" (deleted)")] if exe.endswith(" (deleted)") else exe, [a for a in args if a]


def linux_holders(inodes, first=()):
    """{inode: [pid]} for the listening sockets held by this account's processes. The pids in `first` (our own
    forge's, by its binary) are looked at before the rest."""
    want, found, me = {f"socket:[{i}]": i for i in inodes}, {}, os.getuid()
    try:
        pids = [x for x in os.listdir("/proc") if x.isdigit()]
    except OSError:
        raise DoorUnread(False, "the list of processes could not be read")
    for pid in [*first, *[x for x in pids if x not in first]]:
        try:
            if os.stat(f"/proc/{pid}").st_uid != me:
                continue
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if link in want:
                found.setdefault(want[link], []).append(int(pid))
        if found and set(found) == set(inodes):
            break
    return found


def mac_listeners(port, text):
    """[(pid)] of every TCP listener on this port, from netstat -anv's own columns, read by their header."""
    lines = lines_of(text)
    head = next((i for i, x in enumerate(lines) if x.split()[:1] == ["Proto"]), None)
    if head is None or " ".join(lines[head].split()) not in MAC_NETSTAT_HEADS:
        raise DoorUnread(True, "netstat names columns this release does not know")
    cols = lines[head].split()
    col = "process:pid" if "process:pid" in cols else "pid"
    after = len(cols) - cols.index(col) - 1
    pids = []
    for line in lines[head + 1:]:
        c = line.split()
        if len(c) < after + 7 or not c[0].startswith("tcp"):
            continue
        if c[5] != "LISTEN" or c[3].rsplit(".", 1)[-1] != str(port):
            continue
        try:
            pids.append(int(c[-(after + 1)].rsplit(":", 1)[-1]))
        except (IndexError, ValueError):
            raise DoorUnread(True, "netstat named a process this release cannot read")
    return pids


def mac_proc(pid):
    try:
        a = subprocess.run(["ps", "-ww", "-o", "uid=", "-o", "comm=", "-p", str(pid)], capture_output=True,
                           text=True, timeout=10)
        b = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise DoorUnread(False, "ps could not be asked")
    uid, _, exe = a.stdout.strip().partition(" ")
    # macOS prints the account `nobody` as uid -2 (seen 2026-11-24 on a Mac and in GitHub's Mac runner, M102's
    # stranger): a uid with its sign is a uid, another account's.
    if a.returncode != 0 or not re.fullmatch(r"-?\d+", uid) or not exe.strip():
        raise DoorUnread(False, "a process holding it could not be read")
    return int(uid), exe.strip(), b.stdout.strip()


def our_forge(p, exe, args):
    """Is this process this mirror's own forge: our pinned binary (any version beside it: upgrade-forge keeps both),
    started on our own settings file. args: a list (Linux, exact) or the one line ps prints (macOS)."""
    d, name = os.path.split(exe)
    if not re.fullmatch(r"gitea-\d+(?:\.\d+)*(?:[-+][0-9A-Za-z]+)?", name):
        return False
    try:
        if os.path.realpath(d) != os.path.realpath(os.path.dirname(p["bin"])):
            return False
    except OSError:
        return False
    if isinstance(args, list):
        return any(args[i:i + 2] == ["--config", p["app_ini"]] for i in range(len(args)))
    return f"--config {p['app_ini']}" in args


def read_door(p, port):
    """('mine' | 'other' | 'none', why); DoorUnread when the computer did not say. Never cached."""
    me = os.getuid()
    if system()[0] == "darwin":
        try:
            out = subprocess.run(NETSTAT, capture_output=True, text=True, errors="replace", timeout=20)
        except FileNotFoundError:
            raise DoorUnread(True, "netstat is not there")
        except (OSError, subprocess.SubprocessError):
            raise DoorUnread(False, "netstat did not answer")
        if out.returncode != 0:
            raise DoorUnread(False, "netstat did not answer")
        pids = mac_listeners(port, out.stdout)
        if not pids:
            return "none", "no program listens on it"
        procs = [mac_proc(x) for x in sorted(set(pids))]
    else:
        rows = linux_listeners(port)
        if not rows:
            return "none", "no program listens on it"
        if any(uid != me for uid, _ in rows):
            return "other", "a program of another account holds it"
        first = []
        try:
            for x in os.listdir("/proc"):
                if x.isdigit():
                    try:
                        if os.path.dirname(os.readlink(f"/proc/{x}/exe")) == os.path.dirname(p["bin"]):
                            first.append(x)
                    except OSError:
                        pass
        except OSError:
            pass
        held = linux_holders([i for _, i in rows], first)
        if set(held) != {i for _, i in rows}:
            raise DoorUnread(False, "a listener of this account was not found among its processes")
        procs = [linux_proc(x) for x in sorted({x for v in held.values() for x in v})]
    if any(uid != me for uid, _, _ in procs):
        return "other", "a program of another account holds it"
    if not all(our_forge(p, exe, args) for _, exe, args in procs):
        return "other", "a program of this account that is not this mirror's forge holds it"
    return "mine", "this mirror's own forge holds it"


def door_line(kind, port, why, never=False, again="status"):
    if kind == "other":
        return (f"problem: another program answers on the forge's port 127.0.0.1:{port}, not this mirror's own forge "
                f"({why}), so nothing was sent to it: no key, no copy. Run setup: it moves the forge to a free port "
                "and starts it again; every copy stays.")
    if never:
        return (f"problem: who holds the forge's port 127.0.0.1:{port} cannot be read on this computer ({why}), so "
                "nothing was sent to it: no key, no copy. The mirror needs that read to run; the README's section "
                "\"What it runs\" says where it can.")
    return (f"problem: who holds the forge's port 127.0.0.1:{port} could not be read this time ({why}), so nothing "
            f"was sent to it: no key, no copy. Run {again} again.")


def door(p, port, fresh=False, again="status"):
    """The one place every road to the forge passes: 'mine' or 'none' (no listener: nothing is sent, and the old
    "does not answer" lines stay; the owner's ruling F1); DoorShut on 'other' or unread, with its one line. A read is reused for DOOR_FRESH_S seconds."""
    key = (int(port), p["app_ini"])
    seen = DOOR_SEEN.get(key)
    if fresh or not seen or time.monotonic() - seen[0] > DOOR_FRESH_S:
        try:
            kind, why = read_door(p, int(port))
            never = False
        except DoorUnread as e:
            kind, why, never = "unread", str(e), e.never
        seen = DOOR_SEEN[key] = (time.monotonic(), kind, why, never)
    _, kind, why, never = seen
    if kind in ("mine", "none"):
        return kind
    raise DoorShut(kind, door_line(kind, port, why, never, again), never)


def door_floor():
    """Setup's first act (the owner's ruling: a floor is checked, not only named): a listener of its own for a
    moment, and the door read on it. None when this computer says who holds a port; otherwise the line, by kind
    (the owner's ruling F3, 11-24): a read that is not there or names columns this release does not know "cannot be
    read on this computer"; a read that did not answer this time "could not be read this time", run setup again."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        me = os.getuid()
        try:
            if system()[0] == "darwin":
                out = subprocess.run(NETSTAT, capture_output=True, text=True, errors="replace", timeout=20)
                if out.returncode != 0:
                    raise DoorUnread(False, "netstat did not answer")
                pids = mac_listeners(port, out.stdout)
                ok = pids == [os.getpid()] and mac_proc(os.getpid())[0] == me
            else:
                rows = linux_listeners(port)
                held = linux_holders([i for _, i in rows], [str(os.getpid())])
                ok = (len(rows) == 1 and rows[0][0] == me and held.get(rows[0][1]) == [os.getpid()]
                      and linux_proc(os.getpid())[0] == me)
            if not ok:
                raise DoorUnread(True, "a listener of setup's own was not named as its own")
        except FileNotFoundError:
            return door_line("unread", port, "netstat is not there", True)
        except DoorUnread as e:
            return door_line("unread", port, str(e), e.never, again="setup")
        except subprocess.TimeoutExpired:
            return door_line("unread", port, "netstat did not answer", False, again="setup")
        except (OSError, subprocess.SubprocessError):
            return door_line("unread", port, "the read could not be made", False, again="setup")
        return None
    finally:
        s.close()


def short_lived_start():
    """Where this system's short-lived ports begin: the ones it hands out by itself, for a bind to port 0 and for
    outgoing connections (Linux: ip_local_port_range, 32768 by default; macOS: net.inet.ip.portrange.first, 49152)."""
    try:
        if system()[0] == "darwin":
            out = subprocess.run(["sysctl", "-n", "net.inet.ip.portrange.first"], capture_output=True, text=True, errors="replace",
                                 timeout=10)
            return int(out.stdout.strip())
        with open("/proc/sys/net/ipv4/ip_local_port_range", encoding="ascii") as fh:
            return int(fh.read().split()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 49152


PORT_FLOOR = 20000


def free_port():
    """A free port on 127.0.0.1 below the system's short-lived range, at random from 20000 (G8.7): the system never
    hands such a number to a program by itself, so only one that asks for it by name can take it. Seen on a GitHub
    runner (11-24): a number from that range was handed to another program between the check and Gitea's bind."""
    hi = short_lived_start()
    if hi - PORT_FLOOR < 200:
        raise SystemExit(f"This computer hands out every port from {hi} up to any program that asks, which leaves "
                         f"too little room between {PORT_FLOOR} and {hi} for the forge's own. Nothing was changed. "
                         "Open an issue with this line.")
    for _ in range(200):
        port = random.randrange(PORT_FLOOR, hi)
        if port_is_free(port):
            return port
    raise SystemExit(f"No free port was found between {PORT_FLOOR} and {hi} in 200 tries. Nothing was changed. Run "
                     "setup again.")


class Forge:
    """The person's own forge, through its API, as the mirror account."""

    def __init__(self, p, st):
        self.p, self.port = p, int(st["port"])
        self.base = f"http://127.0.0.1:{st['port']}"
        answer, got = key_file_read(p["mirror_key"], FORGE_KEY_FORM)
        if answer != "read":                    # M155, M156: said as that, with its one step; never "does not answer"
            raise KeptUnread(p["mirror_key"], got if answer == "could not be read" else None)
        self.key = got
        self.user = st.get("mirror_user", MIRROR_USER)

    def call(self, method, path, body=None):
        if door(self.p, self.port) != "mine":    # G8.7: the key goes only to our own forge; on "none" nothing is
            return 0, {"message": f"nothing listens on 127.0.0.1:{self.port}"}   # sent (F1), as no answer
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + "/api/v1" + path, data=data, method=method,
                                     headers={"Authorization": f"token {self.key}", "Content-Type": "application/json",
                                              "User-Agent": USER_AGENT})
        try:
            with forge_opener().open(req, timeout=30) as r:
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


def road(url):
    """The skill's own road to GitHub, held against a person's git settings (row M74): git takes the longest
    url.<...>.insteadOf that matches, so one that names this repository's whole address and maps it to itself
    keeps a line like url."git@github.com:".insteadOf "https://github.com/" from sending the fetch over ssh with
    the person's own ssh key; and no transport but GitHub's own scheme is allowed at all."""
    scheme = urllib.parse.urlsplit(url).scheme
    return ["-c", f"url.{url}.insteadOf={url}", "-c", "protocol.allow=never", "-c", f"protocol.{scheme}.allow=always"]


THIS_COMPUTER = "127.0.0.1,localhost"


def forge_call(forge, cache, *args, env=None, timeout=GIT_TIMEOUT):
    """Every git call that names the skill's own forge's address goes through here, and only here (row M76): the
    forge is on this computer, so no proxy of the person's is asked to reach it: no_proxy names this computer, and
    git and git-lfs both keep to it, for a proxy in git's settings and one in the environment alike; the forge's
    key only in this one process's environment."""
    if door(forge.p, forge.port) != "mine":      # G8.7: the key and a copy go only to our own forge; on "none"
        return subprocess.CompletedProcess(       # nothing is sent (F1): git's own no-answer shape, nothing run
            ["git"] + list(args), 128, "",
            f"fatal: unable to access 'http://127.0.0.1:{forge.port}/': nothing listens on 127.0.0.1:{forge.port}")
    e = dict(forge.git_env(), **(env or {}))
    e.update(no_proxy=THIS_COMPUTER, NO_PROXY=THIS_COMPUTER)     # the call reaches only this computer
    return git(cache, *args, env=e, timeout=timeout)


def forge_opener():
    """The one opener for the skill's own forge: no proxy of the person's (row M76), and no redirect followed (C7)."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)


def github_call(cache, url, *args, key=None, timeout=GIT_TIMEOUT):
    """Every git call that names GitHub's address goes through here, and only here (row M74): the road held to the
    project's own full address, no transport but its scheme, and the key, when there is one, only in this one
    process's environment and only for that address (G3.4)."""
    return git(cache, *args, env=github_env(key) if key else None, timeout=timeout, via=url)


def address_read(cache, url):
    """What git will really use for the remote `github`, read before any fetch: (True, None) when it is the address
    the skill set; (False, where) when the person's git settings send it elsewhere; (None, why) when it could not be
    read. Nothing is fetched on the second or the third."""
    r = github_call(cache, url, "remote", "get-url", "github")
    got = r.stdout.strip()
    if r.returncode != 0 or not got:
        return None, git_reason(r.stderr) or f"git answered {r.returncode}"
    return (True, None) if got == url else (False, where_to(got))


def where_to(url):
    """Only the scheme and the host of an address git was sent to, never a name, a path or a credential in it."""
    s = urllib.parse.urlsplit(url)
    if s.scheme and s.hostname:
        return f"{s.scheme}://{s.hostname}"
    m = re.match(r"^(?:[^@/:]+@)?([^:/]+):", url)
    return f"an ssh address on {m.group(1)}" if m else "another address"


def forge_refusal(text):
    """M120: why the forge refused a push, in its own words: its error lines and the first refused ref's reason
    (seen from Gitea 28.4.0: "Internal Server Error (no message for end users)", "pre-receive hook declined")."""
    ls = [x.strip() for x in shown_lines(text)]
    said = [re.sub(r"^remote:\s*(error:|fatal:)\s*", "", x) for x in ls if re.match(r"^remote:\s*(error:|fatal:)", x)]
    said = [x.strip() for x in said if x.strip()]
    # The refused ref's reason is read from the line as it came (the shown line can end in the sentence about a ?),
    # then shown on its own.
    refused = next((x.strip() for x in program_lines(text) if x.strip().startswith("! [remote rejected]")), "")
    why = re.search(r"\(([^()]*)\)\s*$", refused)
    words = said[:2] + ([shown_line(why.group(1))] if why else [])
    return redact("; ".join(words)) if words else git_reason(text)


def git_reason(text):
    """Why git failed, in its own words: its first fatal or error line (its last line can be the tail of a
    sentence), with anything that looks like a credential hidden."""
    lines = [x.strip() for x in shown_lines(text)]
    first = next((x for x in lines if x.startswith(("fatal:", "error:"))), lines[-1] if lines else "")
    return redact(first)


def git(cache, *args, env=None, timeout=GIT_TIMEOUT, via=None):
    """Every git the skill runs: no prompt, no redirect, no credential helper, and no hook of the person's (row M76:
    a core.hooksPath in the person's git config ran their hooks inside the skill's own caches); `via` holds the road
    to GitHub (github_call); the road to the skill's own forge is forge_call's."""
    e = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    for k in [k for k in e if k.startswith("GIT_CONFIG_")]:
        e.pop(k)
    e.update(env or {})
    cmd = ["git", "-c", "http.followRedirects=false", "-c", "credential.helper=", "-c", f"core.hooksPath={os.devnull}"]
    cmd += road(via) if via else []
    if cache:
        cmd += ["--git-dir", cache]
    return run(cmd + list(args), env=e, timeout=timeout)


def has_git_lfs():
    try:
        return run(["git", "lfs", "version"], timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def uses_lfs(cache):
    """Does any branch or tag tip say, in its top .gitattributes, that files are kept with Git LFS? Decided on the
    file's bytes; a read that fails answers None, never no (M120: an error is never an absence)."""
    refs = git(cache, "for-each-ref", "--format=%(objecttype) %(*objecttype) %(refname)", "refs/heads", "refs/tags")
    if refs.returncode != 0:
        return None
    for line in lines_of(refs.stdout):
        kind, peeled, ref = line.split(" ", 2)
        kind = peeled or kind
        if kind == "tag":                              # a tag of a tag: what it comes to at the end
            out = git(cache, "cat-file", "-t", ref + "^{}")
            if out.returncode != 0:
                return None
            kind = out.stdout.strip()
        if kind not in ("commit", "tree"):
            continue                                   # a tag at a file (a blob): by its kind, no top .gitattributes
        there = git(cache, "ls-tree", "--name-only", ref, ".gitattributes")
        if there.returncode != 0:
            return None
        if not there.stdout.strip():
            continue                                   # this tip has no top .gitattributes
        out = git(cache, "cat-file", "blob", f"{ref}:.gitattributes")
        if out.returncode != 0:
            return None
        if "filter=lfs" in out.stdout:
            return True
    return False


def refs_of(cache):
    """The branches and tags as written down in the state (written(): one form under any language)."""
    out = git(cache, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags")
    return {written(r): s for s, r in (line.split(" ", 1) for line in lines_of(out.stdout))}


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
    return {r: s for s, r in (line.split(" ", 1) for line in lines_of(out.stdout))}


def keep_before_moving(cache, report, full):
    """G5.1: before a branch or tag of the copy moves backwards, is rewritten or is deleted, keep its old commit
    under kept/<date>/<name> (a tag under kept/<date>/tags/<name>). For a tag, its old commit is the commit it
    pointed at, through any tag objects (M126): an annotated tag's own message is not kept. A tag that pointed at no
    commit (a file or a tree) cannot be kept as a branch, and its line says so. Returns the new kept refs, or None on
    a failure."""
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
        what = "deleted" if not new else ("moved" if name.startswith("tags/") else "rewritten or moved back")
        keep = old
        if name.startswith("tags/"):                           # M126: the commit under any tag objects
            peeled = git(cache, "rev-parse", "--verify", "-q", old + "^{}")
            kind = git(cache, "cat-file", "-t", peeled.stdout.strip()) if peeled.returncode == 0 else peeled
            if kind.returncode != 0:
                return None                                    # a read that failed is never taken for a no (G5.4)
            if kind.stdout.strip() != "commit":
                at = {"blob": "a file", "tree": "a folder (a tree)"}.get(kind.stdout.strip(), kind.stdout.strip())
                said, note = shown_one(short)
                report.append(TAG_NOT_KEPT.format(full=full, short=said, what=what, at=at) + note)
                continue
            keep = peeled.stdout.strip()
        dest, n = f"refs/kept/{day}/{short}", 1
        while True:
            have = git(cache, "rev-parse", "--verify", "-q", dest).stdout.strip()
            if not have or have == keep:
                break
            n += 1
            dest = f"refs/kept/{day}/{short}-{n}"
        if git(cache, "update-ref", dest, keep).returncode != 0:
            return None
        said, note = shown_one(short)
        kept, more = shown_one(dest[len("refs/kept/"):])
        report.append(f"{full}: {said} was {what} on GitHub; its old commit is kept as kept/{kept}" + (note or more))
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
                         "refs/kept").stdout
            others = lines_of(others)
            if not [r for r in others if r != ref]:
                continue                                       # the only way to its commit: it stays
        if git(cache, "update-ref", "-d", ref, sha).returncode == 0:
            dropped.append(ref)
    return dropped


def written(name):
    """M120: a name as run() read it, in the one form the state holds under any language: its bytes decoded as UTF-8
    with each byte that is not UTF-8 kept as a stand-in character. The same as the name on a UTF-8 language; so a
    status in the person's language reads what a sync in the service's language wrote down as the same names."""
    return os.fsencode(name).decode("utf-8", "surrogateescape")


def not_text(name):
    """M120: a name (as run() read it) whose bytes are not UTF-8. The forge cannot hold one (seen: Gitea 28.4.0's
    pre-receive hook declined the whole atomic push), so it is left out."""
    return written_not_text(written(name))


def written_not_text(name):
    """The same for a name in the state's form."""
    return any("\udc80" <= c <= "\udcff" for c in name)


BACKTICK, DOLLAR_PAREN = chr(96), "$" + chr(40)               # spelled so, as no string the tool prints holds them


def shows_as_is(name):
    """A name (the state's form) this tool prints as it is: every character printable, no stand-in for a byte, and
    no backtick or dollar-parenthesis (family rule 31(B): a line relayed through a shell runs nothing, and names on
    GitHub are chosen by whoever can push there)."""
    return shown_as_is(name) == name


def shown_as_is(name):
    """The name as this tool prints it: each character that cannot be shown as it is, each backtick, and both
    characters of each dollar-parenthesis put as a ?. Only the printed form; the name itself is stored, copied and
    pushed as it is."""
    out = "".join(c if c.isprintable() and not "\udc80" <= c <= "\udcff" and c != BACKTICK else "?" for c in name)
    return out.replace(DOLLAR_PAREN, "??")


SHOWN_CHANGED = "a ? stands for each character this tool does not print as it is"


def shown_names(names, first=5):
    """Names for a line a person reads (family rule 31(B)): the first few through shown_as_is, the rest counted, and
    the line says when a shown name is not the name itself. Only for the eyes: nothing that decides reads it, and two
    names that show the same stay two."""
    shown = [shown_as_is(n) for n in names]
    out = ", ".join(shown[:first]) + (f" and {len(shown) - first} more" if len(shown) > first else "")
    return out + ("; " + SHOWN_CHANGED if shown[:first] != list(names[:first]) else "")


def shown_one(name):
    """One name for a line a person reads, with the same words when what is shown is not the name itself."""
    return shown_as_is(name), ("" if shows_as_is(name) else f" ({SHOWN_CHANGED})")


def forge_folder(p, forge, name):
    """The folder in which the forge keeps one copy (stock Gitea: data/gitea-repositories/<owner>/<name>.git, each
    in lower case), read from the disk; "" when it is not there."""
    root = os.path.join(p["forge"], "data", "gitea-repositories", forge.user.lower())
    path = os.path.join(root, name.lower() + ".git")
    return path if os.path.isfile(os.path.join(path, "HEAD")) else ""


def not_text_line(refs):
    """The words for a copy without the names the forge cannot hold (refs as git gave them)."""
    shown = [r[len("refs/heads/"):] if r.startswith("refs/heads/") else "tag " + r[len("refs/tags/"):]
             for r in sorted(refs)]
    return NOT_TEXT.format(n=len(shown), names=shown_names(shown))


def github_listing(cache, url, key, limit):
    """M123: GitHub's own list of the branches and tags the fetch asks for (FETCH_SPEC takes these two and nothing
    else), by its bytes in the state's one form; {} for a project with none (read, and none); None when it could
    not be read, which is never taken as none; and the words for why, when it could not be."""
    out = twice(lambda: github_call(cache, url, "ls-remote", "--heads", "--tags", "github", key=key, timeout=limit))
    if out is None:
        return None, f"it took longer than {limit} s, and so did the retry"
    if out.returncode != 0:
        return None, git_reason(out.stderr).replace(key, "<the key>") if key else git_reason(out.stderr)
    got = {}
    for line in lines_of(out.stdout):
        sha, tab, ref = line.partition("\t")
        if not tab or not sha or ref.endswith("^{}") or not ref.startswith(("refs/heads/", "refs/tags/")):
            continue
        got[written(ref)] = sha
    return got, ""


def not_as_listed(held, first, second):
    """M123: (wrong, seen). wrong: each name whose value in held (the cache, or the forge) is neither the first
    listing's nor the second's (None: not read), with why; a name that moved or went on GitHub between the two
    listings matches one of them. seen: what GitHub showed, per name the listing held agrees with."""
    last = second if second is not None else first
    wrong, seen = [], {}
    for ref in sorted(set(held) | set(first) | set(last)):
        if held.get(ref) in (first.get(ref), last.get(ref)):
            if ref in held:
                seen[ref] = held[ref]
        else:
            wrong.append((ref, "missing" if ref not in held else "not on GitHub" if ref not in last
                          else "at another commit"))
    return wrong, seen


def not_as_listed_line(wrong, words=NOT_AS_LISTED):
    """The words for names the copy would not hold (or, after the push, does not hold) as GitHub lists them, at
    most five shown."""
    shown = [(r[len("refs/heads/"):] if r.startswith("refs/heads/") else "tag " + r[len("refs/tags/"):]) + ": " + why
             for r, why in wrong]
    return words.format(n=len(shown), names=shown_names(shown))


def push_specs(cache):
    """What one atomic push sends: every branch and tag as GitHub has it, every kept ref, and a delete for each
    branch, tag or kept ref the copy holds that is gone here (each one already kept, or dropped by G5.2)."""
    now = {r: s for r, s in refs_under(cache, "refs/heads", "refs/tags").items() if not not_text(r)}   # M120
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
        claim(cache, "tree:git")
    url = f"{github_git()}/{full}.git"
    github_call(cache, url, "config", "remote.github.url", url)
    git(cache, "config", "remote.github.pushurl", NO_PUSH)       # G5.3: a push to GitHub cannot even start
    for i, spec in enumerate(FETCH_SPEC):
        git(cache, "config", "--replace-all" if i == 0 else "--add", "remote.github.fetch", spec)
    fetched_from = now_utc()
    same, said = address_read(cache, url)
    if same is None:
        return ADDRESS_UNREAD.format(why=said)
    if not same:
        return REWRITTEN.format(where=said)
    first, why = github_listing(cache, url, key, limit)         # M123: GitHub's own list, read before the fetch
    if first is None:
        return LISTING_UNREAD.format(why=why)
    out = twice(lambda: github_call(cache, url, "fetch", "--prune", "--quiet", "github", key=key, timeout=limit))
    if out is None:
        return f"the fetch from GitHub took longer than {limit} s, and so did the retry"
    if out.returncode != 0:
        return "the fetch from GitHub failed: " + git_reason(out.stderr).replace(key, "<the key>")
    held = refs_of(cache)
    second, why = (None, "") if held == first else github_listing(cache, url, key, limit)   # a name may have moved
    if held != first and second is None:
        return LISTING_UNREAD.format(why=why)
    wrong, seen = not_as_listed(held, first, second)
    # G7.1: what GitHub showed, written down from GitHub's own list, never from the cache (M123)
    rec.update(seen_refs=seen if not wrong else (second if second is not None else first), seen_at=now_utc())
    if wrong:                                  # M123: the forge is not given a set GitHub does not list
        return not_as_listed_line(wrong)
    lfs = None
    if not refs_of(cache):
        pass                                   # an empty repository has no large files to fetch or push
    elif has_git_lfs():
        out = twice(lambda: github_call(cache, url, "lfs", "fetch", "--all", "github", key=key, timeout=limit))
        if out is None:
            return f"the large files took longer than {limit} s to fetch, and so did the retry"
        if out.returncode != 0:
            return "the large files could not be fetched: " + git_reason(out.stderr).replace(key, "<the key>")
        lfs = True
    else:
        said = uses_lfs(cache)
        if said is None:
            return LFS_UNREAD
        if said:
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
    # M125: --no-tags, so git does not follow tags and write the forge's tags back into the cache's refs/tags, where
    # the fetch from GitHub has just taken away every tag GitHub deleted. This fetch writes refs/forge/ and refs/kept/ only.
    out = twice(lambda: forge_call(forge, cache, "fetch", "--quiet", "--prune", "--no-tags", "forge", timeout=limit))
    if out is None or out.returncode != 0:
        return "what the copy holds now could not be read from the forge" + (
            "" if out is None else ": " + last_line(out.stderr))
    # M127 (due 0.1.1): a GitHub branch named kept/... shares its name with a kept ref of the copy once the copy holds
    # it, and git refuses a push with two sources for one name. Said in plain words, before anything is pushed.
    there = refs_under(cache, "refs/forge/heads")                 # the copy's names, as just read from the forge
    clash = sorted(r for r in refs_under(cache, "refs/heads")
                   if r.startswith("refs/heads/kept/") and "refs/forge/heads/" + r[len("refs/heads/"):] in there)
    if clash:
        return KEPT_CLASH.format(n=len(clash), names=shown_names([written(r)[len("refs/heads/"):] for r in clash]))
    kept = keep_before_moving(cache, report, full)
    if kept is None:
        return "an old commit could not be kept, so the copy was not moved"
    dropped = drop_old_kept(cache, kept_days)
    if lfs:
        out = twice(lambda: forge_call(forge, cache, "lfs", "push", "--all", "forge", timeout=limit))
        if out is None:
            return f"the large files took longer than {limit} s to push, and so did the retry"
        if out.returncode != 0:
            return "the large files could not be pushed into the forge: " + last_line(out.stderr)
    specs = push_specs(cache)
    if specs:
        out = twice(lambda: forge_call(forge, cache, "push", "--quiet", "--atomic", "forge", *specs,
                                timeout=limit))
        if out is None:
            return f"the push into the forge took longer than {limit} s, and so did the retry"
        if out.returncode != 0:
            return "the push into the forge failed, so nothing in its copy was moved (the push is all or nothing): " + (
                forge_refusal(out.stderr))
    if dropped:
        report.append(f"{full}: {len(dropped)} kept ref(s) older than the kept period were dropped")
    back = forge_refs_of(forge, check["name"])                    # M123: the copy is the forge; read it back
    if back is None:
        return "what the copy holds after the push could not be read from the forge, so it is not called whole"
    # Both sides leave out refs/heads/kept/: in the forge that is where the tool's own kept refs live, so a GitHub
    # branch named kept/... is checked in the cache (against GitHub's list, above), not here.
    back = {written(r): s for r, s in back.items() if not r.startswith("refs/heads/kept/")}
    want = {r: s for r, s in seen.items() if not written_not_text(r) and not r.startswith("refs/heads/kept/")}
    if back != want:
        return not_as_listed_line(not_as_listed(back, want, want)[0], FORGE_NOT_AS_LISTED)
    # M120: a name the forge cannot hold is a known limit of this copy, not a failed refresh (the owner's ruling,
    # 11-24): it goes the road lfs_missing goes, a flag said at every sync and in status.
    rec.update(full_name=full, forge_name=cur["name"] if cur else copy_name(full), refs=seen,
               last_good=now_utc(), fetched_from=fetched_from, pushed_at=gh.get("pushed_at"), problem="",
               lfs_missing=(lfs is False), left_out=sorted(r for r in seen if written_not_text(r)))
    return ""


def cmd_config(args):
    p = paths()
    s = load_settings(p)
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
            claim(path)
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
        try:
            return sync(p, args)
        except DoorShut as e:
            door_stopped(p, e)
            say(str(e))
            return 1
    finally:
        drop_lock(p)


def kept_step(p, e):
    """The one step for a kept file that could not be read (KeptUnread): the file is moved out of its
    folder, by the person, and what it held is chosen again or found again; never written over by the tool."""
    blocked = blocked_folder(e.path)
    if blocked:
        return f"{blocked} cannot be entered ({folder_step(blocked)[0]}); {folder_step(blocked)[1]}"
    d = os.path.dirname(e.path)
    move = f"move that file out of {d} (into a folder of your own, to keep it)"
    if e.path == p["settings"]:
        try:
            names = os.listdir(p["keys"])
        except OSError:             # not there, or could not be read: no owner is named, and the step still stands
            names = []
        owners = sorted(f[:-4] for f in names if f.endswith(".key") and OWNER_RE.match(f[:-4]))
        keys = "; ".join(own_command(p, "key", "set", o) for o in owners)
        return (f"{move}; then, if you had chosen what is copied, choose it again ({own_command(p, 'config', 'include')} "
                f"or {own_command(p, 'config', 'exclude')}), and run key set again for each owner whose key is kept "
                "here, pasting the key when it asks" + (f": {keys}" if keys else "") + ". Until then nothing is copied")
    if e.path == p["app_ini"]:
        st = load_json(p["state"], {})
        port = st.get("port") if isinstance(st, dict) else None
        where = (f"the forge keeps its address, http://127.0.0.1:{port}/, while that port is free or the forge's "
                 "own, and is given a new one otherwise, which setup then says" if port else
                 "setup gives the forge its address and says it")
        return (f"{move}: it is the forge's own settings as they were, yours to keep. Then run setup again: "
                f"{own_command(p, 'setup')}. Setup writes the forge a new settings file and starts the forge with it; "
                f"{where}; every copy and both Gitea accounts are kept")
    if e.path == p["mirror_key"]:
        return (f"run setup again: {own_command(p, 'setup')}. It gives the '{MIRROR_USER}' account a new key, kept in "
                "that file, and keeps every copy")
    if e.path == p["state"]:
        # M157 (G6.24), 0.1.0's road as seen in a stand-in trial (2026-11-26): what follows is said, never a rebuild
        return (f"{move}, then run setup again: {own_command(p, 'setup')}. What follows: setup finds the forge and "
                "your Gitea accounts from before; the go for the first copy is asked again; each copy is then made "
                "again from GitHub beside its old one, and the old one, with its kept history, stays on the forge "
                "under a new name; so the disk the copies take doubles. Nothing is deleted")
    return f"{move}, then run the same command again"


def folder_step(d):
    """(why, the one step) for a folder of the skill's that this account cannot read, write and enter: it is given
    back to the person by its owner. Never a move of a file inside it (the person cannot reach one), never a write by
    the tool. A command is printed only for a path the tool prints as it is."""
    try:
        theirs = os.stat(d).st_uid != os.getuid()
    except OSError:
        theirs = False
    if theirs:
        cmd = f"sudo chown -R {shlex.quote(getpass.getuser())} {shlex.quote(d)}"
        return ("another account owns it, as one run of the skill with sudo leaves it",
                "give it back to your account, as an administrator of this computer"
                + (f": {cmd}" if shows_as_is(d) else f" ({SHOWN_CHANGED}, so no line is given)")
                + "; then run the same command again")
    cmd = f"chmod u+rwx {shlex.quote(d)}"
    return ("it is yours, and its mode does not let you",
            "give it back its mode" + (f": {cmd}" if shows_as_is(d) else f" ({SHOWN_CHANGED}, so no line is given)")
            + "; then run the same command again")


def link_left_line(link, unit, said):
    """M163 (walk6, 2026-11-26): the link systemctl made at setup and did not take off, named with its step. The rm
    line is printed only for a path the tool prints as it is (no backtick, no dollar-parenthesis, no character that
    cannot be seen), each part through shlex.quote, so the line runs nothing else when pasted."""
    return (f"Not taken: {shown_as_is(link)}, the link systemctl made at setup so that {unit} starts by itself. "
            f"systemctl did not take it off (it answered: {said}), so uninstall stopped the service instead, and read "
            "it stopped; the link stays where systemctl made it. With the service's program gone it starts nothing; "
            "delete it as you delete any file, when you want" + (
                f": rm {shlex.quote(link)}" if shows_as_is(link) else
                f" ({SHOWN_CHANGED} in its path, so no line is given)"))


def folders_floor(p, cmd):
    """M167 (G6.29): before any command reads or writes a kept file, each folder of the
    skill's own that is there (WSM_HOME's, the data and settings folders, the keys folder) is one this account can
    read, write and enter. Else the command stops before it reads or changes anything, naming the folder and why, so
    a failed read is never said as an absence ("Run setup first") and never ends in a traceback. None, or the
    stop."""
    seen = []
    for d in [os.environ.get("WSM_HOME"), p["data"], p["conf"], p["keys"]]:
        if not d or os.path.abspath(d) in seen:
            continue
        d = os.path.abspath(d)
        seen.append(d)
        try:
            st_ = os.stat(d)
        except FileNotFoundError:
            continue
        except NotADirectoryError:
            continue                  # a file where a folder goes: said by the step that reads that place
        except OSError:
            d = blocked_folder(os.path.join(d, "x")) or d
        else:
            if not stat.S_ISDIR(st_.st_mode) or os.access(d, os.R_OK | os.W_OK | os.X_OK):
                continue
        why, step = folder_step(d)
        return (f"Stopped: {d} is a folder of the skill's that this account cannot read, write and enter ({why}), "
                f"so {cmd} cannot read what is kept there, and changes nothing: nothing was copied, written or taken "
                f"away. The one next step: {step}.")
    return None


def kept_stop_words(p, e, cmd):
    what = {p["settings"]: "what you chose to copy", p["state"]: "what this computer's mirror holds"}.get(
        e.path, "what that file holds")
    return (f"Stopped: {e.path} {e.said}. So {cmd} cannot tell {what}, and changes nothing: "
            f"nothing was copied, written or taken away. The one next step: {kept_step(p, e)}.")


def door_stopped(p, e):
    """G8.7: the sync's own record says why it stopped, as its line does."""
    st = load_state(p)
    st["last_sync"] = {"at": now_utc(), "calls": 0, "copied": 0, "problems": 1,
                       "stopped": "another program holds the forge's port" if e.kind == "other" else
                       "who holds the forge's port could not be read"}
    save_state(p, st)


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


def restart_line(p, st):
    """G10.6: what happens after a restart, read now and changing nothing, for the block the person says go to.
    G1.24: "starts by itself" only when the service manager holds the forge's service word for word as written."""
    held, _, parts = service_read(p, "forge")
    if held in ("differs", "unread", "unrecorded"):
        return "After a restart: " + read_back_words("forge", held, not_set_up(held),
                                                     read_back_step("forge", held, "status"), parts)
    if system()[0] == "darwin":
        back, note = comes_back_by_itself()
        return "After a restart it starts by itself." if back else f"After a restart: {note}"
    if linger_state() == "yes":
        return f"After a restart it starts by itself: lingering is on for your account, so {LINGER_MEANS}."
    if st.get("linger_said") == "no":
        return ("After a restart it waits until you log in: lingering is off, as you chose, so the mirror syncs only "
                "while you are logged in. To change that, run setup --linger.")
    return (f"After a restart: lingering is off, so {NO_LOGIN_NOTE}. The computer's administrator can switch it on "
            f"with one command: {LINGER_COMMAND.format(user=shlex.quote(getpass.getuser()))} ({LINGER_MEANS}).")


def announce_first_copy(p, st, picked, complete, to_the_person=False):
    """G3.7, G3.9: what the person says yes to, named: how many, which (the first ANNOUNCE_NAMES by name, the whole
    list in a file of the person's own), how big, where they go, what is not copied, how private. It is kept, and the
    go copies exactly this list. G3.13: `go` prints the same block in the person's own terminal."""
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
          restart_line(p, st), lfs_missing() if not has_git_lfs() else "",
          *[key_end_line(o, (st.get("keys") or {}).get(o.lower(), {}).get("expires") or "") for o in sorted(complete, key=str.lower)],
          *open_folders(p), NOT_COPIED, PRIVATE_AS_THIS_COMPUTER], to_the_person)
    st["announced"] = st.get("announced") or now_utc()
    st["announced_list"] = names
    save_state(p, st)


def ask_go(p, st, names):
    """G3.13 (M61): the person's own go, after the block was printed in their own terminal: Enter alone gives it,
    for exactly this list; anything else, or nothing (the end of input), gives nothing."""
    say(f"Press Enter to give your go for the first copy of exactly these {len(names)} repositories. Type anything "
        "else first, and no go is given.")
    answer = sys.stdin.readline()
    if answer != "\n" and answer != "\r\n":
        say(f"No go was given. Nothing was copied or changed. To give it later, run this again: {own_command(p, 'go')}")
        return 1
    st["person_go"] = {"at": now_utc(), "list": names}
    save_state(p, st)
    say(f"Your go is kept, for these {len(names)} repositories only. The first copy starts with the next sync: tell "
        f"your agent, or run it here yourself: {own_command(p, 'sync')}")
    return 0


def cmd_go(args):
    """G3.13 (M61, the family's rule 7): what the person must hear before the first copy is printed by a command they
    run themselves, in their own terminal, and the copy goes on only after they press a key. Nothing piped in counts."""
    p = paths()
    if not sys.stdin.isatty():
        say("Needs you: the go for the first copy is the person's own: in a terminal window of their own, they run "
            f"{own_command(p, 'go')}, read what it shows and press Enter. No go was given, and nothing was changed.")
        return 2
    return cmd_sync(argparse.Namespace(announce=False, go=True, from_timer=False))


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
    st, s = load_state(p), load_settings(p)
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
        answer, key = key_file_read(kp)
        if answer == "not there":
            problems.append(f"{owner}: no key is kept for it; run key set {owner}")
            continue
        if answer != "read":                    # M155: this owner is said, and the others go on
            problems.append(key_unread_line(p, owner, kp, key))
            continue
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
    st = load_state(p)
    s = load_settings(p)
    owners = s.get("owners") or {}
    if getattr(args, "from_timer", False) and not st.get("go"):
        say("waiting for the person's go: nothing is copied until they give it in a terminal of their own (go) and "
            "a sync is run")
        return 0
    if set_up_misses(p, st):
        say("Run setup first.")
        return 2
    door(p, st["port"], fresh=True, again="sync")    # G8.7: before GitHub is asked anything
    wait = epoch_of(st.get("github_wait_until"))
    if wait > time.time():
        say(spent_line(st))
        return 0
    if not owners:
        say("No GitHub key is kept yet. Run: key set <your GitHub account> (and one per organisation).")
        return 2
    # G3.12 (M59): nothing is announced that cannot then be done. The announcement asks the forge first, as the sync
    # does, and asks GitHub nothing when the forge does not answer.
    person_go = getattr(args, "go", False)
    if (args.announce or person_go) and not st.get("go") and Forge(p, st).call("GET", "/version")[0] != 200:
        say(f"problem: the forge does not answer on 127.0.0.1:{st['port']}, so the first copy is not announced: it "
            "could not be made now. Nothing was changed, and GitHub was not asked. It is your own service on this "
            "computer: run status; if it still does not answer, run setup, which starts it again, then run sync "
            "--announce again.")
        return 1
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
        answer, key = key_file_read(kp)
        if answer == "not there":
            problems.append(f"{owner}: no key is kept for it")
            continue
        if answer != "read":                    # M155: this owner is said, and the others go on
            problems.append(key_unread_line(p, owner, kp, key))
            continue
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
    if args.announce or person_go:
        # G3.7: the announcement never copies, the first time or any time after. G3.13: neither does `go`; it keeps
        # the person's go for exactly this list, and the copy is made by a sync.
        if st.get("go"):
            say(f"The first copy was made on {local_time(st['go'])}, on the person's go; there is nothing to "
                "announce, and nothing was copied now. The timer keeps the copies current.")
            return 0
        if len(complete) < len(owners):
            say("The first copy is not announced: the list of projects could not be read for "
                + "; ".join(problems) + ". Nothing was changed. Run "
                + ("go" if person_go else "sync --announce") + " again when GitHub answers.")
            return 1
        announce_first_copy(p, st, picked, complete, person_go)
        if person_go:
            return ask_go(p, st, first_copy_names(picked))
        say("Nothing was copied. The go for the first copy is the person's own, given in a terminal window of their "
            f"own: they run {own_command(p, 'go')}, which shows them this same block and waits for their Enter. When "
            "it says their go is kept, run: sync")
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
            say("Give the person the block above. Their go, if they gave one, was for the old list: in a terminal "
                f"window of their own, they run {own_command(p, 'go')}, which shows the new list and waits for their "
                "Enter. When it says their go is kept, run: sync")
            return 2
        # G3.13 (M61, rule 7): the go is the person's own Enter, in their own terminal, after the same block was
        # printed there; an agent's relay is never the only road for what they must hear, and a yes in a
        # conversation is not the go.
        if (st.get("person_go") or {}).get("list") != names:
            say("Needs you: the go for the first copy is the person's own, given in a terminal window of their own, "
                f"and it has not been given for this list. They run {own_command(p, 'go')}: it shows what will be "
                "copied and waits for their Enter. Nothing was copied.")
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
        except DoorShut:
            rec.pop("copying_since", None)
            save_state(p, st)
            raise
        except HardStop as e:
            rec["problem"] = "cannot refresh: the sync stopped"
            rec.pop("copying_since", None)
            st["last_sync"] = {"at": now_utc(), "calls": calls, "copied": done, "problems": 1, "stopped": str(e)}
            save_state(p, st)
            say(f"STOPPED: {e}")
            return 1
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            why = str(e)
        except Exception:                    # M120: nothing one project holds ends the sync for every other project
            why = UNFORESEEN
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
    left_out = sorted(((rec["full_name"], rec["left_out"]) for rec in state.values()
                       if rec.get("left_out") and not rec.get("gone_since")), key=lambda x: x[0].lower())
    for name, refs in left_out:
        say(f"problem: {name}: {not_text_line(refs)}")
    say(f"sync: {len(found)} on GitHub, {done} copied now, {unchanged} unchanged, {len(problems)} problem(s), "
        f"{calls} GitHub API call(s)" + (", a full pass" if full_due else "")
        + (f", {len(lfs_missing)} cop{'y' if len(lfs_missing) == 1 else 'ies'} without its large files"
           if lfs_missing else "")
        + (f", {len(left_out)} cop{'y' if len(left_out) == 1 else 'ies'} without branch or tag names the forge "
           "cannot hold" if left_out else ""))
    say(NOT_COPIED)
    if plugin_removed(st):
        gone = PLUGIN_GONE.format(command=own_command(p, "uninstall"))
        say(gone)
    return 0 if not problems else 1


# ---------------------------------------------------------------- Claude Code's own uninstall (rule 8)

PLUGIN = "windstorm-mirror-for-github"
PLUGIN_GONE = ("Claude Code's plugin for this skill was removed; the mirror goes on as your own service on this "
               "computer and keeps every copy current. To take it off: {command}")


def plist_read(f):
    """M150 (G9.15), the family's rule 6: THE one reader of a property list in the tool; every place that parses one
    goes through it. ('read', the dict at its top), ('not there', None) or ('could not be read', where the read
    stopped): cannot be opened, 0 bytes, cut short, not well-formed, not a property list, or a top that is not a
    dict. Where the read stopped is the parser's line and column where it gives them, else the error's class; never
    a byte of the file (a parser's own message can quote one, so only expat's message for its code is said)."""
    try:
        with open(f, "rb") as fh:
            data = fh.read()
    except (FileNotFoundError, NotADirectoryError):
        return "not there", None
    except OSError as e:
        return "could not be read", f"it could not be opened ({type(e).__name__})"
    if not data:
        return "could not be read", "it is empty (0 bytes)"
    try:
        top = plistlib.loads(data)
    except xml.parsers.expat.ExpatError as e:
        return "could not be read", (f"it is not a well-formed property list (the read stopped at line {e.lineno}, "
                                     f"column {e.offset}: {xml.parsers.expat.ErrorString(e.code)})")
    except Exception as e:          # every other way the parser stops (a binary list cut short, a value too large)
        return "could not be read", f"it is not a property list this tool can read ({type(e).__name__})"
    if not isinstance(top, dict):
        return "could not be read", f"it holds no job: its top is {type(top).__name__}, not a dict"
    return "read", top


def plist_job_args(f):
    """M150: the job's arguments in a launch agent file, through plist_read: ('read', a list of strings), ('not
    there', None) or ('could not be read', where the read stopped). Arguments that are not a list of words are
    "could not be read", never a job of anyone's."""
    answer, got = plist_read(f)
    if answer != "read":
        return answer, got
    args = got.get("ProgramArguments")
    if not isinstance(args, list) or not args or not all(isinstance(w, str) for w in args):
        return "could not be read", "its ProgramArguments is not a list of words"
    return "read", args


def other_install(p):
    """G9.12: the service file this computer has for the skill, when it runs another home's copy than p's.
    None: there is none, or it runs p's own. Else (the copy it runs, or "" when that could not be read; the file;
    M150: where the read of the file stopped, when launchd has not loaded the job and the file itself could not be
    read, else "").
    G1.24: what it runs is read as the service manager parsed it (systemd over its bus; launchd's loaded job, or,
    when launchd has not loaded it, the file's own strings as the property-list format gives them), never by a
    pattern over the file's text."""
    f = p["plist"] if system()[0] == "darwin" else p["unit"]
    if not os.path.exists(f):
        return None
    state, unread = service_file_state(p, read_made(p)[0], f)
    if state == "yes":                               # this home wrote these very bytes: its own, whatever is parsed
        return None
    argv = None
    if system()[0] == "darwin":
        got = launchd_held(LAUNCHD_LABEL)
        if got == "absent":
            answer, got = plist_job_args(f)
            if answer != "read":
                return "", f, got if answer == "could not be read" else "it went while it was read"
            got = (got, None)
        if isinstance(got, tuple) and isinstance(got[0], list) and all(isinstance(w, str) for w in got[0]):
            argv = got[0]
    else:
        got = systemd_parsed(SERVICE + ".service")
        if got is not None and not got[0] and state != "could not be read":
            # M153 (G9.17; seen in a stand-in trial in a made home): systemd was read, and the unit shows no start
            # line (masked, emptied, or cut down by hand): it runs nothing, so it is no other install, and the file's
            # own bytes decide (named and left by uninstall, setup's stop to move it). Only that answer: one that
            # could not be read stays below.
            return None
        if got and len(got[0]) == 1:
            argv = got[0][0][1:]
    if argv is None:
        # M152: where the service manager's answer could not be read and the tool's own reader could not read the
        # file either, the stop's step is the one that can be taken (move it out), never "read that file"
        if state != "could not be read" and system()[0] == "darwin":
            answer, why = plist_job_args(f)
            unread = why if answer == "could not be read" else None
        return "", f, unread or ""
    if argv[1:] == ["-I", p["script"], "run-forge"]:
        return None
    return (argv[2] if len(argv) == 4 and argv[1] == "-I" and argv[3] == "run-forge" else ""), f, ""


def launchd_held(label):
    """M152 (G9.16, seen in a stand-in trial in a made home, not on a Mac): launchd_parsed, but where this account's
    domain answers LAUNCHD_NO_SCREEN (no login at the Mac's screen), read as launchd_off reads it: nothing of this
    account is loaded, so the job counts as not held ("absent") and the file's own words decide. Only that answer:
    every other answer launchd_parsed could not read stays None (G9.12's stop)."""
    got = launchd_parsed(label)
    if got is None and mac_no_screen():
        return "absent"
    return got


def unit_is_ours(p, which, starts):
    """M163 (G9.18), the Linux twin of job_is_ours: a unit systemd shows running this home's own copy of the skill,
    one of its start lines word for word after the interpreter (forge_argv, sync_argv, in the form the bus holds)."""
    want = sd_parsed_form(forge_argv(p) if which == "forge" else sync_argv(p))
    return any(isinstance(x, list) and x[2:] == want[2:] for x in starts or [])


def job_is_ours(p, label, args):
    """M150: a loaded job launchd shows running this home's own copy of the skill, argument by argument after the
    interpreter (forge_argv, sync_argv)."""
    want = forge_argv(p) if label == LAUNCHD_LABEL else sync_argv(p)
    return isinstance(args, list) and args[1:] == want[1:]


def other_install_stop(p, cmd, found="read it"):
    """Setup and uninstall do nothing against another home's install: said with the home it uses and one step.
    M150: a launch agent file at the skill's path that could not be read stops setup with the one step that can be
    taken (it is moved out of that folder); uninstall is not stopped by it (rule 8), and goes on within its limit."""
    found = other_install(p) if found == "read it" else found
    if not found:
        return None
    script, f, unread = found
    if unread:
        if cmd == "uninstall":
            return None
        kind = "a launch agent file" if system()[0] == "darwin" else "a service file"
        return (f"Stopped: this computer already has {kind} at the skill's path ({f}), and it could not "
                f"be read: {unread}. So {cmd} cannot tell whose it is: this home's, another home's, or something "
                f"else's. Nothing was changed. The one next step: move that file out of {os.path.dirname(f)} (into a "
                f"folder of your own, to keep it), then run {own_command(p, cmd)}")
    if not script:
        return (f"Stopped: this computer already has the skill's service ({f}), and which copy of the skill it runs "
                f"could not be read, so {cmd} cannot tell whether it is this one ({p['script']}). Nothing was "
                f"changed. The one next step: read that file, and run {cmd} with the copy it names.")
    if script == paths(None)["script"]:
        step = f"unset WSM_HOME, then run {own_command(paths(None), cmd)}"
    elif os.path.basename(os.path.dirname(script)) == "data":
        root = os.path.dirname(os.path.dirname(script))
        step = f"run it with that home: WSM_HOME={shlex.quote(root)} {own_command(paths(root), cmd)}"
    else:
        step = f"run it with the copy that service runs: python3 -I {shlex.quote(script)} {cmd}"
    return (f"Stopped: the skill set up on this computer uses another home. Its service ({f}) runs {script}, and "
            f"this {cmd} would use {p['data']}. Nothing was changed. The one next step, to run as printed: {step}")


def path_there(path):
    """True, False (read, and not there) or None (the read failed): M144's three answers. A read that failed is never
    taken for "not there"."""
    try:
        os.stat(path)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


def tool_copy(p):
    """M144 (G6.12): the copy of the tool a printed command names. The skill's own copy while it is there, or while
    whether it is there could not be read (every line as before); once it is read gone (uninstall takes it), the
    copy that is running, when that is read there (the clone, or the plugin's folder); None when neither is."""
    if path_there(p["script"]) is not False:
        return p["script"]
    here = os.path.abspath(__file__)
    return here if here != p["script"] and path_there(here) is True else None


def own_command(p, *args):
    """G6.12 (M60, M144): a command for the person to run, with a copy of this skill that is there (tool_copy), as it
    runs when pasted whole into a shell: the path is quoted (on a Mac it holds a space, 'Application Support')."""
    return " ".join(["python3", "-I", shlex.quote(tool_copy(p) or p["script"]), *args])


def plugin_place(here):
    """Claude Code keeps an installed plugin in <its folder>/plugins/cache/<catalog>/<plugin>/<version>/, and its
    list of installed plugins in <its folder>/plugins/installed_plugins.json. (that list's path, the catalog's name)
    when `here` is inside such a folder of this plugin; (None, None) otherwise (a clone, or the copy setup made)."""
    parts = os.path.abspath(here).split(os.sep)
    for i in range(len(parts) - 3, 0, -1):
        if parts[i] == "cache" and parts[i - 1] == "plugins" and parts[i + 2] == PLUGIN:
            return os.sep.join(parts[:i] + ["installed_plugins.json"]), parts[i + 1]
    return None, None


def plugin_registry_for(here):
    return plugin_place(here)[0]


def plugin_record(st):
    """G9.14 (M104, the family's rule 29 (e)): (the list's path, the catalog's name) as the record keeps them. A
    record an earlier build wrote kept the plugin's version folder as setup_from; both are read from that path then,
    and the path itself is never said."""
    old = plugin_place(os.path.join(st["setup_from"], "scripts", "mirror.py")) if st.get("setup_from") else (None, None)
    return st.get("plugin_registry") or old[0], st.get("plugin_catalog") or old[1]


def plugin_read(st):
    """G9.6 and G9.14: what Claude Code's list of installed plugins, the one setup was run from, says of this plugin:
    (True, the list's own name for it, <plugin>@<catalog>) read, and it names it; (False, None) read, and it names it
    nowhere; (None, None) there is no such list in the record, or it could not be read, or it is not on this
    computer at all (a record brought from another computer): a failed read is not a no."""
    reg = plugin_record(st)[0]
    if not reg:
        return None, None
    try:
        with open(reg, encoding="utf-8") as f:
            rows = json.load(f)["plugins"]
    except (OSError, ValueError, KeyError, TypeError):
        return None, None
    if not isinstance(rows, dict):
        return None, None
    ours = sorted((k, v) for k, v in rows.items() if isinstance(k, str) and k.split("@")[0] == PLUGIN)
    if not ours:
        return False, None
    # The family's rule 29 (e), as seen 2026-11-24 (Claude Code 2.1.331): an install or an update leaves one row
    # with its installPath; an uninstall takes the key away. A key whose list is empty, or a row with no
    # installPath, was never seen: it is "could not be read", never "removed" and never "installed".
    for k, v in ours:
        if isinstance(v, list) and v and all(isinstance(r, dict) and isinstance(r.get("installPath"), str)
                                             and r["installPath"] for r in v):
            return True, k
    return None, None


def plugin_listed(st):
    """True, False or None, as plugin_read says."""
    return plugin_read(st)[0]


def plugin_removed(st):
    """G9.6: True only when Claude Code's list of installed plugins was READ and names this plugin nowhere. A list
    that could not be read is never taken for 'removed'."""
    return plugin_listed(st) is False


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

def folders_setup_makes(p):
    """G9.9 (M65, the family's rule 23): the folders around the skill's own places that this computer does not have
    yet (on a stock home: ~/.config, ~/.config/systemd/user, ~/.local/share). Setup names them first, makes them,
    records them, and uninstall takes each away again once it is empty; one that is not empty is named as left."""
    own = {os.path.abspath(p["data"]), os.path.abspath(p["conf"])}
    tops = [p["data"], p["conf"], os.path.dirname(p["plist"] if system()[0] == "darwin" else p["unit"])]
    made = []
    for d in tops:
        d = os.path.abspath(d)
        while not os.path.lexists(d) and os.path.dirname(d) != d:
            if d not in own and d not in made:
                made.append(d)
            d = os.path.dirname(d)
    return sorted(made)


def record_made_folders(p, st, made):
    """Kept in the record at once, before anything else is made, so a setup that stops halfway still leaves it."""
    st["made_folders"] = sorted(set(st.get("made_folders") or []) | set(made))
    save_state(p, st)


def take_made_folders(st, removed):
    """Uninstall: each folder setup made goes again only when it is empty and a real folder (never a link)."""
    for d in sorted(st.get("made_folders") or [], key=len, reverse=True):
        try:
            if os.path.isdir(d) and not os.path.islink(d) and not os.listdir(d):
                os.rmdir(d)
                removed.append(f"{d}: an empty folder setup made")
        except OSError:
            pass


def setup_plan(p, st):
    plan = []
    made = folders_setup_makes(p)
    if made:
        plan.append("make the folders " + ", ".join(made) + " (not on this computer yet); uninstall takes each "
                    "away again once it is empty")
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
    elif not os.path.exists(p["password"]):
        plan.append(f"leave your Gitea account '{st['person']}' as it is, from before: setup does not change it or "
                    "its password")
    if not st.get("mirror_user"):
        plan.append(f"make the '{MIRROR_USER}' account that owns the copies, with a key of its own in "
                    f"{p['mirror_key']}, readable by you only")
    elif not os.path.exists(p["mirror_key"]):
        plan.append(f"give the '{st['mirror_user']}' account that owns the copies a new key of its own in "
                    f"{p['mirror_key']}, readable by you only (its key went with uninstall)")
    plan.append(f"copy this script to {p['script']}")
    if system()[0] == "darwin" and mac_no_screen():                    # M143: a step that works there
        plan.append("install the forge as your own service (a launch agent), which cannot start now: this account is "
                    "not logged in at this Mac's screen, and on a Mac the forge starts only in a login session at the "
                    "screen. Setup would stop there; log in to this account at this Mac's screen, then run setup")
    elif system()[0] == "darwin":
        plan.append("install the forge as your own service (a launch agent) and start it")
    else:
        base = os.path.dirname(p["unit"])
        plan.append(f"install the forge as your own service (a systemd user unit in {base}, the sync timer beside "
                    f"it) and start them; systemctl links them in {base}/default.target.wants and "
                    f"{base}/timers.target.wants, and uninstall takes the units and those links away")
    return plan


def own_forge_running(p):
    """True when a process of this account runs this mirror's forge binary on this folder; None when that could not
    be read (taken as yes: setup then moves nothing)."""
    me = os.getuid()
    if system()[0] == "darwin":
        try:
            out = subprocess.run(["ps", "-ww", "-x", "-U", str(me), "-o", "args="], capture_output=True, text=True,
                                 timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        return any(f"--config {p['app_ini']}" in x and os.path.dirname(p["bin"]) + "/gitea-" in x
                   for x in lines_of(out.stdout))
    try:
        pids = [x for x in os.listdir("/proc") if x.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            uid, exe, args = linux_proc(pid)
        except DoorUnread:
            continue
        if uid == me and our_forge(p, exe, args):
            return True
    return False


def move_forge_port(p, st):
    """G8.7 (the owner's condition 5): the forge's port moves only on a certain "other", after its service is
    stopped and no process of this account runs this forge's binary on this folder. Two forges on one data folder
    are worse than a mirror that stands, so otherwise nothing moves and it is said. 0 when moved."""
    old = st["port"]
    service(p, "stop")
    if own_forge_running(p) is not False:
        say(f"problem: another program holds the forge's port 127.0.0.1:{old}, and this mirror's own forge may "
            "still run on its folder, so setup did not move it: two forges on one folder would harm the copies. "
            "Nothing else was changed. Run setup again in a minute.")
        return 1
    port = free_port()
    with open(p["app_ini"], encoding="utf-8") as fh:
        ini = fh.read()
    ini = re.sub(r"^HTTP_PORT\s*=.*$", f"HTTP_PORT = {port}", ini, flags=re.M)
    ini = re.sub(r"^ROOT_URL\s*=.*$", f"ROOT_URL = http://127.0.0.1:{port}/", ini, flags=re.M)
    write_file(p["app_ini"], ini, 0o600)
    st["port"] = port
    save_state(p, st)
    say(f"The forge's address changed: another program holds 127.0.0.1:{old}, so the forge moves to "
        f"http://127.0.0.1:{port}/. Every copy stays.")
    return 0


def cmd_setup(args):
    p = paths()
    # G8.7 (the owner's ruling: a floor is checked, not only named): before setup makes anything, it makes a
    # listener of its own for a moment and reads who holds it. A computer that does not say makes nothing here, so
    # no one finishes setup and then finds every sync stopped.
    floor = door_floor()
    if floor:
        say(floor + " Setup made nothing.")
        return 2
    if os.path.isdir(p["mirror_key"]) and not os.path.islink(p["mirror_key"]):
        # M156 (G6.23): a folder where the forge's key belongs is never written into or removed by setup
        say(f"Stopped: {p['mirror_key']} is a folder, where the '{MIRROR_USER}' account's key file belongs, so setup "
            f"cannot keep its key there. Nothing was changed. The one next step: move that folder out of "
            f"{os.path.dirname(p['mirror_key'])} (into a folder of your own, to keep it), then run setup again: "
            f"{own_command(p, 'setup')}")
        return 2
    bus = bus_floor(p)                              # G1.24: the read-back's road, before the first write
    if bus:
        say(bus)
        return 2
    other = other_install_stop(p, "setup") or home_is_not_ours(p, getattr(args, "use_this_folder", False),
                                                                args.dry_run)
    if other:
        say(other)
        return 2
    if getattr(args, "from_file", None):
        take_on_now()
        return setup_from(p, args)
    have = load_state(p).get("gitea")
    if have and have != GITEA_VERSION:
        say(f"This forge runs Gitea {have}, and this release of the skill pins {GITEA_VERSION}. Setup does not move "
            "the forge: run upgrade-forge, which makes a backup first. Nothing was changed.")
        return 2
    st = load_state(p)
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
                lfs_missing() if not has_git_lfs() else ""] + open_folders(p))
        return 0
    if linger_unchosen(args, st):
        tell([f"After a restart: {linger_step(None, dry=True)[1]}"])
        say(LINGER_CHOICE)
        return 2
    if not os.path.exists(p["app_ini"]) and st.get("port"):
        # M159 (the owner's limit (3), 11-26): the forge's settings file is written new and the forge started again
        # only while no sync uses the forge: setup holds the sync's own lock across the write and the start again
        ok, _ = take_lock(p)
        if not ok:
            say(f"Not now: a sync is running (process {lock_holder(p)}), and setup writes the forge's settings file "
                "new and starts the forge again with it only while no sync is using the forge. Nothing was changed. "
                f"The one next step: run setup again when the sync has ended: {own_command(p, 'setup')}")
            return 3
        _setup_held[0] = True
    take_on_now()
    made = folders_setup_makes(p)
    os.makedirs(p["data"], mode=0o700, exist_ok=True)
    os.makedirs(p["conf"], mode=0o700, exist_ok=True)
    claim(p["conf"], "dir")
    record_made_folders(p, st, made)
    if fetch_forge(p, test_setting("WSM_GITEA_BASE")):
        say(f"Gitea {GITEA_VERSION} downloaded; checksum matches the pin.")
    else:
        say(f"Gitea {GITEA_VERSION} already here; checksum matches the pin.")
    claim(p["forge"], "tree:forge")
    fresh_ini = not os.path.exists(p["app_ini"])
    if fresh_ini:
        # M159 (G6.26): a settings file written anew keeps the forge's address where its port is free or the forge's
        # own (a forge still running from the file as it was), and the line says which
        old = st.get("port") if isinstance(st.get("port"), int) and PORT_FLOOR <= st.get("port") < 65536 else None
        keep = old and (port_is_free(old) or door(p, old) == "mine")
        port = old if keep else free_port()
        write_file(p["app_ini"], app_ini(p, port), 0o600)
        if old:
            say(f"The forge's settings file {p['app_ini']} is written new; the forge "
                + (f"keeps its address, http://127.0.0.1:{port}/." if keep else
                   f"has a new address, http://127.0.0.1:{port}/, since {old} is taken now."))
        st["port"] = port
    st.setdefault("port", int(ini_value(p["app_ini"], "server", "HTTP_PORT") or 0))
    try:
        door(p, st["port"], fresh=True, again="setup")
    except DoorShut as e:
        if e.kind != "other":
            say(str(e))
            return 1
        if move_forge_port(p, st):
            return 1
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
            say(f"Your Gitea account '{name}' is made. Its first password is in {p['password']} (readable by you "
                "only).")
        else:
            say(f"Your Gitea account '{name}' is there from before: setup did not change it or its password.")
        st["person"] = name
    elif not os.path.exists(p["password"]):
        say(f"Your Gitea account '{st['person']}' is there from before: setup did not change it or its password.")
    # M84: the key is made whenever its file is missing (after an uninstall that kept the copies, or a cut one), under
    # a name of its own, since Gitea refuses a key name the account already holds.
    key_answer, key_why = key_file_read(p["mirror_key"], FORGE_KEY_FORM)
    if not st.get("mirror_user") or key_answer != "read":
        # M156 (G6.23): a kept key that could not be read is made whole again from the forge, and said
        if key_answer == "could not be read":
            say(f"The '{MIRROR_USER}' account's key file {p['mirror_key']} could not be read: {key_why}. Setup gives "
                "the account a new key in its place.")
        had = MIRROR_USER in users
        if not had:
            create_user(p, MIRROR_USER, admin=False)
        out = gitea(p, "admin", "user", "generate-access-token", "--username", MIRROR_USER,
                    "--token-name", "windstorm-mirror" + ("-" + time.strftime("%Y%m%d%H%M%S", time.gmtime())
                                                          + "-" + os.urandom(3).hex() if had else ""),
                    "--scopes", MIRROR_SCOPES, "--raw")
        if out.returncode != 0 or not out.stdout.strip():
            raise SystemExit("Gitea did not give the mirror account its key; nothing was printed. Run setup again.")
        write_private(p["mirror_key"], out.stdout.strip() + "\n")
        st["mirror_user"] = MIRROR_USER
        say(f"The '{MIRROR_USER}' account that owns the copies is {'there from before; it has a new key' if had else 'made; its key is'}"
            f" in {p['mirror_key']}.")
    here = os.path.abspath(__file__)
    if os.path.abspath(p["script"]) != here:
        copy_whole(here, p["script"])
    if os.path.abspath(p["script"]) != here:
        # G9.8 (M63): where the skill was downloaded to, so that uninstall can name it as still here. Setup run again
        # from its own copy keeps the record. G9.14 (the family's rule 29 (e)): Claude Code's plugin is kept by
        # Claude Code's own list and the plugin's name alone, never by its version folder, which Claude Code
        # replaces at each update and removes later.
        st.pop("setup_from", None)
        st.pop("setup_from_file", None)
        st.pop("plugin_registry", None)
        st.pop("plugin_catalog", None)
        if plugin_registry_for(here):
            st["plugin_registry"], st["plugin_catalog"] = plugin_place(here)
        else:
            kind, where = downloaded_from(here)
            st["setup_from" if kind == "folder" else "setup_from_file"] = where
    st.update(version=VERSION, gitea=GITEA_VERSION)
    save_state(p, st)
    missing = set_up_misses(p, st)
    if missing:
        say(f"Setup did not finish: {missing} is not in place, so the other commands would say \"Run setup first.\" "
            "Nothing was started. Run setup again; if it stops here again, open an issue with this line.")
        return 1
    ok, why = install_service(p, restart=fresh_ini)
    timer_why = install_timer(p, int(setting(load_settings(p), "interval_min")))[1] if ok else ""
    shut = None
    if ok:
        try:
            up = wait_forge(p, st["port"], seconds=SETUP_WAIT, again="setup")
        except DoorShut as e:
            up, shut = False, str(e)
    if not ok:
        say(f"The forge's service did not start: {why}")
    elif shut:
        ok = False
        say(shut)
    elif up:
        say(f"The forge runs as your own service on http://127.0.0.1:{st['port']}/ (this computer only).")
    else:
        say(not_answering_yet(p, st["port"], SETUP_WAIT))
    if timer_why:                                   # G1.17: the forge's own line stands; the sync's is its own
        ok = False
        say(f"The sync's timer is not on, so nothing is copied until it is: {timer_why}")
    elif ok:                                        # G1.24: a person's own entry is theirs, a warning
        warn = theirs_line(p)                       # read once, and what is said is what was read
        if warn:
            say(warn)
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
    for line in ("After a restart it starts by itself." if back and ok else "", note,
                 lfs_missing() if not has_git_lfs() else "",
                 *open_folders(p, close=True), PRIVATE_AS_THIS_COMPUTER):
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
    if rec.get("problem"):              # the reason, and since when the copy stands still (never "current")
        return False, (f"not refreshed since {rec['last_good']}; " if rec.get("last_good") else "") + rec["problem"]
    if not rec.get("last_good"):
        return False, "never copied yet"
    if time.time() - epoch_of(rec["last_good"]) > max_age:
        return False, f"last good refresh {rec['last_good']}, older than twice the sync interval"
    held = {written(r): s for r, s in (forge_refs or {}).items() if not r.startswith("refs/heads/kept/")}
    if rec.get("left_out") and held == {r: s for r, s in (rec.get("refs") or {}).items() if not written_not_text(r)}:
        return False, not_text_line(rec["left_out"])        # M120: true, and said once; behind, never "not refreshed"
    if held != (rec.get("refs") or {}):
        return False, "the copy's branches and tags differ from what GitHub last showed"
    return True, ""


def forge_refs_of(forge, name):
    out = forge_call(forge, None, "ls-remote", f"{forge.base}/{forge.user}/{name}.git", timeout=60)
    if out.returncode != 0:
        return None
    return {r: s for s, r in (line.split("\t", 1) for line in lines_of(out.stdout) if "\t" in line)
            if r.startswith(("refs/heads/", "refs/tags/")) and not r.endswith("^{}")}


def key_scan(p, st):
    """G6.3: every repository config in the forge and the skill's own files, for anything shaped like a key or
    equal to one the skill holds. Returns (files read, [paths where one was found]). The key files are skipped."""
    known = set(SECRETS)
    for path in [p["mirror_key"]] + [os.path.join(p["keys"], f) for f in (os.listdir(p["keys"]) if os.path.isdir(p["keys"]) else [])]:
        answer, key = key_file_read(path)       # M155: a key file that could not be read is said by status itself
        if answer == "read":
            known.add(key)
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
        lines.append(f"newer release of this skill: {rc['tag']} (this is {VERSION}); self-update is your choice")
    if rc.get("gitea") and vt(rc["gitea"]) > vt(GITEA_VERSION):
        lines.append(f"that release pins Gitea {rc['gitea']} (this forge runs {GITEA_VERSION})"
                     + (", and names it a SECURITY FIX" if rc.get("security") else "")
                     + "; self-update then upgrade-forge is your choice. Nothing was changed.")
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
- Gitea's updates become urgent: a security fix matters the day it is out (status says when one is pinned).
- The usual roads are a private network of your own devices (a VPN), or a reverse proxy with TLS in front of
  it. Both are outside this skill, and status reports the forge as reachable from outside each time it runs.
Nothing was changed."""


def cmd_expose(args):
    """G8.2: the skill does not open the forge; it says what opening it would mean."""
    say(EXPOSE_TEXT)
    return 0


def cmd_status(args):
    """G6: one line per problem, then the count of copies, then the forge, the keys, the disk and the syncs."""
    p = paths()
    try:
        st = load_state(p)
        s = load_settings(p)
    except KeptUnread as e:          # M151, M154, M157: a problem line, with its one step; nothing else is read
        say(f"problem: {e.path} {e.said}. The one next step: {kept_step(p, e)}.")
        return 1
    if set_up_misses(p, st):
        say("Run setup first.")
        return 2
    try:
        answer, why = forge_ini_read(p)
        if answer == "could not be read":
            raise KeptUnread(p["app_ini"], why)
        forge = Forge(p, st)
    except KeptUnread as e:          # M155, M156, M159: the forge's key or settings, said with its one step
        say(f"problem: {e.path} {e.said}. The one next step: {kept_step(p, e)}.")
        return 1
    problems, notes = [], []
    lost = read_made(p)[1]
    if lost:                    # M158 (G6.25): said as that, never as another's files
        problems.append(f"{lost}, so the tool cannot tell which files in its folders it made: setup and uninstall "
                        "take nothing from them while it is so. The one next step: run "
                        f"{own_command(p, 'setup')}, which says what this means and what is yours to choose")
    for owner in sorted((s.get("owners") or {})):
        kp = key_path(p, owner)
        answer, why = key_file_read(kp)
        if answer == "could not be read":
            problems.append(key_unread_line(p, owner, kp, why))
    if not shutil.which("git"):
        problems.append("git is not installed on this computer, so no copy can refresh until it is")
    problems += open_folders(p)                     # rule 28: said again each time, until it is closed
    try:
        code, ver = forge.call("GET", "/version")
        shut = shut_kind = None
    except DoorShut as e:                           # G8.7: said in its one line, and nothing sent to that port
        code, ver, shut, shut_kind = 0, None, str(e)[len("problem: "):], e.kind
    up = code == 200                                # the door read mine AND the forge answered
    no_screen = not up and not shut and mac_no_screen()     # M143: the cause is read, never guessed
    if shut:
        problems.append(shut)
    elif no_screen:
        problems.append(f"the forge does not answer on 127.0.0.1:{st['port']}: {forge_no_screen(p)}")
    elif not up:
        # M105 (the family's rule 7): the problem line ends with its one step. Setup starts the forge again and keeps
        # every copy, or stops with its own reason (seen 2026-11-24 with the service's python3 taken away).
        problems.append(f"the forge does not answer on 127.0.0.1:{st['port']}; run setup again, which starts it and "
                        f"keeps every copy: {own_command(p, 'setup')}")
    elif (ver or {}).get("version") != GITEA_VERSION:
        problems.append(f"the forge runs Gitea {(ver or {}).get('version')}, and this release pins {GITEA_VERSION}"
                        + ("; upgrade-forge moves it, with a backup first" if vt((ver or {}).get("version")) < vt(GITEA_VERSION) else ""))
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
            if not shut:                            # G8.7: one line says why no copy was read; not one per copy
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
                            f", in {int(left)} day(s): make a new one, then run key set {owner}"))
        notes.append(f"key for {owner}: expires {exp}")
    free_mb = shutil.disk_usage(p["data"]).free // (1024 * 1024)
    floor_mb = setting(s, "disk_floor_mb")
    if free_mb < floor_mb:
        problems.append(f"only {free_mb} MB free on this disk, below the floor of {floor_mb} MB: syncs do not start")
    addrs = own_addresses()
    outside = answers_on(addrs, st["port"])
    for a in outside:
        # "the forge" is what answers only when the door read mine and the forge answered: then every listener on
        # the port is ours (the owner's ruling, 11-24: the subject of the sentence is what was read)
        problems.append(f"the forge answers on {a}, not only on 127.0.0.1: other computers may reach it" if up else
                        f"a program answers on port {st['port']} at {a}, and it is not this mirror's forge as far as "
                        "this computer says" if shut_kind == "other" else
                        f"a program answers on port {st['port']} at {a}; whether it is this mirror's forge could not "
                        "be read")
    rec_s = read_made(p)[0]
    for f in service_files(p):                      # M150: a service file whose bytes could not be read, any road
        if os.path.lexists(f) and service_file_state(p, rec_s, f)[0] == "could not be read":
            problems.append(f"the service file {f} could not be read: {service_file_state(p, rec_s, f)[1]}, so this "
                            "tool cannot tell whether it is as setup wrote it, and never writes over it or removes it. "
                            f"The one next step: move that file out of {os.path.dirname(f)} (into a folder of your "
                            f"own, to keep it), then run {own_command(p, 'setup')}")
    if not no_screen:                               # G1.24: what the manager parsed, read now
        reads = {which: service_read(p, which) for which in ("forge", "sync")}     # each read once
        for which, (held, _, parts) in reads.items():
            if held in ("differs", "unread", "unrecorded"):
                problems.append(read_back_words(which, held, not_set_up(held), read_back_step(which, held, "status"),
                                                parts))
            elif held == "absent" and system()[0] == "darwin":
                # M150: the job's file, read by the one reader when launchd holds no job; one line, never a traceback
                f = p["plist"] if which == "forge" else os.path.join(os.path.dirname(p["plist"]), SYNC_LABEL + ".plist")
                answer, why = plist_job_args(f)
                if answer == "could not be read" and not any(f in x for x in problems):
                    problems.append(f"the {which}'s launch agent file {f} could not be read: {why}, so this tool "
                                    "cannot tell whose it is, and launchd holds no job for it now. The one next step: "
                                    f"move that file out of {os.path.dirname(f)} (into a folder of your own, to keep "
                                    f"it), then run {own_command(p, 'setup')}")
        warn = theirs_line(p, reads)
        if warn:
            notes.append("note: " + warn)
    when, tproblem = next_sync()
    if tproblem:
        problems.append(tproblem + (": this account is not logged in at this Mac's screen" if no_screen else ""))
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
        notes.append("plugin: " + PLUGIN_GONE.format(command=own_command(p, "uninstall")))
    for x in problems:
        say(f"problem: {x}")
    say(f"copies: {on_gh} on GitHub, {copied} copied, {behind} behind, {unreadable} unreadable"
        + (f", {no_lfs} without its large files" if no_lfs else ""))
    say(f"first copy: made on {local_time(st['go'])}, on your go" if st.get("go") else
        "first copy: waiting for your go, which can be given once the forge answers (see the problem above)"
        if no_screen else
        "first copy: waiting for your go: in a terminal window of your own, run " + own_command(p, "go")
        + " (it shows what will be copied and waits for your Enter); a sync then starts it")
    for x in notes:
        say(x)
    said_ver = ((ver or {}).get("version") if up else "(not asked: another program answers on its port)"
                if shut_kind == "other" else "(not asked: who holds its port could not be read)" if shut_kind else
                "(not answering)")
    say(f"forge: Gitea {said_ver} on http://127.0.0.1:{st['port']}/ (pinned {GITEA_VERSION})")
    say(f"disk: {free_mb} MB free (floor {floor_mb} MB)")
    others = [a for a in addrs if not (a == "::1" or a.startswith("127."))]
    if up:
        say(f"network: the forge answers on 127.0.0.1 only (tried {len(others)} other address(es) of this computer)"
            if not outside else f"network: the forge answers on {', '.join(outside)} too")
    else:
        say(f"network: the forge was not reached, so where it answers was not checked (nothing answers on this port "
            f"at the {len(others)} other address(es) of this computer)" if not outside else
            f"network: the forge was not reached; a program answers on this port at {', '.join(outside)}")
    last = st.get("last_sync") or {}
    say(f"last good sync: {st.get('last_good_sync') or 'none yet'}")
    if last:
        say(f"last sync: {last.get('at')}, {last.get('copied', 0)} copied, {last.get('unchanged', 0)} unchanged, "
            f"{last.get('problems', 0)} problem(s), {last.get('calls', 0)} GitHub API call(s)"
            + (f"; stopped: {last['stopped']}" if last.get("stopped") else ""))
    if tproblem:
        say("next sync: none (see the problem above)")
    elif not up:                                    # M105: a time is not a promise while the forge does not answer
        say(f"next sync: {when if system()[0] == 'darwin' else 'will be tried at ' + when}; it cannot work until "
            "the forge answers")
    else:
        say(f"next sync: {when}")
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
    d = tempfile.mkdtemp(prefix=f"{what}-", dir=p["data"])
    claim(d, "tree:other")
    return d


def clone_copy(forge, name, into):
    """A bare clone of one copy from the forge, with every host but this computer shut off (G7.2)."""
    out = forge_call(forge, None, "clone", "--quiet", "--bare", f"{forge.base}/{forge.user}/{name}.git", into,
                     env=ONLY_THIS_COMPUTER)
    if out.returncode != 0:
        return "the copy could not be cloned from the forge: " + last_line(out.stderr)
    if uses_lfs(into):
        if not has_git_lfs():
            return "it holds large files, and git lfs is not installed here to check them"
        out = forge_call(forge, into, "lfs", "fetch", "--all", "origin", env=ONLY_THIS_COMPUTER)
        if out.returncode != 0:
            return "its large files could not be fetched from the forge: " + last_line(out.stderr)
        out = forge_call(forge, into, "lfs", "fsck", env=ONLY_THIS_COMPUTER)
        if out.returncode != 0:
            return "its large files are damaged: " + last_line(out.stdout + out.stderr)
    return ""


def cmd_restore(args):
    p = paths()
    st = load_state(p)
    if set_up_misses(p, st):
        say("Run setup first.")
        return 2
    try:
        door(p, st["port"], fresh=True, again="restore --check" if args.check else "status")
    except DoorShut as e:
        say(str(e))
        return 1
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
            have = {written(r): s for r, s in refs_under(into, "refs/heads", "refs/tags").items()
                    if not r.startswith("refs/heads/kept/")}
            seen = last_seen_list(rec) or {}                               # written(): the state's one form
            for ref in sorted(set(have) | set(seen)):
                if have.get(ref) != seen.get(ref):
                    mine += 1
                    said, note = shown_one(ref)
                    diffs.append(f"{name}: {said} is {(have.get(ref) or 'missing')[:12]} in the copy, "
                                 f"{(seen.get(ref) or 'missing')[:12]} when GitHub was last seen{note}")
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


def last_seen_list(rec):
    """M135: GitHub's list of branches and tags as the sync last read it, as restore --check reads it (written(): the
    state's one form); None when the record holds no list at all (rule 6: never taken as none)."""
    if not isinstance(rec.get("seen_refs"), dict) and not isinstance(rec.get("refs"), dict):
        return None
    return rec.get("seen_refs") or rec.get("refs") or {}


def restore_to(p, st, forge, url, repository):
    """G7.3: one copy, every branch and tag, into a new home the person names; never GitHub (INV1)."""
    if not repository:
        say("Name the repository: restore --to <url> <owner/name>.")
        return 2
    # M132 (family rule 31(B), a reviewer's world 20, 11-25): the --to value and the project's name are typed by a
    # person or their agent and passed on; each line shows them through the shown form, and no command carries one
    # that this tool does not print as it is. The restore itself uses them as they are.
    to, who = shown_as_is(url), shown_as_is(repository)
    note = "" if shows_as_is(url) and shows_as_is(repository) else f" ({SHOWN_CHANGED})"
    low = url.lower()
    host = (urllib.parse.urlsplit(url).hostname or re.sub(r"^[^@]*@([^:/]+).*$", r"\1", url)).lower()
    if any(h == host or host.endswith("." + h) for h in github_hosts()) or "github.com" in low:
        if not shows_as_is(url):
            say("This skill never writes to GitHub, so it will not restore there. The address given holds a character "
                f"this tool does not print as it is ({to}; {SHOWN_CHANGED}), so no line is given to put it back there "
                "yourself. Make the new, empty repository on GitHub, then run restore --to again with its address.")
            return 2
        # M132: one line a person runs as it is printed: the copy's own folder in the forge is on this computer, so
        # git needs no login to the forge; each part through shlex.quote, and none from a name it does not print.
        folder = forge_folder(p, forge, copy_name(repository)) if "/" in repository else ""
        if not folder or not shows_as_is(folder):
            say("This skill never writes to GitHub, so it will not restore there. "
                + ("The copy's own folder in your forge could not be found" if not folder else
                   "The copy's folder holds a character this tool does not print as it is")
                + ", so no line is given to put it back there yourself. Run status to see the copies the forge holds, "
                  "and name one of them as owner/name.")
            return 2
        say("This skill never writes to GitHub, so it will not restore there. To put this project back on GitHub "
            "yourself, make a new, empty repository there, then run this line in a terminal:")
        say(f"  git --git-dir {shlex.quote(folder)} push --mirror {shlex.quote(url)}")
        say("Run it as it stands: it sends every branch and tag of the copy, with its kept refs, from the copy's own "
            "folder in your forge to the new repository, with your own login to GitHub, and changes nothing in the "
            "copy.")
        return 2
    rec = next((r for r in (st.get("repos") or {}).values()
                if (r.get("full_name") or "").lower() == repository.lower()
                or (r.get("forge_name") or "").lower() == repository.lower()), None)
    try:
        cur = forge.by_id(rec["forge_id"]) if rec and rec.get("forge_id") else None
    except CouldNotRead as e:
        say(f"The copy of {who}{note} could not be read: {e}. Nothing was pushed; try again when the forge answers.")
        return 1
    if not cur:
        say(f"There is no copy of {who}{note} in the forge. Run status to see the copies it holds, and name one "
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
            say(f"{who}{note}: {why}. Nothing was pushed. Run restore --check to see what the copy needs.")
            return 1
        mine = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        run(["git", "--git-dir", into, "remote", "add", "home", url], env=mine)
        said = uses_lfs(into)
        if said is None:
            say(f"Whether {who}{note} keeps files with Git LFS could not be read from its .gitattributes, so nothing "
                "was pushed. Run restore --check, then restore --to again.")
            return 1
        if said and has_git_lfs():
            # Large files first, to the new home by its own name (git lfs would otherwise ask the forge).
            out = run(["git", "--git-dir", into, "lfs", "push", "--all", "home"], env=mine, timeout=3600)
            if out.returncode != 0:
                say("The large files could not be put in the new home, so nothing else was pushed: "
                    + " / ".join(x.strip() for x in shown_lines(out.stderr)[-3:])
                    + ". Check that the new home takes large files, then run restore --to again.")
                return 1
        specs = ["refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*", "^refs/heads/kept/*"]
        out = run(["git", "--git-dir", into, "-c", "core.hooksPath=/dev/null", "push", "--quiet", "--atomic", "home",
                   *specs], env=mine, timeout=3600)
        if out.returncode != 0:
            say("The push to the new home failed: " + " / ".join(x.strip() for x in shown_lines(out.stderr)[-4:])
                + ". Run restore --to again into a new, empty repository; if it fails the same way, git's words are "
                  "the reason.")
            return 1
        sent = {r: s for r, s in refs_under(into, "refs/heads", "refs/tags").items() if not r.startswith("refs/heads/kept/")}
        back = run(["git", "ls-remote", url], env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), timeout=120)
        there = {r: s for s, r in (l.split("\t", 1) for l in lines_of(back.stdout) if "\t" in l)
                 if r.startswith(("refs/heads/", "refs/tags/")) and not r.endswith("^{}")}
        if there != sent:
            say("The new home does not hold what was sent; do not rely on it. Run restore --to again into a new, "
                "empty repository.")
            return 1
        heads = sum(1 for r in sent if r.startswith("refs/heads/"))
        say(f"restored {who}: {heads} branch(es) and {len(sent) - heads} tag(s) to {to}; "
            f"the kept refs stay in your forge{note}")
        # M128: a name GitHub held, in the copy, that the new home did not get is said by name, and the exit is not 0.
        # Only a GitHub branch named kept/... can be one (it shares its name with the copy's own kept refs, M127):
        # every tag and every other branch was sent, and "there != sent" above stops restore before this otherwise.
        held = {written(r) for r in refs_under(into, "refs/heads", "refs/tags")}
        home = {written(r) for r in there}
        left = sorted(r for r in (rec.get("refs") or {}) if r in held and r not in home)
        # M135: a name GitHub's list held when the sync last read it, which the copy does not hold and so the new home
        # did not get, is said too (a name that is not text, one GitHub added since, one taken out of the copy), with
        # why where the record knows it; the exit is then not 0. What restore pushes does not change.
        seen = last_seen_list(rec)
        lacking = None if seen is None else sorted(r for r in seen if r not in home and r not in left)
        if seen is None:
            say(RESTORE_LIST_UNREAD.format(full=who + note))
        elif lacking:
            odd = sum(1 for r in lacking if written_not_text(r))
            say(RESTORE_LACKS.format(
                n=len(lacking),
                names=shown_names([r[len("refs/heads/"):] if r.startswith("refs/heads/") else "tag " + r[len("refs/tags/"):]
                                   for r in lacking]),
                why=f"; {odd} of them are not UTF-8 text, which the forge cannot hold" if odd else ""))
        if left:
            # Each name stands for itself only where it can be read as it is (rule 31: a name we did not write is
            # never handed to a shell raw): one whole line per name, every name, each part through shlex.quote.
            hidden = [r for r in left if not shows_as_is(r)]
            shown = [r[len("refs/heads/"):] for r in left]                  # only branches: every tag was sent
            say(RESTORE_LEFT_OUT.format(n=len(left), names=shown_names(shown)))
            folder = forge_folder(p, forge, cur["name"])
            if not folder:
                say("The copy's own folder in the forge could not be found, so no line is given to bring them over. "
                    "Run restore --check, and open an issue with these lines if it says the copy is whole.")
                return 1
            if not shows_as_is(url):
                say(RESTORE_TO_HIDDEN)
                say(f"  git --git-dir {shlex.quote(folder)} for-each-ref refs/heads/kept/")
                say("Each can be sent from the copy's own folder in your forge with git push, naming the new home and "
                    "the name yourself; that changes nothing in the copy.")
                return 1
            for r in left:
                if r not in hidden:
                    say(f"  git --git-dir {shlex.quote(folder)} push {shlex.quote(url)} {shlex.quote(r)}")
            if hidden:
                say(RESTORE_HIDDEN.format(n=len(hidden)))
                say(f"  git --git-dir {shlex.quote(folder)} for-each-ref refs/heads/kept/")
            say("Run each git line above as it stands, in a terminal: each sends one name from the copy's own folder "
                "in your forge to the new home, with your own login there, and changes nothing in the copy.")
            return 1
        return 0 if lacking == [] else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def service(p, verb):
    """Stop or start the forge's own service."""
    if system()[0] == "darwin":                       # G1.17: a stop ends once launchd has let go of the job
        if verb == "stop":
            return launchd_off(p, LAUNCHD_LABEL)[0] == "gone"
        out, _ = launchd_load(p, LAUNCHD_LABEL, p["plist"])
        return out is not None and out.returncode == 0
    else:
        cmd = ["systemctl", "--user", verb, SERVICE]
    return run(cmd).returncode == 0


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
    st = load_state(p)
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
        # G8.7: "stopped" is read by the door: another program that takes the port meanwhile is not our forge
        # running, and a door that stays unread is not said as either.
        gone = wait_stopped(p, st["port"])
        if gone == "running":
            say("The forge did not stop, so no backup was made (a backup of a running forge can be torn). Run "
                "backup again; if the forge still does not stop, run status.")
            return 1
        if gone == "unread":
            say(f"Who holds the forge's port 127.0.0.1:{st['port']} could not be read after the forge was told to "
                "stop, so no backup was made (a backup of a running forge can be torn). Run backup again.")
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
    try:
        door(p, st["port"], fresh=True)          # G8.7: another program took the port while the forge was stopped
    except DoorShut as e:
        say(str(e))
        return 1
    return 0


def setup_from(p, args):
    """G7.4: a new computer with only the backup file and the skill gets back every copy."""
    if load_state(p).get("port"):
        say("This computer already has a forge of this skill; setup --from is for a new one. Nothing was changed. "
            "Run setup --from on the new computer; this one keeps its own forge.")
        return 2
    if linger_unchosen(args, {}):
        tell([f"After a restart: {linger_step(None, dry=True)[1]}"])
        say(LINGER_CHOICE)
        return 2
    src = os.path.abspath(os.path.expanduser(args.from_file))
    made = folders_setup_makes(p)
    os.makedirs(p["data"], mode=0o700, exist_ok=True)
    os.makedirs(p["conf"], mode=0o700, exist_ok=True)
    claim(p["conf"], "dir")
    record_made_folders(p, {}, made)
    fetch_forge(p, test_setting("WSM_GITEA_BASE"))
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
        claim(f, "tree:forge")
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
              restored_at=now_utc(), made_folders=made)
    st.pop("keys", None)
    save_state(p, st)
    here = os.path.abspath(__file__)
    if os.path.abspath(p["script"]) != here:
        copy_whole(here, p["script"])
    ok, why = install_service(p)
    timer_why = install_timer(p, int(setting(load_settings(p), "interval_min")))[1] if ok else ""
    # M88 (G7.4, rule 24): what restore says is what it read. The person acts on these lines after losing a
    # computer, so "back" and "runs" are said only when the forge itself answered and held the copies.
    shut = None
    try:
        up = ok and wait_forge(p, port, seconds=SETUP_WAIT)
    except DoorShut as e:
        up, shut = False, e
    ids = [r["forge_id"] for r in (st.get("repos") or {}).values() if r.get("forge_id")]
    n = len(ids)
    owners = ", ".join(sorted((load_settings(p).get("owners") or {}))) or "(none)"
    name = os.path.basename(src)
    held = unread = 0
    if up:
        forge = Forge(p, st)
        for rid in ids:
            try:
                held += forge.by_id(rid) is not None
            except CouldNotRead:
                unread += 1
        if held == n:
            say(f"Restored from {name}: {n} copies are back, on http://127.0.0.1:{port}/ (this computer only), "
                "counted in the forge itself.")
        else:
            say(f"Restored from {name}: the forge holds {held} of the {n} copies in the restored record"
                + (f", and {unread} could not be read" if unread else "") + f", on http://127.0.0.1:{port}/ (this "
                "computer only). Run status: it names each copy that is not there.")
    elif shut:
        say(f"Restored from {name}: {n} copies are in the restored data; they are not counted in the forge, since "
            "nothing was sent to its port:")
        say(str(shut))
        ok = False
    else:
        say(f"Restored from {name}: {n} copies are in the restored data; the forge has not answered, so they are not "
            "counted there yet.")
    say(f"Your GitHub keys are not in a backup. To keep the copies current, make new keys and run key set for "
        f"each owner: {owners}. Until then, status shows them behind.")
    say("Your Gitea password is the one it had on the old computer.")
    if not ok:
        say(f"The forge's service did not start: {why}")
    elif up:
        say("The forge runs as your own service.")
    else:
        say(not_answering_yet(p, port, SETUP_WAIT))
    if timer_why:
        ok = False
        say(f"The sync's timer is not on, so nothing is copied until it is: {timer_why}")
    if system()[0] == "darwin":
        back, note = comes_back_by_itself()
    else:
        st = load_state(p)
        st.pop("linger_by_setup", None)              # the old computer's record means nothing here
        back, note = linger_step(st, allow=not getattr(args, "no_linger", False))
        save_state(p, st)
    if back and ok:                     # never after a service that did not start (M88)
        say("After a restart it starts by itself.")
    if note:
        say(note)
    return 0 if up and held == n else 1       # 1: the forge did not answer, or it does not hold every copy


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
            f"something is holding it up: run status in 5 minutes. If it still does not answer then, the forge's "
            f"own words are the last lines of {os.path.join(p['forge'], 'log', 'gitea.log')}.")


def wait_forge(p, port, seconds=FIRST_START_WAIT, again="status"):
    """G1.9, G8.7: the forge is up only when the door reads it as ours AND its health check answers. Another
    program on the port stops the wait at once (DoorShut); a door that stays unread to the end is said as that."""
    shut = None
    for _ in range(seconds * 2):
        try:
            if door(p, port, fresh=True, again=again) == "mine":
                with forge_opener().open(f"http://127.0.0.1:{port}/api/healthz", timeout=2):
                    return True
            shut = None
        except DoorShut as e:
            if e.kind == "other":
                raise
            shut = e
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    if shut:
        raise shut
    return False


def wait_stopped(p, port, seconds=30):
    """After the service is told to stop: 'stopped' (no listener on the port AND no process of this account runs
    this forge on this folder), 'other' (another program holds the port AND none of ours runs: the forge did stop),
    'running' (ours still runs at the end), 'unread' (the door or our own processes stayed unread to the end, so
    neither is said). The port alone is never the answer (the owner's rulings F2 and F4, 11-24): a stranger can sit
    on ::1 while our forge still runs on 127.0.0.1, and a forge that has closed its listener can still be writing
    its database while it ends, so a dump or a migration then would read a torn database."""
    kind = "unread"
    for _ in range(seconds * 2):
        try:
            seen = door(p, port, fresh=True)          # "mine" or "none"
        except DoorShut as e:
            seen = e.kind                              # "other" or "unread"
        if seen == "mine":
            kind = "running"
        elif seen == "unread":
            kind = "unread"
        else:
            ours = own_forge_running(p)
            if ours is False:
                return "stopped" if seen == "none" else "other"
            kind = "running" if ours else "unread"
        time.sleep(0.5)
    return kind


def cmd_upgrade_forge(args):
    """G9.1: only to the version this release pins; a backup first; the binary checksum-checked; started and
    checked; on any failure the old binary and the database as it was are put back."""
    p = paths()
    st = load_state(p)
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
    try:
        door(p, st["port"], fresh=True, again="upgrade-forge")     # G8.7: before anything is stopped or made
    except DoorShut as e:
        say(str(e) + " Nothing was upgraded.")
        return 1
    ok, why = take_lock(p)
    if not ok:
        say(f"No upgrade: {why}. Run upgrade-forge again when it has ended.")
        return 3
    rollback = os.path.join(p["data"], "upgrade-rollback")
    try:
        say(f"upgrade-forge: Gitea {cur} to {new}. First, a backup.")
        claim(os.path.join(p["data"], "backups"), "tree:other")
        if make_backup(p, st, os.path.join(p["data"], "backups")) != 0:
            say("The backup failed, so the forge was not touched. Run upgrade-forge again once the backup's "
                "reason above is dealt with; it makes the backup first.")
            return 1
        save_state(p, st)
        try:
            fetch_forge(p, test_setting("WSM_GITEA_BASE"))
        except SystemExit as e:
            say(f"{e} The forge was not touched. Run upgrade-forge again.")
            return 1
        service(p, "stop")
        gone = wait_stopped(p, st["port"])
        if gone == "other":
            # G8.7 (the owner's condition ii): the forge did stop; another program took its port meanwhile. Not
            # "did not stop": nothing is upgraded, nothing is sent to that program, and the forge stays stopped,
            # since its service cannot start on a port another program holds.
            say(f"problem: the forge stopped for the upgrade, and another program took its port 127.0.0.1:"
                f"{st['port']} meanwhile, so nothing was upgraded and nothing was sent to that program: no key, no "
                "copy. Run setup: it moves the forge to a free port and starts it again; every copy stays. Then run "
                "upgrade-forge again.")
            return 1
        if gone != "stopped":
            service(p, "start")
            say("The forge did not stop, so it was not upgraded. Run upgrade-forge again; if the forge still does "
                "not stop, run status." if gone == "running" else
                f"Who holds the forge's port 127.0.0.1:{st['port']} could not be read after the forge was told to "
                "stop, so it was not upgraded. Run upgrade-forge again.")
            return 1
        shutil.rmtree(rollback, ignore_errors=True)
        os.makedirs(rollback, mode=0o700)
        claim(rollback, "tree:other")
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
            try:
                if not wait_forge(p, st["port"], seconds=FIRST_START_WAIT, again="upgrade-forge"):
                    why = "the new forge did not answer within 90 s"
                else:
                    code, ver = Forge(p, st).call("GET", "/version")
                    if code != 200 or (ver or {}).get("version") != new:
                        why = f"the new forge says it is {(ver or {}).get('version')}, not {new}"
            except DoorShut as e:
                why = ("another program holds the forge's port" if e.kind == "other" else
                       "who holds the forge's port could not be read") + ", so nothing was sent to it"
        if why:
            service(p, "stop")
            wait_stopped(p, st["port"])
            shutil.copy2(os.path.join(rollback, "gitea.db"), db)
            shutil.copy2(os.path.join(rollback, "app.ini"), p["app_ini"])
            st["gitea"] = cur
            save_state(p, st)
            service(p, "start")
            try:
                back = wait_forge(p, st["port"], seconds=FIRST_START_WAIT)
            except DoorShut:
                back = False
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
        claim(target)
    finally:
        drop_lock(p)
    say(f"self-update: {VERSION} to {tag}, replaced whole at {target}. The forge was not changed.")
    pin = re.search(r'^GITEA_VERSION = "([\d.]+)"', data.decode("utf-8", "replace"), re.M)
    if pin and vt(pin.group(1)) > vt(load_state(p).get("gitea") or GITEA_VERSION):
        say(f"{tag} pins Gitea {pin.group(1)}: run upgrade-forge when you choose (it makes a backup first).")
    return 0


def cmd_uninstall(args):
    """G9.3: everything but the folder of copies goes; that folder goes only on the person's typed word, typed
    at a terminal (it cannot be undone, so nothing piped in counts)."""
    p = paths()
    found = other_install(p)
    # M150: a launch agent file at the skill's path that could not be read never keeps a person from leaving; while
    # it stands, uninstall takes off and removes only what it can show to be this home's own (below).
    limit = found if found and found[2] else None
    other = None if limit else other_install_stop(p, "uninstall", found)
    if other:
        say(other)
        return 2
    if args.delete_copies and not sys.stdin.isatty():
        line = own_command(p, "uninstall", "--delete-copies")
        say("Needs you: deleting the copies cannot be undone, so the person does it themselves: in a terminal "
            f"window of their own, they run {line} and type the number of copies. Nothing was changed.")
        return 2
    # Rule 6 (M65): a record that could not be read is never "setup made nothing": then no folder setup made is
    # taken away, it is said, and the record is never written over.
    raw = load_json(p["state"], None) if os.path.exists(p["state"]) else None
    record = "read" if isinstance(raw, dict) else ("unreadable" if os.path.exists(p["state"]) else "none")
    st = raw if record == "read" else {}
    # M158 (G6.25): while the record of what the skill made is lost, nothing in the skill's folders is written,
    # removed or made (the state file too), and no new record is started
    lost = read_made(p)[1] if os.path.exists(p["data"]) or os.path.exists(p["conf"]) else None
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
    removed, linger_said, left_service, foreign, unreadable, kept, told = [], "", [], [], None, [], []
    try:
        uid = str(os.getuid())
        if system()[0] == "darwin":
            # G1.17 and the family's rule 30: both jobs are told off and read first; while either is not read gone,
            # nothing at all is removed (a job launchd still holds would go on starting a program taken from it).
            # M150: while the forge's file could not be read, a job is told off only when launchd shows it running
            # this home's own copy; any other loaded job is named and left loaded, and its file left with it.
            # A job launchd could not answer for is "could not be read" (a reviewer's trial, 11-26): never "loaded",
            # never left while the rest goes; nothing is told off until every job is read.
            both = ((LAUNCHD_LABEL, p["plist"], "the forge's job"),
                    (SYNC_LABEL, os.path.join(os.path.dirname(p["plist"]), SYNC_LABEL + ".plist"), "the sync's job"))
            held = {label: launchd_held(label) for label, _, _ in both} if limit else {}
            unread_jobs = [f"{words} ({label})" for label, _, words in both if limit and held[label] is None]
            if unread_jobs:
                again = own_command(p, "uninstall", *(["--delete-copies"] if args.delete_copies else []))
                say("Not now: whether launchd holds " + " and ".join(unread_jobs) + ", and what "
                    + ("it runs" if len(unread_jobs) == 1 else "they run") + f", could not be read, and {limit[1]} "
                    f"could not be read either ({limit[2]}), so uninstall cannot show what is this home's. Nothing "
                    f"was removed and nothing was taken off. The one next step: move {limit[1]} out of "
                    f"{os.path.dirname(limit[1])} (into a folder of your own, to keep it), then run uninstall "
                    f"again: {again}")
                return 3
            jobs = []
            for label, plist, words in both:
                got = held.get(label)
                if limit and got == "absent":
                    jobs.append((plist, words, "gone", 0.0, 113))
                elif limit and not job_is_ours(p, label, got[0]):
                    kept.append(f"{words} ({label}), loaded in launchd, which runs another copy than this home's, "
                                f"and its file {plist}")
                else:
                    jobs.append((plist, words, *launchd_off(p, label)))
                    told.append(words)
            if any(answer != "gone" for _, _, answer, _, _ in jobs):
                said = []
                for plist, words, answer, waited, code in jobs:
                    said.append(f"{words} is off, and its file {plist} stays too" if answer == "gone" else
                                f"launchd still had {words} {waited:.0f} s after it was told to take it off"
                                if answer == "there" else f"whether launchd let go of {words} could not be read "
                                f"(launchctl print answered code {code})")
                again = own_command(p, "uninstall", *(["--delete-copies"] if args.delete_copies else []))
                say("Not now: " + "; ".join(said) + f". Nothing was removed. Run uninstall again in a minute: {again}")
                return 3
            for plist, _, _, _, _ in jobs:
                if limit and plist == limit[1]:
                    continue                    # M150: named on its own below, and left byte for byte
                take_service_file(p, plist, removed, left_service)
        else:
            # G1.18 and the family's rule 30: every unit is told to stop and read stopped first; while one is not,
            # nothing at all is removed (a unit systemd still runs would go on running a program taken from it).
            # M163 (G9.18): while the forge's unit could not be read, a unit is told off only when systemd's own start
            # line for it names this home's copy (the timer with the sync's service); one that names another copy, or
            # none, is named and left as it is, its file with it; one systemd could not answer for is a "Not now".
            only, kept_files, base = None, set(), os.path.dirname(p["unit"])
            if limit:
                both = (("forge", SERVICE + ".service", p["unit"], "the forge's service", (None,)),
                        ("sync", SYNC_UNIT + ".service", os.path.join(base, SYNC_UNIT + ".service"),
                         "the sync's service", (".service", ".timer")))
                held = {which: systemd_parsed(unit) for which, unit, _, _, _ in both}
                unread_units = [f"{words} ({unit})" for which, unit, _, words, _ in both if held[which] is None]
                if unread_units:
                    again = own_command(p, "uninstall", *(["--delete-copies"] if args.delete_copies else []))
                    say("Not now: what systemd runs for " + " and ".join(unread_units) + " could not be read, and "
                        f"{limit[1]} could not be read either ({limit[2]}), so uninstall cannot show what is this "
                        f"home's. Nothing was removed and nothing was taken off. The one next step: move {limit[1]} "
                        f"out of {os.path.dirname(limit[1])} (into a folder of your own, to keep it), then run "
                        f"uninstall again: {again}")
                    return 3
                only = []
                for which, unit, f, words, suffixes in both:
                    if unit_is_ours(p, which, held[which][0]):
                        only += list(suffixes)
                    else:
                        runs = ("which systemd runs another copy than this home's" if held[which][0] else
                                "for which systemd shows no start line, so it runs nothing")
                        timer = (f", with the sync's timer ({SYNC_UNIT}.timer) and its file "
                                 f"{os.path.join(base, SYNC_UNIT + '.timer')}" if which == "sync" else "")
                        kept.append(f"{words} ({unit}), {runs}, and its file {f}{timer}")
                        kept_files.add(f)
                        if which == "sync":
                            kept_files.add(os.path.join(base, SYNC_UNIT + ".timer"))
            units = systemd_off(p, only) if only is None or only else []
            if any(answer != "stopped" for _, answer, _, _ in units):
                said, no_bus = [], False
                for words, answer, waited, what in units:
                    if answer == "stopped":
                        said.append(f"{words} is stopped, and its file stays too")
                    elif answer == "running":
                        said.append(f"systemd still had {words} {what} {waited:.0f} s after it was told to stop it")
                    else:
                        said.append(f"whether systemd stopped {words} could not be read (systemctl --user answered "
                                    f"{what})")
                        no_bus = no_bus or bool(NO_USER_BUS.search(what))
                again = own_command(p, "uninstall", *(["--delete-copies"] if args.delete_copies else []))
                say("Not now: " + "; ".join(said) + ". Nothing was removed. " + (
                    "This shell cannot reach your account's own systemd (it has no user bus), so run uninstall again "
                    f"from a login of your own to this computer, such as ssh or a terminal at the screen: {again}"
                    if no_bus else f"Run uninstall again in a minute: {again}"))
                return 3
            told += [words for words, _, _, _ in units]
            for f in (p["unit"], os.path.join(base, SYNC_UNIT + ".service"), os.path.join(base, SYNC_UNIT + ".timer")):
                if limit and f == limit[1] or f in kept_files:
                    continue                    # M150: named on its own below, and left byte for byte; M163: named
                take_service_file(p, f, removed, left_service)
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
            if record == "read" and os.path.exists(p["state"]) and not lost:
                save_state(p, st)
        # Rule 23: what the skill made, by its record, read now (a person's file may have arrived since setup).
        # Rule 8: a person who wants to leave is never refused because of the state of their own data: what the
        # skill did not make stays where it is and is named, and the leave goes on to its end for the rest.
        rec_now, unreadable = read_made(p)
        if rec_now is not None:
            foreign = not_ours(p)
            remove_made(p, [os.path.dirname(p["bin"]), p["cache"], p["conf"], p["script"]], removed)
    finally:
        if os.path.exists(p["data"]):
            drop_lock(p)
    n = len([r for r in (st.get("repos") or {}).values() if r.get("forge_id")])
    rc = 0
    copies_here = os.path.isdir(p["forge"]) and bool(set(os.listdir(p["forge"])) & FORGE_NAMES)
    if args.delete_copies and os.path.exists(p["data"]) and unreadable:
        # M158 (G6.25): the number is not asked; nothing in the skill's folders goes; the folder is the person's now
        say(f"The copies were not deleted: {unreadable}, so the skill cannot tell which files in {p['data']} it "
            f"made, and deletes none of them. That folder, with every copy in it, is still at {p['data']}, and it is "
            "yours now, as it is.")
        rc = 1
    elif args.delete_copies and copies_here:
        say(f"To delete the folder of copies ({n} copies, {p['data']}), type the number of copies it holds. Only "
            "what the skill made goes; anything else in it stays where it is, and is named:")
        answer = input("> ").strip()
        if answer == str(n):
            foreign = sorted(set(foreign) | set(not_ours(p)))     # read again: a file may have arrived meanwhile
            take_data(p, removed, f"the folder of copies ({n} copies)", f"{p['forge']}: the {n} copies (the "
                      "forge's own folders in it)")
        else:
            say(f"That is not {n}. The copies were not deleted. If you mean to delete them, run uninstall "
                f"--delete-copies again and type {n}.")
            rc = 1
    elif args.delete_copies and os.path.exists(p["data"]):         # no copies left in it: nothing to ask
        foreign = sorted(set(foreign) | set(not_ours(p)))
        take_data(p, removed, "the skill's folder", None)
    if record == "read":
        take_made_folders(st, removed)
    uninstall_census(p, removed, linger_said, n, st, record, foreign, unreadable, left_service,
                     unread_service=limit, kept_jobs=kept, told_off=told)
    if unreadable and holds_mirror(p):     # M158: the way to go on, named once, last
        say(USE_FOLDER_STEP + own_command(p, "setup", "--use-this-folder"))
    return rc


def take_data(p, removed, what, copies_line):
    """The delete: what the record holds in the data folder goes, by name; the record goes last, once nothing it
    names is still here; the folder goes once it is empty."""
    gone = len(removed)
    remove_made(p, [p["data"]], removed)
    rec_left, why = read_made(p)
    try:
        if rec_left is not None and not any(os.path.lexists(k) for k in rec_left if k != os.path.abspath(p["data"])):
            os.remove(os.path.join(p["data"], MADE_RECORD))
        if not os.listdir(p["data"]):
            os.rmdir(p["data"])
    except OSError:
        pass
    if not os.path.exists(p["data"]):
        removed[gone:] = [f"{p['data']}: {what} and the record of what the skill did here, each thing the skill "
                          "made in it by name"]
    elif copies_line and (not os.path.exists(p["forge"]) or set(os.listdir(p["forge"])).isdisjoint(FORGE_NAMES)):
        removed.insert(gone, copies_line)


def take_service_file(p, f, removed, left):
    """A service file goes only while it is exactly as the skill wrote it (its recorded SHA-256); else it is named
    and left."""
    if not os.path.lexists(f):
        return
    rec, why = read_made(p)
    state, unread = service_file_state(p, rec, f)
    if state == "no" and rec is None:
        # M158 (G6.25): the record is lost, so the file is read against what this home's setup writes there now
        mine = service_as_written(p, f)
        if mine:
            os.remove(f)
            removed.append(f"{f} (byte for byte what setup writes there for this home)")
        else:
            left.append(f"{f} (" + ("it could not be read" if mine is None else "it is not what setup writes there "
                        "now") + ", and the record of what the skill wrote was not read, so the tool cannot tell "
                        "whose it is; left as it is)")
        return
    if state == "could not be read":
        left.append(f"{f} (it could not be read: {unread}, so the tool could not tell whether it is as the skill "
                    "wrote it; left byte for byte)")
    elif state == "yes":
        os.remove(f)
        removed.append(f)
        unclaim(p, [f])
    else:
        left.append(f"{f} (not as the skill wrote it: changed since, or written by something else)")


def remove_made(p, targets, removed):
    """Remove what the record says the skill made at or inside each target: a file by its name, a tree a program
    of the skill's wrote (checked by its top-level names first), a folder only once it is empty. Each removed thing
    is named; nothing the record does not hold is touched."""
    rec, why = read_made(p)
    if rec is None:
        return
    targets = [os.path.abspath(t) for t in targets]
    mine = sorted((k for k in rec if any(k == t or inside(k, t) for t in targets)), key=len, reverse=True)
    trees = [k for k in mine if rec[k].startswith("tree:")]
    mine = [k for k in mine if not any(inside(k, t) for t in trees)]     # they go with their tree
    gone, caches, as_dir = [], 0, []
    for k in mine:
        kind = rec[k]
        try:
            if kind == "file" and (os.path.isfile(k) or os.path.islink(k)):
                os.remove(k)
                removed.append(k)
            elif kind.startswith("tree:") and os.path.isdir(k) and not os.path.islink(k):
                allowed = FORGE_NAMES if kind == "tree:forge" else GIT_NAMES if kind == "tree:git" else None
                names = os.listdir(k)                 # read now, at the moment of removal
                other = [x for x in names if allowed is not None and x not in allowed and not x.endswith(".lock")]
                if other:                             # what the program wrote goes; the rest and the folder stay
                    for x in names:
                        if x not in other:
                            e = os.path.join(k, x)
                            shutil.rmtree(e) if os.path.isdir(e) and not os.path.islink(e) else os.remove(e)
                    as_dir.append(k)                  # still the skill's folder: it goes once it is empty
                else:
                    shutil.rmtree(k)
                if kind == "tree:git":
                    caches += 1
                elif not other:
                    removed.append(k)
            elif kind == "dir" and os.path.isdir(k) and not os.path.islink(k) and not os.listdir(k):
                os.rmdir(k)
                removed.append(k)
            if not os.path.lexists(k):
                gone.append(k)
        except OSError:
            pass
    for t in targets:                       # a target folder of the skill's own goes once it is empty
        try:
            if t not in rec and os.path.isdir(t) and not os.path.islink(t) and not os.listdir(t) and t != p["data"]:
                os.rmdir(t)
                removed.append(t)
        except OSError:
            pass
    if caches:
        removed.append(f"{p['cache']}: {caches} cache repositories the skill made")
    unclaim(p, gone)
    if as_dir:
        with made_lock(p, make=False):
            rec, why = read_made(p)
            if rec is not None:
                rec.update({k: "dir" for k in as_dir})
                write_made(p, rec)


def downloaded_from(here):
    """G9.8 (M63): ("folder", the skill's own folder) when `here` is <folder>/scripts/mirror.py in a copy of the skill's
    repository or Claude Code's plugin folder (it holds skills/mirror/SKILL.md); otherwise ("file", `here`): a script
    run on its own is named as the one file it is, never the folder around it (a home, a downloads folder)."""
    root = os.path.dirname(os.path.dirname(here))
    if (os.path.basename(os.path.dirname(here)) == "scripts"
            and os.path.isfile(os.path.join(root, "skills", "mirror", "SKILL.md"))):
        return "folder", root
    return "file", here


def still_here(p, st, record="read"):
    """G9.8 (M63, the family's rule 8, 2026-11-23: an agent invents where the tool is silent): everything still on
    this computer because of the skill that uninstall does not take, with where it is and whose it is to delete."""
    lines = []
    reg, catalog = plugin_record(st)
    root = None if plugin_registry_for(os.path.join(st.get("setup_from") or os.sep, "scripts", "mirror.py")) else st.get("setup_from")
    listed, key = plugin_read(st)
    if reg and listed is not False:
        # G9.14 (M104, the family's rule 29 (e)): named by Claude Code's own list, never by a version folder; the one
        # command whole, with the names the list or the record holds.
        key = key or (f"{PLUGIN}@{catalog}" if catalog else None)
        how = (f"claude plugin uninstall {shlex.quote(key)}" if key and system()[0] != "darwin" else "")
        unread = "" if listed else f", if Claude Code still has them (its list of plugins, {reg}, could not be read)"
        # Seen 2026-11-24 (Claude Code 2.1.331, Linux, a stock virtual machine): its uninstall takes the plugin off its
        # list at once and leaves the folder, marked; the folder's removal 14 days later is its documentation's word.
        lines.append(f"Claude Code's folder for this skill as a plugin, and its entry in Claude Code{unread}. They "
                     "are Claude Code's. Claude Code's own plugin uninstall takes this plugin off Claude Code's list at "
                     "once; its folder in Claude Code's cache stays, and Claude Code's documentation says it removes "
                     "that folder 14 days later. Nothing runs from them now."
                     + (f" Its command for this plugin:\n      {how}" if how else ""))
    elif root and os.path.isdir(root):
        lines.append(f"{root}: the folder the skill was downloaded into (a copy of its repository). It is yours; "
                     "delete it like any folder when you want. Nothing runs from it now.")
    elif st.get("setup_from_file") and os.path.isfile(st["setup_from_file"]):
        lines.append(f"{st['setup_from_file']}: the copy of the script setup was run from. It is yours; delete it "
                     "when you want. Nothing runs from it now.")
    elif not root and not os.path.exists(p["state"]):
        lines.append("The folder the skill was downloaded into, if it is still there (a copy of its repository, or "
                     "Claude Code's plugin folder): no record of where it is is left on this computer. It is yours, "
                     "or Claude Code's for a plugin; nothing runs from it now.")
    made = [d for d in (st.get("made_folders") or []) if os.path.isdir(d)]
    holds = [d for d in made if os.path.exists(p["data"]) and (p["data"] + os.sep).startswith(d + os.sep)]
    if holds:
        lines.append(", ".join(holds) + ": folders setup made to hold the folder of copies. They stay while it "
                     "stays, and go with it once they are empty.")
    for d in made:
        if d not in holds:
            lines.append(f"{d}: a folder setup made, left because other files are in it now. It is yours.")
    if record == "unreadable":
        lines.append(f"Folders setup made around its own (on a fresh computer, such as {os.path.dirname(p['unit'] if system()[0] != 'darwin' else p['plist'])}): "
                     f"the record of them, {p['state']}, could not be read, so none was removed. Any of them that "
                     "is empty is yours to delete or keep.")
    elif record == "none":
        lines.append("Folders setup made around its own, if any are still here: no record of them is left on this "
                     "computer, so none was removed.")
    if system()[0] != "darwin":
        lines.append("Any lines of the service in this computer's journal (journalctl --user): the system ages them "
                     "out by itself.")
    return lines


def stayed_for(p, foreign):
    """Each of the skill's folders still here because something it did not make is in it: folder -> those things."""
    roots = own_roots(p)
    held = {}
    for f in foreign:
        if not os.path.lexists(f):
            continue
        d = os.path.dirname(f)
        while os.path.isdir(d) and any(d == r or inside(d, r) for r in roots):
            held.setdefault(d, []).append(os.path.relpath(f, d))
            d = os.path.dirname(d)
    return held


def uninstall_census(p, removed, linger_said, n, st=None, record="read", foreign=(), unreadable=None,
                     left_service=(), unread_service=None, kept_jobs=(), told_off=()):
    """Rule 8: the last thing uninstall prints says what is gone now, what is kept, and (G9.8) what is still on this
    computer because of the skill that uninstall does not take. Run twice, it says so. Rule 23: "gone" only of what
    it removed by name; what it did not make is named as the person's, with each folder that stayed for it."""
    say("uninstall: what is gone, and what is kept.")
    if removed:
        say("Gone now:")
        for r in removed:
            say(f"  {r}")
    if unreadable:
        # M158 (G6.25): said as that; each kept key named, with why it stayed
        keys = []
        try:
            keys = sorted(n for n in os.listdir(p["keys"]) if n.endswith(".key"))
        except OSError:
            pass
        held = ([f"the GitHub key for {n[:-4]} ({os.path.join(p['keys'], n)})" for n in keys]
                + [f"the forge's key for its '{MIRROR_USER}' account ({p['mirror_key']})"] * os.path.lexists(p["mirror_key"])
                + [f"the password of your Gitea account ({p['password']})"] * os.path.lexists(p["password"]))
        say(f"Not taken: {unreadable}, so uninstall cannot tell which files in its folders the skill made, and took "
            "nothing from them; nothing there was deleted or written over. Still there, each as it was: "
            + (f"the folder of copies, {p['data']}, with the forge and its copies; " if holds_mirror(p) else "")
            + ", ".join([w for w, there in (
                (f"the forge's binary ({p['bin']})", os.path.lexists(p["bin"])),
                (f"the copies' caches ({p['cache']})", os.path.lexists(p["cache"])),
                (f"the settings ({p['settings']})", os.path.lexists(p["settings"]))) if there]
                        or ["the other files in those folders"])    # each named only where it is (walk6 (C), 11-26)
            + ("; and, kept because that record cannot show them to be the skill's to take: " + "; ".join(held)
               if held else "") + ". Nothing of the skill's runs. They are yours, to keep or delete after you look "
            "at them.")
    if foreign:
        say("Not the skill's, so left where they are (yours, or put there since setup):")
        for x in foreign:
            say(f"  {x}")
        held = stayed_for(p, foreign)
        if held:
            say("Folders of the skill's that stayed because they hold those:")
            for d in sorted(held):
                say(f"  {d}: holds " + ", ".join(sorted(held[d])[:3]) + (", and more" if len(held[d]) > 3 else ""))
    if (left_service or unread_service) and told_off:
        say("Switched off first, and read off before anything was removed: " + "; ".join(told_off) + ".")
    for unit, said in SYSTEMD_REFUSED:
        link = os.path.join(os.path.dirname(p["unit"]), "timers.target.wants" if unit.endswith(".timer")
                            else "default.target.wants", unit if unit.endswith(".timer") else unit + ".service")
        if os.path.lexists(link):
            say(link_left_line(link, unit, said))
    if left_service:
        say("Not taken, and still there: " + "; ".join(left_service) + ". It is switched off; look at it, and keep "
            "or delete it as you choose.")
    if kept_jobs:
        say("Left as they are, because the tool cannot show them to be this home's: " + "; ".join(kept_jobs) + ".")
    if unread_service:
        f, why = unread_service[1], unread_service[2]
        say(f"Not taken, and left byte for byte: {f}. It could not be read: {why}, so the tool could not tell whose "
            f"it is. It is still in {os.path.dirname(f)}, "
            + ("the folder launchd reads at a login" if system()[0] == "darwin" else
               "the folder systemd reads this account's services from") + "; look at it, and keep or delete it as you "
            "choose.")
    if not removed and not unreadable and not foreign and not left_service and not unread_service and not kept_jobs:
        say("Gone now: nothing; the service, the sync timer, the forge's binary, the settings and the keys were "
            "already gone.")
    if linger_said:
        say(linger_said)
    record_here = os.path.exists(os.path.join(p["data"], MADE_RECORD))
    rec_kept = read_made(p)[0] or {}
    for x in sorted(k for k, v in rec_kept.items() if v == "set-aside record" and os.path.lexists(k)):
        # M158 (G6.25): the record setup --use-this-folder set aside is the person's, and named among what is kept
        say(f"Kept: {x}, the record of what the skill made that could not be read, set aside by setup "
            "--use-this-folder as it was. It is yours.")
    if unreadable:
        pass
    elif os.path.exists(p["forge"]) and record_here and set(os.listdir(p["forge"])) & FORGE_NAMES:
        # M144: while the skill's own copy is there (or could not be read), the words as before; once it is gone, a
        # way that works with the copy of the tool that is there, and one with no copy of it left. When no copy of the
        # tool was read there (uninstall run from the own copy itself), only what was read: the own copy is gone, and
        # another copy only as an "if" (the maintainer's ruling, 11-25: no new read is made for it)
        how = ("with: uninstall --delete-copies" if path_there(p["script"]) is not False else
               f"with: {own_command(p, 'uninstall', '--delete-copies')}, in a terminal window of your own (it asks "
               "you to type the number of copies), or, once no copy of this skill is left on this computer, as you "
               "delete any folder of yours" if tool_copy(p) else
               "as you delete any folder of yours; the skill's own copy is gone now, and if another copy of this skill "
               "is on this computer, its uninstall --delete-copies does it, in a terminal window of your own")
        say(f"Kept: the folder of copies, {p['data']} ({n} copies), with the record of what the skill did here. It "
            f"is yours; delete it only when you are sure, {how}. Setup run again later finds this folder and goes on "
            "from it: the mirror account gets a new key, and your Gitea account and its password are left as they "
            "are.")
    elif record_here:
        say(f"Kept: {p['data']}, the record of what the skill did here. It is yours.")
    elif not foreign:
        say("Kept: nothing; there is no folder of copies and no record of the skill's on this computer now.")
    left = still_here(p, st or {}, record)
    if left:
        say("Still on this computer, and not uninstall's to take:")
        for x in left:
            say(f"  {x}")
    if kept_jobs:
        say("What uninstall could not show to be this home's is named above, and was left as it is; it is yours, "
            "to keep or delete after you look at it.")
    elif foreign or left_service or unreadable or unread_service:
        say("The mirror is off this computer. What stayed, and why, is named above; it is yours, to keep or delete "
            "after you look at it.")


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
    env = forge_env(p)
    binary = running_bin(p)
    os.execve(binary, [binary, "--config", p["app_ini"], "--work-path", p["forge"], "web"], env)


def main(argv=None):
    # Rule 28: whatever umask it was started with (000 in some containers and job runners), nothing the tool or the
    # forge it starts makes can be written by other accounts; the person's own read bits are left as they are.
    os.umask(os.umask(0o022) | 0o022)
    ap = argparse.ArgumentParser(prog="mirror.py", description=__doc__.splitlines()[0])
    ap.add_argument("--version", action="version", version=f"Windstorm Mirror for GitHub {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="install the forge as your own service")
    s.add_argument("--dry-run", action="store_true", help="say what setup would do and change nothing")
    s.add_argument("--from", dest="from_file", metavar="BACKUP", help="on a new computer: bring every copy back from a backup")
    s.add_argument("--linger", action="store_true",
                   help="on Linux: the person said yes to lingering, so the mirror runs after a restart with no one "
                        "logged in")
    s.add_argument("--use-this-folder", action="store_true",
                   help="the person's own word: go on with a folder that holds a mirror whose record of what the "
                        "skill made is lost (its stop names it)")
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
    g = sub.add_parser("go", help="shows you what will be copied and takes your go for the first copy (run it yourself, in a "
                       "terminal window of your own)")
    g.set_defaults(fn=cmd_go)
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
    _made_start.update(data=os.path.abspath(paths()["data"]), fresh=home_fresh(paths()), use=False)    # M158: read before the command writes anything
    lines, stop = settings_lines()
    for line in lines:
        say(line)
    if stop:
        return 2
    aside = set_aside_folder(__file__)
    if aside:
        own, line = paths()["script"], own_command(paths(), a.cmd)
        say(f"Stopped: this copy of the skill is in a folder Claude Code set aside when its plugin was removed or "
            f"updated ({aside}), and nothing runs from there. Nothing was done. "
            + (f"The skill set up on this computer is {own}; run it there: {line}" if os.path.exists(own) else
               "Nothing of the skill's is set up on this computer; to use it, install the plugin again and run "
               "setup."))
        return 3
    floor = folders_floor(paths(), a.cmd + (f" {a.key_cmd}" if a.cmd == "key" else ""))
    if floor:
        say(floor)
        return 2
    try:
        if a.cmd not in ("uninstall", "status", "run-forge", "expose"):
            # M151, M154, M157: the kept files are read first, so a command that cannot read one stops before it
            # changes anything. Uninstall never stops on them (a leave always goes to its end); status says them in
            # its lines.
            load_state(paths())
            load_settings(paths())
        if a.cmd in ("setup", "sync", "go", "restore", "backup", "upgrade-forge"):
            answer, why = forge_ini_read(paths())   # M159: never a new settings file over one in its place
            if answer == "could not be read":
                raise KeptUnread(paths()["app_ini"], why)
        return a.fn(a)
    except DoorShut as e:
        say(str(e))
        return 1
    except KeptUnread as e:          # a kept file that could not be read stops the command before any change
        say(kept_stop_words(paths(), e, a.cmd))
        return 2
    finally:
        if _setup_held[0]:
            _setup_held[0] = False
            drop_lock(paths())


if __name__ == "__main__":
    sys.exit(main())
