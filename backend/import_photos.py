import argparse
import hashlib
import json
import mimetypes
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import UPLOADS, db, initialize

PATTERN = re.compile(r"^(?P<id>\d+)(?:[_ -](?P<kind>abertura|correcao|correção|documento))?(?:[_ -].*)?\.(?P<ext>jpe?g|png|webp)$", re.IGNORECASE)
KINDS = {"abertura": "ABERTURA", "correcao": "CORRECAO", "correção": "CORRECAO", "documento": "DOCUMENTO"}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(folder, apply=False):
    initialize()
    report = {"folder": str(folder), "mode": "apply" if apply else "dry-run", "files": 0, "matched": 0, "inserted": 0, "existing": 0, "unknown_issue": 0, "invalid_name": 0, "errors": []}
    allowed = {"image/jpeg", "image/png", "image/webp"}
    with db() as connection:
        admin = connection.execute("SELECT id FROM users WHERE email='thyago.viegas@vale.com'").fetchone()[0]
        for path in sorted(x for x in folder.rglob("*") if x.is_file()):
            report["files"] += 1
            match = PATTERN.match(path.name)
            if not match:
                report["invalid_name"] += 1
                report["errors"].append({"file": str(path), "problem": "Nome fora do padrão"})
                continue
            source_id = int(match.group("id"))
            kind = KINDS.get((match.group("kind") or "abertura").lower(), "ABERTURA")
            mime = mimetypes.guess_type(path.name)[0]
            if mime not in allowed:
                report["errors"].append({"file": str(path), "problem": "Formato não permitido"})
                continue
            issue = connection.execute("SELECT id,status FROM issues WHERE source_id=?", (source_id,)).fetchone()
            if not issue:
                report["unknown_issue"] += 1
                report["errors"].append({"file": str(path), "source_id": source_id, "problem": "ID não encontrado no banco"})
                continue
            report["matched"] += 1
            if connection.execute("SELECT 1 FROM evidence WHERE issue_id=? AND kind=? AND original_name=?", (issue["id"], kind, path.name)).fetchone():
                report["existing"] += 1
                continue
            if not apply:
                continue
            extension = ".jpg" if path.suffix.lower() in (".jpg", ".jpeg") else path.suffix.lower()
            target_folder = UPLOADS / str(issue["id"])
            target_folder.mkdir(parents=True, exist_ok=True)
            target = target_folder / f"historico-{kind.lower()}-{sha256(path)[:16]}{extension}"
            if not target.exists():
                shutil.copy2(path, target)
            relative = "uploads/" + str(target.relative_to(UPLOADS)).replace("\\", "/")
            connection.execute("INSERT INTO evidence(issue_id,kind,file_path,original_name,mime_type,uploaded_by) VALUES(?,?,?,?,?,?)", (issue["id"], kind, relative, path.name, mime, admin))
            connection.execute("INSERT INTO issue_history(issue_id,event,from_status,to_status,comment,actor_id) VALUES(?,?,?,?,?,?)", (issue["id"], "FOTO_HISTORICA_IMPORTADA", issue["status"], issue["status"], f"{kind}: {path.name}", admin))
            report["inserted"] += 1
    output = Path(__file__).resolve().parent / "data" / "photo-import-report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("mode", "files", "matched", "inserted", "existing", "unknown_issue", "invalid_name")}, ensure_ascii=False, indent=2))
    print(f"Relatório: {output}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Importa fotos históricas para pendências do ATLAS")
    parser.add_argument("folder", type=Path, help="Pasta com fotos nomeadas pelo ID da pendência")
    parser.add_argument("--apply", action="store_true", help="Grava as fotos válidas no banco")
    args = parser.parse_args()
    if not args.folder.is_dir():
        parser.error("A pasta informada não existe")
    raise SystemExit(run(args.folder, args.apply))
