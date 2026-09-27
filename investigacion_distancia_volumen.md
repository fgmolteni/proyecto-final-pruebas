# Proyecto Flamara: Detección Temprana de Incendios Forestales a Larga Distancia (1–10 km)
## Estimación de Distancia, Tamaño, Volumen y Arquitectura de Procesamiento Embebido e Inalámbrico

**Proyecto Final de Carrera — Ingeniería Electrónica**  
**Facultad de Ciencias Exactas y Naturales y Agrimensura (FaCENA) — Universidad Nacional del Nordeste (UNNE)**  
**Área de Aplicación:** Gran Chaco Argentino / Corrientes (Topografía llana, monte nativo, pastizales, sabanas con palmares y esteros).

---

## 1. Resumen Ejecutivo

El presente informe constituye la investigación de ingeniería de soporte para el diseño del nodo y el sistema distribuido del proyecto **Flamara**. El objetivo central es dotar a una red de nodos autónomos de ultra-bajo consumo basados en microcontroladores de la capacidad de:
1. Detectar columnas de humo incipientes durante el día y fuentes de ignición (llamas/resplandor) durante la noche a distancias superiores a **1 km**, cubriendo un alcance operativo de **1 a 10 km**.
2. **Estimar la distancia** al evento tanto mediante técnicas monoculares (nodo individual asistido por actitud del horizonte y Modelos Digitales de Elevación - DEM) como a través de la cooperación multinodo (triangulación por intersección de líneas de marcación o azimut entre torres de vigilancia).
3. **Estimar la severidad física** del incendio: dimensiones de la pluma (altura y ancho), aproximación del volumen de humo dispersado, intensidad de línea de fuego de Byram ($I$) a partir de la longitud aparente de llama, y tasa de liberación de calor mediante la inversión de modelos analíticos de ascenso de penacho (Briggs y Morton-Taylor-Turner).
4. Delimitar estrictamente la partición de tareas entre el nodo de borde (**ESP32-S3** con restricciones severas de energía y memoria) y el servidor central (**Gateway / Backend** en la nube), diseñando una trama binaria **LoRa (SX1276)** optimizada que maximice la probabilidad de recepción sin violar las regulaciones de tiempo al aire.

---

## 2. Hardware Fijo del Nodo y Correcciones a Premisas Técnicas

### 2.1 Especificaciones del Hardware Fijo
El nodo remoto responde a una arquitectura fija, de muy bajo costo y gobernada por un presupuesto estricto de energía:
* **Unidad de Procesamiento y Captura:** Microcontrolador Espressif **ESP32-S3** (Freenove WROOM N8R8: CPU Dual-Core Xtensa LX7 a 240 MHz con extensiones vectoriales PIE, 8 MB Octal PSRAM externa, 512 kB SRAM interna, 8 MB SPI Flash).
* **Interfaz de Sensor de Imagen:** Interfaz de datos paralela **DVP (Digital Video Port)** de 8 bits gestionada por el periférico interno `LCD_CAM` del ESP32-S3 con DMA directo. **No posee transceptor físico ni interfaz MIPI CSI-2**.
* **Sensores de Imagen Compatibles:**
  * **OmniVision OV2640:** Formato óptico 1/4", matriz activa UXGA ($1600 \times 1200$ píxeles), tamaño de píxel $p = 2.20\,\mu\text{m}$.
  * **OmniVision OV5640:** Formato óptico 1/4", matriz activa QSXGA ($2592 \times 1944$ píxeles), tamaño de píxel $p = 1.40\,\mu\text{m}$, procesador de imagen (ISP) embebido.
* **Sensores Ambientales y Temporales (I2C):**
  * **Bosch BME280:** Medición de temperatura ($T$), humedad relativa ($H$) y presión barométrica ($P$) para compensación del gradiente de refracción atmosférica y estimación de flotabilidad.
  * **Maxim DS3231:** Reloj de tiempo real (RTC) de alta precisión con oscilador de cristal compensado en temperatura (TCXO, $\pm 2\,\text{ppm}$), utilizado para el despertar periódico del microcontrolador y el cálculo astronómico de la posición solar.
* **Enlace de Comunicaciones Inalámbrico:** Heltec WiFi LoRa 32 V2 operando **exclusivamente como transmisor LoRa** a 915 MHz (Semtech SX1276) comunicado con el ESP32-S3 mediante UART asíncrona dedicada.
* **Almacenamiento Local:** Tarjeta microSD conectada por bus SPI / SDMMC (1 bit) para registro de auditoría, calibración forense y almacenamiento de fotogramas completos de evidencia.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        NODO REMOTO FLAMARA                             │
│                                                                        │
│   ┌────────────────┐         DVP (8-bit)      ┌────────────────────┐   │
│   │ Sensor OV2640  │ ───────────────────────▶ │     ESP32-S3       │   │
│   │  / OV5640 M12  │                          │  (Freenove N8R8)   │   │
│   └────────────────┘                          │                    │   │
│           ▲                                   │  - Xtensa LX7 Dual │   │
│           │ Riel VDD (Corte MOSFET Canal-P)   │  - 8 MB PSRAM      │   │
│           └────────────────────────────────── │  - TFLite Micro    │   │
│                                               └─────────┬──────────┘   │
│   ┌────────────────┐       I2C (SDA/SCL)                │              │
│   │ BME280 (T/H/P) │ ◀──────────────────────────────────┤ UART         │
│   │ DS3231 (RTC)   │                                    │ (Tx/Rx)      │
│   └────────────────┘                                    ▼              │
│                                               ┌────────────────────┐   │
│   ┌────────────────┐       SPI / SDMMC        │ Heltec LoRa 32 V2  │   │
│   │ Memoria MicroSD│ ◀────────────────────────┤   (Semtech SX1276) │   │
│   │ (Evidencia RAW)│                          │  TX 915 MHz LoRa   │   │
│   └────────────────┘                          └─────────┬──────────┘   │
└─────────────────────────────────────────────────────────┼──────────────┘
                                                          │ Antena Colineal
                                                          ▼ 915 MHz
                                                  [ Enlace LoRa ] ──▶ GATEWAY
