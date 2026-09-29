"""Parquet helpers: a streaming appender and a reader that skips half-written parts."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)


def coerce_table(table: pa.Table, target: pa.Schema) -> pa.Table:
    """Cast ``table`` to ``target`` column by column.

    All-null columns (inferred as ``pa.null()``) become typed nulls, missing
    columns are added as nulls, and a column that cannot be cast is nulled
    rather than failing the whole batch.
    """
    arrays = []
    for f in target:
        if f.name not in table.schema.names:
            arrays.append(pa.nulls(len(table), type=f.type))
            continue
        col = table.column(f.name)
        if col.type == pa.null():
            arrays.append(pa.nulls(len(table), type=f.type))
        elif col.type != f.type:
            try:
                arrays.append(col.cast(f.type, safe=False))
            except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as exc:
                log.warning("column %s: cannot cast %s -> %s (%s); nulled", f.name, col.type, f.type, exc)
                arrays.append(pa.nulls(len(table), type=f.type))
        else:
            arrays.append(col)
    return pa.table({f.name: a for f, a in zip(target, arrays, strict=True)}, schema=target)


class ParquetAppender:
    """Keeps one ``pq.ParquetWriter`` open across many ``write()`` calls.

    The file only gets a valid footer on ``close()``. A crash mid-write leaves
    a truncated part, which :func:`valid_parts` skips.
    """

    def __init__(self, path: str | Path, schema: pa.Schema):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.schema = schema
        self._writer: pq.ParquetWriter | None = None
        self.rows = 0

    def write(self, records: list[dict[str, Any]]) -> int:
        if not records:
            return 0
        table = coerce_table(pa.Table.from_pylist(records), self.schema)
        if self._writer is None:
            self._writer = pq.ParquetWriter(self.path, self.schema, compression="snappy")
        self._writer.write_table(table)
        self.rows += len(records)
        return len(records)

    def close(self) -> int:
        if self._writer:
            self._writer.close()
            self._writer = None
        return self.rows

    def __enter__(self) -> ParquetAppender:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def valid_parts(table_dir: Path, quiet: bool = False) -> list[str]:
    """Readable ``part_*.parquet`` files under ``table_dir``.

    Only footers are read, so this is cheap even on a multi-GB table. Parts the
    collector is still writing (or that a crash truncated) are skipped and
    reported, never silently.
    """
    if not table_dir.is_dir():
        raise FileNotFoundError(f"no table directory at {table_dir}; has this marketplace been collected?")
    good: list[str] = []
    bad: list[str] = []
    for p in sorted(table_dir.glob("part_*.parquet")):
        try:
            pq.read_metadata(p)
            good.append(p.as_posix())
        except Exception:  # truncated footer, mid-write, corrupt
            bad.append(p.name)
    if bad and not quiet:
        log.warning("%s: skipping %d unreadable part(s): %s", table_dir, len(bad), ", ".join(bad))
    if not good:
        raise FileNotFoundError(f"no readable parquet parts under {table_dir}")
    return good
