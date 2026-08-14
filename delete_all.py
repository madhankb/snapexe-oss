"""Teardown tool for open-source OpenSearch snapexe resources.

Standalone script (python delete_all.py --tag ...). It reads snapexe-{tag}-config.json
and removes everything the snapshot tool created for that tag:

  - the snapshots in the OpenSearch repository, then the repository registration
  - the S3 bucket, emptied then deleted            (s3 repo_type only)
  - the IAM user snapexe-{tag}-user, its access keys and inline policy  (s3 only)
  - the local snapexe-{tag}-config.json file

OpenSearch cleanup uses the opensearch_source credentials; AWS cleanup uses the boto3
default credential chain, exactly like opensearch_snapshot.py. This is destructive: it
prompts for confirmation unless --yes, and supports --dry-run. The config file is kept
if any step fails, so the teardown can be re-run.
"""

import argparse
import json
import logging
import os
import sys
import requests
import urllib3
from urllib.parse import urljoin, urlparse

from opensearch_snapshot import load_creds

# Local clusters use self-signed certs and the tool calls verify=False; silence the noise.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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


def iam_user_name(tag):
    return f"snapexe-{tag}-user"


def delete_all_snapshots(session, endpoint, repository, dry_run=False):
    """Delete every snapshot in the repository. Missing repo counts as success."""
    if dry_run:
        logger.info("[DRY RUN] Would delete all snapshots in repository %s", repository)
        return True
    try:
        url = urljoin(endpoint + "/", f"_snapshot/{repository}/_all")
        resp = session.get(url, verify=False, timeout=30)
        if resp.status_code == 404:
            logger.info("Repository %s not found; no snapshots to delete", repository)
            return True
        if resp.status_code != 200:
            logger.warning("Could not list snapshots: %s %s", resp.status_code, resp.text)
            return False
        snapshots = resp.json().get("snapshots", [])
        if not snapshots:
            logger.info("No snapshots in repository %s", repository)
            return True
        ok = True
        for snap in snapshots:
            name = snap.get("snapshot")
            del_url = urljoin(endpoint + "/", f"_snapshot/{repository}/{name}")
            dresp = session.delete(del_url, verify=False, timeout=60)
            if dresp.status_code in (200, 404):
                logger.info("Deleted snapshot %s", name)
            else:
                logger.warning("Failed to delete snapshot %s: %s %s", name, dresp.status_code, dresp.text)
                ok = False
        return ok
    except Exception as exc:
        logger.error("Exception deleting snapshots: %s", exc)
        return False


def deregister_repository(session, endpoint, repository, dry_run=False):
    """Remove the repository registration from the cluster (S3/fs data untouched)."""
    if dry_run:
        logger.info("[DRY RUN] Would deregister repository %s", repository)
        return True
    try:
        url = urljoin(endpoint + "/", f"_snapshot/{repository}")
        resp = session.delete(url, verify=False, timeout=60)
        if resp.status_code in (200, 404):
            logger.info("Deregistered repository %s", repository)
            return True
        logger.warning("Failed to deregister repository %s: %s %s", repository, resp.status_code, resp.text)
        return False
    except Exception as exc:
        logger.error("Exception deregistering repository: %s", exc)
        return False


def delete_s3_bucket(s3_client, bucket, dry_run=False):
    """Empty the bucket (objects, versions, delete markers) then delete it. Idempotent."""
    if dry_run:
        logger.info("[DRY RUN] Would empty and delete S3 bucket %s", bucket)
        return True
    from botocore.exceptions import ClientError
    try:
        for page in s3_client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objects:
                s3_client.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
        for page in s3_client.get_paginator("list_object_versions").paginate(Bucket=bucket):
            markers = [
                {"Key": v["Key"], "VersionId": v["VersionId"]}
                for v in page.get("Versions", []) + page.get("DeleteMarkers", [])
            ]
            if markers:
                s3_client.delete_objects(Bucket=bucket, Delete={"Objects": markers, "Quiet": True})
        s3_client.delete_bucket(Bucket=bucket)
        logger.info("Deleted S3 bucket %s", bucket)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchBucket":
            logger.info("S3 bucket %s does not exist (already deleted)", bucket)
            return True
        logger.error("Error deleting S3 bucket %s: %s", bucket, exc)
        return False


