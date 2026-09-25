#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy", "pillow", "matplotlib"]
# ///
"""Simulador del pipeline de imagen del nodo (Flamara).

Replica en Python lo que corre en el ESP32:
  Etapa 0  BOX downscale QVGA 320x240 -> 16x16 (reciproco fijo, sin division)
  Etapa 1  indice cromatico R-G, clamp [0,255]
  Etapa 2  bitmask de llama (R>150, R-G>50, B<100) + early exit
  Etapa 3  DWT Haar 2D in-place, int16
  Etapa 4  cuantizacion min-max por subbanda, HH descartado
  Etapa 5  empaquetado LoRa + airtime SF10/125kHz
  RX       iDWT y metricas de reconstruccion (PSNR, correlacion, deteccion)

Uso:
    ./pipeline_sim.py                  # imagen sintetica campo+llama
    ./pipeline_sim.py foto.jpg
    ./pipeline_sim.py foto.jpg --plot  # heatmaps
    ./pipeline_sim.py --self-check     # asserts, sin imagen

Fuente: Notion "Pipeline de Procesamiento de Imagen" + "Etapa 1/2".
"""

import argparse
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# --- Constantes del firmware -------------------------------------------------
SRC_W, SRC_H = 320, 240          # QVGA
GRID = 16
BLOCK_W, BLOCK_H = SRC_W // GRID, SRC_H // GRID   # 20 x 15 = 300 px/celda
BOX_RECIP = 14318                # valor documentado en Notion
BOX_SHIFT = 22
# ponytail: 2**22/300 = 13981 es el reciproco correcto (error 0.005%).
# 14318 da 1/292.9 -> +2.4% de ganancia. El script mide el sesgo real abajo;
# usar --recip 13981 para comparar.

TH_R, TH_RG, TH_B = 150, 50, 100  # umbrales bitmask de llama
TH_RX = 50                        # umbral de deteccion en receptor

LORA_SF, LORA_BW, LORA_CR, LORA_PRE = 10, 125_000, 1, 8


# --- Etapa 0: sensor ---------------------------------------------------------
def load_qvga(path=None):
    """Imagen -> array (240,320,3) uint8. Sin path: escena sintetica."""
    if path is None:
        return synthetic_scene()
    img = Image.open(path).convert("RGB").resize((SRC_W, SRC_H), Image.BILINEAR)
    return np.asarray(img, dtype=np.uint8)


def synthetic_scene():
    """Campo amarillo seco + cielo + frente de llama naranja. Valores de Notion."""
    rng = np.random.default_rng(0)
    im = np.zeros((SRC_H, SRC_W, 3), np.int16)
    im[:80] = (110, 130, 180)            # cielo
    im[80:] = (130, 120, 60)             # campo amarillo seco (R-G ~ 10)
    yy, xx = np.mgrid[0:SRC_H, 0:SRC_W]
    llama = ((xx - 200) ** 2 / 45**2 + (yy - 170) ** 2 / 30**2) < 1.0
    im[llama] = (235, 95, 40)            # llama naranja (R-G ~ 140)
    borde = (((xx - 200) ** 2 / 60**2 + (yy - 170) ** 2 / 42**2) < 1.0) & ~llama
    im[borde] = (185, 125, 70)           # borde tibio (zona gris, R-G ~ 60)
    im += rng.integers(-8, 9, im.shape, dtype=np.int16)
    return np.clip(im, 0, 255).astype(np.uint8)


def to_rgb565(rgb):
    """Cuantiza 888 -> 565 y re-expande como hace el firmware (R<<3|R>>2)."""
    r5 = rgb[..., 0] >> 3
    g6 = rgb[..., 1] >> 2
    b5 = rgb[..., 2] >> 3
    return np.stack([(r5 << 3) | (r5 >> 2),
                     (g6 << 2) | (g6 >> 4),
                     (b5 << 3) | (b5 >> 2)], -1).astype(np.uint8)


