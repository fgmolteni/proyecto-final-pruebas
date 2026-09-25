#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Viabilidad de una red neuronal tiny en el nodo Flamara (Freenove ESP32-S3 WROOM).

Modelo ANALITICO: calcula MACs, parametros, memoria pico de activaciones, latencia
y energia de variantes de MobileNetV1 sin entrenar nada y sin instalar TensorFlow.
Sirve para decidir QUE modelo entrenar antes de gastar en datos y entrenamiento.

Anclaje: el ejemplo `person_detection` de tflite-micro es MobileNetV1 alpha=0.25,
entrada 96x96x1 en escala de grises, int8, ~250 kB de flash. Espressif publica su
latencia medida con ESP-NN:

    ESP32    @240 MHz   4084 ms sin ESP-NN  ->   380 ms con ESP-NN
    ESP32-S3 @240 MHz   2300 ms             ->    54 ms
    ESP32-C3 @160 MHz   3355 ms             ->   426 ms
    ESP32-P4 @360 MHz   1395 ms             ->    73 ms

De ahi sale un ns/MAC para cada chip y con eso se extrapola a otras configuraciones.
Es una regla de tres sobre UN punto medido: sirve para comparar variantes entre si,
no como numero absoluto. Verificar contra el hardware antes de comprometer el diseno.

La clave del proyecto: el nodo saca UNA foto cada varios minutos, no video. 380 ms
son 2.6 fps (inutil para video) pero irrelevantes para una captura cada 15 minutos.
El presupuesto real es ENERGIA POR CAPTURA, no latencia.

Uso:
    ./nn_esp32.py                 # reporte de las variantes candidatas
    ./nn_esp32.py --self-check
