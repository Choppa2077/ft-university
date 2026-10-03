from fastapi.testclient import TestClient

from ftuni import config, dbnode


def make_client():
    config.RUN_DIR.mkdir(parents=True, exist_ok=True)
    config.write_registry({"db_primary": "db-a", "epoch": 1})
    return TestClient(dbnode.create_app(config.get("db-a", "baseline")))


def tx(c, tx_id, sql, params, min_epoch=0):
    return c.post("/tx", json={"tx_id": tx_id, "ops": [{"sql": sql, "params": params}], "min_epoch": min_epoch})


def test_seed_and_duplicate_tx_is_applied_once():
    with make_client() as c:
        assert c.get("/role").json()["role"] == "primary"
        n = c.post("/query", json={"sql": "SELECT COUNT(*) FROM students"}).json()["rows"][0][0]
        assert n == dbnode.N_STUDENTS
        sql = "INSERT INTO ledger VALUES (?,?,?,?)"
        r1 = tx(c, "t-1", sql, ["k1", "S0001", 100, 0])
        r2 = tx(c, "t-1", sql, ["k1", "S0001", 100, 0])       # client retried the same write
        assert r1.status_code == 200 and r2.json().get("duplicate") is True
        cnt = c.post("/query", json={"sql": "SELECT COUNT(*) FROM ledger WHERE payment_key='k1'"}).json()
        assert cnt["rows"][0][0] == 1


def test_fencing_rejects_old_epoch_and_bad_sql():
    with make_client() as c:
        assert tx(c, "t-2", "SELECT 1", [], min_epoch=5).status_code == 409
        assert tx(c, "t-3", "INSERT INTO nope VALUES (1)", []).status_code == 400
        assert c.post("/replicate", json={"epoch": 1, "entries": []}).status_code == 409