def box_downsample(rgb, recip=BOX_RECIP, shift=BOX_SHIFT):
    """BOX average 20x15 -> 16x16 con reciproco fijo (identico al ESP32)."""
    acc = (rgb.reshape(GRID, BLOCK_H, GRID, BLOCK_W, 3)
              .sum(axis=(1, 3), dtype=np.int64))          # acumuladores uint32 en C
    return ((acc * recip) >> shift).clip(0, 255).astype(np.uint8)


# --- Etapa 1: indices cromaticos --------------------------------------------
def indices(grid):
    """Todos los indices comparados en Notion. R-G es el elegido."""
    R, G, B = (grid[..., i].astype(np.int16) for i in range(3))
    return {
        "R-G":      np.clip(R - G, 0, 255).astype(np.int16),
        "R-B":      np.clip(R - B, 0, 255).astype(np.int16),
        "R/(G+B)":  (255 * R / np.maximum(G.astype(np.int32) + B, 1)).clip(0, 255).astype(np.int16),
        "gris":     ((77 * R + 150 * G + 29 * B) >> 8).astype(np.int16),
    }


def classify_regions(grid_rgb, th_black=35, th_sky_diff=15, th_green_diff=10):
    """Clasifica celdas de la grilla en Negro, Cielo, Verde y Zona de Interes."""
    R = grid_rgb[..., 0].astype(np.int16)
    G = grid_rgb[..., 1].astype(np.int16)
    B = grid_rgb[..., 2].astype(np.int16)

    is_black = (R < th_black) & (G < th_black) & (B < th_black)
    is_sky = (B - R > th_sky_diff) & (B > G) & (B > 60)
    is_green = (G - R > th_green_diff) & (G > B)

    is_discarded = is_black | is_sky | is_green
    is_interest = ~is_discarded

    return {
        "black": is_black,
        "sky": is_sky,
        "green": is_green,
        "discarded": is_discarded,
        "interest": is_interest,
    }


def maxpool2d(matrix, pool_size=2):
    """Maxpooling 2D (ej: 16x16 -> 8x8). Preserva picos de calor/indice R-G."""
    H, W = matrix.shape
    out_h, out_w = H // pool_size, W // pool_size
    reshaped = matrix[:out_h * pool_size, :out_w * pool_size].reshape(out_h, pool_size, out_w, pool_size)
    return reshaped.max(axis=(1, 3))


def separacion(idx, mask_llama):
    """Margen entre llama y fondo, en unidades de sigma. Mayor = mejor discriminador."""
    a, b = idx[mask_llama].astype(float), idx[~mask_llama].astype(float)
    if a.size == 0 or b.size == 0:
        return float("nan")
    s = math.sqrt((a.var() + b.var()) / 2) or 1e-9
    return (a.mean() - b.mean()) / s


# --- Etapa 2: bitmask --------------------------------------------------------
def flame_bitmask(grid, idx, th_r=TH_R, th_rg=TH_RG, th_b=TH_B, interest_mask=None):
    """1 bit/pixel -> 32 bytes. Si interest_mask esta definido, solo evalua en zonas de interes."""
    R, B = grid[..., 0].astype(np.int16), grid[..., 2].astype(np.int16)
    hit = (R > th_r) & (idx > th_rg) & (B < th_b)
    if interest_mask is not None:
        hit = hit & interest_mask
    hit_flat = hit.ravel()
    bm = np.packbits(hit_flat, bitorder="little").tobytes()
    return bm, int(hit_flat.sum())


# --- Etapa 3: DWT Haar 2D ----------------------------------------------------
def dwt_haar2(m):
    """Butterfly entera (suma/resta), filas y luego columnas. Layout LL|LH/HL|HH."""
    x = m.astype(np.int32).copy()
    for axis in (1, 0):
        x = np.moveaxis(x, axis, -1)
        a, b = x[..., 0::2], x[..., 1::2]
        x = np.concatenate([a + b, a - b], axis=-1)
        x = np.moveaxis(x, -1, axis)
    return x


