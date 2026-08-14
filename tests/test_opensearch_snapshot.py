import json

import pytest
from unittest.mock import MagicMock
from botocore.exceptions import ClientError

import opensearch_snapshot as oss


def test_generate_bucket_name():
    assert oss.generate_bucket_name("prod", "a1b2") == "snapexe-prod-a1b2"


def test_generate_repository_name():
    assert oss.generate_repository_name("prod", "a1b2") == "snapexe-prod-repo-a1b2"


def test_generate_snapshot_name():
    assert (
        oss.generate_snapshot_name("prod", "2026-08-10-15-30-00")
        == "snapexe-prod-hotsnapshot-2026-08-10-15-30-00"
    )


def test_normalize_endpoint_adds_scheme_and_strips_slash():
    assert oss.normalize_endpoint("localhost:9200/") == "https://localhost:9200"


def test_normalize_endpoint_keeps_existing_scheme():
    assert oss.normalize_endpoint("http://localhost:9200") == "http://localhost:9200"


def test_normalize_endpoint_rejects_empty():
    with pytest.raises(ValueError):
        oss.normalize_endpoint("")


def test_filter_hot_indices_excludes_system_and_backing():
    names = ["logs", "_internal", ".kibana", ".ds-metrics-000001", "orders"]
    hot, skipped = oss.filter_hot_indices(names, {})
    assert hot == ["logs", "orders"]
    assert skipped == 0


def test_filter_hot_indices_excludes_searchable_snapshots():
    names = ["logs", "archived", "orders"]
    store_types = {"archived": "remote_snapshot"}
    hot, skipped = oss.filter_hot_indices(names, store_types)
    assert hot == ["logs", "orders"]
    assert skipped == 1


def test_filter_hot_indices_preserves_order():
    names = ["b", "a", "c"]
    hot, _ = oss.filter_hot_indices(names, {})
    assert hot == ["b", "a", "c"]


def _json_response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def test_discover_hot_indices_combines_calls():
    session = MagicMock()

    def get(url, **kwargs):
        if "_cat/indices" in url:
            return _json_response([{"index": "logs"}, {"index": "archived"}])
        return _json_response(
            {
                "logs": {"settings": {"index.store.type": "fs"}},
                "archived": {"settings": {"index.store.type": "remote_snapshot"}},
            }
        )

    session.get.side_effect = get
    assert oss.discover_hot_indices(session, "https://host:9200") == ["logs"]


def test_discover_hot_indices_returns_empty_on_error():
    session = MagicMock()
    session.get.return_value = _json_response({}, status=500)
    assert oss.discover_hot_indices(session, "https://host:9200") == []


def test_discover_data_streams_returns_names():
    session = MagicMock()
    session.get.return_value = _json_response(
        {"data_streams": [{"name": "logs-datastream"}, {"name": "metrics-ds"}]}
    )
    assert oss.discover_data_streams(session, "https://host:9200") == ["logs-datastream", "metrics-ds"]


def test_discover_data_streams_empty_when_none():
    session = MagicMock()
    session.get.return_value = _json_response({"data_streams": []})
    assert oss.discover_data_streams(session, "https://host:9200") == []


def test_discover_data_streams_empty_on_error():
    session = MagicMock()
    session.get.return_value = _json_response({}, status=500)
    assert oss.discover_data_streams(session, "https://host:9200") == []


def test_compute_progress_in_progress():
    payload = {
        "snapshot": "snap-1",
        "state": "IN_PROGRESS",
        "shards_stats": {"done": 3, "total": 6},
        "indices": {
            "logs": {"stage": "STARTED", "shards_stats": {"done": 1, "total": 2}},
            "orders": {"stage": "DONE", "shards_stats": {"done": 2, "total": 2}},
        },
    }
    result = oss.compute_snapshot_progress(payload)
    assert result["state"] == "IN_PROGRESS"
    assert result["percent"] == 50
    assert result["shards_done"] == 3
    assert result["shards_total"] == 6
    by_name = {i["name"]: i for i in result["indices"]}
    assert by_name["logs"]["percent"] == 50
    assert by_name["orders"]["percent"] == 100


def test_compute_progress_success_zero_total():
    payload = {"snapshot": "s", "state": "SUCCESS", "shards_stats": {"done": 0, "total": 0}, "indices": {}}
    result = oss.compute_snapshot_progress(payload)
    assert result["percent"] == 100


def test_compute_progress_pending_zero_total():
    payload = {"snapshot": "s", "state": "IN_PROGRESS", "shards_stats": {"done": 0, "total": 0}, "indices": {}}
    result = oss.compute_snapshot_progress(payload)
    assert result["percent"] == 0


def test_build_fs_repository_body():
    body = oss.build_repository_body("fs", location="/mnt/snapshots")
    assert body == {"type": "fs", "settings": {"location": "/mnt/snapshots"}}


def test_build_s3_repository_body_with_region():
    body = oss.build_repository_body(
        "s3", bucket="snapexe-prod-a1b2", base_path="snapexe-prod-repo-a1b2", region="us-east-1"
    )
    assert body == {
        "type": "s3",
        "settings": {
            "bucket": "snapexe-prod-a1b2",
            "base_path": "snapexe-prod-repo-a1b2",
            "region": "us-east-1",
        },
    }


