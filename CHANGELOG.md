# Changelog

## Unreleased

- G5, never lose a commit: before a branch or tag of a copy is rewritten, moved back or deleted to follow GitHub, its old commit is kept in the same copy as `kept/<date>/<name>` (a tag as `kept/<date>/tags/<name>`), and the sync says so. A kept ref is dropped after 90 days by default (a setting); with the default, one that is the only way to its commit stays. Every push into the copy is one atomic push with explicit deletes, and the copy's own refs are read back before each push, so a lost local cache is rebuilt with its kept refs. A push to GitHub cannot start: GitHub's push address in every cache is a dead one, and the only call to GitHub's API is a read.

- G4, stay current: `setup` also switches on a timer that runs `sync` every 15 minutes (a setting). It copies nothing before the person's go, and `status` reads the timer's next run, so a timer that is on with no next run is a problem. Every fetch and push has a time limit and one retry, and a slow repository is named while the others carry on. Only one sync runs at a time, and a lock whose sync is gone is taken over. Listing stops below a floor of GitHub's remaining allowance. A sync does not start below a floor of free disk. Large files go in first and then the branches and tags in one atomic push, and a copy in progress is marked until it is done. A sync fetches only the repositories whose `pushed_at` moved, and once a day it does a full pass.

- G3, find and copy: `sync` lists every repository of each owner page by page to the end, with that owner's key, and copies branches, tags and large files into a private copy each, known by GitHub's id: a rename follows, a reused name moves the old copy aside, a repository gone from GitHub keeps its copy. The key is only ever in the fetching process's environment. `config include|exclude` chooses; an include that matches nothing stops. `sync --announce` comes before the first copy. A copy that comes back not private is deleted at once and the sync stops. G2.5: a new key is one step, and the next sync proves it.

- G2.2, the keys: `key set <owner>` reads one owner's GitHub key from standard input (never a command line), checks that it reaches that owner's repositories, and keeps it in a file of its own that only the person can read. A refused key, or one that cannot see the owner, is not kept.

- G1, the forge: `setup` installs stock Gitea 28.4.0 (pinned, checksum checked) as the person's own service, on 127.0.0.1 only, with sign-up closed, Actions and Gitea's own mirrors off, and two accounts (the person's, and `mirror` that owns the copies). `setup --dry-run` changes nothing; a second `setup` changes nothing. Setup says when the copy will not come back by itself after a restart.

- Plan 1.1: ten genes (the last is the skill an agent follows), seven invariants, milestones, eleven platform contracts, and the first fifteen promises, from a long-running mirror and from our own lab on stock Gitea 28.4.0.
- The gates come first: `PROMISES.json` and the tests that refuse a version while a promise due in it is not built,
  or the version after it until the promise was seen to work on a real machine. CI on GitHub's standard runners.
