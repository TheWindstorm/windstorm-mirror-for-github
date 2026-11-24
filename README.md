# Windstorm Mirror for GitHub

**A second, always-current copy of all your GitHub projects on a computer you own.**

Status: 0.1.0 (2026-11-25)

This is not made, endorsed or supported by GitHub. GitHub is a trademark of GitHub, Inc.

## Set it up

Say this to your AI agent (Claude Code, or any agent that can run commands on your computer):

> Set up https://github.com/TheWindstorm/windstorm-mirror-for-github on this computer.

**If you are the agent:** clone this release with the line below, read `skills/mirror/SKILL.md` and follow its steps in order.
Stop at each step marked **Person**, say what it gives you to say, and wait for the person. Do nothing that it
does not tell you to do.

```bash
git clone --branch v0.1.0 https://github.com/TheWindstorm/windstorm-mirror-for-github
```

**Or keep it as a Claude Code plugin**, so the skill is there in every session. A plugin installed during a
session loads only after Claude Code is restarted.

```bash
claude plugin marketplace add https://github.com/TheWindstorm/windstorm-mirror-for-github.git
claude plugin install windstorm-mirror-for-github@windstorm-mirror-for-github
```

Then start Claude Code again and say: **set up my GitHub mirror**.

On a Mac, run setup from a session logged in at that Mac's screen: the forge runs as a launch agent, which starts
only in such a session (over ssh into an account no one is logged in to at the screen, setup says so and starts nothing).

## What to expect

The copy is as current as the last sync, and `status` says when that was. While this computer is off or asleep,
nothing is copied. GitHub's issues, pull requests, releases and wikis are not in the copy, so this is a second copy
of your code, not of everything GitHub holds.

## What it runs, and where it connects

Python standard library only. Over the network it reaches only GitHub (its API, with or without a key, and git),
Gitea's download site for the pinned binary, and any host those two send it on to for that same request; its own
forge on 127.0.0.1; and an address the person names for `restore --to`, through git. The programs it runs are `git`,
the pinned Gitea binary, the service manager (`systemctl` and `loginctl`, or `launchctl`), the system's address
list (`ip` or `ifconfig`), on Linux `pkcheck`, read-only and never asking for a password, to ask whether the
person may switch on lingering themselves, on a Mac the two settings it reads (`defaults read` of the
automatic-login account, `fdesetup status`), both read-only, and Python itself to check a self-update.

Your git hooks do not run in the mirror's own copies, and your proxy is never asked to reach the mirror's own
forge on this computer; it still carries the mirror's calls to GitHub.

After `restore --to`, check the new home yourself: the restore pushes from the mirror's own copy, so a
pre-push hook of yours does not check that push.

## What setup does

`setup --dry-run` shows all of this first, and changes nothing.

- It downloads Gitea 28.4.0, a free, open-source program much like GitHub, from dl.gitea.com, and checks it
  against a pinned SHA-256. Gitea runs from `~/.local/share/windstorm-mirror/` and listens on this computer only
  (127.0.0.1), with sign-up closed. Setup needs no administrator rights. Setup and its dry run check the program
  against the pinned SHA-256 each time they run; the service's start does not check it again.
- It makes two Gitea accounts: yours, and `mirror`, which owns the copies. Each password and each key is kept in a
  file that only you can read.
- It runs Gitea as your own service, and syncs every 15 minutes. On Linux that is a systemd user unit, with the
  sync timer beside it, in `~/.config/systemd/user/`; switching them on, systemd links them in that folder's
  `default.target.wants/` and `timers.target.wants/`. On macOS, two launch agents in `~/Library/LaunchAgents/`.
- Where a folder these go in is not on your computer yet (a fresh Linux account has no `~/.config` or
  `~/.local/share`), setup names it in the dry run, makes it and keeps a note of it; uninstall takes it away again
  once it is empty.
- **Settings you can set.** `WSM_HOME` moves every folder of the skill's under it (`data/` and `config/`). On
  Linux, `XDG_DATA_HOME` and `XDG_CONFIG_HOME` move the data and the settings folders as they do for other
  programs. A command whose folders one of them moved says so first; setup and uninstall stop, having changed
  nothing, when this computer's service already runs the skill from another home, and say which. Setup also stops
  at your home folder, or at a folder that already holds anything the skill did not make. The skill reads no other
  setting of yours.
- **Only what it made.** The skill keeps a record of every file and folder it writes in its folders. Uninstall
  takes only what that record holds, a folder only once it is empty. Anything else it finds in its folders when it
  runs stays where it is: uninstall names it as yours, with each folder that stayed because of it, and still goes
  to its end for everything that is the skill's.
