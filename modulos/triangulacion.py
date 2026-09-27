#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "matplotlib"]
# ///
"""Triangulacion de focos por acimut entre nodos fijos (Flamara). Backend + nodo.

Por que existe: en llanura (Chaco/Corrientes) un solo nodo NO mide distancia
(DeltaD/D ~ (D/H)*DeltaTheta: a 5 km, 0.05 deg de cabeceo = 1 km de error).
Dos nodos que ven el mismo penacho lo ubican a < 25 m por interseccion de
rumbos. El error lo domina la calibracion del acimut de cada camara, no el
ruido de pixel.

Que es de cada lado (particion nodo/backend):
  NODO (entero, corre en el ESP32): pixel -> desplazamiento angular
      DeltaPhi = arctan((x - cx)/fx). El nodo manda el centroide x (o el
      acimut ya sumado si conoce Phi_cam) en la trama LoRa de 24 B.
  BACKEND (este modulo, float): suma Phi_cam, intersecta rumbos (2x2 cerrado
      o minimos cuadrados con N nodos), Monte Carlo de error, calibracion
      solar del acimut y asociacion de focos.

Convenciones: plano local en metros (X=Este, Y=Norte, tipo UTM 21S);
acimut geografico en grados desde el Norte, horario (Este = 90).

Uso:
    uv run modulos/triangulacion.py --self-check
"""

import argparse
import math

import numpy as np

# --- Sensores y optica (OV2640 p=2.2 um, OV5640 p=1.4 um; lente M12, f=16 mm) --
P_OV2640, W_OV2640 = 2.2, 1600
P_OV5640, W_OV5640 = 1.4, 2592
F_MM = 16.0

GAMMA_MIN_DEG = 15.0   # rechazo: cruce mas chico que esto -> GDOP intratable
CHI2_95_2D = 5.991     # cuantil chi2(2) al 95 % para elipses


# --- 1. Pixel -> angulo (NODO: asi va en el firmware, en entera) ---------------
def focal_px(f_mm=F_MM, p_um=P_OV2640):
    """Distancia focal en pixeles: fx = f / p."""
    return (f_mm * 1000.0) / p_um


def pixel_a_acimut(x, fx, cx, phi_cam_deg):
    """Rumbo geografico del centroide: beta = Phi_cam + arctan((x-cx)/fx).

    x, cx en pixeles; devuelve grados 0-360.
    """
    return (phi_cam_deg + math.degrees(math.atan((x - cx) / fx))) % 360.0


def pixel_a_elevacion(y, fy, cy, elev_eje_deg=0.0):
    """Elevacion del centroide sobre el horizonte (grados, + arriba).

    y crece hacia abajo, por eso el signo menos.
    """
    return elev_eje_deg - math.degrees(math.atan((y - cy) / fy))


def sigma_ang_desde_px(sigma_px, fx):
    """Ruido de pixel -> ruido angular (grados). Aprox. de chico angulo."""
    return math.degrees(sigma_px / fx)


# --- 2. Interseccion de rumbos (BACKEND) ---------------------------------------
def angulo_cruce(beta_a_deg, beta_b_deg):
    """Angulo de cruce gamma en [0, 180] entre dos rumbos."""
    d = abs((beta_a_deg - beta_b_deg) % 360.0)
    return min(d, 360.0 - d)


