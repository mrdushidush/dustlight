# ADR-0002: Direct3D 11 for the HD renderer

**Status:** Accepted, 2026-10-31

## Context
The project is Windows-only (the author, 2026-10-31). CE presents through `SDL_Renderer` forced
to OpenGL. Options considered: D3D11, OpenGL 4.6, D3D12, Vulkan, SDL3 GPU, bgfx
(trade-off table in PLAN.md §3.2).

## Decision
The HD renderer uses Direct3D 11 with HLSL (Shader Model 5.0, compute for GI), created on
the SDL2 window's HWND. Shaders hot-reload in development builds. Textures ship as BC7
DDS (DirectXTex) instead of KTX2. The vanilla SDL path stays selectable with
`[hd] renderer=sdl|d3d11`.

## Consequences
- Best debugging (D3D debug layer, RenderDoc, PIX, Nsight) and the simplest modern API
  that runs the same on the RTX 5060 Ti and the RTX 3050.
- No SDL2→SDL3 migration, so no extra divergence from upstream.
- Linux/macOS would need another backend; accepted.
