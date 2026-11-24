# Windstorm Mirror for GitHub

**A second, always-current copy of all your GitHub projects on a computer you own.**

GitHub holds the online copy of your work. This skill will keep a complete second copy on a computer of yours,
inside a free, open-source program that works much like GitHub, and keep that copy current. If GitHub is out of
reach for a day, your work is not.

## Status: not released

Nothing here can be installed yet. This skill is built in the open, in this order:

1. The plan, `DNA-STRAND-MASTER-PLAN.md`: every claim the skill makes, each with the test that proves it.
2. The code.
3. Tests on real machines that have none of our set-up, before any release. The first proof is a restore drill:
   every project recovered from the mirror alone.

## Two rules already

The mirror is for you. It listens on this computer only, and the skill never opens it to the network or runs a
tunnel. Making it reachable from other computers is your own choice, and `mirror.py expose` says what that
would mean without changing anything.

The copy is as private as this computer. Anyone who can read this account's files can read every project in it. Disk encryption, and who uses this computer, are yours.

## Settings and rights

The skill needs no administrator rights: none of its commands runs with sudo or as another account. It reads three
settings of yours and no other: `WSM_HOME` moves every folder of the skill's under it, and on Linux
`XDG_DATA_HOME` and `XDG_CONFIG_HOME` move its data and settings folders as they do for other programs. A command
whose folders one of them moved says so first.

Your git hooks do not run in the mirror's own copies, and your proxy is never asked to reach the mirror's own
forge on this computer; it still carries the mirror's calls to GitHub.

## Family

Part of the Windstorm skills: [Pit Stop](https://github.com/TheWindstorm/windstorm-pit-stop),
[Pit Board](https://github.com/TheWindstorm/windstorm-pit-board),
[Tardigrade](https://github.com/TheWindstorm/windstorm-tardigrade).

This project is not affiliated with, endorsed by or sponsored by GitHub. GitHub is a trademark of GitHub, Inc.
