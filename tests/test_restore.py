import json

import pytest
from unittest.mock import MagicMock

import restore


def _json_response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def _write_config(tag, config):
    with open(f"snapexe-{tag}-config.json", "w") as handle:
        json.dump(config, handle, indent=2)


def _write_creds(tmp_path, data):
    (tmp_path / "snapexe-creds.json").write_text(__import__("json").dumps(data))


def _restore_args(**overrides):
    import argparse
    base = dict(
        tag="prod", endpoint="https://target:9200",
        creds_file="snapexe-creds.json", indices=None, dry_run=False, debug=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_filter_restorable_excludes_system_keeps_ds():
    snapshot_indices = ["logs", ".kibana", ".ds-metrics-000001", "orders"]
    existing = []
    assert restore.filter_restorable_indices(snapshot_indices, existing) == [
        "logs", ".ds-metrics-000001", "orders"
    ]


def test_filter_restorable_excludes_existing():
    snapshot_indices = ["logs", "orders", "events"]
    existing = ["orders"]
    assert restore.filter_restorable_indices(snapshot_indices, existing) == ["logs", "events"]


def test_filter_restorable_preserves_order():
    snapshot_indices = ["c", "a", "b"]
    assert restore.filter_restorable_indices(snapshot_indices, []) == ["c", "a", "b"]


def test_get_existing_indices_returns_names():
    session = MagicMock()
    session.get.return_value = _json_response([{"index": "logs"}, {"index": "orders"}])
    assert restore.get_existing_indices(session, "https://h:9200") == ["logs", "orders"]


def test_get_existing_indices_empty_on_error():
    session = MagicMock()
    session.get.return_value = _json_response({}, status=500)
    assert restore.get_existing_indices(session, "https://h:9200") == []


def test_get_snapshot_indices_returns_list():
    session = MagicMock()
    session.get.return_value = _json_response(
        {"snapshots": [{"snapshot": "snap-1", "indices": ["logs", "orders"]}]}
    )
    assert restore.get_snapshot_indices(session, "https://h:9200", "repo-a", "snap-1") == [
        "logs", "orders"
    ]


def test_get_snapshot_indices_empty_on_error():
    session = MagicMock()
    session.get.return_value = _json_response({}, status=404)
    assert restore.get_snapshot_indices(session, "https://h:9200", "repo-a", "snap-1") == []


def test_get_snapshot_indices_empty_when_no_snapshots():
    session = MagicMock()
    session.get.return_value = _json_response({"snapshots": []})
    assert restore.get_snapshot_indices(session, "https://h:9200", "repo-a", "snap-1") == []


def test_restore_snapshot_success():
    session = MagicMock()
    session.post.return_value = _json_response({"accepted": True}, status=200)
    ok = restore.restore_snapshot(session, "https://h:9200", "repo-a", "snap-1", ["logs", "orders"])
    assert ok is True
    sent = session.post.call_args.kwargs["json"]
    assert sent["indices"] == "logs,orders"
    assert sent["include_global_state"] is False
    assert sent["ignore_unavailable"] is True


def test_restore_snapshot_rejects_empty_indices():
    session = MagicMock()
    ok = restore.restore_snapshot(session, "https://h:9200", "repo-a", "snap-1", [])
    assert ok is False
    session.post.assert_not_called()


def test_restore_snapshot_failure():
    session = MagicMock()
    session.post.return_value = _json_response({"error": "bad"}, status=500)
    ok = restore.restore_snapshot(session, "https://h:9200", "repo-a", "snap-1", ["logs"])
    assert ok is False


def test_parse_restore_args():
    ns = restore.parse_arguments(
        ["--tag", "prod", "--endpoint", "https://target:9200", "--indices", "a,b"]
    )
    assert ns.tag == "prod"
    assert ns.endpoint == "https://target:9200"
    assert ns.indices == "a,b"


def test_parse_restore_requires_endpoint():
    with pytest.raises(SystemExit):
        restore.parse_arguments(["--tag", "prod"])


def test_parse_restore_requires_tag():
    with pytest.raises(SystemExit):
        restore.parse_arguments(["--endpoint", "https://target:9200"])


def test_load_creds_file_reads_json(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"opensearch_dest": {"username": "admin", "password": "pw"}}')
    assert restore.load_creds_file(str(p)) == {"opensearch_dest": {"username": "admin", "password": "pw"}}


def test_load_creds_file_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        restore.load_creds_file(str(tmp_path / "nope.json"))


def test_load_creds_file_invalid_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(ValueError):
        restore.load_creds_file(str(p))


def test_load_creds_file_rejects_non_dict(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text("[]")
    with pytest.raises(ValueError):
        restore.load_creds_file(str(p))


def test_resolve_credentials_dest():
    fc = {"opensearch_dest": {"username": "dstuser", "password": "dstpw"}}
    assert restore.resolve_credentials(fc, "opensearch_dest") == ("dstuser", "dstpw")


def test_resolve_credentials_missing_raises():
    with pytest.raises(ValueError):
        restore.resolve_credentials({}, "opensearch_dest")


def test_parse_restore_creds_file_default():
    ns = restore.parse_arguments(["--tag", "prod", "--endpoint", "https://target:9200"])
    assert ns.creds_file == "snapexe-creds.json"


def test_parse_restore_rejects_user():
    with pytest.raises(SystemExit):
        restore.parse_arguments(["--tag", "prod", "--endpoint", "https://target:9200", "--user", "admin"])


def test_run_restore_missing_config_exits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    rc = restore.run_restore(_restore_args(), session_factory=lambda u, p: MagicMock())
    assert rc == 2


def test_run_restore_default_filters_existing_and_system(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_config("prod", {
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
    rc = restore.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 0
    sent = session.post.call_args.kwargs["json"]
    assert sent["indices"] == "logs"  # .kibana (system) and orders (existing) excluded


def test_run_restore_explicit_indices_skip_filter(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "fs",
        "repo_path": "/mnt/snap", "repository": "snapexe-prod-repo-a1b2",
        "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    session = MagicMock()
    session.put.return_value = _json_response({"acknowledged": True})
    session.post.return_value = _json_response({"accepted": True})
    rc = restore.run_restore(_restore_args(indices="logs,orders"), session_factory=lambda u, p: session)
    assert rc == 0
    session.get.assert_not_called()
    sent = session.post.call_args.kwargs["json"]
    assert sent["indices"] == "logs,orders"


def test_run_restore_nothing_to_restore_exits_0(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_config("prod", {
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
    rc = restore.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 0
    session.post.assert_not_called()


def test_run_restore_dry_run_no_mutation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "fs",
        "repo_path": "/mnt/snap", "repository": "snapexe-prod-repo-a1b2",
        "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    session = MagicMock()
    rc = restore.run_restore(_restore_args(dry_run=True), session_factory=lambda u, p: session)
    assert rc == 0
    session.put.assert_not_called()
    session.post.assert_not_called()


def test_run_restore_s3_registers_s3_body(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "s3",
        "bucket": "snapexe-prod-a1b2", "region": "us-east-1",
        "repository": "snapexe-prod-repo-a1b2", "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    session = MagicMock()
    session.put.return_value = _json_response({"acknowledged": True})
    session.get.side_effect = [
        _json_response([{"component": "repository-s3"}]),  # ensure_repository_s3 check
        _json_response({"snapshots": [{"indices": ["logs"]}]}),
        _json_response([]),
    ]
    session.post.return_value = _json_response({"accepted": True})
    rc = restore.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 0
    body = session.put.call_args.kwargs["json"]
    assert body["type"] == "s3"
    assert body["settings"]["bucket"] == "snapexe-prod-a1b2"
    assert body["settings"]["base_path"] == "snapexe-prod-repo-a1b2"
    assert body["settings"]["region"] == "us-east-1"


def test_run_restore_register_failure_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "fs",
        "repo_path": "/mnt/snap", "repository": "snapexe-prod-repo-a1b2",
        "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    session = MagicMock()
    session.put.return_value = _json_response({}, status=500)
    rc = restore.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 1
    session.post.assert_not_called()


def test_run_restore_snapshot_indices_empty_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "fs",
        "repo_path": "/mnt/snap", "repository": "snapexe-prod-repo-a1b2",
        "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    session = MagicMock()
    session.put.return_value = _json_response({"acknowledged": True})
    session.get.return_value = _json_response({"snapshots": []})
    rc = restore.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 1


def test_run_restore_post_failure_exits_1(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_config("prod", {
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
    rc = restore.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 1


def test_run_restore_missing_dest_block_exits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_source": {"username": "admin", "password": "secret"}})
    (tmp_path / "snapexe-prod-config.json").write_text(
        '{"tag": "prod", "endpoint": "https://src:9200", "repo_type": "fs",'
        ' "repo_path": "/mnt/snap", "repository": "snapexe-prod-repo-a1b2",'
        ' "snapshot_name": "snapexe-prod-hotsnapshot-t"}'
    )
    session = MagicMock()
    rc = restore.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 2  # opensearch_dest missing -> creds error


def test_parse_restore_install_container():
    ns = restore.parse_arguments(
        ["--tag", "prod", "--endpoint", "https://t:9200", "--install-container", "os-dest"]
    )
    assert ns.install_container == "os-dest"


def _write_s3_config(tag="prod"):
    _write_config(tag, {
        "tag": tag, "endpoint": "https://src:9200", "repo_type": "s3",
        "bucket": "snapexe-prod-a1b2", "region": "us-east-1",
        "repository": "snapexe-prod-repo-a1b2", "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })


def test_run_restore_install_container_rejects_fs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_config("prod", {
        "tag": "prod", "endpoint": "https://src:9200", "repo_type": "fs",
        "repo_path": "/mnt/snap", "repository": "snapexe-prod-repo-a1b2",
        "snapshot_name": "snapexe-prod-hotsnapshot-t",
    })
    rc = restore.run_restore(_restore_args(install_container="os-dest"),
                             session_factory=lambda u, p: MagicMock())
    assert rc == 2


def test_run_restore_install_container_dry_run_no_aws(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_s3_config()
    called = []
    monkeypatch.setattr(restore, "create_iam_user_with_keys", lambda *a, **k: called.append(1))
    session = MagicMock()
    rc = restore.run_restore(_restore_args(install_container="os-dest", dry_run=True),
                             session_factory=lambda u, p: session)
    assert rc == 0
    assert called == []  # dry run mints no keys
    session.put.assert_not_called()
    session.post.assert_not_called()


def test_run_restore_install_container_ingests_keystore(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_s3_config()

    installed = []
    monkeypatch.setattr(restore, "install_keystore_key",
                        lambda c, n, v: installed.append((c, n)))

    boto3_module = MagicMock()
    boto3_module.client.return_value.create_access_key.return_value = {
        "AccessKey": {"AccessKeyId": "AKIA1", "SecretAccessKey": "sek1"}
    }

    session = MagicMock()
    session.put.return_value = _json_response({"acknowledged": True})   # register repo
    session.get.side_effect = [
        _json_response([{"component": "repository-s3"}]),  # ensure_repository_s3 check
        _json_response({"snapshots": [{"indices": ["logs"]}]}),
        _json_response([]),
    ]
    session.post.side_effect = [
        _json_response({}, status=200),      # reload_secure_settings
        _json_response({"accepted": True}),  # restore
    ]

    rc = restore.run_restore(_restore_args(install_container="os-dest"),
                             session_factory=lambda u, p: session,
                             boto3_module=boto3_module)
    assert rc == 0
    assert [n for _, n in installed] == [
        "s3.client.default.access_key", "s3.client.default.secret_key",
    ]
    assert session.post.call_count == 2  # reload, then restore


def test_run_restore_s3_aborts_when_plugin_unavailable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_s3_config()
    monkeypatch.setattr(restore, "ensure_repository_s3", lambda s, e, c: False)
    session = MagicMock()
    rc = restore.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 1
    session.put.assert_not_called()  # never reached repo registration
