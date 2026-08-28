"""The knowledge graph: one SQLite file, typed nodes and edges, and a cache.

WHY NOT A GRAPH DATABASE. The obvious question, answered with the numbers
rather than a preference. Every relationship this terminal knows about -
ownership, fund holdings, index membership, supply chains, sectors, peers -
totals ~120k edges at full NSE scale. The traversals that matter are two
or three hops ("which schemes hold what LIC also holds", "which companies
does one family control through its LLPs"), and SQLite answers those in
single-digit milliseconds off a covering index. A graph engine earns its
keep past ~100M edges or at unbounded traversal depth; buying one here
would cost a server or a heavy dependency and buy nothing. So: graph
MODEL, relational STORAGE - if the data ever outgrows this, the shape of
the abstraction lets the storage swap without touching callers.

WHAT IT SOLVES. Before this, opening a company re-fetched everything -
profile, filings, a 400-page annual report. The terminal now writes what
it learns into one file and reads it back instantly, refetching only when
a record is older than its own stated freshness.

THREE TABLES, ONE JOB EACH.
  node    an entity: company, holder, person, scheme, amc, sector, index,
          commodity, facility. Identified by "kind:key".
  edge    a typed, weighted, dated, SOURCED relationship between nodes.
          Every edge records where it came from, so the graph can always
          explain itself.
  alias   the mapping service. "LIFE INSURANCE CORPORATION OF INDIA P",
          "Life Insurance Corporation Of India" and "LIC of India" are one
          entity; filings spell it three ways. Aliases resolve raw strings
          to canonical nodes and are the reason the graph joins at all.

Plus `doc`, a plain content cache with per-entry TTL, so expensive fetches
(annual reports, profiles) survive restarts.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS node (
    id      TEXT PRIMARY KEY,      -- "company:RELIANCE"
    kind    TEXT NOT NULL,
    name    TEXT NOT NULL,
    meta    TEXT,                  -- JSON
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS node_kind ON node(kind);
CREATE INDEX IF NOT EXISTS node_name ON node(name);

CREATE TABLE IF NOT EXISTS edge (
    src    TEXT NOT NULL,
    dst    TEXT NOT NULL,
    rel    TEXT NOT NULL,
    weight REAL,                   -- percent, value, whatever the rel means
    unit   TEXT,
    as_of  TEXT,
    source TEXT NOT NULL,          -- an edge that cannot say where it came
    meta   TEXT,                   -- from does not belong in the graph
    PRIMARY KEY (src, dst, rel, as_of)
);
CREATE INDEX IF NOT EXISTS edge_src ON edge(src, rel);
CREATE INDEX IF NOT EXISTS edge_dst ON edge(dst, rel);
CREATE INDEX IF NOT EXISTS edge_rel ON edge(rel, weight);

CREATE TABLE IF NOT EXISTS alias (
    alias   TEXT PRIMARY KEY,      -- normalised raw string
    node_id TEXT NOT NULL,
    raw     TEXT,
    source  TEXT
);
CREATE INDEX IF NOT EXISTS alias_node ON alias(node_id);

CREATE TABLE IF NOT EXISTS doc (
    key     TEXT PRIMARY KEY,
    kind    TEXT,
    payload TEXT NOT NULL,
    fetched REAL NOT NULL,
    ttl_s   REAL
);
CREATE INDEX IF NOT EXISTS doc_kind ON doc(kind);
"""

# Legal-form noise that makes the same entity look like three.
# Deliberately conservative. Strip legal form and appended qualifiers only:
# words that DISCRIMINATE between entities must survive, or "SBI Mutual Fund"
# and "SBI" (the bank) collapse into one node and the graph starts lying.
# Genuine synonymy ("Nippon Life India Trustee Ltd" = "Nippon India MF") is
# an ALIAS problem, not a normalisation problem.
_SUFFIX = re.compile(
    r"\b(LIMITED|LTD|PRIVATE|PVT|LLP|INC|PLC|CORP|CORPORATION|COMPANY|CO|"
    r"AND|THE|OF|A\/C|AC|THROUGH|ITS|VARIOUS|SCHEMES|SCHEME)\b")


