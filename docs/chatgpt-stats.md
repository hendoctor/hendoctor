# Automatic ChatGPT profile statistics

This integration reads the five profile metrics through Codex app-server's
documented `account/usage/read` method. It publishes only aggregate statistics
and changes only the marked ChatGPT section in the profile README.

## Activation status

The initial JSON contains the personal and work screenshots supplied on
November 11, 2026. They are explicitly labeled as manual snapshots. Rounded
display values are kept as strings, with all exact numeric fields set to null.
No live authentication or scheduled private collection is implied by these files.

The collector must be connected and verified on a trusted persistent host before
enabling its schedule. The public GitHub workflow only updates freshness labels;
it cannot fetch new ChatGPT usage.

## Data path

1. A private scheduled process starts a local Codex app-server for each account.
2. It checks the expected account email and plan. Work also requires the expected
   workspace name and a member's workspace access token.
3. It reads usage, rechecks the account identity, validates the response, and saves five numeric fields plus
   the public handle, source, and fetch time. It does not start a model turn.
4. The publisher reads the current README and public data from one GitHub commit,
   merges newer account snapshots, and commits only the stats JSON and README.
5. A separate daily GitHub workflow marks snapshots older than 48 hours as stale.

No chat content, task history, account email, workspace name, credentials, or raw
API response is written to the public data. Daily activity buckets are omitted.

## One-time private setup

Use Python 3.10 or newer, a Codex CLI version exposing `account/usage/read`, and
the GitHub CLI. Keep a reviewed copy of these scripts on a private persistent
computer or trusted private runner. Do not attach an authenticated self-hosted
runner to this public repository. Do not automatically download and execute new
public-repository code as part of the authenticated job.

### Personal account

Run `codex login` on that host and select the personal account. Codex manages the
session and normal refresh. Verify `codex login status`. Do not copy credentials
from another active machine or put `auth.json` in this repository or its Actions
secrets. Run the job as the same operating-system user who logged in.

### Work account

For a Business or Enterprise workspace, create a finite-lived **member** Codex
access token in the correct workspace at
[ChatGPT Access tokens](https://chatgpt.com/admin/access-tokens). The member must
have both token-creation permission and local Codex access. Use the Codex scope
if a scope selector is shown. A service-account token represents a different
identity and is unsuitable for personal profile statistics.

Store the token in your private secret manager and inject it into the scheduled
process as `CHATGPT_WORK_ACCESS_TOKEN`. The collector passes it to only the work
app-server process through `CODEX_ACCESS_TOKEN` with ephemeral credential storage.
It never places a token on a command line or writes it to a data file. Rotate the
token before its expiration.

### Private configuration and first collection

Copy `examples/chatgpt-accounts.example.json` to a private directory outside the
repository and replace its placeholders. `expectedWorkspace` must match the work
workspace's display name exactly. Remove an account section if you are connecting
only one account initially. Both columns remain in the public JSON.

Copy `data/chatgpt-usage.json` to that directory as the initial private data file.
The following paths are examples; substitute absolute paths on your host:

```bash
python3 scripts/chatgpt_stats/collect.py \
  --account personal \
  --config /private/profile-stats/accounts.json \
  --data /private/profile-stats/usage.json

python3 scripts/chatgpt_stats/collect.py \
  --account work \
  --config /private/profile-stats/accounts.json \
  --data /private/profile-stats/usage.json
```

Compare each first successful result with that account's actual profile. The
usage API does not return a public profile handle or an immutable workspace ID.
Email, plan, and workspace-name checks reduce accidental account switching; the
first profile comparison establishes the intended association. Workspace names
are not unique identifiers. The integration does not calculate a combined total.

Authenticate `gh` as an identity allowed to write this repository. A GitHub
fine-grained token, if used, needs Contents read/write on `hendoctor/hendoctor`.
Keep GitHub authentication in the host's supported credential store. The initial
README markers and data file must be present on the target branch before use.

Preview the GitHub update without writing:

```bash
python3 scripts/chatgpt_stats/publish.py \
  --data /private/profile-stats/usage.json
```

After checking the values, run the complete refresh and publish:

```bash
python3 scripts/chatgpt_stats/run.py \
  --config /private/profile-stats/accounts.json \
  --data /private/profile-stats/usage.json \
  --publish
```

Without `--publish`, this command still refreshes the private data but only
previews the GitHub update. A `--branch` argument allows a staging branch during
initial verification.

## Scheduling and operations

Schedule that final command once daily on the chosen private host, using its
native scheduler: Task Scheduler on Windows, launchd on macOS, or a systemd user
timer on Linux. Use absolute executable/script paths and supply the same private
configuration, credential context, and work-token environment. Enable the
scheduler's failure reporting. No collector schedule is installed by this change.

The runner serializes the two accounts and publication. If one account fails,
the other can still update; the failed account retains its previous values and
fetch time. The overall command returns a failure status so the host can report
it. An unexpected account, missing identity field, unsupported plan, invalid
metric, or missing token fails closed. Null metrics display as unavailable.

The public `refresh-chatgpt-display.yml` workflow runs daily at 12:17 UTC after
this change reaches `main`. It uses the already-public JSON and a repository
scoped GitHub token. It does not store or use ChatGPT credentials. Its purpose is
to make an interruption visible even if the private collector stops running.

A concurrent GitHub edit causes a non-forced branch update to fail safely. The
next scheduled run reads the new head. A timeout after a write can leave its
outcome uncertain; inspect the branch before retrying. Branch protection can
require an approved writer or a separate review flow.

To pause new collection, disable the private scheduled task. To remove the
integration, disable the display workflow and revert these files and the marked
README section. Revoking the work token also stops work collection.

## Validation

```bash
python3 -m unittest discover -s tests -v
python3 scripts/chatgpt_stats/render.py \
  --readme README.md --data data/chatgpt-usage.json --check
```

The tests are offline. They cover account mismatches, credential isolation, RPC
errors and timeouts, strict public fields, null metrics, partial failures,
timestamp conflicts, concurrent GitHub edits, and preservation of the README
outside its markers. They do not replace the first authenticated end-to-end run.

## Primary references

- [Codex app-server: account and usage methods](https://learn.chatgpt.com/docs/app-server)
- [Business and Enterprise access tokens](https://learn.chatgpt.com/docs/enterprise/access-tokens)
- [Trusted private automation and session refresh](https://learn.chatgpt.com/docs/auth/ci-cd-auth)
- [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [GitHub Git references API](https://docs.github.com/en/rest/git/refs)

OpenAI's private-auth CI guidance explicitly excludes public/open-source
repositories. This design keeps authenticated collection on a trusted private
host and publishes only the sanitized results to this public repository.
