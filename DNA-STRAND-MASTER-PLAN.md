# DNA Strand Master Plan: Windstorm Mirror for GitHub

Plan version 1.2 (2026-11-22), reviewed; contracts being seen. Builds nothing yet: the plan is reviewed before any code. The first
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

What the person is told before the first copy (G8.3): the copy is as private as this computer. Anyone who can read
this account's files can read every project in it. Disk encryption, and who uses this computer, are the person's.

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

**What a long-running mirror taught** (a Git mirror of about 140 repositories that an AI agent ran every day from
August to November 2026 on a shared Linux workstation, on stock Gitea beside GitHub; each line is what was seen
then):
- Gitea's own pull mirror, given a key, wrote that key into the repository's git config. This was seen in about
  125 repositories at once, then twice more after a clean-up. One exposure lasted about a day, and the key was
  changed.
- Removing those keys froze more than a hundred private mirrors, and nothing said so: Gitea raised no alarm when
  a mirror stopped refreshing. An hourly lag check was added, then a 24-hour alarm, because mirrors marked
  "known stale" had sat frozen for weeks.
- Mirrors set up the other way, from the copy to GitHub, would have overwritten live work with a stale copy on
  their timer. They were removed before they fired.
- A hand-kept list of repositories missed 130 active ones, and a listing capped at 300 would have cut a larger
  account short.
- One hung fetch held every repository behind it for 5 to 10 minutes.
- A GitHub rename, and a reused name, nearly sent one repository's history onto another.
- A check reported "0 found" because it had searched nothing.
- A config file printed in full put a secret into a log.
- After a reboot, a timer showed "active" with no next run for 3 hours.
- A script was replaced while it was running.
- The hourly GitHub API allowance ran out (about 3,900 of 5,000 calls an hour were used). The lesson: a mirror
  spends few calls, and its key is read-only and its own, used for nothing else.
- No one recovered a project from that mirror alone. Its drill restored the forge's own database from a backup.

**What stock Gitea did in our lab** (seen by us, 2026-11-22: Gitea 28.4.0 from the official Linux binary, checksum
checked, in a test account with no administrator rights and no Docker, bound to 127.0.0.1):
- A pull mirror made with a key wrote the key into the repository's git config remote URL, in a file every
  account on the machine could read (mode 664).
- The default mirror interval was 8 hours.
- A force-push or branch deletion at the source was erased from the copy at the next sync: no branch or tag
  reached the old commit any more.
- Asked for everything, a mirror of a public GitHub repository copied: the wiki (as its own mirror), every branch
  and tag, and GitHub's pull-request refs. Releases and issues were not copied (0 of 30 releases; tags only).
- Large files (LFS): not settled for Gitea's own mirror. That test used a single Gitea instance.

**The skill's own road, on stock Gitea** (seen by us, 2026-11-22, same machine, Gitea and conditions, with a
private GitHub repository made for the proof: made-up data, two branches, a tag, one 400 KB large file):
- A bare clone and fetch of branches and tags with the key held only in the git process's environment copied
  every branch and tag.
- `git lfs fetch --all` with the same key, then `git lfs push --all` into Gitea, copied the large file. Gitea
  served all 400,000 bytes.
- Gitea accepted a pushed branch named `kept/2026-11-22/side` and listed it as a branch.
- The key was found in none of the 95 files the run wrote, and in no git config.

So the skill does not use Gitea's own pull mirror with a key. It fetches from GitHub itself, with the key held
only in memory, into a local cache, then pushes into stock Gitea with a narrow Gitea key. The long-running
mirror used this same road for its most important repositories for months. It is also what lets the copy keep
what GitHub loses.

## Genes and codons

The whole tool is one script, `scripts/mirror.py`: Python standard library, plus `git` and the Gitea binary.
Commands: `setup`, `key set <owner>`, `sync`, `status`, `restore`, `backup`, `upgrade-forge`, `self-update`, `uninstall`.

### G1 Install the forge: stock Gitea, at home, for the person only