```

---

### 2.2 Correcciones Críticas a Premisas de Investigaciones Previas

Una revisión minuciosa del documento preliminar `investigacion_deteccion_larga_distancia.md` revela inconsistencias físicas y electrónicas que deben ser rectificadas en la formulación de ingeniería:

1. **Tamaño de Píxel del Sensor OV2640:**
   * *Error previo:* Se asumió $p = 1.75\,\mu\text{m}$.
   * *Corrección física:* El sensor OmniVision OV2640 tiene una matriz activa de $1600 \times 1200$ píxeles sobre una diagonal óptica de 1/4" ($4.0\,\text{mm}$), lo que resulta en un tamaño de píxel exacto de **$p = 2.20\,\mu\text{m}$** ($W = 3.52\,\text{mm}, H = 2.64\,\text{mm}$). En el caso del OV5640, su matriz de $2592 \times 1944$ píxeles en el mismo formato óptico de 1/4" presenta un tamaño de píxel de **$p = 1.40\,\mu\text{m}$**.

2. **Incompatibilidad de Sensores Sony STARVIS / IMX con ESP32-S3:**
   * *Error previo:* Se propuso el uso de sensores STARVIS (e.g., Sony IMX327 o IMX462) para mejorar la sensibilidad a larga distancia.
   * *Corrección de arquitectura:* Los sensores de la serie Sony STARVIS emplean exclusivamente interfaces de transmisión serie de alta velocidad **MIPI CSI-2** (de 2 a 4 carriles de hasta 1 Gbps) o sub-LVDS. El microcontrolador ESP32-S3 **carece de capa física MIPI D-PHY**. Adaptar un sensor STARVIS exigiría un puente convertidor externo (un FPGA tipo Lattice CrossLink o un circuito integrado de puente como Toshiba TC358748), lo cual incrementa el consumo estático en más de $80\text{--}150\,\text{mW}$, eleva drásticamente el costo y complejiza el PCB, invalidando los objetivos de simplicidad y eficiencia de Flamara. La elección de sensores queda estrictamente limitada a sensores DVP paralelos nativos (**OV2640 y OV5640**).

3. **Dispersión Atmosférica: Rayleigh vs. Mie (Penetración NIR de Humo):**
   * *Error previo:* Se afirmó que la banda NIR penetra el humo de 5× a 10× mejor que la luz visible debido a la ley de Rayleigh ($\propto 1/\lambda^4$).
   * *Corrección física:* La dispersión de Rayleigh aplica con rigor únicamente cuando el diámetro de las partículas es significativamente menor que la longitud de onda de la radiación ($d \ll \lambda$, típicamente $d < 0.05\,\mu\text{m}$), como sucede con las moléculas de $N_2$ y $O_2$ del aire atmosférico. Por el contrario, las partículas de humo de incendios forestales (aglomerados de hollín y microgotas de alquitrán condensado) poseen diámetros aerodinámicos en el rango de **$0.1\,\mu\text{m}$ a $1.0\,\mu\text{m}$** (modo de acumulación). Por ende, la interacción óptica responde a la **dispersión de Mie**, cuya atenuación decrece mucho más suavemente con la longitud de onda, gobernada por el exponente de Ångström:
     $$\sigma_{\text{ext}}(\lambda) \propto \lambda^{-\alpha}, \quad \text{con } \alpha \approx 0.5 \text{ a } 1.5 \quad (\text{muy inferior a 4})$$
     En consecuencia, **la radiación NIR (850–940 nm) no vuelve "transparente" una columna de humo denso**. La verdadera ventaja de capturar en NIR o con filtro polarizador diurno radica en la **supresión del velo atmosférico (*airlight*)** provocado por la columna de aire interpuesta entre el nodo y el horizonte, lo que eleva el contraste relativo entre el humo y el fondo de cielo o vegetación.

4. **Invalidez de Reglas Cromáticas en Sensores NoIR durante el Día:**
   * *Error previo:* Se propuso aplicar reglas heurísticas de color (como el índice $R - G > 50$ o $R > G > B$) sobre imágenes obtenidas con cámaras sin filtro de bloqueo infrarrojo (NoIR).
   * *Corrección fotométrica:* Los tintes de la matriz de filtrado de color de Bayer (pigmentos orgánicos rojo, verde y azul depositados sobre el silicio) pierden toda selectividad espectral en longitudes de onda superiores a los $780\text{--}800\,\text{nm}$. A partir de dicha banda, el silicio es uniformemente sensible al infrarrojo cercano en todos los píxeles, de modo que bajo iluminación solar directa (donde la vegetación presenta la reflectancia extrema conocida como *red edge*), los tres canales digitales saturan y se igualan:
     $$R \approx G \approx B \implies (R - G) \to 0$$
     Las heurísticas cromáticas clásicas **fracasan por completo en modo NoIR diurno**. Para mantener la capacidad de análisis cromático diurno y detección térmica nocturna, es imprescindible utilizar un filtro mecánico conmutable (**IRCUT electromecánico de 5 V / 3.3 V**) o reservar el modo NoIR para procesamiento nocturno basado exclusivamente en intensidad radiativa.

---

### 2.3 Régimen de Operación y Presupuesto de Energía

El nodo Flamara no opera como una cámara de videovigilancia de transmisión continua (30 fps), sino como una **estación de alerta temprana estática y aperiódica**.

* **Ciclo de Operación:** El nodo permanece en estado de reposo profundo (*deep sleep*) durante la mayor parte del tiempo, despertando mediante una alarma por interrupción externa provista por el RTC DS3231 cada **$\Delta t = 15\,\text{minutos}$** (96 ciclos al día).
* **Consumo en Reposo vs. Consumo Activo:**
  * Consumo del ESP32-S3 en *deep sleep* con memoria RTC activa: $\approx 10\text{--}15\,\mu\text{A}$ ($< 50\,\mu\text{W}$).
  * Consumo del sensor OV2640 en modo stand-by no desconectado: **$30\text{--}40\,\text{mA}$** ($> 100\,\text{mW}$).
* **Imperativo de Hardware:** Para que el nodo alcance una autonomía ilimitada alimentado por una pequeña celda solar de 5 W y una batería LiFePO4 / Li-ion 18650 (2600 mAh), **es mandatorio incorporar un conmutador de carga basado en MOSFET de canal P** (o un regulador LDO con pin `ENABLE`) en la línea de alimentación de la cámara. El microcontrolador energiza la cámara únicamente durante la captura e inferencia ($\approx 400\text{--}800\,\text{ms}$) y desconecta el riel eléctrico por completo durante los 15 minutos de reposo.

---

## 3. Estado del Arte: Sistemas Operativos en Torres, Datasets y TinyML

La vigilancia forestal mediante estaciones ópticas fijas ha evolucionado significativamente en la última década, pasando de sistemas analógicos con operadores humanos a redes de visión computacional automática.

### 3.1 Sistemas Operativos en Torres de Vigilancia

| Sistema / Proyecto | País / Entidad | Arquitectura de Hardware | Algoritmos y Enfoque | Alcance Declarado | Referencia Verificable |
|---|---|---|---|---|---|
| **Pyronear** | Francia / ONG Open Source | Raspberry Pi 4 / CM4 en torres con cámaras PTZ o fijas | YOLOv8 / YOLO11s frugales con clasificación temporal en dos etapas | 5–15 km | Lostanlen et al. (2024), *arXiv:2402.05349* [1] |
| **HPWREN / FIgLib** | EE. UU. (UC San Diego) | Cámaras fijas de alta resolución en torres de telecomunicaciones | Redes neuronales convolucionales espacio-temporales (**SmokeyNet**) | 5–20 km | Dewangan et al. (2022), *Remote Sens.* [2] |
| **ALERTCalifornia** (ex AlertWildfire) | EE. UU. (UC San Diego / CAL FIRE) | >1050 cámaras PTZ de alta gama sobre torres de montaña | IA centralizada en la nube con triangulación interactiva de operadores | >20 km | ALERTCalifornia (2023), *Technical Overview* [3] |
| **IQ FireWatch** | Alemania (DLR / IQ Technologies) | Sensores multiespectrales rotatorios (360°) de alta resolución | Algoritmos híbridos: textura morfológica + IA multiespectral en estación local | 20–40 km | DLR / IQ Wireless (2012), *Space Tech Hall of Fame* [4] |
| **ForestWatch** | Sudáfrica (EnviroVision Solutions) | Cámaras PTZ en mástiles de 30–50 m con enlaces dedicados | Visión artificial en servidor central integrada con modelos de terreno (DEM) | 15–20 km | EVS ForestWatch (2020), *Technical Specs* [5] |

#### Aportes Directos a Flamara:
* **Pyronear:** Demuestra que la inferencia en el borde (*edge*) con arquitecturas ligeras es viable para alertar sin requerir ancho de banda de video. No obstante, Pyronear corre sobre microcomputadoras monoplaca (Raspberry Pi, $\approx 3\text{--}5\,\text{W}$ de potencia continua), mientras que Flamara debe alcanzar un orden de magnitud inferior en consumo operando en un microcontrolador ($< 0.5\,\text{W}$ durante 1 segundo).
* **HPWREN / FIgLib:** Establece el estándar para modelar el humo como un fenómeno de evolución temporal suave sobre cámaras fijas, demostrando que comparar fotogramas adyacentes reduce drásticamente los falsos positivos generados por nubes estacionarias.
* **ForestWatch e IQ FireWatch:** Demuestran que la geolocalización de precisión a larga distancia sólo es posible combinando la línea de mira óptica con la cota de elevación del terreno (**DEM**), o triangulando desde múltiples puntos fijos.

---

### 3.2 Datasets de Fuego y Humo a Larga Distancia

El entrenamiento y la validación de algoritmos para Flamara requieren datasets que capturen la física del humo distante bajo condiciones atmosféricas reales (bruma, contraste degradado, baja relación señal-ruido).

1. **FIgLib (Fire Ignition Library):**
   * *Origen:* Repositorio de la red HPWREN (San Diego, California).
   * *Características:* Secuencias de imágenes de alta resolución tomadas a intervalos fijos de 1 minuto antes, durante y después del inicio de cientos de incendios reales a distancias de entre 2 km y 25 km. Es el benchmark de referencia mundial para la detección temprana de penachos incipientes.
   * *Referencia:* Dewangan et al. (2022), DOI: [10.3390/rs14041007](https://doi.org/10.3390/rs14041007).
2. **PYRONEAR-2025 Dataset:**
   * *Origen:* Proyecto Pyronear.
   * *Características:* ~150.000 anotaciones sobre ~50.000 imágenes y videos de 640 incendios (Francia, España, Chile, EE. UU.), de cámaras de vigilancia en torres; incluye secuencias para modelos temporales y fases de inicio temprano de humo.
   * *Referencia:* Lostanlen et al. (2024, v3 2025), *arXiv:2402.05349* [1].
3. **D-Fire Dataset:**
   * *Origen:* de Venâncio et al. (2022).
   * *Características:* 21.527 imágenes anotadas en formato YOLO divididas en 4 clases (humo solo, fuego solo, fuego+humo, fondo limpio). Orientado a dispositivos embebidos de baja potencia. Es el dataset utilizado en los notebooks 4, 5, 6 y 7 de Flamara.
   * *Limitación para larga distancia:* Gran parte de las muestras de D-Fire corresponden a incendios estructurales o de vegetación a corta y media distancia (< 500 m). Requiere filtrado y enriquecimiento con muestras de FIgLib para ajustar el comportamiento del modelo frente a penachos lejanos.
   * *Referencia:* de Venâncio et al. (2022), DOI: [10.1007/s00521-022-07467-z](https://doi.org/10.1007/s00521-022-07467-z).

---

### 3.3 Detección en Microcontroladores (TinyML)

La ejecución de modelos de aprendizaje profundo sobre microcontroladores con recursos estrictamente acotados (< 512 kB SRAM interna, memoria Flash mapeada por SPI) exige técnicas de optimización estructural:

* **Arquitectura FOMO (Faster Objects, More Objects):**
  * Diseñada por Edge Impulse para detección de objetos sin la sobrecarga de regresión de cajas delimitadoras (*bounding boxes*) de YOLO o SSD.
  * *Mecanismo:* Sustituye las cabezas convolucionales complejas por una capa de salida que clasifica celdas espaciales fijas en una grilla de resolución reducida (e.g., $12 \times 12$ o $4 \times 4$), identificando los centroides de los objetos detectados.
  * *Memoria y Cómputo:* Reduce la memoria activa requerida a menos de **$80\,\text{kB}$ de arena de tensores**, permitiendo que un backbone MobileNetV1 o MobileNetV2 truncado se ejecute íntegramente en la SRAM interna del ESP32-S3 sin recurrir a la PSRAM externa (más lenta). La tasa de cuadros es irrelevante: el nodo procesa una foto cada 15 min.
  * *Aporte a Flamara:* Es exactamente la técnica ya implementada en el proyecto (`notebooks/6_red_grilla.ipynb`), donde la salida del modelo es directamente una cuadrícula espacial binaria representativa del acimut y elevación del foco.
* **Aceleración Vectorial ESP-NN:**
  * Biblioteca de Espressif optimizada en ensamblador para la extensión vectorial PIE (*Processor Instruction Extensions*) de la arquitectura Xtensa LX7 del ESP32-S3.
  * Acelera operaciones convolucionales cuantizadas en enteros de 8 bits (**int8**) mediante instrucciones SIMD de un solo ciclo, En el ESP32-S3, Espressif publica para `person_detection` (MobileNetV1 α=0.25, 96×96) **2300 ms sin ESP-NN → 54 ms con ESP-NN (~40×)** (ver `modulos/nn_esp32.py`).

---

## 4. Alcance Óptico, Resolución Espacial y Cobertura

El diseño de un sistema de detección a distancias de 1 a 10 km exige modelar matemáticamente la relación entre el sensor, la distancia focal, la difracción atmosférica y la curvatura de la Tierra.

### 4.1 Ground Sample Distance (GSD) y Criterios de Resolución

La resolución espacial en el terreno (*Ground Sample Distance*, GSD) se define como la distancia física cubierta por un solo píxel en el plano del objeto perpendicular al eje óptico:

$$\text{GSD} = \frac{p \cdot D}{f}$$

donde:
* $p$: tamaño de píxel del sensor ($\text{m}$).
* $D$: distancia de línea de vista al objetivo ($\text{m}$).
* $f$: distancia focal efectiva de la lente ($\text{m}$).

Para el sensor **OV2640** ($p = 2.20\,\mu\text{m}$) y el sensor **OV5640** ($p = 1.40\,\mu\text{m}$), se calculan los valores de GSD en función de la focal de la lente M12 seleccionada:

| Sensor | Tamaño de Píxel ($p$) | Focal ($f$) | GSD @ 1 km | GSD @ 2 km | GSD @ 5 km | GSD @ 10 km | HFOV (Horizontal) |
|---|---|---|---|---|---|---|---|
| **OV2640** | $2.20\,\mu\text{m}$ | $3.6\,\text{mm}$ (stock) | $0.61\,\text{m}$ | $1.22\,\text{m}$ | $3.06\,\text{m}$ | $6.11\,\text{m}$ | $52.1^\circ$ |
| **OV2640** | $2.20\,\mu\text{m}$ | $12.0\,\text{mm}$ | $0.18\,\text{m}$ | $0.37\,\text{m}$ | $0.92\,\text{m}$ | $1.83\,\text{m}$ | $16.7^\circ$ |
| **OV2640** | $2.20\,\mu\text{m}$ | **$16.0\,\text{mm}$** | **$0.14\,\text{m}$** | **$0.28\,\text{m}$** | **$0.69\,\text{m}$** | **$1.38\,\text{m}$** | **$12.5^\circ$** |
| **OV2640** | $2.20\,\mu\text{m}$ | $25.0\,\text{mm}$ | $0.09\,\text{m}$ | $0.18\,\text{m}$ | $0.44\,\text{m}$ | $0.88\,\text{m}$ | $8.1^\circ$ |
| **OV5640** | $1.40\,\mu\text{m}$ | $12.0\,\text{mm}$ | $0.12\,\text{m}$ | $0.23\,\text{m}$ | $0.58\,\text{m}$ | $1.17\,\text{m}$ | $17.4^\circ$ |
| **OV5640** | $1.40\,\mu\text{m}$ | **$16.0\,\text{mm}$** | **$0.09\,\text{m}$** | **$0.18\,\text{m}$** | **$0.44\,\text{m}$** | **$0.88\,\text{m}$** | **$13.1^\circ$** |

#### Análisis de Criterios de Johnson para Detección
Según los criterios electro-ópticos clásicos de Johnson:
* **Detección básica de presencia:** Se requieren al menos **1.5 a 2 pares de líneas** (aproximadamente $3\text{--}4$ píxeles continuos a lo largo de la dimensión crítica).
* **Caracterización morfológica (distinción humo vs. nube):** Se requieren al menos **6 a 8 píxeles** en el ancho de la columna.

*Caso Crítico Diurno (Columna de Humo Incipiente):*  
Una columna de humo originada por un fuego de pastizal o monte bajo en su fase inicial suele tener un ancho de base de $W_{\text{humo}} \approx 10\text{--}15\,\text{m}$.
* Con la lente estándar de $3.6\,\text{mm}$ a $5\,\text{km}$, el humo ocupa apenas:
  $$N_{\text{px}} = \frac{10\,\text{m}}{3.06\,\text{m/px}} \approx 3.2\,\text{píxeles}$$
  Si la imagen de $1600 \times 1200$ se reduce globalmente a QVGA ($320 \times 240$, factor $5\times$), el objetivo colapsa a menos de $0.6$ píxeles, volviéndose **físicamente indetectable**.
* Con una lente teleobjetivo M12 de **$f = 16.0\,\text{mm}$** en el OV2640 a $5\,\text{km}$:
  $$N_{\text{px}} = \frac{10\,\text{m}}{0.69\,\text{m/px}} \approx 14.5\,\text{píxeles}$$
  A $10\,\text{km}$, la columna aún subtiende más de $7.2$ píxeles nativos, permitiendo resolver su estructura vertical y gradiente de dispersión.

*Caso Crítico Nocturno (Llama Abierta / Frente de Fuego):*  
Una lengua de llama nocturna de monte bajo tiene una altura vertical de $1.5\text{--}3\,\text{m}$. A $5\,\text{km}$, su imagen geométrica proyectada es menor a 2 píxeles ($< \text{GSD}$). Sin embargo, las fuentes incandescentes de alta temperatura actúan como fuentes puntuales no resueltas: no se detecta la silueta geométrica, sino la **mancha de dispersión del punto (*Point Spread Function* - PSF)** provocada por la difracción de la lente y la saturación del pozo de potencial del píxel (*blooming*). En modo NoIR, una llama de 2 m a 5 km excita un cluster brillante de $2 \times 2$ o $3 \times 3$ píxeles saturados.

---

### 4.2 Curvatura Terrestre y Refracción Atmosférica

En la región del Chaco y Corrientes, el relieve es marcadamente plano (pendientes inferiores al 0.05%, altitudes medias de 50 a 100 msnm), lo que implica que la curvatura de la Tierra y la altura del dosel vegetal imponen el límite geométrico estricto de visibilidad.

Para el cálculo geodésico de la línea de vista visual, la refracción atmosférica curva los rayos ópticos hacia la superficie terrestre. Se utiliza el modelo estándar de la **Tierra equivalente**:

$$R'_e = \frac{R_e}{1 - k}$$

donde $R_e \approx 6371\,\text{km}$ es el radio medio terrestre y $k$ es el coeficiente de refracción atmosférica ($k \approx 0.143$, conocido como el modelo de $4/3 R_e$, válido para gradientes térmicos típicos diurnos en atmósfera estándar). Así:

$$R'_e \approx \frac{6371}{1 - 0.143} \approx 7434\,\text{km} \quad (\text{con } k = 0.25 \text{ resulta el modelo clásico } 4/3\,R_e = 8495\,\text{km}; \text{ acá se adopta } 8495\,\text{km})$$

#### Distancia al Horizonte Visual Geométrico
La distancia máxima al horizonte aparente $D_{\text{horiz}}$ visible desde una cámara instalada a una altura $H_{\text{cam}}$ sobre el suelo se calcula como:

$$D_{\text{horiz}} \approx \sqrt{2 R'_e H_{\text{cam}}} \approx 4.12 \cdot \sqrt{H_{\text{cam}}\text{ [m]}} \quad [\text{km}]$$

#### Caída de Curvatura de la Tierra
A una distancia horizontal $D$, la superficie terrestre se "deprime" respecto al plano tangente local una magnitud $\Delta h_{\text{drop}}$ dada por:

$$\Delta h_{\text{drop}} \approx \frac{D^2}{2 R'_e} \approx 0.0588 \cdot (D_{\text{km}})^2 \quad [\text{metros}]$$

| Altura de Torre ($H_{\text{cam}}$) | Distancia al Horizonte ($D_{\text{horiz}}$) | Caída @ 2 km ($\Delta h_{\text{drop}}$) | Caída @ 5 km ($\Delta h_{\text{drop}}$) | Caída @ 10 km ($\Delta h_{\text{drop}}$) | Caída @ 15 km ($\Delta h_{\text{drop}}$) |
|---|---|---|---|---|---|
| **$15\,\text{m}$** (mástil ligero rural) | $15.9\,\text{km}$ | $0.24\,\text{m}$ | $1.47\,\text{m}$ | $5.88\,\text{m}$ | $13.24\,\text{m}$ |
| **$25\,\text{m}$** (torre de comunicaciones media) | $20.6\,\text{km}$ | $0.24\,\text{m}$ | $1.47\,\text{m}$ | $5.88\,\text{m}$ | $13.24\,\text{m}$ |
| **$45\,\text{m}$** (torre troncal telecomunicaciones) | $27.6\,\text{km}$ | $0.24\,\text{m}$ | $1.47\,\text{m}$ | $5.88\,\text{m}$ | $13.24\,\text{m}$ |

#### Implicancia Crítica para el Monte Chaqueño:
En el Gran Chaco y Corrientes, el monte nativo tiene una altura media de dosel de **$H_{\text{dosel}} \approx 8\text{ a }12\,\text{m}$**.
* A $D = 10\,\text{km}$, la caída de curvatura terrestre es de $\Delta h_{\text{drop}} \approx 5.88\,\text{m}$.
* Si el nodo está montado en un poste de $15\,\text{m}$, la línea visual hacia el suelo en un foco a distancia $D$ pasa, a una distancia $d$ antes del foco, a una altura aproximada $H_{\text{rayo}}(d) \approx H_{\text{cam}} \cdot d / D$ (despreciando curvatura, que a esta escala es menor). Para $D = 10\,\text{km}$ y un árbol de 10 m a $d = 500\,\text{m}$ del foco, el rayo pasa a $\approx 0.75\,\text{m}$: **cualquier franja de monte entre el nodo y el foco ocluye la base del fuego**. El límite es la vegetación interpuesta, no la curvatura terrestre.
* **Conclusión Operativa:** A distancias mayores a $3\text{--}4\,\text{km}$, **es ópticamente imposible observar la llama en superficie durante el día**. Lo único que emerge sobre el horizonte vegetal es la **columna de humo ascendente**, la cual supera rápidamente los 30 a 150 m de elevación. Por ende, el algoritmo diurno a larga distancia debe concentrarse indefectiblemente en el penacho sobre la línea del horizonte.

---

### 4.3 Cobertura Angular y Arquitectura de Nodos

Dado que la lente teleobjetivo de $f = 16\,\text{mm}$ proporciona un campo de visión horizontal estrecho ($\text{HFOV} \approx 12.5^\circ$):
* Un nodo estático individual sólo puede vigilar un sector angular predefinido de $12.5^\circ$.
* Para aplicaciones de protección perimetral o vigilancia de reservas específicas (e.g., bordes de plantaciones forestales de pino o eucalipto en Corrientes, o límites de parques nacionales), la cámara se orienta de manera fija hacia el corredor de mayor riesgo histórico o coincidente con los vientos dominantes (viento Norte cálido en el Chaco).
* Para cobertura de **$360^\circ$ sobre una misma torre**, se requieren clústeres de nodos independientes o una cabeza indexada con motor paso a paso de ultra-bajo consumo. No obstante, en la arquitectura fija de Flamara, la estrategia más robusta y económica consiste en desplegar nodos orientados estratégicamente formando una red en malla.

---

## 5. Estimación de Distancia al Foco

Uno de los aportes centrales solicitados en esta investigación es determinar cómo calcular la distancia $D$ entre el nodo y el origen del incendio. Se analizan dos enfoques rigurosos: **monocular (un solo nodo)** y **cooperativo (multinodo)**.

```
MÉTODO 1: MONOCULAR (INTERSECCIÓN TERRENO)          MÉTODO 2: MULTINODO (TRIANGULACIÓN AZIMUTAL)
         Horizonte óptico                                                   
 Torre        \                                              Torre A                        Torre B
 [H] ──┐       \ Línea de mira (cabeceo θ)                   (XA, YA)                       (XB, YA)
       │        \                                               \                              /
       │         \ Foco en suelo                                 \  Línea de                /
       │          ▼ (X_f, Y_f)                                    \  marcación βA        / Línea de
