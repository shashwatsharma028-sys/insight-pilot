"""
Dataset access for the multi-agent prototype: standard library only.

- load(): reads a CSV with the csv module (no pandas), with a size limit.
- profile(): the only view of the data the Planning and Data Analyst agents
  receive. It holds the schema (column names, inferred kinds, missing counts),
  the row count and the file's SHA-256. It never holds raw cell values.

Missing values use pandas' documented default NA tokens, so the independent
recomputation treats missing data the way pandas-generated analysis code does.
"""

import csv
import hashlib
import os
import re
from dataclasses import dataclass

NA_TOKENS = frozenset({
    "", "#N/A", "#N/A N/A", "#NA", "-1.#IND", "-1.#QNAN", "-NaN", "-nan",
    "1.#IND", "1.#QNAN", "<NA>", "N/A", "NA", "NULL", "NaN", "None", "n/a",
    "nan", "null",
})
MAX_DATASET_BYTES = 50 * 1024 * 1024
BOOLEAN_TOKENS = frozenset({"True", "False"})
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")

NUMERIC, BOOLEAN, DATE, TEXT = "numeric", "boolean", "date", "text"


class DatasetError(ValueError):
    pass


def is_na(value: str) -> bool:
    return value in NA_TOKENS


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class Dataset:
    path: str
    sha256: str
    header: tuple
    rows: tuple

    def column(self, name: str) -> list:
        try:
            i = self.header.index(name)
        except ValueError:
            raise DatasetError(f"unknown column {name!r}") from None
        return [r[i] for r in self.rows]


def load(path: str, max_bytes: int = MAX_DATASET_BYTES) -> Dataset:
    if not os.path.isfile(path):
        raise DatasetError("dataset file not found")
    if os.path.getsize(path) > max_bytes:
        raise DatasetError(f"dataset larger than {max_bytes} bytes")
    digest = sha256_file(path)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = tuple(next(reader))
        except StopIteration:
            raise DatasetError("dataset is empty") from None
        rows = tuple(tuple(r) for r in reader)
    if len(set(header)) != len(header):
        raise DatasetError("duplicate column names")
    bad = [i for i, r in enumerate(rows) if len(r) != len(header)]
    if bad:
        raise DatasetError(f"{len(bad)} row(s) have the wrong number of fields")
    return Dataset(path=path, sha256=digest, header=header, rows=rows)


def _is_float(v: str) -> bool:
    try:
        float(v)
        return True
    except ValueError:
        return False


def infer_kind(values) -> str:
    present = [v for v in values if not is_na(v)]
    if not present:
        return TEXT
    if all(v in BOOLEAN_TOKENS for v in present):
        return BOOLEAN
    if all(_is_float(v) for v in present):
        return NUMERIC
    if all(_DATE.match(v) for v in present):
        return DATE
    return TEXT


def profile(ds: Dataset) -> dict:
    """Schema-level summary. Contains no cell values by construction."""
    columns = []
    for name in ds.header:
        values = ds.column(name)
        columns.append({"name": name, "kind": infer_kind(values),
                        "missing": sum(1 for v in values if is_na(v))})
    return {"file_name": os.path.basename(ds.path), "dataset_sha256": ds.sha256,
            "row_count": len(ds.rows), "columns": columns}
