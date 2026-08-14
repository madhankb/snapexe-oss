# snapexe-oss command & behavior reference

Full flag lists and the non-obvious behaviors. Read the relevant section when a task needs more than
the SKILL.md workflow (data streams, searchable snapshots, multi-node, failures).

## Table of contents
- [snapshot](#snapshot)
- [provision](#provision)
- [status](#status)
- [restore](#restore)
- [delete-all](#delete-all)
- [Credentials](#credentials)
- [Edge cases & gotchas](#edge-cases--gotchas)
- [What a snapshot does NOT carry](#what-a-snapshot-does-not-carry)

## snapshot
`snapexe-oss snapshot` (→ `opensearch_snapshot.py snapshot`)

| Flag | Required | Purpose |
|---|---|---|
| `--tag` | yes | Names resources + config (`snapexe-{tag}-*`, `snapexe-{tag}-config.json`) |
| `--endpoint` | yes | Source cluster URL, e.g. `https://host:9200` |
| `--repo-type` | yes | `fs` or `s3` |
| `--creds-file` | no | OpenSearch creds file (default `snapexe-creds.json`) |
| `--secret-id` | no | AWS Secrets Manager secret with OpenSearch creds; overrides `--creds-file` |
| `--indices` | no | Comma-separated indices/data-stream names; **skips auto-discovery** |
| `--repository` | no | Custom repository name |
| `--snapshot-name` | no | Custom snapshot name |
| `--repo-path` | fs | Directory (must be in `path.repo`) for `fs` |
| `--auto-provision` | no | s3 only: provision bucket+IAM, install repo-s3 + keys, reload, then snapshot |
| `--install-container` | with `--auto-provision` | Local container for node-local setup (keystore keys, repo-s3 install) |
| `--source-containers` | no | Override the auto-discovered node list for `--auto-provision` key install (comma-separated); use only when node names don't match container names |
| `--region` | no | AWS region for `--auto-provision` |
| `--bucket` | no | Reuse an existing bucket for `--auto-provision` |
| `--dry-run` | no | Preview, no changes |
| `--debug` | no | Debug logging |

Behavior:
- With no `--indices`, discovery backs up the whole cluster: all regular hot indices **plus all data
  streams** (fetched from `_data_stream` and captured by name). It excludes names starting with `_`/`.`,
  the raw `.ds-*` backing indices (the data stream is captured by name instead), and `remote_snapshot`
  (searchable) indices. Searchable indices are recorded in the config under `searchable_snapshots` for
  restore-time remap. Pass `--indices "..."` only to restrict the backup to a specific set.
- Snapshot request body sets `ignore_unavailable: true`, `include_global_state: true`, `partial: false`.
  Because `include_global_state` is **true**, the snapshot also carries global cluster state - composable
  and component **index templates** (so data streams restore without pre-creating their template), plus
  ingest pipelines, ISM/SM policies, and persistent settings.
- Async: returns immediately; verify via `status`.

## provision
`snapexe-oss provision` - phase 1 of the manual s3 flow (mints bucket + IAM user + keys, writes config,
prints the keystore install + `_nodes/reload_secure_settings` steps). Creates real AWS resources.
Flags: `--tag` (yes), `--endpoint` (yes), `--bucket`, `--region`, `--debug`. Needs no OpenSearch creds -
it makes only AWS calls, using the boto3 default credential chain (`--creds-file` is accepted but unused).

## status
`snapexe-oss status --tag <tag> [--endpoint <override>] [--creds-file <f>] [--secret-id <name>]` - reads
`snapexe-{tag}-config.json`, prints overall percent + per-index shard progress. States: `IN_PROGRESS`,
`SUCCESS`, `PARTIAL`, `FAILED`.

## restore
`snapexe-oss restore` (→ `restore.py`)

| Flag | Required | Purpose |
|---|---|---|
| `--tag` | yes | Locates `snapexe-{tag}-config.json` |
| `--endpoint` | yes | TARGET cluster URL to restore into |
| `--creds-file` | no | OpenSearch creds file (uses the `opensearch_dest` block) |
| `--secret-id` | no | AWS Secrets Manager secret with OpenSearch creds; overrides `--creds-file` |
| `--indices` | no | Comma-separated indices to restore verbatim (skips discovery/filter) |
| `--install-container` | no | s3 only: on this dest container, install repo-s3 if missing, mint+install a key, reload; also required for data-stream restore (superadmin cert) |
| `--dest-containers` | no | Comma-separated dest containers for searchable-snapshot remaps (defaults to `--install-container`) |
| `--dry-run` | no | Preview, no changes |
| `--debug` | no | Debug logging |

Behavior:
- Regular indices restore over basic auth; existing/system indices are skipped by default.
- If the snapshot has a **data stream**, restore routes it through the destination node's superadmin
  certificate (`config/kirk.pem`) with `include_global_state: true`. Requires `--install-container`.
- If the config recorded **searchable snapshots**, restore recreates each on the target by registering
  the same backing repo (reusing the dest's existing S3 creds, minting+installing only if needed) and
  doing a `remote_snapshot` restore. Requires a warm/search-role node on the dest.

## delete-all
`snapexe-oss delete-all --tag <tag> [--dry-run] [--yes] [--creds-file <f>] [--secret-id <name>] [--debug]`
- Deletes snapshots, deregisters the repo (on the config's endpoint), empties+deletes the S3 bucket,
  deletes the IAM user (keys + inline policy), removes the config file.
- Idempotent ("already gone" = success). Keeps the config file if any step failed, so re-run works.
- `--dry-run` previews; default run prompts `yes/no`; `--yes` skips the prompt (automation only).

## Credentials

**OpenSearch (username/password)** - resolved from a file *or* Secrets Manager:
- **File (default):** `snapexe-creds.json` (gitignored; override with `--creds-file`):
  ```json
  {
    "opensearch_source": { "username": "admin", "password": "..." },
    "opensearch_dest":   { "username": "admin", "password": "..." }
  }
  ```
- **Secrets Manager:** pass `--secret-id <name>` (to `snapshot`/`status`/`restore`/`delete-all`); it
  takes precedence over the file. The secret's `SecretString` has the **same JSON shape** as the file
  above (the two `opensearch_*` blocks). It is read via the AWS credential chain below.

**AWS** - always the boto3 default credential chain (SSO, assumed role, or instance profile). There is
**no `aws` block** in the creds file and the tool never reads static AWS keys from disk. On a laptop,
wire temporary creds (e.g. Isengard) as a refreshable `credential_process` in `~/.aws/config` so
long-running work survives token expiry.

Never print the creds file or any Secrets Manager values.

## Edge cases & gotchas
- **Run location:** config/creds are CWD-relative - always run from the repo dir.
- **New IAM key propagation:** just-minted keys take a few seconds to work; repo registration retries
  the transient `InvalidAccessKeyId` / "does not exist in our records" error automatically (~4×5s).
- **2-key IAM limit:** each `--auto-provision`/restore key-mint counts against IAM's 2-key-per-user
  limit. A full snapshot(source)+restore(dest) cycle uses both; a 3rd mint fails with `LimitExceeded`
  until `delete-all` (or a manual key delete) frees one. Restore reuses existing dest keys when they
  already work, avoiding a needless mint.
- **repository-s3 plugin (auto-discovered):** not in the base OpenSearch image. With `--auto-provision`
  the tool checks the plugin *per node* via `_cat/plugins` and installs + restarts every node that lacks
  it (mapping node name -> same-named container, falling back to `--install-container`), then re-verifies -
  single- and multi-node both handled. A node missing the plugin that can't be mapped to a running
  container is reported as an error to fix manually. (This replaces the old "any node has it" check, which
  silently skipped nodes where the plugin was absent.)
- **Multi-node keystore keys (auto-discovered):** repo registration verifies the repo on *every* node, so
  all nodes need the S3 keys. With `--auto-provision` the tool queries `_cat/nodes` and installs the keys
  on each node's same-named docker container (single- and multi-node both handled from one
  `--install-container` anchor), then does a single cluster-wide `reload_secure_settings`. Nodes it can't
  map to a running container are logged as a warning; override the list with `--source-containers
  <node1,node2,...>` when node names differ from container names. (Restore has the equivalent
  `--dest-containers`.) An un-keyed node fails verification with "The AWS Access Key Id you provided does
  not exist in our records".
- **Data-stream index templates travel with the snapshot.** The snapshot uses
  `include_global_state: true`, and the data-stream restore path also uses `include_global_state: true`
  (via the superadmin cert), so the composable index template is captured and recreated on the target
  automatically - no need to pre-create it. (Restoring global state also applies the snapshot's pipelines/
  ISM/SM/persistent settings to the target, so restore onto a fresh/recovery cluster, not a shared one.)
- **Data-stream restore is blocked over basic auth** by the security plugin's write-privilege check
  (`no permissions for []`); that's why restore uses the superadmin cert path via `--install-container`.
- **Searchable snapshots can't be re-snapshotted** - their data already lives in S3. Migration re-points
  the dest at the same backing snapshot (Approach A); the dest reads the SAME S3 objects (nothing copied),
  so that backing snapshot/bucket must stay available and accessible to the dest.
- **delete-all only deregisters on the config endpoint** (usually the source). If a repo was also
  registered on a destination cluster, remove that registration separately.

## What a snapshot carries (and what it does NOT)
A snapexe-oss snapshot moves index data, data streams (by name) + searchable-snapshot pointers, and -
because `include_global_state: true` - the cluster global state: composable/component **index templates**,
**ingest pipelines**, **ISM/SM policies**, stored scripts, and persistent cluster settings. Global state is
restored only on the data-stream restore path (`include_global_state: true` via the superadmin cert), so a
plain regular-index restore still won't apply it.

It does **not** carry **security config** (users/roles) or **Dashboards saved objects** - those live in
system indices, not global state, and must be migrated via the security API / saved-objects export. Mention
this when a user expects a "full" cluster clone.