def test_build_s3_repository_body_omits_none_region():
    body = oss.build_repository_body("s3", bucket="b", base_path="p", region=None)
    assert "region" not in body["settings"]


def test_build_repository_body_rejects_unknown_type():
    with pytest.raises(ValueError):
        oss.build_repository_body("azure")


def test_build_fs_repository_body_requires_location():
    with pytest.raises(ValueError):
        oss.build_repository_body("fs", location=None)


def _write_creds(tmp_path, data, name="snapexe-creds.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return str(p)


def test_load_creds_file_reads_json(tmp_path):
    path = _write_creds(tmp_path, {"opensearch_source": {"username": "admin", "password": "pw"}})
    assert oss.load_creds_file(path) == {"opensearch_source": {"username": "admin", "password": "pw"}}


def test_load_creds_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        oss.load_creds_file(str(tmp_path / "nope.json"))


def test_load_creds_file_invalid_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ValueError):
        oss.load_creds_file(str(p))


def test_load_creds_file_rejects_non_dict(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text("[]")
    with pytest.raises(ValueError):
        oss.load_creds_file(str(p))


def test_load_creds_uses_file_when_no_secret(tmp_path):
    path = _write_creds(tmp_path, {"opensearch_source": {"username": "admin", "password": "pw"}})
    assert oss.load_creds(None, path) == {"opensearch_source": {"username": "admin", "password": "pw"}}


def test_load_creds_reads_secret_when_secret_id_set():
    payload = {"opensearch_source": {"username": "admin", "password": "sekret"},
               "opensearch_dest": {"username": "admin", "password": "sekret"}}
    boto3_module = MagicMock()
    sm = boto3_module.client.return_value
    sm.get_secret_value.return_value = {"SecretString": json.dumps(payload)}
    result = oss.load_creds("snapexe/opensearch", None, boto3_module=boto3_module)
    assert result == payload
    boto3_module.client.assert_called_once_with("secretsmanager")
    sm.get_secret_value.assert_called_once_with(SecretId="snapexe/opensearch")


def test_load_secret_creds_rejects_non_json():
    boto3_module = MagicMock()
    boto3_module.client.return_value.get_secret_value.return_value = {"SecretString": "not json"}
    with pytest.raises(ValueError, match="not valid JSON"):
        oss.load_secret_creds("snapexe/opensearch", boto3_module=boto3_module)


def test_resolve_credentials_source():
    fc = {"opensearch_source": {"username": "srcuser", "password": "srcpw"},
          "opensearch_dest": {"username": "dstuser", "password": "dstpw"}}
    assert oss.resolve_credentials(fc, "opensearch_source") == ("srcuser", "srcpw")


def test_resolve_credentials_dest():
    fc = {"opensearch_source": {"username": "srcuser", "password": "srcpw"},
          "opensearch_dest": {"username": "dstuser", "password": "dstpw"}}
    assert oss.resolve_credentials(fc, "opensearch_dest") == ("dstuser", "dstpw")


def test_resolve_credentials_missing_section_raises():
    with pytest.raises(ValueError):
        oss.resolve_credentials({}, "opensearch_source")


def test_resolve_credentials_missing_password_raises():
    with pytest.raises(ValueError):
        oss.resolve_credentials({"opensearch_source": {"username": "admin"}}, "opensearch_source")


def test_create_session_sets_basic_auth():
    session = oss.create_session("admin", "secret")
    assert session.auth == ("admin", "secret")


def test_check_repo_status_blocks_when_in_progress():
    session = MagicMock()
    session.get.return_value = _json_response(
        {"snapshots": [{"repository": "repo-a", "snapshot": "s1", "state": "IN_PROGRESS"}]}
    )
    can_proceed, running = oss.check_repository_snapshot_status(session, "https://h:9200", "repo-a")
    assert can_proceed is False
    assert len(running) == 1


def test_check_repo_status_allows_when_other_repo_busy():
    session = MagicMock()
    session.get.return_value = _json_response(
        {"snapshots": [{"repository": "repo-b", "snapshot": "s1", "state": "IN_PROGRESS"}]}
    )
    can_proceed, running = oss.check_repository_snapshot_status(session, "https://h:9200", "repo-a")
    assert can_proceed is True
    assert running == []


def test_register_repository_success():
    session = MagicMock()
    session.put.return_value = _json_response({"acknowledged": True})
    body = {"type": "fs", "settings": {"location": "/mnt/snap"}}
    assert oss.register_repository(session, "https://h:9200", "repo-a", body) is True


def test_register_repository_failure():
    session = MagicMock()
    session.put.return_value = _json_response({"error": "bad"}, status=500)
    assert oss.register_repository(session, "https://h:9200", "repo-a", {}) is False


def test_create_snapshot_async_success():
    session = MagicMock()
    session.put.return_value = _json_response({"task": "task-123"}, status=202)
    ok, task_id = oss.create_snapshot_async(session, "https://h:9200", "repo-a", "snap-1", ["logs"])
    assert ok is True
    assert task_id == "task-123"
    sent_body = session.put.call_args.kwargs["json"]
    assert sent_body["indices"] == "logs"
    assert sent_body["ignore_unavailable"] is True
    assert sent_body["include_global_state"] is False
    assert sent_body["partial"] is False


def test_create_snapshot_async_rejects_empty_indices():
    session = MagicMock()
    ok, task_id = oss.create_snapshot_async(session, "https://h:9200", "repo-a", "snap-1", [])
    assert ok is False
    assert task_id is None
    session.put.assert_not_called()


def test_create_or_get_bucket_us_east_1_no_location_constraint():
    s3 = MagicMock()
    name = oss.create_or_get_bucket(s3, "snapexe-prod-a1b2", "us-east-1")
    assert name == "snapexe-prod-a1b2"
    _, kwargs = s3.create_bucket.call_args
    assert "CreateBucketConfiguration" not in kwargs


def test_create_or_get_bucket_other_region_sets_location():
    s3 = MagicMock()
    oss.create_or_get_bucket(s3, "snapexe-prod-a1b2", "us-west-2")
    _, kwargs = s3.create_bucket.call_args
    assert kwargs["CreateBucketConfiguration"] == {"LocationConstraint": "us-west-2"}


def test_create_iam_user_with_keys_returns_credentials():
    iam = MagicMock()
    iam.create_access_key.return_value = {
        "AccessKey": {"AccessKeyId": "AKIA123", "SecretAccessKey": "secret456"}
    }
    creds = oss.create_iam_user_with_keys(iam, "snapexe-prod-user", "snapexe-prod-a1b2")
    assert creds == {"access_key_id": "AKIA123", "secret_access_key": "secret456"}
    iam.put_user_policy.assert_called_once()
    policy_doc = iam.put_user_policy.call_args.kwargs["PolicyDocument"]
    assert "snapexe-prod-a1b2" in policy_doc


def test_create_iam_user_with_keys_multi_bucket_policy():
    iam = MagicMock()
    iam.create_access_key.return_value = {
        "AccessKey": {"AccessKeyId": "AKIA123", "SecretAccessKey": "secret456"}
    }
    oss.create_iam_user_with_keys(
        iam, "snapexe-nightly-user", ["snapexe-nightly-224c", "snapexe-ss-vsqw"]
    )
    policy = json.loads(iam.put_user_policy.call_args.kwargs["PolicyDocument"])
    resources = []
    for stmt in policy["Statement"]:
        res = stmt["Resource"]
        resources.extend(res if isinstance(res, list) else [res])
    assert "arn:aws:s3:::snapexe-nightly-224c" in resources
    assert "arn:aws:s3:::snapexe-ss-vsqw" in resources
    assert "arn:aws:s3:::snapexe-nightly-224c/*" in resources
    assert "arn:aws:s3:::snapexe-ss-vsqw/*" in resources


def test_keystore_instructions_mentions_both_keys_and_reload():
    text = oss.keystore_instructions("AKIA123", "secret456")
    assert "s3.client.default.access_key" in text
    assert "s3.client.default.secret_key" in text
    assert "reload_secure_settings" in text


def test_save_and_load_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = {"tag": "prod", "repo_type": "fs", "repository": "snapexe-prod-repo-a1b2"}
    path = oss.save_config("prod", config)
    assert path == "snapexe-prod-config.json"
    assert oss.load_config("prod") == config


def test_load_config_missing_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        oss.load_config("nope")


def test_parse_snapshot_fs_args():
    ns = oss.parse_arguments(
        ["snapshot", "--tag", "prod", "--endpoint", "https://h:9200",
         "--repo-type", "fs", "--repo-path", "/mnt/snap"]
    )
    assert ns.command == "snapshot"
    assert ns.tag == "prod"
    assert ns.repo_type == "fs"
    assert ns.repo_path == "/mnt/snap"


def test_parse_snapshot_s3_args():
    ns = oss.parse_arguments(
        ["snapshot", "--tag", "prod", "--endpoint", "https://h:9200",
         "--repo-type", "s3"]
    )
    assert ns.repo_type == "s3"


def test_parse_provision_args():
    ns = oss.parse_arguments(
        ["provision", "--tag", "prod", "--endpoint", "https://h:9200",
         "--region", "us-east-1", "--bucket", "my-bucket"]
    )
    assert ns.command == "provision"
    assert ns.region == "us-east-1"
    assert ns.bucket == "my-bucket"


def test_parse_snapshot_accepts_region_for_auto_provision():
    ns = oss.parse_arguments(
        ["snapshot", "--tag", "prod", "--endpoint", "https://h:9200",
         "--repo-type", "s3", "--region", "us-east-1"]
    )
    assert ns.region == "us-east-1"


def test_parse_status_args():
    ns = oss.parse_arguments(["status", "--tag", "prod"])
    assert ns.command == "status"
    assert ns.tag == "prod"


def test_parse_snapshot_creds_file_default():
    ns = oss.parse_arguments(["snapshot", "--tag", "prod", "--endpoint", "https://h:9200", "--repo-type", "fs"])
    assert ns.creds_file == "snapexe-creds.json"


def test_parse_snapshot_rejects_user():
    with pytest.raises(SystemExit):
        oss.parse_arguments(["snapshot", "--tag", "prod", "--endpoint", "https://h:9200",
                             "--repo-type", "fs", "--user", "admin"])


def test_parse_snapshot_requires_repo_type():
    with pytest.raises(SystemExit):
        oss.parse_arguments(["snapshot", "--tag", "prod", "--endpoint", "https://h:9200"])


def test_parse_snapshot_accepts_secret_id():
    args = oss.parse_arguments(["snapshot", "--tag", "prod", "--endpoint", "https://h:9200",
                                "--repo-type", "fs", "--secret-id", "snapexe/opensearch"])
    assert args.secret_id == "snapexe/opensearch"


def test_parse_status_accepts_secret_id():
    args = oss.parse_arguments(["status", "--tag", "prod", "--secret-id", "snapexe/opensearch"])
    assert args.secret_id == "snapexe/opensearch"


def _snapshot_args(tmp_path, **overrides):
    import argparse
    base = dict(
        command="snapshot", tag="prod", endpoint="localhost:9200", repo_type="fs",
        creds_file="snapexe-creds.json", indices=None, repository=None,
        snapshot_name="snapexe-prod-hotsnapshot-t",
        repo_path="/mnt/snap", dry_run=False, debug=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_run_snapshot_fs_happy_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "snapexe-creds.json").write_text(
        '{"opensearch_source": {"username": "admin", "password": "secret"}}'
    )

    session = MagicMock()
    # _cat/indices, _all/_settings, _data_stream, then _snapshot/_status (proceed check)
    session.get.side_effect = [
        _json_response([{"index": "logs"}]),
        _json_response({"logs": {"settings": {"index.store.type": "fs"}}}),
        _json_response({"data_streams": []}),
        _json_response({"snapshots": []}),
    ]
    session.put.side_effect = [
        _json_response({"acknowledged": True}),          # register repo
        _json_response({"task": "task-1"}, status=202),  # snapshot
    ]

    rc = oss.run_snapshot(_snapshot_args(tmp_path), session_factory=lambda u, p: session)
    assert rc == 0
    assert oss.load_config("prod")["repository"].startswith("snapexe-prod-repo-")
    # config must not contain credentials
    assert "password" not in oss.load_config("prod")


def test_run_snapshot_fs_dry_run_no_mutations(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "snapexe-creds.json").write_text(
        '{"opensearch_source": {"username": "admin", "password": "secret"}}'
    )
    session = MagicMock()
    rc = oss.run_snapshot(_snapshot_args(tmp_path, dry_run=True), session_factory=lambda u, p: session)
    assert rc == 0
    session.put.assert_not_called()


def test_run_snapshot_needs_only_source_block(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "snapexe-creds.json").write_text(
        '{"opensearch_source": {"username": "admin", "password": "secret"}}'
    )
    session = MagicMock()
    session.get.side_effect = [
        _json_response([{"index": "logs"}]),
        _json_response({"logs": {"settings": {"index.store.type": "fs"}}}),
        _json_response({"data_streams": []}),
        _json_response({"snapshots": []}),
    ]
    session.put.side_effect = [
        _json_response({"acknowledged": True}),
        _json_response({"task": "task-1"}, status=202),
    ]
    rc = oss.run_snapshot(_snapshot_args(tmp_path), session_factory=lambda u, p: session)
    assert rc == 0  # no opensearch_dest block present, snapshot still works


def test_run_snapshot_default_includes_data_streams(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "snapexe-creds.json").write_text(
        '{"opensearch_source": {"username": "admin", "password": "secret"}}'
    )
    session = MagicMock()
    session.get.side_effect = [
        _json_response([{"index": "logs"}]),                                   # _cat/indices
        _json_response({"logs": {"settings": {"index.store.type": "fs"}}}),    # store types
        _json_response({"data_streams": [{"name": "logs-datastream"}]}),       # _data_stream
        _json_response({"snapshots": []}),                                     # proceed check
    ]
    session.put.side_effect = [
        _json_response({"acknowledged": True}),          # register repo
        _json_response({"task": "task-1"}, status=202),  # snapshot
    ]
    rc = oss.run_snapshot(_snapshot_args(tmp_path), session_factory=lambda u, p: session)
    assert rc == 0
    # With no --indices, the snapshot body must include both the regular index and the data stream
    snapshot_body = session.put.call_args_list[1].kwargs["json"]
    assert "logs" in snapshot_body["indices"]
    assert "logs-datastream" in snapshot_body["indices"]


def test_run_status_reports_percent(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "snapexe-creds.json").write_text(
        '{"opensearch_source": {"username": "admin", "password": "secret"}}'
    )
    oss.save_config("prod", {
        "tag": "prod", "endpoint": "https://h:9200",
        "repository": "snapexe-prod-repo-a1b2", "snapshot_name": "snap-1",
    })
    session = MagicMock()
    session.get.return_value = _json_response({
        "snapshots": [{
            "snapshot": "snap-1", "state": "IN_PROGRESS",
            "shards_stats": {"done": 1, "total": 2}, "indices": {},
        }]
    })
    import argparse
    args = argparse.Namespace(command="status", tag="prod", creds_file="snapexe-creds.json", endpoint=None, debug=False)
    rc = oss.run_status(args, session_factory=lambda u, p: session)
    assert rc == 0
    assert "50%" in capsys.readouterr().out


def test_resolve_region_prefers_arg():
    assert oss.resolve_region("us-west-2", MagicMock()) == "us-west-2"


def test_resolve_region_falls_back_to_session():
    boto3_module = MagicMock()
    boto3_module.session.Session.return_value.region_name = "eu-west-1"
    assert oss.resolve_region(None, boto3_module) == "eu-west-1"


def test_run_provision_creates_resources_and_saves_stable_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import argparse
    (tmp_path / "snapexe-creds.json").write_text(
        '{"aws": {"access_key_id": "AK", "secret_access_key": "SK", "region": "us-east-1"}}'
    )
    boto3_module = MagicMock()
    boto3_module.session.Session.return_value.region_name = "us-east-1"
    iam = boto3_module.client.return_value
    iam.create_access_key.return_value = {
        "AccessKey": {"AccessKeyId": "AKIA1", "SecretAccessKey": "sek1"}
    }
    args = argparse.Namespace(command="provision", tag="prod", endpoint="localhost:9200",
                              bucket=None, region=None, debug=False, creds_file="snapexe-creds.json")
    rc = oss.run_provision(args, boto3_module=boto3_module)
    assert rc == 0
    cfg = oss.load_config("prod")
    assert cfg["repo_type"] == "s3"
    assert cfg["bucket"].startswith("snapexe-prod-")
    assert cfg["repository"].startswith("snapexe-prod-repo-")
    assert "password" not in cfg and "secret_access_key" not in cfg


def test_run_snapshot_s3_requires_provisioned_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "snapexe-creds.json").write_text(
        '{"opensearch_source": {"username": "admin", "password": "secret"}}'
    )
    import argparse
    args = argparse.Namespace(
        command="snapshot", tag="prod", endpoint="localhost:9200", repo_type="s3",
        creds_file="snapexe-creds.json", indices=None, repository=None,
        snapshot_name="snapexe-prod-hotsnapshot-t",
        repo_path=None, dry_run=False, debug=False,
    )
    rc = oss.run_snapshot(args, session_factory=lambda u, p: MagicMock())
    assert rc == 2  # no provisioned config


