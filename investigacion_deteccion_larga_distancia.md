# Investigación Técnica: Detección de Fuego y Humo a Larga Distancia (Imagen Estática)

## 1. Fundamentos Ópticos y Limitaciones de Larga Distancia
A distancias de 1 km a 15+ km en entornos abiertos:
- **GSD (Ground Sampling Distance)**:
  $$\text{GSD} = \frac{p \cdot D}{f}$$
  - Sensor estándar ($p = 1.75\,\mu\text{m}$, $f = 3.6\,\text{mm}$ tipo OV2640 stock): a 5 km, 1 px = 2.43 m. Una columna incipiente de humo de 10 m ocupa solo 4 px; un foco de fuego de 2 m es sub-píxel.
  - Teleobjetivo ($p = 2.9\,\mu\text{m}$, $f = 25\,\text{mm}$ en sensor STARVIS): a 5 km, 1 px = 0.58 m. Permite resolver textura y perfiles morfológicos.
- **Dispersión Atmosférica de Rayleigh ($\propto 1/\lambda^4$)**:
  - Provoca velo de bruma azul que degrada drásticamente el contraste visible.
  - El espectro NIR (750–1000 nm) sufre entre 5× y 10× menos dispersión, permitiendo penetrar bruma diurna y capturar emisiones térmicas nocturnas.
- **Filtros Polarizadores**:
  - Atenúan la dispersión del cielo y aumentan el contraste del humo entre un 25% y 40%.

---

## 2. Detección Nocturna (Puntos de Luz / Fuego)
- **Física**: Emisión térmica del hollín y líneas de emisión de potasio (766.5 nm y 769.9 nm).
- **Algoritmos Heurísticos Clásicos (Bajo cómputo / MCU)**:
  1. **White Top-Hat Transform (WTH)**: $WTH(I) = I - (I \circ B)$. Extrae picos brillantes sobre fondos oscuros o no uniformes (técnica clásica en IRSTD).
  2. **Laplaciano de Gaussiano (LoG) / Diferencia de Gaussianas (DoG)**: Detección de blobs calibrada al PSF de la lente.
  3. **Reglas cromáticas en YCbCr / HSV**: Núcleo saturado a blanco con corona de alta saturación rojo-naranja.
  4. **Descarte de falsos positivos**:
     - *Máscara de horizonte*: Descartar todo lo que esté por encima de la línea de relieve (estrellas, luna, aviones).
     - *Gradiente espacial / PSF*: Distinguir fuego lejano (dispersión gaussiana) de *hot pixels* (1 px abrupto) y luces urbanas.
- **Deep Learning / TinyML**:
  - FOMO (Faster Objects, More Objects) cuantizado int8 para microcontroladores (< 100 kB RAM, mapa de calor de centroides).
  - YOLOv8/v11-nano adaptado con cabeza P2 (stride 4) para blancos diminutos (< 8x8 px).

---

## 3. Detección Diurna (Columnas de Humo)
- **Física**: Dispersión múltiple, baja saturación cromática, atenuación de textura de alta frecuencia.
- **Algoritmos Heurísticos Clásicos**:
  1. **Dark Channel Prior (DCP) + Saturación**:
     - En escenas naturales sin humo, $J^{\text{dark}} \approx 0$.
     - El humo eleva el canal oscuro ($J^{\text{dark}}$ alto) y mantiene muy baja saturación ($S < 0.15$ en HSV).
  2. **DWT Wavelet 2D (Energía de altas frecuencias)**:
     - Relación $E_{\text{rel}} = \frac{\sum (LH^2 + HL^2 + HH^2)}{\sum LL^2}$. El humo actúa como filtro pasa-bajos, provocando una caída local abrupta de textura.
  3. **Análisis de Perfil de Horizonte**:
     - Monitoreo de discontinuidades en el gradiente vertical de la silueta del horizonte para detectar plumas tempranas emergiendo.
- **Deep Learning**:
  - **Inferencia por Mosaicos / Tiling (SAHI)**: Evitar redimensionar imágenes completas a resoluciones bajas (como 224×224), lo cual destruye objetivos pequeños. Procesar mosaicos nativos sobre la franja del horizonte.
  - Backbones con atención (MobileNetV3 + CBAM) entrenados sobre D-Fire, Smoke5k, FIRe.

---

## 4. Componentes de Hardware
1. **Sensor**: Sony STARVIS / STARVIS 2 (IMX327 / IMX462) con píxel de 2.9 µm y alta sensibilidad NIR.
2. **Óptica**: Lentes M12 o CS-Mount teleobjetivo ($f = 12\text{ mm}$ a $25\text{ mm}$) con corrección IR (*IR-Corrected*).
3. **Filtros**:
   - Mecanismo conmutable IRCUT (Día: IR-Cut activo; Noche: NoIR para captar radiación de llama).
   - Polarizador lineal frontal para reducción de bruma.
4. **Procesamiento**:
   - *Nodo Ultra-Bajo Consumo*: ESP32-S3 (TinyML FOMO int8 + Top-Hat / DWT).
   - *Nodo Edge NPU Económico*: Rockchip RV1106 / Kendryte K230 (0.5 - 1.0 TOPS, ~1W, soporte para tiling y YOLO-nano a resolución nativa).
   - *Estación Base / Torre*: Raspberry Pi CM4 + NPU Hailo-8L.
5. **Gestión de Energía**: Conmutador MOSFET de canal P para apagar completamente la cámara durante el modo *deep sleep*.
