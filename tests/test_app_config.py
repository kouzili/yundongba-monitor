"""后台配置接口 —— 密钥不外传，且留空不会把已存的密钥清掉。

这台机器长期开机且局域网上还有别的容器，配置接口曾经把 secret / SendKey
明文回传给任何访问者。这组测试盯的就是这个。
"""
import json

import pytest

import app as backend


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "CONFIG_FILE", tmp_path / "config.json")
    backend.app.config["TESTING"] = True
    with backend.app.test_client() as c:
        yield c


def write_config(client, **fields):
    cfg = dict(backend.DEFAULT_CONFIG)
    cfg.update(fields)
    backend.save_config(cfg)


# ---- 不外传 ----

def test_get_config_never_returns_the_signing_secret(client):
    write_config(client, secret_api="REALSECRET")

    body = client.get("/api/config").get_json()

    assert "REALSECRET" not in json.dumps(body)
    assert "secret_api" not in body
    assert body["secret_api_set"] is True


def test_get_config_never_returns_the_feishu_webhook(client):
    write_config(client, feishu_webhook="https://open.feishu.cn/hook/TOKEN")

    body = client.get("/api/config").get_json()

    assert "TOKEN" not in json.dumps(body)
    assert body["feishu_webhook_set"] is True


def test_status_never_returns_secrets_either(client):
    write_config(client, secret_api="REALSECRET", appsessionid="SESSION",
                 feishu_secret="SIGNKEY")

    body = client.get("/api/status").get_json()

    dumped = json.dumps(body)
    assert "REALSECRET" not in dumped
    assert "SESSION" not in dumped
    assert "SIGNKEY" not in dumped


def test_unset_secret_is_reported_as_not_set(client):
    write_config(client)

    body = client.get("/api/config").get_json()

    assert body["secret_api_set"] is False
    assert body["feishu_webhook_set"] is False


def test_non_secret_config_is_still_visible(client):
    write_config(client, target_start=19, poll_interval=45,
                 stadiums={"1128": "唛恩东馆"})

    body = client.get("/api/config").get_json()

    assert body["target_start"] == 19
    assert body["poll_interval"] == 45
    assert body["stadiums"] == {"1128": "唛恩东馆"}


# ---- 写入语义 ----

def test_empty_secret_in_post_does_not_wipe_the_stored_one(client):
    write_config(client, secret_api="REALSECRET")

    client.post("/api/config", json={"secret_api": "", "target_start": 20})

    assert backend.load_config()["secret_api"] == "REALSECRET"
    assert backend.load_config()["target_start"] == 20


def test_non_empty_secret_in_post_replaces_the_stored_one(client):
    write_config(client, secret_api="OLD")

    client.post("/api/config", json={"secret_api": "NEW"})

    assert backend.load_config()["secret_api"] == "NEW"


def test_unknown_fields_are_not_written_into_the_config(client):
    write_config(client)

    client.post("/api/config", json={"bind_host": "0.0.0.0", "evil": "x"})

    stored = backend.load_config()
    assert "evil" not in stored
    # bind_host 不在可写字段里 —— 放开监听范围只能改文件，不能通过接口
    assert stored["bind_host"] == "127.0.0.1"


def test_missing_config_file_falls_back_to_defaults(client):
    body = client.get("/api/config").get_json()

    assert body["target_start"] == backend.DEFAULT_CONFIG["target_start"]
    assert body["secret_api_set"] is False


# ---- 默认监听范围 ----

def test_default_bind_host_is_loopback_only():
    assert backend.DEFAULT_CONFIG["bind_host"] == "127.0.0.1"