def interseccion_2(ax, ay, bx, by, beta_a_deg, beta_b_deg,
                   gamma_min_deg=GAMMA_MIN_DEG):
    """Cruce de dos rumbos, solucion cerrada 2x2 (ver investigacion esc. 5.2.1).

    Devuelve dict(X, Y, gamma, tA, tB, ok, motivo). tA/tB = distancia con
    signo a lo largo de cada rumbo; si alguna es negativa el cruce cae
    DETRAS de una camara (rectas divergentes) y se rechaza.
    """
    a, b = math.radians(beta_a_deg), math.radians(beta_b_deg)
    sa, ca, sb, cb = math.sin(a), math.cos(a), math.sin(b), math.cos(b)
    den = math.sin(a - b)                       # = sin(gamma con signo)
    gamma = angulo_cruce(beta_a_deg, beta_b_deg)
    if den == 0.0:
        return dict(X=np.nan, Y=np.nan, gamma=gamma, tA=np.nan, tB=np.nan,
                    ok=False, motivo="rumbos paralelos")
    if abs(den) < math.sin(math.radians(gamma_min_deg)):
        return dict(X=np.nan, Y=np.nan, gamma=gamma, tA=np.nan, tB=np.nan,
                    ok=False, motivo=f"gamma {gamma:.1f} < {gamma_min_deg}")
    cA, cB = -ca * ax + sa * ay, -cb * bx + sb * by  # Cramer 2x2, det = sin(A-B)
    x = (sb * cA - sa * cB) / den
    y = (cb * cA - ca * cB) / den
    dax, day, dbx, dby = sa, ca, sb, cb        # dir = (sin beta, cos beta)
    ta = (x - ax) * dax + (y - ay) * day
    tb = (x - bx) * dbx + (y - by) * dby
    if ta < 0 or tb < 0:
        return dict(X=x, Y=y, gamma=gamma, tA=ta, tB=tb,
                    ok=False, motivo="divergen (cruce detras de camara)")
    return dict(X=x, Y=y, gamma=gamma, tA=ta, tB=tb, ok=True, motivo="")


def triangulacion_n(nodos, betas_deg):
    """Minimos cuadrados con N>=2 rumbos. nodos: (N,2) en metros.

    Cada rumbo da -cos(beta) X + sin(beta) Y = -cos Xn + sin Yn.
    Devuelve dict(X, Y, rms, ok). Con N=2 coincide con interseccion_2
    (salvo el chequeo de divergencia, que hace esa funcion).
    """
    nodos = np.asarray(nodos, float)
    bet = np.radians(np.asarray(betas_deg, float))
    A = np.column_stack([-np.cos(bet), np.sin(bet)])
    b = -np.cos(bet) * nodos[:, 0] + np.sin(bet) * nodos[:, 1]
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    rms = float(np.sqrt(np.mean((A @ sol - b) ** 2)))
    return dict(X=float(sol[0]), Y=float(sol[1]), rms=rms, ok=True)


def rumbo_verdadero(ax, ay, fx_, fy_):
    """Acimut verdadero nodo->punto, grados 0-360."""
    return (math.degrees(math.atan2(fx_ - ax, fy_ - ay))) % 360.0


# --- 3. Error: Monte Carlo + elipses -------------------------------------------
def elipse_95(pts):
    """Elipse del 95 % de una nube (N,2): media, ejes (m) y angulo (deg).

    Ejes = sqrt(autoval * 5.991). Si hay <3 puntos o covarianza degenerada,
    devuelve ejes nan.
    """
    pts = np.asarray(pts, float)
    out = dict(media=(np.nan, np.nan), semieje_mayor=np.nan,
               semieje_menor=np.nan, angulo_deg=np.nan, n=int(pts.shape[0]))
    if pts.shape[0] < 3:
        return out
    c = np.mean(pts, axis=0)
    cov = np.cov(pts.T)
    val, vec = np.linalg.eigh(cov)
    if np.any(~np.isfinite(val)) or np.any(val < 0):
        return out
    orden = np.argsort(val)[::-1]
    val = val[orden]
    ang = math.degrees(math.atan2(vec[1, orden[0]], vec[0, orden[0]]))
    return dict(media=(float(c[0]), float(c[1])),
                semieje_mayor=float(math.sqrt(val[0] * CHI2_95_2D)),
                semieje_menor=float(math.sqrt(val[1] * CHI2_95_2D)),
                angulo_deg=ang, n=int(pts.shape[0]))


