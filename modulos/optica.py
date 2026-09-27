#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""Optica y alcance del nodo Flamara: que lente y resolucion alcanzan para 1-10 km.

Porque existe: el GSD decide si el humo ocupa pixeles o desaparece. Con la lente
stock de 3.6 mm una columna de 10 m a 5 km ocupa ~3 px nativos y <1 px si se
reduce a QVGA: fisicamente indetectable. Este modulo calcula GSD, FOV, pixeles
sobre el blanco, tiles, nodos, oclusion por dosel/curvatura y sensibilidad
monocular, para elegir lente y resolucion antes de comprar hardware.

Convencion: funciones "NODO" usan solo enteros/aritmetica simple (correrian en el
ESP32-S3); funciones "BACKEND" usan float y corren en el servidor.

Uso:
    ./optica.py --self-check
"""

import argparse
import math

import numpy as np

# --- Sensores (formato 1/4", datos de investigacion_distancia_volumen.md) ---
# OV2640: 1600x1200, p = 2.2 um. OV5640: 2592x1944, p = 1.4 um.
SENSORES = {
    "OV2640": dict(w=1600, h=1200, p_um=2.2),
    "OV5640": dict(w=2592, h=1944, p_um=1.4),
}
FOCALES_MM = [3.6, 8.0, 12.0, 16.0, 25.0]
# Capturas tipicas (ancho x alto). El pixel efectivo crece al submuestrear:
# p_ef = p * (W_nativo / W_captura).
CAPTURAS = {
    "UXGA": (1600, 1200),
    "SVGA": (800, 600),
    "QVGA": (320, 240),
    "96x96": (96, 96),
}
R_E_PRIMA_M = 8495e3  # Tierra equivalente 4/3 (refraccion estandar)
PX_DETECT = 3.0   # umbral de deteccion (~Johnson: 3-4 px)
PX_CARACT = 8.0   # umbral de caracterizacion (morfologia humo vs nube)

# --- NODO: geometria de un pixel ---------------------------------------------
# (aritmetica simple; en firmware seria con enteros y tabla)


def gsd(p_um, D_m, f_mm):
    """NODO: metros de terreno por pixel. GSD = p*D/f."""
    return p_um * 1e-6 * D_m / (f_mm * 1e-3)


def ifov_mrad(p_um, f_mm):
    """NODO: resolucion angular de un pixel, en mrad. ~= p/f."""
    return p_um * 1e-6 / (f_mm * 1e-3) * 1e3


def fov_grados(sensor, f_mm):
    """NODO: HFOV/VFOV en grados. 2*atan(W*p/2f)."""
    s = SENSORES[sensor]
    w_m = s["w"] * s["p_um"] * 1e-6
    h_m = s["h"] * s["p_um"] * 1e-6
    f = f_mm * 1e-3
    return (2 * math.degrees(math.atan(w_m / 2 / f)),
            2 * math.degrees(math.atan(h_m / 2 / f)))


def pixel_efectivo_um(sensor, captura):
    """NODO: pixel efectivo al capturar por debajo del nativo (binning)."""
    s = SENSORES[sensor]
    w_cap, _ = CAPTURAS[captura]
    return s["p_um"] * (s["w"] / w_cap)


def pixeles_blanco(ancho_m, p_ef_um, D_m, f_mm):
    """NODO: pixeles que ocupa un blanco de ancho_m a distancia D."""
    return ancho_m / gsd(p_ef_um, D_m, f_mm)


# --- BACKEND: cobertura, energia, oclusion, sensibilidad ---------------------


def nodos_360(hfov_deg, doble=False):
    """BACKEND: nodos para cubrir 360 grados (simple o doble/triangulacion)."""
    n = math.ceil(360.0 / hfov_deg)
    return 2 * n if doble else n


def tiles_franja(hfov_deg, vfov_deg, alto_franja_deg=4.0, tile_px=96,
                 w_cap=1600, h_cap=1200):
    """BACKEND: tiles de 96x96 para cubrir la franja de horizonte.

    La franja util es una banda de alto_franja_deg sobre el horizonte; el resto
    del cielo/suelo no se infiere. Devuelve n_tiles y fraccion del frame.
    """
    px_por_grado_h = w_cap / hfov_deg
    px_por_grado_v = h_cap / vfov_deg
    ancho_px = w_cap
    alto_px = min(h_cap, alto_franja_deg * px_por_grado_v)
    n = math.ceil(ancho_px / tile_px) * max(1, math.ceil(alto_px / tile_px))
    _ = px_por_grado_h
    return n, (ancho_px * alto_px) / (w_cap * h_cap)


def caida_curvatura_m(D_m):
    """NODO: caida de la superficie por curvatura: D^2/2R'e."""
    return D_m ** 2 / (2 * R_E_PRIMA_M)


