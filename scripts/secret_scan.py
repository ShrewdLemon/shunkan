"""Refuse a push that carries a real credential.

Compares FULL secret values (from ~/.shunkan/credentials.json and *_API_KEY /
*_TOKEN / *_SECRET env vars) against every tracked file and the whole git
history. Prints the secret's name on a hit, never its value. Values shorter
than 12 characters are refused as scan keys: a slice of a short secret
matches everything.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

MIN_LEN = 12


def secrets() -> dict[str, str]:
    found: dict[str, str] = {}
    creds = Path.home() / ".shunkan" / "credentials.json"
    if creds.exists():
        def walk(x, path):
            if isinstance(x, dict):
                for k, v in x.items():
                    walk(v, f"{path}.{k}")
            elif isinstance(x, list):
                for i, v in enumerate(x):
                    walk(v, f"{path}[{i}]")
            elif isinstance(x, str) and len(x) >= MIN_LEN:
                found[path] = x
        walk(json.loads(creds.read_text()), "credentials")
    for k, v in os.environ.items():
        if k.endswith(("_API_KEY", "_TOKEN", "_SECRET")) and len(v) >= MIN_LEN:
            found[f"env.{k}"] = v
    return found


def corpus() -> str:
    log = subprocess.run(["git", "log", "-p", "--all", "--no-color", "--text"], capture_output=True, check=True)
    files = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout.split(b"\0")
    parts = [log.stdout.decode("utf-8", "ignore")]
    for f in files:
        if f and Path(f.decode()).is_file():
            parts.append(Path(f.decode()).read_bytes().decode("utf-8", "ignore"))
    return "\n".join(parts)


def main() -> int:
    keys = secrets()
    if not keys and os.environ.get("SHUNKAN_SCAN_ALLOW_EMPTY") != "1":
        print("no scan keys loaded")
        return 1
    text = corpus()
    hits = sorted(name for name, value in keys.items() if value in text)
    print(f"secret scan: {len(keys)} values checked, {len(hits)} found")
    for name in hits:
        print(f"  FOUND {name}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
