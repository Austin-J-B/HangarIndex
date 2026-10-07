"""Local-first aerospace maintenance document search and RAG proof of concept."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
import zipfile
import numpy as np
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent


def load_local_env() -> None:
    """Read simple KEY=VALUE settings without an extra dotenv dependency."""
    env_file = BASE_DIR / ".env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


load_local_env()
WEB_DIR = BASE_DIR / "web"
DATA_DIR = Path(os.environ.get("HANGAR_DATA_DIR", BASE_DIR / ".data"))
DB_PATH = DATA_DIR / "index.sqlite3"
SETTINGS_PATH = DATA_DIR / "settings.json"
EMBEDDINGS_CACHE_PATH = DATA_DIR / "embeddings.sqlite3"
_embeddings_flag = os.environ.get("HANGAR_EMBEDDINGS", "1").strip().lower() or "1"
if _embeddings_flag not in {"", "0", "false", "no", "off", "1", "true", "yes", "on"}:
    raise RuntimeError("HANGAR_EMBEDDINGS must be a boolean value (0/1, false/true, no/yes, or off/on).")
EMBEDDINGS_ENABLED = _embeddings_flag in {"1", "true", "yes", "on"}
EMBEDDING_MODEL = "nomic-embed-text"
EMBEDDING_POOL = 60
RRF_CONSTANT = 60
VECTOR_RRF_WEIGHT = 2.0
EMBEDDING_BATCH_SIZE = 128
EMBEDDING_RETRY_BASE_SECONDS = 2
EMBEDDING_RETRY_MAX_SECONDS = 60
EMBEDDING_STATE: dict[str, Any] = {
    "enabled": EMBEDDINGS_ENABLED,
    "state": "warming" if EMBEDDINGS_ENABLED else "disabled",
    "model": EMBEDDING_MODEL,
    "embedded": 0,
    "total": 0,
    "error": "",
}
EMBEDDING_STATE_LOCK = threading.Lock()
EMBEDDING_REFRESH_LOCK = threading.Lock()
EMBEDDING_REFRESH_REQUESTED = False
EMBEDDING_RETRY_LOCK = threading.Lock()
EMBEDDING_RETRY_TIMER: threading.Timer | None = None
EMBEDDING_RETRY_TOKEN = 0
EMBEDDING_RETRY_ATTEMPT = 0
EMBEDDING_IDS: tuple[int, ...] = ()
EMBEDDING_MATRIX: np.ndarray | None = None
SUPPORTED = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".csv", ".html", ".htm", ".xml", ".json", ".log"}
SOURCE_REGISTER_NAME = "source-register.md"


def resolve_bind_host() -> tuple[str, bool]:
    configured = os.environ.get("HANGAR_BIND", os.environ.get("HOST", "127.0.0.1")).strip()
    if configured.lower() == "localhost":
        return "127.0.0.1", True
    try:
        address = ipaddress.ip_address(configured)
    except ValueError as exc:
        raise RuntimeError("HANGAR_BIND must be localhost, a loopback IP, or an assigned Tailscale IPv4 in 100.64.0.0/10.") from exc
    if address.is_loopback:
        return str(address), True
    if isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network("100.64.0.0/10"):
        return str(address), False
    raise RuntimeError("HANGAR_BIND may only use localhost or an assigned Tailscale IPv4 in 100.64.0.0/10.")


BIND_HOST, BIND_IS_LOCAL = resolve_bind_host()
TAILNET_ACCESS = not BIND_IS_LOCAL
ACCESS_TOKEN = os.environ.get("HANGAR_ACCESS_TOKEN", "")
if TAILNET_ACCESS and not ACCESS_TOKEN:
    raise RuntimeError("HANGAR_ACCESS_TOKEN is required when HANGAR_BIND uses a Tailscale address.")
if TAILNET_ACCESS and len(ACCESS_TOKEN) < 16:
    raise RuntimeError("HANGAR_ACCESS_TOKEN must be at least 16 characters for Tailscale access.")
SESSION_COOKIE = "hangar_session"
SESSION_MAX_AGE = 12 * 60 * 60
LOGIN_BACKOFF_MAX_SECONDS = 300
LOGIN_ATTEMPTS: dict[str, tuple[int, float]] = {}
LOGIN_ATTEMPTS_LOCK = threading.Lock()
_configured_hosts = [host.strip().lower().strip("[]") for host in os.environ.get("HANGAR_ALLOWED_HOSTS", "").split(",") if host.strip()]
if any("*" in host or "/" in host for host in _configured_hosts):
    raise RuntimeError("HANGAR_ALLOWED_HOSTS accepts exact hostnames or IP addresses, not patterns.")
ALLOWED_HOSTS = ({BIND_HOST.lower(), *_configured_hosts} if TAILNET_ACCESS
                 else {"localhost", "127.0.0.1", "::1", *_configured_hosts})
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(120 * 1024 * 1024)))
CHUNK_CHARS = 1250
CHUNK_OVERLAP = 180
IGNORE_DIRS = {"$RECYCLE.BIN", "SYSTEM VOLUME INFORMATION", ".git", "node_modules", "__pycache__", ".data"}
TOKEN_RE = re.compile(r"[\w]+(?:[-./][\w]+)*", re.UNICODE)

DATA_DIR.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=45)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def initialize_db() -> None:
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                path TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                extension TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                modified_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                parse_status TEXT NOT NULL DEFAULT 'indexed',
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                page INTEGER,
                content TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS chunks_path_idx ON chunks(path);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunk_search USING fts5(
                path UNINDEXED, name, folder, doc_type, content,
                tokenize = 'unicode61 remove_diacritics 2'
            );
            CREATE TABLE IF NOT EXISTS caution_passages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
                sentence_ordinal INTEGER NOT NULL,
                page INTEGER,
                content TEXT NOT NULL,
                topic_keywords TEXT NOT NULL,
                title_keywords TEXT NOT NULL DEFAULT '',
                UNIQUE(path,page,content)
            );
            CREATE INDEX IF NOT EXISTS caution_passages_path_idx ON caution_passages(path);
            CREATE VIRTUAL TABLE IF NOT EXISTS caution_search USING fts5(
                path UNINDEXED, page UNINDEXED, name, doc_type, topic_keywords, title_keywords, content,
                tokenize = 'unicode61 remove_diacritics 2'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS caution_vocab USING fts5vocab(caution_search, 'col');
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        row = db.execute("SELECT value FROM app_meta WHERE key='index_generation'").fetchone()
        if row is None:
            db.execute("INSERT INTO app_meta(key,value) VALUES ('index_generation',?)", (uuid.uuid4().hex,))


initialize_db()


def read_settings() -> dict[str, Any]:
    saved: dict[str, Any] = {}
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    root = os.environ.get("CORPUS_DIR") or saved.get("corpus_dir") or str(BASE_DIR / "corpus" / "FAA-Public")
    return {"corpus_dir": root}


SETTINGS = read_settings()
INDEX_STATE: dict[str, Any] = {
    "running": False, "phase": "idle", "current": "", "processed": 0,
    "total": 0, "indexed": 0, "unchanged": 0, "skipped": 0,
    "errors": 0, "started_at": None, "finished_at": None, "message": "Ready to index a folder.",
}
STATE_LOCK = threading.Lock()
REGULATORY_DOC_TYPES = ("FAA ", "EASA ", "AESA ", "CAAC ", "TCCA ")
NON_AUTHORITY_DOC_TYPES = ("FAA Maintenance Training Handbook",)
AUTHORITY_NAME_PREFIXES = (
    "FAA_", "EASA_", "AESA_", "CAAC_", "TCCA_", "UK_CAA_", "UK_REGULATION_", "BIS_", "ITAR_", "OSHA_",
)
CAUTION_INDEX_VERSION = "9"
DOC_TYPE_INDEX_VERSION = "3"
AVIATION_AUTHORITY_NAME_PREFIXES = (
    "FAA_", "EASA_", "AESA_", "CAAC_", "TCCA_", "UK_CAA_", "UK_REGULATION_",
)
SHOP_SAFETY_QUERY_RE = re.compile(
    r"\b(?:osha|shop safety|workplace safety|occupational safety|employee safety|worker safety|"
    r"ppe|personal protective equipment|respirator|exposure|hazard communication|hazcom|"
    r"industrial hygiene|safety data sheet|sds)\b",
    re.IGNORECASE,
)
BIBLIOGRAPHY_QUERY_RE = re.compile(r"\b(?:references?|bibliography|works cited|literature cited|citations?)\b", re.IGNORECASE)
BIBLIOGRAPHY_HEADING_RE = re.compile(
    r"(?im)^\s*(?:\d{1,4}\s+)?(?:references?|reference list|bibliography|works cited|literature cited)"
    r"(?:\s+\(?\s*(?:continued|cont\.?)\s*\)?)?\s*$"
)
BIBLIOGRAPHIC_ENTRY_RE = re.compile(r"(?m)^\s*\d{1,3}[.)]\s+[A-Z][A-Za-z'’-]+,\s*[A-Z](?:\.|\s)")
BIBLIOGRAPHIC_MARKER_RE = re.compile(
    r"\b(?:journal|transactions?|proceedings|symposium|report|contract\s+(?:no\.?|number))\b|\b(?:vol|pp)\.",
    re.IGNORECASE,
)
CAUTION_CUE_RE = re.compile(
    r"\b(?:must\s+not|shall\s+not|do\s+not|not\s+permitted|prohibited|never|warning|caution|unless)\b",
    re.IGNORECASE,
)
CAUTION_STRONG_CUE_RE = re.compile(
    r"\b(?:must\s+not|shall\s+not|do\s+not|not\s+permitted|prohibited|never|warning|caution)\b",
    re.IGNORECASE,
)
CAUTION_FORM_INSTRUCTION_RE = re.compile(
    r"\b(?:check\s+this\s+box|(?:do\s+not|must\s+not|shall\s+not)\s+complete\s+block\s+\d+)\b",
    re.IGNORECASE,
)
CAUTION_UNLESS_TOPIC_RE = re.compile(
    r"\b(?:attack|corros\w*|damag\w*|fail\w*|hazard\w*|danger\w*|injur\w*|fire|fractur\w*|crack\w*|"
    r"leak\w*|wear\w*|electrical|pressur\w*|saf\w*|contamin\w*|inhibit\w*|interfer\w*|fatigue|load\w*)\b",
    re.IGNORECASE,
)
PROCEDURE_QUERY_RE = re.compile(
    r"\b(?:how\s+(?:do|can|to)|steps?\s+to|procedure(?:s)?(?:\s+for)?|method(?:s)?(?:\s+for)?|"
    r"remov(?:e|al)|clean(?:ing)?|treat(?:ment)?|inhibitor|apply)\b",
    re.IGNORECASE,
)
ALUMINUM_CORROSION_REMOVAL_RE = re.compile(
    r"(?=.*\balumin(?:um|ium)\b)(?=.*\bcorros\w*\b)(?=.*\b(?:remov\w*|clean\w*|how|procedure|method)\b)",
    re.IGNORECASE,
)
PROCEDURE_HEADING_RE = re.compile(
    r"\b(?:removal|cleaning|clean|inhibitors?|treatment|rework|surface preparation|corrosion control|restoration)\b",
    re.IGNORECASE,
)
PROCEDURE_ACTION_RE = re.compile(
    r"\b(?:remove|clean|apply|treat|rinse|sand|brush|polish|strip|inhibit|protect|inspect|blend|coat|restore|neutralize)\b",
    re.IGNORECASE,
)
CAUTION_TOPIC_STOP_WORDS = {
    "must", "shall", "not", "do", "never", "warning", "caution", "unless", "prohibited",
    "permitted", "forbidden", "cannot", "should", "may", "will", "required", "require",
    "requirements", "use", "used", "using", "aircraft", "maintenance", "authority", "faa",
    "easa", "aesa", "caac", "tcca", "advisory", "circular", "chapter", "section", "part",
    "paragraph", "figure", "note", "information", "procedure", "procedures", "shall", "must", "under",
    "repair", "station", "apply", "applied", "govern", "governing", "standard", "standards", "practice",
}
CAUTION_EXACT_TOPIC_TERMS = {
    "splic", "weld", "solder", "crimp", "shield", "exciter", "antenna", "doubler",
    "chemical", "hydrogen", "embrittlement", "intergranular", "exfoliation", "galvanic", "insert",
    "sup", "suspect", "unapprov",
}
CAUTION_TOPIC_PAIR_REQUIREMENTS = {
    "splic": {"wire"},
    "shield": {"electrical", "separation", "circuit", "exciter"},
}
ANSWER_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
ANSWER_ABBREVIATION = re.compile(
    r"\b(?:u\.s|u\.k|e\.u|u\.n|e\.g|i\.e|etc|vs|no|figs?|sec|ch|para|pp|p|rev|vol|ed|inc|ltd|dr|mr|mrs|ms|st|jr|sr|approx)\.",
    re.IGNORECASE,
)
SOURCE_CITATION = re.compile(r"\[((?:S\d+)(?:\s*[,;]\s*S\d+)*)\]")
NO_EVIDENCE_DISCLAIMER_RE = re.compile(
    r"^(?:"
    r"(?:the\s+)?(?:(?:provided|supplied|retrieved)\s+)?excerpts?\s+"
    r"(?:do|does|did)\s+not\s+(?:contain|establish|show|provide|address|identify|specify)\b"
    r"|no\s+(?:(?:authority[- ]approved|approved|task[- ]specific|specific|matching|supporting)\s+)*"
    r"(?:repair\s+)?(?:data|procedures?|steps?|instructions?|evidence|sources?|guidance)\s+"
    r"(?:is|are|was|were)\s+(?:provided|available|included|identified|shown|present|found|located)\b"
    r")",
    re.IGNORECASE,
)


class IndexRequest(BaseModel):
    corpus_dir: str | None = None


class QueryRequest(BaseModel):
    query: str = Field(min_length=2, max_length=1200)
    limit: int = Field(default=12, ge=1, le=40)


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1200)
    mode: Literal["quick", "thorough"] = "thorough"


class SettingsRequest(BaseModel):
    corpus_dir: str = Field(min_length=1, max_length=2048)


class OpenRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class LoginRequest(BaseModel):
    token: str = Field(min_length=16, max_length=4096)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify_doc(name: str, text: str) -> str:
    sample = (name + " " + text[:5000]).lower()
    if re.search(r"\bbis_|bureau of industry and security|export administration regulations", sample):
        return "BIS Export Control Regulation"
    if re.search(r"\bacp_der_", sample):
        return "ACP DER Repair Listing"
    if re.search(r"\bacp_", sample):
        return "ACP Supplier / Approval Reference"
    if re.search(r"\bjpe_|jetpartsengineering|jet parts engineering", sample):
        return "JPE Supplier / Parts Reference"
    if re.search(r"\bosha_|occupational safety and health administration|29\s*cfr\s*(?:part\s*)?1910", sample):
        return "OSHA Workplace Safety Regulation"
    # Keep supplier and product references distinct from authority material even
    # when those documents mention FAA rules or military specifications.
    if Path(name).name.lower() == "source-register.md":
        return "Corpus Source Register"
    if re.search(r"lufthansa(?:\s+technik)?", sample):
        return "Lufthansa Supplier / Quality Context"
    if re.search(r"ac[_\s-]*43[-.]?4b|43-4b corrosion control", sample):
        return "FAA Corrosion Control Guidance"
    if re.search(r"sherwin|\bf93\w*|mil-prf-85285|aerospace coating|aircraft topcoat", sample):
        return "Aerospace Coating / Product Reference"
    if re.search(r"mil-dtl-5541|aluminum conversion coatings|aluminium conversion coatings", sample):
        return "Materials / Aluminum Corrosion Reference"
    if re.search(r"faa-h-8083-30b|aviation maintenance technician handbook", sample):
        return "FAA Maintenance Training Handbook"
    if re.search(r"nasa_|ntrs-20\d{8}|materials data handbook.*titanium|smart coatings for corrosion|environmental corrosion.*aluminum|corrosion behavior of primer coated", sample):
        return "Materials Science / Corrosion Research"
    regulatory = [
        (r"\baesa\b|ac-mto-p01", "AESA Part-145 Guidance"),
        (r"\bcaac\b|ccar[- ]?145", "CAAC / CCAR-145"),
        (r"transport canada|\btcca\b|standard\s+573|ac\s*573-008", "TCCA AMO Standard / Guidance"),
        (r"airworthiness directive|\b(?:faa\s*)?ad\s?\d{4}[- ]\d{2}[- ]\d{2}", "FAA Airworthiness Directive"),
        (r"advisory circular|\b(?:faa[_\s-]+)?ac[_\s-]*(?:20-\d{2}[a-z]?|43\.\d{2}-\d[a-z]?|145-\d{1,2}[a-z]?|\d{2}[-.]\d{1,2}[a-z]?)\b", "FAA Advisory Circular"),
        (r"easa|\beu\s*1321[-/]2014\b|continuing airworthiness", "EASA Continuing Airworthiness Regulation / Guidance"),
        (r"(?<![a-z0-9])14[_\s-]*cfr(?![a-z0-9])|\bpart\s*145\b|\bpart\s*43\b|federal aviation regulations", "FAA Regulation / 14 CFR"),
        (r"faa order|faa policy|faa guidance", "FAA Order / Guidance"),
        (r"easa|\bcs-?25\b|\bpart-?21\b", "EASA / Aviation Regulation"),
    ]
    for pattern, label in regulatory:
        if re.search(pattern, sample):
            return label
    manuals = [
        (r"component maintenance manual|\bcmm\b", "Component Maintenance Manual"),
        (r"aircraft maintenance manual|\bamm\b", "Aircraft Maintenance Manual"),
        (r"structural repair manual|\bsrm\b", "Structural Repair Manual"),
        (r"illustrated parts catalog|illustrated parts catalogue|\bipc\b", "Illustrated Parts Catalog"),
        (r"troubleshooting manual|fault isolation manual|\bt(?:sm|f?im)\b", "Troubleshooting / Fault Isolation Manual"),
        (r"maintenance planning document|\bmpd\b", "Maintenance Planning Document"),
        (r"service bulletin|\bsb\b", "Service Bulletin"),
        (r"service letter", "Service Letter"),
    ]
    for pattern, label in manuals:
        if re.search(pattern, sample):
            return label
    return "Other Maintenance Document"


def backfill_document_types() -> int:
    """Refresh stored labels when the classifier rules change, without reparsing files."""
    changed = 0
    with connect() as db:
        current = db.execute("SELECT value FROM app_meta WHERE key='doc_type_index_version'").fetchone()
        if current and current["value"] == DOC_TYPE_INDEX_VERSION:
            return 0
        documents = db.execute(
            "SELECT path,name,doc_type FROM documents WHERE parse_status='indexed' ORDER BY path"
        ).fetchall()
        for document in documents:
            chunks = db.execute(
                "SELECT content FROM chunks WHERE path=? ORDER BY ordinal LIMIT 4", (document["path"],)
            ).fetchall()
            text = " ".join(row["content"] for row in chunks)
            folder = Path(document["path"]).parent.name
            doc_type = classify_doc(f"{folder} {document['name']}", text)
            if doc_type == document["doc_type"]:
                continue
            db.execute("UPDATE documents SET doc_type=? WHERE path=?", (doc_type, document["path"]))
            db.execute("UPDATE chunk_search SET doc_type=? WHERE path=?", (doc_type, document["path"]))
            changed += 1
        db.execute(
            "INSERT INTO app_meta(key,value) VALUES('doc_type_index_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (DOC_TYPE_INDEX_VERSION,)
        )
    return changed


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden_depth += 1
        if tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3"} and not self.hidden_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            clean = data.strip()
            if clean:
                self.parts.append(clean)


def decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def extract_pages(path: Path, data: bytes) -> list[tuple[int | None, str]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError as exc:
            raise RuntimeError("PDF support needs pypdf. Run the included setup script first.") from exc
        import io
        reader = PdfReader(io.BytesIO(data))
        return [(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]
    if suffix == ".docx":
        with zipfile.ZipFile(path if not data else __import__("io").BytesIO(data)) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = []
        for p in root.findall(".//w:p", ns):
            value = "".join(t.text or "" for t in p.findall(".//w:t", ns)).strip()
            if value:
                paragraphs.append(value)
        return [(None, "\n".join(paragraphs))]
    if suffix == ".pptx":
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
            names = sorted((n for n in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.search(r"slide(\d+)", n).group(1)))
            result = []
            for i, name in enumerate(names, 1):
                root = ET.fromstring(archive.read(name))
                bits = [t.text or "" for t in root.iter() if t.tag.endswith("}t")]
                result.append((i, "\n".join(x for x in bits if x.strip())))
            return result
    if suffix == ".xlsx":
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as archive:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(t.text or "" for t in item.iter() if t.tag.endswith("}t")) for item in root]
            sheets = sorted((n for n in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)), key=lambda n: int(re.search(r"sheet(\d+)", n).group(1)))
            result = []
            for si, sheet in enumerate(sheets, 1):
                root = ET.fromstring(archive.read(sheet))
                rows = []
                for row in root.iter():
                    if not row.tag.endswith("}row"):
                        continue
                    cells = []
                    for cell in row:
                        if not cell.tag.endswith("}c"):
                            continue
                        value = next((child.text or "" for child in cell if child.tag.endswith("}v")), "")
                        if cell.attrib.get("t") == "s" and value.isdigit() and int(value) < len(shared):
                            value = shared[int(value)]
                        if value:
                            cells.append(f"{cell.attrib.get('r', '')}={value}")
                    if cells:
                        rows.append(" | ".join(cells))
                result.append((si, "\n".join(rows)))
            return result
    if suffix in {".html", ".htm"}:
        parser = TextExtractor()
        parser.feed(decode_bytes(data))
        return [(None, "\n".join(parser.parts))]
    if suffix in {".xml"}:
        try:
            root = ET.fromstring(data)
            return [(None, "\n".join(x.strip() for x in root.itertext() if x.strip()))]
        except ET.ParseError:
            return [(None, decode_bytes(data))]
    return [(None, decode_bytes(data))]


def chunk_pages(pages: Iterable[tuple[int | None, str]]) -> list[tuple[int | None, str]]:
    chunks: list[tuple[int | None, str]] = []
    for page, raw in pages:
        text = re.sub(r"[\t\r ]+", " ", raw)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            continue
        start = 0
        while start < len(text):
            end = min(start + CHUNK_CHARS, len(text))
            if end < len(text):
                break_at = text.rfind("\n", start + int(CHUNK_CHARS * .6), end)
                if break_at > start:
                    end = break_at
            piece = text[start:end].strip()
            if piece:
                chunks.append((page, piece))
            if end >= len(text):
                break
            start = max(start + 1, end - CHUNK_OVERLAP)
    return chunks


def is_authority_source(name: str, doc_type: str) -> bool:
    if doc_type in NON_AUTHORITY_DOC_TYPES or name.casefold().startswith("faa-h-8083-30b"):
        return False
    return doc_type.startswith(REGULATORY_DOC_TYPES) or name.upper().startswith(AUTHORITY_NAME_PREFIXES)


def replace_caution_passages(db: sqlite3.Connection, path: str, name: str, doc_type: str,
                             passages: list[tuple[int | None, int, str, str, str]]) -> None:
    db.execute("DELETE FROM caution_search WHERE path=?", (path,))
    db.execute("DELETE FROM caution_passages WHERE path=?", (path,))
    if not is_authority_source(name, doc_type):
        return
    for page, sentence_ordinal, content, topics, title_topics in passages:
        cur = db.execute(
            "INSERT OR IGNORE INTO caution_passages(path,sentence_ordinal,page,content,topic_keywords,title_keywords) VALUES(?,?,?,?,?,?)",
            (path, sentence_ordinal, page, content, topics, title_topics),
        )
        if cur.rowcount:
            db.execute(
                "INSERT INTO caution_search(rowid,path,page,name,doc_type,topic_keywords,title_keywords,content) VALUES(?,?,?,?,?,?,?,?)",
                (cur.lastrowid, path, page, name, doc_type, topics, title_topics, content),
            )


def backfill_caution_index() -> int:
    """Build the caution table once from already indexed chunks after schema upgrades."""
    count = 0
    with connect() as db:
        current = db.execute("SELECT value FROM app_meta WHERE key='caution_index_version'").fetchone()
        if current and current["value"] == CAUTION_INDEX_VERSION:
            return 0
        db.execute("DROP TABLE IF EXISTS caution_vocab")
        db.execute("DROP TABLE IF EXISTS caution_search")
        passage_columns = {row["name"] for row in db.execute("PRAGMA table_info(caution_passages)")}
        if "title_keywords" not in passage_columns:
            db.execute("ALTER TABLE caution_passages ADD COLUMN title_keywords TEXT NOT NULL DEFAULT ''")
        db.execute("""
            CREATE VIRTUAL TABLE caution_search USING fts5(
                path UNINDEXED, page UNINDEXED, name, doc_type, topic_keywords, title_keywords, content,
                tokenize = 'unicode61 remove_diacritics 2'
            )
        """)
        db.execute("CREATE VIRTUAL TABLE caution_vocab USING fts5vocab(caution_search, 'col')")
        db.execute("DELETE FROM caution_passages")
        docs = db.execute(
            "SELECT path,name,doc_type FROM documents WHERE parse_status='indexed' ORDER BY path"
        ).fetchall()
        for doc in docs:
            if not is_authority_source(doc["name"], doc["doc_type"]):
                continue
            chunks = db.execute(
                "SELECT page,content FROM chunks WHERE path=? ORDER BY ordinal", (doc["path"],)
            ).fetchall()
            passages = extract_caution_passages([(row["page"], row["content"]) for row in chunks], doc["name"])
            replace_caution_passages(db, doc["path"], doc["name"], doc["doc_type"], passages)
            count += len(passages)
        db.execute(
            "INSERT INTO app_meta(key,value) VALUES('caution_index_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (CAUTION_INDEX_VERSION,)
        )
    return count


def save_document(path: Path, stat: os.stat_result, data: bytes, chunks: list[tuple[int | None, str]],
                  pages: list[tuple[int | None, str]]) -> None:
    full = str(path.resolve())
    name = path.name
    doc_type = classify_doc(f"{path.parent.name} {name}", " ".join(text for _, text in chunks[:4]))
    digest = hashlib.sha256(data).hexdigest()
    indexed_at = utc_now()
    with connect() as db:
        old = db.execute("SELECT sha256 FROM documents WHERE path=?", (full,)).fetchone()
        if old and old["sha256"] == digest:
            db.execute("UPDATE documents SET last_seen=?, modified_ns=?, size=? WHERE path=?", (indexed_at, stat.st_mtime_ns, stat.st_size, full))
            return
        db.execute("""
            INSERT INTO documents(path,name,extension,doc_type,modified_ns,size,sha256,indexed_at,last_seen,parse_status,error)
            VALUES(?,?,?,?,?,?,?,?,?,'indexed',NULL)
            ON CONFLICT(path) DO UPDATE SET name=excluded.name,extension=excluded.extension,doc_type=excluded.doc_type,
                modified_ns=excluded.modified_ns,size=excluded.size,sha256=excluded.sha256,indexed_at=excluded.indexed_at,
                last_seen=excluded.last_seen,parse_status='indexed',error=NULL
        """, (full, name, path.suffix.lower(), doc_type, stat.st_mtime_ns, stat.st_size, digest, indexed_at, indexed_at))
        replace_caution_passages(db, full, name, doc_type, extract_caution_passages(pages, name))
        db.execute("DELETE FROM chunk_search WHERE rowid IN (SELECT id FROM chunks WHERE path=?)", (full,))
        db.execute("DELETE FROM chunks WHERE path=?", (full,))
        for ordinal, (page, content) in enumerate(chunks):
            cur = db.execute("INSERT INTO chunks(path,ordinal,page,content) VALUES(?,?,?,?)", (full, ordinal, page, content))
            db.execute("INSERT INTO chunk_search(rowid,path,name,folder,doc_type,content) VALUES(?,?,?,?,?,?)",
                       (cur.lastrowid, full, name, path.parent.name, doc_type, content))


def mark_error(path: Path, message: str) -> None:
    full = str(path.resolve())
    try:
        with connect() as db:
            exists = db.execute("SELECT 1 FROM documents WHERE path=?", (full,)).fetchone()
            if exists:
                db.execute("UPDATE documents SET parse_status='error',error=?,last_seen=? WHERE path=?", (message[:500], utc_now(), full))
            else:
                stat = path.stat()
                db.execute("INSERT OR REPLACE INTO documents(path,name,extension,doc_type,modified_ns,size,sha256,indexed_at,last_seen,parse_status,error) VALUES(?,?,?,?,?,?,?,?,?,'error',?)",
                           (full, path.name, path.suffix.lower(), "Unparsed Document", stat.st_mtime_ns, stat.st_size, "", utc_now(), utc_now(), message[:500]))
    except OSError:
        pass


def walk_documents(root: Path) -> Iterable[Path]:
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d.upper() not in IGNORE_DIRS and not d.startswith(".")]
        for filename in files:
            if filename.startswith("~$") or filename.startswith(".") or filename.casefold() == SOURCE_REGISTER_NAME:
                continue
            candidate = Path(current) / filename
            if candidate.suffix.lower() in SUPPORTED:
                yield candidate


def remove_source_register_documents() -> int:
    """Remove previously indexed source registers when the owner starts a reindex."""
    with connect() as db:
        rows = db.execute("SELECT path FROM documents WHERE name=? COLLATE NOCASE", (SOURCE_REGISTER_NAME,)).fetchall()
        for row in rows:
            db.execute("DELETE FROM chunk_search WHERE path=?", (row["path"],))
            db.execute("DELETE FROM documents WHERE path=?", (row["path"],))
    return len(rows)


def index_folder(root_text: str) -> None:
    root = Path(root_text).expanduser()
    with STATE_LOCK:
        INDEX_STATE.update(running=True, phase="scanning", current="", processed=0, total=0, indexed=0,
                           unchanged=0, skipped=0, errors=0, started_at=utc_now(), finished_at=None,
                           message="Walking the selected folder…")
    try:
        removed_registers = remove_source_register_documents()
        paths = list(walk_documents(root))
        with STATE_LOCK:
            INDEX_STATE["total"] = len(paths)
            INDEX_STATE["phase"] = "indexing"
            INDEX_STATE["message"] = f"Found {len(paths):,} supported files. Indexing and checking changes…"
            if removed_registers:
                INDEX_STATE["message"] += f" Removed {removed_registers:,} source registers from the index."
        for candidate in paths:
            try:
                stat = candidate.stat()
                full = str(candidate.resolve())
                with connect() as db:
                    row = db.execute("SELECT modified_ns,size,parse_status FROM documents WHERE path=?", (full,)).fetchone()
                if row and row["modified_ns"] == stat.st_mtime_ns and row["size"] == stat.st_size and row["parse_status"] == "indexed":
                    with connect() as db:
                        db.execute("UPDATE documents SET last_seen=? WHERE path=?", (utc_now(), full))
                    with STATE_LOCK:
                        INDEX_STATE["unchanged"] += 1
                elif stat.st_size > MAX_FILE_BYTES:
                    with STATE_LOCK:
                        INDEX_STATE["skipped"] += 1
                else:
                    data = candidate.read_bytes()
                    pages = extract_pages(candidate, data)
                    chunks = chunk_pages(pages)
                    if not chunks:
                        with STATE_LOCK:
                            INDEX_STATE["skipped"] += 1
                    else:
                        save_document(candidate, stat, data, chunks, pages)
                        with STATE_LOCK:
                            INDEX_STATE["indexed"] += 1
            except Exception as exc:
                mark_error(candidate, str(exc))
                with STATE_LOCK:
                    INDEX_STATE["errors"] += 1
            finally:
                with STATE_LOCK:
                    INDEX_STATE["processed"] += 1
                    INDEX_STATE["current"] = candidate.name
        with STATE_LOCK:
            INDEX_STATE["phase"] = "done"
            INDEX_STATE["message"] = "Indexing complete. Search your manuals and regulatory references."
    except Exception as exc:
        with STATE_LOCK:
            INDEX_STATE["phase"] = "error"
            INDEX_STATE["message"] = str(exc)
    finally:
        with STATE_LOCK:
            INDEX_STATE["running"] = False
            INDEX_STATE["finished_at"] = utc_now()
            INDEX_STATE["current"] = ""
        if INDEX_STATE.get("phase") == "done":
            start_embedding_refresh()


STOP_WORDS = {"the", "a", "an", "of", "to", "in", "on", "and", "or", "for", "with", "from", "by", "is", "are", "be", "what", "where", "how", "which", "when", "does", "do", "can", "please", "find", "show", "me"}
EXPANSIONS = {
    "cmm": ["component", "maintenance", "manual"],
    "amm": ["aircraft", "maintenance", "manual"],
    "srm": ["structural", "repair", "manual"],
    "ipc": ["illustrated", "parts", "catalog"],
    "ad": ["airworthiness", "directive"],
    "faa": ["federal", "aviation", "administration"],
    "easa": ["european", "aviation", "safety", "agency"],
    "caac": ["civil", "aviation", "administration", "ccar"],
    "ccar": ["caac", "145"],
    "aesa": ["seguridad", "aerea", "145"],
    "tcca": ["transport", "canada", "573", "amo"],
    "f93": ["sherwin", "williams", "coating"],
    "145": ["repair", "station"],
    "tsm": ["troubleshooting", "manual"],
    "galvanic": ["anodic", "cathodic", "electrolyte", "potential", "current", "accelerated"],
    "risks": ["severe", "accelerated", "pitting"],
}


def query_terms(raw: str) -> list[str]:
    return list(dict.fromkeys(t.lower() for t in TOKEN_RE.findall(raw)))


def make_fts_terms(raw: str) -> list[str]:
    tokens = [t.lower() for t in TOKEN_RE.findall(raw)]
    terms = [t for t in tokens if t not in STOP_WORDS]
    expanded = list(terms)
    for term in terms:
        expanded.extend(EXPANSIONS.get(term, []))
    # SQLite FTS treats punctuation as operators; only use our tokenized terms.
    unique = list(dict.fromkeys(t for t in expanded if len(t) > 1))[:28]
    if not unique:
        unique = list(dict.fromkeys(tokens))[:20]
    return unique


def make_fts_query(raw: str) -> str:
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in make_fts_terms(raw))


def _set_embedding_state(**values: Any) -> None:
    with EMBEDDING_STATE_LOCK:
        EMBEDDING_STATE.update(values)


def _cancel_embedding_retry(*, reset_attempt: bool = False) -> None:
    global EMBEDDING_RETRY_TIMER, EMBEDDING_RETRY_TOKEN, EMBEDDING_RETRY_ATTEMPT
    with EMBEDDING_RETRY_LOCK:
        EMBEDDING_RETRY_TOKEN += 1
        timer = EMBEDDING_RETRY_TIMER
        EMBEDDING_RETRY_TIMER = None
        if reset_attempt:
            EMBEDDING_RETRY_ATTEMPT = 0
    if timer is not None:
        timer.cancel()


def _run_embedding_retry(token: int) -> None:
    global EMBEDDING_RETRY_TIMER
    with EMBEDDING_RETRY_LOCK:
        if token != EMBEDDING_RETRY_TOKEN:
            return
        EMBEDDING_RETRY_TIMER = None
    start_embedding_refresh()


def _schedule_embedding_retry() -> None:
    global EMBEDDING_RETRY_TIMER, EMBEDDING_RETRY_TOKEN, EMBEDDING_RETRY_ATTEMPT
    with EMBEDDING_RETRY_LOCK:
        if EMBEDDING_RETRY_TIMER is not None and EMBEDDING_RETRY_TIMER.is_alive():
            return
        delay = min(EMBEDDING_RETRY_BASE_SECONDS * (2 ** min(EMBEDDING_RETRY_ATTEMPT, 6)),
                    EMBEDDING_RETRY_MAX_SECONDS)
        EMBEDDING_RETRY_ATTEMPT = min(EMBEDDING_RETRY_ATTEMPT + 1, 6)
        EMBEDDING_RETRY_TOKEN += 1
        timer = threading.Timer(delay, _run_embedding_retry, args=(EMBEDDING_RETRY_TOKEN,))
        timer.daemon = True
        EMBEDDING_RETRY_TIMER = timer
    timer.start()


def _normalize_embedding_rows(rows: list[list[float]]) -> np.ndarray:
    vectors = np.asarray(rows, dtype=np.float32)
    if vectors.ndim != 2 or not vectors.shape[0]:
        raise RuntimeError("Ollama returned an empty or invalid embedding matrix.")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    np.divide(vectors, np.maximum(norms, 1e-12), out=vectors)
    return vectors


def embed_texts(texts: list[str], *, timeout: int = 600) -> np.ndarray:
    url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    body = json.dumps({"model": EMBEDDING_MODEL, "input": texts, "truncate": True}).encode("utf-8")
    request = urllib.request.Request(
        url + "/api/embed", data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Embedding request failed: {exc}") from exc
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    vectors = result.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise RuntimeError("Ollama returned an unexpected number of embeddings.")
    return _normalize_embedding_rows(vectors)


def refresh_embedding_cache() -> None:
    """Fill the private vector cache, pruning deleted chunks and loading a NumPy matrix."""
    global EMBEDDING_IDS, EMBEDDING_MATRIX, EMBEDDING_REFRESH_REQUESTED
    if not EMBEDDINGS_ENABLED:
        return
    if not EMBEDDING_REFRESH_LOCK.acquire(blocking=False):
        with EMBEDDING_STATE_LOCK:
            EMBEDDING_REFRESH_REQUESTED = True
        return
    try:
        _set_embedding_state(state="warming", error="")
        with connect() as db:
            generation_row = db.execute("SELECT value FROM app_meta WHERE key='index_generation'").fetchone()
            generation = generation_row["value"] if generation_row else "unknown"
            rows = db.execute("""
                SELECT c.id,d.name,c.content FROM chunks c JOIN documents d ON d.path=c.path
                WHERE d.parse_status='indexed' AND d.name <> ? COLLATE NOCASE ORDER BY c.id
            """, (SOURCE_REGISTER_NAME,)).fetchall()
        if not rows:
            raise RuntimeError("The local index has no chunks to embed.")

        EMBEDDINGS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cache = sqlite3.connect(EMBEDDINGS_CACHE_PATH)
        try:
            cache.execute("CREATE TABLE IF NOT EXISTS emb (id INTEGER PRIMARY KEY, v BLOB NOT NULL)")
            cache.execute("CREATE TABLE IF NOT EXISTS embedding_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            cache.execute("CREATE TEMP TABLE active_ids (id INTEGER PRIMARY KEY)")
            cache.executemany("INSERT INTO active_ids(id) VALUES (?)", ((row["id"],) for row in rows))
            meta = dict(cache.execute("SELECT key,value FROM embedding_meta"))
            if meta.get("index_generation") != generation or meta.get("model") != EMBEDDING_MODEL:
                cache.execute("DELETE FROM emb")
            cache.execute("DELETE FROM emb WHERE id NOT IN (SELECT id FROM active_ids)")
            cache.execute("""
                INSERT INTO embedding_meta(key,value) VALUES('index_generation',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (generation,))
            cache.execute("""
                INSERT INTO embedding_meta(key,value) VALUES('model',?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (EMBEDDING_MODEL,))
            cache.commit()

            cached_ids = {row[0] for row in cache.execute("SELECT id FROM emb")}
            pending = [row for row in rows if row["id"] not in cached_ids]
            _set_embedding_state(state="warming", embedded=len(cached_ids), total=len(rows), error="")
            for offset in range(0, len(pending), EMBEDDING_BATCH_SIZE):
                batch = pending[offset:offset + EMBEDDING_BATCH_SIZE]
                vectors = embed_texts([
                    f"search_document: {row['name']}\n{row['content']}" for row in batch
                ])
                cache.executemany(
                    "INSERT OR REPLACE INTO emb(id,v) VALUES (?,?)",
                    [(row["id"], vector.tobytes()) for row, vector in zip(batch, vectors)],
                )
                cache.commit()
                _set_embedding_state(embedded=len(cached_ids) + min(offset + len(batch), len(pending)), total=len(rows))

            entries = cache.execute("SELECT emb.id,emb.v FROM emb JOIN active_ids ON active_ids.id=emb.id ORDER BY emb.id").fetchall()
        finally:
            cache.close()

        if not entries:
            raise RuntimeError("The vector cache is empty after embedding.")
        vector_ids = tuple(row[0] for row in entries)
        vectors = [np.frombuffer(row[1], dtype=np.float32) for row in entries]
        dims = {vector.shape[0] for vector in vectors}
        if len(dims) != 1:
            raise RuntimeError("The vector cache contains inconsistent embedding dimensions.")
        matrix = np.vstack(vectors)
        with EMBEDDING_STATE_LOCK:
            EMBEDDING_IDS = vector_ids
            EMBEDDING_MATRIX = matrix
            EMBEDDING_STATE.update(state="ready", embedded=len(vector_ids), total=len(rows), error="")
        _cancel_embedding_retry(reset_attempt=True)
        succeeded = True
    except Exception as exc:
        _set_embedding_state(state="retrying", error=str(exc)[:240])
        succeeded = False
    finally:
        EMBEDDING_REFRESH_LOCK.release()
        with EMBEDDING_STATE_LOCK:
            retry = EMBEDDING_REFRESH_REQUESTED
            EMBEDDING_REFRESH_REQUESTED = False
        if retry:
            start_embedding_refresh()
        elif not succeeded:
            _schedule_embedding_retry()


def start_embedding_refresh() -> None:
    if EMBEDDINGS_ENABLED:
        _cancel_embedding_retry()
        threading.Thread(target=refresh_embedding_cache, daemon=True, name="hangar-embedding-refresh").start()


def caution_topic_terms(raw: str) -> list[str]:
    blocked = STOP_WORDS | CAUTION_TOPIC_STOP_WORDS
    normalized = raw.replace("_", " ")
    terms = []
    for token in TOKEN_RE.findall(normalized):
        for value in re.findall(r"[a-z]+|\d+", token.lower()):
            if value in blocked or (value.isdigit() and len(value) < 4):
                continue
            if value.endswith("ies") and len(value) > 5:
                value = value[:-3] + "y"
            elif value in {"splice", "splicing"}:
                value = "splic"
            elif value.endswith("ing") and len(value) > 6:
                value = value[:-3]
                if len(value) > 2 and value[-1] == value[-2]:
                    value = value[:-1]
            elif value.endswith("ied") and len(value) > 5:
                value = value[:-3] + "y"
            elif value.endswith("ed") and len(value) > 5:
                value = value[:-2]
            elif value.endswith("s") and not value.endswith(("ss", "us", "is")) and len(value) > 4:
                value = value[:-1]
            if len(value) > 2 or (value.isdigit() and len(value) >= 4):
                terms.append(value)
    return list(dict.fromkeys(terms))[:24]


def extract_caution_passages(pages: Iterable[tuple[int | None, str]], name: str = "") -> list[tuple[int | None, int, str, str, str]]:
    output: list[tuple[int | None, int, str, str, str]] = []
    seen: set[tuple[int | None, str]] = set()
    sentence_ordinal = 0
    title_topics = caution_topic_terms(name)
    for page, raw in pages:
        normalized = re.sub(r"\s+", " ", raw).strip()
        for sentence in re.split(r"(?<=[.!?])\s+", normalized):
            sentence = sentence.strip()
            if (not sentence or not CAUTION_CUE_RE.search(sentence)
                    or (not CAUTION_STRONG_CUE_RE.search(sentence)
                        and not CAUTION_UNLESS_TOPIC_RE.search(sentence))
                    or CAUTION_FORM_INSTRUCTION_RE.search(sentence)):
                continue
            if len(sentence) > 1000:
                sentence = sentence[:1000].rsplit(" ", 1)[0].rstrip() + "…"
            if len(sentence) < 30:
                continue
            key = (page, sentence.casefold())
            if key in seen:
                continue
            seen.add(key)
            topics = caution_topic_terms(sentence)
            if not topics:
                continue
            output.append((page, sentence_ordinal, sentence, " ".join(topics), " ".join(title_topics)))
            sentence_ordinal += 1
    return output


def excerpt_for_query(text: str, primary_terms: list[str], fallback_terms: list[str], max_chars: int = 600) -> str:
    terms = set(primary_terms)
    hit = next((match.start() for match in TOKEN_RE.finditer(text) if match.group(0).lower() in terms), None)
    if hit is None:
        terms = set(fallback_terms)
        hit = next((match.start() for match in TOKEN_RE.finditer(text) if match.group(0).lower() in terms), None)

    start = max(0, (hit or 0) - max_chars // 2) if hit is not None else 0
    end = min(len(text), start + max_chars)
    # Move edges to word boundaries. Chunks are small, so these scans stay bounded.
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and end > 0 and not text[end].isspace() and not text[end - 1].isspace():
        end += 1
    body = text[start:end].strip()
    return ("…" if start > 0 else "") + body + ("…" if end < len(text) else "")


def procedure_chunk_score(query: str, content: str, doc_type: str = "") -> int:
    if not PROCEDURE_QUERY_RE.search(query):
        return 0
    heading = any(len(line) <= 120 and PROCEDURE_HEADING_RE.search(line)
                  for line in content.splitlines())
    numbered_steps = len(re.findall(r"(?m)^\s*(?:\d+(?:\.\d+){0,4}[.)]?|\(\d+\))\s+", content))
    actions = len(PROCEDURE_ACTION_RE.findall(content))
    if heading and numbered_steps >= 2 and actions >= 2:
        score = 3
    elif heading:
        score = 2
    elif numbered_steps >= 2 and actions >= 3:
        score = 1
    else:
        score = 0
    if score and doc_type == "FAA Corrosion Control Guidance":
        score += 1
    return score


def is_bibliography_chunk(content: str) -> bool:
    opening = "\n".join(content.splitlines()[:14])[:900]
    return bool(
        BIBLIOGRAPHY_HEADING_RE.search(opening)
        or (BIBLIOGRAPHIC_ENTRY_RE.search(opening) and BIBLIOGRAPHIC_MARKER_RE.search(opening))
    )


def retrieval_content_priority(query: str, item: dict[str, Any]) -> tuple[int, int]:
    bibliography_query = bool(BIBLIOGRAPHY_QUERY_RE.search(query))
    bibliography_penalty = int(not bibliography_query and is_bibliography_chunk(item.get("content", "")))
    procedure_priority = 0 if bibliography_query else -procedure_chunk_score(
        query, item.get("content", ""), item.get("doc_type", "")
    )
    return bibliography_penalty, procedure_priority


def retrieve_fts(query: str, limit: int = 30, *, authority_only: bool = False) -> list[dict[str, Any]]:
    terms = make_fts_terms(query)
    if not terms:
        return []
    fts = " OR ".join('"' + token.replace('"', '""') + '"' for token in terms)
    where = "chunk_search MATCH ? AND d.name <> ? COLLATE NOCASE"
    params: list[Any] = [fts, SOURCE_REGISTER_NAME]
    if authority_only:
        authority_terms = ["d.doc_type LIKE ?" for _ in REGULATORY_DOC_TYPES]
        params.extend(prefix + "%" for prefix in REGULATORY_DOC_TYPES)
        where += " AND (" + " OR ".join(authority_terms) + ")"
        if NON_AUTHORITY_DOC_TYPES:
            where += " AND d.doc_type NOT IN (" + ", ".join("?" for _ in NON_AUTHORITY_DOC_TYPES) + ")"
            params.extend(NON_AUTHORITY_DOC_TYPES)
    search_limit = max(limit, 120) if not BIBLIOGRAPHY_QUERY_RE.search(query) else limit
    params.append(search_limit)
    with connect() as db:
        try:
            rows = db.execute(f"""
                SELECT c.id,c.path,c.ordinal,c.page,c.content,d.name,d.doc_type,d.extension,d.modified_ns,d.size,d.parse_status,
                       bm25(chunk_search, 0.0, 2.0, 1.3, 1.5, 1.0) AS rank
                FROM chunk_search JOIN chunks c ON c.id=chunk_search.rowid
                JOIN documents d ON d.path=c.path
                WHERE {where}
                ORDER BY rank ASC LIMIT ?
            """, params).fetchall()
        except sqlite3.OperationalError as exc:
            raise RuntimeError(f"Full-text retrieval failed: {exc}") from exc
    output: list[dict[str, Any]] = []
    primary_terms = query_terms(query)
    for row in rows:
        item = dict(row)
        item["modified"] = datetime.fromtimestamp(item.pop("modified_ns") / 1e9).strftime("%Y-%m-%d")
        item["excerpt"] = excerpt_for_query(item["content"], primary_terms, terms)
        output.append(item)
    output.sort(key=lambda item: (*retrieval_content_priority(query, item), item["rank"]))
    return output[:limit]


def retrieve(query: str, limit: int = 30, *, authority_only: bool = False) -> list[dict[str, Any]]:
    with EMBEDDING_STATE_LOCK:
        matrix, vector_ids = EMBEDDING_MATRIX, EMBEDDING_IDS
        embedding_state = EMBEDDING_STATE.get("state")
    if (not EMBEDDINGS_ENABLED or authority_only or matrix is None or not vector_ids
            or embedding_state not in {"ready", "degraded"}):
        return retrieve_fts(query, limit, authority_only=authority_only)

    fts_rows = retrieve_fts(query, EMBEDDING_POOL)
    try:
        query_vector = embed_texts([f"search_query: {query}"], timeout=90)[0]
        if matrix is None or matrix.shape[1] != query_vector.shape[0]:
            raise RuntimeError("Query embedding dimension does not match the local vector cache.")
        vector_scores = matrix @ query_vector
        vector_indices = np.argsort(-vector_scores)[:EMBEDDING_POOL]
        ranked_vector_ids = [vector_ids[int(index)] for index in vector_indices]
    except Exception as exc:
        _set_embedding_state(state="degraded", error=str(exc)[:240])
        return fts_rows[:limit]

    candidates: dict[int, dict[str, Any]] = {}
    scores: dict[int, float] = {}
    for rank, row in enumerate(fts_rows, 1):
        chunk_id = int(row["id"])
        candidates[chunk_id] = row
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_CONSTANT + rank)
    for rank, chunk_id in enumerate(ranked_vector_ids, 1):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + VECTOR_RRF_WEIGHT / (RRF_CONSTANT + rank)

    missing_ids = [chunk_id for chunk_id in ranked_vector_ids if chunk_id not in candidates]
    if missing_ids:
        placeholders = ",".join("?" for _ in missing_ids)
        with connect() as db:
            rows = db.execute(f"""
                SELECT c.id,c.path,c.ordinal,c.page,c.content,d.name,d.doc_type,d.extension,d.modified_ns,d.size,d.parse_status
                FROM chunks c JOIN documents d ON d.path=c.path
                WHERE c.id IN ({placeholders}) AND d.parse_status='indexed' AND d.name <> ? COLLATE NOCASE
            """, [*missing_ids, SOURCE_REGISTER_NAME]).fetchall()
        primary_terms = query_terms(query)
        fallback_terms = make_fts_terms(query)
        for source in rows:
            row = dict(source)
            row["modified"] = datetime.fromtimestamp(row.pop("modified_ns") / 1e9).strftime("%Y-%m-%d")
            row["excerpt"] = excerpt_for_query(row["content"], primary_terms, fallback_terms)
            candidates[int(row["id"])] = row

    ranked_ids = sorted(
        scores,
        key=lambda chunk_id: (
            *retrieval_content_priority(query, candidates.get(chunk_id, {})),
            -scores[chunk_id],
        ),
    )
    output = []
    for chunk_id in ranked_ids:
        row = candidates.get(chunk_id)
        if row is None:
            continue
        item = dict(row)
        item.pop("id", None)
        output.append(item)
        if len(output) >= limit:
            break
    with EMBEDDING_STATE_LOCK:
        if EMBEDDING_STATE.get("state") == "degraded":
            EMBEDDING_STATE.update(state="ready", error="")
    return output


def retrieve_cautions(question: str, limit: int = 36) -> list[dict[str, Any]]:
    topics = caution_topic_terms(question)
    if not topics:
        return []
    fts_terms = " OR ".join('"' + token.replace('"', '""') + '"' for token in topics)
    fts = f"topic_keywords:({fts_terms}) OR title_keywords:({fts_terms})"
    aviation_question = bool(re.search(
        r"\b(?:FAA|EASA|AESA|CAAC|TCCA|UK\s+CAA)\b|\b(?:14\s+)?CFR\b|\bAC\s+\d",
        question, re.IGNORECASE,
    ))
    with connect() as db:
        try:
            total = db.execute("SELECT count(*) FROM caution_passages").fetchone()[0]
            placeholders = ",".join("?" for _ in topics)
            frequencies = db.execute(
                f"SELECT term,col,doc FROM caution_vocab WHERE col IN ('topic_keywords','title_keywords') AND term IN ({placeholders})",
                topics,
            ).fetchall()
            document_frequency = {(row["col"], row["term"]): row["doc"] for row in frequencies}
            inverse_frequency = {
                (column, term): math.log((total + 1) / (document_frequency.get((column, term), 0) + 1))
                for column in ("topic_keywords", "title_keywords") for term in topics
            }
            sql = """
                SELECT p.path,p.sentence_ordinal AS ordinal,p.page,p.content,p.topic_keywords,p.title_keywords,
                       d.name,d.doc_type,d.extension,
                       d.modified_ns,d.size,d.parse_status,
                       bm25(caution_search,0.0,0.0,0.5,0.0,2.0,0.5,1.0) AS rank
                FROM caution_search
                JOIN caution_passages p ON p.id=caution_search.rowid
                JOIN documents d ON d.path=p.path
                WHERE caution_search MATCH ? AND d.parse_status='indexed'
            """
            params: list[Any] = [fts]
            if aviation_question:
                aviation_types = (*REGULATORY_DOC_TYPES, "UK CAA ", "UK Regulation ")
                type_filter = " OR ".join("d.doc_type LIKE ?" for _ in aviation_types)
                name_filter = " OR ".join("upper(d.name) GLOB ?" for _ in AVIATION_AUTHORITY_NAME_PREFIXES)
                sql += f" AND (({type_filter}) OR ({name_filter}))"
                params.extend(prefix + "%" for prefix in aviation_types)
                params.extend(prefix + "*" for prefix in AVIATION_AUTHORITY_NAME_PREFIXES)
            if not SHOP_SAFETY_QUERY_RE.search(question):
                sql += " AND upper(d.name) NOT GLOB 'OSHA_*' AND d.doc_type NOT LIKE 'OSHA %'"
            sql += " ORDER BY rank ASC LIMIT ?"
            params.append(max(limit, 300))
            rows = db.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            raise RuntimeError(f"Caution retrieval failed: {exc}") from exc
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        sentence_overlap = set(item["topic_keywords"].split()) & set(topics)
        title_overlap = set(item["title_keywords"].split()) & set(topics)
        exact_topics = set(topics) & CAUTION_EXACT_TOPIC_TERMS
        if exact_topics and not (sentence_overlap & exact_topics):
            continue
        if {"splic", "wire"} <= set(topics) and not {"splic", "wire"} <= sentence_overlap:
            continue
        if "shield" in topics and sentence_overlap & {"shield", "exciter"}:
            shield_context = CAUTION_TOPIC_PAIR_REQUIREMENTS["shield"] & set(topics)
            if shield_context and not (sentence_overlap & shield_context):
                continue
        rare_single = (
            len(sentence_overlap) == 1
            and inverse_frequency[("topic_keywords", next(iter(sentence_overlap)))] >= 2.5
        )
        if not sentence_overlap or (len(sentence_overlap) + len(title_overlap) < 2 and not rare_single):
            continue
        if not title_overlap and len(sentence_overlap) < 3 and not rare_single:
            continue
        item["topic_overlap"] = len(sentence_overlap)
        item["title_overlap"] = len(title_overlap)
        item["topic_score"] = (
            sum(inverse_frequency[("topic_keywords", token)] for token in sentence_overlap)
            + 0.25 * sum(inverse_frequency[("title_keywords", token)] for token in title_overlap)
        )
        item["modified"] = datetime.fromtimestamp(item.pop("modified_ns") / 1e9).strftime("%Y-%m-%d")
        item["excerpt"] = item["content"]
        output.append(item)
    output.sort(key=lambda item: (-item["topic_score"], -item["topic_overlap"], item["rank"]))
    return output[:limit]


backfill_document_types()
backfill_caution_index()


def source_list(matches: list[dict[str, Any]], max_sources: int = 8, max_per_doc: int = 2) -> list[dict[str, Any]]:
    seen: set[str] = set()
    seen_families: set[str] = set()
    sources = []
    per_doc: dict[str, int] = {}
    for row in matches:
        path = row["path"]
        family = re.sub(r"[^a-z0-9]+", "", Path(row["name"]).stem.casefold())
        family = re.sub(r"^(?:faa|easa|aesa|caac|tcca|ukcaa|ukregulation)", "", family)
        if family in seen_families:
            continue
        if per_doc.get(path, 0) >= max_per_doc:
            continue
        seen_families.add(family)
        if path not in seen:
            seen.add(path)
            per_doc[path] = 0
            sources.append({"id": f"S{len(sources) + 1}", "path": path, "name": row["name"], "extension": row["extension"], "doc_type": row["doc_type"], "page": row["page"], "excerpt": row["excerpt"]})
        else:
            sources.append({"id": f"S{len(sources) + 1}", "path": path, "name": row["name"], "extension": row["extension"], "doc_type": row["doc_type"], "page": row["page"], "excerpt": row["excerpt"]})
        per_doc[path] += 1
        if len(sources) >= max_sources:
            break
    return sources


def collect_answer_sources(question: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    primary_sources = source_list(retrieve(question, 24), 5)
    if not primary_sources:
        return [], [], []
    caution_matches = retrieve_cautions(question, 20)
    caution_candidates = source_list(caution_matches, 4, max_per_doc=1)

    sources = []
    by_passage: dict[tuple[str, int | None, str], dict[str, Any]] = {}
    for source in primary_sources:
        item = {**source, "primary_source": True, "caution_source": False}
        sources.append(item)
        by_passage[(item["path"], item["page"], item["excerpt"])] = item

    caution_sources = []
    for source in caution_candidates:
        key = (source["path"], source["page"], source["excerpt"])
        existing = by_passage.get(key)
        if existing is not None:
            existing["caution_source"] = True
            caution_sources.append(existing)
            continue
        if len(sources) >= 12:
            break
        item = {**source, "id": f"S{len(sources) + 1}", "primary_source": False, "caution_source": True}
        sources.append(item)
        by_passage[key] = item
        caution_sources.append(item)
    return primary_sources, caution_sources, sources


def llm_config(mode: str = "thorough") -> dict[str, str]:
    provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
    if provider == "openai":
        return {"provider": provider, "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
                "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"), "api_key": os.environ.get("OPENAI_API_KEY", ""),
                "mode": mode}
    quick_model = os.environ.get("OLLAMA_QUICK_MODEL", "qwen3.5:4b").strip() or "qwen3.5:4b"
    thorough_model = os.environ.get("OLLAMA_THOROUGH_MODEL", "qwen3.5:35b-a3b").strip() or "qwen3.5:35b-a3b"
    return {"provider": "ollama", "base_url": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/"),
            "model": thorough_model if mode == "thorough" else quick_model, "api_key": "", "mode": mode,
            "think": os.environ.get("OLLAMA_THINK", "false").strip().lower(),
            "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", "30m").strip() or "30m",
            "num_gpu": os.environ.get("OLLAMA_NUM_GPU", "").strip(),
            "num_thread": os.environ.get("OLLAMA_NUM_THREAD", "").strip()}


def ollama_options(cfg: dict[str, str]) -> dict[str, Any]:
    try:
        num_predict = int(os.environ.get("OLLAMA_NUM_PREDICT", "640"))
    except ValueError as exc:
        raise RuntimeError("OLLAMA_NUM_PREDICT must be an integer.") from exc
    if num_predict < 1:
        raise RuntimeError("OLLAMA_NUM_PREDICT must be at least 1.")
    options: dict[str, Any] = {"temperature": 0.1, "num_ctx": 8192, "num_predict": num_predict}
    for key in ("num_gpu", "num_thread"):
        if cfg.get(key):
            try:
                options[key] = int(cfg[key])
            except ValueError as exc:
                raise RuntimeError(f"OLLAMA_{key.upper()} must be an integer.") from exc
    return options


def trim_truncated_answer(answer: str) -> str:
    """Keep only complete answer text when the model hit its output-token limit."""
    text = answer.rstrip()
    if not text:
        return text
    if re.search(r"\[S\d+\][.!?]?$", text):
        if not text.endswith((".", "!", "?")):
            text += "."
        return text
    if text.endswith(("None.", "None", ".", "!", "?")):
        return text

    # A citation is an atomic unit: if a final clause has started but not finished,
    # close the answer at the last complete ID instead of exposing a partial ID.
    citations = list(re.finditer(r"\[S\d+\]", text))
    if citations:
        text = text[:citations[-1].end()].rstrip()
        if not text.endswith((".", "!", "?")):
            text += "."
        return text

    boundaries = list(re.finditer(r"(?<=[.!?])\s+|\n+", text))
    return text[:boundaries[-1].start()].rstrip() if boundaries else ""


def split_answer_sentences(answer: str) -> list[str]:
    """Split answer text without treating common abbreviations as sentence ends."""
    marker = "\ue000"
    list_marker = "\ue001"
    protected = ANSWER_ABBREVIATION.sub(lambda match: match.group(0).replace(".", marker), answer)
    protected = re.sub(r"(?m)^([ \t]*\d{1,3})\.(?=[ \t\r\n])",
                       lambda match: match.group(1) + list_marker, protected)
    protected = re.sub(
        r"([.!?])([ \t]+)((?:\[(?:S\d+)(?:[ \t]*[,;][ \t]*S\d+)*\][ \t]*)+)(?=\S)",
        lambda match: match.group(1) + match.group(2) + match.group(3) + "\n", protected,
    )
    output: list[str] = []
    for raw_sentence in ANSWER_SENTENCE_SPLIT.split(protected):
        sentence = raw_sentence.replace(marker, ".").replace(list_marker, ".").strip()
        if not sentence or re.fullmatch(r"\d{1,3}\.", sentence):
            continue
        if re.fullmatch(r"(?:\[S\d+(?:\s*[,;]\s*S\d+)*\]\s*)+", sentence) and output:
            output[-1] = output[-1].rstrip() + " " + sentence.strip()
        else:
            output.append(sentence)
    return output


def answer_citation_ids(sentence: str) -> list[str]:
    return [source_id.strip() for group in SOURCE_CITATION.findall(sentence)
            for source_id in re.split(r"\s*[,;]\s*", group)]


def llm_state(mode: str = "thorough") -> dict[str, Any]:
    cfg = llm_config(mode)
    configured = bool(cfg["model"] and (cfg["provider"] == "ollama" or cfg["api_key"]))
    return {"provider": cfg["provider"], "model": cfg["model"], "mode": mode, "configured": configured,
            "privacy": "Local model" if cfg["provider"] == "ollama" else "Configured API endpoint"}


def answer_messages(question: str, primary_sources: list[dict[str, Any]], caution_sources: list[dict[str, Any]]) -> list[dict[str, str]]:
    caution_ids = {s["id"] for s in caution_sources}
    primary_context = [s for s in primary_sources if s["id"] not in caution_ids]
    context = "\n\n".join(
        f"[{s['id']}] {s['name']} | {s['doc_type']} | location {s['page'] or 'n/a'}\n"
        f"{trim_model_excerpt(s['excerpt'], 420)}" for s in primary_context
    )
    caution_context = "\n\n".join(f"[{s['id']}] {s['name']} | {s['doc_type']} | location {s['page'] or 'n/a'}\n{s['excerpt']}" for s in caution_sources)
    procedure_sources = [source for source in primary_context
                         if procedure_chunk_score(question, source.get("excerpt", ""), source.get("doc_type", ""))]
    procedure_guidance = ""
    if PROCEDURE_QUERY_RE.search(question):
        procedure_guidance = (
            " For a how-to question, check the supplied excerpts for an actual method or sequence. "
            "If one provides a method, lead Direct answer with that cited method; do not begin with a missing-procedure disclaimer. "
            "Use an explicit no-evidence statement only when none of the supplied passages gives a method. "
            "A method description alone does not make it approved task-specific repair data."
        )
    if ALUMINUM_CORROSION_REMOVAL_RE.search(question) and procedure_sources:
        method_ids = ", ".join(f"[{source['id']}]" for source in procedure_sources[:2])
        alkali_sources = [source for source in caution_sources
                          if re.search(r"\balkalis?\b", source.get("excerpt", ""), re.IGNORECASE)
                          and re.search(r"\binhibit", source.get("excerpt", ""), re.IGNORECASE)]
        caution_ids = ", ".join(f"[{source['id']}]" for source in alkali_sources[:2])
        procedure_guidance += (
            f" For this aluminum-corrosion removal question, open Direct answer with the supplied removal method and cite {method_ids}; "
            "name its mechanical tools and the requirement to retain structurally sound aluminum. Include a rotary-file limitation only "
            "if the supplied excerpt states it. Keep Direct answer focused on the method and stated tool limitations, without a generic background disclaimer."
        )
        if caution_ids:
            procedure_guidance += (
                f" In Watch out, state the retrieved alkali/inhibitor caution and cite {caution_ids}; do not write 'None.' after that caution."
            )
    system = ("You are an aerospace maintenance document research assistant. Answer only from the supplied excerpts. "
              "Cite every factual statement with source IDs like [S1]. If the excerpts do not establish something, say what is missing. "
              "Call out apparent conflicts or differences in applicability rather than reconciling them by guessing. "
              "Separate authority material (FAA, EASA, AESA, CAAC, or TCCA) from manuals, product data, and supplier/commercial context. "
              "Name the jurisdiction when relevant, and do not imply that one authority's approval or rule applies under another authority. "
              "Treat FAA training handbooks and NASA technical reports as educational or research background, not approved task-specific repair instructions. "
              "Describe FAA advisory circulars as advisory guidance; do not label them educational background. "
              "Treat 'exciter' as ambiguous: distinguish a turbine-engine ignition exciter from an electrical generator or alternator exciter. If the question does not specify the system, identify which type each excerpt covers, cite those identifications, and ask which system is meant before giving repair requirements. Never transfer details between the two types. "
              "Do not issue an approval or substitute for current controlled data. "
              "Keep the answer direct and mention when a source is a manual, directive, regulation, or advisory document. "
              "Use exactly these sections: Direct answer, Watch out, Where to look. "
              "Every factual sentence must end with one or more exact source IDs such as [S1]; use only IDs shown in the excerpts. "
              "Never omit a citation from a factual sentence, and never invent an ID. "
              "The Watch out section may mention only a caution passage that matches the question topic, and it must cite that passage's ID. "
              "If no topic-matched caution passage was retrieved, write exactly 'None.' and no other text under Watch out. Do not infer a caution from memory. "
              "Keep the answer concise enough to finish all three sections. Where to look should name the most relevant supplied file and page, ending each entry with its complete source ID. "
              "Finish a sentence and any citation before stopping; never split a source ID. "
              "If the passages do not establish the answer, say so and cite the closest relevant source." + procedure_guidance)
    caution_block = caution_context or "No authority caution passages were retrieved."
    user = f"Question: {question}\n\nCautions to check (indexed authority caution sentences matched by topic):\n{caution_block}\n\nRetrieved document excerpts:\n{context}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def remove_false_procedure_disclaimer(question: str, answer: str,
                                      primary_sources: list[dict[str, Any]]) -> str:
    """Remove a leading no-evidence sentence when retrieved text supplies a method."""
    if not PROCEDURE_QUERY_RE.search(question) or not any(
        procedure_chunk_score(question, source.get("excerpt", ""), source.get("doc_type", ""))
        for source in primary_sources
    ):
        return answer

    heading = re.match(r"(?is)^\s*(?:#{1,3}\s*)?(?:\*\*)?direct answer(?:\*\*)?\s*:?\s*", answer)
    body_start = heading.end() if heading else 0
    body = answer[body_start:]
    stripped = body.lstrip()
    if not stripped:
        return answer
    first = split_answer_sentences(stripped)
    if not first:
        return answer
    sentence = first[0]
    without_citation = SOURCE_CITATION.sub("", sentence).strip()
    without_marker = re.sub(r"^(?:(?:[-*+]\s+)|(?:\d+[.)]\s+))", "", without_citation).strip()
    if not NO_EVIDENCE_DISCLAIMER_RE.match(without_marker):
        return answer
    if not stripped.startswith(sentence):
        return answer
    return answer[:body_start] + stripped[len(sentence):].lstrip()


def drop_none_after_cited_watch_out(answer: str, caution_sources: list[dict[str, Any]]) -> str:
    """Drop a contradictory trailing None. when Watch out already cites a caution."""
    caution_ids = {source["id"] for source in caution_sources}
    if not caution_ids:
        return answer
    headings = list(re.finditer(
        r"(?im)^[ \t]*(?:#{1,3}[ \t]*)?(?:\*\*)?(direct answer|watch out|where to look)(?:\*\*)?[ \t]*:?[ \t]*",
        answer,
    ))
    for index, heading in enumerate(headings):
        if heading.group(1).casefold() != "watch out":
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(answer)
        section = answer[heading.end():end]
        if not (set(answer_citation_ids(section)) & caution_ids):
            continue
        cleaned = re.sub(r"[ \t]*(?:\r?\n[ \t]*)*(?:[-*+]\s*)?None\.\s*$", "", section, flags=re.IGNORECASE)
        if cleaned != section:
            if end < len(answer):
                cleaned = cleaned.rstrip() + "\n\n"
            answer = answer[:heading.end()] + cleaned + answer[end:]
            break
    return answer


def trim_model_excerpt(text: str, max_chars: int) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= max_chars:
        return normalized
    clipped = normalized[:max_chars]
    boundary = clipped.rfind(" ")
    if boundary > max_chars // 2:
        clipped = clipped[:boundary]
    return clipped.rstrip() + "…"


def validate_answer_citations(answer: str, sources: list[dict[str, Any]]) -> dict[str, Any]:
    valid_ids = {source["id"] for source in sources}
    caution_ids = {source["id"] for source in sources if source.get("caution_source")}
    sentences = split_answer_sentences(answer)
    failures = []
    section = ""
    for index, sentence in enumerate(sentences):
        heading = re.match(r"^\s*(?:[-*+]\s+)?(?:#{1,3}\s*)?(?:\*\*)?(direct answer|watch out|where to look)(?:\*\*)?\s*:?\s*", sentence, re.IGNORECASE)
        if heading:
            section = heading.group(1).casefold()
            sentence = sentence[heading.end():].strip()
            if not sentence:
                continue
        if re.fullmatch(r"(?:[-*+]\s+)?(?:#{1,3}\s*)?(?:\*\*)?(?:direct answer|watch out|where to look):?(?:\*\*)?", sentence, re.IGNORECASE):
            continue
        normalized_sentence = re.sub(r"^(?:(?:[-*+]\s+)|(?:\d+[.)]\s+))+", "", sentence).strip()
        if normalized_sentence.casefold() in {"none.", "none"} or NO_EVIDENCE_DISCLAIMER_RE.match(normalized_sentence):
            continue
        cited_ids = answer_citation_ids(sentence)
        invalid_ids = sorted(set(cited_ids) - valid_ids)
        if section == "watch out" and not (set(cited_ids) & caution_ids):
            reason = "unknown_source_id" if invalid_ids else "missing_citation"
            failures.append({"index": index, "text": sentence, "reason": reason, "invalid_ids": invalid_ids})
        elif not cited_ids:
            failures.append({"index": index, "text": sentence, "reason": "missing_citation", "invalid_ids": []})
        elif invalid_ids:
            failures.append({"index": index, "text": sentence, "reason": "unknown_source_id", "invalid_ids": invalid_ids})
    return {"valid": not failures, "checked_sentences": len(sentences), "uncited_sentences": failures}


def ask_model(question: str, primary_sources: list[dict[str, Any]], caution_sources: list[dict[str, Any]],
              mode: str = "thorough") -> str:
    cfg = llm_config(mode)
    if cfg["provider"] == "openai" and not cfg["api_key"]:
        raise RuntimeError("Set OPENAI_API_KEY to use the configured OpenAI-compatible model.")
    messages = answer_messages(question, primary_sources, caution_sources)
    payload: dict[str, Any]
    url: str
    headers = {"Content-Type": "application/json"}
    if cfg["provider"] == "openai":
        url = cfg["base_url"] + "/chat/completions"
        headers["Authorization"] = "Bearer " + cfg["api_key"]
        payload = {"model": cfg["model"], "temperature": 0.1, "messages": messages}
    else:
        url = cfg["base_url"] + "/api/chat"
        payload = {"model": cfg["model"], "stream": False, "keep_alive": cfg["keep_alive"],
                   "think": cfg["think"] in {"1", "true", "yes", "on"},
                   "options": ollama_options(cfg), "messages": messages}
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {cfg['provider']} at {cfg['base_url']}: {exc.reason}") from exc
    if cfg["provider"] == "openai":
        choice = result["choices"][0]
        answer = choice["message"]["content"].strip()
        return trim_truncated_answer(answer) if choice.get("finish_reason") == "length" else answer
    answer = result["message"]["content"].strip()
    return trim_truncated_answer(answer) if result.get("done_reason") == "length" else answer


def stream_model(question: str, primary_sources: list[dict[str, Any]],
                 caution_sources: list[dict[str, Any]], mode: str = "thorough",
                 finish_state: dict[str, Any] | None = None) -> Iterable[str]:
    cfg = llm_config(mode)
    if finish_state is not None:
        finish_state["truncated"] = False
    messages = answer_messages(question, primary_sources, caution_sources)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if cfg["provider"] == "openai":
        if not cfg["api_key"]:
            raise RuntimeError("Set OPENAI_API_KEY to use the configured OpenAI-compatible model.")
        url = cfg["base_url"] + "/chat/completions"
        headers["Authorization"] = "Bearer " + cfg["api_key"]
        payload = {"model": cfg["model"], "temperature": 0.1, "stream": True, "messages": messages}
    else:
        url = cfg["base_url"] + "/api/chat"
        payload = {"model": cfg["model"], "stream": True, "keep_alive": cfg["keep_alive"],
                   "think": cfg["think"] in {"1", "true", "yes", "on"},
                   "options": ollama_options(cfg), "messages": messages}
        headers["Accept"] = "application/x-ndjson"
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    complete = False
    visible = False
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if cfg["provider"] == "openai":
                    if line.startswith((":", "event:", "id:")):
                        continue
                    data = line[5:].strip() if line.startswith("data:") else line
                    if data == "[DONE]":
                        complete = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(f"OpenAI-compatible endpoint returned invalid stream JSON: {exc}") from exc
                    if chunk.get("error"):
                        raise RuntimeError("OpenAI-compatible endpoint returned an error payload.")
                    choices = chunk.get("choices") or []
                    if choices and choices[0].get("finish_reason") == "length" and finish_state is not None:
                        finish_state["truncated"] = True
                    part = (choices[0].get("delta") or {}).get("content", "") if choices else ""
                    if isinstance(part, list):
                        part = "".join(item.get("text", "") for item in part if isinstance(item, dict))
                    if isinstance(part, str) and part:
                        visible = True
                        yield part
                else:
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(f"Ollama returned invalid stream JSON: {exc}") from exc
                    if chunk.get("error"):
                        raise RuntimeError(str(chunk["error"]))
                    part = chunk.get("message", {}).get("content", "")
                    if part:
                        visible = True
                        yield part
                    if chunk.get("done"):
                        if finish_state is not None:
                            finish_state["truncated"] = chunk.get("done_reason") == "length"
                        complete = True
                        break
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"{cfg['provider']} returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {cfg['provider']} at {cfg['base_url']}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"{cfg['provider']} did not complete within 180 seconds.") from exc
    if not complete:
        raise RuntimeError(f"{cfg['provider']} stream ended before completion.")
    if not visible:
        raise RuntimeError(f"{cfg['provider']} returned no visible answer.")


app = FastAPI(title="HangarIndex", description="Local-first aerospace maintenance document search")
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")

MODEL_PRELOAD_LOCK = threading.Lock()
MODEL_PRELOAD: dict[str, Any] = {"state": "not_started", "model": "", "error": ""}


def preload_model(cfg: dict[str, str]) -> None:
    with MODEL_PRELOAD_LOCK:
        MODEL_PRELOAD.update({"state": "loading", "model": cfg["model"], "error": ""})
    try:
        payload = {"model": cfg["model"], "prompt": "", "stream": False,
                   "keep_alive": cfg.get("keep_alive", "30m"), "options": ollama_options(cfg)}
        request = urllib.request.Request(
            cfg["base_url"] + "/api/generate", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        with MODEL_PRELOAD_LOCK:
            MODEL_PRELOAD.update({"state": "ready", "model": cfg["model"], "error": ""})
    except Exception as exc:
        with MODEL_PRELOAD_LOCK:
            MODEL_PRELOAD.update({"state": "unavailable", "model": cfg["model"], "error": str(exc)[:240]})


@app.on_event("startup")
def preload_default_model_on_startup() -> None:
    cfg = llm_config()
    start_embedding_refresh()
    if cfg["provider"] != "ollama":
        with MODEL_PRELOAD_LOCK:
            MODEL_PRELOAD.update({"state": "skipped", "model": cfg["model"], "error": "Local Ollama is not configured."})
        return
    threading.Thread(target=preload_model, args=(cfg,), daemon=True, name="hangar-model-preload").start()


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#101f2e"><title>HangarIndex access</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f7f7f3;color:#182a39;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{width:calc(100% - 32px);max-width:390px;margin:12vh auto;padding:24px;background:#fff;border:1px solid #e2e5e2;border-radius:8px}
h1{font-size:21px;line-height:1.25;margin:0 0 8px}p{color:#637580;margin:0 0 18px}label{display:block;font-size:13px;font-weight:600;margin-bottom:6px}
input,button{width:100%;height:44px;border-radius:5px;font:inherit}input{padding:0 11px;border:1px solid #cbd4d0}button{margin-top:10px;border:0;background:#172c3d;color:#fff;font-weight:650;cursor:pointer}
#error{min-height:1.5em;margin:10px 0 0;color:#9a4b32;font-size:13px}
</style></head><body><main><h1>HangarIndex</h1><p>Enter the access passphrase to open this private document library.</p>
<form id="access-form"><label for="access-token">Passphrase</label><input id="access-token" name="token" type="password" autocomplete="current-password" required>
<button type="submit">Continue</button><p id="error" role="alert"></p></form></main>
<script>document.getElementById('access-form').addEventListener('submit',async(event)=>{event.preventDefault();const input=document.getElementById('access-token');const error=document.getElementById('error');try{const response=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:input.value})});if(!response.ok)throw new Error('Passphrase not accepted.');location.replace('/');}catch(e){error.textContent=e.message;input.select();}});</script>
</body></html>"""


def session_cookie_value(expires_at: int) -> str:
    message = f"HangarIndex session v1:{expires_at}".encode("ascii")
    signature = hmac.new(ACCESS_TOKEN.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{expires_at}.{signature}"


def has_valid_session(request: Request) -> bool:
    supplied = request.cookies.get(SESSION_COOKIE, "")
    try:
        expires_text, signature = supplied.split(".", 1)
        expires_at = int(expires_text)
    except (ValueError, AttributeError):
        return False
    now = int(datetime.now(timezone.utc).timestamp())
    if expires_at <= now or expires_at > now + SESSION_MAX_AGE + 60:
        return False
    expected = session_cookie_value(expires_at).split(".", 1)[1]
    return hmac.compare_digest(signature.encode("utf-8"), expected.encode("ascii"))


def login_retry_after(client_key: str) -> int:
    now = time.monotonic()
    with LOGIN_ATTEMPTS_LOCK:
        attempt = LOGIN_ATTEMPTS.get(client_key)
        if not attempt or attempt[1] <= now:
            return 0
        return max(1, math.ceil(attempt[1] - now))


def record_failed_login(client_key: str) -> None:
    now = time.monotonic()
    with LOGIN_ATTEMPTS_LOCK:
        for stale_key, (_, retry_at) in tuple(LOGIN_ATTEMPTS.items()):
            if retry_at + LOGIN_BACKOFF_MAX_SECONDS < now:
                LOGIN_ATTEMPTS.pop(stale_key, None)
        failures = LOGIN_ATTEMPTS.get(client_key, (0, 0.0))[0] + 1
        delay = min(LOGIN_BACKOFF_MAX_SECONDS, 2 ** min(failures - 1, 8))
        LOGIN_ATTEMPTS[client_key] = (failures, now + delay)


def clear_login_failures(client_key: str) -> None:
    with LOGIN_ATTEMPTS_LOCK:
        LOGIN_ATTEMPTS.pop(client_key, None)


@app.middleware("http")
async def enforce_host_and_access(request: Request, call_next) -> Response:
    try:
        require_allowed_host(request)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    if TAILNET_ACCESS and request.url.path != "/api/auth/login" and not has_valid_session(request):
        if request.method == "GET" and request.url.path == "/":
            return HTMLResponse(LOGIN_PAGE, headers={"Cache-Control": "no-store"})
        return JSONResponse(status_code=401, content={"detail": "Enter the access passphrase first."},
                            headers={"Cache-Control": "no-store"})
    return await call_next(request)


@app.get("/")
def home() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.post("/api/auth/login")
def login(body: LoginRequest, request: Request) -> Response:
    if not TAILNET_ACCESS:
        raise HTTPException(404, "Passphrase login is only enabled for tailnet access.")
    client_key = request.client.host if request.client else "unknown"
    retry_after = login_retry_after(client_key)
    if retry_after:
        raise HTTPException(429, "Too many attempts. Try again shortly.", headers={"Retry-After": str(retry_after)})
    if not hmac.compare_digest(body.token.encode("utf-8"), ACCESS_TOKEN.encode("utf-8")):
        record_failed_login(client_key)
        raise HTTPException(401, "Passphrase not accepted.")
    clear_login_failures(client_key)
    response = JSONResponse({"ok": True})
    expires_at = int(datetime.now(timezone.utc).timestamp()) + SESSION_MAX_AGE
    response.set_cookie(SESSION_COOKIE, session_cookie_value(expires_at), max_age=SESSION_MAX_AGE, path="/",
                        httponly=True, secure=request.url.scheme == "https", samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/status")
def status() -> dict[str, Any]:
    with connect() as db:
        stats = db.execute("SELECT COUNT(*) AS docs,COALESCE(SUM(size),0) AS bytes FROM documents WHERE parse_status='indexed' AND name <> ? COLLATE NOCASE", (SOURCE_REGISTER_NAME,)).fetchone()
        chunk_count = db.execute("SELECT COUNT(*) FROM chunks c JOIN documents d ON d.path=c.path WHERE d.name <> ? COLLATE NOCASE", (SOURCE_REGISTER_NAME,)).fetchone()[0]
    with STATE_LOCK:
        state = dict(INDEX_STATE)
    with MODEL_PRELOAD_LOCK:
        preload = dict(MODEL_PRELOAD)
    with EMBEDDING_STATE_LOCK:
        embeddings = dict(EMBEDDING_STATE)
    return {"stats": {"documents": stats["docs"], "chunks": chunk_count, "bytes": stats["bytes"]},
            "index": state, "settings": SETTINGS.copy(), "llm": llm_state(), "model_preload": preload,
            "embeddings": embeddings,
            "retrieval_mode": "hybrid" if EMBEDDINGS_ENABLED and embeddings["state"] == "ready" else "fts"}


@app.get("/api/settings")
def settings() -> dict[str, Any]:
    return {"corpus_dir": SETTINGS.get("corpus_dir", "")}


@app.post("/api/settings")
def update_settings(body: SettingsRequest) -> dict[str, Any]:
    normalized = body.corpus_dir.strip().strip('"')
    SETTINGS["corpus_dir"] = normalized
    SETTINGS_PATH.write_text(json.dumps(SETTINGS, indent=2), encoding="utf-8")
    return {"corpus_dir": normalized}


@app.post("/api/index")
def start_index(body: IndexRequest) -> dict[str, str]:
    root_text = (body.corpus_dir or SETTINGS.get("corpus_dir", "")).strip().strip('"')
    if not root_text:
        raise HTTPException(400, "Enter a folder path to index.")
    root = Path(root_text).expanduser()
    if not root.exists() or not root.is_dir():
        raise HTTPException(400, f"Folder is unavailable: {root_text}")
    with STATE_LOCK:
        if INDEX_STATE["running"]:
            raise HTTPException(409, "An index run is already active.")
    SETTINGS["corpus_dir"] = root_text
    SETTINGS_PATH.write_text(json.dumps(SETTINGS, indent=2), encoding="utf-8")
    thread = threading.Thread(target=index_folder, args=(root_text,), daemon=True, name="hangar-indexer")
    thread.start()
    return {"status": "started", "corpus_dir": root_text}


@app.get("/api/search")
def search_get(request: Request, query: str, limit: int = 12) -> dict[str, Any]:
    require_allowed_host(request)
    if len(query.strip()) < 2:
        raise HTTPException(400, "Enter at least two characters.")
    matches = retrieve(query, min(max(limit, 1), 40))
    docs: dict[str, dict[str, Any]] = {}
    for row in matches:
        doc = docs.setdefault(row["path"], {"path": row["path"], "name": row["name"], "doc_type": row["doc_type"], "extension": row["extension"],
                                             "modified": row["modified"], "size": row["size"], "excerpts": [], "passages": [], "pages": []})
        if len(doc["excerpts"]) < 2:
            doc["excerpts"].append(row["excerpt"])
            doc["passages"].append({"excerpt": row["excerpt"], "page": row["page"]})
        if row["page"] and row["page"] not in doc["pages"]:
            doc["pages"].append(row["page"])
    ordered = list(docs.values())[:limit]
    regulations = [d for d in ordered if d["doc_type"].startswith(REGULATORY_DOC_TYPES)]
    for document in ordered:
        document["location_label"] = "Slides" if document["extension"] == ".pptx" else "Sheets" if document["extension"] == ".xlsx" else "Pages"
    return {"query": query, "total": len(ordered), "documents": ordered, "regulatory_refs": regulations,
            "highlight_terms": make_fts_terms(query)}


@app.post("/api/retrieve")
def retrieve_only(body: AskRequest) -> dict[str, Any]:
    """Return the app's frozen primary/caution retrieval and prompt without a model call."""
    primary_sources, caution_sources, sources = collect_answer_sources(body.question)
    return {"question": body.question, "sources": sources,
            "highlight_terms": make_fts_terms(body.question),
            "messages": answer_messages(body.question, primary_sources, caution_sources)}


@app.post("/api/ask")
def ask(body: AskRequest) -> dict[str, Any]:
    primary_sources, caution_sources, sources = collect_answer_sources(body.question)
    if not primary_sources:
        return {"question": body.question, "answer": "I could not find matching passages in the current index. Try a part number, ATA chapter, manual number, document title, or regulatory citation. If you have not indexed a folder yet, select one in the left panel and start indexing.", "sources": [], "model_used": False, "model_error": None,
                "mode": body.mode, "llm": llm_state(body.mode),
                "highlight_terms": make_fts_terms(body.question), "citation_check": None}
    try:
        answer = ask_model(body.question, primary_sources, caution_sources, body.mode)
        answer = remove_false_procedure_disclaimer(body.question, answer, primary_sources)
        answer = drop_none_after_cited_watch_out(answer, caution_sources)
        model_used, model_error = True, None
        citation_check = validate_answer_citations(answer, sources)
    except Exception as exc:
        answer = "I found relevant passages, but the language model is not available right now. Review the cited source excerpts below, or start the configured local model and try again."
        model_used, model_error = False, str(exc)[:400]
        citation_check = None
    reg_sources = [s for s in sources if s["doc_type"].startswith(REGULATORY_DOC_TYPES)]
    return {"question": body.question, "answer": answer, "sources": sources, "regulatory_refs": reg_sources,
            "mode": body.mode, "model_used": model_used, "model_error": model_error, "llm": llm_state(body.mode),
            "highlight_terms": make_fts_terms(body.question), "citation_check": citation_check}


@app.post("/api/ask/stream")
def ask_stream(body: AskRequest) -> StreamingResponse:
    primary_sources, caution_sources, sources = collect_answer_sources(body.question)
    regulatory_sources = [source for source in sources if source["doc_type"].startswith(REGULATORY_DOC_TYPES)]
    no_source_answer = ("I could not find matching passages in the current index. Try a part number, ATA chapter, "
                        "manual number, document title, or regulatory citation. If you have not indexed a folder yet, "
                        "select one in the left panel and start indexing.")

    def events() -> Iterable[str]:
        if not primary_sources:
            metadata = {"question": body.question, "answer": no_source_answer, "sources": [],
                        "regulatory_refs": [], "highlight_terms": make_fts_terms(body.question),
                        "model_used": False, "streaming": False, "mode": body.mode, "llm": llm_state(body.mode)}
            yield "data: " + json.dumps({"type": "meta", "data": metadata}) + "\n\n"
            yield "data: " + json.dumps({"type": "done", "citation_check": None}) + "\n\n"
            return

        metadata = {"question": body.question, "sources": sources, "regulatory_refs": regulatory_sources,
                    "highlight_terms": make_fts_terms(body.question), "model_used": True,
                    "streaming": True, "mode": body.mode, "llm": llm_state(body.mode)}
        yield "data: " + json.dumps({"type": "meta", "data": metadata}, ensure_ascii=False) + "\n\n"
        pieces: list[str] = []
        finish_state: dict[str, Any] = {}
        try:
            for piece in stream_model(body.question, primary_sources, caution_sources, body.mode, finish_state):
                pieces.append(piece)
                yield "data: " + json.dumps({"type": "token", "text": piece}, ensure_ascii=False) + "\n\n"
            answer = "".join(pieces).strip()
            if finish_state.get("truncated"):
                answer = trim_truncated_answer(answer)
            answer = remove_false_procedure_disclaimer(body.question, answer, primary_sources)
            answer = drop_none_after_cited_watch_out(answer, caution_sources)
            yield "data: " + json.dumps({"type": "done", "model_used": True, "mode": body.mode,
                                          "answer": answer,
                                          "citation_check": validate_answer_citations(answer, sources)}) + "\n\n"
        except Exception as exc:
            yield "data: " + json.dumps({"type": "error", "detail": str(exc)[:400]}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/library")
def library(limit: int = 100) -> dict[str, Any]:
    limit = min(max(limit, 1), 300)
    with connect() as db:
        rows = db.execute("SELECT path,name,extension,doc_type,size,modified_ns,parse_status,error FROM documents WHERE name <> ? COLLATE NOCASE ORDER BY name COLLATE NOCASE LIMIT ?", (SOURCE_REGISTER_NAME, limit)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["modified"] = datetime.fromtimestamp(item.pop("modified_ns") / 1e9).strftime("%Y-%m-%d")
        items.append(item)
    return {"documents": items, "count": len(items)}


def is_loopback_request(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        address = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


def require_allowed_host(request: Request) -> None:
    authority = request.headers.get("host", "")
    try:
        parsed = urlsplit("//" + authority)
        host = parsed.hostname
        _ = parsed.port  # Validate malformed authorities such as nonnumeric ports.
    except ValueError:
        parsed = None
        host = None
    if parsed is None or parsed.username or parsed.password or not host or host.lower() not in ALLOWED_HOSTS:
        raise HTTPException(403, "Host header is not allowed.")


def indexed_source(path: str) -> tuple[Path, str]:
    # Resolve client input only through an exact, parameterized database match.
    with connect() as db:
        row = db.execute("SELECT path,extension FROM documents WHERE path=? AND parse_status='indexed'", (path,)).fetchone()
    if row is None:
        raise HTTPException(404, "Source is not in the current index.")
    source_path = Path(row["path"])
    if not source_path.is_file():
        raise HTTPException(404, "The indexed source file is no longer available.")
    return source_path, row["extension"].lower()


def open_with_default_app(path: Path) -> None:
    if os.name == "nt":
        getattr(os, "startfile")(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@app.get("/api/open")
def open_pdf(path: str, request: Request) -> FileResponse:
    require_allowed_host(request)
    if not is_loopback_request(request) and not TAILNET_ACCESS:
        raise HTTPException(403, "Source opening is available only from this computer.")
    source_path, extension = indexed_source(path)
    if extension != ".pdf":
        raise HTTPException(415, "Use the local open action for non-PDF files.")
    response = FileResponse(source_path, media_type="application/pdf")
    response.headers["Content-Disposition"] = "inline"
    return response


@app.post("/api/open", status_code=204)
def open_non_pdf(body: OpenRequest, request: Request) -> Response:
    require_allowed_host(request)
    if not is_loopback_request(request):
        raise HTTPException(403, "Source opening is available only from this computer.")
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site != "same-origin":
        raise HTTPException(403, "Cross-site source opening is not allowed.")
    origin = request.headers.get("origin")
    expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if origin and origin.rstrip("/") != expected_origin.rstrip("/"):
        raise HTTPException(403, "Cross-origin source opening is not allowed.")
    source_path, extension = indexed_source(body.path)
    if extension == ".pdf":
        raise HTTPException(415, "PDF files open in the browser.")
    try:
        open_with_default_app(source_path)
    except OSError as exc:
        raise HTTPException(500, "Could not open this source in its default application.") from exc
    return Response(status_code=204)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=BIND_HOST, port=int(os.environ.get("PORT", "8000")), reload=False)
