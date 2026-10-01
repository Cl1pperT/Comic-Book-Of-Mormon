# Hardware

The comic is written with Codex and drawn locally on one desktop PC. These are its specs and how the pipeline
performs on it, so render-time estimates can be read against the machine that produced them.

## The machine

| Part | Spec |
|---|---|
| CPU | AMD Ryzen 5 5600G (6 cores / 12 threads, 4.2 GHz) |
| GPU | NVIDIA GeForce RTX 3060, 12 GB VRAM, 170 W power limit (driver 616.56) |
| RAM | 32 GB DDR4-3200 (4 sticks) |
| Motherboard | ASUS PRIME B450M-A |
| Storage | Samsung 850 EVO 500 GB SSD (SATA); 1 TB 2.5" HDD (SATA) |
| OS | Windows 11 Pro 64-bit (10.0.26200) |

## Software

| Component | Version / setup |
|---|---|
| Pipeline | Python 3.12, this repository |
| Image generation | ComfyUI portable (PyTorch 2.13, CUDA 13.0) with ComfyUI-GGUF |
| Image model | FLUX.1 dev, Q8 GGUF; FLUX.1 Kontext dev Q8 (reference-portrait path, not used for drafts) |
| Scene writing | Codex CLI signed in with ChatGPT (gpt-6-luna writes and checks; gpt-6-sol and gpt-6-astra repair) |

## Measured performance

| Task | Time |
|---|---|
| One panel (Flux dev Q8, ~1 megapixel, 24 steps, cfg 2) | 195–215 s, about 8 s per step |
| First panel of a run (includes loading the model) | ~245 s |
| One reference portrait | ~3–4 min |
| One night (11 PM–7 AM) | ~100–120 panels, about 6–8 chapters |
| Writing one chapter with Codex (write + check + repairs) | ~2–5 min |

Notes:

- **VRAM:** the Q8 Flux model doesn't fit entirely in 12 GB; ComfyUI keeps about 7.5 GB on the GPU and offloads about
  4.8 GB to system RAM, which is why 32 GB of RAM matters. The Q5_K_S GGUF (about 8.3 GB) fits, but in testing it
  was no faster than Q8. Rendering each panel at about its size on the page (0.4–1.0 MP instead of 1 MP for every
  panel) was; see "Render size" in the README.
- **Heat:** under sustained rendering the GPU runs around 85 °C with the fan near 75% and throttles its clock
  slightly (about 1,780 of 2,100 MHz). Lowering the power limit to ~135 W would cool it by several degrees for a
  small speed cost.
- **Scale:** the whole book is about 3,500 panels, roughly 200 GPU-hours, or about 4–5 weeks of nightly runs.