───────┴───────────■───────────────                                \                   /  marcación βB
         Distancia D = H / tan(θ)                                   \                 /
                                                                     ▼               ▼
 [Incertidumbre extrema en llanura a > 2 km]                           ■ FOCO (X_F, Y_F)
 [Sensibilidad: ΔD/D ≈ (D/H) · Δθ]                            [Precisión alta: error < 25 m a 5 km]
```

---

### 5.1 Estimación Monocular (Un Solo Nodo)

El método monocular busca determinar la distancia al fuego a partir de las coordenadas del píxel en una única cámara calibrada.

#### 5.1.1 Formulación Matemática: Intersección con el Plano de Tierra
Consideremos una cámara fija ubicada en una torre a una altura $H_{\text{cam}}$ sobre el terreno plano local.
* Sea $c_y$ el punto principal vertical del sensor (en píxeles).
* Sea $f_y$ la distancia focal vertical (en píxeles).
* Sea $\theta_0$ el ángulo de cabeceo (*pitch* o depresión) del eje óptico respecto al horizonte verdadero.
* Para un píxel detectado en la coordenada vertical $y$ (correspondiente al pie de la columna de humo o llama en contacto con el suelo), el ángulo relativo $\alpha$ respecto al eje óptico es:
  $$\alpha = \arctan\left(\frac{y - c_y}{f_y}\right)$$
* El ángulo total de depresión respecto al plano horizontal es $\theta = \theta_0 + \alpha$.
* Bajo la hipótesis de terreno plano, la distancia euclidiana en el suelo $D_{\text{flat}}$ es:
  $$D_{\text{flat}} = \frac{H_{\text{cam}}}{\tan(\theta)} = \frac{H_{\text{cam}}}{\tan\left(\theta_0 + \arctan\left(\frac{y - c_y}{f_y}\right)\right)}$$

Si se incluye la corrección por curvatura terrestre ($R'_e$):
$$\tan(\theta) \approx \frac{H_{\text{cam}} - \Delta h_{\text{drop}}}{D} = \frac{H_{\text{cam}} - \frac{D^2}{2 R'_e}}{D} \implies D^2 + 2 R'_e \tan(\theta) D - 2 R'_e H_{\text{cam}} = 0$$
Resolviendo para $D$:
$$D = -R'_e \tan(\theta) + \sqrt{(R'_e \tan(\theta))^2 + 2 R'_e H_{\text{cam}}}$$

#### 5.1.2 Análisis de Sensibilidad y Propagación de Errores (Por qué diverge en llanura)
Analicemos la derivada de la distancia respecto al ángulo de depresión medido:

$$\frac{\partial D}{\partial \theta} = - \frac{H_{\text{cam}}}{\sin^2(\theta)} = - \frac{H_{\text{cam}} \left(1 + \tan^2(\theta)\right)}{\tan^2(\theta)} \approx - \frac{D^2}{H_{\text{cam}}} \quad (\text{para } \theta \ll 1\,\text{rad})$$

Diferenciando en términos relativos:

$$\frac{\Delta D}{D} \approx - \left(\frac{D}{H_{\text{cam}}}\right) \Delta \theta$$

Evaluemos el impacto de un error angular diminuto $\Delta \theta = 0.05^\circ \approx 0.87\,\text{mrad}$ (equivalente a un desvío de apenas **6 píxeles** en una lente de $16\,\text{mm}$ o una ligera flexión de la torre por ráfagas de viento) para una torre de $H_{\text{cam}} = 20\,\text{m}$:
* A $D = 1000\,\text{m}$ (1 km):
  $$\frac{D}{H_{\text{cam}}} = \frac{1000}{20} = 50 \implies \left|\frac{\Delta D}{D}\right| \approx 50 \cdot 0.00087 = 4.35\% \implies \mathbf{\Delta D \approx 43.5\,\text{m}}$$
* A $D = 2000\,\text{m}$ (2 km):
  $$\frac{D}{H_{\text{cam}}} = \frac{2000}{20} = 100 \implies \left|\frac{\Delta D}{D}\right| \approx 100 \cdot 0.00087 = 8.7\% \implies \mathbf{\Delta D \approx 174\,\text{m}}$$
* A $D = 5000\,\text{m}$ (5 km):
  $$\frac{D}{H_{\text{cam}}} = \frac{5000}{20} = 250 \implies \left|\frac{\Delta D}{D}\right| \approx 250 \cdot 0.00087 = 21.8\% \implies \mathbf{\Delta D \approx 1090\,\text{m}}$$
* A $D = 10\,000\,\text{m}$ (10 km):
  $$\frac{D}{H_{\text{cam}}} = \frac{10\,000}{20} = 500 \implies \left|\frac{\Delta D}{D}\right| \approx 500 \cdot 0.00087 = 43.5\% \implies \mathbf{\Delta D \approx 4350\,\text{m}}$$

> [!CAUTION]
> **Conclusión Física:** En terreno perfectamente llano, **la estimación monocular por depresión angular es matemáticamente hiper-sensible e intratable a distancias mayores a 2 km**. Un error de solo medio píxel o una brisa que incline el mástil 0.05° desplaza la estimación en más de un kilómetro. Además, en el monte chaqueño el pie del fuego en el suelo no es visible (ocluido por el dosel), por lo que el píxel inferior detectado pertenece a una porción del penacho suspendida a varios metros sobre el suelo, introduciendo un sesgo positivo incontrolable.

#### 5.1.3 Trazado de Rayos sobre Modelos Digitales de Elevación (DEM)
En el backend, la línea de mira tridimensional se proyecta contra una malla de elevación digital (**Copernicus DEM GLO-30** o **SRTM** de 30 m de resolución):
1. Desde la posición geodésica del nodo $(\text{Lat}_0, \text{Lon}_0, Z_0 + H_{\text{cam}})$, se emite un rayo vector unitario $\mathbf{v}(\phi, \theta)$ parametrizado por la distancia $s$:
   $$\mathbf{r}(s) = \mathbf{r}_0 + s \cdot \mathbf{v}(\phi, \theta)$$
2. Se muestrea la elevación del terreno $Z_{\text{DEM}}(x, y)$ a lo largo de la proyección del rayo utilizando un paso de integración discreto ($\Delta s = 10\,\text{m}$).
3. El punto de impacto se halla cuando la cota del rayo cruza la cota de la superficie:
   $$F(s) = Z_{\mathbf{r}}(s) - Z_{\text{DEM}}(\mathbf{r}(s)) = 0$$
*Viabilidad en Chaco/Corrientes:* Este método es altamente efectivo en zonas con lomadas o valles (como en zonas de barrancas del río Paraná o mesetas correntinas), pero en llanuras aluviales sin relieve, la intersección del rayo rasante con el DEM adolece de la misma indeterminación numérica descrita en la sección anterior.

#### 5.1.4 Métodos Monoculares Alternativos: Deriva de Viento y Ancho de Pluma
Para superar la limitación de la depresión angular, el backend puede aplicar dos métodos basados en conocimiento físico *a priori*:
1. **Velocidad de Expansión por Viento (Advección Temporal):**
   Si una estación meteorológica cercana, un anemómetro agregado al nodo o el modelo atmosférico regional (GFS / ECMWF) reportan una velocidad de viento transversal $U_{\perp}$ (el BME280 **no mide viento**, sólo T/H/P), y la columna de humo es seguida entre dos fotogramas separados $\Delta t$:
   $$v_{\text{aparente}} = \frac{\Delta x_{\text{px}} \cdot p \cdot D}{f \cdot \Delta t} \implies D = \frac{U_{\perp} \cdot f \cdot \Delta t}{\Delta x_{\text{px}} \cdot p}$$
2. **Prior de Ancho Inicial de Columna:**
   En fases tempranas de ignición ($t < 15\,\text{min}$), el ancho físico medio del núcleo de penacho se encuentra típicamente entre $W_0 \approx 20\text{--}40\,\text{m}$. La distancia se acota mediante el tamaño angular subtendido:
   $$D \approx \frac{W_0}{\Delta \theta_w} = \frac{W_0 \cdot f_x}{\Delta x_{\text{px}}}$$
   Este método provee sólo un orden de magnitud (el ancho real varía con combustible, viento y edad del fuego; error típico del orden de ±50 % o más), útil como cota inicial para el operador.

---

### 5.2 Estimación Multinodo (Triangulación por Azimut)

La triangulación azimutal entre dos o más torres de observación fijas es el método empleado históricamente por los vigías forestales (mediante el alidada de Osborne) y automatizado en sistemas modernos como ALERTCalifornia y ForestWatch.

#### 5.2.1 Geometría de Intersección de Rumbos en Coordenadas Proyectadas (UTM)
Consideremos dos nodos de la red Flamara, $A$ y $B$, separados por una línea de base $b$, con coordenadas geográficas conocidas proyectadas en la cuadrícula plana **UTM (WGS84 Zona 21S para Chaco/Corrientes)**:
* Nodo $A$: $(X_A, Y_A)$
* Nodo $B$: $(X_B, Y_B)$

Cada nodo detecta el centroide del evento en la coordenada horizontal de píxel $x_A$ y $x_B$. Conociendo el acimut geográfico de calibración de las cámaras ($\Phi_{\text{cam},A}, \Phi_{\text{cam},B}$) y la distancia focal horizontal en píxeles ($f_x$):
$$\Delta \phi_A = \arctan\left(\frac{x_A - c_{x,A}}{f_{x,A}}\right), \quad \Delta \phi_B = \arctan\left(\frac{x_B - c_{x,B}}{f_{x,B}}\right)$$
Los ángulos de rumbo o acimut geográfico hacia el fuego son:
$$\beta_A = \Phi_{\text{cam},A} + \Delta \phi_A, \quad \beta_B = \Phi_{\text{cam},B} + \Delta \phi_B$$

Las líneas de marcación (*lines of bearing*) en el plano UTM se expresan como:
$$\sin(\beta_A)(Y_F - Y_A) - \cos(\beta_A)(X_F - X_A) = 0$$
$$\sin(\beta_B)(Y_F - Y_B) - \cos(\beta_B)(X_F - X_B) = 0$$

Expresando en forma matricial $\mathbf{A} \mathbf{x}_F = \mathbf{b}$:
$$\begin{bmatrix} -\cos(\beta_A) & \sin(\beta_A) \\ -\cos(\beta_B) & \sin(\beta_B) \end{bmatrix} \begin{bmatrix} X_F \\ Y_F \end{bmatrix} = \begin{bmatrix} Y_A \sin(\beta_A) - X_A \cos(\beta_A) \\ Y_B \sin(\beta_B) - X_B \cos(\beta_B) \end{bmatrix}$$

El determinante de la matriz es:
$$\det(\mathbf{A}) = -\cos(\beta_A)\sin(\beta_B) + \sin(\beta_A)\cos(\beta_B) = \sin(\beta_A - \beta_B)$$
$|\det(\mathbf{A})| = \sin\gamma$, con $\gamma\in(0°,180°]$ el ángulo de intersección entre las líneas de vista (no $|\beta_A-\beta_B|$: el seno cambia de signo si la diferencia es negativa, y un cruce cerca de $180°$ es tan malo como cerca de $0°$).

La solución cerrada (Cramer, mismo denominador que $\det$) es:
$$X_F = \frac{(Y_A - Y_B)\sin(\beta_A)\sin(\beta_B) - X_A \cos(\beta_A)\sin(\beta_B) + X_B \cos(\beta_B)\sin(\beta_A)}{\sin(\beta_A - \beta_B)}$$
$$Y_F = \frac{(X_B - X_A)\cos(\beta_A)\cos(\beta_B) + Y_A \sin(\beta_A)\cos(\beta_B) - Y_B \sin(\beta_B)\cos(\beta_A)}{\sin(\beta_A - \beta_B)}$$

Las distancias individuales desde cada nodo al foco se obtienen directamente:
$$D_A = \sqrt{(X_F - X_A)^2 + (Y_F - Y_A)^2}, \quad D_B = \sqrt{(X_F - X_B)^2 + (Y_F - Y_B)^2}$$

#### 5.2.2 Análisis de Incertidumbre y Dilución Geométrica de Precisión (GDOP)
Si la medición del acimut de cada nodo tiene una desviación estándar instrumental $\sigma_\beta$ (típicamente $\sigma_\beta \approx 0.1^\circ\text{ a }0.2^\circ = 1.7\text{ a }3.5\,\text{mrad}$ para una cámara M12 calibrada), la matriz de covarianza de la posición estimada $\mathbf{P}_F$ viene dada por:
$$\mathbf{P}_F = \left(\mathbf{J}^T \mathbf{R}^{-1} \mathbf{J}\right)^{-1}$$
donde $\mathbf{J}$ es el Jacobiano de las funciones de marcación y $\mathbf{R} = \text{diag}(\sigma_{\beta_A}^2, \sigma_{\beta_B}^2)$.

El error radial cuadrático medio (*Root Mean Square Error*, RMSE) de la posición geográfica es:
$$\sigma_{\text{pos}} \approx \frac{\sqrt{D_A^2 + D_B^2}}{\sin(\gamma)} \cdot \sigma_\beta$$

*Ejemplo de Rendimiento Operativo:*
* Supongamos dos nodos separados por una línea de base $b = 5\,\text{km}$.
* Foco situado a $D_A = 5000\,\text{m}$ y $D_B = 5000\,\text{m}$ con un ángulo de cruce óptimo $\gamma \approx 60^\circ$ ($\sin(60^\circ) = 0.866$).
* Incertidumbre angular: $\sigma_\beta = 0.15^\circ \approx 0.0026\,\text{rad}$ ($\approx 19$ píxeles en lente de 16 mm con OV2640; el error lo domina la calibración del acimut, no la resolución).
$$\sigma_{\text{pos}} \approx \frac{\sqrt{5000^2 + 5000^2}}{0.866} \cdot 0.0026 \approx \frac{7071}{0.866} \cdot 0.0026 \approx \mathbf{21.2\,\text{metros}}$$

> [!IMPORTANT]
> **Veredicto Técnico:** La triangulación azimutal multinodo reduce el error de localización de más de **$1000\,\text{m}$ (monocular)** a menos de **$25\,\text{m}$**. Este resultado permite guiar brigadas terrestres o aeronaves hidrantes directamente a la coordenada catastral del foco sin ambigüedad alguna.
>
> **Costo de diseño:** para triangular, dos nodos tienen que ver la MISMA zona. Con HFOV ≈ 12.5° eso duplica los nodos por área cubierta; donde sólo hay un nodo, la distancia queda como cota gruesa. Además, con varios focos simultáneos hay que asociar qué rumbo de A corresponde a cuál de B (usar tiempo, elevación y ancho angular).

---

## 6. Estimación del Tamaño, Volumen y Severidad del Incendio

Una vez estimada la distancia $D$ (preferentemente mediante triangulación en el backend), es posible convertir las mediciones en píxeles de la cámara en parámetros biofísicos de comportamiento del fuego.

### 6.1 Dimensiones Métricas y Volumen de la Columna de Humo

A partir de la máscara binaria o bounding box suministrada por el modelo TinyML en el nodo:
* Ancho angular aparente: $\Delta \theta_w = \frac{\Delta x_{\text{px}}}{f_x}$
* Alto angular aparente: $\Delta \theta_h = \frac{\Delta y_{\text{px}}}{f_y}$

Las dimensiones métricas reales en el plano ortogonal son:
$$W_{\text{pluma}} = D \cdot \Delta \theta_w = D \cdot \left(\frac{\Delta x_{\text{px}}}{f_x}\right)$$
$$H_{\text{pluma}} = D \cdot \Delta \theta_h = D \cdot \left(\frac{\Delta y_{\text{px}}}{f_y}\right)$$

#### Modelado Volumétrico del Penacho
1. **Modelo de Cono Truncado Invertido (Teoría de Pluma Flotante):**
   Según la teoría clásica de penachos boyantes en atmósfera libre (Morton, Taylor & Turner, 1956), el radio de la columna $R(z)$ crece linealmente con la altura $z$ debido al arrastre turbulento de aire circundante (*entrainment*):
   $$\frac{dR}{dz} = \frac{6}{5}\alpha \approx 0.12\text{ a }0.15$$
   donde $\alpha$ es el coeficiente de arrastre entrainment ($\alpha \approx 0.10$ en aire calmo).
   Modelando la pluma como un cono truncado de altura $H_{\text{pluma}}$, radio basal $R_{\text{base}} = W_{\text{base}}/2$ y radio superior $R_{\text{top}} = W_{\text{top}}/2$:
   $$V_{\text{pluma}} = \frac{\pi}{3} H_{\text{pluma}} \left(R_{\text{top}}^2 + R_{\text{top}} R_{\text{base}} + R_{\text{base}}^2\right) \quad [\text{m}^3]$$

2. **Modelo de Elipsoide de Dispersión (Puff Gaussiano):**
   Para columnas jóvenes arrastradas fuertemente por viento horizontal:
   $$V_{\text{elip}} = \frac{4}{3} \pi \left(\frac{W}{2}\right) \left(\frac{H}{2}\right) \left(\frac{L_{\text{prof}}}{2}\right)$$
   Asumiendo simetría axial respecto a la dirección del viento ($L_{\text{prof}} \approx W$):
   $$V_{\text{elip}} = \frac{\pi}{6} W^2 H$$

#### Propagación de Incertidumbre en el Volumen:
Dado que el volumen depende cúbicamente de la distancia ($V \propto D^3 \cdot \Delta\theta_w^2 \cdot \Delta\theta_h$):
$$\frac{\sigma_V}{V} \approx \sqrt{9 \left(\frac{\sigma_D}{D}\right)^2 + 4 \left(\frac{\sigma_{\theta_w}}{\theta_w}\right)^2 + \left(\frac{\sigma_{\theta_h}}{\theta_h}\right)^2}$$
* Si la distancia se obtiene por triangulación multinodo ($\sigma_D / D \approx 2\%$), el error relativo de volumen es de $\approx 6\text{--}10\%$.
* Si la distancia se estimó de manera monocular con un error del $30\%$, el error del volumen supera el **$90\%$**, demostrando nuevamente la necesidad del enlace cooperativo.

---

### 6.2 Detección de Fuego Nocturno: Área de Llama e Intensidad de Byram

Durante la noche (con el sensor en modo NoIR, alta ganancia analógica y exposición prolongada de $100\text{--}200\,\text{ms}$), los frentes de fuego se manifiestan como agrupaciones de píxeles hiper-brillantes sobre un fondo negro térmico.

#### Área Proyectada de Llama
$$A_{\text{llama}} = N_{\text{px\_llama}} \cdot (\text{GSD})^2 = N_{\text{px\_llama}} \cdot \left(\frac{p \cdot D}{f}\right)^2 \quad [\text{m}^2]$$

> [!WARNING]
> Según §4.2, a más de 3–4 km la llama queda ocluida por el monte. La estimación por longitud de llama sólo aplica a distancias cortas (< ~2 km), a fuegos de copa o a llamas sobre el dosel. El área nocturna además se sobreestima por *blooming* del sensor.

#### Intensidad de Línea de Fuego de Byram ($I$)
La **Intensidad de Byram** (Byram, 1959; Alexander, 1982) es el indicador físico estándar internacional para cuantificar la energía liberada por unidad de longitud del frente de fuego ($\text{kW/m}$):
$$I = H_{\text{comb}} \cdot w \cdot r$$
donde $H_{\text{comb}}$ es el calor de combustión ($\approx 18\,700\,\text{kJ/kg}$ para biomasa vegetal), $w$ es el combustible consumido ($\text{kg/m}^2$) y $r$ es la velocidad de avance ($\text{m/s}$).

Byram y Alexander dedujeron la correlación alométrica empírica universal que vincula la **longitud visual de la llama ($L_f$, en metros)** con la intensidad $I$:

$$I = 259.83 \cdot (L_f)^{2.174} \quad [\text{kW/m}]$$

Inversamente, la longitud de llama teórica es:
$$L_f = 0.0775 \cdot (I)^{0.46} \quad [\text{metros}]$$

A partir de la altura vertical de píxeles detectada en la cámara $h_{\text{px}}$ y el ángulo de inclinación de la llama respecto al suelo $\theta_{\text{llama}}$:
$$L_f \approx \frac{D \cdot h_{\text{px}} \cdot p}{f \cdot \cos(\theta_{\text{llama}})}$$

| Longitud de Llama ($L_f$) | Intensidad de Byram ($I$) | Clase de Severidad | Comportamiento Típico en Gran Chaco / Corrientes | Capacidad de Extinción Operativa |
|---|---|---|---|---|
| **$< 1.0\,\text{m}$** | $< 260\,\text{kW/m}$ | **Baja** | Fuego superficial en pastizal ralo o estero seco | Ataque directo con herramientas manuales (batefuegos) |
| **$1.0\text{--}2.5\,\text{m}$** | $260\text{--}1800\,\text{kW/m}$ | **Moderada** | Frente activo en pajonales densos y sotobosque | Difícil con ataque manual; requiere autobombas o líneas de cortafuego |
| **$2.5\text{--}3.5\,\text{m}$** | $1800\text{--}3700\,\text{kW/m}$ | **Alta** | Quema vigorosa de arbustales y monte medio | Ataque directo imposible; requiere maquinaria pesada y descargas aéreas |
| **$> 3.5\,\text{m}$** | $> 3700\,\text{kW/m}$ | **Extrema** | Fuego de copas en bosque nativo o pinar comercial | Comportamiento eruptivo; combate restringido a flancos defensivos |

---

### 6.3 Modelos Analíticos de Ascenso de Pluma: Inversión de Briggs

En meteorología de incendios forestales, la altura que alcanza la columna de humo sobre el terreno está gobernada por la tasa de calor liberada por la combustión y las condiciones de estabilidad de la atmósfera circundante.

#### 6.3.1 Formulación de Penacho Boyante Doblado por Viento (Briggs, 1969, 1975)
Para un incendio que genera un penacho continuo sometido a un viento medio $U$ ($\text{m/s}$), el flujo de flotabilidad convectivo $F_b$ ($\text{m}^4/\text{s}^3$) se define como:

$$F_b = \frac{g}{\pi \rho_a c_p T_a} \dot{Q}_c$$

donde:
* $g = 9.81\,\text{m/s}^2$: aceleración de la gravedad.
* $\rho_a \approx 1.20\,\text{kg/m}^3$: densidad del aire a temperatura ambiente (calculada con el sensor BME280: $\rho_a = P / (R_{\text{esp}} T)$).
* $c_p \approx 1005\,\text{J/(kg}\cdot\text{K)}$: calor específico del aire a presión constante.
* $T_a$: temperatura ambiente absoluta ($\text{K}$).
* $\dot{Q}_c$: **tasa de liberación de calor convectivo del fuego ($\text{Watts}$)** (típicamente entre el 50% y 70% del calor total liberado).

Briggs demostró que en una atmósfera neutral no estratificada, la sobreelevación de la pluma $\Delta h(x)$ a una distancia a sotavento $x$ responde a la **ley de los dos tercios**:

$$\Delta h(x) = \left(\frac{3}{\beta_1^2}\right)^{1/3} \frac{F_b^{1/3} x^{2/3}}{U} \approx 1.6 \cdot \frac{F_b^{1/3} x^{2/3}}{U} \quad (\text{adoptando } \beta_1 \approx 0.6)$$

#### 6.3.2 Inversión del Modelo de Briggs para Estimar la Potencia del Incendio
Si el backend mide ópticamente en la imagen la altura de equilibrio o estabilización de la columna $\Delta h_{\text{obs}}$ a una distancia $x_{\text{obs}}$ del foco, y se conoce la velocidad del viento $U$ (de una estación meteorológica cercana o del modelo GFS/ECMWF; el BME280 no mide viento):

$$F_b = \left( \frac{\Delta h_{\text{obs}} \cdot U}{1.6 \cdot x_{\text{obs}}^{2/3}} \right)^3$$

Despejando la tasa convectiva de liberación de energía $\dot{Q}_c$:

$$\dot{Q}_c = F_b \cdot \frac{\pi \rho_a c_p T_a}{g} \quad [\text{Watts}]$$

*Estimación de la Tasa de Consumo de Biomasa:*
Conociendo el calor de combustión efectivo de la vegetación chaqueña ($H_{\text{comb}} \approx 18.7\,\text{MJ/kg}$) y la fracción convectiva ($\chi_c \approx 0.60$):
$$\dot{M}_{\text{biomasa}} = \frac{\dot{Q}_c}{\chi_c \cdot H_{\text{comb}}} \quad [\text{kg de combustible / segundo}]$$

> [!TIP]
> **Alcance:** $F_b \propto (\Delta h \cdot U)^3$, así que un 20 % de error en $\Delta h$ o en $U$ se convierte en ~70 % en $\dot{Q}_c$. Tomarlo como orden de magnitud, no como medición.
>
> **Aporte Original:** La inversión de las ecuaciones de Briggs en el backend transforma a Flamara de un simple detector de humo en un **analizador biofísico de potencia de fuego**, permitiendo categorizar el evento no sólo por su presencia, sino por su intensidad en Megavatios (MW), dato crucial para la priorización del despacho de bomberos.

---

## 7. Técnicas para Series Temporales de Fotos Espaciadas (~15 min)

En el nodo Flamara, la captura espaciada a $\Delta t = 15\,\text{minutos}$ impone un desafío singular: **la resta de cuadros clásica utilizada en video continuo ($\Delta I = |I_t - I_{t-1}|$) produce muchos falsos positivos** por cambios de iluminación; sirve como etapa de propuesta, no como decisión.

### 7.1 Física de la Escena a Intervalos de 15 Minutos
* **Movimiento Solar:** La Tierra rota $15^\circ$ por hora, lo que equivale a un desplazamiento de la posición solar de **$3.75^\circ$ cada 15 minutos**. Este cambio en el ángulo cenital y acimutal produce:
  * Elongación y rotación visible de las sombras arrojadas por árboles, alambrados y postes.
  * Cambios pronunciados en la reflectancia bidireccional (BRDF) de los pastizales.
* **Dinámica de Nubes:** A lo largo de 15 minutos, las nubes meteorológicas se desplazan varios kilómetros, proyectando sombras oscuras en movimiento sobre el suelo que generan bordes de gradiente de alta amplitud en la resta de imágenes.

---

### 7.2 Algoritmos Robustos para Serie Temporal Espaciada

Para discriminar con éxito entre cambios de iluminación natural y la aparición de una columna de humo, se proponen cuatro estrategias viables:

```
        FOTOGRAMA ACTUAL I(t)                    BASE HISTÓRICA / REGLAS FÍSICAS
                 │                                              │
                 ▼                                              ▼
       ┌───────────────────┐                         ┌──────────────────────┐
       │ Extracción DWT    │                         │ Modelo Solar RTC     │
       │ de Alta Frecuencia│                         │ (Diccionario Horario)│
       └─────────┬─────────┘                         └──────────┬───────────┘
                 │                                              │
                 └───────────────────────┬──────────────────────┘
                                         ▼
                 ┌───────────────────────────────────────────────┐
                 │       TEST TRIPLE DE DISCRIMINACIÓN           │
                 │                                               │
                 │ 1. ¿Caída localizada de textura DWT? (Humo)   │
                 │ 2. ¿Aumento relativo de canal oscuro DCP?     │
                 │ 3. ¿Descarte por consistencia solar?          │
                 └───────────────────────┬───────────────────────┘
                                         │
                                         ▼
                          ALERTA CONFIRMADA / RECHAZO
