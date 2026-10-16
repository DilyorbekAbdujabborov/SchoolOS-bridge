"""Sign the built bridge binaries and write the update manifest.

    BRIDGE_SIGNING_KEY=<base64 ed25519 seed> python installer/release_manifest.py \
        --version 0.2.0 --base-url https://github.com/o/r/releases/download/bridge-v0.2.0 \
        --linux dist/schoolos-bridge-linux --macos dist/schoolos-bridge-macos \
        --windows dist/schoolos-bridge-windows.exe --out manifest.json

The signed message must match schoolos_bridge.updater.signed_message().
"""

import argparse
import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schoolos_bridge.updater import signed_message  # noqa: E402


def build_manifest(key: Ed25519PrivateKey, version: str, base_url: str, files: dict[str, Path]) -> dict:
    platforms = {}
    for platform, path in files.items():
        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        signature = key.sign(signed_message(version, platform, sha256))
        platforms[platform] = {
            "url": f"{base_url.rstrip('/')}/{path.name}",
            "sha256": sha256,
            "signature": base64.b64encode(signature).decode(),
        }
    return {"version": version, "platforms": platforms}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--linux", type=Path)
    parser.add_argument("--macos", type=Path)
    parser.add_argument("--windows", type=Path)
    parser.add_argument("--out", type=Path, default=Path("manifest.json"))
    args = parser.parse_args()
    files = {p: getattr(args, p) for p in ("linux", "macos", "windows") if getattr(args, p)}
    if not files:
        parser.error("give at least one binary")
    seed = base64.b64decode(os.environ["BRIDGE_SIGNING_KEY"])
    manifest = build_manifest(Ed25519PrivateKey.from_private_bytes(seed), args.version, args.base_url, files)
    args.out.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {args.out}: {args.version} for {', '.join(files)}")


if __name__ == "__main__":
    main()