def idwt_haar2(x):
    """Inversa: a=(s+d)/2, b=(s-d)/2. Exacta sobre coeficientes sin cuantizar."""
    y = x.astype(np.int32).copy()
    for axis in (0, 1):
        y = np.moveaxis(y, axis, -1)
        h = y.shape[-1] // 2
        s, d = y[..., :h], y[..., h:]
        out = np.empty_like(y)
        out[..., 0::2] = (s + d) // 2
        out[..., 1::2] = (s - d) // 2
        y = np.moveaxis(out, -1, axis)
    return y


def subbands(x):
    h = GRID // 2
    return {"LL": x[:h, :h], "LH": x[:h, h:], "HL": x[h:, :h], "HH": x[h:, h:]}


# --- Etapa 4: cuantizacion min-max ------------------------------------------
def quant(sb):
    mn, mx = int(sb.min()), int(sb.max())
    rng = mx - mn
    q = np.zeros_like(sb, np.uint8) if rng == 0 else \
        np.round((sb - mn) * 255.0 / rng).astype(np.uint8)
    return q, mn, mx


def dequant(q, mn, mx):
    return np.round(q.astype(np.float64) / 255.0 * (mx - mn) + mn).astype(np.int32)


# --- Etapa 5: empaquetado + airtime -----------------------------------------
def pack(scal, qs, bitmask, flags=1):
    """Layout de Notion: flags(1) + 6 escalares int16(12) + pad(6) + 3x64 + bitmask(32)."""
    p = bytearray([flags])
    for v in scal:
        p += int(v).to_bytes(2, "little", signed=True)
    p += b"\x00" * 6
    for q in qs:
        p += q.astype(np.uint8).tobytes()
    p += bitmask
    return bytes(p)


def lora_airtime(payload, sf=LORA_SF, bw=LORA_BW, cr=LORA_CR, pre=LORA_PRE,
                 crc=1, header=1):
    """Time-on-air SX1276 (AN1200.13). Devuelve segundos."""
    tsym = (2 ** sf) / bw
    de = 1 if tsym > 0.016 else 0        # low-datarate optimize
    num = 8 * payload - 4 * sf + 28 + 16 * crc - 20 * (0 if header else 1)
    n = 8 + max(math.ceil(num / (4 * (sf - 2 * de))) * (cr + 4), 0)
    return (pre + 4.25) * tsym + n * tsym


def lora_packet_analysis(size_bytes, payload_mtu=222, sf=LORA_SF, bw=LORA_BW, cr=LORA_CR):
    """Calcula paquetes requeridos y tiempo de aire total para transmitir N bytes por LoRa."""
    if size_bytes == 0:
        return {"size_bytes": 0, "num_packets": 0, "airtime_per_pkt": 0.0, "total_airtime": 0.0}
    num_packets = int(math.ceil(size_bytes / payload_mtu))
    last_pkt_size = size_bytes % payload_mtu or payload_mtu
    if num_packets == 1:
        airtime_per_pkt = lora_airtime(size_bytes, sf=sf, bw=bw, cr=cr)
        total_airtime = airtime_per_pkt
    else:
        full_pkt_airtime = lora_airtime(payload_mtu, sf=sf, bw=bw, cr=cr)
        last_pkt_airtime = lora_airtime(last_pkt_size, sf=sf, bw=bw, cr=cr)
        airtime_per_pkt = full_pkt_airtime
        total_airtime = (num_packets - 1) * full_pkt_airtime + last_pkt_airtime
    return {
        "size_bytes": size_bytes,
        "payload_mtu": payload_mtu,
        "num_packets": num_packets,
        "airtime_per_pkt": airtime_per_pkt,
        "total_airtime": total_airtime,
    }


