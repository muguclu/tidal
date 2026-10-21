"""Write JSON Schemas for the API and message models: ``python -m tidal.contracts.export [DIR]``.

Output is deterministic (sorted keys, 2-space indent, LF, trailing newline) so the committed files
in ``docs/contracts/schemas/`` can be compared with a fresh export in the contract tests.
"""

import json
import sys
from pathlib import Path
from typing import Final

from pydantic.json_schema import models_json_schema

from tidal.contracts.messages import MESSAGE_MODELS
from tidal.contracts.models import API_MODELS

DEFAULT_OUT_DIR: Final = Path("docs/contracts/schemas")


def _dump(schema: object) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render() -> dict[str, str]:
    """Return ``{file name: JSON text}`` for every exported schema."""
    _, api = models_json_schema(
        [(model, "validation") for model in API_MODELS], title="Tidal API v1 components"
    )
    files = {"api.v1.json": _dump(api)}
    for name, model in MESSAGE_MODELS.items():
        files[f"{name.replace('/', '.')}.json"] = _dump(model.model_json_schema(by_alias=True))
    return files


def write(out_dir: Path = DEFAULT_OUT_DIR) -> list[Path]:
    """Write the rendered schemas into ``out_dir`` and return the written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in render().items():
        path = out_dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> None:
    """CLI entry point; optional first argument overrides the output directory."""
    args = sys.argv[1:] if argv is None else argv
    write(Path(args[0]) if args else DEFAULT_OUT_DIR)


if __name__ == "__main__":
    main()
