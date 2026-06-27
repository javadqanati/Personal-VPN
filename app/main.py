from fastapi import FastAPI, Depends, HTTPException, Query, Body
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from passlib.context import CryptContext
from contextlib import asynccontextmanager
from urllib.parse import quote
import os, secrets, subprocess, json, asyncio, psycopg, uuid as uuid_lib
import qrcode, io, base64, threading, shutil, tempfile

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS_HASH = os.environ.get("ADMIN_PASS_HASH", "")
XRAY_API = os.environ.get("XRAY_API", "127.0.0.1:10085")
DB_DSN = os.environ.get("DB_DSN", "")
XRAY_CONFIG_PATH = "/xray-config/config.json"
RELOAD_SIGNAL_PATH = "/var/run/vpn-dashboard/reload-xray"
PUBLIC_DOMAIN = os.environ.get("PUBLIC_DOMAIN", "securetunnelforcomet.xyz")
PUBLIC_IP = os.environ.get("PUBLIC_IP", "92.4.216.155")
SNAPSHOT_INTERVAL = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBasic()
config_lock = threading.Lock()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    user_ok = secrets.compare_digest(credentials.username, ADMIN_USER)
    pass_ok = pwd_context.verify(credentials.password, ADMIN_PASS_HASH) if ADMIN_PASS_HASH else False
    if not (user_ok and pass_ok):
        raise HTTPException(401, "Invalid credentials", {"WWW-Authenticate": "Basic"})
    return credentials.username