```

#### 1. Diccionario de Fondo por Elevación Solar (Solar-Angle Codebook)
En lugar de comparar contra la imagen inmediatamente anterior ($t - 15\,\text{min}$), el sistema compara contra un **fotograma histórico de referencia capturado en condiciones despejadas a la misma elevación solar ($\pm 2^\circ$)**.
* Mediante el RTC DS3231, el ESP32-S3 calcula la posición solar astronómica con el algoritmo ultraligero **PSA (Plataforma Solar de Almería, Blanco-Muriel et al., 2001)**, el cual consume menos de $1\,\text{kB}$ de Flash y se ejecuta en $< 0.1\,\text{ms}$.
* Las referencias se almacenan en la microSD indexadas por rangos de elevación solar ($10^\circ, 15^\circ, \dots, 65^\circ$). Al comparar escenas con idéntica geometría de iluminación, las sombras proyectadas coinciden geométricamente, suprimiendo los falsos positivos por rotación de sombra.

#### 2. Detección por Caída Local de Energía DWT (Efecto Filtro Pasa-Bajos)
Como se formuló en el pipeline de Flamara (`modulos/pipeline_sim.py`), la transformada wavelet discreta 2D (DWT Haar) descompone la imagen en subbandas: aproximación ($LL$) y detalles de alta frecuencia horizontal ($LH$), vertical ($HL$) y diagonal ($HH$).
* **Física del Humo:** Una columna de humo es un medio dispersivo túrbido que actúa como un **filtro pasa-bajos espacial**. Al propagarse sobre el fondo vegetal, suaviza los bordes finos de las ramas y hojas, provocando una caída abrupta en la energía de altas frecuencias:
  $$E_{\text{detalles}} = \sum (LH^2 + HL^2 + HH^2)$$
* **Sombras de Nubes:** Una nube proyecta una sombra que reduce la intensidad media ($LL$), pero los bordes de la vegetación (hojas, pasto) siguen presentes bajo la penumbra, conservando la energía relativa de alta frecuencia:
  $$\text{Ratio}_{\text{textura}} = \frac{\sum (LH^2 + HL^2 + HH^2)}{LL^2}$$
* Si $\text{Ratio}_{\text{textura}}(t) < 0.4 \cdot \text{Ratio}_{\text{textura}}(\text{fondo})$, el nodo confirma la presencia de un aerosol difusor (humo) y no una simple sombra.

#### 3. Razón Temporal del Canal Oscuro (Dark Channel Prior Temporal)
Basado en el principio de He, Sun & Tang (2010):
$$J^{\text{dark}}(x) = \min_{c \in \{R,G,B\}} \left( \min_{y \in \Omega(x)} I^c(y) \right)$$
En escenas libres de humo o bruma, al menos un canal de color tiene valores de intensidad cercanos a cero en zonas sombreadas o vegetadas. La presencia de humo introduce radiación de fondo dispersada (*airlight* o velo atmosférico), lo que **eleva sustancialmente el valor mínimo de $J^{\text{dark}}$**.
* Un incremento localizado $\Delta J^{\text{dark}} > 30$ acompañado de una baja saturación cromática ($S_{\text{HSV}} < 0.20$) es una firma fuerte de humo; niebla, bruma matinal y polvo pueden replicarla, por lo que debe combinarse con los otros tests. Válido sólo debajo del horizonte: sobre el cielo el DCP no aplica.

---

## 8. Calibración de Cámara (Lentes M12 12–25 mm)

Para garantizar que los ángulos $\theta_x, \theta_y$ extraídos de los píxeles se traduzcan en coordenadas geográficas confiables, es imprescindible calibrar rigurosamente los parámetros intrínsecos y extrínsecos del sensor óptico.

### 8.1 Calibración Intrínseca (Modelo de Brown-Conrady)

La proyección de un punto tridimensional en el sistema de coordenadas de la cámara hacia el plano del sensor se rige por la matriz intrínseca $\mathbf{K}$:

$$\mathbf{K} = \begin{bmatrix} f_x & 0 & c_x \\ 0 & f_y & c_y \\ 0 & 0 & 1 \end{bmatrix}$$

donde:
* $f_x, f_y$: distancias focales expresadas en píxeles ($f_x = f / p_x, f_y = f / p_y$).
* $c_x, c_y$: coordenadas del punto principal (intersección del eje óptico con el sensor, nominalmente $(800, 600)$ en UXGA o $(160, 120)$ en QVGA).

#### Distorsión Óptica en Lentes Teleobjetivo M12
A diferencia de las lentes gran angular (que presentan fuerte distorsión de barril), las lentes teleobjetivo de distancia focal media a larga ($f = 12\text{ a }25\,\text{mm}$) presentan distorsiones geométricas más bajas ($\approx 1\text{--}3\%$), predominantemente de tipo acerico (*pincushion*) o barril residual ligero. Se modela mediante los coeficientes radiales ($k_1, k_2$) y tangenciales ($p_1, p_2$):

$$x_{\text{corregido}} = x (1 + k_1 r^2 + k_2 r^4) + [2 p_1 x y + p_2 (r^2 + 2x^2)]$$
$$y_{\text{corregido}} = y (1 + k_1 r^2 + k_2 r^4) + [p_1 (r^2 + 2y^2) + 2 p_2 x y]$$
con $r^2 = x^2 + y^2$.

* **Protocolo de Calibración:** Se ejecuta una única vez en laboratorio previo al despliegue mediante el método clásico de Zhang (2000), capturando 15–20 tomas de un patrón de tablero de ajedrez o ChArUco a diferentes distancias y orientaciones. Los parámetros $(\mathbf{K}, k_1, k_2, p_1, p_2)$ se guardan en el archivo de configuración del firmware (`salidas/calibracion_optica.json`).

---

### 8.2 Calibración Extrínseca en Campo

La matriz extrínseca define la transformación rígida $[\mathbf{R} | \mathbf{t}]$ entre el sistema de coordenadas geográfico local (NED: Norte-Este-Abajo) y el sistema de referencia de la cámara.

#### Anclaje de Actitud mediante la Línea de Horizonte
Como se desarrolló en el módulo `modulos/horizonte_sim.py`, el nodo Flamara detecta la recta del horizonte $y(x) = m \cdot x + b$ mediante reducción anisotrópica robusta. Esta recta provee una referencia física absoluta contra el vector de la gravedad:
1. **Ángulo de Alabeo (*Roll* $\psi$):**
   $$\psi = \arctan(m)$$
   Permite rotar digitalmente la imagen para que el horizonte quede perfectamente horizontal ($m = 0$).
2. **Ángulo de Cabeceo (*Pitch* $\theta_0$):**
   La distancia vertical desde el centro óptico $c_y$ hasta la recta de horizonte evaluada en el centro $x = c_x$ determina la depresión angular del sensor:
   $$\theta_0 = \arctan\left(\frac{y_{\text{horiz}}(c_x) - c_y}{f_y}\right)$$

#### Anclaje de Acimut Absoluto (*Yaw* $\Phi_{\text{cam}}$)
El acimut de orientación del nodo no debe depender de brújulas magnéticas digitales de bajo costo, las cuales sufren severas distorsiones por las estructuras metálicas de las torres y cables de arriostramiento. Se aplican dos métodos de ingeniería:
1. **Referencia Geográfica Conocida:** Alineación visual contra un hito georreferenciado visible en la lejanía (e.g., antena de telefonía, silo, cruce de rutas o torre de alta tensión).
2. **Calibración Solar Astronómica:** Durante la instalación, se captura una fotografía del disco solar al amanecer o atardecer anotando la estampa de tiempo exacta del RTC DS3231. El acimut del sol $\Phi_{\text{sol}}$ se calcula analíticamente y la orientación absoluta del nodo se despeja como:
   $$\Phi_{\text{cam}} = \Phi_{\text{sol}} - \arctan\left(\frac{x_{\text{sol}} - c_x}{f_x}\right)$$

---

## 9. Diseño de Trama y Enlace LoRa (SX1276, 915 MHz)

El enlace de datos es el recurso más restringido de todo el sistema. Operando en la banda ISM de 915 MHz (estándar regional AU915 / US915 para Argentina, regulado por ENACOM), la comunicación debe minimizar el tiempo al aire (*airtime*) para preservar energía y respetar los límites de ocupación de espectro.

### 9.1 Comparativa Arquitectónica: Telemetría 1D vs. Recorte 2D

```
OPCIÓN 1: TRAMA 1D (SIEMPRE)                      OPCIÓN 2: RECORTE 2D (SÓLO CON ALARMA CONFIRMADA)
┌─────────────────────────────────┐               ┌──────────────────────────────────────────────┐
│  24 BYTES TOTALES               │               │  600 - 1200 BYTES TOTALES                    │
│  - ID, Tiempo, Estado           │               │  - Requiere 12 a 24 paquetes fragmentados    │
│  - Acimut y Elevación           │               │  - Protocolo complejo ACK / Retransmisión    │
│  - Dimensiones angulares        │               │  - Airtime acumulado: > 4.5 a 10 segundos    │
│  - BME280 (T/H/P) + Batería     │               │  - Enorme probabilidad de colisión o pérdida │
│  - Cuadrícula FOMO (bitmask)    │               └──────────────────────────────────────────────┘
└────────────────┬────────────────┘                                       ▲
                 │                                                        │
                 ▼                                                        │
        Airtime: 61 ms (SF7)                                     Airtime: > 4500 ms (SF10)
        Consumo: 0.02 Joules                                     Consumo: > 1.5 Joules
