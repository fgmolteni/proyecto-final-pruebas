#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy"]
# ///
"""Severidad del incendio desde angulos + distancia (Flamara, backend).

Porque existe: una vez que hay distancia D (idealmente por triangulacion, nb9),
los pixeles se convierten en metros (W = D*dx/fx) y de ahi en volumen, intensidad
de Byram y potencia de Briggs. Pero V ∝ D³ y Qc ∝ (Δh·U)³: con distancia monocular
(30 %) todo es orden de magnitud, no medicion. Este modulo lo cuantifica.

Convencion: funciones "NODO" usan aritmetica simple (podrian ir al ESP32-S3);
funciones "BACKEND" usan float y corren en el servidor. El BME280 da T y P (para
rho del aire); el viento es ENTRADA EXTERNA (estacion/GFS), el BME280 no lo mide.

Uso:
    ./severidad.py --self-check
"""

import argparse
import math

import numpy as np

G = 9.81            # m/s²
CP_AIRE = 1005.0    # J/(kg·K)
R_ESP = 287.05      # J/(kg·K) aire seco
H_COMB = 18.7e6     # J/kg biomasa chaqueña
FRAC_CONV = 0.60    # fracción convectiva del calor

# Tabla de severidad (Byram 1959 / Alexander 1982): (Lf_max, I_max, clase, respuesta)
# I = 259.83 * Lf^2.174  [kW/m]
TABLA_SEVERIDAD = [
    (1.0, 260, "Baja", "ataque directo con herramientas manuales"),
    (2.5, 1800, "Moderada", "difícil a mano; autobombas / cortafuegos"),
    (3.5, 3700, "Alta", "ataque directo imposible; maquinaria + medios aéreos"),
    (float("inf"), float("inf"), "Extrema", "eruptivo; sólo flancos defensivos"),
]


# --- NODO: dimensiones -------------------------------------------------------
# (aritmética simple; en firmware sería con enteros)


def dim_metros(D_m, dpx, f_px):
    """NODO: tamaño métrico desde ángulo. W = D * dpx / fx."""
    return D_m * dpx / f_px


def area_llama_m2(n_px, gsd_m):
    """NODO: área proyectada de llama. A = N_px * GSD²."""
    return n_px * gsd_m ** 2


def llama_longitud_m(D_m, h_px, p_um=2.2, f_mm=16.0, cos_tilt=1.0):
    """NODO: longitud de llama desde alto en px. Lf = D*h_px*p/(f*cos)."""
    return D_m * h_px * p_um * 1e-6 / (f_mm * 1e-3) / cos_tilt


# --- BACKEND: volumen --------------------------------------------------------


def vol_cono_truncado(H, r_base, r_top):
    """BACKEND: pluma como cono truncado (Morton-Taylor-Turner)."""
    return math.pi / 3 * H * (r_top ** 2 + r_top * r_base + r_base ** 2)


def vol_elipsoide(W, H):
    """BACKEND: puff gaussiano joven con L_prof ~= W. V = pi/6 * W² * H."""
    return math.pi / 6 * W ** 2 * H


def mc_vol_rel(sigmaD_rel, n=20000, semilla=11, sigma_ang_rel=0.05):
    """BACKEND: Monte Carlo del error relativo de V (V ∝ D³·θw²·θh).

    Devuelve desvío relativo (std/mean) del volumen muestreando D y ángulos
    con ruido gaussiano. sigma_ang_rel modela el error de segmentación (~5 %).
    """
    rng = np.random.default_rng(semilla)
    D = rng.normal(1.0, sigmaD_rel, n)
    tw = rng.normal(1.0, sigma_ang_rel, n)
    th = rng.normal(1.0, sigma_ang_rel, n)
    V = D ** 3 * tw ** 2 * th
    V = V[V > 0]
    return float(V.std() / V.mean())


# --- BACKEND: Byram ----------------------------------------------------------


def byram_I(Lf_m):
    """BACKEND: intensidad de línea [kW/m] desde longitud de llama [m]."""
    return 259.83 * Lf_m ** 2.174


def byram_Lf(I_kw_m):
    """BACKEND: longitud de llama [m] desde intensidad [kW/m]."""
    return 0.0775 * I_kw_m ** 0.46


def clase_severidad(Lf_m):
    """BACKEND: (clase, I, respuesta) según tabla. Sólo < ~2 km o sobre dosel."""
    I = byram_I(Lf_m)
    for lf_max, _, clase, resp in TABLA_SEVERIDAD:
        if Lf_m <= lf_max:
            return clase, I, resp
    return TABLA_SEVERIDAD[-1][2], I, TABLA_SEVERIDAD[-1][3]


# --- BACKEND: Briggs ---------------------------------------------------------


