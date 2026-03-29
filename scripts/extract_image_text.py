#!/usr/bin/env python3
"""Extract text from PNG/JPEG images via Tesseract OCR and write CSV or Excel."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


def _collect_images(root: Path, recursive: bool) -> List[Path]:
    if root.is_file():
        if root.suffix.lower() in IMAGE_SUFFIXES:
            return [root.resolve()]
        return []

    if not root.is_dir():
        return []

    paths: List[Path] = []
    if recursive:
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES:
                paths.append(p.resolve())
    else:
        for p in root.iterdir():
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES:
                paths.append(p.resolve())
    paths.sort(key=lambda x: str(x).lower())
    return paths


def _ocr_image(path: Path, lang: str) -> Tuple[str, Optional[str]]:
    try:
        from PIL import Image
        import pytesseract
    except ImportError as exc:
        raise SystemExit(
            "Missing dependencies. Install with:\n"
            "  pip install pytesseract Pillow openpyxl\n"
            "Also install the Tesseract OCR engine (e.g. apt install tesseract-ocr)."
        ) from exc

    try:
        with Image.open(path) as img:
            text = pytesseract.image_to_string(img, lang=lang) or ""
        return (text.strip(), None)
    except Exception as exc:  # noqa: BLE001 — surface any PIL/Tesseract failure per file
        return ("", str(exc))


def _write_csv(
    rows: Iterable[Tuple[str, str, str]],
    out_path: Path,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file_name", "extracted_text"])
        for file_name, text, _err in rows:
            w.writerow([file_name, text])


def _write_xlsx(
    rows: Iterable[Tuple[str, str, str]],
    out_path: Path,
) -> None:
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise SystemExit(
            "Excel output requires openpyxl. Install with: pip install openpyxl"
        ) from exc

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "ocr"
    ws.append(["file_name", "extracted_text"])
    for file_name, text, _err in rows:
        ws.append([file_name, text])
    wb.save(out_path)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract text from PNG/JPEG images using Tesseract OCR."
    )
    parser.add_argument(
        "path",
        type=Path,
        help="Image file or directory containing images",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output file (.csv or .xlsx). Default: ocr_results.csv in the target directory",
    )
    parser.add_argument(
        "--format",
        choices=("csv", "excel"),
        default="csv",
        help="Output format (default: csv). excel writes .xlsx",
    )
    parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="When path is a directory, include images in subfolders",
    )
    parser.add_argument(
        "--lang",
        default="eng",
        help="Tesseract language code (default: eng). Example: eng+deu",
    )
    parser.add_argument(
        "--include-errors",
        action="store_true",
        help="Add an error column when a file fails to process",
    )
    args = parser.parse_args(argv)

    target = args.path.expanduser().resolve()
    images = _collect_images(target, args.recursive)
    if not images:
        print(f"No PNG/JPEG images found under {target}", file=sys.stderr)
        return 1

    if args.output is not None:
        out_path = args.output.expanduser().resolve()
    else:
        base_dir = target if target.is_dir() else target.parent
        ext = ".xlsx" if args.format == "excel" else ".csv"
        out_path = (base_dir / f"ocr_results{ext}").resolve()

    if args.format == "excel" and out_path.suffix.lower() != ".xlsx":
        out_path = out_path.with_suffix(".xlsx")
    if args.format == "csv" and out_path.suffix.lower() not in (".csv", ".tsv"):
        out_path = out_path.with_suffix(".csv")

    rows: List[Tuple[str, str, str]] = []
    for img_path in images:
        text, err = _ocr_image(img_path, args.lang)
        rows.append((img_path.name, text, err))

    if args.include_errors:
        if args.format == "csv":
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["file_name", "extracted_text", "error"])
                for file_name, text, err in rows:
                    w.writerow([file_name, text, err or ""])
        else:
            try:
                from openpyxl import Workbook
            except ImportError as exc:
                raise SystemExit(
                    "Excel output requires openpyxl. Install with: pip install openpyxl"
                ) from exc
            out_path.parent.mkdir(parents=True, exist_ok=True)
            wb = Workbook()
            ws = wb.active
            ws.title = "ocr"
            ws.append(["file_name", "extracted_text", "error"])
            for file_name, text, err in rows:
                ws.append([file_name, text, err or ""])
            wb.save(out_path)
    else:
        if args.format == "csv":
            _write_csv(rows, out_path)
        else:
            _write_xlsx(rows, out_path)

    print(f"Wrote {len(rows)} row(s) to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
