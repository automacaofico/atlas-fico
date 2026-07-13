import base64
import hashlib
import hmac
import io
import json
import mimetypes
import os
import secrets
import sqlite3
import threading
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

from reports import closure_certificate, dashboard_pdf, issues_pdf, issues_xlsx

ROOT = Path(__file__).resolve().parents[1]
BACKEND = Path(__file__).resolve().parent
DATA = BACKEND / "data"
UPLOADS = DATA / "uploads"
DB_PATH = DATA / "atlas.db"
SCHEMA = BACKEND / "schema.sql"
POSTGRES_SCHEMA = BACKEND / "postgresql_schema.sql"
SESSION_HOURS = 12
MAX_BODY = 12 * 1024 * 1024
ATLAS_VERSION = "0.7.2"
INITIALIZATION = {"ready": False, "error": None}
STORAGE_BUCKETS_READY = set()


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
        elif os.environ.get("ATLAS_RESET_ADMIN_PASSWORD", "").lower() == "true":
            password = os.environ.get("ATLAS_ADMIN_PASSWORD", "")
            if len(password) < 10:
                raise RuntimeError("ATLAS_ADMIN_PASSWORD deve ter ao menos 10 caracteres para redefinir a senha")
            connection.execute(
                "UPDATE users SET password_hash=?,must_change_password=?,active=?,updated_at=? WHERE email=?",
                (hash_password(password), True, True, iso(utcnow()), email),
            )
            connection.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE email=?)", (email,))
            print("ATLAS: senha administrativa redefinida; remova ATLAS_RESET_ADMIN_PASSWORD.", flush=True)
        if postgres and connection.execute("SELECT COUNT(*) AS total FROM issues").fetchone()["total"] == 0 and DB_PATH.exists():
            migrate_sqlite_data(connection)
        seed_test_users(connection)


def seed_test_users(connection):
    """Perfis temporários de homologação solicitados pelo administrador."""
    if connection.execute("SELECT 1 FROM audit_log WHERE action='TEST_USERS_SEEDED'").fetchone():
        return
    temporary_password = os.environ.get("ATLAS_TEST_PASSWORD", "AtlasTeste@2026")

    def ensure_user(name, email, company, role, global_approval=False, specialties=()):
        existing = connection.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            return existing[0]
        company_row = connection.execute("SELECT id FROM companies WHERE name=?", (company,)).fetchone()
        if not company_row:
            return None
        returning = " RETURNING id" if getattr(connection, "is_postgres", False) else ""
        cursor = connection.execute(
            "INSERT INTO users(name,email,password_hash,company_id,role,global_approval,must_change_password) VALUES(?,?,?,?,?,?,?)" + returning,
            (name, email, hash_password(temporary_password), company_row[0], role, global_approval, True),
        )
        user_id = cursor.fetchone()["id"] if getattr(connection, "is_postgres", False) else cursor.lastrowid
        for specialty in specialties:
            specialty_row = connection.execute("SELECT id FROM specialties WHERE name=?", (specialty,)).fetchone()
            if specialty_row:
                connection.execute("INSERT OR IGNORE INTO user_specialties(user_id,specialty_id) VALUES(?,?)", (user_id, specialty_row[0]))
        return user_id

    specialty_slugs = {
        "Drenagem": "drenagem", "Terraplenagem": "terraplenagem", "Estruturas": "estruturas",
        "Documental": "documental", "Pavimentação": "pavimentacao", "Obras Complementares": "obras-complementares",
    }
    for specialty, slug in specialty_slugs.items():
        ensure_user(f"Fiscal Teste - {specialty}", f"fiscal.{slug}.teste@atlas-fico.local", "FICO", "Fiscal FICO", specialties=(specialty,))

    for company in ("FICO", "EMPA", "ATERPA", "APIA", "GSA", "VALE"):
        role = "Contratada" if company not in ("FICO", "VALE") else "Consulta"
        ensure_user(f"Usuário Teste - {company}", f"usuario.{company.lower()}.teste@atlas-fico.local", company, role)

    role_profiles = (
        ("Administrador Teste", "administrador.teste@atlas-fico.local", "FICO", "Administrador", True),
        ("Gestor FICO Teste", "gestor.teste@atlas-fico.local", "FICO", "Gestor FICO", True),
        ("Fiscal Geral Teste", "fiscal-geral.teste@atlas-fico.local", "FICO", "Fiscal FICO", True),
        ("Contratada Teste", "contratada.teste@atlas-fico.local", "EMPA", "Contratada", False),
        ("Consulta Teste", "consulta.teste@atlas-fico.local", "FICO", "Consulta", False),
    )
    for name, email, company, role, global_approval in role_profiles:
        ensure_user(name, email, company, role, global_approval)
    connection.execute("INSERT INTO audit_log(action,entity_type,details_json) VALUES(?,?,?)", ("TEST_USERS_SEEDED", "USER_BATCH", json.dumps({"temporary_password": "defined"})))


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
            if not rows:
                continue
            columns = list(rows[0].keys())
            values = [
                tuple(bool(row[name]) if name in boolean_columns.get(table, set()) else row[name] for name in columns)
                for row in rows
            ]
            placeholders = ",".join("%s" for _ in columns)
            sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
            target.connection.executemany(sql, values)
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


