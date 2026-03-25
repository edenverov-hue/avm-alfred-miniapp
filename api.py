#!/usr/bin/env python3
"""
Alfred API Server — serwuje dane AVM z config.json i vault.
Port: 8765
"""

import json
import re
from datetime import datetime, date
from pathlib import Path

import sys
sys.path.insert(0, str(Path.home() / "alfred" / "agents"))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from gate_engine import validate_gate, create_project_card, get_gate_definition, GATES

VAULT = Path.home() / "alfred" / "vault"
MEMORY = Path.home() / "alfred" / ".claude" / "memory"
PORTAL = Path.home() / "alfred" / "portal"
CONFIG_FILE = PORTAL / "config.json"

app = FastAPI(title="Alfred API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def load_config():
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def parse_md_table(content: str) -> list[dict]:
    """Parsuje tabelę markdown do listy słowników."""
    rows = []
    lines = content.splitlines()
    headers = []
    for line in lines:
        if not line.strip().startswith("|"):
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if not headers:
            headers = cols
        elif all(c.startswith("-") or c == "" for c in cols):
            continue
        else:
            rows.append(dict(zip(headers, cols)))
    return rows


def parse_tasks_from_memory() -> list[dict]:
    """Parsuje ZADANIA.md do listy tasków."""
    content = read_file(MEMORY / "ZADANIA.md")
    tasks = []
    lines = content.splitlines()
    headers = []
    for line in lines:
        if not line.strip().startswith("|"):
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if not headers:
            if "Zadanie" in cols[0] or "Task" in cols[0]:
                headers = cols
        elif all(c.replace("-", "").replace(" ", "") == "" for c in cols):
            continue
        elif headers:
            if len(cols) >= 4:
                tasks.append({
                    "title": cols[0],
                    "owner_person": cols[1],
                    "deadline": cols[2] if len(cols) > 2 else "",
                    "priority": cols[3] if len(cols) > 3 else "M",
                    "status": cols[4] if len(cols) > 4 else "open",
                })
    return tasks


def parse_tasks_from_vault() -> list[dict]:
    """Parsuje vault/ZADANIA/*.md do listy tasków."""
    zadania_dir = VAULT / "ZADANIA"
    tasks = []
    if not zadania_dir.exists():
        return tasks
    for f in zadania_dir.glob("*.md"):
        content = f.read_text(encoding="utf-8")
        lines = content.splitlines()
        fm = {}
        in_fm = False
        for l in lines:
            if l.strip() == "---":
                if not in_fm:
                    in_fm = True
                else:
                    break
            elif in_fm and ":" in l:
                k, _, v = l.partition(":")
                fm[k.strip()] = v.strip().strip('"')

        if not fm.get("title"):
            continue

        # Map owner role
        owner_role = ""
        for role in ["CEO", "PM", "TECH", "FOREMAN", "KVP", "HUNTER", "FIN",
                     "BRIGADE", "INSTALLER", "SERVICE", "AM"]:
            if role in content:
                owner_role = role
                break

        tasks.append({
            "id": fm.get("task_id", f.stem),
            "title": fm.get("title", f.stem),
            "owner_role": owner_role,
            "owner_person": fm.get("owner", ""),
            "deadline": fm.get("deadline", ""),
            "priority": fm.get("priority", "M"),
            "status": fm.get("status", "open"),
        })
    return tasks


def get_tasks_combined() -> list[dict]:
    """Zwraca zadania z vault ZADANIA/ + memory ZADANIA.md (deduplikacja po tytule)."""
    vault_tasks = parse_tasks_from_vault()
    mem_tasks = parse_tasks_from_memory()
    seen = {t["title"] for t in vault_tasks}
    result = list(vault_tasks)
    for t in mem_tasks:
        if t["title"] not in seen:
            # Enrich with owner_role from config if possible
            config = load_config()
            owner_role = ""
            for role, rdata in config.get("roles", {}).items():
                persons = rdata.get("persons", [])
                owner_p = t.get("owner_person", "")
            if any(owner_p and owner_p in p.get("person", "") for p in persons):
                    owner_role = role
                    break
            result.append({
                "id": f"MEM-{len(result)+1:03d}",
                "title": t["title"],
                "owner_role": owner_role,
                "owner_person": t.get("owner_person", ""),
                "deadline": t.get("deadline", ""),
                "priority": t.get("priority", "M"),
                "status": t.get("status", "open"),
            })
    return result


def get_stats(tasks: list[dict]) -> dict:
    today = date.today().isoformat()
    open_count = sum(1 for t in tasks if t["status"] not in ("done", "closed"))
    critical = sum(1 for t in tasks if t.get("priority") == "H"
                   and t["status"] not in ("done", "closed"))
    overdue = sum(1 for t in tasks if t.get("deadline") and t["deadline"] < today
                  and t["status"] not in ("done", "closed"))
    pending_v = sum(1 for t in tasks if t.get("status") == "pending_verification")
    return {
        "open": open_count,
        "critical": critical,
        "overdue": overdue,
        "pending_verification": pending_v,
    }


HIERARCHY = {
    "CEO": ["KVP", "PM", "FIN"],
    "KVP": ["HUNTER", "FARMER", "AM"],
    "PM": ["TECH", "FOREMAN", "SERVICE"],
    "FOREMAN": ["BRIGADE"],
    "BRIGADE": ["INSTALLER"],
}

REPORTS_TO = {
    "KVP": "CEO", "PM": "CEO", "FIN": "CEO",
    "HUNTER": "KVP", "FARMER": "KVP", "AM": "KVP",
    "TECH": "PM", "FOREMAN": "PM", "SERVICE": "PM",
    "BRIGADE": "FOREMAN", "INSTALLER": "BRIGADE",
}


def parse_library_index() -> list[dict]:
    """Parsuje _LIBRARY/INDEX.md do listy dokumentów."""
    content = read_file(VAULT / "_LIBRARY" / "INDEX.md")
    docs = []
    for line in content.splitlines():
        if not line.strip().startswith("|"):
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) < 8:
            continue
        if cols[0].startswith("-") or cols[0] == "ID":
            continue
        doc_id = cols[0]
        name = cols[1]
        file_path = cols[2]
        doc_type_raw = cols[3].lower()
        version = cols[5] if len(cols) > 5 else ""
        owner = cols[7] if len(cols) > 7 else ""
        status = cols[8] if len(cols) > 8 else "aktywny"

        # Normalize type
        if "sop" in doc_type_raw:
            doc_type = "sop"
        elif "jd" in doc_type_raw:
            doc_type = "jd"
        elif "instrukcj" in doc_type_raw or "instrukcje" in doc_type_raw:
            doc_type = "instrukcja"
        elif "system" in doc_type_raw:
            doc_type = "system"
        else:
            doc_type = doc_type_raw

        docs.append({
            "id": doc_id,
            "name": name,
            "file": file_path,
            "type": doc_type,
            "version": version or "—",
            "owner": owner,
            "status": status,
        })
    return docs