def delete_iam_user(iam_client, user_name, dry_run=False):
    """Delete access keys + inline policies, then the IAM user. Idempotent."""
    if dry_run:
        logger.info("[DRY RUN] Would delete IAM user %s (access keys + inline policies)", user_name)
        return True
    from botocore.exceptions import ClientError
    try:
        for key in iam_client.list_access_keys(UserName=user_name).get("AccessKeyMetadata", []):
            iam_client.delete_access_key(UserName=user_name, AccessKeyId=key["AccessKeyId"])
            logger.info("Deleted access key %s", key["AccessKeyId"])
        for policy_name in iam_client.list_user_policies(UserName=user_name).get("PolicyNames", []):
            iam_client.delete_user_policy(UserName=user_name, PolicyName=policy_name)
            logger.info("Deleted inline policy %s", policy_name)
        iam_client.delete_user(UserName=user_name)
        logger.info("Deleted IAM user %s", user_name)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "NoSuchEntity":
            logger.info("IAM user %s does not exist (already deleted)", user_name)
            return True
        logger.error("Error deleting IAM user %s: %s", user_name, exc)
        return False


def print_preview(config, user_name):
    tag = config.get("tag")
    repository = config.get("repository")
    print(f"\nResources to delete for tag '{tag}':")
    print(f"  - OpenSearch snapshots in repository : {repository}")
    print(f"  - OpenSearch repository registration : {repository} (on {config.get('endpoint')})")
    if config.get("repo_type") == "s3":
        print(f"  - S3 bucket (emptied + deleted)      : {config.get('bucket')}")
        print(f"  - IAM user + access keys + policy    : {user_name}")
    print(f"  - Local config file                  : {config_path(tag)}")


def run_delete_all(args, *, session_factory=create_session, boto3_module=None):
    try:
        config = load_config(args.tag)
    except FileNotFoundError as exc:
        logger.error(str(exc))
        return 2
    try:
        file_creds = load_creds(getattr(args, "secret_id", None), args.creds_file, boto3_module)
    except (FileNotFoundError, ValueError) as exc:
        logger.error(str(exc))
        return 2

    tag = config.get("tag", args.tag)
    repo_type = config.get("repo_type")
    repository = config.get("repository")
    endpoint = config.get("endpoint")
    user_name = iam_user_name(tag)

    print_preview(config, user_name)

    if args.dry_run:
        logger.info("=== DRY RUN - no changes will be made ===")
    elif not args.yes:
        resp = input("\nDelete these resources? This cannot be undone. (yes/no): ")
        if resp.strip().lower() not in ("yes", "y"):
            logger.info("Cancelled - nothing deleted")
            return 0

    failures = 0

    # 1. OpenSearch: delete snapshots, then deregister the repository.
    if endpoint and repository:
        if args.dry_run:
            delete_all_snapshots(None, endpoint, repository, dry_run=True)
            deregister_repository(None, endpoint, repository, dry_run=True)
        else:
            try:
                username, password = resolve_credentials(file_creds, "opensearch_source")
            except ValueError as exc:
                logger.warning("Skipping OpenSearch cleanup: %s", exc)
                username = None
            if username:
                session = session_factory(username, password)
                norm = normalize_endpoint(endpoint)
                if not delete_all_snapshots(session, norm, repository):
                    failures += 1
                if not deregister_repository(session, norm, repository):
                    failures += 1
    else:
        logger.warning("No endpoint/repository in config; skipping OpenSearch cleanup")

    # 2 + 3. AWS: S3 bucket and IAM user (s3 repo type only).
    if repo_type == "s3":
        if args.dry_run:
            delete_s3_bucket(None, config.get("bucket"), dry_run=True)
            delete_iam_user(None, user_name, dry_run=True)
        else:
            if boto3_module is None:
                import boto3 as boto3_module
            if config.get("bucket"):
                s3_client = boto3_module.client("s3", region_name=config.get("region"))
                if not delete_s3_bucket(s3_client, config["bucket"]):
                    failures += 1
            if not delete_iam_user(boto3_module.client("iam"), user_name):
                failures += 1
    else:
        logger.info("repo_type is '%s'; no S3/IAM resources to delete", repo_type)

    # 4. Local config file - kept if anything failed, so the teardown can be re-run.
    if args.dry_run:
        logger.info("[DRY RUN] Would delete config file %s", config_path(tag))
    elif failures:
        logger.warning("%d step(s) failed; keeping %s so you can re-run", failures, config_path(tag))
    else:
        try:
            os.remove(config_path(tag))
            logger.info("Deleted config file %s", config_path(tag))
        except OSError as exc:
            logger.error("Could not delete config file: %s", exc)
            failures += 1

    if args.dry_run:
        print("\nDry run complete - no resources were deleted.")
        return 0
    if failures:
        print(f"\nDone with {failures} failure(s) - see the log above; config file retained.")
        return 1
    print("\nAll resources deleted.")
    return 0


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Delete all AWS + OpenSearch resources snapexe created for a tag."
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--creds-file", dest="creds_file", default=DEFAULT_CREDS_FILE)
    parser.add_argument("--secret-id", dest="secret_id",
                        help="AWS Secrets Manager secret holding OpenSearch creds; overrides --creds-file")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_arguments(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "debug", False) else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    return run_delete_all(args)


if __name__ == "__main__":
    sys.exit(main())