def normalise(name: str) -> str:
    """The key both sides of a join must agree on."""
    n = str(name or "").upper()
    n = re.sub(r"[^A-Z0-9 ]", " ", n)
    n = _SUFFIX.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    # Filings append stray single letters ("... OF INDIA P", "... A"). They
    # carry no identity and split one holder into several nodes.
    return re.sub(r"(\s+\b[A-Z0-9]\b)+$", "", n).strip()


# Curated identity map. Normalisation handles spelling and legal form; it
# cannot know that "LIC OF INDIA" and "LIFE INSURANCE CORPORATION OF INDIA"
# are one institution, because nothing in the strings says so. That is
# knowledge, so it is written down as data - a list, auditable and editable -
# rather than guessed by a fuzzy matcher that would also merge things that
# are genuinely different.
CANONICAL: dict[str, str] = {}


def _canon(pattern: str, name: str) -> None:
    CANONICAL[pattern] = name


for _pat, _name in [
    ("LIC", "Life Insurance Corporation of India"),
    ("LIFE INSURANCE", "Life Insurance Corporation of India"),
    ("SBI MUTUAL", "SBI Mutual Fund"),
    ("SBI FUNDS MANAGEMENT", "SBI Mutual Fund"),
    ("ICICI PRUDENTIAL", "ICICI Prudential Mutual Fund"),
    ("HDFC MUTUAL", "HDFC Mutual Fund"),
    ("HDFC TRUSTEE", "HDFC Mutual Fund"),
    ("NIPPON LIFE INDIA TRUSTEE", "Nippon India Mutual Fund"),
    ("NIPPON INDIA", "Nippon India Mutual Fund"),
    ("KOTAK MAHINDRA TRUSTEE", "Kotak Mahindra Mutual Fund"),
    ("AXIS MUTUAL", "Axis Mutual Fund"),
    ("AXIS TRUSTEE", "Axis Mutual Fund"),
    ("UTI TRUSTEE", "UTI Mutual Fund"),
    ("ADITYA BIRLA SUN LIFE TRUSTEE", "Aditya Birla Sun Life Mutual Fund"),
    ("MIRAE ASSET", "Mirae Asset Mutual Fund"),
    ("GOVERNMENT PENSION FUND GLOBAL", "Norges Bank (Government Pension Fund Global)"),
    ("NORGES BANK", "Norges Bank (Government Pension Fund Global)"),
    ("GOVERNMENT SINGAPORE", "Government of Singapore"),
    ("MONETARY AUTHORITY SINGAPORE", "Monetary Authority of Singapore"),
    ("NPS TRUST", "NPS Trust"),
    ("VANGUARD", "Vanguard"),
    ("BLACKROCK", "BlackRock"),
]:
    _canon(_pat, _name)


def canonical_name(raw: str) -> str:
    """The institution's one name, if this is one we know. Prefix match on
    the normalised string, longest pattern first."""
    n = normalise(raw)
    for pat in sorted(CANONICAL, key=len, reverse=True):
        if n.startswith(pat):
            return CANONICAL[pat]
    return raw


# SBO declarations pack a whole family into one string with no separators:
# "Mukesh Ambani Nita Ambani Isha Ambani Akash Ambani and Anant Ambani
# together and collectively". Stored whole it is one useless node; split
# badly it invents people. The rule below only splits on a REPEATED SURNAME,
# which is evidence in the string itself, and otherwise leaves the text
# alone - a coarse node beats a fabricated person.
_SBO_TAIL = re.compile(
    r"\b(together and collectively|together|collectively|jointly|and others?)\b\.?\s*$",
    re.I)


def split_beneficial_owners(text: str) -> list[str]:
    t = str(text or "").strip()
    for _ in range(3):
        t = _SBO_TAIL.sub("", t).strip(" ,.;")
    if not t:
        return []
    parts = [p.strip() for p in re.split(r"\s*(?:,|;|\band\b|&)\s*", t) if p.strip()]
    out: list[str] = []
    for part in parts:
        toks = part.split()
        if len(toks) <= 3:
            out.append(part)
            continue
        # a surname repeating inside one part means several people ran together
        counts: dict[str, int] = {}
        for tk in toks:
            counts[tk.upper()] = counts.get(tk.upper(), 0) + 1
        surname = next((tk for tk in toks if counts[tk.upper()] > 1
                        and len(tk) > 2), None)
        if not surname:
            out.append(part)
            continue
        buf: list[str] = []
        for tk in toks:
            buf.append(tk)
            if tk.upper() == surname.upper():
                out.append(" ".join(buf))
                buf = []
        if buf:
            out.append(" ".join(buf))
    seen, uniq = set(), []
    for n in out:
        k = normalise(n)
        if k and k not in seen and len(n) > 3:
            seen.add(k)
            uniq.append(n.strip())
    return uniq


