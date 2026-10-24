#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyarrow"]
# ///
"""Parquet de Hugging Face -> estructura D-Fire (`<split>/images/*.jpg`, `<split>/labels/*.txt`).

Asi `dfire.indexar(salida)` lee Pyro-SDIS y Sage smokedataset sin cambios. Convencion
YOLO de D-Fire: 0 = humo, 1 = fuego; `.txt` vacio = normal.

- Pyro-SDIS: columna `annotations` YOLO, clase 0 = humo (v2). Se copia tal cual.
- Sage: columna `label` (0 cloud, 1 other, 2 smoke) sin cajas. Humo -> una caja de
  imagen completa (`0 0.5 0.5 1 1`), el resto -> vacio.

El split sale del nombre del parquet (`train-0000…`, `val-…`, `validation.parquet`,
`test.parquet`). Las imagenes ya son JPEG: se escriben los bytes sin recomprimir.

Uso:
    uv run modulos/parquet_a_yolo.py data/datasets/pyronear/pyro-sdis/data data/datasets/pyronear/pyro-sdis/yolo
    uv run modulos/parquet_a_yolo.py data/datasets/sagecontinuum/smokedataset/data data/datasets/sagecontinuum/smokedataset/yolo --humo 2
    uv run modulos/parquet_a_yolo.py --self-check
"""

import argparse
import tempfile
from pathlib import Path

CAJA_COMPLETA = "0 0.5 0.5 1 1\n"
SPLIT = {"train": "train", "val": "val", "validation": "val", "test": "test"}


def split_de(parquet):
    """Split D-Fire a partir del nombre del archivo: `train-00000-of-00003` -> train."""
    return SPLIT[Path(parquet).stem.split("-")[0]]


def etiqueta(fila, humo):
    """Texto YOLO de una fila: las cajas de Pyro, o una caja completa si `label == humo`."""
    if "annotations" in fila:
        return fila["annotations"].strip() + "\n" if fila["annotations"].strip() else ""
    return CAJA_COMPLETA if fila["label"] == humo else ""


def convertir(dir_parquet, salida, humo=None):
    """Escribe imagenes y etiquetas de todos los parquet de `dir_parquet`. Devuelve
    {split: (imagenes, con_cajas)}."""
    import pyarrow.parquet as pq
    cuenta = {}
    for pq_file in sorted(Path(dir_parquet).glob("*.parquet")):
        split = split_de(pq_file)
        img_dir, lbl_dir = Path(salida) / split / "images", Path(salida) / split / "labels"
        img_dir.mkdir(parents=True, exist_ok=True)
        lbl_dir.mkdir(parents=True, exist_ok=True)
        n, pos = cuenta.get(split, (0, 0))
        # por lotes: el parquet entero de Pyro (~450 MB de JPEG) no hace falta en RAM
        for lote in pq.ParquetFile(pq_file).iter_batches(batch_size=256):
            for fila in lote.to_pylist():
                # prefijo con el contador: Sage repite nombres entre copias aumentadas
                nombre = f"{n:05d}_{Path(fila.get('image_name') or fila['image']['path']).stem}"
                (img_dir / f"{nombre}.jpg").write_bytes(fila["image"]["bytes"])
                txt = etiqueta(fila, humo)
                (lbl_dir / f"{nombre}.txt").write_text(txt)
                n, pos = n + 1, pos + bool(txt)
        cuenta[split] = (n, pos)
    return cuenta


def self_check():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:   # el .venv del proyecto no trae pyarrow: tests.sh no debe caerse por eso
        print("self-check salteado: falta pyarrow (correr con `uv run modulos/parquet_a_yolo.py --self-check`)")
        return
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        jpg = b"\xff\xd8\xff\xe0fake"
        img = lambda p: {"bytes": jpg, "path": p}
        # Pyro: cajas YOLO tal cual; sin cajas -> vacio
        pq.write_table(pa.Table.from_pylist([
            {"image": img(None), "annotations": "0 .5 .5 .1 .1", "image_name": "a.jpg"},
            {"image": img(None), "annotations": "", "image_name": "b.jpg"}]), d / "train-00000-of-00001.parquet")
        # Sage: solo `label`; validation -> val
        pq.write_table(pa.Table.from_pylist([
            {"image": img("x.jpg"), "label": 2}, {"image": img("x.jpg"), "label": 0}]), d / "validation.parquet")
        cuenta = convertir(d, d / "out", humo=2)
        assert cuenta == {"train": (2, 1), "val": (2, 1)}, cuenta
        assert (d / "out/train/labels/00000_a.txt").read_text() == "0 .5 .5 .1 .1\n"
        assert (d / "out/train/labels/00001_b.txt").read_text() == ""
        assert (d / "out/val/labels/00000_x.txt").read_text() == CAJA_COMPLETA
        assert (d / "out/val/labels/00001_x.txt").read_text() == ""   # nombre repetido no pisa
        assert (d / "out/val/images/00000_x.jpg").read_bytes() == jpg
        assert split_de("test.parquet") == "test"
    print("self-check OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dir_parquet", nargs="?")
    ap.add_argument("salida", nargs="?")
    ap.add_argument("--humo", type=int, help="valor de `label` que es humo (Sage: 2)")
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        self_check()
    else:
        for split, (n, pos) in convertir(a.dir_parquet, a.salida, a.humo).items():
            print(f"{split}: {n} imagenes, {pos} con humo/fuego")