def issue_payload(connection, row, histories=None, evidences=None):
    item = dict(row)
    if histories is None:
        item["history"] = [dict(x) for x in connection.execute(
            "SELECT h.event,h.from_status,h.to_status,h.comment,h.created_at,u.name actor FROM issue_history h LEFT JOIN users u ON u.id=h.actor_id WHERE h.issue_id=? ORDER BY h.id", (row["id"],)
        )]
    else:
        item["history"] = histories.get(row["id"], [])
    if evidences is None:
        item["evidence"] = [dict(x) for x in connection.execute(
            "SELECT id,kind,file_path,original_name,mime_type,latitude,longitude,captured_at,created_at FROM evidence WHERE issue_id=? ORDER BY id", (row["id"],)
        )]
    else:
        item["evidence"] = evidences.get(row["id"], [])
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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def binary_response(self, content, content_type, filename):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
            if parts == ["api", "health"]:
                if INITIALIZATION["error"]:
                    return self.json_response(503, {"status": "error", "detail": INITIALIZATION["error"]})
                if not INITIALIZATION["ready"]:
                    return self.json_response(503, {"status": "initializing"})
                database = "postgresql" if os.environ.get("DATABASE_URL") else "sqlite"
                commit = os.environ.get("RENDER_GIT_COMMIT", "local")[:7]
                return self.json_response(200, {"status": "ok", "database": database, "version": ATLAS_VERSION, "commit": commit, "time": iso(utcnow())})
            with db() as connection:
                user = self.require_user(connection)
                if not user:
                    return
                if parts == ["api", "me"]:
                    return self.json_response(200, user)
                if parts == ["api", "issues"]:
                    return self.get_issues(connection, user)
                if parts == ["api", "exports", "issues.xlsx"]:
                    return self.export_issues(connection, user, "xlsx")
                if parts == ["api", "exports", "issues.pdf"]:
                    return self.export_issues(connection, user, "pdf")
                if parts == ["api", "exports", "dashboard.pdf"]:
                    return self.export_dashboard(connection, user)
                if parts == ["api", "exports", "company-dashboards.zip"]:
                    return self.export_company_dashboards(connection, user)
                if len(parts) == 4 and parts[:2] == ["api", "issues"] and parts[3] == "certificate.pdf":
                    return self.export_certificate(connection, user, int(parts[2]))
                if len(parts) == 3 and parts[:2] == ["api", "issues"]:
                    return self.get_issue(connection, user, int(parts[2]))
                if parts == ["api", "users"]:
                    if user["role"] != "Administrador":
                        return self.json_response(403, {"error": "Apenas administradores podem consultar usuários"})
                    rows = connection.execute("SELECT u.id,u.name,u.email,u.role,u.global_approval,u.active,c.name company FROM users u JOIN companies c ON c.id=u.company_id ORDER BY u.name").fetchall()
                    users = []
                    for row in rows:
                        item = dict(row)
                        item["specialties"] = [specialty[0] for specialty in connection.execute("SELECT s.name FROM specialties s JOIN user_specialties us ON us.specialty_id=s.id WHERE us.user_id=? ORDER BY s.name", (row["id"],))]
                        users.append(item)
                    return self.json_response(200, users)
                if parts == ["api", "dashboard"]:
                    return self.get_dashboard(connection, user)
                return self.json_response(404, {"error": "Rota não encontrada"})
        except Exception as exc:
            print(f"ATLAS erro GET {self.path}: {exc}", flush=True)
            return self.json_response(500, {"error": "Falha interna", "detail": str(exc)})

    def do_POST(self):
        if not self.path.startswith("/api/"):
            return self.json_response(404, {"error": "Rota não encontrada"})
        parts = self.route_parts()
        try:
            if not INITIALIZATION["ready"]:
                return self.json_response(503, {"error": "ATLAS ainda está inicializando"})
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
            print(f"ATLAS erro POST {self.path}: {exc}", flush=True)
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

    def do_DELETE(self):
        parts = self.route_parts()
        try:
            with db() as connection:
                user = self.require_user(connection, ["Administrador"])
                if not user:
                    return
                if len(parts) != 3 or parts[:2] != ["api", "users"]:
                    return self.json_response(404, {"error": "Rota não encontrada"})
                target = int(parts[2])
                if target == user["id"]:
                    return self.json_response(400, {"error": "O administrador não pode excluir a própria conta"})
                references = 0
                for table, field in (("issues", "created_by"), ("evidence", "uploaded_by"), ("issue_history", "actor_id"), ("audit_log", "actor_id")):
                    references += connection.execute(f"SELECT COUNT(*) total FROM {table} WHERE {field}=?", (target,)).fetchone()["total"]
                if references:
                    return self.json_response(409, {"error": "Este usuário possui registros de auditoria e não pode ser excluído; desative-o para preservar a rastreabilidade"})
                connection.execute("DELETE FROM users WHERE id=?", (target,))
                audit(connection, user["id"], "USER_DELETED", "USER", target, ip=self.client_address[0])
                return self.json_response(200, {"ok": True})
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
        if not rows:
            return self.json_response(200, [])
        issue_ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in issue_ids)
        histories = {issue_id: [] for issue_id in issue_ids}
        history_rows = connection.execute(
            "SELECT h.issue_id,h.event,h.from_status,h.to_status,h.comment,h.created_at,u.name actor "
            f"FROM issue_history h LEFT JOIN users u ON u.id=h.actor_id WHERE h.issue_id IN ({placeholders}) ORDER BY h.id",
            issue_ids,
        ).fetchall()
        for history in history_rows:
            item = dict(history)
            issue_id = item.pop("issue_id")
            histories[issue_id].append(item)
        evidences = {issue_id: [] for issue_id in issue_ids}
        evidence_rows = connection.execute(
            "SELECT issue_id,id,kind,file_path,original_name,mime_type,latitude,longitude,captured_at,created_at "
            f"FROM evidence WHERE issue_id IN ({placeholders}) ORDER BY id",
            issue_ids,
        ).fetchall()
        for evidence in evidence_rows:
            item = dict(evidence)
            issue_id = item.pop("issue_id")
            evidences[issue_id].append(item)
        return self.json_response(200, [issue_payload(connection, row, histories, evidences) for row in rows])

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
            self.ensure_storage_bucket(supabase_url, secret, bucket)
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

    def ensure_storage_bucket(self, supabase_url, secret, bucket):
        cache_key = (supabase_url, bucket)
        if cache_key in STORAGE_BUCKETS_READY:
            return
        headers = {"Authorization": f"Bearer {secret}", "apikey": secret}
        bucket_url = f"{supabase_url}/storage/v1/bucket/{quote(bucket, safe='')}"
        try:
            with urllib.request.urlopen(urllib.request.Request(bucket_url, headers=headers), timeout=30):
                STORAGE_BUCKETS_READY.add(cache_key)
                return
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Não foi possível verificar o armazenamento de evidências: {detail}") from exc
        payload = json.dumps({
            "id": bucket,
            "name": bucket,
            "public": False,
            "file_size_limit": 10 * 1024 * 1024,
            "allowed_mime_types": ["image/jpeg", "image/png", "image/webp"],
        }).encode("utf-8")
        create = urllib.request.Request(
            f"{supabase_url}/storage/v1/bucket",
            data=payload,
            method="POST",
            headers={**headers, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(create, timeout=30):
                STORAGE_BUCKETS_READY.add(cache_key)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Não foi possível criar o armazenamento de evidências: {detail}") from exc

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

    def export_filter(self, user, ignore_company=False):
        scope, params = self.issue_scope(user)
        query = parse_qs(urlparse(self.path).query)
        labels = []
        filters = (
            ("status", "i.status", "Status"), ("specialty", "i.specialty", "Especialidade"),
            ("company", "c.name", "Empresa"), ("asset", "i.asset", "Ativo"),
            ("classification", "i.classification", "Classificação"), ("fico_owner", "i.fico_owner", "Responsável FICO"),
        )
        for key, column, label in filters:
            value = query.get(key, [""])[0].strip()
            if value and not (ignore_company and key == "company"):
                scope += f" AND {column}=?"
                params.append(value)
                labels.append(f"{label}: {value}")
        opened_from = query.get("opened_from", [""])[0].strip()
        opened_to = query.get("opened_to", [""])[0].strip()
        if opened_from:
            scope += " AND SUBSTR(CAST(i.opened_at AS TEXT),1,10)>=?"; params.append(opened_from); labels.append(f"Abertura a partir de: {opened_from}")
        if opened_to:
            scope += " AND SUBSTR(CAST(i.opened_at AS TEXT),1,10)<=?"; params.append(opened_to); labels.append(f"Abertura até: {opened_to}")
        search = query.get("q", [""])[0].strip().lower()
        if search:
            scope += " AND (LOWER(i.description) LIKE ? OR LOWER(i.asset) LIKE ? OR LOWER(c.name) LIKE ? OR CAST(i.id AS TEXT) LIKE ?)"
            term = f"%{search}%"; params.extend([term, term, term, term]); labels.append(f"Busca: {search}")
        return scope, params, labels

    def export_rows(self, connection, user, ignore_company=False):
        scope, params, labels = self.export_filter(user, ignore_company)
        rows = connection.execute(
            "SELECT i.*,c.name company FROM issues i JOIN companies c ON c.id=i.company_id WHERE 1=1" + scope + " ORDER BY i.id",
            params,
        ).fetchall()
        return rows, labels

    def export_issues(self, connection, user, file_type):
        export_rows, labels = self.export_rows(connection, user)
        rows = [dict(row) for row in export_rows]
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        audit(connection, user["id"], "ISSUES_EXPORTED", "REPORT", details={"type": file_type, "rows": len(rows)}, ip=self.client_address[0])
        applied_filters = " | ".join(labels) if labels else "Carteira completa acessível ao perfil"
        if file_type == "xlsx":
            return self.binary_response(issues_xlsx(rows, applied_filters), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"atlas-pendencias-{stamp}.xlsx")
        return self.binary_response(issues_pdf(rows, applied_filters), "application/pdf", f"atlas-pendencias-{stamp}.pdf")

    def export_dashboard(self, connection, user):
        export_rows, labels = self.export_rows(connection, user)
        rows = [dict(row) for row in export_rows]
        company = parse_qs(urlparse(self.path).query).get("company", [""])[0].strip()
        title = f"Dashboard executivo - {company}" if company else "Dashboard executivo geral"
        content = dashboard_pdf(rows, title, " | ".join(labels) if labels else "Carteira completa acessível ao perfil")
        audit(connection, user["id"], "DASHBOARD_EXPORTED", "REPORT", details={"company": company or "GERAL", "rows": len(rows)}, ip=self.client_address[0])
        suffix = company.lower().replace(" ", "-") if company else "geral"
        return self.binary_response(content, "application/pdf", f"atlas-dashboard-{suffix}.pdf")

    def export_company_dashboards(self, connection, user):
        export_rows, labels = self.export_rows(connection, user, ignore_company=True)
        grouped = {}
        for row in export_rows:
            grouped.setdefault(row["company"], []).append(dict(row))
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for company, rows in sorted(grouped.items()):
                filename = "".join(character.lower() if character.isalnum() else "-" for character in company).strip("-")
                company_filters = [label for label in labels if not label.startswith("Empresa:")]
                content = dashboard_pdf(rows, f"Dashboard executivo - {company}", " | ".join(company_filters) if company_filters else f"Empresa: {company}")
                archive.writestr(f"atlas-dashboard-{filename}.pdf", content)
        audit(connection, user["id"], "COMPANY_DASHBOARDS_EXPORTED", "REPORT", details={"companies": len(grouped), "rows": sum(len(rows) for rows in grouped.values())}, ip=self.client_address[0])
        return self.binary_response(stream.getvalue(), "application/zip", "atlas-dashboards-por-empresa.zip")

    def export_certificate(self, connection, user, issue_id):
        scope, params = self.issue_scope(user)
        issue = connection.execute(
            "SELECT i.*,c.name company FROM issues i JOIN companies c ON c.id=i.company_id WHERE i.id=?" + scope,
            [issue_id] + params,
        ).fetchone()
        if not issue:
            return self.json_response(404, {"error": "Pendência não encontrada"})
        if issue["status"] != "Baixada":
            return self.json_response(409, {"error": "O comprovante formal fica disponível após a baixa da pendência"})
        history = connection.execute(
            "SELECT h.event,h.from_status,h.to_status,h.comment,h.created_at,u.name actor FROM issue_history h LEFT JOIN users u ON u.id=h.actor_id WHERE h.issue_id=? ORDER BY h.id",
            (issue_id,),
        ).fetchall()
        evidence = connection.execute(
            "SELECT kind,file_path,original_name,latitude,longitude,captured_at,created_at FROM evidence WHERE issue_id=? ORDER BY id",
            (issue_id,),
        ).fetchall()
        audit(connection, user["id"], "CLOSURE_CERTIFICATE_EXPORTED", "ISSUE", issue_id, ip=self.client_address[0])
        content = closure_certificate(dict(issue), [dict(row) for row in history], [dict(row) for row in evidence])
        return self.binary_response(content, "application/pdf", f"atlas-comprovante-encerramento-{issue_id}.pdf")


def run(host="127.0.0.1", port=8000):
    server = ThreadingHTTPServer((host, port), AtlasHandler)

    def initialize_background():
        try:
            print("ATLAS: iniciando banco de dados...", flush=True)
            initialize()
            INITIALIZATION["ready"] = True
            print("ATLAS: banco pronto.", flush=True)
        except Exception as exc:
            INITIALIZATION["error"] = str(exc)
            print(f"ATLAS: falha na inicialização: {exc}", flush=True)

    threading.Thread(target=initialize_background, daemon=True).start()
    print(f"ATLAS disponível em http://{host}:{port}", flush=True)
    print("Use Ctrl+C para encerrar", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run(os.environ.get("ATLAS_HOST", "127.0.0.1"), int(os.environ.get("PORT", os.environ.get("ATLAS_PORT", "8000"))))
