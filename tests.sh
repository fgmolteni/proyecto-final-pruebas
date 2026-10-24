#!/bin/sh
# Corre los --self-check de todos los módulos (sin GPU).
CUDA_VISIBLE_DEVICES= TF_CPP_MIN_LOG_LEVEL=3
export CUDA_VISIBLE_DEVICES TF_CPP_MIN_LOG_LEVEL
for m in modulos/*.py; do echo "== $m"; .venv/bin/python "$m" --self-check || exit 1; done