| Codon | Claim | Proof |
|---|---|---|
| G1.1 | `setup` downloads the pinned Gitea version for this system and architecture from Gitea's own download site, naming the skill in its User-Agent, and checks it against the SHA-256 written in the script before it is ever run. A mismatch stops setup, and the file is deleted. | `test_G1_1_the_forge_is_the_pinned_version_and_its_checksum_is_checked_first` |
| G1.2 | The forge runs as the person's own user, with no administrator rights: a systemd user unit on Linux, a launchd agent on macOS. It starts at login, and after a reboot wherever the system lets a person's own service start without a login (G1.6). | `test_G1_2_the_forge_runs_as_the_person_and_comes_back_after_a_reboot` |
| G1.3 | The forge listens on 127.0.0.1 only, on a free port picked at setup. Every start checks the port is still free and says so if not. SSH is off, sign-up is closed, nothing is shown to anyone not logged in, and Actions are off. | `test_G1_3_the_forge_listens_on_this_computer_only_with_signup_closed` |
| G1.4 | Setup makes two Gitea accounts: the person's (an administrator), and a `mirror` account that owns the copies. It writes the person's first password to a file only they can read, and says where. It never prints the password. | `test_G1_4_setup_makes_the_two_accounts_and_never_prints_a_password` |
| G1.5 | `setup --dry-run` says everything setup would do and changes nothing. Running `setup` twice does not make a second forge, account or service. | `test_G1_5_setup_dry_run_changes_nothing_and_setup_twice_is_one_setup` |
| G1.6 | Where the forge and the timer start only once the person logs in, `setup` and `status` say so in plain words: "after a restart, the copy stands still until you log in". That covers a Linux account without lingering, and every Mac (a launchd agent runs only in a login session; with FileVault on, nothing runs before someone logs in). On Linux, `setup` first tries `loginctl enable-linger` for the person's own account. When the system refuses, it names that as a step for the computer's administrator, with the one command they run. | `test_G1_6_when_it_will_not_come_back_by_itself_it_says_so` |
| G1.8 | Setup checks that git is installed before anything else, and if it is not, says so in one plain sentence with how to install it, and changes nothing (no download). git must also be where the forge's service and the sync timer look (on Linux the PATH of the person's own service manager, read with `systemctl --user show-environment`; on a Mac, launchd's): when git is only in a folder outside it, setup says so, with where git is and how to install it for the whole computer, and changes nothing. The dry run and setup name a missing git lfs, and what it costs. Every install line named was run as written on a fresh system first ("Install lines, as seen", M28). A program that is not installed is an answer, never a traceback. | `test_G1_8_a_computer_without_git_is_told_before_anything_is_downloaded` |
| G1.9 | Lingering changes how the person's computer behaves after they log out, so: the dry run says, in plain words, what it means before setup; it asks this computer (polkit, never with a password prompt, with a time limit) whether the person may switch it on themselves, and says one of three things: yes, no (the administrator's one command), or not known (setup will try, without a password, and the command in case it is refused); setup switches it on without a password prompt and records that it did; `uninstall` switches it off only when setup switched it on, and says which; `setup --no-linger` never tries, and says the mirror then syncs only while the person is logged in. The choice is the person's, and the tool carries it: on Linux with lingering off, plain `setup` changes nothing (no download), prints the sentence and the two choices, `--linger` or `--no-linger`, and ends with "Needs you"; where the person may not switch it on themselves, `--linger` ends on the administrator's command. A no is kept, so a later plain `setup` does not ask again. | `test_G1_9_lingering_is_said_first_asked_of_this_computer_and_undone_only_if_setup_did_it` |
| G1.10 | Setup says the forge runs only once it answers; otherwise it says the service was started and the forge was not answering yet (seen: the first `status` right after setup, on a stock machine, found the forge still starting). | `test_G1_10_setup_says_the_forge_runs_only_once_it_answers` |
| G1.7 | On a Mac, the forge comes back by itself after a restart only when the system logs the person in by itself (automatic login set to them, FileVault off). Otherwise setup says the copy stands still until they log in, and why. Of the automatic-login setting it keeps only whether it is set and whether it is this person's own account; another account's name is never said or kept. | `test_G1_6_on_a_mac_it_comes_back_by_itself_only_with_the_systems_own_login` |

