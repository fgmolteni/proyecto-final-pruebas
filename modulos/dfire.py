#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pillow"]
# ///
"""Capa de datos del banco D-Fire, compartida por el entrenamiento y la cuantizacion.

La usan `4.red_entrenada.ipynb` (entrena la CNN) y `5_cuantizacion.ipynb` (la lleva a
int8 para TFLite Micro). Por que un modulo y no celdas copiadas: la cuantizacion calibra
los rangos int8 con imagenes preprocesadas y despues mide el modelo con imagenes
preprocesadas. Si ese preprocesamiento no es EXACTAMENTE el del entrenamiento
(antialias, redondeo, tamano), los rangos salen de otra distribucion y la exactitud
int8 cae sin que nada falle. Una sola definicion, importada por los dos, lo evita.

Etiquetas: dos binarias independientes por imagen, [fuego, humo], derivadas de las
cajas YOLO (0 = humo, 1 = fuego). No son excluyentes: ~80 % de las imagenes con fuego
tambien tienen humo.

Etiquetas por celda (`6_red_grilla.ipynb`): las cajas se pintan en una mascara de
IMG_SIZE x 2 y se reducen a una grilla n x n; la celda vale 1 si tiene al menos un pixel
de caja, y la etiqueta de imagen es el maximo de la grilla.

TensorFlow se importa solo dentro de las funciones que arman tensores: el resto del
modulo (y el self-check) corre sin TF.

Uso:
    ./dfire.py --self-check
"""

import argparse
import functools
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

CLASES = ["fuego", "humo"]                          # etiquetas binarias de las cajas YOLO
COMBOS = ["nada", "humo", "fuego", "fuego+humo"]    # indice = 2*fuego + humo
# Salida softmax de la red (notebooks 4 y 5): toda escena con llama es `incendio`, tenga
# o no humo. Asi `fuego` (5 %) y `fuego+humo` (22 %) suman sus imagenes en una clase.
CATEGORIAS = ["normal", "humo", "incendio"]
IMG_SIZE = (96, 96)        # framesize nativo del OV2640: el firmware no reescala nada
SPLITS = ("train", "val", "test")
YOLO_A_CANAL = {"1": 0, "0": 1}   # clase YOLO -> indice en CLASES (1 = fuego, 0 = humo)


# --- Etiquetas y particion ---------------------------------------------------
def etiqueta_imagen(ruta_txt):
    """[fuego, humo] desde una anotacion YOLO: 1 si hay al menos una caja de esa clase."""
    clases = {l.split()[0] for l in Path(ruta_txt).read_text().splitlines() if l.split()}
    return [int("1" in clases), int("0" in clases)]


def combo(y):
    """Indice en COMBOS de una o varias etiquetas [fuego, humo]: 2*fuego + humo."""
    y = np.asarray(y)
    return (2 * y[..., 0] + y[..., 1]).astype(int)


def categoria(y):
    """Indice en CATEGORIAS de etiquetas [fuego, humo]: incendio si hay fuego, si no humo
    o normal. Es el combo con `fuego` y `fuego+humo` juntos."""
    return np.minimum(combo(y), 2)


def indexar(data_dir):
    """Rutas, etiquetas (N, 2) uint8 y split OFICIAL de cada imagen del banco.

    Se conserva el split oficial: D-Fire son cuadros de video y rearmar la particion
    al azar manda cuadros hermanos del mismo incendio a train y a test.
    """
    archivos, etiquetas, splits = [], [], []
    for split in SPLITS:
        base = Path(data_dir) / split
        for img in sorted((base / "images").glob("*.jpg")):
            archivos.append(str(img))
            etiquetas.append(etiqueta_imagen(base / "labels" / (img.stem + ".txt")))
            splits.append(split)
    return np.array(archivos), np.array(etiquetas, dtype=np.uint8), np.array(splits)


def particion(splits, seed):
    """{split: indices}, desordenados solo DENTRO de cada split (ningun lote queda con
    una sola clase). Misma semilla -> mismos indices en los dos notebooks."""
    rng = np.random.default_rng(seed)
    return {s: rng.permutation(np.flatnonzero(splits == s)) for s in SPLITS}


