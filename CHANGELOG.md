# Changelog

## Unreleased

- G1, the forge: `setup` installs stock Gitea 28.4.0 (pinned, checksum checked) as the person's own service, on 127.0.0.1 only, with sign-up closed, Actions and Gitea's own mirrors off, and two accounts (the person's, and `mirror` that owns the copies). `setup --dry-run` changes nothing; a second `setup` changes nothing. Setup says when the copy will not come back by itself after a restart.

- Plan 1.1: ten genes (the last is the skill an agent follows), seven invariants, milestones, eleven platform contracts, and the first fifteen promises, from a long-running mirror and from our own lab on stock Gitea 28.4.0.
- The gates come first: `PROMISES.json` and the tests that refuse a version while a promise due in it is not built,
  or the version after it until the promise was seen to work on a real machine. CI on GitHub's standard runners.
