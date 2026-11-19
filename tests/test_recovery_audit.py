"""恢复审计写路径失败测（先红后绿，禁止跳过）。

口径（乙路径，不与甲路径混用）：
- 恢复成功必须落审计：改态与审计行按乐观锁版本号一并落库，缺审计列则拒绝落库
- 补采列表、审计附页、源目录、交接卡、水位卡片五处同成功同审计
- 两路值班同版抢恢同一源须占追责锁，后交只记恢复审计争用
"""
import pytest
from fastapi.testclient import TestClient
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def login(c):
    """口令沿用本仓追责侧：现有账号登录，一路会话即一路值班席位。"""
    tok = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def freeze(c, h, source):
    r = c.post("/api/arrivals", headers=h, json={
        "source": source,
        "source_clock": "2026-11-19T08:00:00",
        "ingest_clock": "2026-11-19T12:00:00",  # 滞后 240 分，各源均违约冻结
    })
    assert r.status_code == 200 and r.json()["output_mode"] == "冻结"


def source_of(c, h, name):
    return [s for s in c.get("/api/sources", headers=h).json()["items"] if s["name"] == name][0]


def test_catchup_success_must_persist_sealed_audit():
    """审计失败测：恢复成功必须落审计，五处同成功同审计可追责对齐。"""
    c = TestClient(app)
    h = login(c)
    freeze(c, h, "订单源")

    r = c.post("/api/catchup/订单源", headers=h)
    assert r.status_code == 200
    recovery_id = r.json()["recovery_id"]

    # 审计附页：恢复审计卡已落档并封存，专册号在册
    audits = c.get("/api/recovery-audits", headers=h).json()
    cards = [a for a in audits["items"] if a["source"] == "订单源" and a["action"] == "恢复成功"]
    assert len(cards) == 1, "恢复成功必须落审计，成功无审计无法追责"
    card = cards[0]
    assert card["recovery_id"] == recovery_id
    assert card["sealed"] == 1, "落档后写入恢复审计卡并封存"
    assert card["ledger"], "审计专册号须在册"

    # 五处同成功同审计：补采列表、审计附页、源目录、交接卡、水位卡片
    catchups = [x for x in c.get("/api/catchups", headers=h).json()["items"] if x["source"] == "订单源"]
    assert [x["recovery_id"] for x in catchups] == [recovery_id], "补采列表须与审计对齐"
    src = source_of(c, h, "订单源")
    assert src["output_mode"] == "可写" and src["version"] == 1, "源目录须落恢复态与版本号"
    handovers = [x for x in c.get("/api/handover-cards", headers=h).json()["items"] if x["source"] == "订单源"]
    assert [x["recovery_id"] for x in handovers] == [recovery_id], "交接卡须与审计对齐"
    watermarks = [x for x in c.get("/api/watermark-cards", headers=h).json()["items"] if x["source"] == "订单源"]
    assert len(watermarks) == 1, "水位卡片须同成功落卡"
    assert watermarks[0]["recovery_id"] == recovery_id and watermarks[0]["version"] == 1


def test_concurrent_catchup_later_only_contention_audit():
    """双路抢恢失败测：两路值班同版抢恢同一源，后交只记恢复审计争用。"""
    c = TestClient(app)
    h1 = login(c)  # 值班席位一
    h2 = login(c)  # 值班席位二
    freeze(c, h1, "物流源")

    v = source_of(c, h1, "物流源")["version"]  # 两路同时读到同一版本号

    r1 = c.post("/api/catchup/物流源", headers=h1, json={"expected_version": v})
    assert r1.status_code == 200, "先交者占追责锁恢复成功"
    r2 = c.post("/api/catchup/物流源", headers=h2, json={"expected_version": v})
    assert r2.status_code == 409, "后交者不得再成功，只记恢复审计争用"

    catchups = [x for x in c.get("/api/catchups", headers=h1).json()["items"]
                if x["source"] == "物流源" and x["status"] == "成功"]
    assert len(catchups) == 1, "同一源只能成功恢复一次"

    audits = [a for a in c.get("/api/recovery-audits", headers=h1).json()["items"] if a["source"] == "物流源"]
    assert sorted(a["action"] for a in audits) == ["恢复审计争用", "恢复成功"], \
        "缺审计争用留痕则红"

    assert source_of(c, h1, "物流源")["version"] == v + 1, "乐观锁版本号只许推进一次"