RACI_MATRIX_RAW = {
    "SOP-01": {"CEO": "I", "KVP": "A", "HUNTER": "R", "FARMER": "R", "AM": "I"},
    "SOP-02": {"AM": "A", "KVP": "C", "PM": "I"},
    "SOP-03": {"PM": "A", "TECH": "C", "FOREMAN": "C", "AM": "I", "FIN": "C"},
    "SOP-04": {"PM": "A", "KVP": "C", "AM": "I"},
    "SOP-05": {"PM": "A", "TECH": "R", "FOREMAN": "C", "FIN": "C"},
    "SOP-06": {"FOREMAN": "A", "BRIGADE": "R", "PM": "I"},
    "SOP-07": {"PM": "A", "FOREMAN": "R", "FIN": "I"},
    "SOP-08": {"SERVICE": "A", "AM": "R", "PM": "I"},
    "SOP-09": {"FIN": "A", "PM": "R"},
    "SOP-10": {"FIN": "A", "PM": "C"},
    "SOP-11": {"PM": "A", "AM": "R"},
}

ALL_ROLES = ["CEO", "KVP", "HUNTER", "FARMER", "PM", "TECH",
             "FOREMAN", "BRIGADE", "INSTALLER", "AM", "FIN", "SERVICE"]

# Role → relevant SOPs (A or R)
ROLE_DOCS_MAP = {
    "CEO":      ["DOC-SYS-001", "DOC-SYS-002"],
    "KVP":      ["DOC-JD-001", "DOC-SOP-003", "DOC-SOP-014", "DOC-INS-004",
                 "DOC-INS-005", "DOC-INS-006"],
    "HUNTER":   ["DOC-JD-002", "DOC-SOP-003", "DOC-INS-005"],
    "FARMER":   ["DOC-JD-003", "DOC-SOP-003"],
    "PM":       ["DOC-JD-004", "DOC-SOP-005", "DOC-SOP-006", "DOC-SOP-007",
                 "DOC-SOP-008", "DOC-SOP-009", "DOC-SOP-013", "DOC-INS-007"],
    "TECH":     ["DOC-JD-006", "DOC-INS-003", "DOC-INS-008", "DOC-INS-009", "DOC-INS-010"],
    "FOREMAN":  ["DOC-JD-007", "DOC-SOP-008", "DOC-SOP-009", "DOC-INS-001",
                 "DOC-INS-002", "DOC-INS-003"],
    "BRIGADE":  ["DOC-JD-008", "DOC-SOP-008"],
    "INSTALLER":["DOC-JD-009", "DOC-SOP-008"],
    "AM":       ["DOC-SOP-004", "DOC-SOP-010"],
    "FIN":      ["DOC-SOP-011", "DOC-SOP-012"],
    "SERVICE":  ["DOC-SOP-010"],
}


# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/api/status")
def get_status():
    return {"ok": True, "timestamp": datetime.now().isoformat()}


@app.get("/api/dashboard")
def get_dashboard():
    tasks = get_tasks_combined()
    return {
        "tasks": tasks,
        "stats": get_stats(tasks),
    }


@app.get("/api/org")
def get_org():
    config = load_config()
    roles_data = {}

    for role, rdata in config.get("roles", {}).items():
        primary_id = rdata.get("primary_chat_id")
        person = None
        active = False

        if primary_id:
            worker = config["workers"].get(str(primary_id), {})
            person = worker.get("person")
            active = bool(person)
        else:
            persons = [p for p in rdata.get("persons", [])
                       if not p.get("to") and p.get("person")]
            if persons:
                person = persons[0]["person"]

        roles_data[role] = {
            "role": role,
            "person": person,
            "chat_id": primary_id,
            "active": active,
            "reports_to": REPORTS_TO.get(role),
        }

    return {
        "roles": roles_data,
        "hierarchy": HIERARCHY,
    }


@app.get("/api/worker/{chat_id}")
def get_worker(chat_id: str):
    config = load_config()
    worker = config["workers"].get(chat_id, {})
    if not worker:
        return JSONResponse(status_code=404, content={"error": "Worker not found"})

    roles = worker.get("roles", [])
    primary_role = worker.get("primary_role", roles[0] if roles else "")

    # Zadania tego pracownika
    tasks = get_tasks_combined()
    worker_tasks = [
        t for t in tasks
        if t.get("owner_person", "") in worker.get("person", "")
        or any(r in (t.get("owner_role", "")) for r in roles)
    ]

    # KPI z vault/ROLE/
    kpi = []
    for role in roles:
        role_file = VAULT / "ROLE" / f"{role}.md"
        if role_file.exists():
            content = role_file.read_text(encoding="utf-8")
            lines = content.splitlines()
            kpi_start = next(
                (i for i, l in enumerate(lines) if "KPI" in l and "##" in l),
                None
            )
            if kpi_start is not None:
                for line in lines[kpi_start + 1:kpi_start + 10]:
                    if line.startswith("|") and not line.startswith("|-"):
                        cols = [c.strip() for c in line.split("|")[1:-1]]
                        if len(cols) >= 2 and cols[0] and cols[0] != "KPI":
                            kpi.append(f"{cols[0]}: {cols[2] if len(cols) > 2 else cols[1]}")

    # Dokumenty
    all_docs = parse_library_index()
    doc_ids = set()
    for role in roles:
        doc_ids.update(ROLE_DOCS_MAP.get(role, []))
    documents = [d for d in all_docs if d["id"] in doc_ids]

    return {
        "person": worker.get("person", ""),
        "roles": roles,
        "primary_role": primary_role,
        "registered": worker.get("registered", ""),
        "tasks": worker_tasks[:10],
        "kpi": kpi[:8],
        "documents": documents[:10],
    }