def rayo_sobre_dosel_m(H_cam, D_m, d_m=500.0):
    """BACKEND: altura del rayo sobre el suelo a d_m antes del foco (tierra plana).

    H_rayo(d) ~= H_cam * d / D. Si < H_dosel (~10 m), el monte ocluye la base.
    """
    return H_cam * d_m / D_m


def altura_min_visible_m(H_cam, D_m, h_dosel=10.0, d_borde_m=500.0):
    """BACKEND: altura minima de columna visible a D tras el dosel cercano.

    Recta camara->cima del dosel en el borde (a d_borde_m del foco) extendida
    hasta D. Sin curvatura (a <10 km domina la vegetacion, no la caida).
    """
    h_borde = h_dosel
    # pendiente desde la camara (altura H_cam) pasando por (D-d_borde, h_borde)
    m = (h_borde - H_cam) / (D_m - d_borde_m)
    return H_cam + m * D_m


def dmax_base_visible_m(H_cam, h_dosel=10.0, d_borde_m=500.0):
    """BACKEND: distancia max a la que aun se ve la base (h=0) tras el dosel.

    0 = H_cam + m*D con m como arriba -> D = H_cam*d_borde/(H_cam-h_dosel).
    Si H_cam <= h_dosel, la base nunca se ve (devuelve 0).
    """
    if H_cam <= h_dosel:
        return 0.0
    return H_cam * d_borde_m / (H_cam - h_dosel)


def sensibilidad_mono_m(D_m, H_cam, dtheta_rad):
    """BACKEND: error de distancia monocular: dD ~= D^2/H * dtheta."""
    return D_m ** 2 / H_cam * dtheta_rad


def tabla_gsd(sensor="OV2640", distancias=(1000, 2000, 5000, 10000)):
    """BACKEND: GSD [m/px] para cada focal x distancia (pixel nativo)."""
    p = SENSORES[sensor]["p_um"]
    out = np.zeros((len(FOCALES_MM), len(distancias)))
    for i, f in enumerate(FOCALES_MM):
        for j, D in enumerate(distancias):
            out[i, j] = gsd(p, D, f)
    return out


def self_check():
    # GSD de referencia del informe: OV2640 16 mm @ 5 km = 0.69 m/px
    assert abs(gsd(2.2, 5000, 16.0) - 0.6875) < 1e-9
    # OV5640 16 mm @ 10 km = 0.88 m/px
    assert abs(gsd(1.4, 10000, 16.0) - 0.875) < 1e-9
    # FOV: OV2640 16 mm -> HFOV ~12.5 deg
    hf, vf = fov_grados("OV2640", 16.0)
    assert abs(hf - 12.5) < 0.3, hf
    assert abs(vf - 9.4) < 0.3, vf
    # Caso critico del informe: humo 10 m, stock 3.6 mm, 5 km -> ~3.2 px
    assert abs(pixeles_blanco(10, 2.2, 5000, 3.6) - 3.27) < 0.05
    # Con 16 mm -> ~14.5 px
    assert abs(pixeles_blanco(10, 2.2, 5000, 16.0) - 14.5) < 0.2
    # Pixel efectivo QVGA: 5x el nativo en OV2640
    assert abs(pixel_efectivo_um("OV2640", "QVGA") - 11.0) < 1e-9
    # A QVGA el humo colapsa: 14.5/5 < 3 px -> indetectable
    assert pixeles_blanco(10, 11.0, 5000, 16.0) < PX_DETECT
    # Curvatura @ 10 km = 5.88 m
    assert abs(caida_curvatura_m(10000) - 5.88) < 0.05
    # Sensibilidad: H=20 m, D=5 km, 0.05 deg -> ~1090 m
    assert abs(sensibilidad_mono_m(5000, 20, math.radians(0.05)) - 1090) < 15
    # Oclusion: mastil 15 m, foco a 10 km, borde a 500 m -> rayo a 0.75 m
    assert abs(rayo_sobre_dosel_m(15, 10000) - 0.75) < 1e-9
    # Base visible con mastil 15 m y dosel 10 m: 1500 m
    assert abs(dmax_base_visible_m(15) - 1500) < 1e-9
    # Mastil bajo el dosel: base nunca visible
    assert dmax_base_visible_m(8) == 0.0
    # Nodos 360 con HFOV 12.5 -> 29 simples, 58 doble
    assert nodos_360(12.5) == 29 and nodos_360(12.5, doble=True) == 58
    # Tiles: franja angosta pide menos que el frame completo
    n, frac = tiles_franja(12.5, 9.4, alto_franja_deg=4.0)
    assert n > 0 and 0 < frac < 1.0
    print("optica self-check OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        self_check()
    else:
        ap.print_help()
