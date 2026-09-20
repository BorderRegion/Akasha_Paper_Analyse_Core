"""Verify shipped files before local configuration/build changes."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
manifest = json.loads((root / "MANIFEST.json").read_text())
failures = []
for name, info in manifest["files"].items():
    path = root / name
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != info["sha256"]:
        failures.append(name)
if failures:
    raise SystemExit("Missing or changed files:\n" + "\n".join(failures))
print(f'Verified {len(manifest["files"])} files (SHA-256).')