def test_run_snapshot_s3_registers_from_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "snapexe-creds.json").write_text(
        '{"opensearch_source": {"username": "admin", "password": "secret"}}'
    )
    oss.save_config("prod", {
        "tag": "prod", "endpoint": "https://h:9200", "repo_type": "s3",
        "bucket": "snapexe-prod-a1b2", "region": "us-east-1",
        "repository": "snapexe-prod-repo-a1b2",
    })
    session = MagicMock()
    session.get.side_effect = [
        _json_response([{"component": "repository-s3"}]),  # ensure_repository_s3 check
        _json_response([{"index": "logs"}]),
        _json_response({"logs": {"settings": {"index.store.type": "fs"}}}),
        _json_response({"data_streams": []}),
        _json_response({"snapshots": []}),
    ]
    session.put.side_effect = [
        _json_response({"acknowledged": True}),          # register repo
        _json_response({"task": "task-1"}, status=202),  # snapshot
    ]
    import argparse
    args = argparse.Namespace(
        command="snapshot", tag="prod", endpoint="https://h:9200", repo_type="s3",
        creds_file="snapexe-creds.json", indices=None, repository=None,
        snapshot_name="snapexe-prod-hotsnapshot-t",
        repo_path=None, dry_run=False, debug=False,
    )
    rc = oss.run_snapshot(args, session_factory=lambda u, p: session)
    assert rc == 0
    cfg = oss.load_config("prod")
    assert cfg["repository"] == "snapexe-prod-repo-a1b2"  # stable name preserved
    assert cfg["bucket"] == "snapexe-prod-a1b2"