- **The folders above it.** A file is only as closed as the folders above it. Setup reads every folder from your
  home down to what the skill runs (its service files, its own copy, the forge's program) and to what decides what
  runs (the forge's settings, the skill's settings and state, the Python its service names). A folder of the skill's
  own that others may write in, setup closes. One of yours, it never changes; when another account on this computer
  can really reach it and write in it, setup, its dry run, the go's lines and `status` name it, with the one
  command that closes it (`chmod go-w` and the folder), until it is closed. A folder in your own group with nobody
  else in it is not named, nor anything inside a home only you can enter. A folder on the way that is not yours is
  named with whose it is and no command: you cannot change it, only the computer's administrator can. Two names
  with one user number are one account; an account whose groups cannot be read is said as that, and the others are
  still worked out. What is read is the folders' mode bits and who is in their groups; access
  lists (ACLs) are not read.
- **After a restart.** On Linux, setup can switch on *lingering* for your account. That means the mirror keeps
  running when you are logged out and after a restart, because your account's background services are allowed to
  start without you. The dry run asks your computer whether you may switch lingering on yourself, and says what it
  found. Setup asks you: `setup --linger` is yes, and `setup --no-linger` is no, in which case the mirror syncs only while you are logged in. On a
  Mac, the mirror comes back by itself after a restart only if the Mac logs you in by itself (automatic login set
  to you, and FileVault off). Otherwise it waits until you log in, and setup says so.

## What only you do

- **Install git and Python 3.9 or newer, if your computer lacks them.** On a Mac, the python3 that comes with
  Apple's developer tools is new enough (3.9.6 on macOS 13). Setup checks for git first, and says how to install it. git lfs is optional: without it, projects with large files are copied without those files, and
  setup and `status` say so. On Ubuntu or Debian: `sudo apt update && sudo apt install git`, and for large
  files `sudo apt update && sudo apt install git-lfs`. On Fedora: `sudo dnf install git`, and for large files
  `sudo dnf install git-lfs`. On a Mac: git comes with Apple's developer tools, and `xcode-select --install` is Apple's command for
  them; git lfs comes from its own site, https://git-lfs.com, and setup and sync find it by
  themselves once it is installed.
- **Make one read-only key for each owner whose projects are to be copied**: your own account, and each
  organisation you name. Each one is a fine-grained key with read-only access to Contents and Metadata. An
  organisation may have to approve a key before it works. The agent shows you the page and the settings. It never
  logs in to GitHub for you.
- **Give each key to the skill yourself, after setup, in a terminal window of your own**, with `key set`, not through your
  agent: whatever runs inside an agent's session, even a command you start yourself, is shown to the agent. The
  key is then kept in a file only your account can read. An agent that runs as you could read that file; the
  skill tells it never to, and never to put a key in its output.
- **Watch each key's end date yourself**, on GitHub's key page. 0.1.0 says a key is ending only in `status`, two
  weeks before, and only when it could read the end date; before your go it says, for each key, whether it could. A
  key that stops working is said as that, and its copies as not refreshed since their last good refresh.
- **Give the go for the first copy, yourself, in a terminal window of your own**: run `go` there (the agent
  gives you the line). It shows you which projects they are (by name, the first 20, with the whole list in a
  file), how big they are, where they go, what is not copied, what happens after a restart, when each key ends,
  and how private the copy is; the copy starts only after you press Enter there. A yes to your agent is not the
  go. Your go covers that list only: if it changes before the copy starts, nothing is copied and you run `go`
  again.
- **Say yes or no to lingering** (see "After a restart"). Setup does not choose for you.
- **Delete the copies, if you ever want to**, yourself, in a terminal window of your own:
  `uninstall --delete-copies` asks you to type how many copies there are, and refuses an answer from anywhere
  else.
- **Type the administrator's command**, if you are the administrator of this computer and it does not let you
  switch lingering on yourself: setup shows the one command. Setup itself never asks for a password.

## What is copied

Every branch and every tag of every repository that you own, or that the organisations you name own. To copy only
some of them, name them with `config include`. Large files are copied too, when git lfs is installed. When GitHub
rewrites or deletes a branch, the old commit is kept in the copy as `kept/<date>/<name>`, for 90 days.

## Two rules

The mirror is for you. It listens on this computer only, and the skill never opens it to the network or runs a
tunnel. Making it reachable from other computers is your own choice, and `mirror.py expose` says what that
would mean without changing anything.

The copy is as private as this computer. Anyone who can read this account's files can read every project in it. Disk encryption, and who uses this computer, are yours.

## Seen to work

With 0.1.0's own tool, Linux is seen. On a Mac, setup, status and uninstall are seen with it, in a test account
with no key; a sync and a restore on a Mac are not seen yet.

- **Ubuntu 24.04.5 LTS on x86-64.** Seen on a stock virtual machine with nothing of ours on it, and in accounts
  with no administrator rights:
  - setup;
  - the forge and the sync coming back across restarts;
  - a computer with no git;
  - a full sync from GitHub;
  - a cut road, a stopped forge and a slowed road, with nothing changed;
  - a rename on GitHub and back, and a deleted branch kept under `kept/`;
  - one small git lfs object, copied whole once git lfs was installed (larger ones not seen);
  - a restore onto a second computer.
  Python 3.12, git 2.43, Gitea 28.4.0.
- **The test suite, with the real Gitea 28.4.0**, on GitHub's runners: Ubuntu 24.04 on x86-64 with Python 3.9, 3.10 and
  3.13, and macOS 26 on Apple Silicon with Python 3.9 and 3.13. These runs use a stand-in for GitHub. A person's setup has not been
  seen on either.
- **macOS 13.7 on Intel: the Mac's own main account, on a Mac that carries Apple's developer tools.** Seen: two
  stand-in launch agents, shaped like the skill's forge and timer, coming back after a restart, with automatic
  login on and FileVault off. The skill itself was not run there.
- **macOS 27.2.1 on Apple Silicon: a test account that is not an administrator, on a Mac that carries Apple's
  developer tools** (its python3 3.9.6), logged in at the Mac's screen, with no GitHub key. Seen with 0.1.0's own
  tool (2026-11-24), and before it with an earlier version (2026-11-23): the skill's dry run, setup (the pinned
  Gitea for that Mac, run as the account's own launch agent on this computer's own address), status and uninstall;
  the account's top-level folder and its `~/Library/LaunchAgents` were the same before and after. The dry run and
  setup said the copy stands still after a restart until the person logs in, because that Mac does not log anyone
  in by itself. On that Mac the developer tools' Python lives in folders that belong to another account, so
  setup named them and `status` gave them as problems (exit code 1): only that Mac's administrator can change them.
  No copy from GitHub has yet been made on a Mac by a person's own account: on GitHub's macOS runners the tests
  make first copies and run restore --check with the real pinned Gitea, against a stand-in for GitHub.
