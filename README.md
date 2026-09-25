# Banco de pruebas — procesamiento de imagen del nodo

Simulación en Python del procesamiento que corre en el **ESP32-S3** del nodo Flamara
([Freenove ESP32-S3 WROOM](https://github.com/Freenove/Freenove_ESP32_S3_WROOM_Board):
ESP32-S3-WROOM-1 N8R8 + OV2640).
Cada módulo replica lo que hace el firmware; cada notebook mide una decisión de diseño.

## Hardware que se está simulando

```
ESP32-S3   ──UART──▶  Heltec WiFi LoRa 32  ──LoRa 915 MHz──▶  gateway
captura + procesa     SX1276, SOLO transmite
+ microSD             + BME280 / DS3231 / OLED por I2C
```

Dos destinos con presupuestos opuestos: la **microSD** es barata y guarda evidencia para
recalibrar offline; el **enlace LoRa** se paga en tiempo al aire y sólo lleva el resultado.
La reducción agresiva de la imagen es una restricción *del enlace*, no del procesamiento.

## Estructura

```
Pruebas/
├── notebooks/     bancos de prueba (lo que se lee y se ejecuta)
├── modulos/       la lógica, espejo del firmware (lo que se importa y se testea)
├── datos/         imágenes de entrada
├── salidas/       lo que generan los notebooks (se regenera, no se edita a mano)
└── pyproject.toml
```

### `notebooks/`

| notebook | qué mide |
|---|---|
| `1_fuego.ipynb` | pipeline de detección de llama: índice R−G, bitmask, DWT Haar, paquete LoRa |
| `2_horizonte.ipynb` | detección de la línea de horizonte: **cuánto se puede achicar la imagen** sin perderla |
| `3_red_neuronal.ipynb` | viabilidad de una CNN tiny en el ESP32: cómputo, memoria y energía |
| `4.red_entrenada.ipynb` | entrena la CNN de fuego y humo (MobileNetV1 α=0.25, multi-etiqueta) sobre D-Fire |
| `5_cuantizacion.ipynb` | la cuantiza a int8 para TFLite Micro, la mide como la ve la cámara y fija el umbral de alerta |

### `modulos/`

| módulo | qué hace | verificación |
|---|---|---|
| `pipeline_sim.py` | pipeline de fuego: BOX 16×16, índice R−G, bitmask, DWT, empaquetado | `--self-check` |
| `horizonte_sim.py` | horizonte: reducción anisotrópica, perfil por columna, ajuste robusto, trama UART, presupuesto de SD | `--self-check` |
| `nn_esp32.py` | modelo analítico de MobileNetV1: MACs, memoria, latencia, energía | `--self-check` |
| `dfire.py` | datos de D-Fire compartidos por 4 y 5: etiquetas [fuego, humo], partición oficial, lectura de imágenes, gemelos | `--self-check` |

Corren solos, sin notebook:

```bash
uv run modulos/horizonte_sim.py --self-check
uv run modulos/horizonte_sim.py datos/image_1.png --grid 120x16
uv run modulos/nn_esp32.py --chip esp32s3
uv run modulos/dfire.py --self-check
```

### `salidas/`

| archivo | lo genera | qué es |
|---|---|---|
| `indice_rg.csv` | `1_fuego` | grilla del índice R−G, formato del volcado UART |
| `paquete.bin` | `1_fuego` | trama LoRa del pipeline de fuego (243 B) |
| `registro_sd.bin` | `2_horizonte` | lo que iría a la microSD: cabecera + recta + perfil + índice |
| `trama_uart.bin` | `2_horizonte` | trama al Heltec: `SOF │ tipo │ len │ payload │ CRC8` |
| `perfil_horizonte.csv` | `2_horizonte` | perfil `x_px, y_px, inlier` por columna |
| `variantes_nn.csv` | `3_red_neuronal` | presupuesto de cada variante de MobileNetV1 en cada chip |
| `modelos/` | `4` y `5` | modelo entrenado (`.keras`) y entregables int8 para el firmware: `.tflite`, `.cc`, `cuantizacion.json` |

La carpeta se crea sola al exportar (`ruta_salida()`), así que se puede borrar entera para
regenerar todo desde cero.

## Cómo se ejecuta

```bash
cd Desarrollo/Pruebas
uv sync
uv run jupyter lab
```

Python fijado en **3.13**: TensorFlow 2.21 no publica wheels para 3.14.

En el editor, elegir el kernel **Pruebas (uv)** (`.venv/bin/python`).

Los notebooks localizan la raíz buscando la carpeta `modulos/` hacia arriba, así que
funcionan tanto abiertos desde `notebooks/` como desde `Pruebas/`, y escriben siempre
en `salidas/`.

## Resultados hasta acá

- **Horizonte**: la reducción tiene que ser *anisotrópica*. A igual presupuesto de celdas,
  60×16 da 1.09 px de error y 15×64 da 5.19 px — 5× peor con el mismo tamaño. El horizonte
  es `y(x)`: las filas son resolución de medida, las columnas sólo muestras para el ajuste.
- **Enlace**: 153 600 B de frame QVGA → **6 B** de recta = 25 600×, 247.8 ms de airtime SF10.
- **Red neuronal**: entra. MobileNetV1 α=0.25 a 96×96 gris son 380 ms y 70 kB de arena en el
  ESP32 con ESP-NN; una cabeza FOMO cortada en la capa 5 baja a 112 ms y 5 kB de pesos, y
  devuelve una grilla 12×12 que **es** el payload.
- **Energía**: el reposo se come el ~99 % del presupuesto diario porque el OV2640 no se
  duerme con el ESP32. Un MOSFET cortando el riel del sensor vale más autonomía que
  cualquier optimización del modelo.
