# Datasets públicos de incendio/humo para ampliar D-Fire

Investigación del 2026-09-27: 4 agentes agy (Gemini 3.8 Flash) con búsqueda web, más una verificación
propia de enlaces, licencias y tamaños. Objetivo: entrenar la red de 3 clases (normal / humo / incendio,
MobileNetV1 α=0.25, QVGA, int8) con otros datos para atacar sus puntos débiles: humo confundido con
nubes o niebla, sol y luces tomados como incendio, e incendios chicos o lejanos.

✔ = verificado (API, README o página oficial). ~ = lo reportaron los agentes y no se verificó.

## Recomendados

| Dataset | Qué aporta | Tamaño | Anotación | Licencia | Fuga con D-Fire |
|---|---|---|---|---|---|
| **Pyro-SDIS v2** (Pyronear, Francia) — `huggingface.co/datasets/pyronear/pyro-sdis` | humo real desde torre forestal + 1.634 falsos positivos reales del sistema | ✔ 12.378 imgs (10.744 con humo), 1,4 GB | ✔ cajas YOLO, sólo `smoke` | ✔ Apache-2.0 | nula |
| **sagecontinuum/smokedataset** (HPWREN) — HF | clasificación directa **cloud / other / smoke**: la confusión nube-humo tal cual | ✔ 19.832 parches (14.318/3.671/1.843), 119 MB | ✔ clase por imagen | ✔ MIT | nula (sí con FIgLib) |
| **HPWREN FIgLib** — `cdn.hpwren.ucsd.edu/HPWREN-FIgLib-Data/` | secuencias ±40 min alrededor de la ignición: el tramo previo es "normal" con nubes, niebla y sol en la misma escena | ~ ~25k imgs, ~24–30 GB, 2048×1536+ | etiqueta por tiempo (offset en el nombre) | ~ libre para investigación, citar HPWREN | nula |
| **MIVIA LFDN** (Salerno) — `mivia.unisa.it/large-fire-dataset-with-negative-samples-lfdn/` | llama, humo y **4.154 negativos difíciles** (sol, nubes, niebla, reflejos, faros, polvo, ropa y banderas rojas) | ✔ 36.554 imgs (8.070 llama, 15.330 humo, 9.000 ambos) | ✔ cajas flame/smoke | ~ sólo "citar"; revisar | media (videos viejos de MIVIA) |
| **FASDD_CV** (Wuhan) — `doi.org/10.57760/sciencedb.j00104.00103` | mucho volumen de incendio + humo + negativos, cámara terrestre | ~ 95.314 imgs, >20 GB | cajas YOLO/VOC/COCO | ~ CC BY 4.0 o CC BY-SA 4.0 (los agentes no coinciden) | **moderada**: deduplicar |
| **Nemo** (ALERTWildfire) — `github.com/SayBender/Nemo` | humo lejano con densidad low/mid/high, útil para medir sensibilidad | ~ ~2,9k imgs | cajas COCO | ✔ Apache-2.0 (repo) | nula |
| **SkyFinder** — `cs.valdosta.edu/~rpmihail/skyfinder/` | sólo "normal": 53 webcams fijas durante ~1 año, con toda la meteorología e iluminación | ✔ ~90k imgs | máscara cielo/suelo | ~ no indicada | nula |

## Útiles con cuidado o como banco de prueba

- **AI for Mankind** (`github.com/aiformankind/wildfire-smoke-dataset`): 744 imágenes con cajas de humo (v1) y
  más en v2, más un **dataset de nubes** aparte, todo en Google Drive. ✔ Licencia CC BY-NC-SA 4.0: permite
  uso académico, pero los derivados quedan bajo la misma licencia. Sale de HPWREN, así que se solapa con
  FIgLib y Sage.
- **DFS** (Wu et al. 2022, `github.com/siyuanwu/DFS-FIRE-SMOKE-Dataset`): ~ 9.462 imágenes con cajas fire,
  smoke y **other** (distractores); los nombres de archivo indican el tamaño del fuego. ✔ Se baja de
  Baidu u OneDrive. ✔ El repo **no declara licencia**.