@app.get("/api/documents/{role}")
def get_documents(role: str):
    all_docs = parse_library_index()

    if role == "ALL":
        return {"role": "ALL", "documents": all_docs}

    role_upper = role.upper()
    doc_ids = set(ROLE_DOCS_MAP.get(role_upper, []))

    if not doc_ids:
        # Fallback: filter by owner field
        docs = [d for d in all_docs
                if role_upper in d.get("owner", "").upper()
                and d.get("status") != "gap"]
    else:
        docs = [d for d in all_docs if d["id"] in doc_ids]

    return {"role": role_upper, "documents": docs}


@app.get("/api/raci")
def get_raci():
    roles = ALL_ROLES[:6]  # Frontend shows 6 columns
    matrix = []
    for sop, sop_roles in RACI_MATRIX_RAW.items():
        row = {"sop": sop}
        for role in roles:
            row[role] = sop_roles.get(role, "")
        matrix.append(row)
    return {"roles": roles, "matrix": matrix}


@app.get("/api/analytics/weekly")
async def analytics_weekly():
    from datetime import timedelta

    vault = VAULT / "ZADANIA"
    today = date.today()
    weeks = {}
    for i in range(4):
        start = today - timedelta(days=today.weekday() + 7 * i)
        key = start.strftime("%d.%m")
        weeks[key] = {"week": key, "open": 0, "closed": 0,
                      "in_progress": 0, "overdue": 0}

    if vault.exists():
        for f in vault.glob("*.md"):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                date_m = re.search(r'^date_created:\s*(\d{4}-\d{2}-\d{2})',
                                   content, re.MULTILINE)
                if not date_m:
                    date_m = re.search(r'^date:\s*(\d{4}-\d{2}-\d{2})',
                                       content, re.MULTILINE)
                status_m = re.search(r'^status:\s*(\w+)',
                                     content, re.MULTILINE)
                deadline_m = re.search(r'^deadline:\s*(\d{4}-\d{2}-\d{2})',
                                       content, re.MULTILINE)
                if not (date_m and status_m):
                    continue
                task_date = datetime.strptime(
                    date_m.group(1), "%Y-%m-%d").date()
                status = status_m.group(1)
                from datetime import timedelta as td
                week_start = task_date - td(days=task_date.weekday())
                key = week_start.strftime("%d.%m")
                if key in weeks:
                    if status == "closed":
                        weeks[key]["closed"] += 1
                    elif status == "in_progress":
                        weeks[key]["in_progress"] += 1
                    elif deadline_m:
                        dl = datetime.strptime(
                            deadline_m.group(1), "%Y-%m-%d").date()
                        if dl < today and status not in ("closed", "cancelled"):
                            weeks[key]["overdue"] += 1
                        else:
                            weeks[key]["open"] += 1
                    else:
                        weeks[key]["open"] += 1
            except Exception:
                pass

    return {"weeks": list(reversed(list(weeks.values())))}


@app.get("/api/analytics/roles")
async def analytics_roles():
    config = load_config()
    vault = VAULT / "ZADANIA"
    roles_cfg = config.get("roles", {})
    workers = config.get("workers", {})

    result = []
    for role_name, role_data in roles_cfg.items():
        person = None
        for w in workers.values():
            if role_name in w.get("roles", []) and w.get("active"):
                person = w.get("person")
                break

        tasks_open = tasks_closed = 0
        if vault.exists():
            for f in vault.glob("*.md"):
                try:
                    content = f.read_text(encoding="utf-8", errors="ignore")
                    if f"owner: {role_name}" not in content and \
                       f"owner_role: {role_name}" not in content:
                        continue
                    status_m = re.search(r'^status:\s*(\w+)',
                                         content, re.MULTILINE)
                    if status_m:
                        if status_m.group(1) == "closed":
                            tasks_closed += 1
                        else:
                            tasks_open += 1
                except Exception:
                    pass

        result.append({
            "role": role_name,
            "person": person or "\u2014",
            "active": bool(role_data.get("primary_chat_id")),
            "tasks_open": tasks_open,
            "tasks_closed": tasks_closed,
        })

    return {"roles": [r for r in result
                      if r["tasks_open"] > 0 or r["tasks_closed"] > 0
                      or r["active"]]}