def montecarlo_punto(foco, nodos, fx=focal_px(), sigma_px=2.0,
                     sigma_cal_deg=0.15, n=2000, seed=0,
                     gamma_min_deg=GAMMA_MIN_DEG):
    """Un punto: sesgo fijo de calibracion por nodo + ruido de pixel por tiro.

    El sesgo (sigma_cal) se sortea UNA vez por corrida: asi se modela que la
    calibracion manda sobre el ruido. Devuelve rmse/p50/p95 (m), fraccion de
    rechazo, gamma y la nube aceptada para la elipse.
    """
    rng = np.random.default_rng(seed)
    foco = np.asarray(foco, float)
    nodos = np.asarray(nodos, float)
    nn = len(nodos)
    verd = np.array([rumbo_verdadero(x, y, foco[0], foco[1]) for x, y in nodos])
    sesgo = rng.normal(0, sigma_cal_deg, nn)
    spix = sigma_ang_desde_px(sigma_px, fx)
    nube, n_rech = [], 0
    for _ in range(n):
        med = verd + sesgo + rng.normal(0, spix, nn)
        if nn == 2:
            r = interseccion_2(*nodos[0], *nodos[1], med[0], med[1],
                               gamma_min_deg=gamma_min_deg)
            if not r["ok"]:
                n_rech += 1
                continue
            nube.append((r["X"], r["Y"]))
        else:
            r = triangulacion_n(nodos, med)
            nube.append((r["X"], r["Y"]))
    nube = np.array(nube) if nube else np.zeros((0, 2))
    err = np.linalg.norm(nube - foco, axis=1) if len(nube) else np.array([np.nan])
    gamma = (interseccion_2(*nodos[0], *nodos[1], verd[0], verd[1],
                            gamma_min_deg=0)["gamma"] if nn == 2 else np.nan)
    return dict(rmse=float(np.sqrt(np.nanmean(err ** 2))),
                p50=float(np.nanmedian(err)), p95=float(np.nanpercentile(err, 95)),
                rechazo=n_rech / n, gamma=float(gamma),
                sesgo_cal=tuple(float(s) for s in sesgo), nube=nube)


def mapa_error(xs, ys, nodos, **kw):
    """Grilla de rmse/p95/gamma sobre el area (xs, ys en metros)."""
    rmse = np.full((len(ys), len(xs)), np.nan)
    p95 = np.full_like(rmse, np.nan)
    gam = np.full_like(rmse, np.nan)
    for j, y in enumerate(ys):
        for i, x in enumerate(xs):
            r = montecarlo_punto((x, y), nodos, **kw)
            rmse[j, i], p95[j, i], gam[j, i] = r["rmse"], r["p95"], r["gamma"]
    return dict(rmse=rmse, p95=p95, gamma=gam)


# --- 4. Sol: acimut astronomico + calibracion de Phi_cam ------------------------
def sol_az_elev(ano, mes, dia, hora_utc, lat_deg, lon_deg):
    """Acimut (0-360 desde el Norte, horario) y elevacion del sol (deg).

    NOAA simplificado (Meeus de baja precision, ~0.01 deg): entra fecha/hora
    UTC del DS3231 + lat/lon del despliegue. Sin dependencias.
    """
    if not (-90.0 <= lat_deg <= 90.0):
        raise ValueError("latitud fuera de rango")
    # Dia juliano a las hora_utc
    a = (14 - mes) // 12
    y, m = ano + 4800 - a, mes + 12 * a - 3
    jdn = dia + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    jd = jdn - 0.5 + hora_utc / 24.0
    jc = (jd - 2451545.0) / 36525.0
    L0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    M = (357.52911 + jc * (35999.05029 - jc * 0.0001537)) % 360.0
    Mr = math.radians(M)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    C = (math.sin(Mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
         + math.sin(2 * Mr) * (0.019993 - 0.000101 * jc)
         + math.sin(3 * Mr) * 0.000289)
    lon_true = L0 + C
    omega = 125.04 - 1934.136 * jc
    lon_app = lon_true - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    eps0 = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))
    la, ep = math.radians(lon_app), math.radians(eps)
    decl = math.asin(math.sin(ep) * math.sin(la))
    y_eq = math.tan(ep / 2) ** 2
    L0r, Mr2 = math.radians(L0), math.radians(M)
    eq_min = 4 * math.degrees(y_eq * math.sin(2 * L0r) - 2 * e * math.sin(Mr2)
                              + 4 * e * y_eq * math.sin(Mr2) * math.cos(2 * L0r)
                              - 0.5 * y_eq ** 2 * math.sin(4 * L0r)
                              - 1.25 * e ** 2 * math.sin(2 * Mr2))
    tst = (hora_utc * 60.0 + eq_min + lon_deg * 4.0) % 1440.0
    ha = math.radians(tst / 4.0 - 180.0)
    lat, dec = math.radians(lat_deg), decl
    ce = (math.sin(lat) * math.sin(dec)
          + math.cos(lat) * math.cos(dec) * math.cos(ha))
    elev = math.degrees(math.asin(max(-1.0, min(1.0, ce))))
    az = (math.degrees(math.atan2(math.sin(ha),
                                  math.cos(ha) * math.sin(lat)
                                  - math.tan(dec) * math.cos(lat))) + 180.0) % 360.0
    return az, elev