# --- Pipeline completo -------------------------------------------------------
def preparar(rgb888, recip=BOX_RECIP):
    """Etapas 0,1,3,4 + reconstruccion RX: todo lo que NO depende de los umbrales."""
    grid = box_downsample(to_rgb565(rgb888), recip)
    idx = indices(grid)["R-G"]
    regions = classify_regions(grid)

    coef = dwt_haar2(idx)
    sb = subbands(coef)
    qs, scal = [], []
    for nombre in ("LL", "LH", "HL"):                 # HH se descarta
        q, mn, mx = quant(sb[nombre])
        qs.append(q)
        scal += [mn, mx]

    h = GRID // 2
    rec = np.zeros((GRID, GRID), np.int32)            # HH queda en cero
    rec[:h, :h] = dequant(qs[0], scal[0], scal[1])
    rec[:h, h:] = dequant(qs[1], scal[2], scal[3])
    rec[h:, :h] = dequant(qs[2], scal[4], scal[5])
    idx_rec = np.clip(idwt_haar2(rec), 0, 255)

    return dict(rgb=rgb888, grid=grid, idx=idx, coef=coef, qs=qs, scal=scal,
                idx_rec=idx_rec, regions=regions)


def transmitir(p, th_r=TH_R, th_rg=TH_RG, th_b=TH_B, use_region_filter=False):
    """Etapas 2 y 5 con umbrales libres. Devuelve (mascara 16x16, count, trama)."""
    interest_mask = p["regions"]["interest"] if (use_region_filter and "regions" in p) else None
    bm, n = flame_bitmask(p["grid"], p["idx"], th_r, th_rg, th_b, interest_mask=interest_mask)
    hit = np.unpackbits(np.frombuffer(bm, np.uint8),
                        bitorder="little").reshape(GRID, GRID).astype(bool)
    pkt = b"\x00" if n == 0 else pack(p["scal"], p["qs"], bm)   # early exit: 1 B
    return hit, n, pkt


# --- Metricas ----------------------------------------------------------------
def psnr(a, b, peak=255.0):
    mse = np.mean((a.astype(float) - b.astype(float)) ** 2)
    return float("inf") if mse == 0 else 10 * math.log10(peak ** 2 / mse)


