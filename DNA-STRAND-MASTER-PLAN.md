# DNA Strand Master Plan: Windstorm Mirror for GitHub

Plan version 1.0 (2026-11-22), for review. Builds nothing yet: the plan is reviewed before any code. The first
release will be 0.1.0. What is promised for it is in the genes below and in `PROMISES.json`.

This is the plan the skill is built from. Each gene is one thing the skill does. Each codon is one claim small
enough to test, with its proof next to it. A proof is a test in `tests/test_mirror.py`, named after its codon.
A codon whose test does not exist yet is marked *(to build)*. The plan-to-test link is itself tested, so a codon
moves out of *(to build)* only when its test is real.

## The want

Restated from what the person who asked for it said:

1. A second copy of every GitHub project a person has, on a computer they own, that stays current by itself.
2. The copy lives inside a free program that works like GitHub (stock Gitea), so the person can browse it, clone
   from it and, if GitHub is ever out of reach, carry on from it.
3. The person tells their agent "set up Windstorm Mirror for GitHub", and the agent does everything except the
   steps that are theirs by nature: logging in to GitHub and making a key there.

The test of success: with GitHub out of reach, every project is recovered from this copy alone.

## The name

"Windstorm Mirror for GitHub": our own name first, then what it works with, as a plain description. It is not a
GitHub product, it is not endorsed by GitHub, and it never uses a GitHub logo or the Octocat. The README says
"not affiliated with GitHub" where a person first reads about it.

Where "mirror" stops being the right word: a mirror shows only what is in front of it now. This copy keeps what
GitHub loses. A branch deleted or rewritten on GitHub is kept here (G5).

## Field notes (2026-11-22)

Written before the build. Each line says where it comes from.

**What exists already** (web search and the projects' own pages, 2026-11-22; not tested by us unless said):
- Gitea and Forgejo both have pull mirrors: one repository at a time, set when it is created.
- A web app that mirrors whole GitHub accounts into Gitea exists, under the AGPL. We learn from it and do not
  embed it.
- Two smaller tools exist: one under Apache-2.0 (a config file, many hosts) and one under MIT (in maintenance
  mode).
- We did not find a skill an agent can follow, end to end, for a newcomer that does all of these: finds every
  repository by itself, never puts the GitHub key on disk inside the copy, keeps what GitHub loses, says when the
  copy is behind, and proves a restore with GitHub out of reach.

**Why Gitea, and why stock:**
- Gitea is MIT. Forgejo is GPL v3 or later since v9. Both would be lawful here, because the skill installs the
  unmodified program as a separate service and ships none of it. The permissive licence needs no argument.
- Gitea 28.4.0 (released 2026-11-15) has official binaries with checksums for Linux, macOS and Windows, on Intel
  and ARM. No Docker and no package manager are needed.
- Forgejo publishes long-term releases with end dates. We found no support period for Gitea, so the skill pins
  one version and makes an upgrade a step it does itself (G9).
- **How the pin moves.** A new Gitea version is pinned only in a new release of this skill. That release names
  the version and its checksums, and has been through the same real-machine tests. A Gitea security release is a
  reason for a release of the skill. On the person's computer the move is `upgrade-forge`: a backup first, then
  the swap, then `status`. The old binary and the backup are kept as the rollback point until `status` passes.
- Stock means no patch, no fork and no custom templates. Everything the skill adds sits beside Gitea and talks to
  it through its API.

**What a long-running mirror taught** (a Git mirror of about 140 repositories that an AI agent has run every day
since August 2026 on a shared Linux workstation, on stock Gitea beside GitHub):
- A Gitea pull mirror given a key writes that key into the repository's git config. Seen in about 125
  repositories at once, then twice more after a clean-up; one exposure lasted about a day and led to the key
  being changed.
- Removing those keys froze more than a hundred private mirrors, and nothing said so. A mirror that stops
  refreshing raises no alarm in Gitea. An hourly lag check had to be added, then a 24-hour alarm, because mirrors
  marked "known stale" sat frozen for weeks.
- Mirrors pointed the other way, from the copy to GitHub, would have overwritten live work with a stale copy on
  their timer. They were removed before they fired.
- A hand-kept list of repositories missed 130 active ones, and a listing capped at 300 would have cut a larger
  account short.
- One hung fetch held every repository behind it for 5 to 10 minutes.
- A GitHub rename, and a reused name, nearly sent one repository's history onto another.
- A check reported "0 found" because it had searched nothing.
- A config file printed in full put a secret into a log.
- A timer showed "active" after a reboot with no next run for 3 hours.
- A script was replaced while it was running.
- The key used could write to GitHub, and one key was shared by everything on the account. Its hourly API
  allowance ran out.
- No one ever recovered a project from that mirror alone. Its drill restored the forge's own database from a
  backup.

**What stock Gitea does today** (seen by us, 2026-11-22: Gitea 28.4.0 from the official Linux binary, checksum
checked, in a test account with no administrator rights and no Docker, bound to 127.0.0.1):
- A pull mirror made with a key writes the key into the repository's git config remote URL, in a file every
  account on the machine can read (mode 664).
- The default mirror interval is 8 hours.
- A force-push or branch deletion at the source is erased from the copy at the next sync: no branch or tag
  reaches the old commit any more.
- Asked for everything, a mirror of a public GitHub repository copied: the wiki (as its own mirror), every branch
  and tag, and GitHub's pull-request refs. Releases and issues were not copied (0 of 30 releases; tags only).
- Large files (LFS): not settled for Gitea's own mirror. Our test used a single Gitea instance.

**The skill's own road, on stock Gitea** (seen by us, 2026-11-22, same machine, Gitea and conditions, with a
private GitHub repository made for the proof: made-up data, two branches, a tag, one 400 KB large file):
- A bare clone and fetch of branches and tags with the key held only in the git process's environment copied
  every branch and tag.