def test_parse_snapshot_auto_provision_flags():
    ns = oss.parse_arguments([
        "snapshot", "--tag", "local", "--endpoint", "https://h:9200", "--repo-type", "s3",
        "--auto-provision", "--install-container", "os-source",
    ])
    assert ns.auto_provision is True
    assert ns.install_container == "os-source"


def _auto_provision_args(**overrides):
    import argparse
    base = dict(
        command="snapshot", tag="local", endpoint="localhost:9200", repo_type="s3",
        creds_file="snapexe-creds.json", indices=None, repository=None,
        snapshot_name="snapexe-local-hotsnapshot-t", repo_path=None,
        auto_provision=True, install_container="os-source", source_containers=None,
        region=None, bucket=None, dry_run=False, debug=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _write_source_creds(tmp_path):
    (tmp_path / "snapexe-creds.json").write_text(
        '{"opensearch_source": {"username": "admin", "password": "secret"}}'
    )


def test_auto_provision_rejects_fs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_source_creds(tmp_path)
    rc = oss.run_snapshot(_auto_provision_args(repo_type="fs"),
                          session_factory=lambda u, p: MagicMock())
    assert rc == 2


def test_auto_provision_requires_install_container(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_source_creds(tmp_path)
    rc = oss.run_snapshot(_auto_provision_args(install_container=None),
                          session_factory=lambda u, p: MagicMock())
    assert rc == 2


def test_auto_provision_rejects_dry_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_source_creds(tmp_path)
    rc = oss.run_snapshot(_auto_provision_args(dry_run=True),
                          session_factory=lambda u, p: MagicMock())
    assert rc == 2


def test_auto_provision_happy_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_source_creds(tmp_path)

    installed = []
    monkeypatch.setattr(
        oss, "install_keystore_key",
        lambda container, name, value: installed.append((container, name)),
    )
    # single-node cluster: discovery maps the one node to its container
    monkeypatch.setattr(oss, "get_cluster_node_names", lambda s, e: ["os-source"], raising=False)
    monkeypatch.setattr(oss, "running_docker_container_names", lambda: {"os-source"}, raising=False)

    boto3_module = MagicMock()
    boto3_module.session.Session.return_value.region_name = "us-east-1"
    boto3_module.client.return_value.create_access_key.return_value = {
        "AccessKey": {"AccessKeyId": "AKIA1", "SecretAccessKey": "sek1"}
    }

    session = MagicMock()
    session.post.return_value = _json_response({}, status=200)  # reload_secure_settings
    session.get.side_effect = [
        _json_response([{"component": "repository-s3"}]),  # ensure_repository_s3 check
        _json_response([{"index": "logs"}]),
        _json_response({"logs": {"settings": {"index.store.type": "fs"}}}),
        _json_response({"data_streams": []}),
        _json_response({"snapshots": []}),
    ]
    session.put.side_effect = [
        _json_response({"acknowledged": True}),          # register repo
        _json_response({"task": "task-1"}, status=202),  # snapshot
    ]

    rc = oss.run_snapshot(_auto_provision_args(),
                          session_factory=lambda u, p: session,
                          boto3_module=boto3_module)
    assert rc == 0
    session.post.assert_called_once()  # reload happened
    assert [name for _, name in installed] == [
        "s3.client.default.access_key", "s3.client.default.secret_key",
    ]
    cfg = oss.load_config("local")
    assert cfg["repo_type"] == "s3"
    assert cfg["snapshot_name"] == "snapexe-local-hotsnapshot-t"
    assert "secret_access_key" not in cfg


def test_parse_snapshot_source_containers():
    ns = oss.parse_arguments([
        "snapshot", "--tag", "local", "--endpoint", "https://h:9200", "--repo-type", "s3",
        "--auto-provision", "--install-container", "os-source-hot",
        "--source-containers", "os-source-hot,os-source-warm",
    ])
    assert ns.source_containers == "os-source-hot,os-source-warm"


def test_auto_provision_installs_keys_on_all_source_containers(tmp_path, monkeypatch):
    # Explicit --source-containers overrides auto-discovery and is used verbatim.
    monkeypatch.chdir(tmp_path)
    _write_source_creds(tmp_path)

    installed = []
    monkeypatch.setattr(
        oss, "install_keystore_key",
        lambda container, name, value: installed.append((container, name)),
    )

    boto3_module = MagicMock()
    boto3_module.session.Session.return_value.region_name = "us-east-1"
    boto3_module.client.return_value.create_access_key.return_value = {
        "AccessKey": {"AccessKeyId": "AKIA1", "SecretAccessKey": "sek1"}
    }

    session = MagicMock()
    session.post.return_value = _json_response({}, status=200)  # reload_secure_settings
    session.get.side_effect = [
        _json_response([{"component": "repository-s3"}]),  # ensure_repository_s3 check
        _json_response([{"index": "logs"}]),
        _json_response({"logs": {"settings": {"index.store.type": "fs"}}}),
        _json_response({"data_streams": []}),
        _json_response({"snapshots": []}),
    ]
    session.put.side_effect = [
        _json_response({"acknowledged": True}),          # register repo
        _json_response({"task": "task-1"}, status=202),  # snapshot
    ]

    rc = oss.run_snapshot(
        _auto_provision_args(source_containers="os-source-hot,os-source-warm"),
        session_factory=lambda u, p: session,
        boto3_module=boto3_module,
    )
    assert rc == 0
    key = "s3.client.default.access_key"
    sec = "s3.client.default.secret_key"
    # Every source node gets both keys; the reload is a single cluster-wide call.
    assert installed == [
        ("os-source-hot", key), ("os-source-hot", sec),
        ("os-source-warm", key), ("os-source-warm", sec),
    ]
    session.post.assert_called_once()


def test_discover_source_containers_maps_all_cluster_nodes(monkeypatch):
    monkeypatch.setattr(oss, "get_cluster_node_names",
                        lambda s, e: ["os-source-hot", "os-source-warm"], raising=False)
    monkeypatch.setattr(oss, "running_docker_container_names",
                        lambda: {"os-source-hot", "os-source-warm", "os-dest-hot"}, raising=False)
    got = oss.discover_source_containers(MagicMock(), "https://h:9200", "os-source-hot")
    assert got == ["os-source-hot", "os-source-warm"]


def test_discover_source_containers_single_node(monkeypatch):
    monkeypatch.setattr(oss, "get_cluster_node_names", lambda s, e: ["os-solo"], raising=False)
    monkeypatch.setattr(oss, "running_docker_container_names", lambda: {"os-solo"}, raising=False)
    got = oss.discover_source_containers(MagicMock(), "https://h:9200", "os-solo")
    assert got == ["os-solo"]


def test_discover_source_containers_warns_on_unmapped_node(monkeypatch, caplog):
    monkeypatch.setattr(oss, "get_cluster_node_names",
                        lambda s, e: ["os-source-hot", "remote-node"], raising=False)
    monkeypatch.setattr(oss, "running_docker_container_names",
                        lambda: {"os-source-hot"}, raising=False)
    with caplog.at_level("WARNING"):
        got = oss.discover_source_containers(MagicMock(), "https://h:9200", "os-source-hot")
    assert got == ["os-source-hot"]           # only mappable nodes are keyed
    assert "remote-node" in caplog.text        # the un-mappable one is surfaced


def test_auto_provision_auto_discovers_all_nodes(tmp_path, monkeypatch):
    # No --source-containers: the tool discovers both nodes and keys each one.
    monkeypatch.chdir(tmp_path)
    _write_source_creds(tmp_path)

    monkeypatch.setattr(oss, "get_cluster_node_names",
                        lambda s, e: ["os-source-hot", "os-source-warm"], raising=False)
    monkeypatch.setattr(oss, "running_docker_container_names",
                        lambda: {"os-source-hot", "os-source-warm"}, raising=False)

    installed = []
    monkeypatch.setattr(
        oss, "install_keystore_key",
        lambda container, name, value: installed.append((container, name)),
    )

    boto3_module = MagicMock()
    boto3_module.session.Session.return_value.region_name = "us-east-1"
    boto3_module.client.return_value.create_access_key.return_value = {
        "AccessKey": {"AccessKeyId": "AKIA1", "SecretAccessKey": "sek1"}
    }

    session = MagicMock()
    session.post.return_value = _json_response({}, status=200)  # reload_secure_settings
    session.get.side_effect = [
        _json_response([{"component": "repository-s3"}]),  # ensure_repository_s3 check
        _json_response([{"index": "logs"}]),
        _json_response({"logs": {"settings": {"index.store.type": "fs"}}}),
        _json_response({"data_streams": []}),
        _json_response({"snapshots": []}),
    ]
    session.put.side_effect = [
        _json_response({"acknowledged": True}),          # register repo
        _json_response({"task": "task-1"}, status=202),  # snapshot
    ]

    rc = oss.run_snapshot(
        _auto_provision_args(install_container="os-source-hot", source_containers=None),
        session_factory=lambda u, p: session,
        boto3_module=boto3_module,
    )
    assert rc == 0
    key = "s3.client.default.access_key"
    sec = "s3.client.default.secret_key"
    assert installed == [
        ("os-source-hot", key), ("os-source-hot", sec),
        ("os-source-warm", key), ("os-source-warm", sec),
    ]
    session.post.assert_called_once()  # single cluster-wide reload


def test_nodes_missing_repository_s3_all_present():
    session = MagicMock()
    session.get.return_value = _json_response([
        {"name": "n1", "component": "repository-s3"},
        {"name": "n1", "component": "opensearch-sql"},
        {"name": "n2", "component": "repository-s3"},
    ])
    assert oss.nodes_missing_repository_s3(session, "https://h:9200") == []


def test_nodes_missing_repository_s3_detects_partial():
    # The bug the old any-node check missed: present on n1, absent on n2.
    session = MagicMock()
    session.get.return_value = _json_response([
        {"name": "n1", "component": "repository-s3"},
        {"name": "n2", "component": "opensearch-sql"},
    ])
    assert oss.nodes_missing_repository_s3(session, "https://h:9200") == ["n2"]


def test_nodes_missing_repository_s3_unknown_on_error():
    session = MagicMock()
    session.get.return_value = _json_response({}, status=500)
    assert oss.nodes_missing_repository_s3(session, "https://h:9200") is None


def test_ensure_repository_s3_present_is_noop(monkeypatch):
    monkeypatch.setattr(oss, "nodes_missing_repository_s3", lambda s, e: [], raising=False)
    assert oss.ensure_repository_s3(MagicMock(), "https://h:9200", "os-source") is True


def test_ensure_repository_s3_unknown_proceeds(monkeypatch):
    monkeypatch.setattr(oss, "nodes_missing_repository_s3", lambda s, e: None, raising=False)
    assert oss.ensure_repository_s3(MagicMock(), "https://h:9200", None) is True


def test_ensure_repository_s3_missing_unmappable_node_fails(monkeypatch):
    monkeypatch.setattr(oss, "nodes_missing_repository_s3",
                        lambda s, e: ["remote-node"], raising=False)
    monkeypatch.setattr(oss, "running_docker_container_names", lambda: set())
    assert oss.ensure_repository_s3(MagicMock(), "https://h:9200", None) is False


def test_ensure_repository_s3_installs_single_node(monkeypatch):
    states = iter([["os-solo"], []])  # missing before, none after install+restart
    monkeypatch.setattr(oss, "nodes_missing_repository_s3", lambda s, e: next(states), raising=False)
    monkeypatch.setattr(oss, "running_docker_container_names", lambda: {"os-solo"})
    calls = []
    monkeypatch.setattr(oss, "install_repository_s3_plugin", lambda c: calls.append(("install", c)))
    monkeypatch.setattr(oss, "restart_container", lambda c: calls.append(("restart", c)))
    monkeypatch.setattr(oss, "wait_for_cluster", lambda s, e: True)
    assert oss.ensure_repository_s3(MagicMock(), "https://h:9200", "os-solo") is True
    assert calls == [("install", "os-solo"), ("restart", "os-solo")]


def test_ensure_repository_s3_installs_on_all_missing_nodes(monkeypatch):
    # Multi-node: plugin missing on both nodes -> install + restart both, then re-verify.
    states = iter([["os-source-hot", "os-source-warm"], []])
    monkeypatch.setattr(oss, "nodes_missing_repository_s3", lambda s, e: next(states), raising=False)
    monkeypatch.setattr(oss, "running_docker_container_names",
                        lambda: {"os-source-hot", "os-source-warm"})
    calls = []
    monkeypatch.setattr(oss, "install_repository_s3_plugin", lambda c: calls.append(("install", c)))
    monkeypatch.setattr(oss, "restart_container", lambda c: calls.append(("restart", c)))
    monkeypatch.setattr(oss, "wait_for_cluster", lambda s, e: True)
    assert oss.ensure_repository_s3(MagicMock(), "https://h:9200", "os-source-hot") is True
    assert calls == [
        ("install", "os-source-hot"), ("install", "os-source-warm"),
        ("restart", "os-source-hot"), ("restart", "os-source-warm"),
    ]


def test_register_repository_retries_transient_key_error(monkeypatch):
    monkeypatch.setattr(oss.time, "sleep", lambda _s: None)
    session = MagicMock()
    bad = MagicMock()
    bad.status_code = 500
    bad.text = "The AWS Access Key Id you provided does not exist in our records."
    good = MagicMock()
    good.status_code = 200
    good.json.return_value = {"acknowledged": True}
    session.put.side_effect = [bad, good]
    assert oss.register_repository(session, "https://h:9200", "repo", {"type": "s3"}) is True
    assert session.put.call_count == 2  # retried once after propagation error


def test_register_repository_no_retry_on_other_error(monkeypatch):
    monkeypatch.setattr(oss.time, "sleep", lambda _s: None)
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 500
    resp.text = "repository already exists"
    session.put.return_value = resp
    assert oss.register_repository(session, "https://h:9200", "repo", {}) is False
    assert session.put.call_count == 1  # non-transient -> no retry


def test_gather_searchable_remaps_returns_backing_pointers():
    session = MagicMock()
    settings_body = {
        "logs-searchable": {"settings": {
            "index.store.type": "remote_snapshot",
            "index.searchable_snapshot.repository": "snapexe-ss-repo-abcd",
            "index.searchable_snapshot.snapshot_id.name": "ss-snap",
            "index.searchable_snapshot.snapshot_id.uuid": "snapuuid123",
            "index.searchable_snapshot.index.id": "idxid456",
        }},
        "logs": {"settings": {"index.store.type": "fs"}},  # regular index - excluded
    }
    repo_body = {"snapexe-ss-repo-abcd": {"type": "s3", "settings": {
        "bucket": "snapexe-ss-abcd", "base_path": "snapexe-ss-repo-abcd", "region": "us-west-2",
    }}}
    snap_body = {"snapshots": [{"snapshot": "ss-snap", "indices": ["logs"]}]}
    session.get.side_effect = [
        _json_response(settings_body),  # _all/_settings
        _json_response(repo_body),      # _snapshot/{repo}
        _json_response(snap_body),      # _snapshot/{repo}/{snap}
    ]
    remaps = oss.gather_searchable_remaps(session, "https://h:9200")
    assert len(remaps) == 1
    m = remaps[0]
    assert m["name"] == "logs-searchable"
    assert m["source_index"] == "logs"
    assert m["backing_repository"] == "snapexe-ss-repo-abcd"
    assert m["backing_snapshot"] == "ss-snap"
    assert m["backing_snapshot_uuid"] == "snapuuid123"
    assert m["source_index_id"] == "idxid456"
    assert m["bucket"] == "snapexe-ss-abcd"
    assert m["region"] == "us-west-2"


def test_gather_searchable_remaps_empty_when_none():
    session = MagicMock()
    session.get.return_value = _json_response(
        {"logs": {"settings": {"index.store.type": "fs"}}}
    )
    assert oss.gather_searchable_remaps(session, "https://h:9200") == []