- **The plugin commands above**, with no GitHub key and no GitHub login: Claude Code 2.1.280 in a Linux account,
  and 2.1.330, installed from its site, not signed in, on a stock Ubuntu 24.04. The plugin is installed and
  listed, with the skill in place. The short form (`TheWindstorm/windstorm-mirror-for-github`) worked the same
  way on 2.1.280. Versions older than 2.1.280: not seen.
- **Not seen yet:**
  - a sync and a restore with 0.1.0's own tool on a Mac: not seen by us; they need a key, and wait for a
    fine-grained read-only one. Seen with it on a Mac: setup, status and uninstall in a test account with no key
    (above), and the test suite on GitHub's macOS runners, against a stand-in for GitHub;
  - a fine-grained read-only key on a real account: not seen by us. On the rounds against the real GitHub, a
    broader key of ours stood in for it; the tool asks GitHub the same questions with either;
  - GitHub's own answer about when such a key ends: not seen by us. The tests use a stand-in for that answer; when
    the end date cannot be read, the tool says so and promises no warning;
  - a first copy from GitHub on a Mac in a person's own account, and the copy coming back after a Mac's restart
    with the skill itself: not seen by us. Seen instead: first copies and restore --check with the real pinned
    Gitea on GitHub's macOS runners, against a stand-in for GitHub; and setup, status and uninstall in a Mac test
    account with no key;
  - a Mac that logs the person in by itself, and a Mac without Apple's developer tools;
  - Debian, Fedora and other Ubuntu versions;
  - Windows (not supported in 0.1);
  - `self-update` moving to a newer version: none exists before 0.1.0. Its read of the latest release is seen at
    the release. The road that always works: Claude Code's own plugin update, then `setup`;
- **Known, and not right yet (due 0.1.1):**
  - `restore --check` counts a clone from the forge that failed as a difference in the copy, where it should say
    the copy could not be read;
  - when `git lfs` does not answer, a sync makes the copy without its large files and marks it, as if git lfs were
    not installed; the next sync that hears git lfs mends it.

## Taking it off

```bash
python3 -I ~/.local/share/windstorm-mirror/mirror.py uninstall
```

On a Mac (the path holds a space, so it is quoted):

```bash
python3 -I ~/"Library/Application Support/windstorm-mirror/mirror.py" uninstall
```

This removes the service, the timer and their links, Gitea, the settings and every key, and each folder setup
made that is empty now. It switches lingering off only if setup
switched it on. It keeps the folder of copies and the record of what the skill did here, which are yours, and the
last thing it prints says what is gone and what is kept. If a sync is running, it waits for it to end. Run twice,
it is safe. `uninstall --delete-copies`, run in a terminal window of your own, deletes that folder too, but only
after you type how many copies it holds.

Claude Code's own `claude plugin uninstall` takes the plugin away, but the forge and the sync timer keep running:
they run the copy of the script that setup made in the folder above, not the plugin's files. The command above
takes them off, and needs nothing else installed. From then on, `status` and each sync say the plugin was removed,
with that command. Nothing of the skill's acts on your Claude Code sessions, and nothing runs from the plugin's old
folder: a command run from there stops and names the copy setup made.

## Family

Part of the Windstorm skills: [Pit Stop](https://github.com/TheWindstorm/windstorm-pit-stop),
[Pit Board](https://github.com/TheWindstorm/windstorm-pit-board),
[Tardigrade](https://github.com/TheWindstorm/windstorm-tardigrade).
