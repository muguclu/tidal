"""Mint a local Entra-like dev token for a running API with ``TIDAL_ENV=local`` (T-113).

Keys are cached under ``./.data/dev-jwks/`` (git-ignored); point a local API at ``jwks.json``.
Private keys and tokens are never logged; only the token is written to stdout.
"""

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Annotated

import typer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from fakes.jwks.core import (
    DEFAULT_AUDIENCE,
    DEFAULT_OID,
    TEST_TENANT_ID,
    KeyPair,
    issuer_for,
    mint_token,
)

DEFAULT_KEYS_DIR = Path(".data") / "dev-jwks"
KID = "dev-key-1"
ROLES = ("Tidal.Reader", "Tidal.Trader", "Tidal.Quant", "Tidal.Admin")

app = typer.Typer(add_completion=False, help=__doc__)


def _load_or_create_key(keys_dir: Path) -> KeyPair:
    keys_dir.mkdir(parents=True, exist_ok=True)
    pem_path = keys_dir / "private.pem"
    if pem_path.exists():
        return KeyPair.from_pem(KID, pem_path.read_bytes())
    key = KeyPair.generate(KID)
    pem_path.write_bytes(key.private_pem())
    with contextlib.suppress(OSError):  # best effort on filesystems without POSIX modes
        pem_path.chmod(0o600)
    return key


@app.command()
def main(  # noqa: PLR0917 - typer options
    role: Annotated[list[str], typer.Option("--role", help="App role; repeatable.")],
    ttl: Annotated[int, typer.Option(help="Token lifetime in seconds.")] = 3600,
    print_jwks: Annotated[
        Path | None, typer.Option("--print-jwks", help="Also write the public JWKS to PATH.")
    ] = None,
    keys_dir: Annotated[Path, typer.Option(help="Key cache directory.")] = DEFAULT_KEYS_DIR,
    audience: Annotated[str, typer.Option(help="aud claim.")] = DEFAULT_AUDIENCE,
    tenant_id: Annotated[str, typer.Option(help="Tenant id for the iss claim.")] = TEST_TENANT_ID,
    oid: Annotated[str, typer.Option(help="oid claim.")] = DEFAULT_OID,
) -> None:
    """Print a signed JWT carrying ``--role`` app roles."""
    env = os.environ.get("TIDAL_ENV")
    if env is not None and env != "local":
        typer.echo("refusing to mint dev tokens: TIDAL_ENV is not 'local'", err=True)
        raise typer.Exit(2)
    unknown = [r for r in role if r not in ROLES]
    if unknown:
        typer.echo(f"unknown role(s): {', '.join(unknown)}; expected one of {ROLES}", err=True)
        raise typer.Exit(2)
    key = _load_or_create_key(keys_dir)
    jwks_json = json.dumps({"keys": [key.public_jwk()]})
    (keys_dir / "jwks.json").write_text(jwks_json, encoding="utf-8")
    if print_jwks is not None:
        print_jwks.parent.mkdir(parents=True, exist_ok=True)
        print_jwks.write_text(jwks_json, encoding="utf-8")
    token = mint_token(
        key,
        roles=role,
        aud=audience,
        iss=issuer_for(tenant_id),
        oid=oid,
        exp_offset=ttl,
    )
    typer.echo(token)


if __name__ == "__main__":
    app()