"""

import argparse
import math

# --- Anclaje medido ----------------------------------------------------------
ANCLA = dict(nombre="person_detection", alpha=0.25, res=96, canales=1, clases=2)
LATENCIA_MEDIDA_MS = {"esp32": 380, "esp32s3": 54, "esp32c3": 426, "esp32p4": 73}
MHZ = {"esp32": 240, "esp32s3": 240, "esp32c3": 160, "esp32p4": 360}

# --- Hardware del nodo: Freenove ESP32-S3 WROOM (ESP32-S3-WROOM-1 N8R8 + OV2640) ---
# https://github.com/Freenove/Freenove_ESP32_S3_WROOM_Board  -- sin esquematico publico.
# ponytail: los consumos son estimados de placas equivalentes, NO medidos en esta.
V_ALIM = 5.0
MA_ACTIVO = 180.0        # S3 @240 MHz + PSRAM + OV2640 capturando, sin WiFi: 100-200 mA
MA_SLEEP = 6.0           # deep sleep de la PLACA: PWDN y RESET del OV2640 no llegan a
                         # ningun GPIO (-1 en camera_pins.h), la camara no se apaga por
                         # software; suman LDO, CH343 y LED de power. MEDIR.
MA_SLEEP_MOSFET = 1.5    # cortando el riel de la camara; quedan LDO + CH343 + LED. MEDIR.
SRAM_LIBRE_KB = 250      # de 512 kB internos, sin WiFi (el enlace es el Heltec) y con el
                         # framebuffer en PSRAM. MEDIR: heap_caps_get_largest_free_block
PSRAM_KB = 8 * 1024      # octal; GPIO35-37 quedan tomados por la PSRAM
FLASH_KB = 8 * 1024      # la variante roja N16R8 trae 16 MB

MS_CAMARA = 400.0        # encendido + convergencia AEC/AGC + captura. MEDIR.

# --- MobileNetV1: especificacion por capa ------------------------------------
# (tipo, filtros_base, stride).  "conv" = convolucion 3x3 estandar
#                               "dw"   = separable en profundidad (dw 3x3 + pw 1x1)
MOBILENET_V1 = [
    ("conv", 32, 2),
    ("dw", 64, 1),
    ("dw", 128, 2), ("dw", 128, 1),
    ("dw", 256, 2), ("dw", 256, 1),
    ("dw", 512, 2),
    ("dw", 512, 1), ("dw", 512, 1), ("dw", 512, 1), ("dw", 512, 1), ("dw", 512, 1),
    ("dw", 1024, 2), ("dw", 1024, 1),
]


def canales(base, alpha):
    """MobileNet escala los filtros por alpha, con piso de 8."""
    return max(8, int(base * alpha))


def perfil(alpha=0.25, res=96, canales_in=1, clases=2, corte=None):
    """Cuenta MACs, parametros y activaciones capa por capa.

    corte=None      red completa: global average pool + capa densa -> clases
    corte=k         se trunca despues de la capa k y se le pega una cabeza 1x1
                    (estilo FOMO): la salida es una GRILLA de clases, no un escalar.

    Devuelve dict con la lista de capas y los totales.
    """
    if corte is not None and not 0 < corte <= len(MOBILENET_V1):
        raise ValueError(f"corte fuera de rango: {corte}")

    capas = []
    h = w = res
    c_in = canales_in
    cuerpo = MOBILENET_V1[:corte] if corte else MOBILENET_V1

    for i, (tipo, base, stride) in enumerate(cuerpo):
        c_out = canales(base, alpha)
        act_in = h * w * c_in                       # int8: 1 byte por elemento
        h, w = math.ceil(h / stride), math.ceil(w / stride)
        act_out = h * w * c_out

        if tipo == "conv":
            macs = h * w * c_in * c_out * 9
            par = 9 * c_in * c_out + c_out
        else:                                        # depthwise + pointwise
            macs = h * w * c_in * 9 + h * w * c_in * c_out
            par = 9 * c_in + c_in + c_in * c_out + c_out

        capas.append(dict(i=i, tipo=tipo, salida=(h, w, c_out), macs=macs,
                          params=par, act_in=act_in, act_out=act_out,
                          pico=act_in + act_out))
        c_in = c_out

    if corte is None:                                # cabeza de clasificacion
        macs = c_in * clases
        par = c_in * clases + clases
        capas.append(dict(i=len(cuerpo), tipo="pool+fc", salida=(1, 1, clases),
                          macs=macs, params=par, act_in=h * w * c_in,
                          act_out=clases, pico=h * w * c_in + clases))
        grilla = None
    else:                                            # cabeza FOMO: 1x1 -> clases
        macs = h * w * c_in * clases
        par = c_in * clases + clases
        capas.append(dict(i=len(cuerpo), tipo="cabeza 1x1", salida=(h, w, clases),
                          macs=macs, params=par, act_in=h * w * c_in,
                          act_out=h * w * clases, pico=h * w * (c_in + clases)))
        grilla = (h, w)

    total_macs = sum(c["macs"] for c in capas)
    total_par = sum(c["params"] for c in capas)
    pico = max(c["pico"] for c in capas)
    return dict(alpha=alpha, res=res, canales_in=canales_in, clases=clases,
                corte=corte, grilla=grilla, capas=capas,
                macs=total_macs, params=total_par,
                flash_kb=total_par / 1024,           # int8: 1 byte por parametro
                pico_act_kb=pico / 1024)


# --- Latencia y energia ------------------------------------------------------
def ns_por_mac(chip="esp32"):
    """Calibra ns/MAC contra el unico punto medido (person_detection + ESP-NN)."""
    p = perfil(ANCLA["alpha"], ANCLA["res"], ANCLA["canales"], ANCLA["clases"])
    return LATENCIA_MEDIDA_MS[chip] * 1e6 / p["macs"]


def latencia_ms(p, chip="esp32", en_psram=False):
    """Extrapola la latencia por MACs. en_psram: la arena externa cuesta ~25 % mas."""
    ms = p["macs"] * ns_por_mac(chip) / 1e6
    return ms * (1.25 if en_psram else 1.0)


def donde_entra(p, sram_kb=SRAM_LIBRE_KB, psram_kb=PSRAM_KB, flash_kb=FLASH_KB):
    """Veredicto de memoria. La arena de TFLM reusa buffers: el pico se estima como
    el mayor (entrada + salida) de una capa, mas un 30 % de scratch y persistentes."""
    arena_kb = p["pico_act_kb"] * 1.3
    if p["flash_kb"] > flash_kb:
        return "NO ENTRA (flash)", arena_kb
    if arena_kb <= sram_kb:
        return "SRAM", arena_kb
    if arena_kb <= psram_kb:
        return "PSRAM", arena_kb
    return "NO ENTRA (arena)", arena_kb


def energia_mj(ms, ma=MA_ACTIVO, v=V_ALIM):
    """Energia de un tramo activo, en milijoules."""
    return v * ma / 1000.0 * ms


def energia_captura(p=None, chip="esp32", ms_camara=MS_CAMARA, ms_clasico=3.1,
                    p_disparo=1.0, en_psram=False):
    """Energia de UNA captura con la cascada: camara -> filtro clasico -> red.

    p_disparo: fraccion de capturas en las que el filtro clasico escala a la red.
    Con p_disparo=1 la red corre siempre (sin cascada).
    """
    e_cam = energia_mj(ms_camara)
    e_cla = energia_mj(ms_clasico)
    ms_nn = latencia_ms(p, chip, en_psram) if p else 0.0
    e_nn = energia_mj(ms_nn) * p_disparo
    return dict(ms_camara=ms_camara, ms_clasico=ms_clasico, ms_nn=ms_nn,
                e_camara=e_cam, e_clasico=e_cla, e_nn=e_nn,
                total=e_cam + e_cla + e_nn,
                ms_total=ms_camara + ms_clasico + ms_nn * p_disparo)


def autonomia(e_captura_mj, capturas_dia=96, ma_sleep=MA_SLEEP, v=V_ALIM,
              panel_wh_dia=4.0, bateria_wh=9.25):
    """Balance diario. bateria_wh: 18650 de 2500 mAh a 3.7 V por defecto."""
    e_capturas_j = e_captura_mj * capturas_dia / 1000.0
    p_sleep_w = v * ma_sleep / 1000.0
    e_sleep_j = p_sleep_w * 86400.0
    total_j = e_capturas_j + e_sleep_j
    total_wh = total_j / 3600.0
    return dict(capturas_j=e_capturas_j, sleep_j=e_sleep_j, total_j=total_j,
                total_wh=total_wh, panel_wh_dia=panel_wh_dia,
                margen=panel_wh_dia / total_wh if total_wh else float("inf"),
                dias_sin_sol=bateria_wh / total_wh if total_wh else float("inf"),
                frac_sleep=e_sleep_j / total_j if total_j else 0.0)


# --- Variantes candidatas ----------------------------------------------------
def candidatos():
    """Las configuraciones que vale la pena comparar para este nodo."""
    return [
        ("clasificar 96x96 gris a=0.25 (ancla)", dict(alpha=0.25, res=96, canales_in=1)),
        ("clasificar 128x128 RGB a=0.35",        dict(alpha=0.35, res=128, canales_in=3)),
        ("clasificar 96x96 gris a=0.35",         dict(alpha=0.35, res=96, canales_in=1)),
        ("clasificar 96x96 RGB  a=0.25",         dict(alpha=0.25, res=96, canales_in=3)),
        ("clasificar 64x64 gris a=0.25",         dict(alpha=0.25, res=64, canales_in=1)),
        ("clasificar 160x160 gris a=0.25",       dict(alpha=0.25, res=160, canales_in=1)),
        ("FOMO 96x96 gris a=0.25 -> 12x12",      dict(alpha=0.25, res=96, canales_in=1, corte=6)),
        ("FOMO 96x96 gris a=0.35 -> 12x12",      dict(alpha=0.35, res=96, canales_in=1, corte=6)),
        ("FOMO 160x160 gris a=0.25 -> 20x20",    dict(alpha=0.25, res=160, canales_in=1, corte=6)),
        ("FOMO 96x96 gris a=0.25 -> 24x24",      dict(alpha=0.25, res=96, canales_in=1, corte=4)),
    ]


# --- Reporte -----------------------------------------------------------------
def run(chip="esp32s3", capturas_dia=96, p_disparo=0.1):
    p_ancla = perfil(**{k: ANCLA[k] for k in ("alpha", "res")},
                     canales_in=ANCLA["canales"], clases=ANCLA["clases"])
    print(f"\n=== Anclaje: {ANCLA['nombre']} en {chip} ===")
    print(f"MobileNetV1 alpha={ANCLA['alpha']} {ANCLA['res']}x{ANCLA['res']}x{ANCLA['canales']} int8")
    print(f"{p_ancla['macs']/1e6:.2f} MMAC   {p_ancla['params']/1024:.0f} kB de pesos   "
          f"(el modelo real de tflite-micro pesa ~250 kB)")
    for c in LATENCIA_MEDIDA_MS:
        print(f"  {c:<9} {LATENCIA_MEDIDA_MS[c]:>5} ms medidos @ {MHZ[c]} MHz  ->  "
              f"{ns_por_mac(c):.1f} ns/MAC  ({1e3/ns_por_mac(c):.1f} MMAC/s)")

    print(f"\n=== Variantes en {chip} ===")
    print(f"{'variante':<38}{'MMAC':>8}{'pesos kB':>10}{'arena kB':>10}"
          f"{'ms':>8}{'mJ':>8}   memoria")
    print("-" * 100)
    for nombre, kw in candidatos():
        p = perfil(**kw)
        donde, arena = donde_entra(p)
        ms = latencia_ms(p, chip, en_psram=(donde == "PSRAM"))
        print(f"{nombre:<38}{p['macs']/1e6:>8.2f}{p['flash_kb']:>10.0f}{arena:>10.0f}"
              f"{ms:>8.0f}{energia_mj(ms):>8.0f}   {donde}")

    print(f"\n=== Energia por captura (cascada, p_disparo={p_disparo:.0%}) ===")
    p = perfil(alpha=0.25, res=96, canales_in=3)       # el modelo elegido para D-Fire
    for pd, etiqueta in ((1.0, "red siempre"), (p_disparo, f"cascada {p_disparo:.0%}")):
        e = energia_captura(p, chip, p_disparo=pd)
        print(f"{etiqueta:<16} camara {e['e_camara']:>6.0f} mJ + clasico {e['e_clasico']:>5.1f} mJ"
              f" + red {e['e_nn']:>6.0f} mJ = {e['total']:>7.0f} mJ")

    print(f"\n=== Autonomia ({capturas_dia} capturas/dia) ===")
    e = energia_captura(p, chip, p_disparo=p_disparo)
    for ma, etiqueta in ((MA_SLEEP, "sleep tal cual (OV2640 despierto)"),
                         (MA_SLEEP_MOSFET, "sleep con MOSFET al sensor")):
        a = autonomia(e["total"], capturas_dia, ma_sleep=ma)
        print(f"{etiqueta:<34} {a['total_wh']:>6.2f} Wh/dia  "
              f"({a['frac_sleep']:>4.0%} es reposo)  margen del panel x{a['margen']:.1f}  "
              f"{a['dias_sin_sol']:.1f} dias sin sol")
    return p_ancla


# --- Self-check --------------------------------------------------------------
def self_check():
    p = perfil(0.25, 96, 1, 2)
    # el modelo real pesa ~250 kB: el conteo analitico tiene que quedar en ese orden
    assert 150 < p["flash_kb"] < 400, f"{p['flash_kb']:.0f} kB de pesos, esperado ~250"
    assert p["macs"] > 0 and p["params"] > 0

    # escalar alpha: MACs crecen aprox con alpha^2 en el grueso de la red
    r = perfil(0.5, 96, 1, 2)["macs"] / p["macs"]
    assert 2.0 < r < 4.5, f"alpha 0.25->0.5 multiplico MACs por {r:.2f}"

    # escalar resolucion: MACs crecen con res^2
    r = perfil(0.25, 192, 1, 2)["macs"] / p["macs"]
    assert 3.5 < r < 4.5, f"res 96->192 multiplico MACs por {r:.2f}"

    # RGB cuesta mas que gris, pero poco: solo afecta a la primera capa
    r = perfil(0.25, 96, 3, 2)["macs"] / p["macs"]
    assert 1.0 < r < 1.2, f"gris->RGB multiplico MACs por {r:.2f}"

    # FOMO: truncar tiene que ser mas barato y devolver una grilla
    f = perfil(0.25, 96, 1, 2, corte=6)
    assert f["grilla"] == (12, 12), f["grilla"]
    assert f["macs"] < p["macs"], "truncar no abarato la red"
    assert f["flash_kb"] < p["flash_kb"] / 3, "truncar no achico los pesos"
    assert perfil(0.25, 160, 1, 2, corte=6)["grilla"] == (20, 20)
    assert perfil(0.25, 96, 1, 2, corte=4)["grilla"] == (24, 24)

    # calibracion: por construccion el ancla tiene que reproducir su latencia medida
    for chip, ms in LATENCIA_MEDIDA_MS.items():
        assert abs(latencia_ms(p, chip) - ms) < 1e-6, chip
    # y el S3 tiene que salir ~7x mas rapido que el ESP32
    assert 5 < latencia_ms(p, "esp32") / latencia_ms(p, "esp32s3") < 9

    # memoria
    donde, arena = donde_entra(p)
    assert donde in ("SRAM", "PSRAM"), donde
    assert arena > p["pico_act_kb"], "la arena tiene que incluir el margen de scratch"
    gordo = perfil(1.0, 320, 3, 2)               # 4.2 M parametros, activaciones de MB
    assert donde_entra(gordo)[0] == "PSRAM", donde_entra(gordo)
    assert donde_entra(gordo, psram_kb=0)[0] == "NO ENTRA (arena)"
    assert donde_entra(gordo, flash_kb=64)[0] == "NO ENTRA (flash)"
    assert donde_entra(p, sram_kb=0)[0] == "PSRAM", "sin SRAM deberia caer a PSRAM"

    # energia: la camara domina cuando la red casi no dispara
    e_solo = energia_captura(p, "esp32", p_disparo=0.0)
    e_full = energia_captura(p, "esp32", p_disparo=1.0)
    assert e_solo["e_camara"] > 10 * e_solo["e_clasico"], "el filtro clasico deberia ser marginal"
    assert e_full["total"] > e_solo["total"], "la red tiene que sumar energia"
    assert e_full["e_nn"] > e_full["e_camara"] / 2, "la red no puede ser despreciable"

    # cascada: bajar la tasa de disparo tiene que bajar la energia monotonamente
    tot = [energia_captura(p, "esp32", p_disparo=x)["total"] for x in (0.0, 0.1, 0.5, 1.0)]
    assert all(a < b for a, b in zip(tot, tot[1:])), tot

    # autonomia: con el sensor apagado el reposo tiene que pesar menos
    a1 = autonomia(e_full["total"], 96, ma_sleep=MA_SLEEP)
    a2 = autonomia(e_full["total"], 96, ma_sleep=MA_SLEEP_MOSFET)
    assert a2["total_wh"] < a1["total_wh"] and a2["margen"] > a1["margen"]
    assert a1["frac_sleep"] > 0.5, "con el OV2640 despierto el reposo deberia dominar"

    run()
    print("\nself-check OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chip", default="esp32s3", choices=list(LATENCIA_MEDIDA_MS))
    ap.add_argument("--capturas-dia", type=int, default=96)
    ap.add_argument("--p-disparo", type=float, default=0.1)
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        self_check()
    else:
        run(a.chip, a.capturas_dia, a.p_disparo)
