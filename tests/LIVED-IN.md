# Lived in: the settings a person's machine carries and a stock one does not

A stock test machine has an empty git config and no proxy. A developer's machine does not. Each line below is a
setting seen in a sighting run (the real pinned forge, a stand-in for GitHub, the setting in the person's own git
config or environment, then a first sync and status), what it did to the skill before, and what it does now, with
the test that holds it. The list grows by one sighting at a time (row M77 decides its shape in 0.1.1).

## Tried

| Setting | Before | Now | Held by |
|---|---|---|---|
| `GIT_CONFIG_GLOBAL` at a file of the person's (and `XDG_CONFIG_HOME/git/config`) | the forge wrote its user.name, user.email, core and gc lines into it | the forge runs without git's settings from the environment, and its own git settings stay in its folder | G8.5 (M73) |
| `url.<ssh address>.insteadOf <GitHub's https address>`, both common forms | the fetch went over ssh, where the person's own ssh key would read the project; the sync broke with half a sentence | the fetch stays on HTTPS with the read-only key; a line for one project's whole address stops that copy with a sentence | G3.14 (M74) |
| `http.proxy` in git's config, at an address that does not reach this computer | the push to the skill's own forge went to the proxy; the sync stopped, 0 copied | the skill's roads to its own forge take no proxy; the proxy still carries the calls to GitHub | G8.6 (M76) |
| `http_proxy` / `https_proxy` in the environment, no `no_proxy` | the skill's calls to its own forge went to the proxy; the sync stopped at its first step | as above | G8.6 (M76) |
| `core.hooksPath` with hooks of the person's | the person's hooks ran inside the skill's git (reference-transaction, pre-push) | every git the skill runs has hooks off | G8.6 (M76) |
| `init.templateDir` with hooks of the person's | the hooks were copied into each new cache and ran there | as above | G8.6 (M76) |
| `credential.helper` | never asked: the skill passes an empty helper list | unchanged | sighting 2026-11-24 |
| `core.sshCommand` | never run: the skill reads GitHub over HTTPS | unchanged | sighting 2026-11-24 |

## Not tried yet

These have not been seen with the skill. Nothing here says what they do.

- `transfer.fsckObjects` / `fetch.fsckObjects` set to true.
- `push.gpgSign` set to true or `if-asked`.
- An `include.path` (or `includeIf`) naming a file that is missing.
