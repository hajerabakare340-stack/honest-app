import os, hmac, hashlib, json, sqlite3, time, threading
from contextlib import contextmanager
from urllib.parse import parse_qsl
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

E = os.environ
BOT_TOKEN = E["BOT_TOKEN"]
BOT, APP = E.get("BOT_USERNAME", "honest_app_bot"), E.get("APP_SHORT", "market")
ADMINS = {int(x) for x in E.get("ADMIN_IDS", "8922764894").split(",") if x.strip()}
PAY_INFO = E.get("PAY_INFO", "Telebirr: +251941470214 (Geleta Adem)")
SUPPORT = E.get("SUPPORT", "withdrawwal")
CHANNEL = E.get("CHANNEL", "honestsapp")
FEE_PCT, REF_PCT, MIN_W = 15, 20, 100  # commission %, referrer share of commission %, min withdraw

app = FastAPI()
db = sqlite3.connect("app.db", check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, balance INTEGER DEFAULT 0, ref_by INTEGER);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, descr TEXT, price INTEGER, client INTEGER, worker INTEGER, status TEXT DEFAULT 'open', result TEXT);
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER, amount INTEGER, kind TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS deposits(id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER, amount INTEGER, ref TEXT UNIQUE, status TEXT DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER, amount INTEGER, method TEXT, account TEXT, status TEXT DEFAULT 'pending');
""")
lock = threading.Lock()


@contextmanager
def tx():
    with lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def credit(uid, amt, kind):  # uid 0 = platform (commission)
    if uid:
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amt, uid))
    db.execute("INSERT INTO ledger(user,amount,kind,ts) VALUES(?,?,?,?)", (uid, amt, kind, int(time.time())))


def bal(uid):
    return db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]


def err(msg, code=400):
    raise HTTPException(code, msg)


def user(init):
    try:
        d = dict(parse_qsl(init, strict_parsing=True))
        h = d.pop("hash")
        chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), h):
            raise ValueError
        if time.time() - int(d["auth_date"]) > 86400:
            raise ValueError
        u, sp = json.loads(d["user"]), d.get("start_param", "")
    except Exception:
        err("Auth failed", 401)
    with lock:
        if not db.execute("SELECT 1 FROM users WHERE id=?", (u["id"],)).fetchone():
            ref = int(sp[4:]) if sp.startswith("ref_") and sp[4:].isdigit() else None
            if ref == u["id"] or not db.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
                ref = None
            db.execute("INSERT INTO users(id,name,ref_by) VALUES(?,?,?)", (u["id"], u.get("first_name", "User"), ref))
    return dict(db.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone())


def admin(init):
    u = user(init)
    if u["id"] not in ADMINS:
        err("Admin only", 403)
    return u


class Dep(BaseModel):
    amount: int = Field(gt=0, le=1_000_000)
    ref: str = Field(min_length=4, max_length=64)


class Wd(BaseModel):
    amount: int = Field(gt=0, le=1_000_000)
    method: str = Field(min_length=2, max_length=20)
    account: str = Field(min_length=4, max_length=64)


class NewTask(BaseModel):
    title: str = Field(min_length=3, max_length=80)
    descr: str = Field(min_length=3, max_length=1000)
    price: int = Field(ge=10, le=100_000)


class Result(BaseModel):
    result: str = Field(min_length=1, max_length=5000)


@app.get("/")
def index():
    return FileResponse("index.html")


@app.get("/api/me")
def me(x_init: str = Header("")):
    u = user(x_init)
    return {**u, "is_admin": u["id"] in ADMINS, "pay_info": PAY_INFO, "support": SUPPORT, "channel": CHANNEL, "min_w": MIN_W, "fee": FEE_PCT,
            "link": f"https://t.me/{BOT}/{APP}?startapp=ref_{u['id']}",
            "refs": db.execute("SELECT COUNT(*) FROM users WHERE ref_by=?", (u["id"],)).fetchone()[0],
            "ledger": [dict(r) for r in db.execute("SELECT amount,kind,ts FROM ledger WHERE user=? ORDER BY id DESC LIMIT 20", (u["id"],))],
            "pending": [dict(r) for r in db.execute(
                "SELECT 'deposit' t,amount,status FROM deposits WHERE user=? UNION ALL SELECT 'withdraw',amount,status FROM withdrawals WHERE user=? ORDER BY 3 LIMIT 10", (u["id"], u["id"]))]}


@app.post("/api/deposit")
def deposit(b: Dep, x_init: str = Header("")):
    u = user(x_init)
    try:
        with tx():
            db.execute("INSERT INTO deposits(user,amount,ref) VALUES(?,?,?)", (u["id"], b.amount, b.ref.strip()))
    except sqlite3.IntegrityError:
        err("Lakkoofsi transaction kun duraan galmaa'eera")
    return {"ok": True}


@app.post("/api/withdraw")
def withdraw(b: Wd, x_init: str = Header("")):
    u = user(x_init)
    if b.method != "Telebirr":
        err("Telebirr qofa")
    if b.amount < MIN_W:
        err(f"Xiqqaan {MIN_W} ETB")
    with tx():
        if bal(u["id"]) < b.amount:
            err("Balance gahaa miti")
        credit(u["id"], -b.amount, "withdraw_hold")
        db.execute("INSERT INTO withdrawals(user,amount,method,account) VALUES(?,?,?,?)", (u["id"], b.amount, b.method, b.account))
    return {"ok": True}


@app.get("/api/tasks")
def tasks(x_init: str = Header("")):
    u = user(x_init)
    q = "SELECT t.*, c.name client_name FROM tasks t JOIN users c ON c.id=t.client "
    rows = db.execute(q + "WHERE t.status='open' OR t.client=? OR t.worker=? ORDER BY t.id DESC LIMIT 100", (u["id"], u["id"]))
    return [dict(r) for r in rows]


@app.post("/api/tasks")
def new_task(b: NewTask, x_init: str = Header("")):
    u = user(x_init)
    with tx():
        if bal(u["id"]) < b.price:
            err("Balance gahaa miti. Duraan deposit godhi")
        credit(u["id"], -b.price, "escrow")
        db.execute("INSERT INTO tasks(title,descr,price,client) VALUES(?,?,?,?)", (b.title, b.descr, b.price, u["id"]))
    return {"ok": True}


def get_task(tid, **need):
    t = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        err("Hojiin hin argamne", 404)
    for k, v in need.items():
        if t[k] != v:
            err("Hojii kana gochuu hin dandeessu")
    return t


@app.post("/api/tasks/{tid}/accept")
def accept(tid: int, x_init: str = Header("")):
    u = user(x_init)
    with tx():
        t = get_task(tid, status="open")
        if t["client"] == u["id"]:
            err("Hojii ofii fudhachuu hin dandeessu")
        db.execute("UPDATE tasks SET status='in_progress', worker=? WHERE id=?", (u["id"], tid))
    return {"ok": True}


@app.post("/api/tasks/{tid}/submit")
def submit(tid: int, b: Result, x_init: str = Header("")):
    u = user(x_init)
    with tx():
        get_task(tid, status="in_progress", worker=u["id"])
        db.execute("UPDATE tasks SET status='submitted', result=? WHERE id=?", (b.result, tid))
    return {"ok": True}


@app.post("/api/tasks/{tid}/approve")
def approve(tid: int, x_init: str = Header("")):
    u = user(x_init)
    with tx():
        t = get_task(tid, status="submitted", client=u["id"])
        fee = t["price"] * FEE_PCT // 100
        ref = db.execute("SELECT ref_by FROM users WHERE id=?", (t["worker"],)).fetchone()[0]
        bonus = fee * REF_PCT // 100 if ref else 0
        credit(t["worker"], t["price"] - fee, "task_paid")
        if bonus:
            credit(ref, bonus, "referral_bonus")
        credit(0, fee - bonus, "commission")
        db.execute("UPDATE tasks SET status='completed' WHERE id=?", (tid,))
    return {"ok": True}


@app.post("/api/tasks/{tid}/cancel")
def cancel(tid: int, x_init: str = Header("")):
    u = user(x_init)
    with tx():
        t = get_task(tid, status="open", client=u["id"])
        credit(u["id"], t["price"], "escrow_refund")
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (tid,))
    return {"ok": True}


@app.get("/api/admin")
def admin_view(x_init: str = Header("")):
    admin(x_init)
    return {"deposits": [dict(r) for r in db.execute("SELECT * FROM deposits WHERE status='pending'")],
            "withdrawals": [dict(r) for r in db.execute("SELECT * FROM withdrawals WHERE status='pending'")],
            "commission": db.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE user=0").fetchone()[0],
            "users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0]}


@app.post("/api/admin/deposit/{did}/{act}")
def adm_dep(did: int, act: str, x_init: str = Header("")):
    admin(x_init)
    with tx():
        d = db.execute("SELECT * FROM deposits WHERE id=? AND status='pending'", (did,)).fetchone()
        if not d:
            err("Hin argamne", 404)
        if act == "ok":
            credit(d["user"], d["amount"], "deposit")
        db.execute("UPDATE deposits SET status=? WHERE id=?", ("approved" if act == "ok" else "rejected", did))
    return {"ok": True}


@app.post("/api/admin/withdraw/{wid}/{act}")
def adm_wd(wid: int, act: str, x_init: str = Header("")):
    admin(x_init)
    with tx():
        w = db.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wid,)).fetchone()
        if not w:
            err("Hin argamne", 404)
        if act != "ok":
            credit(w["user"], w["amount"], "withdraw_refund")
        db.execute("UPDATE withdrawals SET status=? WHERE id=?", ("paid" if act == "ok" else "rejected", wid))
    return {"ok": True}import os, hmac, hashlib, json, sqlite3, time, threading
from contextlib import contextmanager
from urllib.parse import parse_qsl
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

E = os.environ
BOT_TOKEN = E["BOT_TOKEN"]
BOT, APP = E.get("BOT_USERNAME", "honest_app_bot"), E.get("APP_SHORT", "market")
ADMINS = {int(x) for x in E.get("ADMIN_IDS", "8922764894").split(",") if x.strip()}
PAY_INFO = E.get("PAY_INFO", "Telebirr: +251941470214 (Geleta Adem)")
SUPPORT = E.get("SUPPORT", "withdrawwal")
CHANNEL = E.get("CHANNEL", "honestsapp")
FEE_PCT, REF_PCT, MIN_W = 15, 20, 100  # commission %, referrer share of commission %, min withdraw

app = FastAPI()
db = sqlite3.connect("app.db", check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, balance INTEGER DEFAULT 0, ref_by INTEGER);
CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, descr TEXT, price INTEGER, client INTEGER, worker INTEGER, status TEXT DEFAULT 'open', result TEXT);
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER, amount INTEGER, kind TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS deposits(id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER, amount INTEGER, ref TEXT UNIQUE, status TEXT DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT, user INTEGER, amount INTEGER, method TEXT, account TEXT, status TEXT DEFAULT 'pending');
""")
lock = threading.Lock()