def query_xray_stats():
    try:
        r = subprocess.run(
            ["xray", "api", "statsquery", f"--server={XRAY_API}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None

def parse_stat_name(name):
    parts = name.split(">>>")
    if len(parts) == 4 and parts[2] == "traffic":
        return {"kind": parts[0], "tag": parts[1], "direction": parts[3]}
    return None

def init_db():
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stat_snapshots (
                ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                kind        TEXT NOT NULL,
                tag         TEXT NOT NULL,
                direction   TEXT NOT NULL,
                value       BIGINT NOT NULL,
                PRIMARY KEY (ts, kind, tag, direction)
            );
            CREATE INDEX IF NOT EXISTS idx_ss_ts ON stat_snapshots (ts DESC);
            CREATE INDEX IF NOT EXISTS idx_ss_tag ON stat_snapshots (kind, tag, direction, ts DESC);
            CREATE TABLE IF NOT EXISTS vpn_users (
                id          SERIAL PRIMARY KEY,
                uuid        UUID NOT NULL UNIQUE,
                email       TEXT NOT NULL UNIQUE,
                label       TEXT,
                inbounds    TEXT[] NOT NULL DEFAULT ARRAY['vless-reality', 'vless-ws'],
                active      BOOLEAN NOT NULL DEFAULT TRUE,
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                revoked_at  TIMESTAMPTZ
            );
        """)
        conn.commit()

def take_snapshot():
    data = query_xray_stats()
    if not data or "stat" not in data:
        return
    rows = []
    for s in data["stat"]:
        p = parse_stat_name(s["name"])
        if p:
            rows.append((p["kind"], p["tag"], p["direction"], int(s.get("value", 0) or 0)))
    if not rows:
        return
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO stat_snapshots (kind, tag, direction, value) VALUES (%s, %s, %s, %s)",
            rows,
        )
        conn.commit()

async def snapshot_loop():
    while True:
        try:
            await asyncio.to_thread(take_snapshot)
        except Exception as e:
            print(f"snapshot error: {e}", flush=True)
        await asyncio.sleep(SNAPSHOT_INTERVAL)

def db_list_users(active_only=True):
    q = "SELECT id, uuid, email, label, inbounds, active, created_at, revoked_at FROM vpn_users"
    if active_only:
        q += " WHERE active = TRUE"
    q += " ORDER BY id"
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(q)
        rows = cur.fetchall()
    return [
        {
            "id": r[0], "uuid": str(r[1]), "email": r[2], "label": r[3],
            "inbounds": r[4], "active": r[5],
            "created_at": r[6].isoformat() if r[6] else None,
            "revoked_at": r[7].isoformat() if r[7] else None,
        }
        for r in rows
    ]

def db_create_user(email, label, inbounds):
    new_uuid = str(uuid_lib.uuid4())
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO vpn_users (uuid, email, label, inbounds) VALUES (%s, %s, %s, %s) RETURNING id",
            (new_uuid, email, label, inbounds),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
    return {"id": new_id, "uuid": new_uuid, "email": email, "label": label, "inbounds": inbounds}

def db_revoke_user(user_id):
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE vpn_users SET active = FALSE, revoked_at = NOW() WHERE id = %s AND active = TRUE RETURNING id",
            (user_id,),
        )
        row = cur.fetchone()
        conn.commit()
    return row is not None

def load_xray_config():
    with open(XRAY_CONFIG_PATH, "r") as f:
        return json.load(f)

def regenerate_xray_config():
    """Rewrite Xray config.json from the active users in the database.
    Validates the new config before swapping. Returns (ok: bool, message: str).
    """
    with config_lock:
        try:
            users = db_list_users(active_only=True)
            cfg = load_xray_config()

            # Rebuild clients[] for each VLESS inbound based on who's in that inbound's user list
            for inbound in cfg.get("inbounds", []):
                tag = inbound.get("tag")
                if not tag or inbound.get("protocol") != "vless":
                    continue
                wants_flow = (tag == "vless-reality")
                clients = []
                for u in users:
                    if tag not in u["inbounds"]:
                        continue
                    client = {
                        "id": u["uuid"],
                        "level": 0,
                        "email": u["email"],
                    }
                    if wants_flow:
                        client["flow"] = "xtls-rprx-vision"
                    clients.append(client)
                if "settings" not in inbound:
                    inbound["settings"] = {}
                inbound["settings"]["clients"] = clients
                inbound["settings"]["decryption"] = "none"

            # Strip "log" only for validation (paths exist on host, not in container)
            cfg_for_test = {k: v for k, v in cfg.items() if k != "log"}
            tmp_dir = os.path.dirname(XRAY_CONFIG_PATH)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", dir=tmp_dir, delete=False
            ) as tmp:
                json.dump(cfg_for_test, tmp, indent=2)
                tmp_path = tmp.name

            # Validate the new file before swapping
            r = subprocess.run(
                ["xray", "run", "-test", "-config", tmp_path],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                os.unlink(tmp_path)
                return False, "(see above)"
                return False, f"Config validation failed: {r.stderr or r.stdout}"

            # Back up old config, then atomic-swap
            backup_path = XRAY_CONFIG_PATH + ".prev"
            try:
                shutil.copy2(XRAY_CONFIG_PATH, backup_path)
            except Exception:
                pass
            with open(tmp_path, "w") as f:
                json.dump(cfg, f, indent=2)
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, XRAY_CONFIG_PATH)

            # Signal the host's path-watcher to restart Xray
            try:
                with open(RELOAD_SIGNAL_PATH, "w") as f:
                    f.write(str(int(asyncio.get_event_loop().time() * 1000)) if False else "reload")
            except Exception as e:
                return True, f"Config written but reload signal failed: {e}"

            return True, "Config regenerated and reload signaled"

        except Exception as e:
            return False, f"Regeneration error: {type(e).__name__}: {e}"

def build_vless_link(user, inbound_tag):
    """Construct a VLESS share link for the given user and inbound."""
    cfg = load_xray_config()
    inbound = next((i for i in cfg.get("inbounds", []) if i.get("tag") == inbound_tag), None)
    if not inbound:
        return None
    label = quote(user.get("label") or user.get("email") or "user")

    if inbound_tag == "vless-reality":
        rs = inbound["streamSettings"]["realitySettings"]
        sni = rs["serverNames"][0]
        # Public key is not stored on the server side; we derive it via xray x25519 lookup is not possible.
        # Instead we read it from an env var that the operator can set, OR we keep a stored copy.
        pbk = os.environ.get("REALITY_PUBLIC_KEY", "")
        sid = rs["shortIds"][0] if rs.get("shortIds") else ""
        return (
            f"vless://{user['uuid']}@{PUBLIC_IP}:8443"
            f"?encryption=none&flow=xtls-rprx-vision&security=reality"
            f"&sni={sni}&fp=chrome&pbk={pbk}&sid={sid}&type=tcp#{label}"
        )

    if inbound_tag == "vless-ws":
        ws_path = quote(inbound["streamSettings"]["wsSettings"]["path"])
        return (
            f"vless://{user['uuid']}@{PUBLIC_DOMAIN}:443"
            f"?encryption=none&security=tls&sni={PUBLIC_DOMAIN}"
            f"&type=ws&path={ws_path}&host={PUBLIC_DOMAIN}#{label}-ws"
        )
    return None

def qr_png_base64(text):
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    take_snapshot()
    task = asyncio.create_task(snapshot_loop())
    yield
    task.cancel()

app = FastAPI(title="VPN Dashboard", lifespan=lifespan)

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.get("/api/me")
def me(user: str = Depends(verify_admin)):
    return {"user": user}

@app.get("/api/stats")
def stats(user: str = Depends(verify_admin)):
    raw = query_xray_stats()
    if not raw:
        raise HTTPException(500, "xray api unreachable")
    inbounds, outbounds, users = {}, {}, {}
    for s in raw.get("stat", []):
        p = parse_stat_name(s["name"])
        if not p:
            continue
        value = int(s.get("value", 0) or 0)
        if p["kind"] == "inbound":
            inbounds.setdefault(p["tag"], {"uplink": 0, "downlink": 0})[p["direction"]] = value
        elif p["kind"] == "outbound":
            outbounds.setdefault(p["tag"], {"uplink": 0, "downlink": 0})[p["direction"]] = value
        elif p["kind"] == "user":
            users.setdefault(p["tag"], {"uplink": 0, "downlink": 0})[p["direction"]] = value
    return {"inbounds": inbounds, "outbounds": outbounds, "users": users}

@app.get("/api/history")
def history(
    user: str = Depends(verify_admin),
    hours: int = Query(24, ge=1, le=720),
    tag: str = Query("vless-reality"),
    kind: str = Query("inbound"),
):
    with psycopg.connect(DB_DSN) as conn, conn.cursor() as cur:
        cur.execute("""
            WITH ordered AS (
                SELECT ts, direction, value,
                       LAG(value) OVER (PARTITION BY direction ORDER BY ts) AS prev_value
                FROM stat_snapshots
                WHERE kind = %s AND tag = %s
                  AND ts > NOW() - (%s || ' hours')::interval
            )
            SELECT ts, direction, GREATEST(value - prev_value, 0) AS delta
            FROM ordered
            WHERE prev_value IS NOT NULL
            ORDER BY ts ASC
        """, (kind, tag, str(hours)))
        rows = cur.fetchall()
    series = {"uplink": [], "downlink": []}
    for ts, direction, delta in rows:
        if direction in series:
            series[direction].append({"ts": ts.isoformat(), "bytes": int(delta)})
    return {"tag": tag, "kind": kind, "hours": hours, "series": series}

@app.get("/api/users")
def list_users(user: str = Depends(verify_admin), include_revoked: bool = False):
    return {"users": db_list_users(active_only=not include_revoked)}

@app.post("/api/users")
def create_user(payload: dict = Body(...), user: str = Depends(verify_admin)):
    email = (payload.get("email") or "").strip()
    label = (payload.get("label") or "").strip() or None
    inbounds = payload.get("inbounds") or ["vless-reality", "vless-ws"]
    if not email:
        raise HTTPException(400, "email required")
    if not isinstance(inbounds, list) or not all(t in ("vless-reality", "vless-ws") for t in inbounds):
        raise HTTPException(400, "inbounds must be a list of valid tags")
    try:
        new_user = db_create_user(email, label, inbounds)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "email already exists")
    ok, msg = regenerate_xray_config()
    if not ok:
        # Roll back the DB insert if config regen fails
        db_revoke_user(new_user["id"])
        raise HTTPException(500, msg)
    return {"user": new_user, "reload": msg}

@app.delete("/api/users/{user_id}")
def revoke_user(user_id: int, user: str = Depends(verify_admin)):
    if not db_revoke_user(user_id):
        raise HTTPException(404, "user not found or already revoked")
    ok, msg = regenerate_xray_config()
    if not ok:
        raise HTTPException(500, msg)
    return {"revoked": user_id, "reload": msg}

@app.get("/api/users/{user_id}/link")
def user_link(user_id: int, inbound: str = "vless-reality", user: str = Depends(verify_admin)):
    users = db_list_users(active_only=False)
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        raise HTTPException(404, "user not found")
    link = build_vless_link(target, inbound)
    if not link:
        raise HTTPException(400, "unknown inbound")
    return {"link": link, "qr": qr_png_base64(link)}

app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
