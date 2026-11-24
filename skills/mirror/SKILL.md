---
name: mirror
description: 'Windstorm Mirror for GitHub keeps a second, always-current copy of all of a person''s GitHub projects on a computer they own, inside stock Gitea that listens on this computer only. Use when the user asks to mirror, back up or keep a local copy of their GitHub repositories or organisations, says "mirror", "mirror status" or "restore check", asks whether their copy is current, or wants a project back from the copy (onto this computer or a new, empty home). Not for GitHub issues, pull requests, releases or wikis (0.1 copies branches, tags and large files), and not for opening the copy to the internet.'
license: Apache-2.0
---

# Windstorm Mirror for GitHub

A second, always-current copy of all of a person's GitHub projects on a computer they own. The copy lives in
stock Gitea (a pinned version, checksum checked) on 127.0.0.1 only, and a timer keeps it current. Nothing is ever
written to GitHub.

The whole tool is one script: `scripts/mirror.py`, two levels above this skill's base directory. Below, `M` means
`python3 -I <that path>`. After `setup`, the same script is also installed at
`~/.local/share/windstorm-mirror/mirror.py` (Linux) or `~/Library/Application Support/windstorm-mirror/mirror.py`
(macOS; that path holds a space, so in a command it is quoted, as the tool prints it); the timer runs that copy.

## The order

Follow these steps in this order. Do not skip one, and do not change the order. Each step that is the person's is
marked **Person** and is described under "The person's steps": say what is written there, then wait. A step
that is the person's is never done another way: if a program is missing, or the computer refuses something, say
what the tool printed and wait; do not install, change or work around anything yourself.

