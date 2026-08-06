from app.main import app


def test_health():
    client = app.test_client()
    res = client.get("/health")
    assert res.status_code == 200
    assert res.get_json()["status"] == "ok"


def test_index():
    client = app.test_client()
    res = client.get("/")
    assert res.status_code == 200
