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
(macOS); the timer runs that copy.

## The order

Follow these steps in this order. Do not skip one, and do not change the order. Each step that is the person's is
marked **Person** and is described under "The person's steps": say what is written there, then wait.

1. `M setup --dry-run`. Show the person its whole output: it says what setup would do and changes nothing.
2. **Person**: the keys to make. One read-only key for each owner whose projects are to be copied: their own
   account, and each organisation they name. See "Making each key".
3. `M setup`. It installs the forge as the person's own service. If it prints a command for an administrator
   (lingering), that is a **Person** step: see "The administrator's command".
4. **Person**: `key set` for each owner. The person runs it themselves, so the key never passes through you:
   ask them to type `! python3 -I <the script> key set <owner>` and paste the key when it asks. Run nothing that
   would put a key on a command line, in a file you write, or in your own output.
5. `M sync --announce`. Read its lines to the person: how many repositories, how big, what is not copied, and
   that the copy is as private as this computer. Then the **Person** step: the go for the first copy.
6. `M sync`, only after the person said go. The first copy can take a while; it says how far it is.
7. `M status`. Show the person its `copies:` line and every `problem:` line.
8. `M restore --check`. Show the person its total. It proves every copy from the forge alone.

To copy only some repositories, before step 5: `M config include <owner/name> ...` or
`M config exclude <owner/name> ...` (patterns like `someone/*` work).

## The person's steps

**Logging in to GitHub.** Only the person logs in, in their own browser. You never log in for them, never ask for
their password, and never type into a login page. Say: "Please log in to GitHub in your browser. I will not see
your password."

**Making each key.** Say, for each owner: "Open https://github.com/settings/personal-access-tokens/new and make a
fine-grained key. Name: windstorm-mirror-<owner>. Expiration: your choice (I will warn you
two weeks before it ends). Resource owner: <owner>. Repository access: All repositories. Permissions, Repository
permissions: Contents, Read-only (Metadata, Read-only, comes with it). Nothing else. Then copy the key; you will
paste it in a moment, not to me." A key has one owner: an organisation needs a key of its own.

**An organisation's approval.** An organisation can require its owners to approve a key. Say: "<organisation>
may ask one of its owners to approve this key. Until they do, the key waits as pending and `key set` will say it
cannot see the organisation's repositories. Please ask an owner of <organisation> to approve it." Then wait; do
not work around it.

**The go for the first copy.** After step 5, say: "That is what will be copied. Shall I start the first copy?"
Only a clear yes from the person is the go. Do not take silence, or an earlier yes to something else, as the go.

**The administrator's command.** If setup says the copy stands still after a restart until the person logs in,
and prints a command such as `sudo loginctl enable-linger <user>`, say: "To keep the copy current after a restart
without your logging in, an administrator of this computer can run: <the command>. It is your choice; without it,
the copy catches up when you log in." Never run it yourself.

**Terms screens.** If anything shows a terms, licence or privacy screen whose only way on is Agree, Accept or
Continue, never press it. Stop, and tell the person it is theirs to read and decide.

## Every day after

- `M status`: one line per problem, then `copies: N on GitHub, N copied, M behind, U unreadable`, the forge's
  version, each key's expiry, the free disk, the last good sync and the next. A copy is never shown as current
  unless it was seen current.
- A key expires or is refused: the person makes a new one (same page) and runs `key set <owner>` again.
- `M backup <folder>`: the forge's own backup in one file only the person can read. It holds the forge's secrets;
  say so. On a new computer: `M setup --from <that file>`, then new keys.
- `M restore --to <url> <owner/name>`: one copy into a new, empty home the person names. It refuses GitHub: to put
  a project back on GitHub, the person pushes it themselves (the skill prints how).
- `M status` says when a newer release pins a newer Gitea. `M self-update`, then `M upgrade-forge`, is the
  person's choice; ask first. Upgrade-forge makes a backup before it changes anything.
- `M expose` says what opening the forge to other computers would mean. The skill never does it.
- `M uninstall` removes everything but the folder of copies. Deleting the copies needs
  `M uninstall --delete-copies` and the person typing the number of copies; never type it for them.

## Never

- Never put a key or password on a command line, in a file you write, in a message or in your output.
- Never run `sync` without `--announce` first, or without the person's go, on a new install.
- Never change the forge's address from 127.0.0.1, and never start a tunnel to it.
- Never push to GitHub, and never answer a GitHub prompt on the person's behalf.
