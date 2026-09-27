#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pillow"]
# ///
"""Serie temporal a 15 min: separar humo de nubes/sombras/iluminacion (Flamara).

Porque existe: con fotos cada 15 min la resta |I(t)-I(t-1)| da falsos positivos
(sol mueve sombras 3.75°, nubes se desplazan km). En vez de comparar contra la
foto anterior se compara contra un FONDO de referencia tomado con la misma
elevacion solar (+-2.5°), donde las sombras caen en el mismo lugar. Sobre ese
par (actual, fondo) corren tres tests baratos por celda; la alerta exige los
tres + persistencia en 2 capturas seguidas.

Que va donde (particion nodo/backend):
  DEL NODO (enteros, corre en ESP32 cada 15 min): reduccion BOX via
      horizonte_sim.reduce, gris, DWT Haar via pipeline_sim.dwt_haar2,
      canal oscuro + saturacion, los tres tests y persistencia.
  DEL BACKEND: barrido de umbrales (TPR/FPR) y guardado del banco de fondos.
      El nodo solo guarda UNA grilla de fondo por franja de 5° en la SD.

Uso:
    ./serie_temporal.py --self-check
"""

import argparse
import json
import math

import numpy as np

import horizonte_sim as hs
import pipeline_sim as ps

# --- Grilla de trabajo -------------------------------------------------------
GH, GW = 60, 16          # grilla reducida; DWT la baja a 30x8 superceldas
ANCHO_FRANJA = 5.0       # banco de fondos: una referencia cada 5° de elevacion

# --- Umbrales nominales (los fija el barrido; estos son el punto de partida) --
TH_TEX = 0.5             # r_actual < TH_TEX * r_fondo -> humo (pasa-bajos)
TH_DARK = 15             # dark_actual - dark_fondo > TH_DARK -> velo (0-255)
TH_SAT = 0.35            # saturacion < TH_SAT -> grisaceo (humo, no objeto colorido)


# --- 1. Elevacion solar minima (NODO: tabla + float; <0.1 ms en ESP32) ---------
def elevacion_solar(dia_anio, hora_utc, lat, lon):
    """Elevacion solar en grados. Algoritmo minimo tipo Cooper: declinacion +
    angulo horario con correccion de longitud. Precision ~+-1°, sobra para
    franjas de 5°. nb9 hace la suya en paralelo; se unifican despues."""
    decl = math.radians(-23.44 * math.cos(math.radians(360.0 / 365.0 * (dia_anio + 10))))
    lat_r = math.radians(lat)
    # hora solar: mediodia UTC + longitud/15 (este positivo)
    h_ang = math.radians(15.0 * (hora_utc - 12.0) + lon)
    s = (math.sin(lat_r) * math.sin(decl)
         + math.cos(lat_r) * math.cos(decl) * math.cos(h_ang))
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


def franja_elev(elev, ancho=ANCHO_FRANJA):
    """Indice de franja para el banco de fondos (5° por franja)."""
    return int(math.floor(elev / ancho))


# --- 2. Banco de fondos por franja solar (NODO guarda, BACKEND elige) ---------
def actualizar_fondo(banco, grilla, elev):
    """Suma la grilla a la franja que le toca; el fondo es el promedio de las
    capturas despejadas de esa franja. `banco` es dict franja -> [acum, n]."""
    f = franja_elev(elev)
    if f not in banco:
        banco[f] = [np.zeros_like(grilla, dtype=np.float64), 0]
    banco[f][0] += grilla.astype(np.float64)
    banco[f][1] += 1
    return f


def fondo_para(banco, elev):
    """Fondo promedio de la franja, o None si todavia no hay referencia."""
    f = franja_elev(elev)
    if f not in banco or banco[f][1] == 0:
        return None
    return (banco[f][0] / banco[f][1]).clip(0, 255).astype(np.uint8)


# --- Utilidades de grilla ----------------------------------------------------
def gris(grilla_rgb):
    """Luminancia entera 77R+150G+29B (igual que pipeline_sim, en grilla chica)."""
    R, G, B = (grilla_rgb[..., i].astype(np.int32) for i in range(3))
    return ((77 * R + 150 * G + 29 * B) >> 8).astype(np.int16)