@contextmanager
def tx():
    with lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def credit(uid, amt, kind):  # uid 0 = platform (commission)
    if uid:
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amt, uid))
    db.execute("INSERT INTO ledger(user,amount,kind,ts) VALUES(?,?,?,?)", (uid, amt, kind, int(time.time())))


def bal(uid):
    return db.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]


def err(msg, code=400):
    raise HTTPException(code, msg)


def user(init):
    try:
        d = dict(parse_qsl(init, strict_parsing=True))
        h = d.pop("hash")
        chk = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, chk.encode(), hashlib.sha256).hexdigest(), h):
            raise ValueError
        if time.time() - int(d["auth_date"]) > 86400:
            raise ValueError
        u, sp = json.loads(d["user"]), d.get("start_param", "")
    except Exception:
        err("Auth failed", 401)
    with lock:
        if not db.execute("SELECT 1 FROM users WHERE id=?", (u["id"],)).fetchone():
            ref = int(sp[4:]) if sp.startswith("ref_") and sp[4:].isdigit() else None
            if ref == u["id"] or not db.execute("SELECT 1 FROM users WHERE id=?", (ref,)).fetchone():
                ref = None
            db.execute("INSERT INTO users(id,name,ref_by) VALUES(?,?,?)", (u["id"], u.get("first_name", "User"), ref))
    return dict(db.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone())


