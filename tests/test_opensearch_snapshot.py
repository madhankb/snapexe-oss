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


def test_resolve_credentials_from_env(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    assert oss.resolve_credentials(None) == ("admin", "secret")


def test_resolve_credentials_arg_overrides_env_user(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_USER", "envuser")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    assert oss.resolve_credentials("arguser") == ("arguser", "secret")


def test_resolve_credentials_prompts_password(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.delenv("OPENSEARCH_PASSWORD", raising=False)
    monkeypatch.setattr(oss.getpass, "getpass", lambda *a, **k: "prompted")
    assert oss.resolve_credentials(None) == ("admin", "prompted")


def test_resolve_credentials_no_prompt_raises(monkeypatch):
    monkeypatch.delenv("OPENSEARCH_USER", raising=False)
    monkeypatch.delenv("OPENSEARCH_PASSWORD", raising=False)
    with pytest.raises(ValueError):
        oss.resolve_credentials(None, allow_prompt=False)


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


def test_parse_snapshot_rejects_region():
    with pytest.raises(SystemExit):
        oss.parse_arguments(
            ["snapshot", "--tag", "prod", "--endpoint", "https://h:9200",
             "--repo-type", "s3", "--region", "us-east-1"]
        )


def test_parse_status_args():
    ns = oss.parse_arguments(["status", "--tag", "prod"])
    assert ns.command == "status"
    assert ns.tag == "prod"


def test_parse_snapshot_requires_repo_type():
    with pytest.raises(SystemExit):
        oss.parse_arguments(["snapshot", "--tag", "prod", "--endpoint", "https://h:9200"])


def _snapshot_args(tmp_path, **overrides):
    import argparse
    base = dict(
        command="snapshot", tag="prod", endpoint="localhost:9200", repo_type="fs",
        user="admin", indices=None, repository=None, snapshot_name="snapexe-prod-hotsnapshot-t",
        repo_path="/mnt/snap", dry_run=False, debug=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_run_snapshot_fs_happy_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")

    session = MagicMock()
    # _cat/indices, then _all/_settings, then _snapshot/_status (proceed check)
    session.get.side_effect = [
        _json_response([{"index": "logs"}]),
        _json_response({"logs": {"settings": {"index.store.type": "fs"}}}),
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
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    session = MagicMock()
    rc = oss.run_snapshot(_snapshot_args(tmp_path, dry_run=True), session_factory=lambda u, p: session)
    assert rc == 0
    session.put.assert_not_called()


def test_run_status_reports_percent(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
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
    args = argparse.Namespace(command="status", tag="prod", user="admin", endpoint=None, debug=False)
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
    boto3_module = MagicMock()
    boto3_module.session.Session.return_value.region_name = "us-east-1"
    iam = boto3_module.client.return_value
    iam.create_access_key.return_value = {
        "AccessKey": {"AccessKeyId": "AKIA1", "SecretAccessKey": "sek1"}
    }
    args = argparse.Namespace(
        command="provision", tag="prod", endpoint="localhost:9200",
        bucket=None, region=None, debug=False,
    )
    rc = oss.run_provision(args, boto3_module=boto3_module)
    assert rc == 0
    cfg = oss.load_config("prod")
    assert cfg["repo_type"] == "s3"
    assert cfg["bucket"].startswith("snapexe-prod-")
    assert cfg["repository"].startswith("snapexe-prod-repo-")
    assert "password" not in cfg and "secret_access_key" not in cfg


def test_run_snapshot_s3_requires_provisioned_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    import argparse
    args = argparse.Namespace(
        command="snapshot", tag="prod", endpoint="localhost:9200", repo_type="s3",
        user="admin", indices=None, repository=None, snapshot_name="snapexe-prod-hotsnapshot-t",
        repo_path=None, dry_run=False, debug=False,
    )
    rc = oss.run_snapshot(args, session_factory=lambda u, p: MagicMock())
    assert rc == 2  # no provisioned config


def test_run_snapshot_s3_registers_from_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    oss.save_config("prod", {
        "tag": "prod", "endpoint": "https://h:9200", "repo_type": "s3",
        "bucket": "snapexe-prod-a1b2", "region": "us-east-1",
        "repository": "snapexe-prod-repo-a1b2",
    })
    session = MagicMock()
    session.get.side_effect = [
        _json_response([{"index": "logs"}]),
        _json_response({"logs": {"settings": {"index.store.type": "fs"}}}),
        _json_response({"snapshots": []}),
    ]
    session.put.side_effect = [
        _json_response({"acknowledged": True}),          # register repo
        _json_response({"task": "task-1"}, status=202),  # snapshot
    ]
    import argparse
    args = argparse.Namespace(
        command="snapshot", tag="prod", endpoint="https://h:9200", repo_type="s3",
        user="admin", indices=None, repository=None, snapshot_name="snapexe-prod-hotsnapshot-t",
        repo_path=None, dry_run=False, debug=False,
    )
    rc = oss.run_snapshot(args, session_factory=lambda u, p: session)
    assert rc == 0
    cfg = oss.load_config("prod")
    assert cfg["repository"] == "snapexe-prod-repo-a1b2"  # stable name preserved
    assert cfg["bucket"] == "snapexe-prod-a1b2"


def test_filter_restorable_excludes_system_keeps_ds():
    snapshot_indices = ["logs", ".kibana", ".ds-metrics-000001", "orders"]
    existing = []
    assert oss.filter_restorable_indices(snapshot_indices, existing) == [
        "logs", ".ds-metrics-000001", "orders"
    ]


def test_filter_restorable_excludes_existing():
    snapshot_indices = ["logs", "orders", "events"]
    existing = ["orders"]
    assert oss.filter_restorable_indices(snapshot_indices, existing) == ["logs", "events"]


def test_filter_restorable_preserves_order():
    snapshot_indices = ["c", "a", "b"]
    assert oss.filter_restorable_indices(snapshot_indices, []) == ["c", "a", "b"]


def test_get_existing_indices_returns_names():
    session = MagicMock()
    session.get.return_value = _json_response([{"index": "logs"}, {"index": "orders"}])
    assert oss.get_existing_indices(session, "https://h:9200") == ["logs", "orders"]


def test_get_existing_indices_empty_on_error():
    session = MagicMock()
    session.get.return_value = _json_response({}, status=500)
    assert oss.get_existing_indices(session, "https://h:9200") == []


def test_get_snapshot_indices_returns_list():
    session = MagicMock()
    session.get.return_value = _json_response(
        {"snapshots": [{"snapshot": "snap-1", "indices": ["logs", "orders"]}]}
    )
    assert oss.get_snapshot_indices(session, "https://h:9200", "repo-a", "snap-1") == [
        "logs", "orders"
    ]


def test_get_snapshot_indices_empty_on_error():
    session = MagicMock()
    session.get.return_value = _json_response({}, status=404)
    assert oss.get_snapshot_indices(session, "https://h:9200", "repo-a", "snap-1") == []


def test_get_snapshot_indices_empty_when_no_snapshots():
    session = MagicMock()
    session.get.return_value = _json_response({"snapshots": []})
    assert oss.get_snapshot_indices(session, "https://h:9200", "repo-a", "snap-1") == []


def test_restore_snapshot_success():
    session = MagicMock()
    session.post.return_value = _json_response({"accepted": True}, status=200)
    ok = oss.restore_snapshot(session, "https://h:9200", "repo-a", "snap-1", ["logs", "orders"])
    assert ok is True
    sent = session.post.call_args.kwargs["json"]
    assert sent["indices"] == "logs,orders"
    assert sent["include_global_state"] is False
    assert sent["ignore_unavailable"] is True


def test_restore_snapshot_rejects_empty_indices():
    session = MagicMock()
    ok = oss.restore_snapshot(session, "https://h:9200", "repo-a", "snap-1", [])
    assert ok is False
    session.post.assert_not_called()


def test_restore_snapshot_failure():
    session = MagicMock()
    session.post.return_value = _json_response({"error": "bad"}, status=500)
    ok = oss.restore_snapshot(session, "https://h:9200", "repo-a", "snap-1", ["logs"])
    assert ok is False


def test_parse_restore_args():
    ns = oss.parse_arguments(
        ["restore", "--tag", "prod", "--endpoint", "https://target:9200", "--indices", "a,b"]
    )
    assert ns.command == "restore"
    assert ns.tag == "prod"
    assert ns.endpoint == "https://target:9200"
    assert ns.indices == "a,b"


def test_parse_restore_requires_endpoint():
    with pytest.raises(SystemExit):
        oss.parse_arguments(["restore", "--tag", "prod"])


def _restore_args(**overrides):
    import argparse
    base = dict(
        command="restore", tag="prod", endpoint="https://target:9200",
        user="admin", indices=None, dry_run=False, debug=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_run_restore_missing_config_exits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    rc = oss.run_restore(_restore_args(), session_factory=lambda u, p: MagicMock())
    assert rc == 2


def test_run_restore_default_filters_existing_and_system(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    oss.save_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "fs",
        "repo_path": "/mnt/snap", "repository": "snapexe-prod-repo-a1b2",
        "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    session = MagicMock()
    # register repo (PUT), then snapshot indices (GET), then existing indices (GET)
    session.put.return_value = _json_response({"acknowledged": True})
    session.get.side_effect = [
        _json_response({"snapshots": [{"indices": ["logs", ".kibana", "orders"]}]}),
        _json_response([{"index": "orders"}]),
    ]
    session.post.return_value = _json_response({"accepted": True})
    rc = oss.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 0
    sent = session.post.call_args.kwargs["json"]
    assert sent["indices"] == "logs"  # .kibana (system) and orders (existing) excluded


def test_run_restore_explicit_indices_skip_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    oss.save_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "fs",
        "repo_path": "/mnt/snap", "repository": "snapexe-prod-repo-a1b2",
        "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    session = MagicMock()
    session.put.return_value = _json_response({"acknowledged": True})
    session.post.return_value = _json_response({"accepted": True})
    rc = oss.run_restore(_restore_args(indices="logs,orders"), session_factory=lambda u, p: session)
    assert rc == 0
    # No discovery GETs when --indices given
    session.get.assert_not_called()
    sent = session.post.call_args.kwargs["json"]
    assert sent["indices"] == "logs,orders"


def test_run_restore_nothing_to_restore_exits_0(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    oss.save_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "fs",
        "repo_path": "/mnt/snap", "repository": "snapexe-prod-repo-a1b2",
        "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    session = MagicMock()
    session.put.return_value = _json_response({"acknowledged": True})
    session.get.side_effect = [
        _json_response({"snapshots": [{"indices": ["orders"]}]}),
        _json_response([{"index": "orders"}]),
    ]
    rc = oss.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 0
    session.post.assert_not_called()


def test_run_restore_dry_run_no_mutation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    oss.save_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "fs",
        "repo_path": "/mnt/snap", "repository": "snapexe-prod-repo-a1b2",
        "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    session = MagicMock()
    rc = oss.run_restore(_restore_args(dry_run=True), session_factory=lambda u, p: session)
    assert rc == 0
    session.put.assert_not_called()
    session.post.assert_not_called()


def test_run_restore_s3_registers_s3_body(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    oss.save_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "s3",
        "bucket": "snapexe-prod-a1b2", "region": "us-east-1",
        "repository": "snapexe-prod-repo-a1b2", "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    session = MagicMock()
    session.put.return_value = _json_response({"acknowledged": True})
    session.get.side_effect = [
        _json_response({"snapshots": [{"indices": ["logs"]}]}),
        _json_response([]),
    ]
    session.post.return_value = _json_response({"accepted": True})
    rc = oss.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 0
    body = session.put.call_args.kwargs["json"]
    assert body["type"] == "s3"
    assert body["settings"]["bucket"] == "snapexe-prod-a1b2"
    assert body["settings"]["base_path"] == "snapexe-prod-repo-a1b2"
    assert body["settings"]["region"] == "us-east-1"


def test_run_restore_register_failure_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    oss.save_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "fs",
        "repo_path": "/mnt/snap", "repository": "snapexe-prod-repo-a1b2",
        "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    session = MagicMock()
    session.put.return_value = _json_response({}, status=500)
    rc = oss.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 1
    session.post.assert_not_called()


def test_run_restore_snapshot_indices_empty_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    oss.save_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "fs",
        "repo_path": "/mnt/snap", "repository": "snapexe-prod-repo-a1b2",
        "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    session = MagicMock()
    session.put.return_value = _json_response({"acknowledged": True})
    session.get.return_value = _json_response({"snapshots": []})
    rc = oss.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 1


def test_run_restore_post_failure_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSEARCH_USER", "admin")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "secret")
    oss.save_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "fs",
        "repo_path": "/mnt/snap", "repository": "snapexe-prod-repo-a1b2",
        "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    session = MagicMock()
    session.put.return_value = _json_response({"acknowledged": True})
    session.get.side_effect = [
        _json_response({"snapshots": [{"indices": ["logs"]}]}),
        _json_response([]),
    ]
    session.post.return_value = _json_response({}, status=500)
    rc = oss.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 1