# --- Reporte -----------------------------------------------------------------
def run(path=None, recip=BOX_RECIP, plot=False, csv=None):
    rgb888 = load_qvga(path)
    rgb = to_rgb565(rgb888)
    p = preparar(rgb888, recip)
    grid, idx, coef = p["grid"], p["idx"], p["coef"]
    idxs = indices(grid)
    _, flame_count, pkt = transmitir(p)

    src_name = str(path) if path else "sintetica (campo + llama)"
    print(f"\n=== Entrada: {src_name} ===")
    print(f"BOX {BLOCK_W}x{BLOCK_H} px/celda ({BLOCK_W*BLOCK_H} px)  reciproco={recip}>>{BOX_SHIFT}")
    exacto = box_downsample(rgb, round(2**BOX_SHIFT / (BLOCK_W*BLOCK_H)))
    sesgo = 100 * (grid.astype(float).mean() / max(exacto.astype(float).mean(), 1e-9) - 1)
    print(f"Sesgo del reciproco vs division exacta: {sesgo:+.2f} %")

    # --- indices ---
    llama = idxs["R-G"] > 80          # referencia para medir separabilidad
    print("\n--- Indices cromaticos (separabilidad llama/fondo) ---")
    print(f"{'indice':<10} {'min':>5} {'max':>5} {'media':>7} {'sep(sigma)':>11}")
    for k, v in idxs.items():
        print(f"{k:<10} {v.min():>5} {v.max():>5} {v.mean():>7.1f} {separacion(v, llama):>11.2f}")

    print(f"\nBitmask: {flame_count}/256 px de llama  densidad={flame_count/256:.3f}")
    if flame_count == 0:
        print("EARLY EXIT -> deep sleep, sin DWT ni TX (caso nominal sin fuego)")

    # --- DWT + cuantizacion ---
    sb = subbands(coef)
    for i, name in enumerate(("LL", "LH", "HL")):
        print(f"{name}: rango [{p['scal'][2*i]}, {p['scal'][2*i+1]}]  -> 64 B uint8")
    print(f"HH: rango [{sb['HH'].min()}, {sb['HH'].max()}]  -> DESCARTADO (64 B ahorrados)")

    idx_rec = p["idx_rec"]
    det_o, det_r = idx > TH_RX, idx_rec > TH_RX
    corr = np.corrcoef(idx.ravel(), idx_rec.ravel())[0, 1] if idx.std() else 1.0

    print("\n--- Reconstruccion en receptor (iDWT, HH=0) ---")
    print(f"PSNR        {psnr(idx, idx_rec):.2f} dB      (criterio Notion: >= 35 dB)")
    print(f"Correlacion {corr:.4f}          (referencia: ~0.997)")
    print(f"MAE         {np.abs(idx - idx_rec).mean():.2f}  max err {np.abs(idx-idx_rec).max()}")
    print(f"Deteccion (umbral {TH_RX}): {int((det_o != det_r).sum())} px difieren de 256")

    # --- tamanos ---
    disco = Path(path).stat().st_size if path else 0
    filas = [
        ("Archivo original en disco", disco),
        (f"Frame QVGA RGB565 crudo ({SRC_W}x{SRC_H})", SRC_W * SRC_H * 2),
        ("Grilla 16x16 RGB tras BOX", GRID * GRID * 3),
        ("Indice R-G 16x16 uint8 (sin comprimir)", GRID * GRID),
        ("Alternativa: cuantizacion 4 bits", GRID * GRID // 2),
        ("Alternativa: downscale 8x8 (solo LL)", 64),
        ("DWT: LL+LH+HL cuantizados", 192),
        ("DWT: escalares min/max (6 x int16)", 12),
        ("Bitmask de llama", 32),
        ("PAQUETE LORA COMPLETO", len(pkt)),
    ]
    print("\n--- Tamanos ---")
    print(f"{'etapa':<42} {'bytes':>9} {'vs QVGA':>9}")
    base = SRC_W * SRC_H * 2
    for n, b in filas:
        if b == 0:
            continue
        print(f"{n:<42} {b:>9} {base/b:>8.0f}x")

    print("\n--- Transmision LoRa SF10 / 125 kHz / CR 4:5 ---")
    for n, b in [("Paquete DWT (actual)", len(pkt)),
                 ("Si fuera 4 bits (128+45 hdr)", 128 + 45),
                 ("Sin comprimir (256+51)", 256 + 51)]:
        t = lora_airtime(min(b, 255))
        extra = " [>255 B: 2 paquetes]" if b > 255 else ""
        print(f"{n:<32} {b:>4} B   airtime {t*1000:>6.1f} ms{extra}")

    if csv:
        np.savetxt(csv, idx, fmt="%d", delimiter=",")
        print(f"\nIndice R-G guardado en {csv} (formato del volcado UART)")

    if plot:
        show(rgb888, grid, idx, idx_rec, coef)
    return dict(idx=idx, idx_rec=idx_rec, pkt=pkt, flame_count=flame_count)


def show(rgb, grid, idx, idx_rec, coef):
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap
    cmap = LinearSegmentedColormap.from_list("rg", ["#0a0c0f", "#1a3a1a", "#ff6b2b"])
    fig, ax = plt.subplots(2, 3, figsize=(13, 8))
    fig.patch.set_facecolor("#0a0c0f")
    paneles = [
        (rgb, "Entrada QVGA", None, None, None),
        (grid, "BOX 16x16", None, None, None),
        (idx, "Indice R-G (TX)", cmap, 0, 170),
        (np.abs(coef), "|Coef DWT| (LL|LH/HL|HH)", "viridis", None, None),
        (idx_rec, "R-G reconstruido (RX)", cmap, 0, 170),
        (np.abs(idx - idx_rec), "Error absoluto", "hot", 0, 30),
    ]
    for a, (d, t, cm, lo, hi) in zip(ax.ravel(), paneles):
        im = a.imshow(d, cmap=cm, vmin=lo, vmax=hi, interpolation="nearest")
        a.set_title(t, color="white")
        a.tick_params(colors="white")
        if cm:
            plt.colorbar(im, ax=a, fraction=0.046)
    plt.tight_layout()
    plt.show()


# --- Self-check --------------------------------------------------------------
def self_check():
    rng = np.random.default_rng(1)
    m = rng.integers(0, 171, (GRID, GRID)).astype(np.int16)
    assert np.array_equal(idwt_haar2(dwt_haar2(m)), m), "DWT/iDWT no reversible"

    c = dwt_haar2(m)
    assert abs(int(c.max())) < 32768 and abs(int(c.min())) < 32768, "overflow int16"

    q, mn, mx = quant(c[:8, :8])
    assert np.abs(dequant(q, mn, mx) - c[:8, :8]).max() <= (mx - mn) / 255 / 2 + 1, "cuant fuera de rango"

    r = run()  # escena sintetica: debe detectar llama y reconstruir bien
    assert r["flame_count"] > 0, "escena con llama pero bitmask vacio"
    assert len(r["pkt"]) == 243, f"paquete {len(r['pkt'])} B, esperado 243"
    assert psnr(r["idx"], r["idx_rec"]) >= 35, "PSNR bajo el criterio de Notion"

    # --- umbrales libres ---
    p = preparar(load_qvga())
    hit, n, _ = transmitir(p)
    R, B = p["grid"][..., 0].astype(np.int16), p["grid"][..., 2].astype(np.int16)
    directa = (R > TH_R) & (p["idx"] > TH_RG) & (B < TH_B)
    assert np.array_equal(hit, directa), "unpack del bitmask no coincide con la mascara"

    cuentas = [transmitir(p, TH_R, t, TH_B)[1] for t in range(0, 256, 15)]
    assert all(a >= b for a, b in zip(cuentas, cuentas[1:])), "flame_count no monotono"

    # --- comprobaciones de nuevos filtros y analisis LoRa ---
    grid_test = np.zeros((16, 16, 3), dtype=np.uint8)
    grid_test[0:4, :] = [10, 10, 10]       # negro
    grid_test[4:8, :] = [50, 100, 180]     # cielo
    grid_test[8:12, :] = [40, 150, 50]     # verde
    grid_test[12:16, :] = [220, 100, 40]    # interes (posible llama)

    reg = classify_regions(grid_test)
    assert reg["black"][0, 0] and reg["sky"][5, 0] and reg["green"][9, 0] and reg["interest"][13, 0], "fallo classify_regions"

    mp = maxpool2d(m, pool_size=2)
    assert mp.shape == (8, 8), f"maxpool2d shape incorrecto: {mp.shape}"
    assert mp[0, 0] == m[0:2, 0:2].max(), "maxpool2d valor invalido"

    l_ana = lora_packet_analysis(153600, payload_mtu=222)
    assert l_ana["num_packets"] == 692, f"esperados 692 paquetes para 153600 B, dio {l_ana['num_packets']}"

    _, n0, pkt0 = transmitir(p, 255, 255, 0)
    assert n0 == 0 and len(pkt0) == 1, "early exit no produce trama de 1 B"
    print("\nself-check OK")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("imagen", nargs="?", type=Path)
    p.add_argument("--plot", action="store_true", help="heatmaps del pipeline")
    p.add_argument("--csv", type=Path, help="guardar indice R-G como CSV")
    p.add_argument("--recip", type=int, default=BOX_RECIP, help="reciproco BOX")
    p.add_argument("--self-check", action="store_true")
    a = p.parse_args()
    if a.self_check:
        self_check()
    elif a.imagen and not a.imagen.exists():
        sys.exit(f"no existe: {a.imagen}")
    else:
        run(a.imagen, a.recip, a.plot, a.csv)