def admin(init):
    u = user(init)
    if u["id"] not in ADMINS:
        err("Admin only", 403)
    return u


class Dep(BaseModel):
    amount: int = Field(gt=0, le=1_000_000)
    ref: str = Field(min_length=4, max_length=64)


class Wd(BaseModel):
    amount: int = Field(gt=0, le=1_000_000)
    method: str = Field(min_length=2, max_length=20)
    account: str = Field(min_length=4, max_length=64)


class NewTask(BaseModel):
    title: str = Field(min_length=3, max_length=80)
    descr: str = Field(min_length=3, max_length=1000)
    price: int = Field(ge=10, le=100_000)


class Result(BaseModel):
    result: str = Field(min_length=1, max_length=5000)


@app.get("/")
def index():
    return FileResponse("index.html")


@app.get("/api/me")
def me(x_init: str = Header("")):
    u = user(x_init)
    return {**u, "is_admin": u["id"] in ADMINS, "pay_info": PAY_INFO, "support": SUPPORT, "channel": CHANNEL, "min_w": MIN_W, "fee": FEE_PCT,
            "link": f"https://t.me/{BOT}/{APP}?startapp=ref_{u['id']}",
            "refs": db.execute("SELECT COUNT(*) FROM users WHERE ref_by=?", (u["id"],)).fetchone()[0],
            "ledger": [dict(r) for r in db.execute("SELECT amount,kind,ts FROM ledger WHERE user=? ORDER BY id DESC LIMIT 20", (u["id"],))],
            "pending": [dict(r) for r in db.execute(
                "SELECT 'deposit' t,amount,status FROM deposits WHERE user=? UNION ALL SELECT 'withdraw',amount,status FROM withdrawals WHERE user=? ORDER BY 3 LIMIT 10", (u["id"], u["id"]))]}


