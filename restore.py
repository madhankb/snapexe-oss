"""Restore tool for open-source OpenSearch - restore a hot-index snapshot into a target cluster.

Standalone script (python restore.py --tag ...). It reads the config written by the
snapshot tool (snapexe-{tag}-config.json), registers the repository on the target
cluster named by --endpoint, and fires an asynchronous _restore. Fire-and-forget: it
prints the _cat/recovery command to monitor progress.

If the snapshot contains a data stream, restore routes it through the node's superadmin
certificate (via --install-container) with include_global_state=true, since the security
plugin rejects data-stream restore over basic auth.

If the source cluster had searchable-snapshot (remote_snapshot) indices, the snapshot
config records their backing (repo, snapshot, bucket). Restore then recreates each one on
the target by registering the same backing repo on dest and issuing a remote_snapshot
restore - Approach A: dest reads the SAME S3 objects, no data copied. Provide the target
container(s) via --dest-containers (defaults to --install-container).
"""

import argparse
import logging
import subprocess
import sys
import json
import requests
import urllib3
from urllib.parse import urljoin, urlparse

# Local clusters use self-signed certs and the tool calls verify=False; silence the noise.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from opensearch_snapshot import (
    apply_aws_creds_from_file,
    build_repository_body,
    create_iam_user_with_keys,
    ensure_repository_s3,
    install_keystore_key,
    register_repository,
    reload_secure_settings,
)

logger = logging.getLogger(__name__)

DEFAULT_CREDS_FILE = "snapexe-creds.json"


def normalize_endpoint(url):
    if not url:
        raise ValueError("Endpoint URL cannot be empty")
    url = url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    if not urlparse(url).netloc:
        raise ValueError(f"Invalid endpoint URL: {url}")
    return url


def load_creds_file(path):
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Credentials file {path} not found. Create it (see snapexe-creds.example.json) "
            "or pass --creds-file PATH."
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in credentials file {path}: {exc}")
    if not isinstance(data, dict):
        raise ValueError(f"Credentials file {path} must contain a JSON object")
    return data


def resolve_credentials(file_creds, section):
    creds = file_creds.get(section, {})
    username = creds.get("username")
    password = creds.get("password")
    if not username:
        raise ValueError(f"Credentials file missing {section}.username")
    if not password:
        raise ValueError(f"Credentials file missing {section}.password")
    return username, password


def create_session(username, password):
    session = requests.Session()
    session.auth = (username, password)
    return session


def config_path(tag):
    return f"snapexe-{tag}-config.json"


def load_config(tag):
    path = config_path(tag)
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Config {path} not found. Run: snapshot --tag {tag} ... first"
        )


def build_repository_body(repo_type, *, location=None, bucket=None, base_path=None, region=None):
    if repo_type == "fs":
        if not location:
            raise ValueError("fs repository requires a location")
        return {"type": "fs", "settings": {"location": location}}
    if repo_type == "s3":
        if not bucket:
            raise ValueError("s3 repository requires a bucket")
        settings = {"bucket": bucket, "base_path": base_path}
        if region:
            settings["region"] = region
        return {"type": "s3", "settings": settings}
    raise ValueError(f"Unknown repository type: {repo_type}")


def get_existing_indices(session, endpoint):
    try:
        url = urljoin(endpoint + "/", "_cat/indices?format=json&h=index")
        resp = session.get(url, verify=False, timeout=30)
        if resp.status_code != 200:
            logger.warning("Could not list existing indices: %s", resp.status_code)
            return []
        return [row.get("index", "") for row in resp.json()]
    except Exception as exc:
        logger.warning("Error getting existing indices: %s", exc)
        return []


def get_snapshot_info(session, endpoint, repository, snapshot):
    """Return the snapshot's metadata dict (has `indices` and `data_streams`), or None."""
    try:
        url = urljoin(endpoint + "/", f"_snapshot/{repository}/{snapshot}")
        resp = session.get(url, verify=False, timeout=30)
        if resp.status_code != 200:
            logger.error("Failed to query snapshot: %s %s", resp.status_code, resp.text)
            return None
        snapshots = resp.json().get("snapshots", [])
        if not snapshots:
            logger.error("No snapshot data found for %s/%s", repository, snapshot)
            return None
        return snapshots[0]
    except Exception as exc:
        logger.error("Error getting snapshot info: %s", exc)
        return None


def get_snapshot_indices(session, endpoint, repository, snapshot):
    info = get_snapshot_info(session, endpoint, repository, snapshot)
    return info.get("indices", []) if info else []


