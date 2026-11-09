from fastapi.testclient import TestClient

from keepon.application import Application
from keepon.config import Settings
from keepon.server import create_app


def test_health_and_json_rpc(tmp_path):
    application = Application(Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace"))
    with TestClient(create_app(application)) as client:
        assert client.get("/health").json()["status"] == "ok"
        with client.websocket_connect("/rpc") as ws:
            ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "system.info", "params": {}})
            response = ws.receive_json()
            assert response["id"] == 1
            assert response["result"]["name"] == "KeepOn"


def test_rpc_errors_are_structured(tmp_path):
    application = Application(Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace"))
    with TestClient(create_app(application)) as client:
        with client.websocket_connect("/rpc") as ws:
            ws.send_json({"jsonrpc": "2.0", "id": "x", "method": "missing", "params": {}})
            assert ws.receive_json()["error"]["code"] == -32601
            ws.send_text("not json")
            assert ws.receive_json()["error"]["code"] == -32700


def test_acceptance_endpoint_requires_launcher_token(tmp_path, monkeypatch):
    token = "testing-" + "0" * 32
    monkeypatch.setenv("KEEPON_ACCEPTANCE_TOKEN", token)
    application = Application(Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace"))
    with TestClient(create_app(application)) as client:
        payload = {"method": "acceptance.review", "params": {"run_id": "missing", "criterion_id": "item", "decision": "pass"}}
        assert client.post("/acceptance/rpc", json=payload).status_code == 403
        headers = {"Authorization": f"Bearer {token}"}
        response = client.post("/acceptance/rpc", json=payload, headers=headers)
        assert response.json()["error"]["code"] == -32000
        assert client.post("/acceptance/rpc", json={"method": "system.restart", "params": {}}, headers=headers).status_code == 400
        with client.websocket_connect("/rpc") as ws:
            ws.send_json({"jsonrpc": "2.0", "id": 1, **payload, "params": {**payload["params"], "_access": token}})
            assert ws.receive_json()["error"]["code"] == -32003
