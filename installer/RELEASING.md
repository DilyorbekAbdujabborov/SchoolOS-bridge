# Releasing the bridge (auto-update)

Bridges poll `GET /api/devices/update/` about every 6 hours. They install a
build only if its Ed25519 signature verifies against `PUBLIC_KEY` in
`schoolos_bridge/updater.py`, it is strictly newer, and it passes `--self-test`.
The previous binary is kept as `<exe>.bak`.

## Cut a release

1. Bump `__version__` in `schoolos_bridge/__init__.py`, merge to main.
2. `git tag bridge-vX.Y.Z && git push origin bridge-vX.Y.Z`
   (tag must equal `__version__`, CI refuses otherwise).
3. CI builds all OSes, signs with the `BRIDGE_SIGNING_KEY` secret and creates a
   GitHub Release with the binaries and `manifest.json`.
4. Put `manifest.json` on the server and point `BRIDGE_UPDATE_MANIFEST` at it
   (env, absolute path), then restart gunicorn. Bridges update on their next check.

## Notes

- Release asset URLs must be downloadable without auth: the repo (or at least
  the releases) must be public, otherwise host the binaries elsewhere over https.
- Private key: `~/.schoolos-keys/bridge-signing.key` (backup it; losing it
  means shipping a new `PUBLIC_KEY` by hand to every bridge). Never commit it.
- Rollback: publish a manifest with a *higher* version containing the old build.
  Downgrades are rejected by design.