def filter_restorable_indices(snapshot_indices, existing_indices):
    existing = set(existing_indices)
    restorable = []
    for name in snapshot_indices:
        if name.startswith(".") and not name.startswith(".ds-"):
            continue
        if name in existing:
            continue
        restorable.append(name)
    return restorable


def restore_snapshot(session, endpoint, repository, snapshot, indices):
    if not indices:
        logger.error("No indices to restore")
        return False
    try:
        url = urljoin(endpoint + "/", f"_snapshot/{repository}/{snapshot}/_restore")
        body = {
            "ignore_unavailable": True,
            "include_global_state": False,
            "indices": ",".join(indices),
        }
        resp = session.post(url, json=body, verify=False, timeout=60)
        if resp.status_code == 200:
            logger.info("Restore started: %s/%s", repository, snapshot)
            return True
        logger.error("Failed to restore: %s %s", resp.status_code, resp.text)
        return False
    except Exception as exc:
        logger.error("Exception restoring snapshot: %s", exc)
        return False


def _restore_repo_body(config):
    if config["repo_type"] == "fs":
        return build_repository_body("fs", location=config["repo_path"])
    return build_repository_body(
        "s3",
        bucket=config["bucket"],
        base_path=config["repository"],
        region=config.get("region"),
    )


def ingest_dest_keystore(config, session, endpoint, container, boto3_module=None):
    """Mint a fresh access key for the tag's IAM user, install it into the destination
    node's keystore, and reload secure settings - the restore-side mirror of
    opensearch_snapshot.py's --auto-provision keystore step. Returns True on success.

    The secret minted at provision time is never persisted, so a new key is minted here
    (IAM allows two per user; delete an old one if you hit the limit).
    """
    from botocore.exceptions import ClientError

    if boto3_module is None:
        import boto3 as boto3_module
    tag = config.get("tag")
    try:
        keys = create_iam_user_with_keys(
            boto3_module.client("iam"), f"snapexe-{tag}-user", config.get("bucket")
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "LimitExceeded":
            logger.error(
                "IAM user snapexe-%s-user already has the maximum number of access keys. "
                "Delete an unused key and retry.", tag,
            )
        else:
            logger.error("Failed to mint destination access key: %s", exc)
        return False
    try:
        install_keystore_key(container, "s3.client.default.access_key", keys["access_key_id"])
        install_keystore_key(container, "s3.client.default.secret_key", keys["secret_access_key"])
    except RuntimeError as exc:
        logger.error(str(exc))
        return False
    return reload_secure_settings(session, endpoint)


def restore_via_admin_cert(container, repository, snapshot, indices,
                           cert="config/kirk.pem", key="config/kirk-key.pem"):
    """Restore using the node's superadmin client certificate (docker exec + mTLS).

    Data-stream restore is rejected over basic auth by the security plugin's
    snapshot-restore write-privilege check; the admin cert (admin_dn) bypasses
    authorization. Uses include_global_state=true so the data stream's template is
    restored too. Synchronous. Single-node local use.
    """
    body = json.dumps({
        "indices": ",".join(indices),
        "include_global_state": True,
        "ignore_unavailable": True,
    })
    url = f"https://localhost:9200/_snapshot/{repository}/{snapshot}/_restore?wait_for_completion=true"
    result = subprocess.run(
        ["docker", "exec", container, "curl", "-sk", "--cert", cert, "--key", key,
         "-X", "POST", url, "-H", "Content-Type: application/json", "-d", body],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        logger.error("Admin-cert restore could not run: %s", (result.stderr or "").strip())
        return False
    out = (result.stdout or "").strip()
    try:
        payload = json.loads(out)
    except ValueError:
        logger.error("Admin-cert restore: unexpected response: %s", out[:300])
        return False
    if "snapshot" in payload:
        logger.info("Data-stream restore completed via superadmin cert: %s/%s", repository, snapshot)
        return True
    logger.error("Admin-cert restore failed: %s", out[:400])
    return False


def _ensure_backing_repo(m, session, endpoint, dest_containers, boto3_module):
    """Register the backing repo on the target for a searchable remap. Tries the dest's
    existing S3 credentials first; only mints+installs a key for the backing IAM user if
    that fails (avoids needless minting and the 2-key IAM limit). Returns True on success.
    """
    from botocore.exceptions import ClientError

    body = build_repository_body(
        "s3", bucket=m["bucket"], base_path=m.get("base_path"), region=m.get("region"),
    )
    backing_repo = m["backing_repository"]
    # 1. Probe: register with whatever credentials the destination already has (no retry).
    if register_repository(session, endpoint, backing_repo, body, retries=0):
        return True
    # 2. Not accessible - need to install the backing bucket's key on the dest node(s).
    if not dest_containers:
        logger.error(
            "Backing repo '%s' is not accessible with the destination's current credentials "
            "and no --dest-containers/--install-container was given to install its key.",
            backing_repo,
        )
        return False
    # Backing IAM user follows snapexe-{tag}-user, with tag embedded in the repo name
    # (generate_repository_name -> snapexe-{tag}-repo-{suffix}).
    parts = backing_repo.split("-repo-")
    if len(parts) != 2 or not parts[0].startswith("snapexe-"):
        logger.error("Cannot infer IAM user from backing repo name '%s'", backing_repo)
        return False
    user_name = f"snapexe-{parts[0][len('snapexe-'):]}-user"
    if boto3_module is None:
        import boto3 as boto3_module
    try:
        keys = create_iam_user_with_keys(boto3_module.client("iam"), user_name, m["bucket"])
    except ClientError as exc:
        logger.error("Could not mint key for backing user %s: %s", user_name, exc)
        return False
    try:
        for c in dest_containers:
            install_keystore_key(c, "s3.client.default.access_key", keys["access_key_id"])
            install_keystore_key(c, "s3.client.default.secret_key", keys["secret_access_key"])
    except RuntimeError as exc:
        logger.error("Keystore install failed for backing repo '%s': %s", backing_repo, exc)
        return False
    if not reload_secure_settings(session, endpoint):
        return False
    return register_repository(session, endpoint, backing_repo, body)


def remap_searchable_snapshots(remaps, session, endpoint, dest_containers,
                               existing_indices, boto3_module=None):
    """Recreate each searchable-snapshot index on the target by pointing at its backing
    (repo, snapshot) in S3 - Approach A: dest shares the same bucket, zero data copied.

    For each remap: ensure the backing repo is registered on dest (reusing existing dest
    credentials, or minting+installing the backing IAM user's key if needed), then
    remote_snapshot-restore the source index (renamed back to the searchable index name).

    Skips remaps whose target name already exists. Returns True if every remap succeeded
    or was correctly skipped.
    """
    if not remaps:
        return True
    existing = set(existing_indices)
    all_ok = True
    for m in remaps:
        target = m["name"]
        if target in existing:
            logger.info("Searchable remap '%s' already exists on target - skipping", target)
            continue
        if any(not m.get(k) for k in ("backing_repository", "backing_snapshot", "source_index", "bucket")):
            logger.error("Searchable remap for '%s' missing required fields; skipping", target)
            all_ok = False
            continue
        if not _ensure_backing_repo(m, session, endpoint, dest_containers, boto3_module):
            all_ok = False
            continue
        url = urljoin(
            endpoint + "/",
            f"_snapshot/{m['backing_repository']}/{m['backing_snapshot']}/_restore?wait_for_completion=true",
        )
        rb = {
            "storage_type": "remote_snapshot",
            "indices": m["source_index"],
            "rename_pattern": m["source_index"],
            "rename_replacement": target,
        }
        r = session.post(url, json=rb, verify=False, timeout=180)
        if r.status_code == 200 and "snapshot" in r.json():
            logger.info("Searchable remap complete: %s -> %s (from %s/%s)",
                        m["source_index"], target, m["backing_repository"], m["backing_snapshot"])
        else:
            logger.error("Searchable remap failed for '%s': %s %s", target, r.status_code, r.text[:200])
            all_ok = False
    return all_ok


def run_restore(args, *, session_factory=create_session, boto3_module=None):
    try:
        config = load_config(args.tag)
    except FileNotFoundError as exc:
        logger.error(str(exc))
        return 2
    try:
        file_creds = load_creds_file(args.creds_file)
        username, password = resolve_credentials(file_creds, "opensearch_dest")
    except (FileNotFoundError, ValueError) as exc:
        logger.error(str(exc))
        return 2

    endpoint = normalize_endpoint(args.endpoint)
    repository = config["repository"]
    snapshot_name = config["snapshot_name"]
    session = session_factory(username, password)
    install_container = getattr(args, "install_container", None)

    if args.dry_run:
        if install_container:
            logger.info(
                "[DRY RUN] Would validate/install repository-s3 in '%s', mint an access key "
                "for snapexe-%s-user, install it, and reload secure settings",
                install_container, config.get("tag"),
            )
        logger.info(
            "[DRY RUN] Would register %s repository %s on %s and restore snapshot %s",
            config["repo_type"], repository, endpoint, snapshot_name,
        )
        return 0

    if config.get("repo_type") == "s3":
        if not ensure_repository_s3(session, endpoint, install_container):
            return 1

    if install_container:
        if config.get("repo_type") != "s3":
            logger.error("--install-container only applies to s3 snapshots")
            return 2
        apply_aws_creds_from_file(file_creds)
        if not ingest_dest_keystore(config, session, endpoint, install_container, boto3_module):
            logger.error("Destination keystore ingest failed")
            return 1

    body = _restore_repo_body(config)
    if not register_repository(session, endpoint, repository, body):
        logger.error(
            "Repository registration failed on target. For s3, ensure keystore keys are "
            "installed on the target nodes and secure settings reloaded; for fs, ensure "
            "the path is in path.repo on the target and the node restarted."
        )
        return 1

    info = get_snapshot_info(session, endpoint, repository, snapshot_name)
    if info is None:
        logger.error("Could not retrieve snapshot %s", snapshot_name)
        return 1
    data_streams = info.get("data_streams", [])
    snapshot_indices = info.get("indices", [])

    if args.indices:
        targets = [i.strip() for i in args.indices.split(",") if i.strip()]
    else:
        existing = get_existing_indices(session, endpoint)
        # Data streams are restored by name (they bring their backing indices); exclude
        # raw .ds-* backing indices from the plain-index list.
        regular = [i for i in snapshot_indices if not i.startswith(".ds-")]
        targets = data_streams + filter_restorable_indices(regular, existing)
        if not targets:
            print(
                f"{len(snapshot_indices)} index(es) from snapshot already exist on target "
                "(or are system indices) - nothing to restore"
            )
            return 0

    if data_streams:
        # The security plugin rejects data-stream restore over basic auth, so use the
        # node's superadmin certificate (via --install-container), with global state.
        if not install_container:
            logger.error(
                "Snapshot contains data stream(s): %s. Restoring a data stream requires the "
                "OpenSearch superadmin certificate (the security plugin rejects it over basic "
                "auth). Re-run with --install-container <target-container> so the restore can "
                "use the node's admin cert (config/kirk.pem).", ", ".join(data_streams),
            )
            return 1
        if not restore_via_admin_cert(install_container, repository, snapshot_name, targets):
            return 1
        print(f"\nData-stream restore completed via superadmin cert: {repository}/{snapshot_name} -> {endpoint}")
        print(f"Restored: {', '.join(targets)}")
        remaps = config.get("searchable_snapshots") or []
        dest_containers = _resolve_dest_containers(args)
        if remaps and not remap_searchable_snapshots(
            remaps, session, endpoint, dest_containers,
            get_existing_indices(session, endpoint), boto3_module,
        ):
            return 1
        return 0

    if not restore_snapshot(session, endpoint, repository, snapshot_name, targets):
        return 1

    print(f"\nRestore started: {repository}/{snapshot_name} -> {endpoint}")
    print(f"Indices: {len(targets)}")
    remaps = config.get("searchable_snapshots") or []
    if remaps:
        dest_containers = _resolve_dest_containers(args)
        if not remap_searchable_snapshots(
            remaps, session, endpoint, dest_containers,
            get_existing_indices(session, endpoint), boto3_module,
        ):
            return 1
        print(f"Searchable remaps completed: {len(remaps)}")
    print(
        f"Monitor recovery with (reads the dest password from {args.creds_file}):\n"
        f"  PW=$(python3 -c \"import json;print(json.load(open('{args.creds_file}'))['opensearch_dest']['password'])\")\n"
        f"  curl -ku \"{username}:$PW\" \"{endpoint}/_cat/recovery?v\"\n"
    )
    return 0


def _resolve_dest_containers(args):
    raw = getattr(args, "dest_containers", None) or getattr(args, "install_container", None) or ""
    return [c.strip() for c in raw.split(",") if c.strip()]


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Restore an open-source OpenSearch snapshot into a target cluster."
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--creds-file", dest="creds_file", default=DEFAULT_CREDS_FILE)
    parser.add_argument("--indices")
    parser.add_argument("--install-container", dest="install_container",
                        help="s3 only: on this destination container, install repository-s3 if "
                             "missing, mint a key for the tag's IAM user, install it into the "
                             "keystore, and reload - all before restoring")
    parser.add_argument("--dest-containers", dest="dest_containers",
                        help="Comma-separated destination containers for searchable-snapshot "
                             "remaps (Approach A). Defaults to --install-container.")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_arguments(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "debug", False) else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    return run_restore(args)


if __name__ == "__main__":
    sys.exit(main())