@app.get("/api/analytics/scribe")
async def analytics_scribe():
    """Statystyki transkrypcji SCRIBE."""
    spotkania = VAULT / "SPOTKANIA"
    stats = {"total_meetings": 0, "total_tasks": 0,
             "total_decisions": 0, "formats": {}}

    if spotkania.exists():
        for f in spotkania.glob("*.md"):
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                if "type: meeting" not in content:
                    continue
                stats["total_meetings"] += 1
                tc = re.search(r'tasks:\s*(\d+)', content)
                dc = re.search(r'decisions:\s*(\d+)', content)
                src = re.search(r'format:\s*(.+)', content)
                if tc:
                    stats["total_tasks"] += int(tc.group(1))
                if dc:
                    stats["total_decisions"] += int(dc.group(1))
                if src:
                    fmt = src.group(1).strip()[:20]
                    stats["formats"][fmt] = stats["formats"].get(fmt, 0) + 1
            except Exception:
                pass

    return stats


# ─────────────────────────────────────────────
# GATE ENDPOINTS
# ─────────────────────────────────────────────

@app.get("/api/gates")
async def list_gates():
    """Lista dostępnych GATE."""
    return {"gates": [
        {"gate_id": gid, "sop": g["sop"], "title": g["title"]}
        for gid, g in GATES.items()
    ]}


@app.get("/api/gates/{gate_id}")
async def get_gate(gate_id: str):
    """Definicja GATE — pola formularza."""
    defn = get_gate_definition(gate_id)
    if not defn:
        return JSONResponse(status_code=404, content={"error": f"GATE {gate_id} nie istnieje"})
    return defn


@app.post("/api/gates/{gate_id}/submit")
async def submit_gate(gate_id: str, request: Request):
    """Walidacja i submit formularza GATE."""
    data = await request.json()
    result = validate_gate(gate_id, data)

    if result["passed"]:
        filename = create_project_card(gate_id, data)
        result["project_card"] = filename

    return result


# ─────────────────────────────────────────────
# PROCESS GRAPH
# ─────────────────────────────────────────────

PROCESS_NODES = [
    # SOPy
    {"id": "SOP-01", "label": "Sprzedaz", "type": "sop", "desc": "Proces sprzedazy B2C/B2B"},
    {"id": "SOP-02", "label": "Zapytania", "type": "sop", "desc": "Obsluga zapytan i zgloszen klientow"},
    {"id": "SOP-03", "label": "Zmiany/CR", "type": "sop", "desc": "Zarzadzanie zmianami i pracami dodatkowymi"},
    {"id": "SOP-04", "label": "Przekazanie", "type": "gate", "desc": "Przekazanie projektu ze sprzedazy do realizacji"},
    {"id": "SOP-05", "label": "Planowanie", "type": "sop", "desc": "Planowanie realizacji i zasobow"},
    {"id": "SOP-06", "label": "Montaz", "type": "gate", "desc": "Zarzadzanie realizacja i kontrola montazu"},
    {"id": "SOP-07", "label": "Zakupy", "type": "sop", "desc": "Zakupy i materialy"},
    {"id": "SOP-08", "label": "Serwis", "type": "sop", "desc": "Serwis i reklamacje"},
    {"id": "SOP-09", "label": "Zamkniecie", "type": "gate", "desc": "Finansowe zamkniecie projektu"},
    {"id": "SOP-10", "label": "Faktury", "type": "sop", "desc": "Rachunki i platnosci"},
    {"id": "SOP-11", "label": "Statusy", "type": "sop", "desc": "Statusy projektu i komunikacja"},
    # Role
    {"id": "CEO", "label": "CEO", "type": "role", "desc": "Dyrektor generalny"},
    {"id": "KVP", "label": "KVP", "type": "role", "desc": "Kierownik dzialu sprzedazy"},
    {"id": "PM", "label": "PM", "type": "role", "desc": "Menedzer projektow"},
    {"id": "TECH", "label": "TECH", "type": "role", "desc": "Glowny specjalista techniczny"},
    {"id": "FOREMAN", "label": "FOREMAN", "type": "role", "desc": "Prораб / Kierownik budowy"},
    {"id": "BRIGADE", "label": "BRIGADE", "type": "role", "desc": "Brygadzista"},
    {"id": "INSTALLER", "label": "INSTALLER", "type": "role", "desc": "Montazysta"},
    {"id": "AM", "label": "AM", "type": "role", "desc": "Account Manager"},
    {"id": "FIN", "label": "FIN", "type": "role", "desc": "Finanse"},
    {"id": "SERVICE", "label": "SERVICE", "type": "role", "desc": "Serwis"},
    {"id": "HUNTER", "label": "HUNTER", "type": "role", "desc": "Menedzer sprzedazy (akwizycja)"},
    {"id": "FARMER", "label": "FARMER", "type": "role", "desc": "Menedzer sprzedazy (relacje)"},
]