def mascara_suelo(gh=GH, gw=GW, m=-0.08, b_px=118.0):
    """True debajo del horizonte. b_px en px QVGA; se escala a la grilla.
    (NODO: la recta viene del detector de horizonte_sim, aca parametrizada
    para las escenas sinteticas.)"""
    bh, bw = hs.SRC_H / gh, hs.SRC_W / gw
    yy, xx = np.mgrid[0:gh, 0:gw]
    y_horiz = (m * (xx + 0.5) * bw + b_px) / bh - 0.5
    return yy > y_horiz


def canal_oscuro(grilla_rgb):
    """Dark channel por celda: min(R,G,B). El humo mete airlight y lo levanta."""
    return grilla_rgb.min(axis=-1).astype(np.int16)


def saturacion(grilla_rgb):
    """Saturacion 0..1 por celda: (max-min)/max. Humo = gris = baja."""
    mx = grilla_rgb.max(axis=-1).astype(float)
    mn = grilla_rgb.min(axis=-1).astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0.0)
    return s


# --- 3a. Test de textura DWT (NODO) -------------------------------------------
def ratio_textura(g):
    """Energia de detalle / energia de aproximacion por supercelda 2x2.

    dwt_haar2 sobre grilla par GHxGW -> LL,LH,HL,HH de GH/2 x GW/2; cada
    supercelda cubre su bloque 2x2. El humo suaviza bordes (pasa-bajos):
    cae el numerador, el denominador casi no se mueve.
    Reusa pipeline_sim.dwt_haar2 (misma butterfly entera del firmware)."""
    c = ps.dwt_haar2(np.asarray(g, dtype=np.int16))
    h, w = c.shape[0] // 2, c.shape[1] // 2
    ll = c[:h, :w].astype(float) / 4.0       # la butterfly suma: dividir por 4
    det = (c[:h, w:].astype(float) ** 2 + c[h:, :w].astype(float) ** 2
           + c[h:, w:].astype(float) ** 2)
    return det / (ll ** 2 + 1.0)


def test_textura(g_ahora, g_fondo, th=TH_TEX):
    """True donde la textura cayo a menos de th veces el fondo."""
    rf = ratio_textura(g_fondo)
    ra = ratio_textura(g_ahora)
    with np.errstate(divide="ignore", invalid="ignore"):
        return (ra < th * rf) & (rf > 1e-9)


# --- 3b. Test de canal oscuro + saturacion bajo horizonte (NODO) ---------------
def test_velo(rgb_ahora, rgb_fondo, suelo, th_dark=TH_DARK, th_sat=TH_SAT):
    """True donde subio el dark (velo) Y la saturacion es baja, solo en suelo.
    Sobre el cielo el DCP no aplica: se enmascara."""
    d = canal_oscuro(rgb_ahora) - canal_oscuro(rgb_fondo)
    return (d > th_dark) & (saturacion(rgb_ahora) < th_sat) & suelo


# --- 3c. Persistencia + decision (NODO) ----------------------------------------
def test_triple(g_ahora, rgb_ahora, g_fondo, rgb_fondo, suelo,
                th_tex=TH_TEX, th_dark=TH_DARK, th_sat=TH_SAT):
    """AND de textura y velo, devuelto en grilla de superceldas (GH/2 x GW/2).
    El velo (a resolucion de celda) se reduce con max 2x2 a supercelda."""
    t = test_textura(g_ahora, g_fondo, th_tex)
    v = test_velo(rgb_ahora, rgb_fondo, suelo, th_dark, th_sat)
    h, w = t.shape
    v_super = v.reshape(h, 2, w, 2).max(axis=(1, 3))
    return t & v_super


def con_persistencia(cand, previo):
    """La alerta exige el candidato en 2 capturas consecutivas (15+15 min).
    Nubes/sombras se mueven entre capturas; el humo nascente persiste."""
    return cand & previo


