import ast
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "shunkan"
MOVED = {"shunkan.data.funds", "shunkan.data.msci", "shunkan.data.ingest", "shunkan.data.filings",
         "shunkan.data.supply_chain", "shunkan.data.llm", "shunkan.data.manual",
         "shunkan.data.indas", "shunkan.data.bse", "shunkan.store.graph"}


def _private_words():
    words = ["shunkan_desk", "projects/"]
    path = os.environ.get("SHUNKAN_PRIVATE_WORDS")
    if path:
        for line in Path(path).read_text().splitlines():
            line = line.strip().lower()
            if line and not line.startswith("#"):
                words.append(line)
    return tuple(words)


PRIVATE = _private_words()


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
            for a in node.names:
                yield f"{node.module}.{a.name}"


def _py():
    return sorted(SRC.rglob("*.py"))


def test_public_never_imports_desk_or_moved_modules():
    bad = []
    for p in _py():
        for name in _imports(ast.parse(p.read_text())):
            if name.split(".")[0] == "shunkan_desk" or name in MOVED or any(name.startswith(m + ".") for m in MOVED):
                bad.append(f"{p.relative_to(ROOT)}: {name}")
    assert not bad, bad


def test_no_private_names_in_code_or_ui():
    bad = []
    for p in _py():
        for node in ast.walk(ast.parse(p.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                low = node.value.lower()
                bad += [f"{p.relative_to(ROOT)}:{node.lineno}: {w}" for w in PRIVATE if w in low]
    for p in (SRC / "server" / "static").glob("*.*"):
        if p.suffix in (".js", ".html", ".css"):
            low = p.read_text().lower()
            bad += [f"{p.relative_to(ROOT)}: {w}" for w in PRIVATE if w in low]
    assert not bad, bad


def test_moved_modules_are_gone():
    left = [m for m in MOVED if (ROOT / "src" / (m.replace(".", "/") + ".py")).exists()]
    assert not left, left