def calibrar_acimut_sol(x_sol_px, fx, cx, az_sol_deg):
    """Despeje de Phi_cam desde la columna del sol en la imagen.

    Phi_cam = az_sol - arctan((x_sol - cx)/fx). Una foto al amanecer con
    timestamp del RTC alcanza para calibrar sin brujula.
    """
    return (az_sol_deg - math.degrees(math.atan((x_sol_px - cx) / fx))) % 360.0


# --- 5. Asociacion: intersecciones fantasma ------------------------------------
def intersecciones_fantasma(ax, ay, bx, by, betas_a, betas_b):
    """Las 4 combinaciones de 2 rumbos de A x 2 de B.

    Con 2 focos simultaneos solo 2 son reales; las otras 2 ("fantasmas")
    son el problema de asociacion: se resuelve con tiempo, elevacion y
    ancho angular, no con mas geometria.
    """
    out = []
    for i, ba in enumerate(betas_a):
        for j, bb in enumerate(betas_b):
            r = interseccion_2(ax, ay, bx, by, ba, bb, gamma_min_deg=0)
            out.append(dict(i=i, j=j, **r))
    return out


# --- Self-check -----------------------------------------------------------------
def self_check():
    fx = focal_px()                       # 16 mm / 2.2 um
    assert abs(fx - 7272.727) < 0.01, fx
    cx = W_OV2640 / 2
    assert pixel_a_acimut(cx, fx, cx, 90.0) == 90.0
    d1 = math.degrees(math.atan(1 / fx))  # ~0.0079 deg por pixel
    assert abs(pixel_a_acimut(cx + 1, fx, cx, 90.0) - 90.0 - d1) < 1e-9
    assert abs(sigma_ang_desde_px(2.0, fx) - 2 * d1) < 1e-9
    assert pixel_a_elevacion(600, fx, 600, 0.0) == 0.0
    assert pixel_a_elevacion(600 + fx, fx, 600, 0.0) == -45.0

    # Cruce exacto sin ruido: A(0,0) B(5000,0), foco (2500,4000)
    A, B, F = (0.0, 0.0), (5000.0, 0.0), (2500.0, 4000.0)
    ba = rumbo_verdadero(*A, *F)          # ~32.0 deg
    bb = rumbo_verdadero(*B, *F)          # ~328.0 deg
    assert abs(ba - 32.005) < 0.01 and abs(bb - 327.995) < 0.01
    r = interseccion_2(*A, *B, ba, bb)
    assert r["ok"] and abs(r["X"] - 2500) < 1e-6 and abs(r["Y"] - 4000) < 1e-6
    assert abs(r["gamma"] - 64.01) < 0.05, r["gamma"]
    t = triangulacion_n([A, B], [ba, bb])
    assert abs(t["X"] - 2500) < 1e-6 and abs(t["Y"] - 4000) < 1e-6 and t["rms"] < 1e-9
    # Tercer nodo no mueve el exacto
    C = (2500.0, -3000.0)
    bc = rumbo_verdadero(*C, *F)
    t3 = triangulacion_n([A, B, C], [ba, bb, bc])
    assert abs(t3["X"] - 2500) < 1e-6 and abs(t3["Y"] - 4000) < 1e-6

    # Rechazos: gamma chico y divergencia
    r2 = interseccion_2(0, 0, 1000, 0, 0.0, 5.0)
    assert not r2["ok"] and "gamma" in r2["motivo"]
    r3 = interseccion_2(0, 0, 1000, 0, 180.0, 180.0, gamma_min_deg=0)
    assert not r3["ok"] and "paralelos" in r3["motivo"]
    r4 = interseccion_2(0, 0, 1000, 0, 180.0, 135.0, gamma_min_deg=0)
    assert not r4["ok"] and "detras" in r4["motivo"], r4
    assert angulo_cruce(10, 350) == 20.0 and angulo_cruce(0, 180) == 180.0

    # Monte Carlo sin ruido = error cero; con ruido nominal da ~21 m a 5 km
    m0 = montecarlo_punto(F, [A, B], sigma_px=0, sigma_cal_deg=0, n=50, seed=1)
    assert m0["rmse"] < 1e-6 and m0["rechazo"] == 0.0
    rms_lista = [montecarlo_punto(F, [A, B], sigma_px=2.0, sigma_cal_deg=0.15,
                                   n=400, seed=s)["rmse"] for s in range(5)]
    m1 = montecarlo_punto(F, [A, B], sigma_px=2.0, sigma_cal_deg=0.15,
                          n=2000, seed=7)
    assert 5.0 < float(np.mean(rms_lista)) < 60.0, rms_lista  # doc: ~21 m
    assert m1["p95"] >= m1["p50"] >= 0
    el = elipse_95(m1["nube"])
    assert el["n"] > 300 and el["semieje_mayor"] >= el["semieje_menor"] > 0
    assert elipse_95(np.zeros((2, 2)))["n"] == 2  # muy pocos puntos -> nan

    # mapa chico corre y el error crece lejos de la base
    xs = np.array([2500.0, 2500.0])
    ys = np.array([4000.0, 9000.0])
    mp = mapa_error(xs, ys, [A, B], sigma_px=2.0, sigma_cal_deg=0.15,
                    n=100, seed=3)
    assert mp["rmse"].shape == (2, 2)
    assert mp["rmse"][1, 0] > mp["rmse"][0, 0], mp["rmse"]  # lejos = peor

    # Sol: equinoccio al mediodia en el ecuador -> casi cenital
    az, el_ = sol_az_elev(2025, 3, 20, 12.0, 0.0, 0.0)
    assert el_ > 85.0, (az, el_)
    # Mediodia solar en Resistencia (-27.45, -58.98): sol al Norte, alto
    az2, el2 = sol_az_elev(2025, 1, 15, 15.0, -27.45, -58.98)
    assert -5.0 < el2 < 90.0 and 0.0 <= az2 < 360.0, (az2, el2)
    try:
        sol_az_elev(2025, 1, 1, 12.0, 95.0, 0.0)
    except ValueError:
        pass
    else:
        raise AssertionError("latitud invalida aceptada")
    # roundtrip de calibracion: Phi_cam se recupera exacto
    phi = 137.5
    azs = 200.0
    xsol = cx + fx * math.tan(math.radians(azs - phi))
    assert abs(calibrar_acimut_sol(xsol, fx, cx, azs) - phi) < 1e-9

    # Fantasmas: 2 focos -> 4 cruces, 2 reales + 2 fantasmas
    F2 = (6000.0, 5000.0)
    bA = [rumbo_verdadero(*A, *F), rumbo_verdadero(*A, *F2)]
    bB = [rumbo_verdadero(*B, *F), rumbo_verdadero(*B, *F2)]
    gh = intersecciones_fantasma(*A, *B, bA, bB)
    assert len(gh) == 4 and all(g["ok"] for g in gh)
    reales = [g for g in gh if g["i"] == g["j"]]
    fantas = [g for g in gh if g["i"] != g["j"]]
    assert len(reales) == 2 and len(fantas) == 2
    assert abs(reales[0]["X"] - 2500) < 1e-6 and abs(reales[1]["X"] - 6000) < 1e-6
    dmin = min(math.hypot(g["X"] - F[0], g["Y"] - F[1]) for g in fantas)
    assert dmin > 500, dmin  # el fantasma cae lejos de ambos focos

    print(f"fx={fx:.1f} px  1px={d1:.4f} deg  cruce64deg rmse~{float(np.mean(rms_lista)):.1f} m  "
          f"sol_equinox_elev={el_:.1f} deg")
    print("self-check OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args()
    if a.self_check:
        self_check()
    else:
        ap.print_help()