- `git lfs fetch --all` with the same key, then `git lfs push --all` into Gitea, copied the large file. Gitea
  serves all 400,000 bytes.
- Gitea accepts a pushed branch named `kept/2026-11-22/side` and lists it as a branch.
- The key was found in none of the 95 files the run wrote, and in no git config.

So the skill does not use Gitea's own pull mirror with a key. It fetches from GitHub itself, with the key held
only in memory, into a local cache, then pushes into stock Gitea with a narrow Gitea key. The long-running
mirror used this same road for its most important repositories for months. It is also what lets the copy keep
what GitHub loses.

## Genes and codons

The whole tool is one script, `scripts/mirror.py`: Python standard library, plus `git` and the Gitea binary.
Commands: `setup`, `key`, `sync`, `status`, `restore`, `backup`, `upgrade-forge`, `self-update`, `uninstall`.

### G1 Install the forge: stock Gitea, at home, for the person only

| Codon | Claim | Proof |
|---|---|---|
| G1.1 | `setup` downloads the pinned Gitea version for this system and architecture from Gitea's own download site, and checks it against the SHA-256 written in the script before it is ever run. A mismatch stops setup, and the file is deleted. | `test_G1_1_the_forge_is_the_pinned_version_and_its_checksum_is_checked_first` *(to build)* |
| G1.2 | The forge runs as the person's own user, with no administrator rights: a systemd user unit on Linux, a launchd agent on macOS. It starts at login and after a reboot. | `test_G1_2_the_forge_runs_as_the_person_and_comes_back_after_a_reboot` *(to build)* |
| G1.3 | The forge listens on 127.0.0.1 only, on a free port picked at setup. Every start checks the port is still free and says so if not. SSH is off, sign-up is closed, nothing is shown to anyone not logged in, and Actions are off. | `test_G1_3_the_forge_listens_on_this_computer_only_with_signup_closed` *(to build)* |
| G1.4 | Setup makes two Gitea accounts: the person's (an administrator), and a `mirror` account that owns the copies. It writes the person's first password to a file only they can read, and says where. It never prints the password. | `test_G1_4_setup_makes_the_two_accounts_and_never_prints_a_password` *(to build)* |
| G1.5 | `setup --dry-run` says everything setup would do and changes nothing. Running `setup` twice does not make a second forge, account or service. | `test_G1_5_setup_dry_run_changes_nothing_and_setup_twice_is_one_setup` *(to build)* |

### G2 The GitHub key: read-only, in one place, watched

