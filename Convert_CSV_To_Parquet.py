from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

import R_DataSet as dataset_core


COMPRESSION = os.getenv("R_DATASET_PARQUET_COMPRESSION", "zstd")


def convert_one(csv_path: Path) -> tuple[Path, int]:
    parquet_path = csv_path.with_suffix(".parquet")
    temp_path = parquet_path.with_suffix(".parquet.tmp")

    frame = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig")
    for column in frame.columns:
        frame[column] = frame[column].astype("string")
    frame.to_parquet(
        temp_path,
        index=False,
        engine="pyarrow",
        compression=COMPRESSION,
    )

    verified = pd.read_parquet(temp_path, engine="pyarrow")
    if len(verified) != len(frame) or list(verified.columns) != list(frame.columns):
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Parquet verification failed: {csv_path}")

    temp_path.replace(parquet_path)
    return parquet_path, len(frame)


def main() -> None:
    dataset_core.download_google_dataset()
    output_dir = dataset_core.OUTPUT_DIR
    csv_files = sorted(output_dir.rglob("*.csv"))

    if not csv_files:
        print(f"No CSV files found under {output_dir}", flush=True)
        return

    converted = []
    for csv_path in csv_files:
        parquet_path, rows = convert_one(csv_path)
        converted.append(parquet_path)
        print(
            f"converted: {csv_path.relative_to(output_dir)} -> "
            f"{parquet_path.relative_to(output_dir)}, rows={rows}",
            flush=True,
        )

    dataset_core.upload_google_dataset()
    print(
        f"CSV to Parquet migration completed: files={len(converted)}, "
        f"compression={COMPRESSION}",
        flush=True,
    )


if __name__ == "__main__":
    main()
