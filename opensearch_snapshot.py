"""Snapshot tool for open-source OpenSearch - hot-index snapshot and status.

Restore is a separate script - see restore.py.
"""

import argparse
import json
import logging
import os
import random
import string
import sys
import requests
from datetime import datetime
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

DEFAULT_CREDS_FILE = "snapexe-creds.json"


def generate_bucket_name(tag, suffix):
    return f"snapexe-{tag}-{suffix}"


def generate_repository_name(tag, suffix):
    return f"snapexe-{tag}-repo-{suffix}"


def generate_snapshot_name(tag, timestamp):
    return f"snapexe-{tag}-hotsnapshot-{timestamp}"


def normalize_endpoint(url):
    if not url:
        raise ValueError("Endpoint URL cannot be empty")
    url = url.rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    if not urlparse(url).netloc:
        raise ValueError(f"Invalid endpoint URL: {url}")
    return url


def filter_hot_indices(index_names, store_types):
    hot = []
    skipped_searchable = 0
    for name in index_names:
        if name.startswith(("_", ".")) or name.startswith(".ds-"):
            continue
        if store_types.get(name) == "remote_snapshot":
            skipped_searchable += 1
            continue
        hot.append(name)
    return hot, skipped_searchable


def discover_hot_indices(session, endpoint):
    try:
        cat_url = urljoin(endpoint + "/", "_cat/indices?format=json&h=index")
        cat_resp = session.get(cat_url, verify=False, timeout=30)
        if cat_resp.status_code != 200:
            logger.error("Failed to list indices: %s", cat_resp.status_code)
            return []
        index_names = [row.get("index", "") for row in cat_resp.json()]

        settings_url = urljoin(
            endpoint + "/", "_all/_settings/index.store.type?flat_settings=true"
        )
        settings_resp = session.get(settings_url, verify=False, timeout=30)
        if settings_resp.status_code != 200:
            logger.error("Failed to read index settings: %s", settings_resp.status_code)
            return []
        store_types = {
            name: body.get("settings", {}).get("index.store.type")
            for name, body in settings_resp.json().items()
        }

        hot, skipped = filter_hot_indices(index_names, store_types)
        if skipped:
            logger.info("Skipped %d searchable-snapshot index(es)", skipped)
        return hot
    except Exception as exc:
        logger.error("Exception discovering hot indices: %s", exc)
        return []


def _percent(done, total, state):
    if total > 0:
        return round(100 * done / total)
    return 100 if state == "SUCCESS" else 0


def compute_snapshot_progress(status_payload):
    state = status_payload.get("state", "UNKNOWN")
    shards = status_payload.get("shards_stats", {})
    done = shards.get("done", 0)
    total = shards.get("total", 0)

    indices = []
    for name, body in status_payload.get("indices", {}).items():
        idx_shards = body.get("shards_stats", {})
        idx_done = idx_shards.get("done", 0)
        idx_total = idx_shards.get("total", 0)
        idx_state = body.get("stage", state)
        indices.append(
            {
                "name": name,
                "state": idx_state,
                "percent": _percent(idx_done, idx_total, idx_state),
                "done": idx_done,
                "total": idx_total,
            }
        )

    return {
        "state": state,
        "percent": _percent(done, total, state),
        "shards_done": done,
        "shards_total": total,
        "indices": indices,
    }


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


def apply_aws_creds_from_file(file_creds):
    aws = file_creds.get("aws", {})
    if aws.get("access_key_id"):
        os.environ["AWS_ACCESS_KEY_ID"] = aws["access_key_id"]
    if aws.get("secret_access_key"):
        os.environ["AWS_SECRET_ACCESS_KEY"] = aws["secret_access_key"]
    if aws.get("region"):
        os.environ["AWS_DEFAULT_REGION"] = aws["region"]


def resolve_credentials(file_creds):
    opensearch = file_creds.get("opensearch", {})
    username = opensearch.get("username")
    password = opensearch.get("password")
    if not username:
        raise ValueError("Credentials file missing opensearch.username")
    if not password:
        raise ValueError("Credentials file missing opensearch.password")
    return username, password