### G2 The GitHub keys: one read-only key per owner, each in its own file, each watched

A fine-grained GitHub key belongs to one owner: the person's own account, or one organisation (GitHub's docs, read
2026-11-22; to be seen with a real key, contract C3). So the skill asks for one key per owner. Only a classic key
reaches several owners, and a classic key that can read private repositories can also write to them.

| Codon | Claim | Proof |
|---|---|---|
| G2.1 | Before anything else, the skill lists the keys the person will be asked to make: one for their own account, and one for each organisation named in the settings. Each is fine-grained and read-only (Contents and Metadata), on all that owner's repositories. For each one it gives the exact page and settings, and says when an organisation may have to approve the key. Making each key, and approving it, is the person's step, or the organisation's. The agent never logs in to GitHub for them. | `test_G2_1_the_person_is_told_first_which_read_only_keys_to_make` *(to build)* |
| G2.2 | `key set <owner>` reads one key from standard input, never from a command line. It checks against GitHub that the key reaches that owner's repositories, then keeps it in a file of its own that only the person can read. A key is used only for its own owner's repositories. It is never written anywhere else: not into Gitea, not into a git config, not into a log. | `test_G2_2_each_key_is_kept_in_its_own_file_and_nowhere_else` |
| G2.3 | A key that can write is named for what it is. When GitHub's answer shows a classic key with write scopes, `key set` and `status` say "this key can write to GitHub; the skill never uses that, make a read-only one". | `test_G2_3_a_key_that_can_write_is_named_for_what_it_is` *(to build)* |
| G2.4 | `status` shows each key's expiry, from GitHub's answer, on its own line, and warns 14 days before it. A key that is refused, or still waiting for an organisation's approval, turns that owner's copies to "cannot refresh", never "current", and `status` says which key it is. | `test_G2_4_each_keys_expiry_is_watched_and_a_refused_key_is_never_current` *(to build)* |
| G2.5 | Changing a key is one step, `key set <owner>` again. The next sync proves that every repository of that owner refreshed with the new key, or names those that did not. | `test_G2_5_a_new_key_is_one_step_and_the_next_sync_proves_it` |

### G3 Find every project, and copy it

