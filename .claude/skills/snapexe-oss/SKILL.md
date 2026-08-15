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
  (snapshot -> status -> restore -> delete-all). If the user doesn't give a tag, ask for one or propose a
  short descriptive default.

## Safety rules (read first - these are non-negotiable)

These commands create real, billable AWS resources and can delete data, so treat them like production
actions:

1. **Destructive ops need a dry-run + explicit confirmation.** For `delete-all`, ALWAYS run `--dry-run`
   first, show the user the preview of what will be deleted, and get an explicit "yes" before the real
   run. Do not pass `--yes` (skip-prompt) unless the user has clearly authorized skipping confirmation.
2. **Backups provision by default - do NOT prompt.** A "back up my cluster" request implies
   provisioning, so `snapshot --auto-provision` (and `provision`) may create a real S3 bucket + IAM user
   + access keys and **proceed without a separate yes/no**. Don't stop to ask - just run it, then list
   exactly what was created in the summary (see Output discipline). Only `delete-all` still needs
   confirmation (rule #1).
3. **Never echo secrets.** Don't print `snapexe-creds.json` contents, Secrets Manager values, passwords,
   or access keys. The tool never logs them; you shouldn't either.
4. **Report outcomes faithfully; don't wait for a snapshot to finish.** Check the exit code and printed
   result; if a command fails, say so with the key error line. A snapshot is asynchronous and can run for
   hours or days - once it's **initiated**, report it as *started* (never claim it "completed" or
   "SUCCESS") and point the user to `status`. Do NOT poll or block waiting for completion.
5. **Confirm the target when it's outward-facing.** For restore and delete-all, make sure `--endpoint`
   / `--tag` point at the intended cluster. If you can't tell whether a cluster is production, assume it
   is and ask.

## Output discipline (natural language, no logs, no emojis)

The user must see **only clean, natural-language output** - never the tool's INFO logs, curl/docker
output, or pre-flight commands. **Do not use emojis or decorative glyphs anywhere in this skill.**

- **Suppress the tool's logs.** Run the CLI with output redirected to a temp file, e.g.
  `snapexe-oss snapshot ... > /tmp/snapexe-<tag>.out 2>&1`, then read that file yourself and present only
  the summary below. Raw INFO/log lines must not appear in the chat.
- **No visible pre-flight.** Don't run separate orienting commands (`ls`, `docker ps`,
  `curl _cat/nodes`, creds/config peeks) as their own steps, and don't narrate ("I'll run a few
  checks..."). Run the single command you need and let the tool orient itself.
- **Hide all plumbing.** Never mention docker containers, node names (os-source-hot/warm), keystore
  keys, plugin installs, reloads, retries, or shard-level internals. Report only what the user cares
  about: what was captured, where it lives, how to check progress. (Never write a line like "keyed both
  source nodes (os-source-hot, os-source-warm)".)
- **Don't claim completion.** Snapshot and restore are asynchronous - report them as *started* and point
  to the progress check; never say "completed"/"SUCCESS" without a live status/recovery query.

Present a short heading and grouped facts - reproduce these templates, plain text only.

Snapshot started (from scratch):

```
Snapshot started - tag "maha"

  Snapshot   snapexe-maha-hotsnapshot-2026-08-14-22-38-44
  Captured   10 indices (incl. the logs-datastream data stream); 1 searchable snapshot remapped for restore

  Created in AWS
    S3 bucket    snapexe-maha-mq19
    IAM user     snapexe-maha-user
    Repository   snapexe-maha-repo-mq19

  Snapshots run in the background and can take a while.
  Check progress:  snapexe-oss status --tag maha
```

Restore started:

```
Restore started - tag "maha" to https://localhost:9201

  Restoring   logs, orders, users + logs-datastream (data stream) + logs-searchable (searchable, remapped)
  Skipped     2 indices already on the target

  Recovery runs in the background. Ask for restore progress to see the heatmap.
```

delete-all - dry-run preview, then the confirmed delete:

```
delete-all (dry run) - tag "maha" - would delete:

  Repository + snapshots     snapexe-maha-repo-mq19
  S3 bucket                  snapexe-maha-mq19
  IAM user (+ keys/policy)   snapexe-maha-user
  Config file                snapexe-maha-config.json

  Confirm to run the real delete.
```
```
Deleted - tag "maha"

  Repository + snapshots         removed
  S3 bucket snapexe-maha-mq19    removed
  IAM user snapexe-maha-user     removed
  Config file                    removed
```

(status and restore progress use the plain ▓/░ heatmap shown in their workflow sections.)

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
  resources - apply safety rule #2.
- **Multi-node source (automatic):** repo verification runs on every node, so all nodes need the S3 keys.
  With `--auto-provision` the tool auto-discovers the nodes (mapping each cluster node name to a same-named
  docker container) and keys each one before the single cluster-wide reload - single- and multi-node both
  work from one `--install-container` anchor. It warns about any node it can't map to a running container;
  for those (or when node names != container names) pass `--source-containers <node1,node2,...>` to
  override discovery. (Restore's equivalent is `--dest-containers`.) An un-keyed node fails verification
  with "The AWS Access Key Id you provided does not exist in our records".
- `--dry-run` previews without changes. For `fs` repositories use `--repo-type fs --repo-path <dir>`.
- Snapshot is asynchronous and may run for hours or days. Once it's **initiated**, stop there - report
  it as started (with the clean summary above) and point the user to `status`. Do NOT wait or poll for
  completion.

### 2. Status (progress)

```bash
snapexe-oss status --tag <tag>
```
**Whenever the user asks about a snapshot's status, run this command live** - it queries the cluster's
`_status` API. Never answer from memory or a previous run; re-run it each time.

Render the result as a **heatmap** - same `▓` (done) / `░` (remaining) bar style as the check-capacity
skill - one row per index. Each bar is 20 chars: `fill = round(done / total * 20)`, `▓` repeated fill
times then `░` for the remaining `20 - fill`. Add the shard count and the plain-text state (SUCCESS /
IN_PROGRESS / FAILED / PENDING - no glyphs). Lead with an overall line + overall bar. Show only this -
no raw logs:

```
Snapshot status - tag "maha" - IN_PROGRESS, 16/20 shards (80%)

Overall  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░  80%

INDEX                         PROGRESS               SHARDS  STATE
logs                          ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓   2/2    SUCCESS
orders                        ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓   2/2    SUCCESS
users                         ▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░   1/2    IN_PROGRESS
.ds-logs-datastream-000001    ░░░░░░░░░░░░░░░░░░░░   0/1    PENDING
```

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
- Restore is asynchronous (fire-and-forget) - like a snapshot, report it as *started*, don't wait for
  recovery to finish.

**Restore progress uses the same heatmap.** Whenever the user asks how a restore is going, query the
dest's recovery API live and render the **same `▓`/`░` heatmap** as status - one row per index, bar from
the recovery percent (`fill = round(bytes_percent / 100 * 20)`), plain-text state (done / recovering /
pending - no glyphs):

```bash
curl -sku "<dest-creds>" "<dest-url>/_cat/recovery?v&active_only=false&format=json&h=index,stage,bytes_percent"
```
Aggregate shards to one bar per index (use the min bytes_percent across an index's shards; `stage=done`
means 100%). Lead with an overall line, e.g.:

```
Restore progress - tag "maha" to https://localhost:9201 - 3/5 indices done

INDEX                         RECOVERY               %      STATE
logs                          ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  100%   done
users                         ▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░   67%   recovering
orders                        ░░░░░░░░░░░░░░░░░░░░    0%   pending
```

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