```

#### Análisis Numérico de Tiempo al Aire (SX1276)
La duración de un paquete LoRa $T_{\text{paquete}}$ se calcula según la especificación de Semtech:
$$T_{\text{paquete}} = (N_{\text{preámbulo}} + 4.25 + N_{\text{payload\_sim}}) \cdot T_{\text{sim}}$$
donde $T_{\text{sim}} = 2^{\text{SF}} / \text{BW}$. Para $\text{BW} = 125\,\text{kHz}$, preámbulo de 8 símbolos y $\text{CR} = 4/5$:
* **SF7:** $T_{\text{sim}} = 1.024\,\text{ms}$
* **SF10:** $T_{\text{sim}} = 8.192\,\text{ms}$

| Tipo de Carga | Tamaño | Airtime @ SF7 / 125 kHz | Airtime @ SF10 / 125 kHz | Energía Consumida ($3.3\,\text{V}, 100\,\text{mA}$) | Viabilidad Operativa |
|---|---|---|---|---|---|
| **Telemetría 1D (Flamara)** | **24 Bytes** | **$61.7\,\text{ms}$** | **$370.7\,\text{ms}$** | **$0.020\text{--}0.122\,\text{J}$** | **Óptima (Poco tiempo al aire, robusta)** |
| **Bitmask DWT + FOMO** | 48 Bytes | $102.7\,\text{ms}$ | $616.4\,\text{ms}$ | $0.034\text{--}0.203\,\text{J}$ | Muy buena |
| **Recorte 2D ($32 \times 32$ int8)** | 256 Bytes | $450.8\,\text{ms}$ | $2883.6\,\text{ms}$ (Fragm.) | $0.149\text{--}0.951\,\text{J}$ | Límite regulatorio excedido a SF10 |
| **Mini-JPEG ($64 \times 64$ comprimido)** | 800 Bytes | $1450\,\text{ms}$ (Fragm.) | $9200\,\text{ms}$ (Fragm.) | $> 3.0\,\text{J}$ | **Viable sólo por evento** (a SF7, una vez por alarma) |

---

### 9.2 Estructura Exacta de la Trama LoRa 1D (24 Bytes)

Se define la estructura binaria empaquetada que el ESP32-S3 transfiere por UART al Heltec LoRa para su emisión inmediata:

```c
// Estructura binaria de la trama Flamara (Exactamente 24 bytes, packed)
typedef struct __attribute__((packed)) {
    uint8_t  node_id;          // [Byte 0] Identificador único del nodo (0 - 255)
    uint32_t timestamp;        // [Bytes 1-4] Tiempo Unix epoch (DS3231 RTC)
    uint8_t  status_flags;     // [Byte 5] BITS: [7: Alarma][6: Humo][5: Fuego][4: Noche][3..0: Batería %]
    int16_t  azimuth_cdeg;     // [Bytes 6-7] Acimut geográfico centroide en centigrados (-18000 a +18000 = -180.00° a +180.00°)
    int16_t  elevation_cdeg;   // [Bytes 8-9] Elevación angular centroide en centigrados (-9000 a +9000 = -90.00° a +90.00°)
    uint16_t span_x_cdeg;      // [Bytes 10-11] Ancho angular de la columna (en centigrados de grado)
    uint16_t span_y_cdeg;      // [Bytes 12-13] Alto angular de la columna (en centigrados de grado)
    uint8_t  confidence;       // [Byte 14] Nivel de confianza de la CNN FOMO (0 - 100%)
    uint16_t grid_activation;  // [Bytes 15-16] Bitmask 16 bits de la cuadrícula 4x4 FOMO activa
    int16_t  temperature_cC;   // [Bytes 17-18] BME280: Temperatura ambiente en centígrados de °C (e.g. 2530 = 25.30 °C)
    uint16_t humidity_cRH;     // [Bytes 19-20] BME280: Humedad relativa en centésimas % (e.g. 6540 = 65.40 %)
    uint16_t pressure_hPa;     // [Bytes 21-22] BME280: Presión atmosférica en décimas de hPa (e.g. 10132 = 1013.2 hPa)
    uint8_t  crc8;             // [Byte 23] Checksum CRC-8 de integridad de la trama
} FlamaraTelemetryPacket_t;
```

---

## 10. Arquitectura de Reparto: Nodo (ESP32-S3) vs. Gateway / Backend

Para maximizar la autonomía energética del nodo y optimizar la precisión analítica del sistema completo, se establece una división estricta de responsabilidades de cómputo:

```
┌──────────────────────────────────────────────┐
│          NODO REMOTO (ESP32-S3)              │
│       Presupuesto: < 800 ms, < 100 kB RAM    │
├──────────────────────────────────────────────┤
│ 1. Despertar periódico por RTC DS3231 (15m)  │
│ 2. Encendido de riel de cámara vía MOSFET    │
│ 3. Lectura de T, H, P desde sensor BME280    │
│ 4. Captura fotograma DVP (UXGA / SVGA)       │
│ 5. Detección de horizonte (reducción anis.)  │
│ 6. Recorte / Tiling sobre franja horizonte   │
│ 7. Inferencia CNN int8 (FOMO / MobileNetV1)  │
│ 8. Extracción de acimut, elevación y anchos  │
│ 9. Armado y transmisión de trama LoRa (24 B) │
│ 10. Guardado de fotograma RAW en microSD     │
│ 11. Apagado de cámara y entrada en Deep Sleep│
└──────────────────────┬───────────────────────┘
                       │
                       │ Trama LoRa 24 Bytes (915 MHz)
                       ▼
