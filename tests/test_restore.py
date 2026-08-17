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


def test_parse_restore_accepts_secret_id():
    args = restore.parse_arguments(["--tag", "prod", "--endpoint", "https://h:9200",
                                    "--secret-id", "snapexe/opensearch"])
    assert args.secret_id == "snapexe/opensearch"


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
    # Even with --indices, restore peeks at the snapshot to detect data streams (none here).
    session.get.return_value = _json_response({"snapshots": [{"indices": ["logs", "orders"]}]})
    session.post.return_value = _json_response({"accepted": True})
    rc = restore.run_restore(_restore_args(indices="logs,orders"), session_factory=lambda u, p: session)
    assert rc == 0
    sent = session.post.call_args.kwargs["json"]
    assert sent["indices"] == "logs,orders"  # given list used verbatim, no filter


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
    # Node discovery hits _cat/nodes + docker; stub it to the anchor for a deterministic test.
    monkeypatch.setattr(restore, "discover_source_containers", lambda s, e, c: [c])

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


def test_ingest_dest_keystore_includes_searchable_backing_buckets(monkeypatch):
    captured = {}

    def fake_create(iam, user, buckets):
        captured["user"] = user
        captured["buckets"] = buckets
        return {"access_key_id": "AKIA", "secret_access_key": "sek"}

    monkeypatch.setattr(restore, "create_iam_user_with_keys", fake_create)
    monkeypatch.setattr(restore, "install_keystore_key", lambda *a, **k: None)
    monkeypatch.setattr(restore, "reload_secure_settings", lambda *a, **k: True)

    config = {
        "tag": "nightly",
        "bucket": "snapexe-nightly-224c",
        "searchable_snapshots": [
            {"bucket": "snapexe-ss-vsqw"},
            {"bucket": "snapexe-ss-vsqw"},  # duplicate -> should be deduped
        ],
    }
    ok = restore.ingest_dest_keystore(
        config, MagicMock(), "https://localhost:9201", "os-dest-hot",
        boto3_module=MagicMock(),
    )
    assert ok is True
    assert captured["user"] == "snapexe-nightly-user"
    # tag bucket first, then the (deduped) searchable backing bucket
    assert captured["buckets"][0] == "snapexe-nightly-224c"
    assert "snapexe-ss-vsqw" in captured["buckets"]
    assert captured["buckets"].count("snapexe-ss-vsqw") == 1


def test_run_restore_s3_aborts_when_plugin_unavailable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_s3_config()
    monkeypatch.setattr(restore, "ensure_repository_s3", lambda s, e, c: False)
    session = MagicMock()
    rc = restore.run_restore(_restore_args(), session_factory=lambda u, p: session)
    assert rc == 1
    session.put.assert_not_called()  # never reached repo registration


def _ds_snapshot_get_responses():
    return [
        _json_response({"snapshots": [{
            "indices": [".ds-logs-datastream-000001", ".ds-logs-datastream-000002"],
            "data_streams": ["logs-datastream"],
        }]}),
        _json_response([]),  # existing indices on target
    ]


def test_run_restore_datastream_without_cert_aborts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_s3_config()
    monkeypatch.setattr(restore, "ensure_repository_s3", lambda s, e, c: True)
    basic, cert = [], []
    monkeypatch.setattr(restore, "restore_snapshot", lambda *a, **k: basic.append(1) or True)
    monkeypatch.setattr(restore, "restore_via_admin_cert", lambda *a, **k: cert.append(1) or True)
    session = MagicMock()
    session.put.return_value = _json_response({"acknowledged": True})
    session.get.side_effect = _ds_snapshot_get_responses()
    rc = restore.run_restore(_restore_args(), session_factory=lambda u, p: session)  # no --install-container
    assert rc == 1  # data stream present but no cert reachable -> abort
    assert basic == [] and cert == []  # neither restore path attempted


