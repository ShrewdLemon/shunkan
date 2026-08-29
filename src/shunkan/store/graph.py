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

CREATE INDEX IF NOT EXISTS idx_node_name ON node(name);
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

    # A company outranks a role. The same name legitimately exists as several
    # node kinds - RELIANCE is a company, and it is also a `customer` node
    # because another filer lists it as a buyer, and an `input` node because a
    # third lists it as a supplier. The alias table maps one key to one node,
    # so whichever writer ran last won, and resolve("RELIANCE") returned
    # customer:RELIANCE - a node with none of the company's edges. Anything
    # walking the graph from a ticker got an empty trail and no error.
    _KIND_RANK = {"company": 0, "holder": 1, "person": 2, "scheme": 3,
                  "amc": 4, "customer": 5, "input": 6, "output": 7,
                  "facilitie": 8, "sector": 9}

    def resolve(self, text: str, kind: str | None = None,
                prefer: str | None = "company") -> str | None:
        """The mapping service: any spelling in, canonical node id out.

        `prefer` breaks the tie when one name spans several kinds. It defaults
        to "company" because that is what a caller almost always means when it
        hands over a ticker or a legal name; pass prefer=None for the raw
        alias hit.
        """
        key = normalise(text)
        if not key:
            return None
        if kind:
            # An explicit kind is a CONSTRAINT, not a hint. Falling through to
            # the unfiltered alias when the kind-filtered lookup missed is how
            # resolve("RELIANCE", kind="company") returned customer:RELIANCE,
            # and link_legal_names then aliased every legal name onto that
            # wrong node. Ask for a company, get a company or nothing.
            row = self._con.execute(
                "SELECT n.id FROM alias a JOIN node n ON n.id=a.node_id "
                "WHERE a.alias=? AND n.kind=?", (key, kind)).fetchone()
            if row:
                return row["id"]
            row = self._con.execute(
                "SELECT id FROM node WHERE name=? COLLATE NOCASE AND kind=?",
                (text, kind)).fetchone()
            return row["id"] if row else None
        if prefer:
            # every node reachable by this name, best kind first
            rows = self._con.execute(
                "SELECT n.id, n.kind FROM node n WHERE n.id IN "
                "(SELECT node_id FROM alias WHERE alias=?) "
                "UNION SELECT id, kind FROM node WHERE name=? COLLATE NOCASE",
                (key, text)).fetchall()
            if rows:
                best = min(rows, key=lambda r: self._KIND_RANK.get(r["kind"], 50))
                return best["id"]
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

    # ------------------------------------------------------------ traversal

    # Structural relations describe WHO an entity is; trade relations describe
    # what moved. A UI wants them separated, and so does anyone reasoning
    # about the graph: a fellow-subsidiary link and a Rs 7,966 Cr sale are
    # different kinds of claim even when they join the same two nodes.
    STRUCTURAL = ("group_entity_of", "subsidiary_of", "wholly_owned_subsidiary_of",
                  "fellow_subsidiary_of", "subsidiary_of_ultimate_parent",
                  "holding_company_of", "associate_of", "joint_venture_with",
                  "promoter_group_of", "significant_influence_over",
                  "key_management_of", "related_party_of")
    TRADE = ("rpt_sells_to", "rpt_buys_from")
    DISCLOSED = ("consumes", "produces", "sells_to", "operates")

    def trail(self, nid: str, *, hops: int = 2, rels: tuple | None = None,
              limit_per_node: int = 60, max_nodes: int = 400) -> dict:
        """Walk outward from a node and return the subgraph reached.

        Breadth-first with a hard node cap, because a group like Reliance
        reaches thousands of entities within two hops and an unbounded walk
        would return a graph nobody can read or draw. The cap is REPORTED in
        `truncated` rather than applied silently - a partial graph presented
        as complete is worse than a small one.

        Every returned edge keeps its source. That is not decoration: the
        graph mixes an annual-report sentence with an XBRL filing with a
        mutual-fund disclosure, and a reader must be able to tell which is
        which without leaving the view.
        """
        seen = {nid}
        frontier = [nid]
        nodes, edges = {}, []
        root = self.node(nid)
        if root:
            nodes[nid] = {**root, "hop": 0}
        truncated = False
        for hop in range(1, hops + 1):
            nxt = []
            for src in frontier:
                for rel in (rels or (None,)):
                    for n in self.neighbours(src, rel=rel, limit=limit_per_node):
                        edges.append({
                            "src": src if n.direction == "out" else n.id,
                            "dst": n.id if n.direction == "out" else src,
                            "rel": n.rel, "weight": n.weight, "unit": n.unit,
                            "as_of": n.as_of, "source": n.source, "hop": hop,
                        })
                        if n.id in seen:
                            continue
                        if len(nodes) >= max_nodes:
                            truncated = True
                            continue
                        seen.add(n.id)
                        nodes[n.id] = {"id": n.id, "kind": n.kind,
                                       "name": n.name, "hop": hop}
                        nxt.append(n.id)
            frontier = nxt
            if not frontier:
                break
        return {"root": nid, "hops": hops, "nodes": list(nodes.values()),
                "edges": edges, "truncated": truncated,
                "note": (f"walk stopped at {max_nodes} nodes; the graph "
                         "continues beyond this view") if truncated else ""}

    def trade_summary(self, nid: str, *, top: int = 25) -> dict:
        """Counterparties by rupee value, split by direction.

        Aggregated across periods because a counterparty appearing in six
        half-years is one relationship, not six. The period breakdown is kept
        so a trend can be drawn without a second query.
        """
        out: dict = {"sells_to": {}, "buys_from": {}}
        for rel, key in (("rpt_sells_to", "sells_to"), ("rpt_buys_from", "buys_from")):
            for n in self.neighbours(nid, rel=rel, direction="out", limit=4000):
                if not n.weight:
                    continue
                row = out[key].setdefault(n.id, {
                    "id": n.id, "name": n.name, "total": 0.0, "periods": {}})
                row["total"] += n.weight
                row["periods"][n.as_of or "?"] = (
                    row["periods"].get(n.as_of or "?", 0.0) + n.weight)
        for key in out:
            out[key] = sorted(out[key].values(), key=lambda r: -r["total"])[:top]
        return out

    def structure(self, nid: str, *, limit: int = 400) -> list[dict]:
        """Who this entity IS related to, with the relation stated.

        `limit` is PER RELATION, not per call. HDFC Bank files 528 entities
        under one relation alone, so a caller passing a total-looking number
        gets a surprise either way - hence structure_counts(), which is what
        the UI must display.
        """
        rows = []
        for rel in self.STRUCTURAL:
            for n in self.neighbours(nid, rel=rel, limit=limit):
                rows.append({"id": n.id, "name": n.name, "rel": rel,
                             "direction": n.direction, "source": n.source})
        return rows

    def structure_counts(self, nid: str) -> dict[str, int]:
        """TRUE totals per relation, independent of any display cap.

        Counting the rows that came back conflates "this is all of them" with
        "this is as many as we asked for". The header would then shrink to the
        cap and read as complete - a silent truncation wearing a total's
        clothing. These come from COUNT(*), so a capped list can be reported
        as capped.
        """
        marks = ",".join("?" * len(self.STRUCTURAL))
        sql = (f"SELECT rel, COUNT(*) n FROM edge "
               f"WHERE (src = ? OR dst = ?) AND rel IN ({marks}) GROUP BY rel")
        return {r["rel"]: r["n"] for r in
                self._con.execute(sql, (nid, nid, *self.STRUCTURAL))}

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