def rho_aire(T_c=25.0, P_hpa=1013.0):
    """BACKEND: densidad del aire desde BME280 (T, P). rho = P/(R·T)."""
    return P_hpa * 100.0 / (R_ESP * (T_c + 273.15))


def briggs_Fb(dh_m, U_ms, x_m):
    """BACKEND: flujo de flotabilidad [m⁴/s³] desde Δh obs, viento U y x.

    Fb = (Δh·U / (1.6·x^(2/3)))³. U es EXTERNA (no la mide el BME280).
    """
    return (dh_m * U_ms / (1.6 * x_m ** (2.0 / 3.0))) ** 3


def briggs_Qc(Fb, T_c=25.0, P_hpa=1013.0):
    """BACKEND: potencia convectiva [W]. Qc = Fb·π·ρ·cp·T/g."""
    rho = rho_aire(T_c, P_hpa)
    return Fb * math.pi * rho * CP_AIRE * (T_c + 273.15) / G


def biomasa_kgs(Qc_w):
    """BACKEND: consumo de biomasa [kg/s]. M = Qc/(χc·Hcomb)."""
    return Qc_w / (FRAC_CONV * H_COMB)


def ejemplo(D_m=5000.0, dx_px=14.0, dy_px=40.0, fx_px=7273.0,
            dh_m=120.0, U_ms=5.0, x_m=300.0, Lf_m=1.5):
    """BACKEND: dict de ejemplo para salidas/severidad_ejemplo.json."""
    W = dim_metros(D_m, dx_px, fx_px)
    H = dim_metros(D_m, dy_px, fx_px)
    Fb = briggs_Fb(dh_m, U_ms, x_m)
    Qc = briggs_Qc(Fb)
    clase, I, resp = clase_severidad(Lf_m)
    return {
        "D_m": D_m, "W_pluma_m": round(W, 1), "H_pluma_m": round(H, 1),
        "V_cono_m3": round(vol_cono_truncado(H, W / 4, W / 2), 0),
        "V_elipsoide_m3": round(vol_elipsoide(W, H), 0),
        "mc_vol_rel_2pct": round(mc_vol_rel(0.02), 3),
        "mc_vol_rel_10pct": round(mc_vol_rel(0.10), 3),
        "mc_vol_rel_30pct": round(mc_vol_rel(0.30), 3),
        "Byram_Lf_m": Lf_m, "Byram_I_kW_m": round(I, 0),
        "severidad": clase, "respuesta": resp,
        "Briggs_dh_m": dh_m, "viento_U_ms_EXTERNO": U_ms,
        "Briggs_Fb_m4_s3": round(Fb, 3),
        "Briggs_Qc_MW": round(Qc / 1e6, 2),
        "biomasa_kg_s": round(biomasa_kgs(Qc), 2),
    }


def self_check():
    # Dimensiones: D·Δθ (OV2640 16 mm, fx ≈ 7273 px; 14 px a 5 km ≈ 9.6 m)
    assert abs(dim_metros(5000, 14, 7273) - 9.62) < 0.05
    # Cono degenerado a cilindro: V = πR²H
    assert abs(vol_cono_truncado(100, 5, 5) - math.pi * 25 * 100) < 1e-9
    # Elipsoide esférico: V = 4/3πR³
    assert abs(vol_elipsoide(10, 10) - 4 / 3 * math.pi * 125) < 1e-9
    # Byram: Lf=1 m -> ~260 kW/m; ida y vuelta consistente
    assert abs(byram_I(1.0) - 259.83) < 1e-9
    assert abs(byram_Lf(byram_I(2.0)) - 2.0) < 0.01
    assert clase_severidad(0.5)[0] == "Baja" and clase_severidad(5.0)[0] == "Extrema"
    # Aire estándar: ~1.18 kg/m³
    assert abs(rho_aire(25.0, 1013.0) - 1.184) < 0.005
    # Briggs ida y vuelta: Fb -> Qc -> M > 0 y del orden de MW
    Fb = briggs_Fb(120.0, 5.0, 300.0)
    Qc = briggs_Qc(Fb)
    assert 0.5e6 < Qc < 500e6, Qc
    assert biomasa_kgs(Qc) > 0
    # V ∝ D³·θw²·θh: var_rel ≈ 9σD² + 5σa² (σa = 5 % segmentación)
    assert abs(mc_vol_rel(0.02) - 0.13) < 0.03
    assert abs(mc_vol_rel(0.10) - 0.32) < 0.05
    assert mc_vol_rel(0.30) > 0.8  # monocular: >80 %, orden de magnitud
    e = ejemplo()
    assert e["Briggs_Qc_MW"] > 0 and e["V_cono_m3"] > 0
    print("severidad self-check OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        self_check()
    else:
        ap.print_help()
