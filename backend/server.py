import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]
BACKEND = Path(__file__).resolve().parent
DATA = BACKEND / "data"
UPLOADS = DATA / "uploads"
DB_PATH = DATA / "atlas.db"
SCHEMA = BACKEND / "schema.sql"
POSTGRES_SCHEMA = BACKEND / "postgresql_schema.sql"
SESSION_HOURS = 12
MAX_BODY = 12 * 1024 * 1024


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def db():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("Dependência psycopg não instalada") from exc
        return PostgresConnection(psycopg.connect(database_url, row_factory=dict_row))
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class PostgresCursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.lastrowid = None

    def fetchone(self):
        row = self.cursor.fetchone()
        return CompatRow(row) if isinstance(row, dict) else row

    def fetchall(self):
        return [CompatRow(row) if isinstance(row, dict) else row for row in self.cursor.fetchall()]

    def __iter__(self):
        return (CompatRow(row) if isinstance(row, dict) else row for row in self.cursor)


class CompatRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class PostgresConnection:
    """Compatibilidade mínima com a API sqlite usada pelo ATLAS."""
    is_postgres = True

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type:
            self.connection.rollback()
        else:
            self.connection.commit()
        self.connection.close()

    def execute(self, sql, params=()):
        sql = sql.replace("?", "%s")
        sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
        if "INSERT INTO sync_operations" in sql and "ON CONFLICT" not in sql:
            sql += " ON CONFLICT (sync_id) DO NOTHING"
        if "INSERT INTO user_specialties" in sql and "ON CONFLICT" not in sql:
            sql += " ON CONFLICT (user_id,specialty_id) DO NOTHING"
        return PostgresCursor(self.connection.execute(sql, params))

    def executescript(self, sql):
        self.connection.execute(sql, prepare=False)


def hash_password(password, iterations=310000):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password, encoded):
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        postgres = getattr(connection, "is_postgres", False)
        connection.executescript((POSTGRES_SCHEMA if postgres else SCHEMA).read_text(encoding="utf-8"))
        conflict = " ON CONFLICT (name) DO NOTHING" if postgres else ""
        verb = "INSERT INTO" if postgres else "INSERT OR IGNORE INTO"
        connection.execute(f"{verb} companies(name,kind) VALUES('FICO','FICO'){conflict}")
        for company in ("EMPA", "ATERPA", "APIA", "GSA", "VALE"):
            kind = "FICO" if company == "VALE" else "CONTRATADA"
            connection.execute(f"{verb} companies(name,kind) VALUES(?,?){conflict}", (company, kind))
        for specialty in ("Drenagem", "Terraplenagem", "Estruturas", "Documental", "Pavimentação", "Obras Complementares"):
            connection.execute(f"{verb} specialties(name) VALUES(?){conflict}", (specialty,))
        company_id = connection.execute("SELECT id FROM companies WHERE name='FICO'").fetchone()[0]
        email = "thyago.viegas@vale.com"
        if not connection.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            password = os.environ.get("ATLAS_ADMIN_PASSWORD", "Atlas@2026")
            connection.execute(
                "INSERT INTO users(name,email,password_hash,company_id,role,global_approval,must_change_password) VALUES(?,?,?,?,?,?,?)",
                ("Thyago Pinheiro Viégas Mendonça", email, hash_password(password), company_id, "Administrador", True, True),
            )
        if postgres and connection.execute("SELECT COUNT(*) AS total FROM issues").fetchone()["total"] == 0 and DB_PATH.exists():
            migrate_sqlite_data(connection)


