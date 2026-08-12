# OpenSearch Snapshot Tool (open-source OpenSearch)

Takes an asynchronous snapshot of a self-managed OpenSearch cluster's hot indices
into a filesystem (`fs`) or S3 (`s3`) repository, and reports snapshot progress.

Built for open-source (self-managed) OpenSearch using its public snapshot APIs. It
snapshots only regular ("hot") indices and skips searchable-snapshot indices, which
cannot be re-snapshotted.

## Requirements

- Python 3.8+ (`pip install -r requirements.txt`)
- OpenSearch admin credentials (security plugin enabled, HTTPS)
- For `s3` mode: the `repository-s3` plugin installed on all nodes, plus AWS
  credentials for this tool (standard boto3 credential chain)
- For `fs` mode: a directory listed in `path.repo` in `opensearch.yml` on all nodes

## Credentials

All credentials are read from a JSON file (default `snapexe-creds.json` in the working
directory; override with `--creds-file PATH`). Copy the template and fill it in:

```bash
cp snapexe-creds.example.json snapexe-creds.json
# edit snapexe-creds.json
```

```json
{
  "opensearch_source": { "username": "admin", "password": "..." },
  "opensearch_dest":   { "username": "admin", "password": "..." },
  "aws": { "access_key_id": "...", "secret_access_key": "...", "region": "us-east-1" }
}
```

- `snapshot` and `status` use `opensearch_source` (the source cluster they read from).
- `restore` uses `opensearch_dest` (the destination cluster it restores into).
- `provision` uses the `aws` section (creates the S3 bucket + IAM user via boto3).
- Each command needs only its own block: you can snapshot with just `opensearch_source`
  filled in, and restore with just `opensearch_dest`.
- The file must exist; a command errors clearly if the block it needs is missing.
- `snapexe-creds.json` is gitignored. Credentials are never logged or persisted by the tool.

## Searchable snapshots

Indices whose store type is `remote_snapshot` (searchable snapshot indices) are
detected via the index settings API and excluded automatically - OpenSearch cannot
re-snapshot them.

## Usage

### Filesystem repository (local / on-prem)

`path.repo` must include the target directory in `opensearch.yml`, for example:
```yaml
path.repo: ["/mnt/opensearch-snapshots"]
```
Then:
```bash
python opensearch_snapshot.py snapshot \
  --tag prod \
  --endpoint https://localhost:9200 \
  --repo-type fs \
  --repo-path /mnt/opensearch-snapshots
```

### S3 repository (two-phase)

S3 is a two-phase flow because OpenSearch verifies an s3 repository at registration
using keystore keys that do not exist yet the first time you run the tool. Phase 1
mints those keys (without touching the cluster); after you install them, phase 2
registers the repository and starts the snapshot.

1. Provision the bucket + IAM user (no cluster calls):
   ```bash
   python opensearch_snapshot.py provision \
     --tag prod \
     --endpoint https://opensearch.example.com:9200 \
     --region us-east-1
   ```
   This creates (or reuses) the bucket and an IAM user with scoped access keys, saves
   the stable bucket/region/repository names to `snapexe-prod-config.json`, and prints
   the `opensearch-keystore add` and `POST _nodes/reload_secure_settings` steps. The
   repo type is always `s3` for `provision`.

2. Install the printed keys in the keystore on every node and reload secure settings:
   ```bash
   POST _nodes/reload_secure_settings
   ```

3. Register the repository and take the snapshot (reads the provisioned config; no AWS
   calls):
   ```bash
   python opensearch_snapshot.py snapshot \
     --tag prod \
     --endpoint https://opensearch.example.com:9200 \
     --repo-type s3
   ```

### S3 in one command (single-node, local `docker exec` access)

For a single-node cluster reachable via `docker exec` (e.g. the local test setup),
`--auto-provision` folds all three phases into one command: it provisions the bucket +
IAM user, installs the minted keys into the named container's keystore, reloads secure
settings, then takes the snapshot. This creates real AWS resources.

```bash
python opensearch_snapshot.py snapshot \
  --tag local \
  --endpoint https://localhost:9200 \
  --repo-type s3 \
  --auto-provision \
  --install-container os-source
```

