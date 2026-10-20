#!/usr/bin/env -S uv run --script
"""Filtro "cámara OV": hace que una foto de D-Fire se vea como la entrega el OV5640 del nodo.

Es **adaptación de dominio por aumento** (simulación del sensor): no se tienen miles de
incendios vistos por el OV5640, pero sí miles de D-Fire; se los "ensucia" con lo que hace la
cámara y la red aprende a ignorar esa diferencia. Lo usa `12_filtro_camara.ipynb` para re-entrenar
con D-Fire filtrado (y `13_fine_tuning_camara` como aumento). Es aleatorio: cada imagen sale con
parámetros distintos dentro de los rangos de abajo, así la red no aprende "una" cámara sino la
variación que da el sensor.

Cadena, en el orden físico (óptica -> sensor -> ISP):

    velo magenta -> viñeteo -> balance de blancos -> exposición (quema altas luces)
    -> noche (escena oscura, fuentes de luz cálidas intactas) -> gamma -> saturación
    -> nitidez/desenfoque -> ruido (crece con la señal) -> nivel de negro -> RGB565

Rangos medidos el 2026-10-20 con `estadisticas()` sobre `data/capturas/` (todas a QVGA, ~150
por grupo) contra 177 fotos de D-Fire train, y lo que da el filtro sobre esas mismas D-Fire:

| medida                  | D-Fire | día JPEG 04-10 | día BMP 18-10 | noche BMP 18-10 | filtro día | filtro noche |
|-------------------------|--------|----------------|---------------|-----------------|------------|--------------|
| R/G                     | 1,01   | 0,97           | 0,87          | 0,86            | 0,93       | 0,97         |
| B/G                     | 1,00   | 0,89           | 0,90          | 0,88            | 0,91       | 0,76         |
| luminancia p10 / p50    | 31/81  | 28/68          | 23/68         | 5/10            | 25/77      | 1/7          |
| luminancia p90          | 170    | 241            | 181           | 26              | 171        | 44           |
| píxeles con canal > 250 | 2,9 %  | 10,5 %         | 9,7 %         | 0,4 %           | 13,4 %     | 1,5 %        |
| píxeles con L < 20      | 19 %   | 11 %           | 22 %          | 90 %            | 29 %       | 80 %         |
| ruido de alta frecuencia| 1,7    | 2,6 (JPEG q70) | 4,0           | 0,3 (aplastado) | 3,0        | 0,8          |

"filtro día" es con `P_NOCHE = 0` y "filtro noche" con `P_NOCHE = 1`. Los JPEG del 04-10 vienen
de QVGA, VGA y UXGA reducidas: el p90 alto es cielo nublado quemado.

Lo que se ve a ojo en las capturas y no está en D-Fire: noches con el fuego como un punto de
pocos píxeles sobre negro, velo rosado/magenta que entra por un costado o el centro (reflejo en
la óptica sin filtro IR bueno), viñeteo, cielos quemados y negros aplastados. Las capturas de día
son pocas escenas (patio con pasto y muro): parte del verde de R/G es la escena, no el sensor;
por eso los rangos de color abarcan "neutro".

El tamaño chico del fuego (lejos de la cámara) no es del sensor: lo cubre el `RandomZoom` con
alejamiento del aumento del notebook. Tampoco se simula JPEG: el nodo recibe RGB565 crudo; el JPEG
de `tools/view.py --save` es sólo cómo se guardaron las capturas del 2026-10-04.

Uso:
    ./camara_ov.py --self-check
"""
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "tensorflow"]
# ///

import argparse

import numpy as np
import tensorflow as tf
from tensorflow import keras

# Rangos U(lo, hi) por imagen. Ver la tabla del docstring.
GANANCIA_R = (0.78, 1.02)    # R/G de la cámara ≈ 0,90 × el de D-Fire (día)
GANANCIA_B = (0.78, 1.00)    # B/G ≈ 0,89 ×
EXPOSICION = (0.90, 1.60)    # altas luces más arriba y quemadas (clip 2,9 % -> ~10 %)
GAMMA = (1.00, 1.60)         # >1 cierra sombras y medios: la cámara tiene más contraste que D-Fire
SATURACION = (0.90, 1.30)
NITIDEZ = (-0.8, 0.8)        # >0 máscara de enfoque del ISP; <0 desenfoque (óptica, VGA->QVGA)
RUIDO = (1.5, 6.0)           # sigma del ruido en niveles 0-255, a señal media (crece con la señal)
NEGRO = (0.0, 0.10)          # nivel de negro recortado: aplasta las sombras (p10 más bajo)
VINETEO = (0.0, 0.45)        # caída de brillo en las esquinas (1 - v·r²)

P_NOCHE = 0.20               # fracción de imágenes llevadas a noche
NOCHE = (0.10, 0.40)         # factor sobre lo que no es fuente de luz cálida (p50 81 -> ~10)
P_VELO = 0.25                # fracción con velo magenta
VELO = (0.15, 0.60)          # intensidad del velo en su centro, 0-1
VELO_ANCHO = (0.25, 0.80)    # sigma del velo, en fracciones de la diagonal


