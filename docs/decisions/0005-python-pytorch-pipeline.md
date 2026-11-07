# ADR-0005: Plain Python + PyTorch asset pipeline

**Status:** Accepted, 2026-10-31

## Context
The art pipeline must be fully automated (no manual art step), scriptable, and resumable
over tens of thousands of frames. chaiNNer is GUI-first.

## Decision
- Python with PyTorch (CUDA on the RTX 5060 Ti), spandrel for ESRGAN-family/OpenModelDB
  models, diffusers/transformers for normal and depth models. Environments managed with uv.
- One CLI (`fo2hd`) with idempotent stages: extract → decode → upscale → normals → height →
  emissive → pack.
- Content-addressed cache: key = hash(input bytes, stage params, model file hash, stage code
  version), stored under `<data_root>/cache/<stage>/<key>`. A SQLite job DB makes batches
  resumable.
- All inputs and outputs live under `data_root` (outside the repo).

## Consequences
- Re-running after a code or model change recomputes only what changed.
- Python 3.14 is installed; if a dependency lacks 3.14 or sm_120 wheels, the pipeline pins
  a Python 3.12 environment through uv.