| Codon | Claim | Proof |
|---|---|---|
| G2.1 | The skill asks the person to make a fine-grained GitHub key with read-only access to Contents and Metadata, on all their repositories. It gives them the exact page and settings. Making it is the person's step: the agent never logs in to GitHub for them. | `test_G2_1_the_person_is_told_exactly_which_read_only_key_to_make` *(to build)* |
| G2.2 | `key set` reads the key from standard input (never from a command line), checks it against GitHub, and keeps it in one file only the person can read. It is never written anywhere else: not into Gitea, not into a git config, not into a log. | `test_G2_2_the_key_is_kept_in_one_file_and_nowhere_else` *(to build)* |
| G2.3 | A key that can write is named for what it is. When GitHub's answer shows a classic key with write scopes, `key set` and `status` say "this key can write to GitHub; the skill never uses that, make a read-only one". | `test_G2_3_a_key_that_can_write_is_named_for_what_it_is` *(to build)* |
| G2.4 | The key's expiry, from GitHub's answer, is shown by `status`, and a warning starts 14 days before it. A key GitHub refuses turns every copy to "cannot refresh", never "current". | `test_G2_4_the_keys_expiry_is_watched_and_a_refused_key_is_never_current` *(to build)* |
| G2.5 | Changing the key is one step, `key set` again. The next sync proves every repository refreshed with the new key, or names those that did not. | `test_G2_5_a_new_key_is_one_step_and_the_next_sync_proves_it` *(to build)* |

### G3 Find every project, and copy it

| Codon | Claim | Proof |
|---|---|---|
| G3.1 | Each sync asks GitHub for every repository the person owns, plus those of the organisations named in the settings, page by page to the last page. A page that fails stops the listing: the skill never acts on half a list as if it were whole. | `test_G3_1_every_repository_is_found_page_by_page_to_the_end` *(to build)* |
| G3.2 | A repository is known by GitHub's numeric id, not by its name. When it is renamed on GitHub, the copy is renamed too. When a new repository takes an old copy's name, the old copy is moved aside under a new name, never overwritten. | `test_G3_2_a_rename_or_a_reused_name_never_writes_one_project_over_another` *(to build)* |
| G3.3 | A copy is made as a private repository of the `mirror` account, with Actions off. Branches and tags are copied. GitHub's pull-request refs, releases, issues and wikis are not copied in 0.1, and `status` says so. | `test_G3_3_branches_and_tags_are_copied_and_what_is_not_is_said` *(to build)* |
| G3.4 | The fetch from GitHub carries the key as a header set in the environment of that one git process. The key is never in a URL, a git config, a command line or the process list. | `test_G3_4_the_key_travels_only_in_the_fetchs_own_environment` *(to build)* |
| G3.5 | Settings can leave repositories out by name or pattern, or name the only ones to copy (`include`). The default is all of them, and `status` lists what was left out and why. Archived, empty, forked and private repositories are copied unless left out. An `include` that matches nothing stops the sync: it never widens to everything. | `test_G3_5_what_is_left_out_is_chosen_and_listed` *(to build)* |
| G3.6 | A repository deleted on GitHub, or no longer visible to the key, keeps its copy. `status` shows it as "gone from GitHub since <date>". The skill never deletes a copy. | `test_G3_6_a_repository_gone_from_github_keeps_its_copy` *(to build)* |
| G3.7 | Before the first copy, the skill says what it is about to do: how many repositories, their total size as GitHub reports it, and a rough time measured from the first one. It waits for the person's go before starting the rest. The first copy runs in the background; `status` shows its progress and what is left. | `test_G3_7_the_first_copy_is_announced_with_its_size_and_time_before_it_starts` *(to build)* |
| G3.8 | Large files (Git LFS) are copied with the repository when `git lfs` is installed, using the same in-memory key. A repository that uses large files on a computer without `git lfs` is copied without them, and `status` names it. | `test_G3_8_large_files_are_copied_or_named_as_missing` *(to build)* |

### G4 Stay current

| Codon | Claim | Proof |
|---|---|---|
| G4.1 | A timer runs `sync` every 15 minutes by default (a setting). After a reboot, `status` reads the timer's next run, not only whether it is switched on. | `test_G4_1_a_timer_runs_sync_and_status_reads_its_next_run` *(to build)* |
| G4.2 | Every fetch and push has a time limit and one retry. One slow or broken repository never holds up the rest: it is named, and the others carry on. | `test_G4_2_one_slow_repository_never_holds_up_the_rest` *(to build)* |
| G4.3 | Only one sync runs at a time. A second one started meanwhile says so and exits. A lock whose owner is gone is taken over and noted. | `test_G4_3_one_sync_at_a_time` *(to build)* |
| G4.4 | A sync counts its GitHub API calls. It stops listing, and says so, when GitHub's remaining allowance falls below a floor (a setting). | `test_G4_4_a_sync_spends_few_api_calls_and_stops_above_a_floor` *(to build)* |
| G4.5 | Before each sync the free disk space is checked against a floor (a setting). Below it, the sync does not start, and `status` says why. A sync stopped part-way leaves every copy as it was, or as it fully became, and never marks a half-done copy current. | `test_G4_5_a_full_disk_stops_the_sync_cleanly` *(to build)* |