def create_session(username, password):
    session = requests.Session()
    session.auth = (username, password)
    return session


def check_repository_snapshot_status(session, endpoint, repository):
    try:
        url = urljoin(endpoint + "/", "_snapshot/_status")
        resp = session.get(url, verify=False, timeout=30)
        if resp.status_code != 200:
            return True, []
        running = [
            s for s in resp.json().get("snapshots", [])
            if s.get("repository") == repository
        ]
        if running:
            for s in running:
                logger.warning(
                    "Snapshot in progress: %s/%s (%s)",
                    repository, s.get("snapshot"), s.get("state"),
                )
            return False, running
        return True, []
    except Exception as exc:
        logger.error("Exception checking snapshot status: %s", exc)
        return True, []


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


def create_snapshot_async(session, endpoint, repository, snapshot_name, indices):
    if not indices:
        logger.error("No indices provided for snapshot")
        return False, None
    try:
        url = urljoin(
            endpoint + "/",
            f"_snapshot/{repository}/{snapshot_name}?wait_for_completion=false",
        )
        body = {"indices": ",".join(indices)}
        resp = session.put(url, json=body, verify=False, timeout=60)
        if resp.status_code in (200, 201, 202):
            task_id = resp.json().get("task")
            logger.info("Snapshot initiated: %s/%s", repository, snapshot_name)
            return True, task_id
        logger.error("Failed to create snapshot: %s %s", resp.status_code, resp.text)
        return False, None
    except Exception as exc:
        logger.error("Exception creating snapshot: %s", exc)
        return False, None


def create_or_get_bucket(s3_client, bucket_name, region):
    from botocore.exceptions import ClientError

    try:
        if region and region != "us-east-1":
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
        else:
            s3_client.create_bucket(Bucket=bucket_name)
        logger.info("Created S3 bucket: %s", bucket_name)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            logger.info("Reusing existing S3 bucket: %s", bucket_name)
        else:
            raise
    return bucket_name


