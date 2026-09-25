#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pillow"]
# ///
"""Deteccion de linea de horizonte en el nodo (Flamara). Simulador del firmware.

Hardware real del nodo (ver ../../diagramas/nodo-hardware.html):

    ESP32-CAM  ---- UART ---->  Heltec WiFi LoRa 32  ---- LoRa 915 MHz ---->  gateway
    captura + procesa           SX1276, solo transmite
    + microSD (evidencia)       + BME280 / DS3231 / OLED por I2C

El ESP32-CAM hace TODO el procesamiento y tiene microSD, asi que hay DOS destinos con
presupuestos completamente distintos:

  - SD local: barata, grande, sirve para recalibrar el detector offline y para
    store-and-forward cuando el enlace LoRa se cae. Aca conviene guardar la grilla
    del indice completa, no solo el resultado.
  - Enlace LoRa (via UART al Heltec): caro en tiempo al aire y en ciclo de trabajo.
    Aca va solo la recta: 6 bytes.

La reduccion agresiva es una restriccion del ENLACE, no del procesamiento ni del
almacenamiento. Es lo que separa este pipeline del de fuego, donde todo iba al aire.

Etapas (todas enteras, sin FPU, sin division en el camino caliente):

  Etapa 0  captura QVGA 320x240 RGB565 (el driver esp32-camera la deja en PSRAM)
  Etapa 1  reduccion BOX a grilla gh x gw con reciproco fijo (sin division)
  Etapa 2  indice cromatico cielo/suelo (entero, 1-2 ops por celda)
  Etapa 3  umbral (fijo u Otsu sobre histograma de 256 bins)
  Etapa 4  cruce del umbral por columna + interpolacion sub-celda -> perfil y(x)
  Etapa 5  ajuste robusto de recta (minimos cuadrados enteros + rechazo de outliers)
  Etapa 6  registro en SD + trama UART al Heltec (6 B utiles) -> LoRa

La reduccion es anisotropica a proposito: el horizonte es una funcion y(x), asi que
las FILAS son resolucion de medida y las COLUMNAS son solo cantidad de muestras para
el ajuste. A igual presupuesto de celdas conviene grilla alta y angosta (120x16)
antes que cuadrada (16x16). El notebook `notebooks/2_horizonte.ipynb` mide eso.

Uso:
    ./horizonte_sim.py                  # escena sintetica con horizonte conocido
    ./horizonte_sim.py foto.jpg
    ./horizonte_sim.py --self-check
"""

import argparse
import math
import sys
from pathlib import Path

import numpy as np

import pipeline_sim as ps          # reusa to_rgb565, lora_airtime, load de imagen

# --- Constantes del firmware -------------------------------------------------
SRC_W, SRC_H = ps.SRC_W, ps.SRC_H        # QVGA 320x240
GRID_H, GRID_W = 120, 16                 # grilla nominal (alta y angosta)
BOX_SHIFT = 22                           # mismo esquema de reciproco que el pipeline de fuego

TH_CONTRASTE = 6        # salto minimo cielo->suelo para aceptar una columna
K_OUTLIER = 2.0         # rechazo: |residuo| > K * error medio absoluto
MIN_INLIERS = 4         # menos que esto -> deteccion invalida

# Divisores utiles (la grilla debe dividir exacto a QVGA)
FILAS_OK = (15, 16, 20, 24, 30, 40, 48, 60, 80, 120, 240)
COLS_OK = (8, 10, 16, 20, 32, 40, 64, 80)