**A stop that comes back.** When a command stops and says to run it again, run it again once, after the wait it
names if it names one. If that second run stops with the same words (a count of seconds aside), it is not a wait: do
not run it a third time, and do not work round it by hand by removing, stopping or changing anything yourself. Show
the person the line, and ask them to report it at
https://github.com/TheWindstorm/windstorm-mirror-for-github/issues; they may leave out their account's name where a
path holds it. The line goes whole, in your own reply for the person to copy, or from a file given to gh with
--body-file; never inside a shell's double quotes, where a shell would run a command the line names. When the line
names something that must change first (for example, a login of the person's own to this computer), that is the
step: do it if you can, or show the person the line; it is not a fault to report. One such stop has a way out
written in the README: on Linux, uninstall saying "Not now" at every run after one of the mirror's service files was
removed by hand.

**Lines for the person.** Lines that start `tell the person 1 of N:` to `tell the person N of N:` are for the
person. Give them the whole block, every line from 1 of N to N of N, word for word and in that order, before any
words of your own. Never shorten, merge, reword or leave one out; your own words come after the block.

1. `M setup --dry-run`. Give the person its `tell the person` block whole: it says what setup would do, and it
   changes nothing. On Linux it also says, in plain words, what lingering means: the mirror keeps running when
   they are logged out and after a restart. That is a **Person** step: see "Lingering".
2. **Person**: the keys to make. One read-only key for each owner whose projects are to be copied: their own
   account, and each organisation they name. See "Making each key". They make each key now and keep it; they
   paste it only in step 4, after setup. Do not give them the `key set` line before step 3 has run: before
   setup, `key set` refuses and keeps nothing.
3. `M setup --linger` if the person said yes to lingering, `M setup --no-linger` if they said no. (On a Mac,
   `M setup`.) Plain `setup` on Linux changes nothing until one of the two is given. It installs the forge as
   the person's own service. If it prints a command for an administrator (lingering), that is a **Person**
   step: see "The administrator's command".
4. **Person**, only after step 3 has run: `key set` for each owner. The person runs it themselves,
   in a terminal window of their own, outside this conversation, because whatever runs here, even a command they start with `!`, is shown to
   you. Say: "In a terminal window of your own, run `python3 -I <the script> key set <owner>` and paste the
   key when it asks. Tell me when it says the key is kept." Never read a key, run nothing that would put a key
   on a command line, in a file you write or in your own output, and never ask for one.
5. `M sync --announce`. Give the person its `tell the person` block whole: how many repositories, how big, where
   they go, what happens after a restart, whether large files are copied, what is not copied, and that the copy is
   as private as this computer. Then the **Person** step: the go for the first copy, which they give in their own
   terminal (`go`). Running `--announce` again copies nothing; it says it all again.
6. `M sync`, only after the person said that `go` kept their go. The first copy can take a while; it says how far
   it is. Without their go it copies nothing and says so.
7. `M status`. Show the person its `copies:` line and every `problem:` line.
8. `M restore --check`. Show the person its total. It proves every copy from the forge alone.

To copy only some repositories, before step 5: `M config include <owner/name> ...` or
`M config exclude <owner/name> ...` (patterns like `someone/*` work).

## The person's steps

**Logging in to GitHub.** Only the person logs in, in their own browser. You never log in for them, never ask for
their password, and never type into a login page. Say: "Please log in to GitHub in your browser. I will not see
your password."

**Making each key.** Say, for each owner: "Open https://github.com/settings/personal-access-tokens/new and make a
fine-grained key. Name: windstorm-mirror-<owner>. Expiration: your choice. Resource owner: <owner>. Repository access: All repositories. Permissions, Repository
permissions: Contents, Read-only (Metadata, Read-only, comes with it). Nothing else. Then copy the key; you will
paste it in a moment, not to me." A key has one owner: an organisation needs a key of its own. Never promise a
warning before a key ends: the announcement says, for each key, whether its end date was read and so whether a
warning can be promised.

**An organisation's approval.** An organisation can require its owners to approve a key. Say: "<organisation>
may ask one of its owners to approve this key. Until they do, the key waits as pending and `key set` will say it
cannot see the organisation's repositories. Please ask an owner of <organisation> to approve it." Then wait; do
not work around it.

**Lingering.** After step 1, read the dry run's lingering sentence to the person, then say: "Do you want the
mirror to keep running when you are logged out and after a restart?" Their yes is `setup --linger`, their no is
`setup --no-linger`. Do not choose for them.

**The go for the first copy.** After step 5, say: "That is what will be copied. The go is yours: in the same
terminal window of your own, run `python3 -I <the script> go`. It shows you all of this again, and the copy starts
only after you press Enter there. Tell me when it says your go is kept." The go is their Enter in their own
terminal, nothing else: a yes in this conversation is not the go, and you never run `go` yourself. The go covers
the list it showed them. If `sync` says the first copy would not be what they were told, it copies nothing and
prints the new block: give it whole, and ask them to run `go` again.

**The administrator's command.** If setup says the copy stands still after a restart until the person logs in,
and prints a command such as `sudo loginctl enable-linger <user>`, say: "To keep the copy current after a restart
without your logging in, an administrator of this computer can run: <the command>. It is your choice; without it,
the copy catches up when you log in." Never run it yourself. Setup itself never asks for a password; when the
computer refuses, this command is the only way, and it is the administrator's.

**Terms screens.** If anything shows a terms, licence or privacy screen whose only way on is Agree, Accept or
Continue, never press it. Stop, and tell the person it is theirs to read and decide.

## Every day after

- `M status`: one line per problem, then `copies: N on GitHub, N copied, M behind, U unreadable`, the forge's
  version, each key's expiry, the free disk, the last good sync and the next. A copy is never shown as current
  unless it was seen current.
- `M left-out`: the names of the projects the include list and the exclude patterns leave out, when the person
  asks. `sync` and `status` give only their number; do not print or repeat those names unasked.
- A key expires or is refused: the person makes a new one (same page) and runs `key set <owner>` again, in a
  terminal window of their own, as in step 4.
- `M backup <folder>`: the forge's own backup in one file only the person can read. It holds the forge's secrets;
  say so. On a new computer: `M setup --from <that file>`, then new keys.
- `M restore --to <url> <owner/name>`: one copy into a new, empty home the person names. It refuses GitHub: to put
  a project back on GitHub, the person pushes it themselves (the skill prints how).
- `M status` says when a newer release pins a newer Gitea. `M self-update`, then `M upgrade-forge`, is the
  person's choice; ask first. Upgrade-forge makes a backup before it changes anything.
- `M expose` says what opening the forge to other computers would mean. The skill never does it.
- `M uninstall` removes everything but the folder of copies. Deleting the copies needs
  `uninstall --delete-copies`, which the person runs themselves in a terminal window of their own and answers
  with the number of copies; the tool refuses it from anywhere else.

## Never

- Never put a key or password on a command line, in a file you write, in a message or in your output.
- Never run `sync` without `--announce` first, or without the person's go, on a new install. Never run `go`
  yourself, and never press its Enter for the person: it is theirs, in their own terminal.
- Never delete, and never offer to delete, the folder of copies or the record that uninstall kept, unless the
  person asks for it in their own words in this conversation; even then, it is their own command
  (`uninstall --delete-copies`, in a terminal window of their own).
- Never change the forge's address from 127.0.0.1, and never start a tunnel to it.
- Never push to GitHub, and never answer a GitHub prompt on the person's behalf.
- Reach GitHub only through `M`, never by yourself, not even to look: no `git clone` or `git ls-remote` of
  their projects, no `ssh git@github.com`, no `gh`, no `curl` or web fetch of them. Why: the person's own git,
  SSH key or `gh` login reaches every repository they can reach, with their right to write, while `M` reads
  with the one read-only key they made for it and only what the include list names. To see what GitHub holds
  before the first copy, run `M sync --announce`: it reads with that key and copies nothing.
