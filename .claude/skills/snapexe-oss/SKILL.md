---
name: snapexe-oss
description: >-
  Take, restore, and tear down OpenSearch snapshots with the snapexe-oss tool
  (opensearch_snapshot.py / restore.py / delete_all.py). Use this whenever the user wants to
  snapshot or back up an OpenSearch cluster to S3, restore or migrate indices / data streams /
  searchable snapshots from one OpenSearch cluster to another, check snapshot progress, or
  delete/clean up a snapshot's AWS + repository resources - even if they don't name snapexe-oss
  explicitly (e.g. "back up my cluster", "restore these indices to the new cluster", "migrate
  the data stream to dest", "how far along is the snapshot", "tear down the snapshot resources").
  Drives the tool's CLI, confirms before destructive or AWS-creating actions, and reports concise
  status instead of raw logs.
---

# snapexe-oss: OpenSearch snapshot, restore & teardown

`snapexe-oss` drives three operations against self-managed OpenSearch clusters that use an S3
snapshot repository. Your job with this skill is to translate a natural-language request into the
right command, run it, enforce the safety rules, and report a short, honest summary - not raw logs.

- **snapshot** - back up hot indices and data streams to S3. Searchable-snapshot
  (`remote_snapshot`) indices are auto-skipped; their remap info is recorded for restore.
- **restore** - restore into a target cluster: regular indices, data streams (via the node's
  superadmin cert), and searchable snapshots (auto-remapped to the same S3 backing snapshot).
- **delete-all** - tear down everything a tag created: snapshots, repository registration, the S3
  bucket, the IAM user + keys + policy, and the local config file.

## Environment & prerequisites

- **Run from the snapexe-oss repo directory** - the folder containing `opensearch_snapshot.py`,
  `restore.py`, `delete_all.py`. On this machine that is `/Users/mkbn/Public/hackathon`. Config and
  credential files are read/written relative to the current directory, so always `cd` there first.
- **Invoke the tool** with the `snapexe-oss` command if it's on PATH; otherwise run the scripts with
  the repo's virtualenv Python: `.venv/bin/python opensearch_snapshot.py ...`,
  `.venv/bin/python restore.py ...`, `.venv/bin/python delete_all.py ...`. Both forms are equivalent.
- **OpenSearch credentials** come from either a JSON file (default `snapexe-creds.json`, working dir;
  override with `--creds-file`) or **AWS Secrets Manager** via `--secret-id <name>` (which takes
  precedence when set). Both use the same shape: an `opensearch_source` block (snapshot/status read this)
  and an `opensearch_dest` block (restore reads this). If a needed block/secret is missing the command
  errors clearly.
- **AWS credentials** always come from the boto3 default credential chain (SSO, assumed role, or instance
  profile) - there is no `aws` block in the creds file. Confirm identity with `aws sts get-caller-identity`
  if unsure.
- **Every operation is keyed by `--tag`.** The tag names the resources (`snapexe-{tag}-*` bucket, repo,
  IAM user) and the config file `snapexe-{tag}-config.json`. Reuse the same tag across the lifecycle
  (snapshot → status → restore → delete-all). If the user doesn't give a tag, ask for one or propose a
  short descriptive default.

## Safety rules (read first - these are non-negotiable)

These commands create real, billable AWS resources and can delete data, so treat them like production
actions:

1. **Destructive ops need a dry-run + explicit confirmation.** For `delete-all`, ALWAYS run `--dry-run`
   first, show the user the preview of what will be deleted, and get an explicit "yes" before the real
   run. Do not pass `--yes` (skip-prompt) unless the user has clearly authorized skipping confirmation.
2. **AWS-creating ops need confirmation.** `snapshot --auto-provision` and `provision` mint a real S3
   bucket + IAM user + access keys via the user's AWS credentials. Say so and confirm the user wants
   real AWS resources created, unless they've already made that clear this turn.
3. **Never echo secrets.** Don't print `snapexe-creds.json` contents, Secrets Manager values, passwords,
   or access keys. The tool never logs them; you shouldn't either.
4. **Report outcomes faithfully.** Check the exit code and the printed result. If a command fails or
   logs an error, say so with the key error line - never claim a snapshot/restore succeeded that you
   didn't verify (for snapshots, confirm via `status` that state is `SUCCESS`).
5. **Confirm the target when it's outward-facing.** For restore and delete-all, make sure `--endpoint`
   / `--tag` point at the intended cluster. If you can't tell whether a cluster is production, assume it
   is and ask.

## Output discipline (limited logs)

The tools emit INFO logs and progress lines. Do **not** paste raw multi-line logs back to the user.
Run the command, read its output, and report a **one-to-three-line summary**: what happened, the key
result, and the next step. Surface a warning or error only when it changes the outcome.

Good summaries:
- `Snapshot started (tag=prod): 5 indices, skipped 1 searchable. Track with status.`
- `Snapshot state: SUCCESS - 5/5 shards.`
- `Restored to dest: logs, orders, users (100 each) + logs-searchable remapped to the warm node.`
- `delete-all (tag=prod): dry run shows bucket snapexe-prod-a1b2, IAM user snapexe-prod-user, repo, config. Confirm to delete.`

## Workflows

### 1. Snapshot (back up a cluster)

```bash
# Cluster whose repo/keys are already set up (most common):
snapexe-oss snapshot --tag <tag> --endpoint <source-url> --repo-type s3

# From nothing (single- OR multi-node): provision S3 + install plugin/keys, then snapshot. The tool
# auto-discovers every cluster node and keys each one - just pass one reachable node as the anchor:
snapexe-oss snapshot --tag <tag> --endpoint <source-url> --repo-type s3 \
  --auto-provision --install-container <a-source-node-container>

# Override discovery only when node names != container names:
#   ... --auto-provision --install-container <node1> --source-containers <node1,node2,...>
```

- With no `--indices`, discovery backs up the **whole cluster**: all regular hot indices **and all data
  streams** (by name). It skips system indices, raw `.ds-*` backing indices (the data stream is captured
  by name instead), and searchable-snapshot (`remote_snapshot`) indices. Pass `--indices "..."` only to
  restrict the backup to a specific set.
- `--auto-provision --install-container <node>` is the "from nothing" path: it provisions the bucket +
  IAM user + keys, installs `repository-s3` on any node missing it (restarting those nodes), installs the
  keystore keys on every discovered node, reloads, then snapshots. Both the plugin check and the key
  install are per-node, so single- and multi-node clusters work from one anchor. It creates real AWS
  resources → apply safety rule #2.
- **Multi-node source (automatic):** repo verification runs on every node, so all nodes need the S3 keys.
  With `--auto-provision` the tool auto-discovers the nodes (mapping each cluster node name to a same-named
  docker container) and keys each one before the single cluster-wide reload - single- and multi-node both
  work from one `--install-container` anchor. It warns about any node it can't map to a running container;
  for those (or when node names != container names) pass `--source-containers <node1,node2,...>` to
  override discovery. (Restore's equivalent is `--dest-containers`.) An un-keyed node fails verification
  with "The AWS Access Key Id you provided does not exist in our records".
- `--dry-run` previews without changes. For `fs` repositories use `--repo-type fs --repo-path <dir>`.
- Snapshot is asynchronous. After it starts, run **status** to confirm completion.

### 2. Status (progress)

```bash
snapexe-oss status --tag <tag>
```
Report `state` and overall percent (e.g. `SUCCESS - 5/5 shards`). Poll again if it's `IN_PROGRESS`.

### 3. Restore (into a target/destination cluster)

```bash
# Regular indices only (dest already has repo access):
snapexe-oss restore --tag <tag> --endpoint <dest-url>

# Full path (data streams + searchable snapshots), single-node dest reachable via docker:
snapexe-oss restore --tag <tag> --endpoint <dest-url> \
  --install-container <dest-node> --dest-containers <dest-node1,dest-node2>
```

- Default restore brings back non-existing, non-system indices (skips ones already on the target).
  Use `--indices "..."` to restore a specific set.
- **Data streams** in the snapshot are restored via the destination node's superadmin certificate
  (requires `--install-container`) with `include_global_state: true`, so the data stream's **index
  template travels in the snapshot and is recreated on the target automatically** - no need to pre-create
  it. (Global state also carries pipelines/ISM/SM/persistent settings, so restore onto a fresh/recovery
  cluster, not a shared one.) See `references/commands.md`.
- **Searchable snapshots** recorded at snapshot time are auto-recreated on the target by re-pointing at
  the same S3 backing snapshot (`--dest-containers` names the dest nodes for key install). Dest needs a
  warm/search-role node.
- Restore is fire-and-forget for regular indices; the tool prints a `_cat/recovery` curl to monitor.

### 4. Delete-all (tear down a tag's resources)

```bash
snapexe-oss delete-all --tag <tag> --dry-run     # 1. preview - ALWAYS do this first
snapexe-oss delete-all --tag <tag>               # 2. after user confirms (prompts yes/no)
```
Deletes the repository's snapshots, deregisters the repo (on the config's endpoint), empties + deletes
the S3 bucket, deletes the IAM user (keys + inline policy), and removes the config file. It is
idempotent and keeps the config file if any step fails so it can be re-run. Note: it deregisters the
repo only on the config's endpoint - if the same repo was also registered on a **destination** cluster,
that registration must be removed separately.

## Detailed flag reference & edge cases

For the complete per-command flag list and the important edge cases - data streams needing a template on
the target and the superadmin-cert path, searchable-snapshot remap mechanics, multi-node key
installation, the S3 new-key propagation retry, the 2-key IAM limit, and what ISM/SM/security config a
snapshot does *not* carry - read `references/commands.md`.