| Codon | Claim | Proof |
|---|---|---|
| G3.1 | Each sync asks GitHub, with each owner's own key, for every repository the person owns, plus those of the organisations named in the settings, page by page to the last page. A page that fails stops the listing: the skill never acts on half a list as if it were whole. | `test_G3_1_every_repository_is_found_page_by_page_to_the_end` |
| G3.2 | A repository is known by GitHub's numeric id, not by its name. When it is renamed on GitHub, the copy is renamed too. When a new repository takes an old copy's name, the old copy is moved aside under a new name, never overwritten. Before every push, the skill reads the copy's current name by Gitea's id and checks it, and git never follows a redirect (`http.followRedirects false`), because Gitea sends a push to an old name on to the renamed repository (C7). | `test_G3_2_a_rename_or_a_reused_name_never_writes_one_project_over_another` |
| G3.3 | A copy is made as a private repository of the `mirror` account, with Actions off. Branches and tags are copied. GitHub's pull-request refs, releases, issues and wikis are not copied in 0.1, and `status` says so. | `test_G3_3_branches_and_tags_are_copied_and_what_is_not_is_said` |
| G3.4 | The fetch from GitHub carries the key as a header set in the environment of that one git process. The key is never in a URL, a git config, a command line or the process list. | `test_G3_4_the_key_travels_only_in_the_fetchs_own_environment` |
| G3.5 | Settings can leave repositories out by name or pattern, or name the only ones to copy (`include`). The default is all of them, and `status` lists what was left out and why. Archived, empty, forked and private repositories are copied unless left out. An `include` that matches nothing stops the sync: it never widens to everything. | `test_G3_5_what_is_left_out_is_chosen_and_listed` |
| G3.6 | A repository deleted on GitHub, or no longer visible to the key, keeps its copy. `status` shows it as "gone from GitHub since <date>". The skill never deletes a copy. | `test_G3_6_a_repository_gone_from_github_keeps_its_copy` |
| G3.7 | Before the first copy, the skill says what it is about to do: how many repositories, their total size as GitHub reports it (which can read 0, C1), and a rough time measured from the first ones. It waits for the person's go. `sync --announce` never copies, the first time or any time after (a cold start saw an agent run it again "to re-check" and get the first copy made, M26); after the go it says when the first copy was made. The go is a sync run on the person's word, recorded with its time; the timer waits for it, and `status` says "first copy: made on <time>, on your go" or "first copy: waiting for your go". During the first copy, after the first three repositories, it says how long the rest will take at that pace. | `test_G3_7_the_first_copy_is_announced_with_its_size_and_time_before_it_starts` |
| G3.8 | Large files (Git LFS) are copied with the repository when `git lfs` is installed, using the same in-memory key. A repository that uses large files on a computer without `git lfs` is copied without them, and `sync`, `status` and `restore --check` each say it as a `problem:` line, before the counts, with the one next step (install git lfs). Once git lfs is installed, the next sync copies that repository again, not the next full pass (a cold start saw a day's wait and no problem line, M27). | `test_G3_8_large_files_are_copied_or_named_as_missing` |

### G4 Stay current

| Codon | Claim | Proof |
|---|---|---|
| G4.1 | A timer runs `sync` every 15 minutes by default (a setting); after the person's go, the first copy can run from it in the background, and `status` (G6.5) shows how far it is. After a reboot, `status` reads the timer's next run, not only whether it is switched on. | `test_G4_1_a_timer_runs_sync_and_status_reads_its_next_run` |
| G4.2 | Every fetch and push has a time limit and one retry. One slow or broken repository never holds up the rest: it is named, and the others carry on. | `test_G4_2_one_slow_repository_never_holds_up_the_rest` |
| G4.3 | Only one sync runs at a time. A second one started meanwhile says so and exits. A lock whose owner is gone is taken over and noted. | `test_G4_3_one_sync_at_a_time` |
| G4.4 | A sync counts its GitHub API calls. It stops listing, and says so, when GitHub's remaining allowance falls below a floor (a setting). | `test_G4_4_a_sync_spends_few_api_calls_and_stops_above_a_floor` |
| G4.5 | Before each sync the free disk space is checked against a floor (a setting). Below it, the sync does not start, and `status` says why. A sync stopped part-way leaves every copy as it was, or as it fully became, and never marks a half-done copy current. | `test_G4_5_a_full_disk_stops_the_sync_cleanly` |
| G4.7 | The sync timer counts from the start of the person's own service manager (`OnStartupSec`), never from the boot alone: with lingering it runs 2 minutes after boot, and without it 2 minutes after the person logs in, however late. After a restart it always has a next run. | `test_G4_7_after_a_restart_the_timer_always_has_a_next_run` |
| G4.6 | A sync fetches only what changed. It reads each repository's `pushed_at` from the listing it already made (G3.1), and fetches only the repositories whose `pushed_at` is newer than their last good refresh. A full pass, once a day by default (a setting), fetches every repository, so a change the listing did not show cannot hide for more than a day. A quiet sync costs one listing call per 100 repositories per owner and no fetch. For a person with 300 repositories and two organisations of fewer than 100 each, that is 5 calls; at one sync every 15 minutes, 20 calls an hour, against GitHub's 5,000. `status` shows the calls the last sync spent. | `test_G4_6_a_sync_fetches_only_what_changed_and_a_daily_pass_fetches_all` |

### G5 Keep what GitHub loses

| Codon | Claim | Proof |
|---|---|---|
| G5.1 | Before a branch or tag is moved backwards, rewritten or deleted in the copy, its old commit is kept under `kept/<date>/<name>` in the same copy. | `test_G5_1_a_rewritten_or_deleted_branch_is_kept_before_the_copy_follows` |
| G5.2 | Kept refs are dropped after 90 days by default (a setting). Nothing is dropped while it is the only ref that reaches a commit, unless the person has set that period. | `test_G5_2_kept_refs_live_for_the_set_period` |
| G5.3 | The skill never writes to GitHub. It has no push to GitHub, no push mirror and no API call that changes anything there. | `test_G5_3_nothing_is_ever_written_to_github` |
| G5.4 | A read that failed is a third answer, "could not be read", never a no. Every read of GitHub and of the forge that a decision hangs on (an owner's list of projects, a project's branches and tags, a copy found by its id or its name, what a copy holds) tells "not there" from "could not read". On a failed read nothing is deleted, renamed, made, moved to `kept/`, marked or said gone, and no owner is announced as having 0 repositories. Because GitHub answers 404 for a private repository both when it was deleted and when the key can no longer see it, "gone from GitHub" is said only when a list read that succeeded, with the same key, no longer names the project, and the copy is kept; a 404 on the project is "could not be read", and a refused key is said as a key that stopped working, with the step to make a new one. Proven by failing each read in turn, once and from then on, under sync, status and restore. | `test_G5_4_a_read_that_failed_is_never_taken_for_a_no` |

### G6 Say what is true

| Codon | Claim | Proof |
|---|---|---|
| G6.1 | `status` prints one line per problem, then `copies: N on GitHub, N copied, M behind, U unreadable`. A copy is behind when its last good refresh is older than twice the sync interval, or when its refs differ from GitHub's after a grace period. | `test_G6_1_status_says_how_many_are_current_and_names_the_rest` |
| G6.2 | A copy that cannot refresh is never shown as current, for any reason. There is no "known stale" pardon. | `test_G6_2_a_copy_that_cannot_refresh_is_never_shown_as_current` |
| G6.3 | `status` scans every repository config in the forge, and the skill's own files, for anything shaped like a key. A scan that read no files is a failure, not a pass. | `test_G6_3_the_key_scan_counts_what_it_read_and_zero_is_a_failure` |
| G6.4 | The skill never prints or logs a key, a password or a config file whole. It reads settings by name. | `test_G6_4_no_secret_reaches_the_output_or_the_log` |
| G6.5 | `status` shows the forge's version, the key's expiry, free disk space, the last good sync, and the next. The expiry leans on a header not seen yet (C3): when GitHub's answer carries no expiry the skill can read, `status` says "expiry not known", never "no expiry" and never a date it worked out, and no warning fires on a guess. | `test_G6_5_status_shows_version_expiry_disk_and_the_last_and_next_sync` |
| G6.7 | `status` reads the timer's next run on the wall clock or on the boot clock, and says "no next run" only when there is none on either. | `test_G6_7_a_next_run_on_the_boot_clock_is_a_next_run` |
| G6.6 | Once a day, `status` asks GitHub for this skill's latest release, one call with no key. It says when that release pins a newer Gitea than the one running, and whether the release names it as a security fix. It changes nothing by itself: `self-update`, then `upgrade-forge`, is the person's choice. | `test_G6_6_status_says_when_a_newer_release_pins_a_newer_forge` |
| G6.8 | Every stop and needs-you line names what happened and the one next step: each line the tool says just before it stops with a failing code, and each message it stops with. A line built elsewhere (the key check, a missing git, lingering) is checked where it is built. A cold start saw a stop line with no next step send the agent into the source four times, and 39 of 63 such lines had none (M30). | `test_G6_8_every_stop_and_needs_you_line_names_what_happened_and_the_next_step` |

### G7 Restore

| Codon | Claim | Proof |
|---|---|---|
| G7.1 | Each sync writes down the refs it last saw on GitHub for every repository. | `test_G7_1_each_sync_writes_down_the_refs_github_last_showed` |
| G7.2 | `restore --check` needs no network beyond this computer. It clones every copy from the forge into a scratch folder, checks every branch and tag against the last refs seen on GitHub, runs `git fsck`, prints one line per difference, then a total, and removes the scratch folder. | `test_G7_2_restore_check_proves_every_copy_from_the_forge_alone` |
| G7.3 | `restore --to <url> <repository>` pushes one copy, every branch and tag, to a new, empty home the person names. It refuses a home that is not empty. | `test_G7_3_a_copy_is_restored_to_a_new_empty_home` |
| G7.4 | `backup <folder>` writes the forge's own backup (Gitea's dump) plus the skill's settings without its keys. The file holds the forge's own secrets, so it is readable by the person only, and the skill says so (C8). A new computer with only that file and the skill gets back every copy (`setup --from <file>`). | `test_G7_4_a_new_computer_gets_every_copy_back_from_the_backup` |