# --- 4. Escenas sinteticas con ground truth (BACKEND: fija umbrales) -----------
def escena(tipo="fondo", seed=0, m=-0.08, b_px=118.0):
    """Genera (rgb QVGA uint8, hay_humo bool, mask_humo QVGA bool).

    fondo:       cielo + campo con textura (referencia despejada).
    sombra:      parche oscuro sobre el suelo, TEXTURA intacta (nube).
    iluminacion: ganancia global x1.25 (sol mas alto, sin humo).
    humo:        pluma difusa sobre el horizonte que suaviza el fondo + velo.
    """
    rng = np.random.default_rng(seed)
    base, _ = hs.synthetic_scene(m=m, b=b_px, seed=seed, nubes=False)
    base = base.astype(float)
    H, W = base.shape[:2]
    yy, xx = np.mgrid[0:H, 0:W].astype(float)
    yh = m * xx + b_px
    mask = np.zeros((H, W), bool)

    if tipo == "sombra":
        g = np.exp(-((xx - 110) ** 2) / 3600.0 - ((yy - 190) ** 2) / 900.0)
        base *= (1 - 0.45 * g)[..., None]
    elif tipo == "iluminacion":
        base *= 1.25
    elif tipo == "humo":
        # pluma: gaussiana ancha sobre el horizonte
        g = (np.exp(-((xx - 200) ** 2) / 3600.0)
             * np.exp(-np.clip(yh - yy, 0, None) ** 2 / 3600.0)
             * (yy < yh + 6))
        mask = g > 0.25
        # suavizado local (pasa-bajos) ANTES del ruido: el humo tapa el grano
        from numpy.lib.stride_tricks import sliding_window_view
        pad = np.pad(base, ((3, 3), (3, 3), (0, 0)), mode="edge")
        # ventana 7x7 por canal; el reshape (7,7,3)->(3,49) mezclaba canales
        blur = sliding_window_view(pad, (7, 7), axis=(0, 1)).mean(axis=(-1, -2))
        base = base * (1 - g[..., None]) + (0.4 * blur + 0.6 * 170) * g[..., None]
        base += rng.normal(0, 1.0, base.shape) * (1 - g[..., None])  # ruido solo fuera
        return np.clip(base, 0, 255).astype(np.uint8), True, mask
    base += rng.normal(0, 3.0, base.shape)
    return np.clip(base, 0, 255).astype(np.uint8), tipo == "humo", mask


def grilla_recortada(rgb):
    """QVGA -> grilla de trabajo GHxGW via el BOX del firmware (hs.reduce)."""
    return hs.reduce(ps.to_rgb565(rgb), GH, GW, modo="box")


def evaluar(tipos=("fondo", "sombra", "iluminacion", "humo"), n=8,
            th_tex=TH_TEX, th_dark=TH_DARK, th_sat=TH_SAT):
    """TPR (humo detectado) y FPR (no-humo con alerta) sobre sinteticas.

    El fondo de cada escena es el promedio de 3 fondos limpios (otra semilla),
    misma franja solar: asi se mide el test, no el ruido."""
    suelo_full = mascara_suelo()
    h, w = GH // 2, GW // 2
    suelo_super = suelo_full.reshape(h, 2, w, 2).max(axis=(1, 3))
    tp = fp = nt = nf = 0
    for i, tipo in enumerate(tipos):
        prev = np.zeros((h, w), bool)  # la persistencia no cruza de un evento a otro
        for k in range(n):
            rgb, hay, _ = escena(tipo, seed=100 * i + k)
            fondos = [grilla_recortada(escena("fondo", seed=9000 + j)[0]) for j in range(3)]
            rgb_f = np.stack(fondos).mean(axis=0).astype(np.uint8)
            g, gf = gris(grilla_recortada(rgb)), gris(rgb_f)
            cand = test_triple(g, grilla_recortada(rgb), gf, rgb_f, suelo_full,
                               th_tex, th_dark, th_sat)
            alerta = con_persistencia(cand, prev)
            prev = cand
            if k == 0:
                continue  # persistencia x2: la 1a captura no puede alertar
            det = bool(alerta[suelo_super].any())
            if hay:
                tp += det
                nt += 1
            else:
                fp += det
                nf += 1
    return dict(tpr=tp / max(nt, 1), fpr=fp / max(nf, 1))


def barrido_umbrales():
    """Barrido chico sobre sinteticas; devuelve dict listo para JSON.

    Si varios umbrales empatan en (TPR, FPR), el banco no los separa: se
    exporta el primero y `empatados` dice cuantos dan lo mismo.
    """
    mejor = None
    empatados = 0
    for th_tex in (0.3, 0.4, 0.5, 0.6):
        for th_dark in (10, 15, 20):
            for th_sat in (0.25, 0.35):
                m = evaluar(n=6, th_tex=th_tex, th_dark=th_dark, th_sat=th_sat)
                puntaje = (round(m["tpr"], 6), round(-m["fpr"], 6))
                if mejor is None or puntaje > mejor[0]:
                    mejor = (puntaje, th_tex, th_dark, th_sat, m)
                    empatados = 1
                elif puntaje == mejor[0]:
                    empatados += 1
    _, th_tex, th_dark, th_sat, m = mejor
    return dict(th_tex=th_tex, th_dark=th_dark, th_sat=th_sat,
                tpr=round(m["tpr"], 3), fpr=round(m["fpr"], 3),
                empatados=empatados,
                grilla=[GH, GW], franja_elev_grados=ANCHO_FRANJA,
                persistencia_capturas=2)