### G5 Keep what GitHub loses

| Codon | Claim | Proof |
|---|---|---|
| G5.1 | Before a branch or tag is moved backwards, rewritten or deleted in the copy, its old commit is kept under `kept/<date>/<name>` in the same copy. | `test_G5_1_a_rewritten_or_deleted_branch_is_kept_before_the_copy_follows` *(to build)* |
| G5.2 | Kept refs are dropped after 90 days by default (a setting). Nothing is dropped while it is the only ref that reaches a commit, unless the person has set that period. | `test_G5_2_kept_refs_live_for_the_set_period` *(to build)* |
| G5.3 | The skill never writes to GitHub. It has no push to GitHub, no push mirror and no API call that changes anything there. | `test_G5_3_nothing_is_ever_written_to_github` *(to build)* |

### G6 Say what is true

| Codon | Claim | Proof |
|---|---|---|
| G6.1 | `status` prints one line per problem, then `copies: N on GitHub, N copied, M behind, U unreadable`. A copy is behind when its last good refresh is older than twice the sync interval, or when its refs differ from GitHub's after a grace period. | `test_G6_1_status_says_how_many_are_current_and_names_the_rest` *(to build)* |
| G6.2 | A copy that cannot refresh is never shown as current, for any reason. There is no "known stale" pardon. | `test_G6_2_a_copy_that_cannot_refresh_is_never_shown_as_current` *(to build)* |
| G6.3 | `status` scans every repository config in the forge, and the skill's own files, for anything shaped like a key. A scan that read no files is a failure, not a pass. | `test_G6_3_the_key_scan_counts_what_it_read_and_zero_is_a_failure` *(to build)* |
| G6.4 | The skill never prints or logs a key, a password or a config file whole. It reads settings by name. | `test_G6_4_no_secret_reaches_the_output_or_the_log` *(to build)* |
| G6.5 | `status` shows the forge's version, the key's expiry, free disk space, the last good sync, and the next. | `test_G6_5_status_shows_version_expiry_disk_and_the_last_and_next_sync` *(to build)* |

### G7 Restore

| Codon | Claim | Proof |
|---|---|---|
| G7.1 | Each sync writes down the refs it last saw on GitHub for every repository. | `test_G7_1_each_sync_writes_down_the_refs_github_last_showed` *(to build)* |
| G7.2 | `restore --check` needs no network beyond this computer. It clones every copy from the forge into a scratch folder, checks every branch and tag against the last refs seen on GitHub, runs `git fsck`, prints one line per difference, then a total, and removes the scratch folder. | `test_G7_2_restore_check_proves_every_copy_from_the_forge_alone` *(to build)* |
| G7.3 | `restore --to <url> <repository>` pushes one copy, every branch and tag, to a new, empty home the person names. It refuses a home that is not empty. | `test_G7_3_a_copy_is_restored_to_a_new_empty_home` *(to build)* |
| G7.4 | `backup <folder>` writes the forge's own backup (Gitea's dump) plus the skill's settings without its keys. A new computer with only that file and the skill gets back every copy (`setup --from <file>`). | `test_G7_4_a_new_computer_gets_every_copy_back_from_the_backup` *(to build)* |

### G8 Never reachable from outside

| Codon | Claim | Proof |
|---|---|---|
| G8.1 | No setting the skill writes opens the forge to the network, and the skill runs no tunnel. `status` tries the forge's port on each of this computer's own network addresses, and reports a failure if it answers on any but 127.0.0.1. | `test_G8_1_the_forge_answers_on_this_computer_only` *(to build)* |
| G8.2 | Making the forge reachable from other computers is the person's own choice. The skill does not do it, and it says what that would mean. | `test_G8_2_exposing_the_forge_is_left_to_the_person` *(to build)* |

### G9 Upgrade, self-update, uninstall