┌──────────────────────────────────────────────┐
│             GATEWAY LOCAL                    │
├──────────────────────────────────────────────┤
│ 1. Recepción de paquete de radio SX1276      │
│ 2. Incorporación de metadatos (RSSI, SNR)    │
│ 3. Reenvío IP vía enlace celular 4G o WiFi   │
└──────────────────────┬───────────────────────┘
                       │
                       │ Protocolo MQTT / HTTPS
                       ▼
┌──────────────────────────────────────────────┐
│        BACKEND CENTRAL (SERVIDOR CLOUD)      │
├──────────────────────────────────────────────┤
│ 1. Recepción y desempacado de telemetrías    │
│ 2. Correlación multinodo (fusión temporal)   │
│ 3. Triangulación por intersección de acimut  │
│ 4. Trazado de rayos contra DEM (Copernicus)  │
│ 5. Cálculo de distancia definitiva al foco   │
│ 6. Cálculo de ancho métrico y volumen pluma  │
│ 7. Cálculo de Byram (kW/m) y Briggs (MW)     │
│ 8. Emisión de alerta GIS (GeoJSON, Telegram) │
│ 9. Almacenamiento en base histórica          │
└──────────────────────────────────────────────┘
```

### Tabla Comparativa de Responsabilidades

| Tarea de Procesamiento | Dónde Corre | Justificación Técnica | Tiempo / Consumo Típico |
|---|---|---|---|
| **Cálculo de Horizonte Anisotrópico** | **Nodo (ESP32-S3)** | Se calcula sobre una matriz de proyección reducida ($120 \times 16$), insume $< 15\,\text{ms}$ y define el recorte de interés. | $15\,\text{ms}$ / Memoria $< 4\,\text{kB}$ |
| **Inferencia TinyML (FOMO int8)** | **Nodo (ESP32-S3)** | Procesa la franja recortada ($96 \times 96$) con aceleración ESP-NN sin requerir transmisión de imágenes. | $110\text{--}380\,\text{ms}$ / Arena $70\,\text{kB}$ |
| **Empaquetado de Trama 1D** | **Nodo (ESP32-S3)** | Convierte activaciones de la red y lecturas de sensores en 24 bytes para salida UART al Heltec LoRa. | $< 1\,\text{ms}$ |
| **Almacenamiento de Evidencia** | **Nodo (MicroSD)** | Guarda el fotograma JPEG/RAW completo para auditoría forense y re-entrenamiento offline sin costo de radio. | $80\text{--}120\,\text{ms}$ |
| **Triangulación Multinodo** | **Backend Cloud** | Requiere asociar vectores de rumbos provenientes de nodos geográficamente dispersos en tiempo casi real. | Servidor central ($< 5\,\text{ms}$) |
| **Raycasting sobre DEM (30 m)** | **Backend Cloud** | Mallas de elevación pesadas (decenas de MB por cuadrícula SRTM); prohibitivo para la Flash/RAM del microcontrolador. | Servidor central ($< 20\,\text{ms}$) |
| **Modelos Briggs / Byram / Volumen** | **Backend Cloud** | Requieren la distancia precisa obtenida de la triangulación, datos meteorológicos sinópticos y precisión en coma flotante doble. | Servidor central ($< 1\,\text{ms}$) |
| **Generación de Alertas GIS** | **Backend Cloud** | Publicación en dashboards web (Leaflet/Mapbox), despacho de SMS/Telegram y generación de capas GeoJSON para brigadistas. | Servidor central |

---

## 11. Recomendaciones Priorizadas y Experimentos Concretos para Notebooks

Para trasladar de inmediato los hallazgos de este informe al banco de pruebas en Python del proyecto (`Pruebas/notebooks/`), se estructuran las siguientes recomendaciones priorizadas por su relación impacto/esfuerzo:

### 11.1 Matriz de Priorización

```
ALTO  ▲
      │    [P1] Notebook 8:                    [P2] Notebook 9:
      │    Calibración Óptica & DEM            Triangulación Multinodo (Azimut)
      │    (Lente 16 mm + Sensibilidad Monoc.) (Monte Carlo, GDOP y Error < 25 m)