# --- Etapa 0: escena de prueba con horizonte conocido ------------------------
def synthetic_scene(m=-0.08, b=118.0, seed=0, noche=False, nubes=True, fuego=False,
                    ruido=3.5):
    """Escena con horizonte exacto y(x) = m*x + b (en px de la imagen QVGA).

    El borde se genera con anti-aliasing por cobertura de pixel: sin eso la
    interpolacion sub-celda no tendria nada real que recuperar y las metricas
    saldrian optimistas.
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:SRC_H, 0:SRC_W].astype(np.float32)
    yh = m * xx + b                                   # altura del horizonte por columna

    if noche:
        cielo_alto, cielo_bajo = (12, 16, 34), (40, 30, 30)
        suelo = (26, 22, 18)
    else:
        cielo_alto, cielo_bajo = (70, 110, 185), (155, 180, 205)
        suelo = (96, 86, 52)

    t = np.clip(yy / np.maximum(yh, 1.0), 0, 1)[..., None]
    cielo = np.array(cielo_alto, np.float32) * (1 - t) + np.array(cielo_bajo, np.float32) * t
    tierra = np.zeros_like(cielo) + np.array(suelo, np.float32)
    tierra *= (1.0 + 0.25 * np.sin(xx / 9.0) * np.cos(yy / 7.0))[..., None]   # textura

    if nubes:
        n = np.exp(-((yy - 45) ** 2) / 900.0) * np.exp(-((xx - 90) ** 2) / 5000.0)
        cielo += (n * 55)[..., None]
    if fuego:
        g = np.exp(-((yy - yh) ** 2) / 260.0) * np.exp(-((xx - 210) ** 2) / 9000.0)
        tierra += (g[..., None] * np.array([210, 90, 25], np.float32))
        cielo += (g[..., None] * np.array([150, 60, 15], np.float32))

    cob = np.clip(yh - yy + 0.5, 0.0, 1.0)[..., None]      # 1 = cielo puro, 0 = suelo puro
    im = cielo * cob + tierra * (1 - cob)
    im += rng.normal(0, ruido, im.shape)
    return np.clip(im, 0, 255).astype(np.uint8), (float(m), float(b))


def load_qvga(src=None, **kw):
    """None -> escena sintetica (rgb, ground truth). Path/BytesIO -> (rgb, None)."""
    if src is None:
        return synthetic_scene(**kw)
    return ps.load_qvga(src), None


# --- Etapa 1: reduccion ------------------------------------------------------
def reciproco(npx, shift=BOX_SHIFT):
    """Reciproco entero para dividir por npx con un shift. acc*recip cabe en uint32:
    acc <= 255*npx, luego acc*recip ~ 255*2**shift = 1.07e9 < 2**32 para shift=22."""
    return round(2 ** shift / npx)


def reduce(rgb, gh=GRID_H, gw=GRID_W, modo="box", shift=BOX_SHIFT):
    """QVGA -> grilla (gh, gw, 3) uint8.

    modo="box"   promedio del bloque con reciproco fijo (lo que hace el firmware)
    modo="decim" toma el pixel central del bloque (mas barato, mucho mas ruidoso)
    """
    if SRC_H % gh or SRC_W % gw:
        raise ValueError(f"la grilla {gh}x{gw} no divide exacto a {SRC_H}x{SRC_W}")
    bh, bw = SRC_H // gh, SRC_W // gw
    if modo == "decim":
        return rgb[bh // 2::bh, bw // 2::bw].copy()
    acc = (rgb.reshape(gh, bh, gw, bw, 3).sum(axis=(1, 3), dtype=np.int64))
    return ((acc * reciproco(bh * bw, shift)) >> shift).clip(0, 255).astype(np.uint8)


# --- Etapa 2: indices cielo / suelo -----------------------------------------
def indices(grid):
    """Candidatos de indice. Todos enteros; los lineales conmutan con el BOX
    (BOX(B-R) == BOX(B)-BOX(R)), asi que da igual calcularlos antes o despues."""
    R, G, B = (grid[..., i].astype(np.int32) for i in range(3))   # int32: 77*R+150*G+29*B desborda int16
    return {
        "B-R":        np.clip(B - R, 0, 255).astype(np.int16),
        "B-G":        np.clip(B - G, 0, 255).astype(np.int16),
        "2B-R-G":     np.clip((2 * B - R - G) >> 1, 0, 255).astype(np.int16),
        "B-max(R,G)": np.clip(B - np.maximum(R, G), 0, 255).astype(np.int16),
        "gris":       ((77 * R + 150 * G + 29 * B) >> 8).astype(np.int16),
    }


def orientar(idx):
    """Deja el indice con el cielo ALTO. Compara el cuarto superior contra el
    inferior; si esta al reves lo invierte (255-x, un solo sub por celda)."""
    k = max(1, idx.shape[0] // 4)
    if idx[:k].mean() >= idx[-k:].mean():
        return np.clip(idx, 0, 255).astype(np.int16), False
    return np.clip(255 - idx, 0, 255).astype(np.int16), True


def separacion(idx, mask_cielo):
    """Margen cielo/suelo en sigmas (Fisher). Mayor = mejor discriminador."""
    a = idx[mask_cielo].astype(float)
    b = idx[~mask_cielo].astype(float)
    if a.size == 0 or b.size == 0:
        return float("nan")
    s = math.sqrt((a.var() + b.var()) / 2) or 1e-9
    return abs(a.mean() - b.mean()) / s


# --- Etapa 3: umbral ---------------------------------------------------------
def otsu(idx):
    """Umbral de Otsu sobre histograma de 256 bins. En el ESP32: un array de
    256 uint16 (512 B) y una pasada por la grilla."""
    h = np.bincount(np.clip(idx, 0, 255).ravel().astype(np.int64), minlength=256).astype(float)
    niveles = np.arange(256, dtype=float)
    w0 = np.cumsum(h)[:-1]
    w1 = idx.size - w0
    s0 = np.cumsum(niveles * h)[:-1]
    s_tot = float((niveles * h).sum())
    with np.errstate(divide="ignore", invalid="ignore"):
        mu0 = np.where(w0 > 0, s0 / np.maximum(w0, 1), 0.0)
        mu1 = np.where(w1 > 0, (s_tot - s0) / np.maximum(w1, 1), 0.0)
    var = w0 * w1 * (mu0 - mu1) ** 2
    return int(np.argmax(var))


# --- Etapa 4: perfil del horizonte por columna -------------------------------
def horizon_profile(idx, th, metodo="umbral", th_contraste=TH_CONTRASTE, subcelda=True):
    """Por cada columna busca el paso cielo->suelo y devuelve la fila fraccionaria.

    metodo="umbral"  cruce descendente del umbral con MAYOR salto, interpolacion
                     lineal sub-celda. Se queda con el cruce mas fuerte y no con el
                     primero: un cielo con degrade o nubes cruza el umbral varias
                     veces y el primer cruce caeria dentro del cielo.
    metodo="grad"    maximo gradiente vertical, interpolacion parabolica de 3 puntos

    Devuelve (y_celda float[gw], valido bool[gw]) en unidades de fila de la grilla
    (0 = centro de la primera fila). Escrito como bucle porque asi va en C.
    """
    gh, gw = idx.shape
    y = np.zeros(gw, float)
    ok = np.zeros(gw, bool)
    p_all = idx.astype(np.int32)

    for c in range(gw):
        p = p_all[:, c]
        if metodo == "grad":
            g = np.abs(np.diff(p))
            r = int(np.argmax(g))
            if g[r] < th_contraste:
                continue
            pos = r + 0.5
            if subcelda and 0 < r < gh - 2:
                den = float(g[r - 1] - 2 * g[r] + g[r + 1])
                if den != 0:
                    pos += np.clip(0.5 * (g[r - 1] - g[r + 1]) / den, -0.5, 0.5)
            y[c], ok[c] = pos, True
            continue

        cruces = np.flatnonzero((p[:-1] > th) & (p[1:] <= th))   # cielo -> suelo
        if cruces.size == 0:                 # horizonte fuera de cuadro
            continue
        saltos = p[cruces] - p[cruces + 1]
        j = int(np.argmax(saltos))
        r, d = int(cruces[j]) + 1, int(saltos[j])
        if d < th_contraste:
            continue
        if not subcelda:
            y[c], ok[c] = (r - 1) + 0.5, True     # borde entre las dos celdas
            continue
        # Con BOX el valor de una celda es cielo*cob + suelo*(1-cob): el borde real
        # esta donde cob = 0.5, o sea en el NIVEL MEDIO LOCAL, no en el umbral global
        # (Otsu no cae en el medio y mete un sesgo fijo de varios px).
        alto = int(p[max(r - 2, 0)])
        bajo = int(p[min(r + 1, gh - 1)])
        medio = (alto + bajo) >> 1
        if p[r - 1] > medio >= p[r]:
            y[c] = (r - 1) + (p[r - 1] - medio) / d
        else:
            y[c] = (r - 1) + 0.5
        ok[c] = True
    return y, ok


def a_pixeles(y_celda, ok, gh, gw):
    """Perfil en coordenadas de la grilla -> (x_px, y_px) de los centros de celda."""
    bh, bw = SRC_H / gh, SRC_W / gw
    x = (np.arange(gw) + 0.5) * bw
    return x[ok], (y_celda[ok] + 0.5) * bh


# --- Etapa 5: ajuste robusto de recta ---------------------------------------
def fit_line(x, y):
    """Minimos cuadrados. En el ESP32: 4 acumuladores int32 y 2 divisiones."""
    n = x.size
    if n < 2:
        return None
    sx, sy = x.sum(), y.sum()
    sxx, sxy = (x * x).sum(), (x * y).sum()
    den = n * sxx - sx * sx
    if den == 0:
        return None
    m = (n * sxy - sx * sy) / den
    return float(m), float((sy - m * sx) / n)


def fit_line_robust(x, y, k=K_OUTLIER, pasadas=1, min_inliers=MIN_INLIERS):
    """LS + rechazo de outliers por error medio absoluto.

    Devuelve (m, b, n_inliers, mask_inliers) o None.
    ponytail: usa la media de |residuo| en vez de la mediana (MAD) para no
    ordenar en el ESP32; con >=8 columnas validas la diferencia es despreciable.
    Si aparecen escenas con >30 % de outliers, pasar a MAD (sort de gw elementos).
    """
    if x.size < min_inliers:
        return None
    keep = np.ones(x.size, bool)
    ajuste = fit_line(x, y)
    for _ in range(pasadas):
        if ajuste is None:
            return None
        m, b = ajuste
        r = np.abs(y - (m * x + b))
        umbral = k * r.mean()
        nuevo = r <= max(umbral, 1e-6)
        if nuevo.sum() < min_inliers:
            break
        keep = nuevo
        ajuste = fit_line(x[keep], y[keep])
    if ajuste is None:
        return None
    return ajuste[0], ajuste[1], int(keep.sum()), keep


# --- Etapa 6: empaquetado ----------------------------------------------------
def pack(m, b, inliers, flags=1):
    """6 bytes: flags(1) + pendiente Q8.8(2) + offset Q14.2(2) + inliers(1)."""
    mq = int(np.clip(round(m * 256), -32768, 32767))
    bq = int(np.clip(round(b * 4), -32768, 32767))
    return bytes([flags & 0xFF]) + mq.to_bytes(2, "little", signed=True) + \
        bq.to_bytes(2, "little", signed=True) + bytes([min(inliers, 255)])


def unpack(pkt):
    """Lado receptor. Devuelve (m, b, inliers) o None si es la trama vacia."""
    if len(pkt) < 6 or pkt[0] == 0:
        return None
    m = int.from_bytes(pkt[1:3], "little", signed=True) / 256.0
    b = int.from_bytes(pkt[3:5], "little", signed=True) / 4.0
    return m, b, pkt[5]


# --- Metricas ----------------------------------------------------------------
def error_recta(a, b_recta, ancho=SRC_W):
    """Error entre dos rectas (m, b) barriendo el ancho de la imagen.
    Devuelve dict con MAE, error maximo y error angular en grados."""
    if a is None or b_recta is None:
        return dict(mae=float("nan"), emax=float("nan"), ang=float("nan"))
    x = np.arange(ancho, dtype=float)
    d = np.abs((a[0] * x + a[1]) - (b_recta[0] * x + b_recta[1]))
    ang = math.degrees(abs(math.atan(a[0]) - math.atan(b_recta[0])))
    return dict(mae=float(d.mean()), emax=float(d.max()), ang=ang)


def mascara_cielo(m, b):
    """Mascara QVGA de cielo segun una recta (para separabilidad y overlays)."""
    yy, xx = np.mgrid[0:SRC_H, 0:SRC_W]
    return yy < (m * xx + b)


# --- Costo en el ESP32 -------------------------------------------------------
def costo_esp32(gh, gw, metodo="umbral", otsu_on=True, mhz=240, ciclos_op=3.0,
                origen="fb"):
    """Operaciones enteras por frame en el ESP32-CAM.

    El termino dominante es fijo: la pasada BOX sobre los 76800 px, que NO depende
    del tamano de la grilla. Subir de 16x16 a 120x16 casi no cuesta tiempo.

    origen="fb"      camino normal: esp_camera_fb_get() deja el frame QVGA RGB565
                     completo en PSRAM y el BOX lo recorre in-place. No hace falta
                     ningun buffer extra, pero la PSRAM esta ocupada igual.
    origen="stream"  variante por callback de linea del DMA: no retiene el frame,
                     solo gw*3 acumuladores int32. Ahorra PSRAM pero deja al nodo
                     sin la imagen para guardar en la SD ni para comprimir a JPEG.
    """
    npx = SRC_W * SRC_H
    ops = {
        "BOX: acumular frame":  npx * 3,
        "BOX: normalizar":      gh * gw * 3 * 2,
        "indice cromatico":     gh * gw * 2,
        "histograma + Otsu":    (gh * gw + 256 * 6) if otsu_on else 0,
        "perfil por columna":   gh * gw + gw * (12 if metodo == "grad" else 6),
        "ajuste robusto":       gw * 10 + 40,
    }
    total = sum(ops.values())
    divisiones = gw + (6 if metodo == "umbral" else 4)
    sram = {
        "acumuladores de fila (int32)": gw * 3 * 4,
        "grilla (uint8 RGB)":           gh * gw * 3,
        "indice (uint8)":               gh * gw,
        "histograma (uint16)":          512 if otsu_on else 0,
        "perfil y validez":             gw * 3,
    }
    psram = SRC_W * SRC_H * 2 if origen == "fb" else 0
    return dict(ops=ops, total_ops=total, divisiones=divisiones,
                ram=sram, ram_total=sum(sram.values()),
                psram=psram, origen=origen,
                ms=1000 * total * ciclos_op / (mhz * 1e6))


# --- Enlace UART ESP32-CAM -> Heltec -----------------------------------------
UART_SOF = 0xA5
UART_TIPO_HORIZONTE = 0x01
UART_BAUD = 115200


def crc8(datos, poly=0x07, init=0x00):
    """CRC-8/ATM. Tabla de 256 B o bucle de 8 bits: en 8 bytes no se nota."""
    c = init
    for byte in datos:
        c ^= byte
        for _ in range(8):
            c = ((c << 1) ^ poly) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def pack_uart(payload, tipo=UART_TIPO_HORIZONTE):
    """SOF + tipo + len + payload + CRC8. El Heltec valida y reenvia por LoRa.

    El CRC no es opcional: son dos placas separadas con un cable, y una trama
    corrupta de 6 B se transmitiria igual y llegaria al gateway como un horizonte
    valido pero equivocado.
    """
    cuerpo = bytes([tipo, len(payload)]) + bytes(payload)
    return bytes([UART_SOF]) + cuerpo + bytes([crc8(cuerpo)])


def unpack_uart(trama):
    """Lado Heltec. Devuelve (tipo, payload) o None si la trama no valida."""
    if len(trama) < 4 or trama[0] != UART_SOF:
        return None
    tipo, n = trama[1], trama[2]
    if len(trama) != 4 + n or crc8(trama[1:-1]) != trama[-1]:
        return None
    return tipo, bytes(trama[3:-1])


# --- Almacenamiento en la microSD del ESP32-CAM ------------------------------
CABECERA_SD = 12        # timestamp DS3231 (4) + contador (2) + th (1) + inliers (1) + flags/CRC (4)

POLITICAS_SD = ("recta", "perfil", "grilla", "grilla+jpeg", "raw565")


def bytes_por_registro(politica, gh=GRID_H, gw=GRID_W, jpeg_kb=14):
    """Bytes en la SD por captura, segun que se decida conservar.

    recta        solo el resultado. Sirve para la serie temporal, no para recalibrar.
    perfil       + el perfil y(x) por columna: permite ver si el terreno no es recto.
    grilla       + el indice gh x gw entero: permite RE-CORRER el detector offline con
                 otros umbrales, otro indice y otro metodo. Es el que hace util a la SD.
    grilla+jpeg  + un JPEG QVGA de la escena: verdad visual para el informe.
    raw565       el frame crudo. Solo para depurar el driver de camara.
    """
    if politica == "recta":
        return CABECERA_SD + 6
    if politica == "perfil":
        return CABECERA_SD + 6 + gw * 2      # y_px en Q14.2 por columna
    if politica == "grilla":
        return CABECERA_SD + 6 + gw * 2 + gh * gw
    if politica == "grilla+jpeg":
        return CABECERA_SD + 6 + gw * 2 + gh * gw + int(jpeg_kb * 1024)
    if politica == "raw565":
        return CABECERA_SD + SRC_W * SRC_H * 2
    raise ValueError(f"politica desconocida: {politica}")


def presupuesto_sd(politica, gh=GRID_H, gw=GRID_W, capturas_dia=1440,
                   tarjeta_gb=8, jpeg_kb=14, sector=512):
    """Cuanto dura la tarjeta. Redondea cada registro al sector de FAT: con
    registros de decenas de bytes el desperdicio domina, y conviene agrupar
    varias capturas por escritura (bloque) en vez de un fwrite por captura."""
    b = bytes_por_registro(politica, gh, gw, jpeg_kb)
    b_sector = math.ceil(b / sector) * sector
    por_dia = b * capturas_dia
    por_dia_sector = b_sector * capturas_dia
    cap = tarjeta_gb * 1000 ** 3
    return dict(politica=politica, bytes_registro=b, bytes_con_sector=b_sector,
                bytes_dia=por_dia, bytes_dia_sector=por_dia_sector,
                dias=cap / por_dia, dias_sector=cap / por_dia_sector,
                registros_por_sector=max(1, sector // b) if b <= sector else 1)


# --- Pipeline completo -------------------------------------------------------
def detectar(rgb888, gh=GRID_H, gw=GRID_W, indice="B-R", modo="box",
             metodo="umbral", th=None, th_contraste=TH_CONTRASTE, subcelda=True):
    """QVGA -> recta del horizonte. Devuelve todo lo intermedio para graficar."""
    rgb565 = ps.to_rgb565(rgb888)
    grid = reduce(rgb565, gh, gw, modo)
    idx_raw = indices(grid)[indice]
    idx, invertido = orientar(idx_raw)
    th_usado = otsu(idx) if th is None else int(th)

    y_celda, ok = horizon_profile(idx, th_usado, metodo, th_contraste, subcelda)
    x_px, y_px = a_pixeles(y_celda, ok, gh, gw)
    ajuste = fit_line_robust(x_px, y_px)

    if ajuste is None:
        recta, inliers, keep, pkt = None, 0, np.zeros(x_px.size, bool), b"\x00"
    else:
        m, b, inliers, keep = ajuste
        recta, pkt = (m, b), pack(m, b, inliers)

    return dict(rgb=rgb888, rgb565=rgb565, grid=grid, idx=idx, invertido=invertido,
                th=th_usado, y_celda=y_celda, ok=ok, x_px=x_px, y_px=y_px,
                keep=keep, recta=recta, inliers=inliers, pkt=pkt,
                gh=gh, gw=gw, indice=indice, modo=modo, metodo=metodo)


def evaluar_grillas(rgb888, referencia, grillas, indice="B-R", modo="box",
                    metodo="umbral", th=None, subcelda=True):
    """Corre el pipeline en varias grillas y compara contra la recta de referencia."""
    filas = []
    for gh, gw in grillas:
        d = detectar(rgb888, gh, gw, indice, modo, metodo, th, subcelda=subcelda)
        e = error_recta(d["recta"], referencia)
        c = costo_esp32(gh, gw, metodo, th is None)
        filas.append(dict(gh=gh, gw=gw, celdas=gh * gw, px_por_fila=SRC_H / gh,
                          bytes_grilla=gh * gw, columnas_validas=int(d["ok"].sum()),
                          inliers=d["inliers"], recta=d["recta"],
                          mae=e["mae"], emax=e["emax"], ang=e["ang"],
                          ram=c["ram_total"], ms=c["ms"]))
    return filas


def barrido_escenas(grillas, n=16, ruido=3.5, noche=False, fuego=False, seed=0, **kw):
    """MAE promedio sobre n escenas sinteticas con horizonte aleatorio.

    Una sola escena no alcanza para decidir el tamano de grilla: el error de
    cuantizacion depende de donde cae el horizonte dentro de la celda.
    """
    rng = np.random.default_rng(seed)
    acum = {g: [] for g in grillas}
    fallas = {g: 0 for g in grillas}
    for i in range(n):
        m = float(rng.uniform(-0.25, 0.25))
        b = float(rng.uniform(80, 165))
        rgb, gt = synthetic_scene(m, b, seed=1000 + i, ruido=ruido,
                                  noche=noche, fuego=fuego)
        for gh, gw in grillas:
            d = detectar(rgb, gh, gw, **kw)
            if d["recta"] is None:
                fallas[(gh, gw)] += 1
                continue
            acum[(gh, gw)].append(error_recta(d["recta"], gt)["mae"])
    filas = []
    for (gh, gw) in grillas:
        v = np.array(acum[(gh, gw)]) if acum[(gh, gw)] else np.array([np.nan])
        c = costo_esp32(gh, gw, kw.get("metodo", "umbral"), kw.get("th") is None)
        filas.append(dict(gh=gh, gw=gw, celdas=gh * gw, px_por_fila=SRC_H / gh,
                          mae=float(np.nanmean(v)), p95=float(np.nanpercentile(v, 95)),
                          peor=float(np.nanmax(v)), fallas=fallas[(gh, gw)],
                          ram=c["ram_total"], ms=c["ms"]))
    return filas


# --- Reporte -----------------------------------------------------------------
def run(path=None, gh=GRID_H, gw=GRID_W, indice="B-R", metodo="umbral"):
    rgb888, gt = load_qvga(path)
    d = detectar(rgb888, gh, gw, indice, metodo=metodo)

    fuente = str(path) if path else "sintetica (horizonte conocido)"
    print(f"\n=== {fuente} · grilla {gh}x{gw} · indice {indice} · metodo {metodo} ===")
    print(f"bloque {SRC_H//gh}x{SRC_W//gw} px  reciproco {reciproco((SRC_H//gh)*(SRC_W//gw))}>>{BOX_SHIFT}")
    print(f"umbral Otsu {d['th']}   indice invertido: {d['invertido']}")
    print(f"columnas validas {int(d['ok'].sum())}/{gw}   inliers {d['inliers']}")

    if d["recta"] is None:
        print("SIN HORIZONTE -> trama de 1 B (early exit)")
    else:
        m, b = d["recta"]
        print(f"recta y = {m:+.4f} x {b:+.2f}   inclinacion {math.degrees(math.atan(m)):+.2f} deg")
        m_rx, b_rx, _ = unpack(d["pkt"])
        e = error_recta((m, b), (m_rx, b_rx))
        print(f"perdida por cuantizacion Q8.8/Q14.2: MAE {e['mae']:.3f} px")

    if gt:
        e = error_recta(d["recta"], gt)
        print(f"\nvs ground truth y = {gt[0]:+.4f} x {gt[1]:+.2f}")
        print(f"MAE {e['mae']:.2f} px   err max {e['emax']:.2f} px   angulo {e['ang']:.2f} deg")

    c = costo_esp32(gh, gw, metodo)
    print(f"\nESP32-CAM: {c['total_ops']:,} ops  ~{c['ms']:.1f} ms @240 MHz  "
          f"{c['divisiones']} divisiones")
    print(f"           SRAM {c['ram_total']} B + PSRAM {c['psram']:,} B (framebuffer del driver)")

    trama = pack_uart(d["pkt"])
    base = SRC_W * SRC_H * 2
    print(f"\nUART -> Heltec: {trama.hex(' ')}  ({len(trama)} B con SOF/len/CRC8)")
    print(f"LoRa: {len(d['pkt'])} B utiles  airtime SF{ps.LORA_SF} "
          f"{ps.lora_airtime(len(d['pkt']))*1000:.1f} ms")
    print(f"      compresion {base:,} B (QVGA RGB565) -> {len(d['pkt'])} B = {base/len(d['pkt']):,.0f}x")

    print(f"\nSD del ESP32-CAM (8 GB, 1440 capturas/dia):")
    for pol in POLITICAS_SD:
        s = presupuesto_sd(pol, gh, gw)
        dias = s["dias_sector"]
        dur = f"{dias/365:.1f} anos" if dias > 730 else f"{dias:.0f} dias"
        print(f"  {pol:<12} {s['bytes_registro']:>8,} B/captura  "
              f"{s['bytes_dia_sector']/1e6:>8.2f} MB/dia  -> {dur}")
    return d, gt


# --- Self-check --------------------------------------------------------------
def self_check():
    rgb, gt = synthetic_scene(m=-0.08, b=118.0)
    d = detectar(rgb, 120, 16)
    assert d["recta"] is not None, "no detecto horizonte en escena sintetica"
    e = error_recta(d["recta"], gt)
    assert e["mae"] < 2.0, f"MAE {e['mae']:.2f} px, esperado < 2"
    assert e["ang"] < 0.6, f"error angular {e['ang']:.2f} deg, esperado < 0.6"

    # sub-celda: en grilla gruesa tiene que ganarle a redondear al centro de celda
    con = error_recta(detectar(rgb, 16, 16, subcelda=True)["recta"], gt)["mae"]
    sin = error_recta(detectar(rgb, 16, 16, subcelda=False)["recta"], gt)["mae"]
    assert con < sin, f"sub-celda {con:.2f} px no mejora contra {sin:.2f} px"

    # a igual presupuesto de celdas (960), mas filas tiene que ganarle a mas columnas
    alto, ancho = barrido_escenas([(60, 16), (15, 64)], n=6)
    assert alto["mae"] * 2 < ancho["mae"], \
        f"60x16 {alto['mae']:.2f} px no gano claro a 15x64 {ancho['mae']:.2f} px"

    # BOX aguanta ruido, la decimacion no (es el argumento para promediar el bloque)
    box_r = barrido_escenas([(120, 16)], n=6, ruido=12.0, modo="box")[0]["mae"]
    dec_r = barrido_escenas([(120, 16)], n=6, ruido=12.0, modo="decim")[0]["mae"]
    assert box_r * 3 < dec_r, f"BOX {box_r:.2f} px vs decimacion {dec_r:.2f} px con ruido"

    # reduccion
    g = reduce(ps.to_rgb565(rgb), 120, 16)
    assert g.shape == (120, 16, 3), g.shape
    assert reduce(ps.to_rgb565(rgb), 120, 16, "decim").shape == (120, 16, 3)
    try:
        reduce(ps.to_rgb565(rgb), 7, 16)
    except ValueError:
        pass
    else:
        raise AssertionError("acepto una grilla que no divide exacto")

    # otsu sobre un histograma bimodal claro
    # otsu devuelve el nivel mas alto de la clase baja: "<= th" es suelo
    bim = np.concatenate([np.full(500, 20), np.full(500, 200)]).reshape(50, 20)
    assert 20 <= otsu(bim) < 200, otsu(bim)

    # orientacion automatica: escena invertida debe dar la misma recta
    d_gris = detectar(rgb, 120, 16, indice="gris")
    assert d_gris["recta"] is not None

    # ajuste exacto sobre puntos exactos
    x = np.arange(16, dtype=float) * 20
    m0, b0 = -0.05, 130.0
    assert fit_line(x, m0 * x + b0) == (m0, b0) or \
        abs(fit_line(x, m0 * x + b0)[0] - m0) < 1e-9

    # rechazo de outliers
    y = m0 * x + b0
    y[3], y[11] = 5.0, 230.0
    m1, b1, n1, _ = fit_line_robust(x, y)
    assert n1 <= 14 and error_recta((m1, b1), (m0, b0))["mae"] < 4.0, \
        f"outliers no rechazados: MAE {error_recta((m1,b1),(m0,b0))['mae']:.2f}"

    # paquete
    pkt = pack(-0.08, 118.0, 15)
    assert len(pkt) == 6, len(pkt)
    mr, br, nr = unpack(pkt)
    assert abs(mr + 0.08) < 1 / 256 and abs(br - 118.0) < 0.25 and nr == 15

    # trama UART al Heltec: ida y vuelta, y deteccion de corrupcion
    trama = pack_uart(pkt)
    assert len(trama) == len(pkt) + 4, len(trama)
    assert unpack_uart(trama) == (UART_TIPO_HORIZONTE, pkt)
    for i in range(1, len(trama)):                     # un bit dado vuelta en cualquier byte
        malo = bytearray(trama); malo[i] ^= 0x01
        assert unpack_uart(bytes(malo)) is None, f"CRC no detecto el error en el byte {i}"

    # presupuesto de SD: guardar la grilla tiene que seguir entrando en la tarjeta
    g = presupuesto_sd("grilla", 120, 16, capturas_dia=1440, tarjeta_gb=8)
    assert g["dias_sector"] > 365, f"la grilla llena la SD en {g['dias_sector']:.0f} dias"
    assert presupuesto_sd("raw565")["dias_sector"] < g["dias_sector"], "raw no puede durar mas"
    assert bytes_por_registro("recta") < bytes_por_registro("perfil") \
        < bytes_por_registro("grilla") < bytes_por_registro("grilla+jpeg")

    # el framebuffer es PSRAM, no SRAM: no confundir los dos presupuestos
    cfb = costo_esp32(120, 16, origen="fb")
    cst = costo_esp32(120, 16, origen="stream")
    assert cfb["psram"] == SRC_W * SRC_H * 2 and cst["psram"] == 0
    assert cfb["ram_total"] == cst["ram_total"] < 16 * 1024, "la SRAM del pipeline se fue de rango"

    # escena nocturna con fuego: los indices de azul mueren, queda gris + gradiente
    rgb_n, gt_n = synthetic_scene(m=0.05, b=105.0, noche=True, fuego=True, seed=3)
    assert detectar(rgb_n, 120, 16, indice="B-R")["recta"] is None, \
        "B-R no deberia enganchar de noche (sin contraste de azul)"
    d_n = detectar(rgb_n, 120, 16, indice="gris", metodo="grad")
    assert d_n["recta"] is not None, "escena nocturna sin deteccion"
    assert error_recta(d_n["recta"], gt_n)["mae"] < 8.0, error_recta(d_n["recta"], gt_n)

    run()
    print("\nself-check OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("imagen", nargs="?", type=Path)
    ap.add_argument("--grid", default=f"{GRID_H}x{GRID_W}", help="filas x columnas")
    ap.add_argument("--indice", default="B-R", choices=list(indices(np.zeros((1, 1, 3), np.uint8))))
    ap.add_argument("--metodo", default="umbral", choices=("umbral", "grad"))
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        self_check()
    elif a.imagen and not a.imagen.exists():
        sys.exit(f"no existe: {a.imagen}")
    else:
        gh, gw = (int(v) for v in a.grid.lower().split("x"))
        run(a.imagen, gh, gw, a.indice, a.metodo)