### G8 Never reachable from outside

| Codon | Claim | Proof |
|---|---|---|
| G8.1 | No setting the skill writes opens the forge to the network, and the skill runs no tunnel. `status` tries the forge's port on each of this computer's own network addresses, and reports a failure if it answers on any but 127.0.0.1. | `test_G8_1_the_forge_answers_on_this_computer_only` |
| G8.2 | Making the forge reachable from other computers is the person's own choice. The skill does not do it, and it says what that would mean. | `test_G8_2_exposing_the_forge_is_left_to_the_person` |
| G8.3 | Before the first copy, `setup` says, and the README says: "The copy is as private as this computer. Anyone who can read this account's files can read every project in it. Disk encryption, and who uses this computer, are yours." | `test_G8_3_the_person_is_told_the_copy_is_as_private_as_this_computer` |

### G9 Upgrade, self-update, uninstall

| Codon | Claim | Proof |
|---|---|---|
| G9.1 | `upgrade-forge` moves only to a version pinned in a newer release of the skill. It makes a backup first, stops the forge, swaps the binary (checksum checked), starts it and runs `status`. When that fails, it puts the old binary and the backup back. | `test_G9_1_a_forge_upgrade_backs_up_first_and_rolls_back_on_failure` |
| G9.2 | `self-update` replaces the skill's own files whole, in one rename, and never while a sync runs. | `test_G9_2_self_update_swaps_whole_and_never_during_a_sync` |
| G9.3 | `uninstall` stops and removes the service, the timer, the binary, the settings and both keys, then lists what is left: the folder of copies. That folder is deleted only by `uninstall --delete-copies`, after the person types the number of copies it holds, at a terminal: with anything else on standard input the tool refuses before it removes anything, and says the person runs it themselves in a terminal window of their own. | `test_G9_3_uninstall_leaves_only_the_copies_and_deletes_them_only_on_the_persons_word` |

