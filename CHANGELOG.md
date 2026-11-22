# Changelog

## Unreleased

- G3, find and copy: `sync` lists every repository of each owner page by page to the end, with that owner's key, and copies branches, tags and large files into a private copy each, known by GitHub's id: a rename follows, a reused name moves the old copy aside, a repository gone from GitHub keeps its copy. The key is only ever in the fetching process's environment. `config include|exclude` chooses; an include that matches nothing stops. `sync --announce` comes before the first copy. A copy that comes back not private is deleted at once and the sync stops. G2.5: a new key is one step, and the next sync proves it.

- G2.2, the keys: `key set <owner>` reads one owner's GitHub key from standard input (never a command line), checks that it reaches that owner's repositories, and keeps it in a file of its own that only the person can read. A refused key, or one that cannot see the owner, is not kept.

- G1, the forge: `setup` installs stock Gitea 28.4.0 (pinned, checksum checked) as the person's own service, on 127.0.0.1 only, with sign-up closed, Actions and Gitea's own mirrors off, and two accounts (the person's, and `mirror` that owns the copies). `setup --dry-run` changes nothing; a second `setup` changes nothing. Setup says when the copy will not come back by itself after a restart.

- Plan 1.1: ten genes (the last is the skill an agent follows), seven invariants, milestones, eleven platform contracts, and the first fifteen promises, from a long-running mirror and from our own lab on stock Gitea 28.4.0.
- The gates come first: `PROMISES.json` and the tests that refuse a version while a promise due in it is not built,
  or the version after it until the promise was seen to work on a real machine. CI on GitHub's standard runners.