I     │
M     │──────────────────────────────────────────────────────────────────────────
P     │
A     │    [P3] Notebook 10:                   [P4] Notebook 11:
C     │    Serie Temporal 15 min               Inversión de Briggs & Byram
T     │    (Fondo solar + DWT vs. sombras)     (Volumen de pluma + MW de fuego)
O     │
BAJO  ▼
      └─────────────────────────────────────────────────────────────────────────▶
       BAJO                     DIFICULTAD / ESFUERZO                      ALTO
```

1. **Prioridad 1 (P1): Simulación de Calibración Óptica y Análisis de Sensibilidad Monocular.** Demostrar formalmente en el trabajo final por qué un solo nodo es insuficiente a $>2\,\text{km}$ en terreno llano y fijar la elección de la lente M12 de $16\,\text{mm}$.
2. **Prioridad 2 (P2): Triangulación Cooperativa Multinodo.** Implementar el algoritmo de intersección de rumbos UTM con análisis de covarianza, demostrando la precisión submétrica/decamétrica de la red Flamara.
3. **Prioridad 3 (P3): Procesamiento de Serie Temporal a 15 Minutos.** Evaluar con secuencias reales de FIgLib/HPWREN el rechazo de sombras de nubes mediante la caída de energía de altas frecuencias DWT.
4. **Prioridad 4 (P4): Estimador de Severidad Física (Byram, Briggs y Volumen).** Implementar los módulos biofísicos que consumen la distancia calculada y entregan potencia en MW y severidad operativa.

---

### 11.2 Especificación de Nuevos Notebooks para el Proyecto

#### `notebooks/8_calibracion_optica_dem.ipynb`
* **Objetivo:** Modelar la óptica de las lentes M12 (3.6 mm, 12 mm, 16 mm, 25 mm) acopladas al sensor OV2640 y simular la sensibilidad de la estimación monocular sobre terreno llano.
* **Entradas:** Imágenes sintéticas y reales con horizonte detectado por `modulos/horizonte_sim.py`; perfil topográfico sintético plano y con suave depresión.
* **Procesamiento:**
  * Aplicación del modelo de proyección pinhole con distorsión radial ($k_1, k_2$).
  * Curvas de GSD vs. distancia ($1\text{ a }10\,\text{km}$).
  * Gráfico de dispersión del error de distancia $\Delta D$ frente a perturbaciones de cabeceo $\Delta \theta \in [0.01^\circ, 0.2^\circ]$.
  * Simulación de trazado de rayos monocular contra un modelo DEM plano.
* **Salida Generada:** `salidas/curvas_gsd_optica.csv` y gráfico comparativo de sensibilidad monocular.

#### `notebooks/9_triangulacion_multinodo.ipynb`
* **Objetivo:** Validar el algoritmo de geolocalización por intersección de azimuts entre dos y tres nodos remotos en coordenadas UTM.
* **Entradas:** Coordenadas simuladas de dos torres en Chaco/Corrientes (e.g., separadas 6 km) y un foco de incendio variable en una grilla de $10 \times 10\,\text{km}$.
* **Procesamiento:**
  * Generación de mediciones de acimut con ruido gaussiano $\sigma_\beta \sim \mathcal{N}(0, 0.15^\circ)$.
  * Resolución por mínimos cuadrados de la intersección de rumbos.
  * Cálculo y graficación del mapa de calor de **GDOP (Dilución Geométrica de Precisión)**.
  * Trazado de las elipses de error del 95% de confianza en el plano geográfico.
* **Salida Generada:** `salidas/mapa_gdop_red.png` y tabla de exactitud geográfica.

#### `notebooks/10_serie_temporal_15min.ipynb`
* **Objetivo:** Medir la capacidad de discriminación entre nubes en movimiento y humo incipiente utilizando pares de imágenes espaciadas 15 minutos.
* **Entradas:** Secuencias temporales de incendios reales del dataset **FIgLib (HPWREN)** tomadas a intervalos de 15 minutos (o submuestreadas de las de 1 min).
* **Procesamiento:**
  * Comparación de resta simple $|I_t - I_{t-15}|$ vs. resta normalizada.
  * Evaluación del ratio de textura DWT Haar ($\Delta E_{\text{DWT}}$).
  * Evaluación de la variación de canal oscuro ($\Delta J^{\text{dark}}$).
  * Matriz de confusión: Falsos positivos por sombras de nubes frente a detección de humo real.
* **Salida Generada:** Métricas de ROC-AUC temporal y umbrales óptimos de decisión para el firmware.

#### `notebooks/11_volumen_pluma_byram.ipynb`
* **Objetivo:** Implementar las fórmulas de volumen de cono truncado (Morton-Taylor-Turner), intensidad de línea de fuego de Byram e inversión de pluma de Briggs.
* **Entradas:** Distancias y dimensiones angulares simuladas de los notebooks 6, 8 y 9; T y P del BME280 ($T = 32^\circ\text{C}, P = 1010\,\text{hPa}$) y viento externo (estación/GFS, $U = 4\,\text{m/s}$).
* **Procesamiento:**
  * Cálculo del volumen $V_{\text{pluma}}$ y análisis de propagación de error.
  * Conversión de altura de llama nocturna a intensidad de Byram ($I$) y clasificación de severidad.
  * Inversión de Briggs para estimar el flujo de flotabilidad $F_b$ y la potencia convectiva $\dot{Q}_c$ en MW.
* **Salida Generada:** `salidas/estimacion_severidad_incendio.json` con reporte de biomasa consumida y nivel de peligro para despacho de brigadas.

---

## 12. Referencias Bibliográficas Verificadas

1. **Lostanlen, M., Isla, N., Guillen, J., Zanca, R., Veith, F., Buc, C., & Barriere, V.** (2024, v3 2025). *Constructing a Real-World Benchmark for Early Wildfire Detection with the New PYRONEAR-2025 Dataset*. arXiv preprint. DOI / Enlace: [arXiv:2402.05349](https://arxiv.org/abs/2402.05349).  
   *Aporte a Flamara:* Justifica la inferencia frugal en el borde (*edge*) para detección de humo y provee criterios de etiquetado para penachos lejanos.
2. **Dewangan, A., Pande, Y., Braun, H.-W., Vernon, F., Perez, I., Altintas, I., Cottrell, G. W., & Nguyen, M. H.** (2022). *FIgLib & SmokeyNet: Dataset and Deep Learning Model for Real-Time Wildland Fire Smoke Detection*. Remote Sensing, 14(4), 1007. DOI: [10.3390/rs14041007](https://doi.org/10.3390/rs14041007).  
   *Aporte a Flamara:* Dataset de referencia mundial en cámaras de torre y modelado espacio-temporal de penachos a distancias de 2 a 20 km.
3. **de Venâncio, P. V. A. B., Lisboa, A. C., & Barbosa, A. V.** (2022). *An automatic fire detection system based on deep convolutional neural networks for low-power, resource-constrained devices*. Neural Computing and Applications, 34, 15349–15368. DOI: [10.1007/s00521-022-07467-z](https://doi.org/10.1007/s00521-022-07467-z).  
   *Aporte a Flamara:* Publicación del dataset D-Fire utilizado en los notebooks 4–7 del proyecto; métricas de cuantización int8 para microcontroladores.
4. **Byram, G. M.** (1959). *Combustion of wildland fuels*. In K. P. Davis (Ed.), *Forest Fire: Control and Use* (pp. 61–89). McGraw-Hill, New York.  
   *Aporte a Flamara:* Formulación original de la intensidad de línea de fuego ($I = H w r$), base del cálculo de severidad a partir de longitud de llama.
5. **Alexander, M. E.** (1982). *Calculating and interpreting forest fire intensities*. Canadian Journal of Botany, 60(4), 349–357. DOI: [10.1139/b82-048](https://doi.org/10.1139/b82-048).  
   *Aporte a Flamara:* Deducción de la ecuación alométrica práctica $I = 259.83 \cdot L_f^{2.174}$ utilizada en el backend.
6. **Briggs, G. A.** (1969). *Plume Rise*. U.S. Atomic Energy Commission, Critical Review Series, TID-25075.  
   *Aporte a Flamara:* Teoría analítica del ascenso de penachos doblados por viento y ley de los dos tercios.
7. **Briggs, G. A.** (1975). *Plume rise predictions*. In *Lectures on Air Pollution and Environmental Impact Analyses* (pp. 59–111). American Meteorological Society, Boston.  
   *Aporte a Flamara:* Ecuaciones de penetración en atmósfera neutral y estratificada, base para la inversión de calor convectivo ($\dot{Q}_c$).
8. **Morton, B. R., Taylor, G. I., & Turner, J. S.** (1956). *Turbulent gravitational convection from maintained and instantaneous sources*. Proceedings of the Royal Society of London. Series A, 234(1196), 1–23. DOI: [10.1098/rspa.1956.0011](https://doi.org/10.1098/rspa.1956.0011).  
   *Aporte a Flamara:* Fundamento físico del coeficiente de arrastre turbulento ($\alpha$) y modelo volumétrico de cono invertido.
9. **Wooster, M. J., Zhukov, B., & Oertel, D.** (2003). *Fire radiative energy for quantitative study of biomass burning: derivation from the BIRD experimental satellite sensor*. Remote Sensing of Environment, 86(1), 83–107. DOI: [10.1016/S0034-4257(03)00070-1](https://doi.org/10.1016/S0034-4257(03)00070-1).  
   *Aporte a Flamara:* Marco teórico de la Potencia Radiativa del Fuego (FRP) y consumo de biomasa por unidad de tiempo.
10. **He, K., Sun, J., & Tang, X.** (2010). *Single Image Haze Removal Using Dark Channel Prior*. IEEE Transactions on Pattern Analysis and Machine Intelligence, 33(12), 2341–2353. DOI: [10.1109/TPAMI.2010.168](https://doi.org/10.1109/TPAMI.2010.168).  
    *Aporte a Flamara:* Principio del canal oscuro ($J^{\text{dark}}$) aplicado como detector invariante de aerosoles y humo frente a sombras terrestres.
11. **Zhang, Z.** (2000). *A flexible new technique for camera calibration*. IEEE Transactions on Pattern Analysis and Machine Intelligence, 22(11), 1330–1334. DOI: [10.1109/34.888718](https://doi.org/10.1109/34.888718).  
    *Aporte a Flamara:* Algoritmo matemático estándar para la calibración intrínseca de lentes M12 sobre matrices CMOS.
12. **Blanco-Muriel, M., Alarcón-Padilla, D. C., López-Moratalla, T., & Lara-Coira, M.** (2001). *Computing the solar vector: a fast and accurate algorithm*. Solar Energy, 70(5), 431–441. DOI: [10.1016/S0038-092X(00)00156-0](https://doi.org/10.1016/S0038-092X(00)00156-0).  
    *Aporte a Flamara:* Algoritmo astronómico ultrarrápido (PSA) implementable en microcontroladores para el cálculo de la elevación solar y comparación en series temporales.
13. **Semtech Corporation** (2020). *SX1276/77/78/79 - 137 MHz to 1020 MHz Low Power Long Range Transceiver Datasheet*. Rev. 7. Semtech Corporation, Camarillo, CA.  
    *Aporte a Flamara:* Fórmulas oficiales de tiempo al aire, sensibilidad del receptor y presupuesto de enlace de radio a 915 MHz.
14. **Espressif Systems** (2024). *ESP32-S3 Technical Reference Manual (v1.5)*. Espressif Systems, Shanghai, China.  
    *Aporte a Flamara:* Especificación del bus paralelo DVP del periférico `LCD_CAM` y registros de control de DMA y memoria PSRAM.