PROCESS_LINKS = [
    # Flow glowny
    {"source": "SOP-01", "target": "SOP-04", "type": "flow"},
    {"source": "SOP-04", "target": "SOP-05", "type": "flow"},
    {"source": "SOP-05", "target": "SOP-06", "type": "flow"},
    {"source": "SOP-06", "target": "SOP-09", "type": "flow"},
    {"source": "SOP-09", "target": "SOP-10", "type": "flow"},
    # Boczne
    {"source": "SOP-01", "target": "SOP-02", "type": "flow"},
    {"source": "SOP-01", "target": "SOP-03", "type": "flow"},
    {"source": "SOP-05", "target": "SOP-07", "type": "flow"},
    {"source": "SOP-06", "target": "SOP-08", "type": "flow"},
    {"source": "SOP-06", "target": "SOP-11", "type": "flow"},
    # RACI: Accountable
    {"source": "KVP", "target": "SOP-01", "type": "accountable"},
    {"source": "AM", "target": "SOP-02", "type": "accountable"},
    {"source": "PM", "target": "SOP-03", "type": "accountable"},
    {"source": "PM", "target": "SOP-04", "type": "accountable"},
    {"source": "PM", "target": "SOP-05", "type": "accountable"},
    {"source": "FOREMAN", "target": "SOP-06", "type": "accountable"},
    {"source": "PM", "target": "SOP-07", "type": "accountable"},
    {"source": "SERVICE", "target": "SOP-08", "type": "accountable"},
    {"source": "FIN", "target": "SOP-09", "type": "accountable"},
    {"source": "FIN", "target": "SOP-10", "type": "accountable"},
    {"source": "PM", "target": "SOP-11", "type": "accountable"},
    # Rola hierarchy
    {"source": "CEO", "target": "KVP", "type": "hierarchy"},
    {"source": "CEO", "target": "PM", "type": "hierarchy"},
    {"source": "CEO", "target": "FIN", "type": "hierarchy"},
    {"source": "KVP", "target": "HUNTER", "type": "hierarchy"},
    {"source": "KVP", "target": "FARMER", "type": "hierarchy"},
    {"source": "KVP", "target": "AM", "type": "hierarchy"},
    {"source": "PM", "target": "TECH", "type": "hierarchy"},
    {"source": "PM", "target": "FOREMAN", "type": "hierarchy"},
    {"source": "PM", "target": "SERVICE", "type": "hierarchy"},
    {"source": "FOREMAN", "target": "BRIGADE", "type": "hierarchy"},
    {"source": "BRIGADE", "target": "INSTALLER", "type": "hierarchy"},
]


def _get_node_status(node_id: str) -> str:
    """Sprawdza status wezla na podstawie vault."""
    # GATE: sprawdz czy sa otwarte zadania
    zadania_dir = VAULT / "ZADANIA"
    if not zadania_dir.exists():
        return "ok"
    for f in zadania_dir.glob("*.md"):
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
            if node_id not in content:
                continue
            status_m = re.search(r'^status:\s*(\w+)', content, re.MULTILINE)
            deadline_m = re.search(r'^deadline:\s*(\d{4}-\d{2}-\d{2})', content, re.MULTILINE)
            if status_m and status_m.group(1) not in ("closed", "cancelled"):
                if deadline_m:
                    dl = datetime.strptime(deadline_m.group(1), "%Y-%m-%d").date()
                    if dl < date.today():
                        return "critical"
                return "active"
        except Exception:
            pass
    return "ok"


@app.get("/api/process-graph")
async def process_graph():
    """Graf procesow AVM: nodes + links z live statusem."""
    nodes = []
    for n in PROCESS_NODES:
        node = dict(n)
        node["status"] = _get_node_status(n["id"])
        nodes.append(node)

    # Sprawdz obsadzenie rol
    try:
        config = load_config()
        workers = config.get("workers", {})
        for node in nodes:
            if node["type"] == "role":
                role_id = node["id"]
                assigned = any(
                    role_id in w.get("roles", []) and w.get("active")
                    for w in workers.values()
                )
                if not assigned:
                    node["status"] = "unassigned"
    except Exception:
        pass

    return {"nodes": nodes, "links": PROCESS_LINKS}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765)
