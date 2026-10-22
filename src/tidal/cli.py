"""Root ``tidal`` command. Sub-command groups live in the modules their tasks own.

This file is created by T-001 so that lanes never have to edit it (PARALLELISM §1.7). Each group is
registered from ``module:attribute`` and silently skipped until its owning task creates the module.
"""

import importlib

import typer

#: group name -> "module:attribute" of a ``typer.Typer`` (owning task in the comment).
SUBCOMMANDS: dict[str, str] = {
    "ingest": "tidal.ingest.cli:app",  # T-105 — backfill, daily
    "synth": "tidal.ingest.cli:synth_app",  # T-105 — synthetic generator
    "db": "tidal.storage.sql_repo:db_app",  # T-106 — migrate
    "tiering": "tidal.storage.tiering_cli:app",  # T-205 — run, retag, compact
    "dlq": "tidal.worker.dlq:app",  # T-303 — inspect, resubmit
}


def _load(target: str) -> typer.Typer | None:
    """Return the Typer app at ``target``, or ``None`` while its module does not exist yet."""
    module_name, _, attribute = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name is not None and module_name.startswith(exc.name):
            return None  # the owning task has not landed yet
        raise  # a real missing dependency inside an existing module
    sub_app = getattr(module, attribute)
    if not isinstance(sub_app, typer.Typer):
        msg = f"{target} is not a typer.Typer"
        raise TypeError(msg)
    return sub_app


def build_app(subcommands: dict[str, str] = SUBCOMMANDS) -> typer.Typer:
    """Build the root CLI from the sub-command groups that are currently available."""
    root = typer.Typer(
        name="tidal",
        no_args_is_help=True,
        help="Tidal platform CLI.",
        # Never print locals in tracebacks: they can hold connection strings or keys.
        pretty_exceptions_show_locals=False,
    )

    @root.callback()
    def _root() -> None:
        """Tidal platform CLI."""

    for name, target in subcommands.items():
        sub_app = _load(target)
        if sub_app is not None:
            root.add_typer(sub_app, name=name)
    return root


app = build_app()