### G10 The skill an agent follows

| Codon | Claim | Proof |
|---|---|---|
| G10.1 | `skills/mirror/SKILL.md` is in the Agent Skills format. It passes a strict YAML parser and the format's limits, and `gh skill publish --dry-run`, in CI. | `test_G10_1_skill_md_fits_the_agent_skills_format` |
| G10.2 | SKILL.md gives one fixed order, and the agent follows it without skipping or reordering: `setup --dry-run` (shown to the person), the keys to make (G2.1), `setup`, `key set` for each owner, the first copy announced (G3.7) and the person's go, `status`, then `restore --check`. | `test_G10_2_skill_md_gives_the_steps_in_the_fixed_order` |
| G10.3 | Each step that is the person's is named in SKILL.md, with what the agent says to them. The steps: logging in to GitHub; making each key, with its page and settings; an organisation's approval; the go for the first copy; and, where needed, the administrator's lingering command. The agent never logs in for the person, and never presses Agree, Accept or Continue on a terms screen. | `test_G10_3_every_step_that_is_the_persons_is_named_with_what_to_say` |
| G10.4 | The cold start works. A fresh agent in a clean account, given only this repository's address and one sentence, reaches `copies: N on GitHub, N copied, 0 behind` and a passing `restore --check`. The write-up counts the person's steps. Anything else the person had to do is a finding, and gets a row. | by hand on a real machine, Linux and macOS (Milestones) |
| G10.5 | What the person must hear comes from the tool as one numbered block, `tell the person 1 of N:` to `N of N:`, in the dry run (every step of setup, lingering, a missing git lfs), in setup (after a restart, how private the copy is) and in the announcement (how many, how big, where the copies go, what is not copied, how private). SKILL.md says to give the block whole, word for word and in order, before any words of the agent's own, so a line dropped, shortened or merged on its way to the person shows (a cold start saw the smallest model do all three, M29). | `test_G10_5_what_the_person_must_hear_comes_as_one_numbered_block` |

## Milestones

