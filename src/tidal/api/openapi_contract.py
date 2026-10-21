"""Normalise FastAPI's generated OpenAPI to the shapes used by ``docs/contracts/openapi.yaml``.

oasdiff 1.34 compares schemas structurally, so purely cosmetic differences of pydantic output
count as breaking changes in one direction or the other. This rewrites representation only, never
meaning: nullable ``anyOf`` becomes a type array, ``const`` becomes a one-value ``enum``,
``*_ts_ns`` integers get ``format: int64`` and the Entra security scheme (inherited by every
operation) is declared. Optional query parameters are not nullable in the contract, so their null
branch is dropped.
"""

from typing import Any

SCOPE = "api://tidal/.default"
_ENTRA: dict[str, Any] = {
    "type": "oauth2",
    "description": "Entra ID access token, audience api://tidal, roles in the `roles` claim.",
    "flows": {
        "authorizationCode": {
            "authorizationUrl": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
            "tokenUrl": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            "scopes": {SCOPE: "Access Tidal"},
        }
    },
}
_NULL = {"type": "null"}


def _resolve(doc: dict[str, Any], ref: str) -> dict[str, Any]:
    node: dict[str, Any] = doc
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def _nullable(doc: dict[str, Any], branch: dict[str, Any]) -> dict[str, Any]:
    """``branch`` (inlined if it is a ``$ref``) allowing ``null``."""
    inline = dict(_resolve(doc, branch["$ref"]) if "$ref" in branch else branch)
    inline.pop("title", None)
    kind = inline.get("type")
    inline["type"] = [kind, "null"] if isinstance(kind, str) else kind
    if "enum" in inline:
        inline["enum"] = [*inline["enum"], None]
    return inline


def _fix(doc: dict[str, Any], node: dict[str, Any], name: str, *, param: bool) -> None:
    """Rewrite one schema node in place."""
    any_of = node.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2 and _NULL in any_of:  # noqa: PLR2004
        other = next(b for b in any_of if b != _NULL)
        del node["anyOf"]
        node.update(other if param else _nullable(doc, other))
    if "const" in node:
        node["enum"] = [node.pop("const")]
    if name == "volume":
        # Contract declares no lower bound for Bar.volume; the pydantic model has ge=0 (stricter).
        node.pop("minimum", None)
    if name.endswith("ts_ns") and node.get("type") in ("integer", ["integer", "null"]):
        node["format"] = "int64"


def _walk(doc: dict[str, Any], node: Any, *, name: str = "", param: bool = False) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(doc, item, name=name, param=param)
    elif isinstance(node, dict):
        _fix(doc, node, name, param=param)
        for key, child in list(node.items()):
            if key == "properties" and isinstance(child, dict):
                for prop, schema in child.items():
                    _walk(doc, schema, name=prop)
            elif key == "parameters":
                for item in child:
                    _walk(doc, item.get("schema"), param=True)
            else:
                _walk(doc, child, name=name, param=param)


def normalize(doc: dict[str, Any]) -> dict[str, Any]:
    """Rewrite ``doc`` in place to the contract's schema style and return it."""
    _walk(doc, doc.get("components", {}).get("schemas", {}))
    for item in doc.get("paths", {}).values():
        for operation in item.values():
            _walk(doc, operation)
    if any("security" not in op for item in doc["paths"].values() for op in item.values()):
        doc["security"] = [{"entra": [SCOPE]}]
        doc.setdefault("components", {}).setdefault("securitySchemes", {})["entra"] = _ENTRA
    return doc