def a_rgb565(x):
    """Tensor float 0-255 (..., 3) -> color 5/6/5 bits vuelto a 8, como lo entrega el sensor."""
    q = tf.cast(tf.clip_by_value(tf.round(x), 0, 255), tf.int32)
    bits = tf.constant([5, 6, 5])
    v = tf.bitwise.right_shift(q, 8 - bits)
    y = tf.bitwise.bitwise_or(tf.bitwise.left_shift(v, 8 - bits),
                              tf.bitwise.right_shift(v, 2 * bits - 8))
    return tf.cast(y, x.dtype)


def coordenadas(y):
    """Malla (1, H, W, 1) x e y normalizadas a [0, 1] para un lote y (n, H, W, 3)."""
    h, w = tf.shape(y)[1], tf.shape(y)[2]
    gy, gx = tf.meshgrid(tf.linspace(0.0, 1.0, h), tf.linspace(0.0, 1.0, w), indexing="ij")
    return gx[None, :, :, None], gy[None, :, :, None]


def velo(y, intensidad, cx, cy, ancho, verde):
    """Mancha suave magenta sumada (reflejo en la óptica). Parámetros (n, 1, 1, 1), y en 0-1."""
    gx, gy = coordenadas(y)
    d2 = ((gx - cx) ** 2 + (gy - cy) ** 2) / 2.0           # /2: distancia en diagonales
    mancha = intensidad * tf.exp(-d2 / (2 * ancho ** 2))
    color = tf.concat([tf.ones_like(verde), verde, tf.ones_like(verde)], axis=-1)
    return y + mancha * color


def vineteo(y, v):
    gx, gy = coordenadas(y)
    r2 = ((gx - 0.5) ** 2 + (gy - 0.5) ** 2) / 0.5         # 1 en las esquinas
    return y * (1.0 - v * r2)


def noche(y, factor):
    """Oscurece la escena dejando las fuentes de luz cálidas (llamas): brillantes y R > B."""
    fuente = (tf.clip_by_value((tf.reduce_max(y, -1, keepdims=True) - 0.70) / 0.20, 0.0, 1.0)
              * tf.clip_by_value((y[..., :1] - y[..., 2:]) / 0.15, 0.0, 1.0))
    return y * (factor + fuente * (1.0 - factor))


class CamaraOV(keras.layers.Layer):
    """Con probabilidad `p`, pasa cada imagen (float 0-255) por la cadena de la cámara del
    docstring del módulo. Sólo en entrenamiento: en inferencia deja pasar la imagen tal cual.
    Reemplaza a `ColorRGB565` del notebook 4: el RGB565 es el último paso de la cadena.
    """

    def __init__(self, p=0.5, **kwargs):
        super().__init__(**kwargs)
        self.p = p
        self.desenfoque = tf.constant(
            np.tile((np.array([[1, 2, 1], [2, 4, 2], [1, 2, 1]], np.float32) / 16)[:, :, None, None],
                    (1, 1, 3, 1)))

    def call(self, x, training=None):
        if not training:
            return x
        n = tf.shape(x)[:1]

        def u(rango):         # un valor por imagen, listo para multiplicar (n, 1, 1, 1)
            return tf.random.uniform(n, *rango)[:, None, None, None]

        def sorteo(p):        # 1.0 en una fracción p de las imágenes, 0.0 en el resto
            return tf.cast(tf.random.uniform(n) < p, x.dtype)[:, None, None, None]

        y = x / 255.0
        y = velo(y, u(VELO) * sorteo(P_VELO), u((-0.1, 1.1)), u((-0.1, 1.1)), u(VELO_ANCHO),
                 u((0.55, 0.85)))
        y = vineteo(y, u(VINETEO))
        y = y * tf.concat([u(GANANCIA_R), tf.ones_like(u(GANANCIA_R)), u(GANANCIA_B)], axis=-1)
        y = tf.clip_by_value(y * u(EXPOSICION), 0.0, 1.0)
        es_noche = sorteo(P_NOCHE)
        y = noche(y, es_noche * u(NOCHE) + (1.0 - es_noche))
        y = tf.pow(y + 1e-6, u(GAMMA))
        lum = tf.reduce_mean(y, axis=-1, keepdims=True)
        y = lum + (y - lum) * u(SATURACION)
        # Bordes espejados: con relleno de ceros el desenfoque oscurece el marco.
        borroso = tf.nn.depthwise_conv2d(tf.pad(y, [[0, 0], [1, 1], [1, 1], [0, 0]], "REFLECT"),
                                         self.desenfoque, [1, 1, 1, 1], "VALID")
        y = y + u(NITIDEZ) * (y - borroso)
        # Ruido de disparo + lectura: sigma ∝ sqrt(señal), igual a RUIDO a señal 0,5.
        sigma = u(RUIDO) / 255.0 * tf.sqrt(tf.clip_by_value(y, 0.02, 1.0) / 0.5)
        y = y + tf.random.normal(tf.shape(y)) * sigma
        b = u(NEGRO)
        y = (y - b) / (1.0 - b)
        y = a_rgb565(tf.clip_by_value(y, 0.0, 1.0) * 255.0)
        usar = tf.random.uniform(n) < self.p
        return tf.where(usar[:, None, None, None], y, x)

    def get_config(self):
        return {**super().get_config(), "p": self.p}