@app.post("/api/deposit")
def deposit(b: Dep, x_init: str = Header("")):
    u = user(x_init)
    try:
        with tx():
            db.execute("INSERT INTO deposits(user,amount,ref) VALUES(?,?,?)", (u["id"], b.amount, b.ref.strip()))
    except sqlite3.IntegrityError:
        err("Lakkoofsi transaction kun duraan galmaa'eera")
    return {"ok": True}


@app.post("/api/withdraw")
def withdraw(b: Wd, x_init: str = Header("")):
    u = user(x_init)
    if b.method != "Telebirr":
        err("Telebirr qofa")
    if b.amount < MIN_W:
        err(f"Xiqqaan {MIN_W} ETB")
    with tx():
        if bal(u["id"]) < b.amount:
            err("Balance gahaa miti")
        credit(u["id"], -b.amount, "withdraw_hold")
        db.execute("INSERT INTO withdrawals(user,amount,method,account) VALUES(?,?,?,?)", (u["id"], b.amount, b.method, b.account))
    return {"ok": True}


@app.get("/api/tasks")
def tasks(x_init: str = Header("")):
    u = user(x_init)
    q = "SELECT t.*, c.name client_name FROM tasks t JOIN users c ON c.id=t.client "
    rows = db.execute(q + "WHERE t.status='open' OR t.client=? OR t.worker=? ORDER BY t.id DESC LIMIT 100", (u["id"], u["id"]))
    return [dict(r) for r in rows]


