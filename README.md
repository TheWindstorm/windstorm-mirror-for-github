# Windstorm Mirror for GitHub

**A second, always-current copy of all your GitHub projects on a computer you own.**

This is not made, endorsed or supported by GitHub. GitHub is a trademark of GitHub, Inc.

## Set it up

Say this to your AI agent (Claude Code, or any agent that can run commands on your computer):

> Set up https://github.com/TheWindstorm/windstorm-mirror-for-github on this computer.

**If you are the agent:** clone this repository, read `skills/mirror/SKILL.md` and follow its steps in order.
Stop at each step marked **Person**, say what it gives you to say, and wait for the person. Do nothing that it
does not tell you to do.

```bash
git clone https://github.com/TheWindstorm/windstorm-mirror-for-github
```

**Or keep it as a Claude Code plugin**, so the skill is there in every session. A plugin installed during a
session loads only after Claude Code is restarted.

```bash
claude plugin marketplace add https://github.com/TheWindstorm/windstorm-mirror-for-github.git
claude plugin install windstorm-mirror-for-github@windstorm-mirror-for-github
```

Then start Claude Code again and say: **set up my GitHub mirror**.

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

## What setup does

`setup --dry-run` shows all of this first, and changes nothing.

- It downloads Gitea 28.4.0, a free, open-source program much like GitHub, from dl.gitea.com, and checks it
  against a pinned SHA-256. Gitea runs from `~/.local/share/windstorm-mirror/` and listens on this computer only
  (127.0.0.1), with sign-up closed. Setup needs no administrator rights.
- It makes two Gitea accounts: yours, and `mirror`, which owns the copies. Each password and each key is kept in a
  file that only you can read.
- It runs Gitea as your own service, and syncs every 15 minutes. On Linux that is a systemd user unit; on macOS, a
  launch agent.
- **After a restart.** On Linux, setup can switch on *lingering* for your account. That means the mirror keeps
  running when you are logged out and after a restart, because your account's background services are allowed to
  start without you. The dry run asks your computer whether you may switch lingering on yourself, and says what it
  found. Setup asks you: `setup --linger` is yes, and `setup --no-linger` is no, in which case the mirror syncs only while you are logged in. On a
  Mac, the mirror comes back by itself after a restart only if the Mac logs you in by itself (automatic login set
  to you, and FileVault off). Otherwise it waits until you log in, and setup says so.

## What only you do

- **Install git and Python 3.10 or newer, if your computer lacks them.** Setup checks for git first, and says how
  to install it. git lfs is optional: without it, projects with large files are copied without those files, and
  setup and `status` say so. On Ubuntu or Debian: `sudo apt install git git-lfs`. On Fedora:
  `sudo dnf install git git-lfs`. On a Mac: `xcode-select --install`, then `brew install git-lfs`.
- **Make one read-only key for each owner whose projects are to be copied**: your own account, and each
  organisation you name. Each one is a fine-grained key with read-only access to Contents and Metadata. An
  organisation may have to approve a key before it works. The agent shows you the page and the settings. It never
  logs in to GitHub for you.
- **Give each key to the skill yourself, in a terminal window of your own**, with `key set`, not through your
  agent: whatever runs inside an agent's session, even a command you start yourself, is shown to the agent. The
  key is then kept in a file only your account can read. An agent that runs as you could read that file; the
  skill tells it never to, and never to put a key in its output.
- **Say yes to the first copy.** You say it after you have heard how many projects there are, how big they are, and
  what is not copied.
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

- **Ubuntu 24.04.5 LTS on x86-64.** Seen on a stock virtual machine with nothing of ours on it, and in accounts
  with no administrator rights:
  - setup;
  - the forge and the sync coming back across restarts;
  - a computer with no git;
  - a full sync from GitHub;
  - a restore onto a second computer.
  Python 3.12, git 2.43, Gitea 28.4.0.
- **The test suite, with the real Gitea 28.4.0**, on GitHub's runners: Ubuntu 26.04 on x86-64 and macOS 26 on Apple
  Silicon, each with Python 3.10 and 3.13. These runs use a stand-in for GitHub. A person's setup has not been
  seen on either.
- **macOS 13.7 on Intel.** Seen: launch agents coming back after a restart, with automatic login on and FileVault
  off.
- **The plugin commands above**, with no GitHub key and no GitHub login: Claude Code 2.1.280 in a Linux account,
  and 2.1.330, installed from its site, not signed in, on a stock Ubuntu 24.04. The plugin is installed and
  listed, with the skill in place. The short form (`TheWindstorm/windstorm-mirror-for-github`) worked the same
  way on 2.1.280. Versions older than 2.1.280: not seen.
- **Not seen yet:**
  - setup from start to end on any Mac;
  - a Mac restart with FileVault on;
  - Apple Silicon outside the test suite;
  - Debian, Fedora and other Ubuntu versions;
  - Windows (not supported in 0.1);

## Taking it off

```bash
python3 -I ~/.local/share/windstorm-mirror/mirror.py uninstall
```

This removes the service, the timer, Gitea, the settings and every key. It switches lingering off only if setup
switched it on. It leaves the folder of copies, which is yours. `uninstall --delete-copies`, run in a terminal window of your own,
deletes that folder too, but only after you type how many copies it holds.

## Family

Part of the Windstorm skills: [Pit Stop](https://github.com/TheWindstorm/windstorm-pit-stop),
[Pit Board](https://github.com/TheWindstorm/windstorm-pit-board),
[Tardigrade](https://github.com/TheWindstorm/windstorm-tardigrade).