def migrate_sqlite_data(target):
    """Carrega uma única vez a base real homologada no PostgreSQL vazio."""
    source = sqlite3.connect(DB_PATH)
    source.row_factory = sqlite3.Row
    boolean_columns = {
        "companies": {"active"},
        "specialties": {"active"},
        "users": {"global_approval", "active", "must_change_password"},
    }
    tables = ("companies", "specialties", "users", "user_specialties", "issues", "evidence", "issue_history")
    try:
        for table in tables:
            rows = source.execute(f"SELECT * FROM {table}").fetchall()
            for row in rows:
                columns = list(row.keys())
                values = [bool(row[name]) if name in boolean_columns.get(table, set()) else row[name] for name in columns]
                placeholders = ",".join("?" for _ in columns)
                conflict = " ON CONFLICT DO NOTHING"
                target.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}){conflict}", values)
        for table in ("companies", "specialties", "users", "issues", "evidence", "issue_history"):
            target.execute(
                "SELECT setval(pg_get_serial_sequence(?, 'id'), COALESCE((SELECT MAX(id) FROM " + table + "), 1), true)",
                (table,),
            )
    finally:
        source.close()


def user_payload(connection, user_id):
    row = connection.execute(
        "SELECT u.id,u.name,u.email,u.role,u.global_approval,u.active,u.must_change_password,c.id company_id,c.name company "
        "FROM users u JOIN companies c ON c.id=u.company_id WHERE u.id=?", (user_id,)
    ).fetchone()
    if not row:
        return None
    payload = dict(row)
    payload["global_approval"] = bool(payload["global_approval"])
    payload["active"] = bool(payload["active"])
    payload["must_change_password"] = bool(payload["must_change_password"])
    payload["specialties"] = [x[0] for x in connection.execute(
        "SELECT s.name FROM specialties s JOIN user_specialties us ON us.specialty_id=s.id WHERE us.user_id=? ORDER BY s.name", (user_id,)
    )]
    return payload


def issue_payload(connection, row):
    item = dict(row)
    item["history"] = [dict(x) for x in connection.execute(
        "SELECT h.event,h.from_status,h.to_status,h.comment,h.created_at,u.name actor FROM issue_history h LEFT JOIN users u ON u.id=h.actor_id WHERE h.issue_id=? ORDER BY h.id", (row["id"],)
    )]
    item["evidence"] = [dict(x) for x in connection.execute(
        "SELECT id,kind,file_path,original_name,mime_type,latitude,longitude,captured_at,created_at FROM evidence WHERE issue_id=? ORDER BY id", (row["id"],)
    )]
    return item


def audit(connection, actor_id, action, entity_type, entity_id=None, details=None, ip=None):
    connection.execute(
        "INSERT INTO audit_log(actor_id,action,entity_type,entity_id,details_json,ip_address) VALUES(?,?,?,?,?,?)",
        (actor_id, action, entity_type, str(entity_id) if entity_id is not None else None, json.dumps(details or {}, ensure_ascii=False), ip),
    )


