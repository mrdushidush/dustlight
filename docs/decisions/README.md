# Architecture Decision Records

One file per decision: `NNNN-short-title.md` with **Status**, **Context**, **Decision**,
**Consequences**. A decision is never edited away — a later ADR supersedes it and both
stay. `PLAN.md` holds the reasoning and trade-off tables; ADRs hold the outcome.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-base-community-fork.md) | Base on the `fallout2-ce/fallout2-ce` community fork | Accepted |
| [0002](0002-gpu-api-d3d11.md) | Direct3D 11 for the HD renderer | Accepted |
| [0003](0003-4x-masters-with-tiers.md) | 4× asset masters with pre-filtered 3× and 2× tiers | Accepted |
| [0004](0004-baked-light-modulation.md) | Modulate baked art; "original-equivalent" invariant | Accepted |
| [0005](0005-python-pytorch-pipeline.md) | Plain Python + PyTorch asset pipeline with a content-addressed cache | Accepted |
| [0006](0006-repo-layout-and-safety-rails.md) | Single local fork repo, layout, game-data safety rails | Accepted |
| [0007](0007-name-and-license.md) | Working title kept private; new code under the Sustainable Use License | Accepted |
| [0008](0008-ui-tech.md) | Dear ImGui for the dev overlay; RmlUi for player UI | Accepted / Proposed |
| [0009](0009-voice-track.md) | Text-to-speech for unvoiced dialogue and holodisks | Accepted |
| [0010](0010-inventory-track.md) | Inventory UX overhaul as pure presentation | Accepted |
| [0011](0011-upscaler-model.md) | 4x-UltraSharpV2 upscaler for all categories | Accepted |
| [0012](0012-map-lighting.md) | Map lighting starts from CE's own light field | Accepted |
| [0013](0013-map-effects.md) | Map effects: palette cycling on HD art, emissives, post chain, HD fades | Accepted |
| [0014](0014-ui-hd-skin.md) | UI as an HD skin over CE's own screens (recorded blits, exact check per pixel) | Proposed |
| [0015](0015-release-upscaler.md) | The release art's upscaler: UltraSharpV2, with its author's permission (a permissive mix as the unused fallback) | Accepted |