@app.post("/api/tasks")
def new_task(b: NewTask, x_init: str = Header("")):
    u = user(x_init)
    with tx():
        if bal(u["id"]) < b.price:
            err("Balance gahaa miti. Duraan deposit godhi")
        credit(u["id"], -b.price, "escrow")
        db.execute("INSERT INTO tasks(title,descr,price,client) VALUES(?,?,?,?)", (b.title, b.descr, b.price, u["id"]))
    return {"ok": True}


def get_task(tid, **need):
    t = db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        err("Hojiin hin argamne", 404)
    for k, v in need.items():
        if t[k] != v:
            err("Hojii kana gochuu hin dandeessu")
    return t


@app.post("/api/tasks/{tid}/accept")
def accept(tid: int, x_init: str = Header("")):
    u = user(x_init)
    with tx():
        t = get_task(tid, status="open")
        if t["client"] == u["id"]:
            err("Hojii ofii fudhachuu hin dandeessu")
        db.execute("UPDATE tasks SET status='in_progress', worker=? WHERE id=?", (u["id"], tid))
    return {"ok": True}


@app.post("/api/tasks/{tid}/submit")
def submit(tid: int, b: Result, x_init: str = Header("")):
    u = user(x_init)
    with tx():
        get_task(tid, status="in_progress", worker=u["id"])
        db.execute("UPDATE tasks SET status='submitted', result=? WHERE id=?", (b.result, tid))
    return {"ok": True}


@app.post("/api/tasks/{tid}/approve")
def approve(tid: int, x_init: str = Header("")):
    u = user(x_init)
    with tx():
        t = get_task(tid, status="submitted", client=u["id"])
        fee = t["price"] * FEE_PCT // 100
        ref = db.execute("SELECT ref_by FROM users WHERE id=?", (t["worker"],)).fetchone()[0]
        bonus = fee * REF_PCT // 100 if ref else 0
        credit(t["worker"], t["price"] - fee, "task_paid")
        if bonus:
            credit(ref, bonus, "referral_bonus")
        credit(0, fee - bonus, "commission")
        db.execute("UPDATE tasks SET status='completed' WHERE id=?", (tid,))
    return {"ok": True}


@app.post("/api/tasks/{tid}/cancel")
def cancel(tid: int, x_init: str = Header("")):
    u = user(x_init)
    with tx():
        t = get_task(tid, status="open", client=u["id"])
        credit(u["id"], t["price"], "escrow_refund")
        db.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (tid,))
    return {"ok": True}


@app.get("/api/admin")
def admin_view(x_init: str = Header("")):
    admin(x_init)
    return {"deposits": [dict(r) for r in db.execute("SELECT * FROM deposits WHERE status='pending'")],
            "withdrawals": [dict(r) for r in db.execute("SELECT * FROM withdrawals WHERE status='pending'")],
            "commission": db.execute("SELECT COALESCE(SUM(amount),0) FROM ledger WHERE user=0").fetchone()[0],
            "users": db.execute("SELECT COUNT(*) FROM users").fetchone()[0]}


@app.post("/api/admin/deposit/{did}/{act}")
def adm_dep(did: int, act: str, x_init: str = Header("")):
    admin(x_init)
    with tx():
        d = db.execute("SELECT * FROM deposits WHERE id=? AND status='pending'", (did,)).fetchone()
        if not d:
            err("Hin argamne", 404)
        if act == "ok":
            credit(d["user"], d["amount"], "deposit")
        db.execute("UPDATE deposits SET status=? WHERE id=?", ("approved" if act == "ok" else "rejected", did))
    return {"ok": True}


@app.post("/api/admin/withdraw/{wid}/{act}")
def adm_wd(wid: int, act: str, x_init: str = Header("")):
    admin(x_init)
    with tx():
        w = db.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wid,)).fetchone()
        if not w:
            err("Hin argamne", 404)
        if act != "ok":
            credit(w["user"], w["amount"], "withdraw_refund")
        db.execute("UPDATE withdrawals SET status=? WHERE id=?", ("paid" if act == "ok" else "rejected", wid))
    return {"ok": True}