class AtlasHandler(SimpleHTTPRequestHandler):
    server_version = "ATLAS/0.2"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
        super().end_headers()

    def json_response(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def synced_response(self, connection, request_payload, status, response_payload):
        sync_id = request_payload.get("sync_id") if isinstance(request_payload, dict) else None
        if sync_id:
            connection.execute("INSERT OR IGNORE INTO sync_operations(sync_id,response_status,response_json) VALUES(?,?,?)", (sync_id, status, json.dumps(response_payload, ensure_ascii=False)))
        return self.json_response(status, response_payload)

    def storage_config(self):
        return (
            os.environ.get("SUPABASE_URL", "").rstrip("/"),
            os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SECRET_KEY", ""),
            os.environ.get("SUPABASE_BUCKET", "evidencias"),
        )

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            raise ValueError("Requisição excede o limite permitido")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def bearer(self):
        header = self.headers.get("Authorization", "")
        return header[7:] if header.startswith("Bearer ") else None

    def current_user(self, connection):
        token = self.bearer()
        if not token:
            return None
        row = connection.execute(
            "SELECT user_id FROM sessions WHERE token_hash=? AND expires_at>?",
            (token_hash(token), iso(utcnow())),
        ).fetchone()
        return user_payload(connection, row[0]) if row else None

    def require_user(self, connection, roles=None):
        user = self.current_user(connection)
        if not user or not user["active"]:
            self.json_response(HTTPStatus.UNAUTHORIZED, {"error": "Sessão inválida ou expirada"})
            return None
        if roles and user["role"] not in roles:
            self.json_response(HTTPStatus.FORBIDDEN, {"error": "Perfil sem permissão para esta ação"})
            return None
        return user

    def route_parts(self):
        return [x for x in urlparse(self.path).path.split("/") if x]

    def do_GET(self):
        if not self.path.startswith("/api/"):
            if self.path.startswith("/backend"):
                return self.send_error(HTTPStatus.NOT_FOUND)
            if self.path.startswith("/uploads/"):
                relative = Path(urlparse(self.path).path.removeprefix("/uploads/"))
                supabase_url, secret, bucket = self.storage_config()
                if supabase_url and secret:
                    object_path = str(relative).replace("\\", "/")
                    request = urllib.request.Request(
                        f"{supabase_url}/storage/v1/object/{bucket}/{object_path}",
                        headers={"Authorization": f"Bearer {secret}", "apikey": secret},
                    )
                    try:
                        with urllib.request.urlopen(request, timeout=30) as response:
                            content = response.read()
                            self.send_response(200)
                            self.send_header("Content-Type", response.headers.get_content_type())
                            self.send_header("Content-Length", str(len(content)))
                            self.end_headers()
                            self.wfile.write(content)
                            return
                    except urllib.error.HTTPError:
                        return self.send_error(HTTPStatus.NOT_FOUND)
                target = (UPLOADS / relative).resolve()
                if UPLOADS.resolve() not in target.parents or not target.is_file():
                    return self.send_error(HTTPStatus.NOT_FOUND)
                content = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            if self.path == "/":
                self.path = "/index.html"
            return super().do_GET()
        parts = self.route_parts()
        try:
            with db() as connection:
                if parts == ["api", "health"]:
                    database = "postgresql" if os.environ.get("DATABASE_URL") else "sqlite"
                    connection.execute("SELECT 1").fetchone()
                    return self.json_response(200, {"status": "ok", "database": database, "time": iso(utcnow())})
                user = self.require_user(connection)
                if not user:
                    return
                if parts == ["api", "me"]:
                    return self.json_response(200, user)
                if parts == ["api", "issues"]:
                    return self.get_issues(connection, user)
                if len(parts) == 3 and parts[:2] == ["api", "issues"]:
                    return self.get_issue(connection, user, int(parts[2]))
                if parts == ["api", "users"]:
                    if user["role"] != "Administrador":
                        return self.json_response(403, {"error": "Apenas administradores podem consultar usuários"})
                    rows = connection.execute("SELECT u.id,u.name,u.email,u.role,u.global_approval,u.active,c.name company FROM users u JOIN companies c ON c.id=u.company_id ORDER BY u.name").fetchall()
                    return self.json_response(200, [dict(x) for x in rows])
                if parts == ["api", "dashboard"]:
                    return self.get_dashboard(connection, user)
                return self.json_response(404, {"error": "Rota não encontrada"})
        except Exception as exc:
            return self.json_response(500, {"error": "Falha interna", "detail": str(exc)})

    def do_POST(self):
        if not self.path.startswith("/api/"):
            return self.json_response(404, {"error": "Rota não encontrada"})
        parts = self.route_parts()
        try:
            payload = self.read_json()
            with db() as connection:
                if parts == ["api", "login"]:
                    return self.login(connection, payload)
                user = self.require_user(connection)
                if not user:
                    return
                if payload.get("sync_id"):
                    previous = connection.execute("SELECT response_status,response_json FROM sync_operations WHERE sync_id=?", (payload["sync_id"],)).fetchone()
                    if previous:
                        cached = previous["response_json"]
                        return self.json_response(previous["response_status"], json.loads(cached) if isinstance(cached, str) else cached)
                if parts == ["api", "logout"]:
                    token = self.bearer()
                    connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))
                    audit(connection, user["id"], "LOGOUT", "SESSION", ip=self.client_address[0])
                    return self.json_response(200, {"ok": True})
                if parts == ["api", "change-password"]:
                    return self.change_password(connection, user, payload)
                if parts == ["api", "issues"]:
                    return self.create_issue(connection, user, payload)
                if len(parts) == 4 and parts[:2] == ["api", "issues"] and parts[3] == "correction":
                    return self.submit_correction(connection, user, int(parts[2]), payload)
                if len(parts) == 4 and parts[:2] == ["api", "issues"] and parts[3] == "decision":
                    return self.decide_issue(connection, user, int(parts[2]), payload)
                if len(parts) == 4 and parts[:2] == ["api", "issues"] and parts[3] == "evidence":
                    return self.add_historical_evidence(connection, user, int(parts[2]), payload)
                if parts == ["api", "users"]:
                    return self.create_user(connection, user, payload)
                return self.json_response(404, {"error": "Rota não encontrada"})
        except (ValueError, json.JSONDecodeError) as exc:
            return self.json_response(400, {"error": str(exc)})
        except sqlite3.IntegrityError as exc:
            return self.json_response(409, {"error": "Registro duplicado ou inválido", "detail": str(exc)})
        except Exception as exc:
            return self.json_response(500, {"error": "Falha interna", "detail": str(exc)})

    def do_PATCH(self):
        parts = self.route_parts()
        try:
            payload = self.read_json()
            with db() as connection:
                user = self.require_user(connection, ["Administrador"])
                if not user:
                    return
                if len(parts) == 3 and parts[:2] == ["api", "issues"]:
                    return self.update_issue(connection, user, int(parts[2]), payload)
                if len(parts) == 4 and parts[:2] == ["api", "users"] and parts[3] == "status":
                    target = int(parts[2])
                    if target == user["id"] and not payload.get("active", True):
                        return self.json_response(400, {"error": "O administrador não pode desativar a própria conta"})
                    connection.execute("UPDATE users SET active=?,updated_at=? WHERE id=?", (bool(payload.get("active")), iso(utcnow()), target))
                    audit(connection, user["id"], "USER_STATUS", "USER", target, payload, self.client_address[0])
                    return self.json_response(200, {"ok": True})
                return self.json_response(404, {"error": "Rota não encontrada"})
        except Exception as exc:
            return self.json_response(500, {"error": "Falha interna", "detail": str(exc)})

    def login(self, connection, payload):
        email = str(payload.get("email", "")).strip().lower()
        password = str(payload.get("password", ""))
        row = connection.execute("SELECT id,password_hash,active FROM users WHERE email=?", (email,)).fetchone()
        if not row or not row["active"] or not verify_password(password, row["password_hash"]):
            return self.json_response(401, {"error": "E-mail ou senha inválidos"})
        token = secrets.token_urlsafe(32)
        expires = utcnow() + timedelta(hours=SESSION_HOURS)
        connection.execute("DELETE FROM sessions WHERE expires_at<=?", (iso(utcnow()),))
        connection.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (token_hash(token), row["id"], iso(expires)))
        connection.execute("UPDATE users SET last_login_at=? WHERE id=?", (iso(utcnow()), row["id"]))
        audit(connection, row["id"], "LOGIN", "SESSION", ip=self.client_address[0])
        return self.json_response(200, {"token": token, "expires_at": iso(expires), "user": user_payload(connection, row["id"])})

    def change_password(self, connection, user, payload):
        current = str(payload.get("current_password", ""))
        new = str(payload.get("new_password", ""))
        row = connection.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if not verify_password(current, row[0]):
            return self.json_response(400, {"error": "Senha atual inválida"})
        if len(new) < 10 or not any(c.isupper() for c in new) or not any(c.islower() for c in new) or not any(c.isdigit() for c in new):
            return self.json_response(400, {"error": "A nova senha deve ter 10 caracteres, maiúscula, minúscula e número"})
        connection.execute("UPDATE users SET password_hash=?,must_change_password=?,updated_at=? WHERE id=?", (hash_password(new), False, iso(utcnow()), user["id"]))
        connection.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        audit(connection, user["id"], "PASSWORD_CHANGED", "USER", user["id"], ip=self.client_address[0])
        return self.json_response(200, {"ok": True, "reauthenticate": True})

    def issue_scope(self, user):
        if user["role"] == "Contratada":
            return " AND i.company_id=?", [user["company_id"]]
        return "", []

    def get_issues(self, connection, user):
        query = parse_qs(urlparse(self.path).query)
        where, params = self.issue_scope(user)
        if query.get("status"):
            where += " AND i.status=?"; params.append(query["status"][0])
        if query.get("company") and user["role"] != "Contratada":
            where += " AND c.name=?"; params.append(query["company"][0])
        rows = connection.execute(
            "SELECT i.*,c.name company FROM issues i JOIN companies c ON c.id=i.company_id WHERE 1=1" + where + " ORDER BY i.id DESC LIMIT 2500", params
        ).fetchall()
        return self.json_response(200, [issue_payload(connection, x) for x in rows])

    def get_issue(self, connection, user, issue_id):
        where, params = self.issue_scope(user)
        row = connection.execute("SELECT i.*,c.name company FROM issues i JOIN companies c ON c.id=i.company_id WHERE i.id=?" + where, [issue_id] + params).fetchone()
        return self.json_response(200, issue_payload(connection, row)) if row else self.json_response(404, {"error": "Pendência não encontrada"})

    def create_issue(self, connection, user, payload):
        if user["role"] not in ("Administrador", "Gestor FICO", "Fiscal FICO"):
            return self.json_response(403, {"error": "Perfil sem permissão para abrir pendências"})
        required = ("company", "asset", "specialty", "description", "classification", "opened_at")
        missing = [x for x in required if not payload.get(x)]
        if missing:
            return self.json_response(400, {"error": "Campos obrigatórios ausentes", "fields": missing})
        company = connection.execute("SELECT id FROM companies WHERE name=? AND active=?", (payload["company"], True)).fetchone()
        if not company:
            return self.json_response(400, {"error": "Empresa inválida"})
        returning = " RETURNING id" if getattr(connection, "is_postgres", False) else ""
        cursor = connection.execute(
            "INSERT INTO issues(company_id,asset,segment,side,specialty,description,classification,km_start,km_end,status,fico_owner,opened_at,deadline_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)" + returning,
            (company[0], payload["asset"], payload.get("segment"), payload.get("side"), payload["specialty"], payload["description"], payload["classification"], payload.get("km_start"), payload.get("km_end"), "Aberta", payload.get("fico_owner", user["name"]), payload["opened_at"], payload.get("deadline_at"), user["id"]),
        )
        issue_id = cursor.fetchone()["id"] if getattr(connection, "is_postgres", False) else cursor.lastrowid
        if payload.get("photo_data_url"):
            file_path, mime, original = self.save_data_url(issue_id, payload["photo_data_url"], payload.get("photo_name"), user["id"], "ABERTURA")
            connection.execute("INSERT INTO evidence(issue_id,kind,file_path,original_name,mime_type,latitude,longitude,captured_at,uploaded_by) VALUES(?,?,?,?,?,?,?,?,?)", (issue_id, "ABERTURA", file_path, original, mime, payload.get("latitude"), payload.get("longitude"), payload.get("captured_at"), user["id"]))
        connection.execute("INSERT INTO issue_history(issue_id,event,to_status,comment,actor_id) VALUES(?,?,?,?,?)", (issue_id, "PENDENCIA_CRIADA", "Aberta", payload.get("comment"), user["id"]))
        audit(connection, user["id"], "ISSUE_CREATED", "ISSUE", issue_id, ip=self.client_address[0])
        return self.synced_response(connection, payload, 201, {"id": issue_id, "status": "Aberta"})

    def save_data_url(self, issue_id, data_url, original_name, actor_id, kind):
        if not data_url or not data_url.startswith("data:"):
            raise ValueError("Foto obrigatória em formato data URL")
        meta, encoded = data_url.split(",", 1)
        mime = meta[5:].split(";", 1)[0]
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            raise ValueError("Formato de imagem não permitido")
        content = base64.b64decode(encoded, validate=True)
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("Imagem excede 10 MB")
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime]
        filename = f"{kind.lower()}-{secrets.token_hex(8)}{extension}"
        object_path = f"{issue_id}/{filename}"
        supabase_url, secret, bucket = self.storage_config()
        if supabase_url and secret:
            request = urllib.request.Request(
                f"{supabase_url}/storage/v1/object/{bucket}/{object_path}",
                data=content,
                method="POST",
                headers={"Authorization": f"Bearer {secret}", "apikey": secret, "Content-Type": mime, "x-upsert": "false"},
            )
            try:
                with urllib.request.urlopen(request, timeout=60):
                    pass
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Falha ao enviar evidência ao Supabase: {detail}") from exc
        else:
            path = UPLOADS / object_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return "uploads/" + object_path, mime, original_name

    def submit_correction(self, connection, user, issue_id, payload):
        row = connection.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        if not row:
            return self.json_response(404, {"error": "Pendência não encontrada"})
        if user["role"] != "Contratada" or user["company_id"] != row["company_id"]:
            return self.json_response(403, {"error": "Somente a contratada responsável pode enviar a correção"})
        if row["status"] not in ("Aberta", "Em tratamento", "Rejeitada"):
            return self.json_response(409, {"error": "Status atual não permite nova correção"})
        file_path, mime, original = self.save_data_url(issue_id, payload.get("photo_data_url"), payload.get("photo_name"), user["id"], "CORRECAO")
        connection.execute("INSERT INTO evidence(issue_id,kind,file_path,original_name,mime_type,latitude,longitude,captured_at,uploaded_by) VALUES(?,?,?,?,?,?,?,?,?)", (issue_id, "CORRECAO", file_path, original, mime, payload.get("latitude"), payload.get("longitude"), payload.get("captured_at"), user["id"]))
        connection.execute("UPDATE issues SET status='Aguardando validação',contractor_owner=?,updated_at=? WHERE id=?", (user["name"], iso(utcnow()), issue_id))
        connection.execute("INSERT INTO issue_history(issue_id,event,from_status,to_status,comment,actor_id) VALUES(?,?,?,?,?,?)", (issue_id, "CORRECAO_ENVIADA", row["status"], "Aguardando validação", payload.get("comment"), user["id"]))
        audit(connection, user["id"], "CORRECTION_SUBMITTED", "ISSUE", issue_id, ip=self.client_address[0])
        return self.synced_response(connection, payload, 200, {"id": issue_id, "status": "Aguardando validação"})

    def decide_issue(self, connection, user, issue_id, payload):
        row = connection.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        if not row:
            return self.json_response(404, {"error": "Pendência não encontrada"})
        if row["status"] != "Aguardando validação":
            return self.json_response(409, {"error": "Pendência não está aguardando validação"})
        authorized = user["role"] in ("Administrador", "Gestor FICO", "Fiscal FICO") and (user["global_approval"] or row["specialty"] in user["specialties"])
        if not authorized:
            return self.json_response(403, {"error": "Fiscal sem alçada para esta especialidade"})
        decision = payload.get("decision")
        if decision not in ("approve", "reject"):
            return self.json_response(400, {"error": "Decisão inválida"})
        status = "Baixada" if decision == "approve" else "Rejeitada"
        if decision == "reject" and not str(payload.get("comment", "")).strip():
            return self.json_response(400, {"error": "Justificativa obrigatória para rejeição"})
        connection.execute("UPDATE issues SET status=?,closed_at=?,updated_at=? WHERE id=?", (status, iso(utcnow()) if status == "Baixada" else None, iso(utcnow()), issue_id))
        connection.execute("INSERT INTO issue_history(issue_id,event,from_status,to_status,comment,actor_id) VALUES(?,?,?,?,?,?)", (issue_id, "BAIXA_APROVADA" if decision == "approve" else "CORRECAO_REJEITADA", row["status"], status, payload.get("comment"), user["id"]))
        audit(connection, user["id"], "ISSUE_APPROVED" if decision == "approve" else "ISSUE_REJECTED", "ISSUE", issue_id, {"authorization": "global" if user["global_approval"] else "specialty"}, self.client_address[0])
        return self.synced_response(connection, payload, 200, {"id": issue_id, "status": status})

    def create_user(self, connection, user, payload):
        if user["role"] != "Administrador":
            return self.json_response(403, {"error": "Apenas administradores podem cadastrar usuários"})
        required = ("name", "email", "company", "role", "temporary_password")
        missing = [x for x in required if not payload.get(x)]
        if missing:
            return self.json_response(400, {"error": "Campos obrigatórios ausentes", "fields": missing})
        password = str(payload["temporary_password"])
        if len(password) < 10:
            return self.json_response(400, {"error": "Senha temporária deve ter ao menos 10 caracteres"})
        company = connection.execute("SELECT id FROM companies WHERE name=?", (payload["company"],)).fetchone()
        if not company:
            return self.json_response(400, {"error": "Empresa inválida"})
        returning = " RETURNING id" if getattr(connection, "is_postgres", False) else ""
        cursor = connection.execute("INSERT INTO users(name,email,password_hash,company_id,role,global_approval,must_change_password) VALUES(?,?,?,?,?,?,?)" + returning, (payload["name"], payload["email"].strip().lower(), hash_password(password), company[0], payload["role"], bool(payload.get("global_approval")), True))
        target = cursor.fetchone()["id"] if getattr(connection, "is_postgres", False) else cursor.lastrowid
        for specialty in payload.get("specialties", []):
            spec = connection.execute("SELECT id FROM specialties WHERE name=?", (specialty,)).fetchone()
            if spec:
                connection.execute("INSERT OR IGNORE INTO user_specialties(user_id,specialty_id) VALUES(?,?)", (target, spec[0]))
        audit(connection, user["id"], "USER_CREATED", "USER", target, {"role": payload["role"], "company": payload["company"]}, self.client_address[0])
        return self.json_response(201, {"id": target, "must_change_password": True})

    def update_issue(self, connection, user, issue_id, payload):
        row = connection.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
        if not row:
            return self.json_response(404, {"error": "Pendência não encontrada"})
        allowed = {
            "asset", "segment", "side", "specialty", "description", "classification", "km_start", "km_end",
            "contractor_owner", "fico_owner", "deadline_at", "expected_close_at", "notes", "protocol_code",
            "origin", "protocol", "protocol_type", "protocol_item", "element"
        }
        changes = {}
        values = []
        assignments = []
        if payload.get("company"):
            company = connection.execute("SELECT id FROM companies WHERE name=? AND active=?", (payload["company"], True)).fetchone()
            if not company:
                return self.json_response(400, {"error": "Empresa inválida"})
            if company[0] != row["company_id"]:
                changes["company_id"] = {"from": row["company_id"], "to": company[0]}
                assignments.append("company_id=?"); values.append(company[0])
        for field in allowed:
            if field in payload and payload[field] != row[field]:
                if field == "classification" and payload[field] not in ("Tipo A", "Tipo B", "Tipo C"):
                    return self.json_response(400, {"error": "Classificação inválida"})
                changes[field] = {"from": row[field], "to": payload[field]}
                assignments.append(f"{field}=?"); values.append(payload[field])
        if not changes:
            return self.json_response(200, {"id": issue_id, "updated": False})
        assignments.append("updated_at=?"); values.append(iso(utcnow())); values.append(issue_id)
        connection.execute(f"UPDATE issues SET {','.join(assignments)} WHERE id=?", values)
        connection.execute("INSERT INTO issue_history(issue_id,event,from_status,to_status,comment,actor_id) VALUES(?,?,?,?,?,?)", (issue_id, "PENDENCIA_EDITADA", row["status"], row["status"], json.dumps(changes, ensure_ascii=False), user["id"]))
        audit(connection, user["id"], "ISSUE_UPDATED", "ISSUE", issue_id, changes, self.client_address[0])
        return self.json_response(200, {"id": issue_id, "updated": True, "changes": changes})

    def add_historical_evidence(self, connection, user, issue_id, payload):
        if user["role"] != "Administrador":
            return self.json_response(403, {"error": "Apenas administradores podem anexar evidências históricas"})
        if not connection.execute("SELECT 1 FROM issues WHERE id=?", (issue_id,)).fetchone():
            return self.json_response(404, {"error": "Pendência não encontrada"})
        kind = payload.get("kind", "ABERTURA")
        if kind not in ("ABERTURA", "CORRECAO", "DOCUMENTO"):
            return self.json_response(400, {"error": "Tipo de evidência inválido"})
        file_path, mime, original = self.save_data_url(issue_id, payload.get("photo_data_url"), payload.get("photo_name"), user["id"], kind)
        returning = " RETURNING id" if getattr(connection, "is_postgres", False) else ""
        cursor = connection.execute("INSERT INTO evidence(issue_id,kind,file_path,original_name,mime_type,latitude,longitude,captured_at,uploaded_by) VALUES(?,?,?,?,?,?,?,?,?)" + returning, (issue_id, kind, file_path, original, mime, payload.get("latitude"), payload.get("longitude"), payload.get("captured_at"), user["id"]))
        connection.execute("INSERT INTO issue_history(issue_id,event,from_status,to_status,comment,actor_id) SELECT id,'EVIDENCIA_HISTORICA_ADICIONADA',status,status,?,? FROM issues WHERE id=?", (f"{kind}: {original}", user["id"], issue_id))
        audit(connection, user["id"], "HISTORICAL_EVIDENCE_ADDED", "ISSUE", issue_id, {"kind": kind, "file": original}, self.client_address[0])
        evidence_id = cursor.fetchone()["id"] if getattr(connection, "is_postgres", False) else cursor.lastrowid
        return self.json_response(201, {"id": evidence_id, "issue_id": issue_id, "kind": kind, "file_path": file_path})

    def get_dashboard(self, connection, user):
        scope, params = self.issue_scope(user)
        rows = connection.execute("SELECT i.status,i.specialty,i.deadline_at,c.name company FROM issues i JOIN companies c ON c.id=i.company_id WHERE 1=1" + scope, params).fetchall()
        today = datetime.now().date().isoformat()
        total = len(rows)
        counts = {}
        companies = {}
        specialties = {}
        overdue = 0
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            companies[row["company"]] = companies.get(row["company"], 0) + 1
            specialties[row["specialty"]] = specialties.get(row["specialty"], 0) + 1
            if row["status"] != "Baixada" and row["deadline_at"] and str(row["deadline_at"])[:10] < today:
                overdue += 1
        return self.json_response(200, {"total": total, "by_status": counts, "by_company": companies, "by_specialty": specialties, "overdue": overdue, "closure_rate": round(counts.get("Baixada", 0) / total * 100, 1) if total else 0})


def run(host="127.0.0.1", port=8000):
    initialize()
    server = ThreadingHTTPServer((host, port), AtlasHandler)
    print(f"ATLAS disponível em http://{host}:{port}")
    print("Use Ctrl+C para encerrar")
    server.serve_forever()


if __name__ == "__main__":
    run(os.environ.get("ATLAS_HOST", "127.0.0.1"), int(os.environ.get("PORT", os.environ.get("ATLAS_PORT", "8000"))))
