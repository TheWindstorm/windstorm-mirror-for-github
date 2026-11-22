# Platform contracts: what was seen

The plan's "Platform contracts" table lists what GitHub, git, Gitea and each system must do for the code to be
right. Each entry here says what was seen, on what and when. Nothing here is assumed, and an entry not seen yet
says so. Code that leans on a contract is merged only after the contract is seen.

**Where.** The GitHub checks used a private repository of a free GitHub organisation, made for these checks and
holding made-up data. They read GitHub's REST answers through the GitHub CLI, signed in with a classic key that
has the `repo` scope. That login was used only for these checks; the skill itself asks for read-only
fine-grained keys. Key values were never printed, and only headers and the proof repository's own fields were
read. The Gitea checks ran in a Linux test account with no administrator rights and no Docker, using Gitea
28.4.0 from its official binary, checksum checked, listening on 127.0.0.1.
**When.** 2026-11-22, 17:46Z to 18:20Z, and C9 at 19:12Z. C7 and C8 used a second Linux test account, and for the restore a second Linux computer, Ubuntu on x86-64, also with no administrator rights. Their data was made up, and GitHub was not involved.

## C1 Repository listings

- Seen:
  - `GET /orgs/<org>/repos` gave, for the proof repository: `id` (a number), `name`, `full_name`, `private`
    (true), `archived`, `fork`, `size`, `pushed_at`, `default_branch` and `visibility` ("private").
  - With `per_page=1`, the answer carried `Link: <...page=2>; rel="next", <...page=7>; rel="last"` for an
    organisation of 7 repositories. The last page has no `rel="next"`.
- Finding for G3.7: `size` read 0 for the proof repository after several pushes, one of them a 400 KB large
  file. So `size` can lag or read 0, and the first-run estimate must not rest on it alone.
- Not seen yet:
  - The cap of 100 to a page: the organisation had only 7 repositories.
  - `GET /user/repos?affiliation=owner`. It is not called with this login, because it would list repositories
    that are not made for the proof. It will be seen in a clean account that holds only throwaway repositories.

## C2 `pushed_at` moves on every kind of change

Seen. Each line was read about 8 seconds after its push:

| Change | `pushed_at` |
|---|---|
| none (start) | 17:52:21Z |
| a new commit pushed to `main` | 18:15:42Z |
| a force-push of `main` (amended commit) | 18:15:53Z |
| the `side` branch deleted | 18:16:02Z |
| a tag pushed | 18:16:13Z |

Not seen yet: how long `pushed_at` can lag under load. G4.6's daily full pass covers a change the listing does not
show.

## C3 What GitHub's answer shows about a key

- Seen, for a classic key: `X-Oauth-Scopes: gist, read:org, repo, workflow`, and `X-Accepted-Oauth-Scopes: repo`
  for a repository read. There was no `github-authentication-token-expiration` header; this classic key has no
  set expiry, as far as the answer shows.
- From GitHub's docs only (read 2026-11-22): a fine-grained key has one resource owner, a user or one
  organisation, and an organisation can require approval, in which case the key waits as pending.
- Not seen yet, because it needs a key made in a GitHub login, which is the person's step:
  - a fine-grained key's headers;
  - the expiry header on a key that has an expiry;
  - the answer for a key still waiting for approval.

## C4 The allowance

- Seen on a REST answer: `X-Ratelimit-Limit: 5000`, `X-Ratelimit-Remaining`, `X-Ratelimit-Used`,
  `X-Ratelimit-Reset` and `X-Ratelimit-Resource: core`. With no key the limit was 60.
- Seen with no key: 5 `git ls-remote` calls and one `git clone` of a public repository left the REST allowance's
  `used` at 0 of 60. Git over HTTPS did not count against the REST allowance.
- Not seen yet: the same with a key. Git has limits of its own, not measured here.

## C5 The fetch with the key in memory

Seen, in the Linux test account:
- The key was given only in the git process's environment, as `GIT_CONFIG_COUNT` with an `http.<github>/.extraheader`
  scoped to github.com.