| Version | What it is | On | Accepted when |
|---|---|---|---|
| 0.1.0 | Genes G1 to G10, with Gitea 28.4.0 pinned. | Linux on x86-64; macOS 13 and later on Intel and Apple Silicon. | Every codon is built, and its test is green in CI on Linux and macOS. Every row due in 0.1.0 has its test. Every platform contract below has been seen. The cold start (G10.4) has passed once on Linux and once on a Mac, in clean accounts. Each hard case under "How it is tested" has been run on at least one of the two. The restore drill with GitHub blocked has passed on both. The skill's owner decides the tag. |
| 0.1.x | Fixes from what real machines show. | The same. | Each fix has its row, its test and its proof. |
| 0.2.0 | Candidates, not promises: wikis, Forgejo as a setting, Windows, a sync at wake (seen on a stock Ubuntu 24.04 VM, 2026-11-23: after a 90 s suspend the timer's next run moved later by exactly the time asleep, because `OnUnitActiveSec` counts on the boot clock, which stops in sleep; so after a night asleep the first sync comes up to one interval after the wake, not at once). | | Planned only after 0.1.0 has been out a while. |

Before 0.1.0 nothing is offered for install: main's README says "Nothing here can be installed yet". The one exception is a release branch (`release-X.Y.Z`), to which a cold start is sent before the tag: it may offer the install only as today's main (main's tip is in it) plus the README, the changelog and the manifest, nothing else; a pull request into main never gets the exception, so the install lines reach main only with a version of 0.1.0 or more; and before every cold start main is merged into the release branch and its checks are read (`test_nothing_is_offered_for_install_before_0_1_0`).

Order of work after this plan: the platform contracts first (written in `docs/CONTRACTS.md`), then one gene per
pull request, G1 first and G10 last.

## Platform contracts

What GitHub, git, Gitea and each system must do for the code to be right. Each one is seen on a real machine, and
written in `docs/CONTRACTS.md` with what was seen, on what and when, before the code that leans on it is merged.

| Contract | What must hold | Leaned on by | Seen |
|---|---|---|---|
| C1 | GitHub's repository listings (`/user/repos`, owner affiliation; `/orgs/<org>/repos`) give `id`, `name`, `full_name`, `private`, `archived`, `fork`, `size`, `pushed_at` and `default_branch`. Paging works through the `Link` header, at most 100 to a page. | G3.1, G3.2, G3.7 | in part, 2026-11-22: the fields and `Link` paging on an organisation's listing. `size` read 0 after pushes. Not yet: the 100 cap, `/user/repos` |
| C2 | `pushed_at` changes after a push, a force-push, a branch deletion and a tag push. | G4.6 | 2026-11-22: it moved within 8 s on all four |
| C3 | A fine-grained key has one owner. What GitHub's answer shows about a key: scopes for a classic key, the expiry header, anything about a fine-grained key's rights, and the answer while an organisation's approval is pending. | G2.1 to G2.4 | in part, 2026-11-22: a classic key's scope headers. A fine-grained key from GitHub's docs only; a real one needs a login, the person's step |
| C4 | The allowance headers on REST answers, and whether git fetches over HTTPS count against the allowance. | G4.4, G4.6 | 2026-11-22: the headers; with no key, git fetches spent no REST calls. Not yet with a key |
| C5 | `git fetch` with the key in that process's environment (`GIT_CONFIG_COUNT`, an extra header scoped to github.com), and `git lfs fetch` with the same key. | G3.4, G3.8 | 2026-11-22, a Linux test account, a private repository made for the proof |
| C6 | In stock Gitea 28.4.0: a repository created through the API, then branches, tags, large files and `kept/...` branches pushed to it. | G3.3, G3.8, G5.1 | 2026-11-22, as an administrator, and as the `mirror` account (its key needs `write:repository` and `write:user`) |
| C7 | A rename through Gitea's API, and what Gitea answers for the old name. | G3.2 | 2026-11-22: the id is kept, the old name answers 301, and **a push to the old URL lands on the renamed repository** |
| C8 | `gitea dump` on 28.4.0, and a restore from it on another computer. | G7.4, G9.1 | 2026-11-22: a dump restored on another Linux computer, with every user and repository under its old id, and fsck clean. Not yet with large files |
| C9 | The boot service: a systemd user unit with and without lingering, and `loginctl enable-linger` run by a person who is not an administrator; a launchd agent on macOS on Intel and Apple Silicon, with FileVault on and off. | G1.2, G1.6, G4.1 | not yet |
| C10 | Gitea's settings take effect: it listens on 127.0.0.1 only, sign-up is closed, signing in is needed to view anything, and Actions are off (for the whole forge and per repository). Probed from another computer. | G1.3, G8.1 | in part, 2026-11-22: the settings were set; not probed from another computer |
| C11 | GitHub's answer for this skill's latest release, read with no key. | G6.6 | 2026-11-22, with no key, 60 an hour |

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
| INV6 Python standard library only. Over the network it reaches only GitHub (its API, with or without a key, and git), Gitea's download site for the pinned binary, and any host those two send it on to for that same request; its own forge on 127.0.0.1; and an address the person names for `restore --to`, through git. The programs it runs are `git`, the pinned Gitea binary, the service manager (`systemctl` and `loginctl`, or `launchctl`), the system's address list (`ip` or `ifconfig`), on Linux `pkcheck`, read-only and never asking for a password, to ask whether the person may switch on lingering themselves, on a Mac the two settings it reads (`defaults read` of the automatic-login account, `fdesetup status`), both read-only, and Python itself to check a self-update. | `test_INV6_standard_library_only_and_no_other_network` |
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

### What counts as a proof

| Codon | Claim | Proof |
|---|---|---|
| T1 | A proof named in this plan counts only when the suite's own runner saw it pass. The runner refuses a test name defined twice, two classes of one name, a test that is not where unittest collects it, and any skip mark. A skip raised inside a test counts as "not run here", never as a pass, and only when its reason is on a fixed list (no real pinned Gitea for this system, no git lfs here) and that is true on that machine. In CI it is never true, and a last job needs every proof to have passed on at least one of the four jobs. | `test_the_gate_refuses_every_way_a_test_can_hide` |

## Proven by hand on real machines, not by a test

The lab facts above are about stock Gitea, not about this skill.

### Install lines, as seen

Every install line the tool names (`INSTALL_GIT`, `INSTALL_LFS` in `scripts/mirror.py`) was run as written on a
fresh system before it was written; `test_G1_8_a_computer_without_git_is_told_before_anything_is_downloaded` holds
that the tool names no command that is not in this table. Fresh cloud images, a new disk each, 2026-11-23 (row M28):

| Line | System | Seen |
|---|---|---|
| `sudo apt update && sudo apt install git` | Ubuntu 24.04.5 cloud image; Debian 13 genericcloud image | works (git 2.43.0; 2.47.3) |
| `sudo apt update && sudo apt install git-lfs` | Ubuntu 24.04.5; Debian 13 | works (git-lfs 3.4.1; 3.6.1) |
| `sudo dnf install git` | Fedora 45 Cloud Base | works (git 2.56.1; the image has no git) |
| `sudo dnf install git-lfs` | Fedora 45 Cloud Base | works (git-lfs 3.7.1) |
| `xcode-select --install` | macOS 13.7.8 (Intel) and macOS 27.2.1 (Apple M4), both with the tools already | seen only on a Mac that already had them: "already installed", exit 1. The installer window on a fresh Mac is not seen. |
| large files on a Mac: https://git-lfs.com | | not seen installed on a Mac |

Lines tried and NOT used, so that no one puts them back:
- `sudo apt install git` and `sudo apt install git-lfs` alone: on a fresh Ubuntu 24.04.5 image, git failed with a
  404 on the stale package list and git-lfs said "Unable to locate package"; on a fresh Debian 13 image, which
  has no package lists at all, both said "Unable to locate package". Both cold-start agents met the git-lfs one.
- `brew install git-lfs`: on macOS 13.7.8 (Intel), with Homebrew installed, it did not install git lfs; Homebrew
  called the system an unsupported "Tier 3" configuration. The other test Mac's Homebrew belongs to another
  account and was not touched.

## Not verified

- Every platform contract marked "not yet" above.
- Whether anything in GitHub's answer shows a fine-grained key's rights. G2.3 covers classic keys only until this
  is seen (C3).

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
