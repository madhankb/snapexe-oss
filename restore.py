"""Restore tool for open-source OpenSearch - restore a hot-index snapshot into a target cluster.

Standalone script (python restore.py --tag ...). It reads the config written by the
snapshot tool (snapexe-{tag}-config.json), registers the repository on the target
cluster named by --endpoint, and fires an asynchronous _restore. Fire-and-forget: it
prints the _cat/recovery command to monitor progress.
"""

import argparse
import logging
import sys
import json
import requests
from urllib.parse import urljoin, urlparse

from opensearch_snapshot import (
    apply_aws_creds_from_file,
    create_iam_user_with_keys,
    install_keystore_key,
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


def register_repository(session, endpoint, repository, body):
    try:
        url = urljoin(endpoint + "/", f"_snapshot/{repository}")
        resp = session.put(url, json=body, verify=False, timeout=60)
        if resp.status_code == 200 and resp.json().get("acknowledged"):
            logger.info("Repository registered: %s", repository)
            return True
        logger.error("Failed to register repository: %s %s", resp.status_code, resp.text)
        return False
    except Exception as exc:
        logger.error("Exception registering repository: %s", exc)
        return False


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


def get_snapshot_indices(session, endpoint, repository, snapshot):
    try:
        url = urljoin(endpoint + "/", f"_snapshot/{repository}/{snapshot}")
        resp = session.get(url, verify=False, timeout=30)
        if resp.status_code != 200:
            logger.error("Failed to query snapshot: %s %s", resp.status_code, resp.text)
            return []
        snapshots = resp.json().get("snapshots", [])
        if not snapshots:
            logger.error("No snapshot data found for %s/%s", repository, snapshot)
            return []
        return snapshots[0].get("indices", [])
    except Exception as exc:
        logger.error("Error getting snapshot indices: %s", exc)
        return []


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
                "[DRY RUN] Would mint an access key for snapexe-%s-user, install it into "
                "'%s', and reload secure settings", config.get("tag"), install_container,
            )
        logger.info(
            "[DRY RUN] Would register %s repository %s on %s and restore snapshot %s",
            config["repo_type"], repository, endpoint, snapshot_name,
        )
        return 0

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

    if args.indices:
        indices = [i.strip() for i in args.indices.split(",") if i.strip()]
    else:
        snapshot_indices = get_snapshot_indices(session, endpoint, repository, snapshot_name)
        if not snapshot_indices:
            logger.error("Could not retrieve indices from snapshot %s", snapshot_name)
            return 1
        existing = get_existing_indices(session, endpoint)
        indices = filter_restorable_indices(snapshot_indices, existing)
        if not indices:
            print(
                f"{len(snapshot_indices)} index(es) from snapshot already exist on target "
                "(or are system indices) - nothing to restore"
            )
            return 0

    if not restore_snapshot(session, endpoint, repository, snapshot_name, indices):
        return 1

    print(f"\nRestore started: {repository}/{snapshot_name} -> {endpoint}")
    print(f"Indices: {len(indices)}")
    print(f"Monitor recovery with:\n  GET {endpoint}/_cat/recovery?v\n")
    return 0


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Restore an open-source OpenSearch snapshot into a target cluster."
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--creds-file", dest="creds_file", default=DEFAULT_CREDS_FILE)
    parser.add_argument("--indices")
    parser.add_argument("--install-container", dest="install_container",
                        help="s3 only: mint a key for the tag's IAM user, install it into "
                             "this destination container's keystore, and reload before restoring")
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