def node_id(kind: str, key: str) -> str:
    return f"{kind}:{str(key).strip().upper()}"


@dataclass
class Neighbour:
    id: str
    kind: str
    name: str
    rel: str
    weight: float | None
    unit: str | None
    as_of: str | None
    source: str
    direction: str


class GraphStore:
    def __init__(self, path: Path | None = None) -> None:
        from shunkan.config import APP_DIR, ensure_dirs

        if path is None:
            ensure_dirs()
            path = APP_DIR / "shunkan.db"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self.path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        # WAL: the ingest loop writes while the UI reads, and a reader must
        # never block behind a bulk import.
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        # Wait for a writer instead of failing instantly. WAL lets readers and
        # ONE writer coexist, but two writers still collide - the web process
        # and an ingest run, say - and the default timeout is zero, so the
        # loser raised "database is locked" and a bulk import died mid-way.
        # Thirty seconds is far longer than any write here takes.
        self._con.execute("PRAGMA busy_timeout=30000")
        self._con.executescript(SCHEMA)
        self._con.commit()

    # -- writes ------------------------------------------------------------

    def put_node(self, kind: str, key: str, name: str, meta: dict | None = None) -> str:
        nid = node_id(kind, key)
        self._con.execute(
            "INSERT INTO node(id,kind,name,meta,updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "meta=COALESCE(excluded.meta, node.meta), updated=excluded.updated",
            (nid, kind, name, json.dumps(meta or {}), time.time()))
        return nid

    def put_alias(self, raw: str, nid: str, source: str = "") -> None:
        key = normalise(raw)
        if not key:
            return
        self._con.execute(
            "INSERT OR REPLACE INTO alias(alias,node_id,raw,source) VALUES(?,?,?,?)",
            (key, nid, raw, source))

    def put_edges(self, rows: list[dict]) -> int:
        """Bulk upsert. Every row must carry a source; the graph explains
        itself or it does not get written."""
        payload = []
        for r in rows:
            if not r.get("source"):
                raise ValueError(f"edge without a source: {r}")
            payload.append((r["src"], r["dst"], r["rel"], r.get("weight"),
                            r.get("unit"), r.get("as_of") or "",
                            r["source"], json.dumps(r.get("meta") or {})))
        self._con.executemany(
            "INSERT INTO edge(src,dst,rel,weight,unit,as_of,source,meta) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(src,dst,rel,as_of) DO UPDATE SET "
            "weight=excluded.weight, unit=excluded.unit, source=excluded.source, "
            "meta=excluded.meta", payload)
        return len(payload)

    def commit(self) -> None:
        self._con.commit()

    # -- reads -------------------------------------------------------------

    def resolve(self, text: str, kind: str | None = None) -> str | None:
        """The mapping service: any spelling in, canonical node id out."""
        key = normalise(text)
        if not key:
            return None
        row = self._con.execute(
            "SELECT node_id FROM alias WHERE alias=?", (key,)).fetchone()
        if row:
            return row["node_id"]
        q = "SELECT id FROM node WHERE name=? COLLATE NOCASE"
        args: list = [text]
        if kind:
            q += " AND kind=?"
            args.append(kind)
        row = self._con.execute(q, args).fetchone()
        return row["id"] if row else None

    def node(self, nid: str) -> dict | None:
        r = self._con.execute("SELECT * FROM node WHERE id=?", (nid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["meta"] = json.loads(d.get("meta") or "{}")
        return d

    def search(self, q: str, kind: str | None = None, limit: int = 30) -> list[dict]:
        sql = "SELECT id,kind,name FROM node WHERE name LIKE ?"
        args: list = [f"%{q}%"]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY length(name) LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._con.execute(sql, args)]

    def neighbours(self, nid: str, rel: str | None = None,
                   direction: str = "both", limit: int = 200) -> list[Neighbour]:
        out: list[Neighbour] = []
        if direction in ("out", "both"):
            sql = ("SELECT e.dst id, n.kind, n.name, e.rel, e.weight, e.unit, "
                   "e.as_of, e.source FROM edge e JOIN node n ON n.id=e.dst "
                   "WHERE e.src=?")
            args: list = [nid]
            if rel:
                sql += " AND e.rel=?"
                args.append(rel)
            sql += " ORDER BY e.weight DESC NULLS LAST LIMIT ?"
            args.append(limit)
            out += [Neighbour(**dict(r), direction="out")
                    for r in self._con.execute(sql, args)]
        if direction in ("in", "both"):
            sql = ("SELECT e.src id, n.kind, n.name, e.rel, e.weight, e.unit, "
                   "e.as_of, e.source FROM edge e JOIN node n ON n.id=e.src "
                   "WHERE e.dst=?")
            args = [nid]
            if rel:
                sql += " AND e.rel=?"
                args.append(rel)
            sql += " ORDER BY e.weight DESC NULLS LAST LIMIT ?"
            args.append(limit)
            out += [Neighbour(**dict(r), direction="in")
                    for r in self._con.execute(sql, args)]
        return out

    def co_held(self, nid: str, rel: str = "scheme_holds", limit: int = 25) -> list[dict]:
        """Two hops: everything the holders of THIS also hold.

        The query a graph database is usually bought for, in one indexed
        join - which is exactly why one is not needed at this size."""
        sql = f"""
        SELECT e2.dst id, n.name, COUNT(*) shared, SUM(e2.weight) total_weight
        FROM edge e1
        JOIN edge e2 ON e2.src = e1.src AND e2.rel = e1.rel AND e2.dst != e1.dst
        JOIN node n ON n.id = e2.dst
        WHERE e1.dst = ? AND e1.rel = ?
          AND n.name IS NOT NULL AND TRIM(n.name) NOT IN ('', '-', '.')
        GROUP BY e2.dst ORDER BY shared DESC, total_weight DESC LIMIT ?
        """
        return [dict(r) for r in self._con.execute(sql, (nid, rel, limit))]

    def stats(self) -> dict:
        c = self._con
        kinds = {r["kind"]: r["n"] for r in
                 c.execute("SELECT kind, COUNT(*) n FROM node GROUP BY kind")}
        rels = {r["rel"]: r["n"] for r in
                c.execute("SELECT rel, COUNT(*) n FROM edge GROUP BY rel")}
        return {
            "nodes": c.execute("SELECT COUNT(*) n FROM node").fetchone()["n"],
            "edges": c.execute("SELECT COUNT(*) n FROM edge").fetchone()["n"],
            "aliases": c.execute("SELECT COUNT(*) n FROM alias").fetchone()["n"],
            "by_kind": kinds, "by_rel": rels,
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "path": str(self.path),
        }

    # -- content cache -----------------------------------------------------

    def cache_get(self, key: str, max_age_s: float | None = None):
        r = self._con.execute("SELECT payload, fetched, ttl_s FROM doc WHERE key=?",
                              (key,)).fetchone()
        if not r:
            return None
        age = time.time() - r["fetched"]
        limit = max_age_s if max_age_s is not None else r["ttl_s"]
        if limit is not None and age > limit:
            return None
        return json.loads(r["payload"])

    def cache_age(self, key: str) -> float | None:
        r = self._con.execute("SELECT fetched FROM doc WHERE key=?", (key,)).fetchone()
        return (time.time() - r["fetched"]) if r else None

    def cache_put(self, key: str, payload, kind: str = "", ttl_s: float | None = None):
        self._con.execute(
            "INSERT OR REPLACE INTO doc(key,kind,payload,fetched,ttl_s) VALUES(?,?,?,?,?)",
            (key, kind, json.dumps(payload, default=str), time.time(), ttl_s))
        self._con.commit()


_STORE: GraphStore | None = None


def graph(path: Path | None = None) -> GraphStore:
    """Process-wide handle. One connection, WAL, safe across threads."""
    global _STORE
    if _STORE is None or path is not None:
        _STORE = GraphStore(path)
    return _STORE