def test_run_restore_datastream_uses_admin_cert(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    _write_s3_config()
    monkeypatch.setattr(restore, "ensure_repository_s3", lambda s, e, c: True)
    monkeypatch.setattr(restore, "ingest_dest_keystore", lambda *a, **k: True)
    monkeypatch.setattr(restore, "discover_source_containers", lambda s, e, c: [c])
    basic, cert = [], []
    monkeypatch.setattr(restore, "restore_snapshot", lambda *a, **k: basic.append(1) or True)
    monkeypatch.setattr(restore, "restore_via_admin_cert",
                        lambda container, repo, snap, targets, **k: cert.append((container, targets)) or True)
    session = MagicMock()
    session.put.return_value = _json_response({"acknowledged": True})
    session.get.side_effect = _ds_snapshot_get_responses()
    rc = restore.run_restore(_restore_args(install_container="os-dest"), session_factory=lambda u, p: session)
    assert rc == 0
    assert basic == []  # basic-auth path not used for a data stream
    assert cert == [("os-dest", ["logs-datastream"])]  # restored the data stream by name via cert


_REMAP = {
    "name": "logs-searchable", "source_index": "logs",
    "backing_repository": "snapexe-ss-repo-abcd", "backing_snapshot": "ss-snap",
    "bucket": "snapexe-ss-abcd", "base_path": "snapexe-ss-repo-abcd", "region": "us-west-2",
}


def test_remap_reuses_existing_creds_no_mint(monkeypatch):
    # register probe succeeds with the dest's existing creds -> no key minted/installed
    monkeypatch.setattr(restore, "register_repository", lambda s, e, name, body, **kw: True)
    minted, installed = [], []
    monkeypatch.setattr(restore, "create_iam_user_with_keys", lambda *a, **k: minted.append(1) or {})
    monkeypatch.setattr(restore, "install_keystore_key", lambda c, k, v: installed.append(c))
    session = MagicMock()
    session.post.return_value = _json_response({"snapshot": {"snapshot": "ss-snap"}})
    ok = restore.remap_searchable_snapshots(
        [dict(_REMAP)], session, "https://target:9201", ["os-dest-hot"], [], MagicMock())
    assert ok is True
    assert minted == [] and installed == []  # existing creds worked
    body = session.post.call_args.kwargs["json"]
    assert body["storage_type"] == "remote_snapshot"
    assert body["indices"] == "logs" and body["rename_replacement"] == "logs-searchable"


def test_remap_mints_and_installs_when_creds_missing(monkeypatch):
    # probe register fails, then succeeds after minting + installing the backing key
    n = {"c": 0}
    def fake_register(s, e, name, body, **kw):
        n["c"] += 1
        return n["c"] > 1  # first call (probe) fails, second (after mint) succeeds
    monkeypatch.setattr(restore, "register_repository", fake_register)
    monkeypatch.setattr(restore, "reload_secure_settings", lambda s, e: True)
    installed = []
    monkeypatch.setattr(restore, "install_keystore_key", lambda c, k, v: installed.append((c, k)))
    boto3_module = MagicMock()
    boto3_module.client.return_value.create_access_key.return_value = {
        "AccessKey": {"AccessKeyId": "AKIA1", "SecretAccessKey": "sek1"}}
    session = MagicMock()
    session.post.return_value = _json_response({"snapshot": {"snapshot": "ss-snap"}})
    ok = restore.remap_searchable_snapshots(
        [dict(_REMAP)], session, "https://target:9201",
        ["os-dest-hot", "os-dest-warm"], [], boto3_module)
    assert ok is True
    assert [c for c, _ in installed] == [
        "os-dest-hot", "os-dest-hot", "os-dest-warm", "os-dest-warm"]
    assert session.post.called


def test_remap_skips_existing(monkeypatch):
    monkeypatch.setattr(restore, "register_repository", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
    session = MagicMock()
    ok = restore.remap_searchable_snapshots(
        [dict(_REMAP)], session, "https://target:9201",
        ["os-dest-hot"], existing_indices=["logs-searchable"], boto3_module=MagicMock())
    assert ok is True
    session.post.assert_not_called()  # skipped: target already present, register never attempted


def test_remap_fails_when_creds_missing_and_no_containers(monkeypatch):
    monkeypatch.setattr(restore, "register_repository", lambda s, e, name, body, **kw: False)  # probe fails
    ok = restore.remap_searchable_snapshots(
        [dict(_REMAP)], MagicMock(), "https://t:9201", [], existing_indices=[], boto3_module=MagicMock())
    assert ok is False


def test_remap_searchable_snapshots_no_remaps_is_noop():
    assert restore.remap_searchable_snapshots(
        [], MagicMock(), "https://t:9201", [], existing_indices=[], boto3_module=MagicMock(),
    ) is True


def test_compute_recovery_progress_aggregates_and_filters():
    rows = [
        {"index": "logs", "stage": "done", "bytes_percent": "100.0%"},
        {"index": "users", "stage": "index", "bytes_percent": "60.0%"},
        {"index": ".opendistro_security", "stage": "done", "bytes_percent": "100.0%"},   # system - skip
        {"index": ".plugins-ml-config", "stage": "done", "bytes_percent": "100.0%"},     # system - skip
        {"index": ".ds-logs-datastream-000001", "stage": "done", "bytes_percent": "100.0%"},
        {"index": ".ds-logs-datastream-000002", "stage": "index", "bytes_percent": "40.0%"},  # rolls up, min wins
        {"index": "orders", "stage": "init", "bytes_percent": "0.0%"},
    ]
    p = restore.compute_recovery_progress(rows)
    by = {i["name"]: i for i in p["indices"]}
    assert set(by) == {"logs", "users", "logs-datastream", "orders"}   # system indices excluded
    assert by["logs"]["state"] == "done" and by["logs"]["percent"] == 100
    assert by["users"]["state"] == "recovering" and by["users"]["percent"] == 60
    assert by["logs-datastream"]["percent"] == 40 and by["logs-datastream"]["state"] == "recovering"  # min across backing
    assert by["orders"]["state"] == "pending" and by["orders"]["percent"] == 0
    assert p["done"] == 1 and p["total"] == 4


def test_run_restore_progress_reports_and_skips_system(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_creds(tmp_path, {"opensearch_dest": {"username": "admin", "password": "secret"}})
    session = MagicMock()
    session.get.return_value = _json_response([
        {"index": "bulk50k", "stage": "done", "bytes_percent": "100.0%"},
        {"index": "users", "stage": "index", "bytes_percent": "50.0%"},
        {"index": ".opendistro_security", "stage": "done", "bytes_percent": "100.0%"},
        {"index": ".ds-logs-datastream-000001", "stage": "done", "bytes_percent": "100.0%"},
    ])
    args = _restore_args(endpoint="https://localhost:9201", tag="day1culture",
                         secret_id=None, progress=True)
    rc = restore.run_restore_progress(args, session_factory=lambda u, p: session)
    assert rc == 0
    out = capsys.readouterr().out
    assert "2/3 indices done" in out              # bulk50k + logs-datastream done, users recovering
    assert "bulk50k" in out and "logs-datastream" in out
    assert "users: 50%" in out
    assert ".opendistro_security" not in out      # system index filtered out


def test_parse_restore_progress_flag():
    ns = restore.parse_arguments(["--tag", "d", "--endpoint", "https://h:9201", "--progress"])
    assert ns.progress is True
    assert ns.endpoint == "https://h:9201"
