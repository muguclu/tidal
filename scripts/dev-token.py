"""Mint a local Entra-like dev token for a running API with ``TIDAL_ENV=local`` (T-113).

Keys are cached under ``./.data/dev-jwks/`` (git-ignored); point a local API at ``jwks.json``.
Private keys and tokens are never logged; only the token is written to stdout.
"""

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
    QUERY_ROLE,
    TEST_TENANT_ID,
    KeyPair,
    issuer_for,
    mint_token,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_KEYS_DIR = REPO_ROOT / ".data" / "dev-jwks"
KID = "dev-key-1"
ROLES = ("Tidal.Reader", "Tidal.Trader", "Tidal.Quant", "Tidal.Admin")

app = typer.Typer(add_completion=False, help=__doc__)


def _load_or_create_key(keys_dir: Path) -> KeyPair:
    keys_dir.mkdir(parents=True, exist_ok=True)
    pem_path = keys_dir / "private.pem"
    if pem_path.exists():
        return KeyPair.from_pem(KID, pem_path.read_bytes())
    key = KeyPair.generate(KID)
    # O_EXCL + mode 0o600 at creation: never world-readable, even briefly.
    fd = os.open(pem_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key.private_pem())
    return key


@app.command()
def main(  # noqa: PLR0917 - typer options
    role: Annotated[list[str] | None, typer.Option("--role", help="App role; repeatable.")] = None,
    ttl: Annotated[int, typer.Option(help="Token lifetime in seconds.")] = 3600,
    print_jwks: Annotated[
        Path | None, typer.Option("--print-jwks", help="Also write the public JWKS to PATH.")
    ] = None,
    keys_dir: Annotated[Path, typer.Option(help="Key cache directory.")] = DEFAULT_KEYS_DIR,
    audience: Annotated[
        str, typer.Option(help="aud claim (use api://tidal-query with --app).")
    ] = DEFAULT_AUDIENCE,
    tenant_id: Annotated[str, typer.Option(help="Tenant id for the iss claim.")] = TEST_TENANT_ID,
    oid: Annotated[str, typer.Option(help="oid claim.")] = DEFAULT_OID,
    app_token: Annotated[
        bool,
        typer.Option(
            "--app",
            help="App token for a local tidal-query: idtyp=app, no scp, azp=id-api stand-in.",
        ),
    ] = False,
    azp: Annotated[
        str, typer.Option(help="azp claim (default: dev CLI, or the id-api stand-in with --app).")
    ] = "",
) -> None:
    """Print a signed JWT: a user token (``--role`` app roles) or, with ``--app``, an app token."""
    env = os.environ.get("TIDAL_ENV")
    if env != "local":  # fail closed: unset is refused too
        typer.echo("refusing to mint dev tokens: TIDAL_ENV is not 'local'", err=True)
        raise typer.Exit(2)
    roles = role or []
    if not roles and not app_token:
        typer.echo("at least one --role is required (or use --app)", err=True)
        raise typer.Exit(2)
    unknown = [r for r in roles if r not in ROLES and not (app_token and r == QUERY_ROLE)]
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
        roles=roles,
        aud=audience,
        iss=issuer_for(tenant_id),
        oid=oid,
        exp_offset=ttl,
        kind="app" if app_token else "user",
        azp=azp or None,
    )
    typer.echo(token)


if __name__ == "__main__":
    app()