| Codon | Claim | Proof |
|---|---|---|
| G9.1 | `upgrade-forge` moves only to a version pinned in a newer release of the skill. It makes a backup first, stops the forge, swaps the binary (checksum checked), starts it and runs `status`. When that fails, it puts the old binary and the backup back. | `test_G9_1_a_forge_upgrade_backs_up_first_and_rolls_back_on_failure` *(to build)* |
| G9.2 | `self-update` replaces the skill's own files whole, in one rename, and never while a sync runs. | `test_G9_2_self_update_swaps_whole_and_never_during_a_sync` *(to build)* |
| G9.3 | `uninstall` stops and removes the service, the timer, the binary, the settings and both keys, then lists what is left: the folder of copies. That folder is deleted only by `uninstall --delete-copies`, after the person types the number of copies it holds. | `test_G9_3_uninstall_leaves_only_the_copies_and_deletes_them_only_on_the_persons_word` *(to build)* |

## Promises

`PROMISES.json` holds what real machines, and the people using them, showed must change: what was seen, the
promise, its codon, and the version it is due in. Five tests keep it honest. A version cannot be released while a
promise due in it has no test. The version after that cannot be released until the row says on which real machine
the promise was seen to work. A date moves only with a written reason, and a promise that came from what the
person saw on their own machine moves only by their word. Rows are never deleted. A lesson that was heard and not
taken is written under `declined` with the reason.

A row whose test leans on a stand-in for GitHub, git, Gitea or Claude Code is seen on a real machine before the
tag of its version, not after.

## Invariants

These hold across every gene. A change that breaks one is a plan change first.

| Invariant | Proof |
|---|---|
| INV1 It never writes to GitHub. | G5.3 |
| INV2 The forge is not reachable from any other computer unless the person opens it themselves. | G1.3, G8.1 |
| INV3 A key is kept in one file of its own, only the person can read it, and it is never printed, logged, put on a command line or given to Gitea for GitHub. | G2.2, G3.4, G6.4 |
| INV4 It never deletes a copy, and never drops the only ref to a commit before the set period. | G3.6, G5.2, G9.3 |
| INV5 It never shows a copy as current that it has not seen current. | G6.1, G6.2 |
| INV6 Python standard library only; it calls `git`, the pinned Gitea binary, GitHub's API and its own forge, and nothing else. | `test_INV6_standard_library_only_and_no_other_network` *(to build)* |
| INV7 Nothing it does costs money. It uses no paid service and no paid store, and it never touches billing. | by review: no code path names one |

The link between this plan and the tests is itself tested: every proof named here is a real test, and every codon
test has a codon here. A proof marked *(to build)* is the one exception, and it may be named only while the
version is 0.0.0.

## How it is tested

- **Unit tests, in CI** on GitHub's standard runners: a stand-in GitHub (a local HTTP server for the API, and git
  over local HTTP for the fetch), and the real pinned Gitea binary, downloaded and checksum checked in the job.
- **Labs and proofs never see a real project.**
  - A login that can see more than the repositories made for the proof is used only with an `include` list
    naming those repositories. The lab checks the list before anything else, and fails when it is empty or names
    anything else.
  - "Every repository the person owns" runs only in a clean account with nothing but throwaway repositories.
- **Real machines, before any tag:** clean test accounts on a Linux machine and a Mac, with no administrator
  rights. The proof that counts is a fresh agent in a clean account, given only this repository's address and
  one sentence, getting it working end to end with a real GitHub account. It uses throwaway private
  repositories made for the proof and deleted after.
- **The hard cases:**
  - GitHub out of reach, with the restore drill (G7.2) run from the copy alone.
  - The computer rebooted, or put to sleep, in the middle of a sync.
  - The disk full.
  - Two syncs at once.
  - A key expired, or one that can write.
  - A repository renamed, deleted, force-pushed or replaced under its old name.
  - An uninstall that leaves nothing behind but the copies.
  - A new computer restored from the backup.

## Proven by hand on real machines, not by a test

Nothing yet. The lab facts above are about stock Gitea, not about this skill.

## Not verified

- Whether GitHub's answer shows the permissions of a fine-grained key. G2.3 covers classic keys only, until
  this is seen.
- Whether a systemd user unit starts at boot without a login on a machine where the person cannot turn on
  lingering.

## Not goals (0.1)

- **Forgejo.** One forge, done well. It may come later as a setting, after its own real-machine proof.
- **Copying issues, pull requests, releases or wikis.** Each could be a later gene, after a real check on the
  pinned version. Wikis are the nearest: Gitea's own mirror copied one.
- **CI on the copy.** That is a different skill (Windstorm Repo Minions).
- **Windows.**
- **Reaching the copy from other computers.**
- **Writing back to GitHub, ever.**
- **GitHub Enterprise Server.**
- **Gists.**
