"""Convert each KEDA ScaledJob in a rendered multi-document stream into a batch/v1 Job.

kube-linter skips unknown CRDs, so the worker pod spec (ScaledJob.spec.jobTargetRef) would never be
linted. This copies every document through unchanged and appends one Job per ScaledJob, built from
its jobTargetRef. Usage: scaledjob_to_job.py rendered.yaml > lint.yaml (T-409, ADR-0008 R-19).
"""

import sys

import yaml


def convert(docs: list[dict]) -> list[dict]:
    """Return the input documents plus one batch/v1 Job per ScaledJob."""
    out = list(docs)
    for doc in docs:
        if doc.get("kind") != "ScaledJob":
            continue
        meta = doc["metadata"]
        out.append(
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": meta["name"],
                    "namespace": meta.get("namespace", "default"),
                    "labels": meta.get("labels", {}),
                },
                "spec": doc["spec"]["jobTargetRef"],
            }
        )
    return out


def main() -> int:
    """CLI entry: read the file in argv[1], write the converted stream to stdout."""
    with open(sys.argv[1], encoding="utf-8") as fh:
        docs = [d for d in yaml.safe_load_all(fh) if d]
    converted = convert(docs)
    if len(converted) == len(docs):
        sys.stderr.write("no ScaledJob found; nothing to lint\n")
        return 1
    yaml.safe_dump_all(converted, sys.stdout, sort_keys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