- **FURG Fire** (`github.com/steffensbola/furg-fire-dataset`): 24 videos. ✔ CC0. Submuestrear y separar
  los splits por video.
- **SKLFS-WildFire test** (`github.com/WCUSTC/CCPE`): 50.735 imágenes de 3.649 clips, casi todas
  negativas. Sólo está publicado el test, en un servidor por IP con clave. Sirve para evaluar falsas
  alarmas, no para entrenar.
- **BoWFire** (226 imágenes) y **VisiFire** (videos QVGA): chicos. Sirven como prueba de estrés contra sol y
  luces, no para entrenar.
- **FASDD_UAV**: sólo las tomas oblicuas de dron que muestran horizonte (hay que filtrarlas a mano).

## Descartados

- **FLAME 1/2/3** (dron cenital): no tienen cielo ni horizonte. En FLAME 2 la etiqueta Fire se puso
  mirando también el canal IR, así que imágenes RGB que sólo muestran humo figuran como Fire. FLAME 3 es
  termografía radiométrica.
- **Satélite** (FASDD_RS, Sen2Fire, CEMS, FIRMS): escala y geometría incompatibles con la OV2640.
- **AIDER**: humo y fuego en una sola clase, escenas urbanas.
- **FiSmo** (✔ GPL-3.0) y **Kaggle phylake1337**: recopilaciones de internet con solapamiento alto con
  D-Fire.
- **Smoke100k**: humo sintético.
- **Corsican Fire DB**: exige registro y prohíbe redistribuir, lo que choca con que el proyecto sea
  público. Además casi no tiene humo solo ni negativos.
- **Agregadores**: `fireviewer/fire-smoke-detection-corpus-v1` (✔ = FASDD + Pyro-SDIS v1 re-empaquetados,
  licencia "other") y `Hajorda/flameye-wildfire-detection` (✔ 15.615 imágenes, **incluye D-Fire**: fuga
  segura). Es mejor ir a las fuentes. Roboflow Universe: muchos sets son D-Fire o BoWFire con aumentos
  ya aplicados.

## Errores de los agentes corregidos en la verificación

- Pyro-SDIS: los agentes dieron 33.636 imágenes y 3,3 GB, o 30.200 y 12 GB. Eso es la v1. La v2 actual
  tiene 12.378 imágenes y 1,4 GB, deja como mucho 10 frames por secuencia y **no tiene clase de llama**.
- Sage smokedataset: "41.000 imágenes, 2,8 GB" es la cifra del trabajo original con 5 clases. Lo
  publicado en HF son 19.832 parches con 3 clases, 119 MB.
- SkyFinder: el enlace Zenodo 3547748 que dio un agente es de otro dataset. La página correcta es la de
  Valdosta.
- "80 % menos falsos positivos con fotos locales" y el "10–15 % de solapamiento FASDD–D-Fire" son cifras
  sin fuente.

## Cómo usarlos con la red actual

1. **Mapeo por imagen**: si hay alguna caja fire → incendio; si sólo hay smoke → humo; si no hay cajas →
   normal. Pyro-SDIS, Nemo, Sage y FIgLib no tienen llama, así que la clase incendio sigue saliendo de
   D-Fire, LFDN y FASDD_CV.
2. **No reducir 2–3 MP directo a 320×240**: una columna de humo lejana baja a 2–3 px. Hay que recortar
   alrededor de la caja con contexto y luego escalar, o elegir escalas donde la caja quede de ≥ ~10 px en
   QVGA. Es el mismo problema que con los incendios chicos.
3. **Deduplicar** (dhash, igual que con los gemelos de D-Fire) cada fuente nueva contra el test de
   D-Fire, y separar los splits **por secuencia o cámara**, nunca por frame.
4. **Test propio del NEA**: juntar fotos de la OV2640 en el sitio real (sol de frente, niebla, tormenta)
   como clase normal. Es el único dato que cubre el dominio real.

Orden sugerido: Sage smokedataset (liviano, ataca nube↔humo) → Pyro-SDIS v2 → LFDN (negativos
difíciles + llama) → FASDD_CV deduplicado → FIgLib en recortes.
