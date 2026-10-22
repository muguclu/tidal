"""Write the app's generated OpenAPI document as JSON: ``export_openapi.py <output-path>``.

Used by the CI ``openapi-diff`` job. Builds the app with explicit ``TIDAL_ENV=local`` settings
(the process environment is ignored, so the document is reproducible) and no clients; nothing is
contacted.
"""

import json
import sys
from pathlib import Path

from tidal.api.app import create_app
from tidal.common.config import Settings


def export(path: Path) -> None:
    """Generate the OpenAPI document and write it to ``path`` (UTF-8, sorted keys)."""
    app = create_app(Settings.from_env({"TIDAL_ENV": "local"}))
    path.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    """CLI entry: exactly one argument, the output path."""
    if len(argv) != 2:  # noqa: PLR2004 - program name + path
        sys.stderr.write("usage: export_openapi.py <output-path>\n")
        return 2
    export(Path(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