def create_iam_user_with_keys(iam_client, user_name, bucket_name):
    from botocore.exceptions import ClientError

    try:
        iam_client.create_user(UserName=user_name)
        logger.info("Created IAM user: %s", user_name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "EntityAlreadyExists":
            logger.info("Reusing existing IAM user: %s", user_name)
        else:
            raise

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
                "Resource": f"arn:aws:s3:::{bucket_name}",
            },
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": f"arn:aws:s3:::{bucket_name}/*",
            },
        ],
    }
    iam_client.put_user_policy(
        UserName=user_name,
        PolicyName=f"{user_name}-s3-access",
        PolicyDocument=json.dumps(policy),
    )

    key = iam_client.create_access_key(UserName=user_name)["AccessKey"]
    return {
        "access_key_id": key["AccessKeyId"],
        "secret_access_key": key["SecretAccessKey"],
    }


def keystore_instructions(access_key_id, secret_access_key):
    return (
        "Install these credentials in the OpenSearch keystore on EVERY node, then\n"
        "reload secure settings. Run on each node:\n\n"
        f"  echo '{access_key_id}' | bin/opensearch-keystore add --stdin --force s3.client.default.access_key\n"
        f"  echo '{secret_access_key}' | bin/opensearch-keystore add --stdin --force s3.client.default.secret_key\n\n"
        "Then, once per cluster:\n\n"
        "  POST _nodes/reload_secure_settings\n"
    )


def config_path(tag):
    return f"snapexe-{tag}-config.json"


def save_config(tag, config):
    path = config_path(tag)
    with open(path, "w") as handle:
        json.dump(config, handle, indent=2)
    logger.info("Wrote config: %s", path)
    return path


def load_config(tag):
    path = config_path(tag)
    try:
        with open(path, "r") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Config {path} not found. Run: snapshot --tag {tag} ... first"
        )


def parse_arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Snapshot open-source OpenSearch hot indices and check status."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot", help="Take an async snapshot of hot indices")
    snap.add_argument("--tag", required=True)
    snap.add_argument("--endpoint", required=True)
    snap.add_argument("--repo-type", dest="repo_type", required=True, choices=["fs", "s3"])
    snap.add_argument("--creds-file", dest="creds_file", default=DEFAULT_CREDS_FILE)
    snap.add_argument("--indices")
    snap.add_argument("--repository")
    snap.add_argument("--snapshot-name", dest="snapshot_name")
    snap.add_argument("--repo-path", dest="repo_path")
    snap.add_argument("--dry-run", dest="dry_run", action="store_true")
    snap.add_argument("--debug", action="store_true")

    prov = sub.add_parser("provision", help="Provision s3 bucket + IAM user (phase 1 of s3)")
    prov.add_argument("--tag", required=True)
    prov.add_argument("--endpoint", required=True)
    prov.add_argument("--creds-file", dest="creds_file", default=DEFAULT_CREDS_FILE)
    prov.add_argument("--bucket")
    prov.add_argument("--region")
    prov.add_argument("--debug", action="store_true")

    stat = sub.add_parser("status", help="Report progress of the last snapshot")
    stat.add_argument("--tag", required=True)
    stat.add_argument("--creds-file", dest="creds_file", default=DEFAULT_CREDS_FILE)
    stat.add_argument("--endpoint")
    stat.add_argument("--debug", action="store_true")

    return parser.parse_args(argv)


def _random_suffix(n=4):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _timestamp():
    return datetime.now().strftime("%Y-%m-%d-%H-%M-%S")


def resolve_region(region_arg, boto3_module):
    if region_arg:
        return region_arg
    try:
        return boto3_module.session.Session().region_name
    except Exception:
        return None


def _setup_repository(args, session, endpoint):
    """Set up the backend repository. Returns (ok, repository_name)."""
    if args.repo_type == "fs":
        repository = args.repository or generate_repository_name(args.tag, _random_suffix())
        body = build_repository_body("fs", location=args.repo_path)
        if args.dry_run:
            logger.info("[DRY RUN] Would register fs repository at %s", args.repo_path)
            return True, repository
        ok = register_repository(session, endpoint, repository, body)
        return ok, repository

    # s3: read the config written by `provision`
    try:
        cfg = load_config(args.tag)
    except FileNotFoundError:
        logger.error(
            "No provisioned s3 config for tag '%s'. Run: provision --tag %s --endpoint %s ... first",
            args.tag, args.tag, args.endpoint,
        )
        return False, None
    if cfg.get("repo_type") != "s3":
        logger.error("Config for tag '%s' is not an s3 provision", args.tag)
        return False, None
    repository = cfg["repository"]
    if args.dry_run:
        logger.info("[DRY RUN] Would register s3 repository %s", repository)
        return True, repository
    body = build_repository_body(
        "s3", bucket=cfg["bucket"], base_path=repository, region=cfg.get("region")
    )
    ok = register_repository(session, endpoint, repository, body)
    if not ok:
        logger.error(
            "s3 repository registration failed. If keys are not yet installed, run on "
            "each node: opensearch-keystore add s3.client.default.access_key / "
            "secret_key, then POST _nodes/reload_secure_settings, and retry."
        )
    return ok, repository


def run_provision(args, *, boto3_module=None):
    try:
        file_creds = load_creds_file(args.creds_file)
    except (FileNotFoundError, ValueError) as exc:
        logger.error(str(exc))
        return 2
    apply_aws_creds_from_file(file_creds)
    if boto3_module is None:
        import boto3 as boto3_module
    region = resolve_region(args.region, boto3_module)
    suffix = _random_suffix()
    bucket = args.bucket or generate_bucket_name(args.tag, suffix)
    repository = generate_repository_name(args.tag, suffix)

    s3_client = boto3_module.client("s3", region_name=region)
    iam_client = boto3_module.client("iam")
    create_or_get_bucket(s3_client, bucket, region)
    creds = create_iam_user_with_keys(iam_client, f"snapexe-{args.tag}-user", bucket)

    config = {
        "tag": args.tag,
        "endpoint": normalize_endpoint(args.endpoint),
        "repo_type": "s3",
        "bucket": bucket,
        "region": region,
        "repository": repository,
    }
    save_config(args.tag, config)

    print(keystore_instructions(creds["access_key_id"], creds["secret_access_key"]))
    print(f"\nProvisioned bucket {bucket} and repository name {repository}.")
    print("After installing the keys above and reloading secure settings, run:")
    print(f"  python opensearch_snapshot.py snapshot --tag {args.tag} --repo-type s3 --endpoint {args.endpoint}\n")
    return 0


def run_snapshot(args, *, session_factory=create_session):
    try:
        file_creds = load_creds_file(args.creds_file)
        username, password = resolve_credentials(file_creds)
    except (FileNotFoundError, ValueError) as exc:
        logger.error(str(exc))
        return 2

    endpoint = normalize_endpoint(args.endpoint)
    snapshot_name = args.snapshot_name or generate_snapshot_name(args.tag, _timestamp())
    session = session_factory(username, password)

    ok, repository = _setup_repository(args, session, endpoint)
    if not ok:
        logger.error("Repository setup failed")
        return 2 if repository is None else 1

    if args.indices:
        indices = [i.strip() for i in args.indices.split(",") if i.strip()]
    elif args.dry_run:
        indices = ["<discovered-at-runtime>"]
    else:
        indices = discover_hot_indices(session, endpoint)

    if not indices and not args.dry_run:
        logger.error("No hot indices to snapshot")
        return 1

    if args.dry_run:
        logger.info("[DRY RUN] Would snapshot %s into %s/%s", indices, repository, snapshot_name)
        return 0

    can_proceed, _ = check_repository_snapshot_status(session, endpoint, repository)
    if not can_proceed:
        logger.error("A snapshot is already in progress in %s", repository)
        return 1

    ok, task_id = create_snapshot_async(session, endpoint, repository, snapshot_name, indices)
    if not ok:
        return 1

    if args.repo_type == "s3":
        config = load_config(args.tag)
    else:
        config = {"tag": args.tag, "repo_type": "fs", "repo_path": args.repo_path}
    config.update({
        "tag": args.tag,
        "endpoint": endpoint,
        "repository": repository,
        "snapshot_name": snapshot_name,
    })
    save_config(args.tag, config)

    print(f"\nSnapshot started: {repository}/{snapshot_name}")
    print(f"Indices: {len(indices)}")
    print(f"Check progress with:\n  python opensearch_snapshot.py status --tag {args.tag}\n")
    return 0


def run_status(args, *, session_factory=create_session):
    try:
        config = load_config(args.tag)
    except FileNotFoundError as exc:
        logger.error(str(exc))
        return 2
    try:
        file_creds = load_creds_file(args.creds_file)
        username, password = resolve_credentials(file_creds)
    except (FileNotFoundError, ValueError) as exc:
        logger.error(str(exc))
        return 2

    endpoint = normalize_endpoint(args.endpoint or config["endpoint"])
    repository = config["repository"]
    snapshot_name = config["snapshot_name"]
    session = session_factory(username, password)

    url = urljoin(endpoint + "/", f"_snapshot/{repository}/{snapshot_name}/_status")
    resp = session.get(url, verify=False, timeout=30)
    if resp.status_code != 200:
        logger.error("Failed to get snapshot status: %s %s", resp.status_code, resp.text)
        return 1
    snapshots = resp.json().get("snapshots", [])
    if not snapshots:
        print(f"No status yet for {repository}/{snapshot_name} (it may be queued).")
        return 0

    progress = compute_snapshot_progress(snapshots[0])
    print(f"\nSnapshot: {repository}/{snapshot_name}")
    print(f"State: {progress['state']}")
    print(f"Overall: {progress['percent']}% ({progress['shards_done']}/{progress['shards_total']} shards)")
    for idx in progress["indices"]:
        print(f"  - {idx['name']}: {idx['percent']}% ({idx['done']}/{idx['total']}) {idx['state']}")
    return 0


def main(argv=None):
    args = parse_arguments(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "debug", False) else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    if args.command == "provision":
        return run_provision(args)
    if args.command == "snapshot":
        return run_snapshot(args)
    if args.command == "status":
        return run_status(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
