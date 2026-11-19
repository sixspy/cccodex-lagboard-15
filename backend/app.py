
import hashlib, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)  # 占追责锁时容许等待先交者
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL,
          recovery_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS recovery_audits(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          recovery_id TEXT NOT NULL,
          ledger TEXT NOT NULL,
          source TEXT NOT NULL,
          action TEXT NOT NULL,
          actor TEXT NOT NULL,
          from_mode TEXT,
          to_mode TEXT,
          version_before INTEGER,
          version_after INTEGER,
          note TEXT NOT NULL,
          sealed INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS handover_cards(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          recovery_id TEXT NOT NULL,
          source TEXT NOT NULL,
          from_mode TEXT NOT NULL,
          to_mode TEXT NOT NULL,
          operator TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watermark_cards(
          source TEXT PRIMARY KEY,
          recovery_id TEXT NOT NULL,
          latest_source_clock TEXT,
          version INTEGER NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS recovery_audits_sealed_no_update
        BEFORE UPDATE ON recovery_audits
        BEGIN SELECT RAISE(ABORT,'审计专册封存不可更换'); END;
        CREATE TRIGGER IF NOT EXISTS recovery_audits_sealed_no_delete
        BEFORE DELETE ON recovery_audits
        BEGIN SELECT RAISE(ABORT,'审计专册封存不可更换'); END;
        """)
        # 存量库迁移：追责仓区库文件原地补列，不另起库
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)")}
        if "version" not in cols:
            conn.execute("ALTER TABLE sources ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(catchups)")}
        if "recovery_id" not in cols:
            conn.execute("ALTER TABLE catchups ADD COLUMN recovery_id TEXT NOT NULL DEFAULT ''")
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)", [
                ("订单源", 15, "可写"), ("支付源", 20, "降级只读"), ("物流源", 30, "冻结"),
            ])
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("支付源","2026-11-16T08:00:00","2026-11-16T08:35:00",35,"告警"))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("物流源","2026-11-16T07:00:00","2026-11-16T09:10:00",130,"违约"))

def level_of(lag, sla):
    if lag <= sla: return "正常"
    if lag <= sla * 2: return "告警"
    return "违约"

def mode_of(level):
    return {"正常":"可写", "告警":"降级只读", "违约":"冻结"}[level]

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]}

class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str

@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
        if not src: raise HTTPException(404, "未知源")
        # lag by minute rough parse
        def parse(t):
            return datetime.fromisoformat(t)
        lag = int((parse(body.ingest_clock) - parse(body.source_clock)).total_seconds() // 60)
        if lag < 0: lag = 0
        lv = level_of(lag, src["sla_minutes"])
        mode = mode_of(lv)
        c.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                  (body.source, body.source_clock, body.ingest_clock, lag, lv))
        c.execute("UPDATE sources SET output_mode=? WHERE name=?", (mode, body.source))
        return {"lag_minutes": lag, "level": lv, "output_mode": mode}

@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall()]}

LEDGER_PREFIX = "恢复审计专册"
# 恢复看板须标出席位校验与乐观锁何者生效：本仓走乙路径，不混甲路径
AUDIT_MECHANISM = {"路径": "乙路径", "乐观锁": "生效", "席位校验": "未启用"}

class CatchupIn(BaseModel):
    expected_version: int | None = None
    restored_mode: str = "可写"
    note: str = "补齐后恢复可写"

def _write_audit(conn, *, recovery_id, ledger, source, action, actor,
                 from_mode, to_mode, version_before, version_after, note, now):
    """落档恢复审计卡并封存（sealed=1，触发器禁改禁删，当日专册不更换）。"""
    cur = conn.execute(
        """INSERT INTO recovery_audits(
             recovery_id,ledger,source,action,actor,from_mode,to_mode,
             version_before,version_after,note,sealed,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,1,?)""",
        (recovery_id, ledger, source, action, actor, from_mode, to_mode,
         version_before, version_after, note, now))
    if cur.rowcount != 1 or cur.lastrowid is None:
        raise RuntimeError("缺审计列，拒绝落库")

@app.post("/api/catchup/{source}")
def catchup(source: str, body: CatchupIn | None = None, authorization: str | None = Header(default=None)):
    user = require_user(authorization)  # 口令沿用本仓追责侧：现有 Bearer 会话
    body = body or CatchupIn()
    now = datetime.now().isoformat(timespec="seconds")
    ledger = f"{LEDGER_PREFIX}-{now[:10]}"  # 追责当天不得更换审计专册
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")  # 追责锁：同一源的恢复写串行占锁
        src = conn.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
        if not src:
            conn.rollback()
            raise HTTPException(404, "未知源")
        rid = f"R-{now[:10].replace('-', '')}-{secrets.token_hex(4)}"
        # 偏离恢复审计卡即拒写并记下追责偏了
        if body.restored_mode != "可写" or not body.note.strip():
            _write_audit(conn, recovery_id=rid, ledger=ledger, source=source, action="追责偏离",
                         actor=user, from_mode=src["output_mode"], to_mode=body.restored_mode,
                         version_before=src["version"], version_after=src["version"],
                         note=body.note.strip() or "空备注偏离", now=now)
            conn.commit()
            raise HTTPException(422, "追责偏离：恢复写偏离恢复审计卡，已拒写并记审计")
        # 后交者（版本号不符或源已被先交恢复）只记恢复审计争用
        stale = body.expected_version is not None and body.expected_version != src["version"]
        if stale or src["output_mode"] == "可写":
            _write_audit(conn, recovery_id=rid, ledger=ledger, source=source, action="恢复审计争用",
                         actor=user, from_mode=src["output_mode"], to_mode=src["output_mode"],
                         version_before=src["version"], version_after=src["version"],
                         note="后交重复恢复，占追责锁留痕不改态", now=now)
            conn.commit()
            raise HTTPException(409, "恢复审计争用：该源已被先交恢复，后交只记争用")
        v = src["version"]
        # 乙路径：恢复改态与审计行按乐观锁版本号一并落库
        cur = conn.execute("UPDATE sources SET output_mode='可写', version=? WHERE name=? AND version=?",
                           (v + 1, source, v))
        if cur.rowcount != 1:
            conn.rollback()
            raise HTTPException(409, "恢复审计争用：版本号已推进")
        conn.execute("INSERT INTO catchups(source,status,restored_mode,note,created_at,recovery_id) VALUES (?,?,?,?,?,?)",
                     (source, "成功", "可写", body.note, now, rid))
        _write_audit(conn, recovery_id=rid, ledger=ledger, source=source, action="恢复成功",
                     actor=user, from_mode=src["output_mode"], to_mode="可写",
                     version_before=v, version_after=v + 1, note=body.note, now=now)
        conn.execute("INSERT INTO handover_cards(recovery_id,source,from_mode,to_mode,operator,created_at) VALUES (?,?,?,?,?,?)",
                     (rid, source, src["output_mode"], "可写", user, now))
        wm = conn.execute("SELECT MAX(source_clock) m FROM arrivals WHERE source=?", (source,)).fetchone()["m"]
        conn.execute("""INSERT INTO watermark_cards(source,recovery_id,latest_source_clock,version,updated_at)
                        VALUES (?,?,?,?,?)
                        ON CONFLICT(source) DO UPDATE SET
                          recovery_id=excluded.recovery_id,
                          latest_source_clock=excluded.latest_source_clock,
                          version=excluded.version,
                          updated_at=excluded.updated_at""",
                     (source, rid, wm, v + 1, now))
        # 缺审计列则拒绝落库恢复态不落：提交前核验封存审计行在册
        n = conn.execute(
            "SELECT COUNT(*) c FROM recovery_audits WHERE recovery_id=? AND action='恢复成功' AND sealed=1",
            (rid,)).fetchone()["c"]
        if n != 1:
            conn.rollback()
            raise HTTPException(500, "缺审计列，拒绝落库")
        conn.commit()
        return {"ok": True, "output_mode": "可写", "recovery_id": rid,
                "version": v + 1, "audit": "恢复成功", "ledger": ledger}
    except HTTPException:
        raise
    except Exception:
        conn.rollback()  # 任一环缺失则五处同滚，恢复态不落
        raise HTTPException(500, "缺审计列，拒绝落库")
    finally:
        conn.close()

@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}

@app.get("/api/recovery-audits")
def recovery_audits(authorization: str | None = Header(default=None)):
    """审计附页：追责入口挂在恢复看板，不另起追责台。"""
    require_user(authorization)
    with connect() as c:
        items = [dict(r) for r in c.execute("SELECT * FROM recovery_audits ORDER BY id DESC").fetchall()]
    return {"mechanism": AUDIT_MECHANISM, "items": items}

@app.get("/api/handover-cards")
def handover_cards(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM handover_cards ORDER BY id DESC").fetchall()]}

@app.get("/api/watermark-cards")
def watermark_cards(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM watermark_cards ORDER BY source").fetchall()]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