# --- Self-check --------------------------------------------------------------
def self_check():
    # sol: mediodia equinoccio en ecuador ~90°, medianoche < 0
    assert 85 < elevacion_solar(80, 12.0, 0.0, 0.0) <= 90, elevacion_solar(80, 12.0, 0.0, 0.0)
    assert elevacion_solar(80, 0.0, 0.0, 0.0) < -50
    # Chaco (-27.5°) mediodia invernal (~16 UTC) ~39°
    assert 34 < elevacion_solar(172, 16.0, -27.5, -58.8) < 44
    assert franja_elev(12.3) == 2 and franja_elev(12.3) != franja_elev(17.6)

    # banco: guarda y recupera promedio
    banco = {}
    g0 = np.full((GH, GW, 3), 100, np.uint8)
    actualizar_fondo(banco, g0, 12.0)
    actualizar_fondo(banco, g0, 13.0)
    assert fondo_para(banco, 12.5).mean() == 100
    assert fondo_para(banco, 40.0) is None

    # textura: el humo sintetico cae DENTRO de la pluma, la sombra no
    rgb_h, _, mask_h = escena("humo", seed=1)
    rgb_s, _, _ = escena("sombra", seed=1)
    rgb_f, _, _ = escena("fondo", seed=1)
    gh_, gs_, gf_ = (gris(grilla_recortada(r)) for r in (rgb_h, rgb_s, rgb_f))
    cob = mask_h.reshape(GH // 2, 2 * (hs.SRC_H // GH), GW // 2,
                         2 * (hs.SRC_W // GW)).mean(axis=(1, 3)) > 0.3
    d_h = ratio_textura(gh_) / (ratio_textura(gf_) + 1e-9)
    assert cob.sum() > 10, "pluma demasiado chica"
    assert (d_h[cob] < 0.6).mean() > 0.25, "humo no suaviza en la pluma"
    assert (d_h[~cob] < 0.6).mean() < 0.15, "caidas fuera de la pluma"
    rs_ = ratio_textura(gs_) / (ratio_textura(gf_) + 1e-9)
    assert (rs_ < 0.5).mean() < 0.08, "la sombra no debe caer como el humo"  # el test solo no decide: por eso es triple

    # velo: sube con humo, y solo bajo el horizonte
    suelo = mascara_suelo()
    assert suelo.any() and (~suelo).any()
    rgb_b = np.stack([grilla_recortada(escena("fondo", seed=9000 + j)[0])
                      for j in range(3)]).mean(axis=0).astype(np.uint8)
    v_h = test_velo(grilla_recortada(rgb_h), rgb_b, suelo)
    cob60 = mask_h.reshape(GH, 240 // GH, GW, 320 // GW).mean(axis=(1, 3)) > 0.3
    sel = cob60 & suelo
    assert sel.sum() > 5, "la pluma no pisa el suelo"
    assert v_h[sel].mean() > 0.5, "velo no detecta la base de la pluma"
    assert not v_h[~suelo].any(), "velo debe enmascarar el cielo"
    v_s = test_velo(grilla_recortada(rgb_s), rgb_b, suelo)
    assert v_s.mean() < v_h.mean(), "sombra no debe dar mas velo que humo"

    # persistencia: un destello aislado no alerta
    uno = np.zeros((GH // 2, GW // 2), bool)
    uno[5, 3] = True
    assert not con_persistencia(uno, np.zeros_like(uno)).any()
    assert con_persistencia(uno, uno).any()

    # punta a punta: humo se detecta, fondo/sombra/iluminacion casi no
    m = evaluar(n=6)
    assert m["tpr"] >= 0.75, m
    assert m["fpr"] <= 0.25, m

    u = barrido_umbrales()
    assert u["tpr"] >= 0.75 and u["fpr"] <= 0.25, u
    assert set(("th_tex", "th_dark", "th_sat", "tpr", "fpr")) <= set(u)
    print("tpr=%.2f fpr=%.2f umbrales=%s" % (m["tpr"], m["fpr"], u))
    print("\nself-check OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--barrido", action="store_true", help="imprime umbrales JSON")
    a = ap.parse_args()
    if a.self_check:
        self_check()
    elif a.barrido:
        print(json.dumps(barrido_umbrales(), indent=2))
    else:
        ap.print_help()