# --- Imagenes (TensorFlow) ---------------------------------------------------
def leer_imagen(ruta, tam=IMG_SIZE):
    """JPEG -> tensor `tam` x 3 uint8: bilineal con antialias y redondeo.

    `tam` default IMG_SIZE (96x96, lo que usan los notebooks 4 y 5): el notebook 6
    pasa (128, 128) explicito. Sin antialias, reducir fotos de hasta 1920x1080
    crea aliasing y los penachos finos de humo se pierden.
    """
    import tensorflow as tf
    img = tf.image.resize(tf.io.decode_jpeg(tf.io.read_file(ruta), channels=3), tam,
                          antialias=True)
    return tf.cast(tf.round(img), tf.uint8)


def armar_ds(archivos, etiquetas, batch, mezclar=False, seed=0, tam=IMG_SIZE):
    """tf.data de (imagenes float32 0-255, etiquetas float32), en lotes.

    uint8 en el cache y float32 recien en el lote: ~415 MB de RAM en vez de ~1,66 GB.
    La escala a [-1, 1] esta adentro del modelo (`Rescaling`). Sin mezclar, los lotes
    salen en el mismo orden que `archivos`.
    """
    import tensorflow as tf
    ds = tf.data.Dataset.from_tensor_slices((archivos, etiquetas))
    ds = ds.map(lambda r, y: (leer_imagen(r, tam), y), num_parallel_calls=tf.data.AUTOTUNE)
    # cache() sobre imagenes sueltas, no lotes: quedan decodificadas en RAM y shuffle
    # arma lotes distintos en cada epoca.
    ds = ds.cache()
    if mezclar:
        ds = ds.shuffle(1000, seed=seed, reshuffle_each_iteration=True)
    ds = ds.batch(batch).map(lambda x, y: (tf.cast(x, tf.float32), tf.cast(y, tf.float32)),
                             num_parallel_calls=tf.data.AUTOTUNE)
    return ds.prefetch(tf.data.AUTOTUNE)


# --- Cajas y grilla ----------------------------------------------------------
def cajas_imagen(ruta_txt):
    """Cajas de una anotacion YOLO como filas [canal, x0, y0, x1, y1], normalizadas a
    [0, 1]. `canal` es el indice en CLASES (YOLO 1 = fuego -> 0, YOLO 0 = humo -> 1)."""
    filas = []
    for linea in Path(ruta_txt).read_text().splitlines():
        p = linea.split()
        if p and p[0] in YOLO_A_CANAL:
            cx, cy, w, h = map(float, p[1:5])
            filas.append([YOLO_A_CANAL[p[0]], cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2])
    return filas


def tabla_cajas(archivos):
    """(N, K, 5) float32 con las cajas de cada imagen (ver `cajas_imagen`), K = el maximo
    de cajas por imagen. El relleno lleva canal -1, que no enciende ningun pixel: la
    tabla tiene que ser rectangular para entrar en un tf.data."""
    cajas = [cajas_imagen(Path(a).parent.parent / "labels" / (Path(a).stem + ".txt"))
             for a in archivos]
    tabla = np.full((len(cajas), max([1, *map(len, cajas)]), 5), -1, dtype=np.float32)
    for i, c in enumerate(cajas):
        if c:
            tabla[i, :len(c)] = c
    return tabla


def mascara_cajas(cajas, tam=IMG_SIZE):
    """Cajas de UNA imagen (K, 5) -> mascara `tam` x 2 float32 [fuego, humo].

    `tam` default IMG_SIZE (lo que usan los notebooks 4 y 5); el notebook 6 pasa
    (128, 128) explicito.

    Un pixel vale 1 si cae dentro de alguna caja de ese canal. Los bordes se redondean al
    pixel mas cercano y toda caja ocupa al menos un pixel: un fuego de medio pixel a
    96x96 sigue estando en la mascara, igual que en `etiqueta_imagen`.
    """
    import tensorflow as tf
    canal, x0, y0, x1, y1 = tf.unstack(tf.convert_to_tensor(cajas, tf.float32), axis=-1)

    def dentro(ini, fin, lado):
        # (K, lado): True en los pixeles [ini, fin) de cada caja
        ini = tf.clip_by_value(tf.round(ini * lado), 0.0, lado - 1.0)
        fin = tf.maximum(tf.round(fin * lado), ini + 1.0)
        px = tf.range(lado, dtype=tf.float32)
        return (px >= ini[:, None]) & (px < fin[:, None])

    alto, ancho = tam
    en_caja = dentro(y0, y1, alto)[:, :, None] & dentro(x0, x1, ancho)[:, None, :]  # (K, H, W)
    canales = [tf.reduce_any(en_caja & tf.equal(canal, float(c))[:, None, None], axis=0)
               for c in range(len(CLASES))]
    return tf.cast(tf.stack(canales, axis=-1), tf.float32)