This is a convenience for the local single-node case only: multi-node clusters have no
REST API for the keystore, so each node's keys must be installed by your own
orchestration - use the three-step flow above there.

### Check status

```bash
python opensearch_snapshot.py status --tag prod
```

Reports overall percent-complete and per-index progress from the snapshot `_status` API.

### Restore a snapshot into a target cluster

Restore is fire-and-forget and always targets a cluster you name with `--endpoint`
(typically a new/recovery cluster). The repository name, snapshot name, and backend
settings are read from `snapexe-{tag}-config.json`, so the target must be able to read
the same repository: for `s3`, install the keystore keys on the target nodes (see the
`provision` output) and reload secure settings; for `fs`, the same `path.repo`
directory must be configured on the target.

```bash
# Restore all non-existing, non-system indices from the saved snapshot
python restore.py \
  --tag prod \
  --endpoint https://target-cluster:9200

# Restore only specific indices (restored verbatim, no existing-index filter)
python restore.py \
  --tag prod \
  --endpoint https://target-cluster:9200 \
  --indices "logs,orders"
```

For a single-node s3 target reachable via `docker exec` (e.g. the local `os-dest`),
`--install-container` performs the keystore step for you during the run: it mints a fresh
access key for `snapexe-{tag}-user`, installs it into the named container's keystore,
reloads secure settings, then registers the repo and restores. This creates a new IAM
access key (IAM allows two per user, so delete an old one if you hit the limit).

```bash
python restore.py \
  --tag prod \
  --endpoint https://target-cluster:9200 \
  --install-container os-dest
```

By default, restore skips indices that already exist on the target and system indices
(names starting with `.`, except `.ds-*` datastream backing indices). With `--indices`,
the named list is restored verbatim; if one already exists on the target, OpenSearch
rejects that restore and the error is surfaced.

Restore is asynchronous. Monitor recovery with:
```bash
curl -ku "$OPENSEARCH_USER:$OPENSEARCH_PASSWORD" "https://target-cluster:9200/_cat/recovery?v"
```

## Command reference

`snapshot`:

| Flag | Required | Description |
|---|---|---|
| `--tag` | yes | Resource naming tag |
| `--endpoint` | yes | Cluster URL, e.g. `https://host:9200` |
| `--repo-type` | yes | `fs` or `s3` |
| `--indices` | no | Comma-separated indices; skips discovery |
| `--repository` | no | Custom repository name |
| `--snapshot-name` | no | Custom snapshot name |
| `--repo-path` | fs | Repository directory (must be in `path.repo`) |
| `--auto-provision` | no | s3 only: provision + install keys + reload + snapshot in one command |
| `--install-container` | with `--auto-provision` | Container to install keystore keys into via `docker exec` |
| `--region` | no | AWS region for `--auto-provision` |
| `--bucket` | no | Reuse an existing bucket for `--auto-provision` |
| `--dry-run` | no | Preview without changes |
| `--debug` | no | Debug logging |

`provision` (phase 1 of `s3`, always provisions an `s3` repository):

| Flag | Required | Description |
|---|---|---|
| `--tag` | yes | Resource naming tag |
| `--endpoint` | yes | Cluster URL, e.g. `https://host:9200` |
| `--bucket` | no | Reuse an existing bucket |
| `--region` | no | AWS region (defaults to the boto3 session region) |
| `--debug` | no | Debug logging |

`status`:

| Flag | Required | Description |
|---|---|---|
| `--tag` | yes | Locates `snapexe-{tag}-config.json` |
| `--endpoint` | no | Override the stored endpoint |
| `--debug` | no | Debug logging |

`restore`:

| Flag | Required | Description |
|---|---|---|
| `--tag` | yes | Locates `snapexe-{tag}-config.json` |
| `--endpoint` | yes | TARGET cluster URL to restore into |
| `--indices` | no | Comma-separated indices to restore verbatim (skips discovery/filter) |
| `--install-container` | no | s3 only: mint a key, install it into this target container's keystore, and reload before restoring |
| `--dry-run` | no | Preview without changes |
| `--debug` | no | Debug logging |

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -v
```