def estadisticas(imagenes):
    """Medidas de "cómo se ve" un conjunto de imágenes (iterable de arrays H x W x 3, 0-255).

    Son las de la tabla del docstring: lo que separa a la cámara de D-Fire. Promedio sobre
    las imágenes; R/G y B/G como cociente de sumas (un frame negro no da nan).
    """
    m, sat, pct, hf, clip, osc = [], [], [], [], [], []
    for a in imagenes:
        a = np.asarray(a, dtype=np.float32)
        lum = a.mean(-1)
        mx, mn = a.max(-1), a.min(-1)
        m.append(a.reshape(-1, 3).mean(0))
        sat.append(((mx - mn) / (mx + 1e-3)).mean())
        pct.append(np.percentile(lum, [10, 50, 90, 99]))
        # Residuo contra el promedio de los 4 vecinos; MAD robusto a los bordes de objetos.
        h = lum[1:-1, 1:-1] - (lum[:-2, 1:-1] + lum[2:, 1:-1] + lum[1:-1, :-2] + lum[1:-1, 2:]) / 4
        hf.append(np.median(np.abs(h)) * 1.4826)
        clip.append((a > 250).any(-1).mean())
        osc.append((lum < 20).mean())
    m, pct = np.sum(m, 0), np.mean(pct, 0)
    return {"R/G": float(m[0] / (m[1] + 1e-6)), "B/G": float(m[2] / (m[1] + 1e-6)),
            "saturación": float(np.mean(sat)),
            "L p10": float(pct[0]), "L p50": float(pct[1]), "L p90": float(pct[2]), "L p99": float(pct[3]),
            "ruido HF": float(np.mean(hf)), "clip>250": float(np.mean(clip)), "L<20": float(np.mean(osc))}


def tabla(columnas):
    """Imprime {nombre: estadisticas(...)} como tabla, una columna por conjunto."""
    nombres = list(columnas)
    print(f"{'medida':<12}" + "".join(f"{c:>16}" for c in nombres))
    for k in next(iter(columnas.values())):
        print(f"{k:<12}" + "".join(f"{columnas[c][k]:>16.3f}" for c in nombres))


def self_check():
    rng = np.random.default_rng(0)
    # Imagen suave y cálida tipo D-Fire: gradiente + poco ruido.
    yy, xx = np.mgrid[0:60, 0:80]
    img = np.stack([120 + xx, 100 + yy, 90 + 0 * xx], -1).astype(np.float32)
    img += rng.normal(0, 1, img.shape).astype(np.float32)
    lote = tf.constant(np.repeat(img[None], 64, 0))

    assert np.array_equal(CamaraOV(1.0)(lote, training=False).numpy(), lote.numpy())
    assert np.array_equal(CamaraOV(0.0)(lote, training=True).numpy(), lote.numpy())
    tf.random.set_seed(0)
    y = CamaraOV(1.0)(lote, training=True).numpy()
    assert y.shape == lote.shape and y.min() >= 0 and y.max() <= 255
    assert np.all(y[..., 1].astype(int) % 4 == y[..., 1].astype(int) // 64), "G no es de 6 bits"
    antes, despues = estadisticas(lote.numpy()), estadisticas(y)
    assert despues["ruido HF"] > antes["ruido HF"], (antes, despues)
    assert despues["L<20"] > antes["L<20"], (antes, despues)     # noches y negros aplastados

    # Noche: la llama (cálida y brillante) queda, el resto se apaga.
    escena = np.full((1, 8, 8, 3), 0.5, np.float32)
    escena[0, 3:5, 3:5] = (1.0, 0.6, 0.2)
    oscura = noche(tf.constant(escena), tf.constant(0.1, shape=(1, 1, 1, 1))).numpy()
    assert np.allclose(oscura[0, 0, 0], 0.05) and np.allclose(oscura[0, 3, 3], escena[0, 3, 3])
    # Velo: suma más en verde-magenta (R, B) que en G, y más cerca de su centro.
    v = velo(tf.zeros((1, 9, 9, 3)), *[tf.constant(c, shape=(1, 1, 1, 1)) for c in (0.5, 0.0, 0.0, 0.3, 0.7)]).numpy()
    assert v[0, 0, 0, 0] > v[0, 0, 0, 1] and v[0, 0, 0, 0] > v[0, 8, 8, 0]
    # Viñeteo: centro intacto, esquinas más oscuras.
    w = vineteo(tf.ones((1, 9, 9, 3)), tf.constant(0.4, shape=(1, 1, 1, 1))).numpy()
    assert np.isclose(w[0, 4, 4, 0], 1.0) and np.isclose(w[0, 0, 0, 0], 0.6)
    print("self-check ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-check", action="store_true")
    if ap.parse_args().self_check:
        self_check()