def objetivos(mascaras, n, tam=IMG_SIZE):
    """Lote de mascaras (B, H, W, 2) -> {"salida": (B, 2), "grilla": (B, n, n, 2)} float32.

    La celda vale 1 si tiene al menos un pixel de caja de esa clase (max pooling de
    (H/n)x(W/n)). La etiqueta de imagen es el maximo de la grilla: las dos no se
    contradicen nunca, tampoco despues de aumentar imagen y mascara juntas.
    """
    import tensorflow as tf
    alto, ancho = tam
    assert alto % n == 0 and ancho % n == 0, f"una grilla de {n} no divide {tuple(tam)}"
    celda = (alto // n, ancho // n)
    grilla = tf.nn.max_pool2d(mascaras, celda, celda, "VALID")
    return {"salida": tf.reduce_max(grilla, axis=(1, 2)), "grilla": grilla}


# --- Gemelos -----------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def dhash(ruta, lado=8):
    """Entero de 64 bits: 1 donde el pixel crece hacia la derecha, sobre una miniatura
    en gris. Sobrevive a recompresion, reescalado y cambios chicos de brillo: identifica
    dos cuadros del mismo video. Memorizado: train se hashea una sola vez aunque se
    compare contra validacion y contra prueba."""
    px = np.asarray(Image.open(ruta).convert("L").resize((lado + 1, lado), Image.LANCZOS))
    bits = 0
    for fila in px:
        for c in range(lado):
            bits = (bits << 1) | int(fila[c] < fila[c + 1])
    return bits


def sin_gemelo(referencia, archivos):
    """Mascara: True para cada archivo cuyo dhash NO aparece entre los de `referencia`.

    Con 64 bits y ~21 k imagenes la probabilidad de una colision por azar es ~1e-11:
    un hash igual es un gemelo real, no un accidente.
    """
    hashes = {dhash(r) for r in referencia}
    return np.array([dhash(r) not in hashes for r in archivos])


# --- Self-check --------------------------------------------------------------
def self_check():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)

        # etiquetas: las cuatro combinaciones, fuego+humo incluida (no excluyentes)
        casos = {"": [0, 0], "0 .5 .5 .2 .2\n": [0, 1], "1 .5 .5 .2 .2\n": [1, 0],
                 "0 .1 .1 .1 .1\n1 .5 .5 .2 .2\n\n": [1, 1]}
        for i, (txt, esperado) in enumerate(casos.items()):
            (d / f"{i}.txt").write_text(txt)
            assert etiqueta_imagen(d / f"{i}.txt") == esperado, (txt, esperado)
        assert [COMBOS[c] for c in combo([[0, 0], [0, 1], [1, 0], [1, 1]])] == COMBOS
        # 3 categorias: fuego con o sin humo es incendio
        assert [CATEGORIAS[c] for c in categoria([[0, 0], [0, 1], [1, 0], [1, 1]])] == \
            ["normal", "humo", "incendio", "incendio"]

        # particion: disjunta, completa, respeta el split oficial y es deterministica
        splits = np.array(["train"] * 7 + ["val"] * 2 + ["test"] * 3)
        a, b = particion(splits, 42), particion(splits, 42)
        assert all((a[s] == b[s]).all() and (splits[a[s]] == s).all() for s in SPLITS)
        assert sorted(np.concatenate([a[s] for s in SPLITS])) == list(range(len(splits)))

        # indexar: rutas, etiquetas y split desde la estructura del banco
        for s, txt in zip(SPLITS, ("1 .5 .5 .2 .2", "", "0 .5 .5 .2 .2")):
            (d / s / "images").mkdir(parents=True)
            (d / s / "labels").mkdir()
            Image.new("RGB", (8, 8)).save(d / s / "images" / "x.jpg")
            (d / s / "labels" / "x.txt").write_text(txt)
        archivos, etiquetas, sp = indexar(d)
        assert list(sp) == list(SPLITS), sp
        assert etiquetas.tolist() == [[1, 0], [0, 0], [0, 1]], etiquetas

        # cajas: YOLO (clase cx cy w h) -> [canal, x0, y0, x1, y1]; la tabla se rellena
        # con canal -1 (imagen sin cajas incluida)
        (d / "c.txt").write_text("1 .5 .5 .2 .4\n\n0 .25 .25 .5 .5\n")
        assert np.allclose(cajas_imagen(d / "c.txt"), [[0, .4, .3, .6, .7], [1, 0, 0, .5, .5]])
        tabla = tabla_cajas(archivos)
        assert tabla.shape == (3, 1, 5), tabla.shape
        assert np.allclose(tabla[:, 0], [[0, .4, .4, .6, .6], [-1] * 5, [1, .4, .4, .6, .6]])

        # dhash: el mismo cuadro recomprimido da el mismo hash; otra escena, otro
        bandas = np.array([0, 200, 50, 250, 20, 180, 90, 240, 10], dtype=np.uint8)
        escena = Image.fromarray(np.tile(bandas.repeat(20), (80, 1)))
        otra = Image.fromarray(np.tile(np.roll(bandas, 1).repeat(20), (80, 1)))
        escena.save(d / "a.png")
        escena.save(d / "a.jpg", quality=90)
        otra.save(d / "b.png")
        a_png, a_jpg, b_png = (str(d / n) for n in ("a.png", "a.jpg", "b.png"))
        assert dhash(a_png) == dhash(a_jpg), "la recompresion cambio el hash"
        assert dhash(a_png) != dhash(b_png), "dos escenas distintas dieron el mismo hash"
        assert sin_gemelo([a_png], [a_jpg, b_png]).tolist() == [False, True]

        # tam opcional: el default sigue siendo IMG_SIZE (notebooks 4 y 5 intactos)
        import inspect
        for f in (leer_imagen, mascara_cajas, objetivos, armar_ds):
            assert inspect.signature(f).parameters["tam"].default == IMG_SIZE, f.__name__

        # 128x128 y grilla 16 (notebook 6): formas y contenido con TensorFlow.
        try:
            import tensorflow as tf
        except ImportError:
            tf = None
        if tf is None:
            print("self-check OK (sin TensorFlow: 128 y grilla 16 no verificados)")
            return
        grande = np.zeros((160, 120, 3), dtype=np.uint8)
        grande[40:120, 30:90] = 200
        Image.fromarray(grande).save(d / "g.jpg")
        assert tuple(leer_imagen(str(d / "g.jpg")).shape) == (96, 96, 3)
        assert tuple(leer_imagen(str(d / "g.jpg"), (128, 128)).shape) == (128, 128, 3)
        # caja que cubre todo: mascara llena a los dos tamanos; la grilla 16x16 de
        # 128 sale de celdas de 8x8 y la etiqueta de imagen es el maximo.
        llena = [[0, 0.0, 0.0, 1.0, 1.0]]
        m96 = mascara_cajas(llena).numpy()
        m128 = mascara_cajas(llena, (128, 128)).numpy()
        assert m96.shape == (96, 96, 2) and m128.shape == (128, 128, 2)
        assert m96[..., 0].all() and not m96[..., 1].any()
        assert m128[..., 0].all() and not m128[..., 1].any()
        o = objetivos(tf.expand_dims(tf.convert_to_tensor(m128), 0), 16, (128, 128))
        assert tuple(o["grilla"].shape) == (1, 16, 16, 2), o["grilla"].shape
        assert o["grilla"].numpy()[0, ..., 0].all()
        assert o["salida"].numpy().tolist() == [[1.0, 0.0]]
        # el default viejo sigue dando la grilla 4x4 de 96
        o4 = objetivos(tf.expand_dims(tf.convert_to_tensor(m96), 0), 4)
        assert tuple(o4["grilla"].shape) == (1, 4, 4, 2), o4["grilla"].shape

    print("self-check OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-check", action="store_true")
    if ap.parse_args().self_check:
        self_check()
    else:
        ap.print_help()