- `git clone --bare`, then `git fetch --prune` of `+refs/heads/*` and `+refs/tags/*`, copied every branch and the
  tag of the private proof repository.
- `git lfs fetch --all` with the same environment fetched its one large file.
- The key was in none of the 95 files the run wrote, and in no git config.

## C6 Pushing into stock Gitea

- Seen, in the same account on Gitea 28.4.0:
  - A repository was created through `POST /api/v1/user/repos`, with Actions switched off by `PATCH` (`has_actions: false`).
  - Branches and tags were pushed to it with the forge key in the process's environment.
  - `git lfs push --all` copied the large file, and Gitea served all 400,000 bytes.
  - A pushed `kept/2026-11-22/side` was accepted and listed as a branch.
- Seen as a non-administrator `mirror` account:
  - With a key scoped `write:repository` alone, creating a repository answered 403. With `write:repository` and
    `write:user` it answered 201.
  - Pushing, and switching Actions off for its own repository, both worked.

## C7 Renames in Gitea

Seen:
- `PATCH /api/v1/repos/mirror/alpha` with a new name `beta` kept the repository's id (2).
- `GET` on the old name answered `301` to `/api/v1/repos/mirror/beta`.
- `git ls-remote` of the old URL answered.
- **A `git push` to the old URL followed the redirect and landed on the renamed repository.**
- A new repository created under the old name (id 3) took that name, and the redirect ended.

Consequence for G3.2: the skill reads a copy's current name by Gitea's id (`/api/v1/repositories/<id>`) before
every push, checks the name in the answer, and pushes with git's `http.followRedirects` set to `false`.

## C8 `gitea dump` and a restore on another computer

Seen:
- With the forge stopped, `gitea dump --type tar.gz` wrote 111,203 bytes holding `app.ini`, `custom`, `data` (with
  the SQLite database file itself), `gitea-db.sql`, `log` and `repos`.
- `app.ini` holds the forge's own secrets, so the backup file is as secret as the forge. G7.4 keeps it readable
  by the person only, and says so.
- On another computer, with the same binary (checksum checked):
  - the dump was unpacked, `data/` copied as it was, `repos/` copied into `data/gitea-repositories/`, and the
    paths and port in `app.ini` rewritten;
  - Gitea started with every user and every repository, each under its old id;
  - a clone of a repository came back with both its commits, and `git fsck` found 0 errors.
- Replaying `gitea-db.sql` on top of the copied database fails ("index ... already exists"). With SQLite, the
  database file in `data/` is the restore and the SQL is not replayed.
- Seen on 2026-11-22, in a Linux test account on Gitea 28.4.0: a dump holds the large files (under `data/lfs`).
  Restored into a second home with other paths and another port, the copies came back with their large
  files, and `git lfs fsck` passed. Gitea's hooks name the old paths, so the restore runs
  `gitea admin regenerate hooks`.
- Not seen yet: a restore into a forge whose binary is newer.

## C9 The boot service

- Seen in part, on 2026-11-22, in a Linux test account with no administrator rights and lingering already on:
  `setup` wrote the systemd user unit, and `systemctl --user enable --now` started it. The forge answered on
  127.0.0.1, and the sync timer was enabled beside it.
- Not seen yet: either one across a restart; lingering switched on by a person who is not an administrator;
  launchd on macOS, on Intel and Apple Silicon, with FileVault on and off.

## C10 Gitea's settings take effect

- Seen in part: Gitea listened on 127.0.0.1 with registration disabled and sign-in required to view.
- Not seen yet: a probe from another computer; Actions off for the whole forge.

## C11 The skill's latest release, read with no key

Seen. `GET /repos/<owner>/<repo>/releases/latest` with no key answered 200 for a sister skill's repository, with
`tag_name`, `published_at` and `body`. The limit with no key was 60 calls an hour, shared by everything on that
computer's address. One call a day (G6.6) is within it.
