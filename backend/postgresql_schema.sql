CREATE TABLE IF NOT EXISTS companies (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK(kind IN ('FICO','CONTRATADA')),
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  company_id BIGINT NOT NULL REFERENCES companies(id),
  role TEXT NOT NULL CHECK(role IN ('Administrador','Gestor FICO','Fiscal FICO','Contratada','Consulta')),
  global_approval BOOLEAN NOT NULL DEFAULT FALSE,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  must_change_password BOOLEAN NOT NULL DEFAULT TRUE,
  last_login_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS specialties (id BIGSERIAL PRIMARY KEY,name TEXT NOT NULL UNIQUE,active BOOLEAN NOT NULL DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS user_specialties (user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,specialty_id BIGINT NOT NULL REFERENCES specialties(id) ON DELETE CASCADE,PRIMARY KEY(user_id,specialty_id));

CREATE TABLE IF NOT EXISTS issues (
  id BIGSERIAL PRIMARY KEY,source_id BIGINT UNIQUE,package TEXT,segment TEXT,asset TEXT NOT NULL,side TEXT,
  company_id BIGINT NOT NULL REFERENCES companies(id),protocol_code TEXT,origin TEXT,protocol TEXT,protocol_type TEXT,
  protocol_item TEXT,element TEXT,specialty TEXT NOT NULL,description TEXT NOT NULL,
  classification TEXT NOT NULL CHECK(classification IN ('Tipo A','Tipo B','Tipo C')),km_start TEXT,km_end TEXT,
  status TEXT NOT NULL CHECK(status IN ('Rascunho','Aberta','Em tratamento','Aguardando validação','Rejeitada','Baixada','Cancelada')),
  contractor_owner TEXT,fico_owner TEXT,opened_at DATE NOT NULL,deadline_at DATE,expected_close_at DATE,closed_at DATE,
  notes TEXT,created_by BIGINT REFERENCES users(id),updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status); CREATE INDEX IF NOT EXISTS idx_issues_company ON issues(company_id);
CREATE INDEX IF NOT EXISTS idx_issues_specialty ON issues(specialty); CREATE INDEX IF NOT EXISTS idx_issues_deadline ON issues(deadline_at);

CREATE TABLE IF NOT EXISTS evidence (id BIGSERIAL PRIMARY KEY,issue_id BIGINT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,kind TEXT NOT NULL CHECK(kind IN ('ABERTURA','CORRECAO','DOCUMENTO')),file_path TEXT NOT NULL,original_name TEXT,mime_type TEXT,latitude DOUBLE PRECISION,longitude DOUBLE PRECISION,captured_at TIMESTAMPTZ,uploaded_by BIGINT NOT NULL REFERENCES users(id),created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS issue_history (id BIGSERIAL PRIMARY KEY,issue_id BIGINT NOT NULL REFERENCES issues(id) ON DELETE CASCADE,event TEXT NOT NULL,from_status TEXT,to_status TEXT,comment TEXT,actor_id BIGINT REFERENCES users(id),created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY,user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires_at TIMESTAMPTZ NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS password_reset_requests (id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,status TEXT NOT NULL DEFAULT 'PENDENTE' CHECK(status IN ('PENDENTE','RESOLVIDA')),requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),resolved_at TIMESTAMPTZ,resolved_by BIGINT REFERENCES users(id) ON DELETE SET NULL);
CREATE INDEX IF NOT EXISTS idx_password_reset_status ON password_reset_requests(status);
CREATE TABLE IF NOT EXISTS audit_log (id BIGSERIAL PRIMARY KEY,actor_id BIGINT REFERENCES users(id),action TEXT NOT NULL,entity_type TEXT,entity_id TEXT,details_json JSONB,ip_address INET,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS sync_operations (sync_id TEXT PRIMARY KEY,response_status INTEGER NOT NULL,response_json JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE IF NOT EXISTS portfolio_backups (id BIGSERIAL PRIMARY KEY,label TEXT NOT NULL,snapshot_json JSONB NOT NULL,issue_count INTEGER NOT NULL,created_by BIGINT NOT NULL REFERENCES users(id),created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