def test_concurrent_catchup_two_threads_one_contention():
    """双路值班同时点同一源恢复：须占追责锁，恰一成一争用。"""
    import concurrent.futures
    c1, c2 = TestClient(app), TestClient(app)
    h1, h2 = login(c1), login(c2)
    freeze(c1, h1, "支付源")

    def go(c, h):
        return c.post("/api/catchup/支付源", headers=h)

    with concurrent.futures.ThreadPoolExecutor(2) as ex:
        codes = sorted(r.status_code for r in ex.map(lambda args: go(*args), [(c1, h1), (c2, h2)]))
    assert codes == [200, 409], "同时抢恢须恰一成功一争用"

    audits = [a for a in c1.get("/api/recovery-audits", headers=h1).json()["items"] if a["source"] == "支付源"]
    assert sorted(a["action"] for a in audits) == ["恢复审计争用", "恢复成功"]
    catchups = [x for x in c1.get("/api/catchups", headers=h1).json()["items"]
                if x["source"] == "支付源" and x["status"] == "成功"]
    assert len(catchups) == 1


def test_deviation_from_audit_card_rejected_and_audited():
    """偏离恢复审计卡即拒写并记下追责偏了。"""
    c = TestClient(app)
    h = login(c)
    freeze(c, h, "订单源")

    r = c.post("/api/catchup/订单源", headers=h, json={"restored_mode": "冻结"})
    assert r.status_code == 422 and "追责偏离" in r.json()["detail"]

    src = source_of(c, h, "订单源")
    assert src["output_mode"] == "冻结" and src["version"] == 0, "偏离拒写，恢复态不落"
    assert not [x for x in c.get("/api/catchups", headers=h).json()["items"] if x["source"] == "订单源"]
    audits = [a for a in c.get("/api/recovery-audits", headers=h).json()["items"] if a["source"] == "订单源"]
    assert [a["action"] for a in audits] == ["追责偏离"]


def test_missing_audit_column_refuses_to_persist():
    """缺审计列则拒绝落库恢复态不落（乙路径硬约束）。"""
    import sqlite3
    from app import DB_PATH
    c = TestClient(app)
    h = login(c)
    freeze(c, h, "订单源")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE recovery_audits")
    conn.commit(); conn.close()

    r = c.post("/api/catchup/订单源", headers=h)
    assert r.status_code == 500, "缺审计列必须拒绝落库"
    src = source_of(c, h, "订单源")
    assert src["output_mode"] == "冻结" and src["version"] == 0, "恢复态不落"
    assert not [x for x in c.get("/api/catchups", headers=h).json()["items"] if x["source"] == "订单源"], \
        "补采列表同滚，不留成功"


def test_audit_ledger_sealed_no_update_no_delete():
    """追责当天不得更换审计专册：审计卡封存，禁改禁删。"""
    import sqlite3
    from app import DB_PATH
    c = TestClient(app)
    h = login(c)
    freeze(c, h, "订单源")
    assert c.post("/api/catchup/订单源", headers=h).status_code == 200

    conn = sqlite3.connect(DB_PATH)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE recovery_audits SET ledger='换册' ")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM recovery_audits")
    conn.close()

    audits = c.get("/api/recovery-audits", headers=h).json()
    assert audits["mechanism"]["乐观锁"] == "生效", "看板须标乐观锁生效"
    assert audits["mechanism"]["席位校验"] == "未启用", "甲乙不混用，席位校验未启用"
    assert all(a["sealed"] == 1 and a["ledger"] for a in audits["items"])
