# Performance (task 8.8, Requirement 2.10)

Requirement 2.10: the office loads within 5 seconds and runs at 30 fps or more with up to 8 agents on a
typical laptop, and a low-quality mode exists.

**The 30 fps target is NOT verified on real hardware.** The only machine used so far has no GPU. What was
measured are the numbers that decide GPU and memory load (draw calls, triangles, textures, bytes). The
checklist at the end is how to measure real frame rates on a laptop.

## Quality modes

| | Normal | Low |
|---|---|---|
| Pixel ratio | device ratio, at most 2 | at most 1, times 0.75 |
| Antialiasing | on | off |
| Shadows | on (one 1024x1024 shadow map) | off, no shadow map is allocated |
| Character normal and roughness maps | on | off (they are never uploaded to the GPU) |
| Eyebrows and beard shells | shown | hidden |

Switch with the **Quality** button in the top bar (saved in the browser) or `?quality=low` in the URL. In
normal mode, when the rolling frame time stays above 40 ms for 3 seconds, a non-blocking bar offers
"Switch to low quality?". It never switches by itself. A hidden browser tab neither updates nor renders.

## Method

Page: `?mock=1&agents=8&perf=1` (mock office with 8 agents, probe on), served from `skeleton/office/static`
with `python3 -m http.server` on localhost. Headless Chromium with SwiftShader (software WebGL), window
1280x800, device pixel ratio 1, HTTP cache disabled. Each row is one run: 3 s warm-up, then
`window.__perf.snapshot()` after 6 s of rolling averages (`perf.js`).

`?perf=1` is the only way to turn the probe on. Without it `startPerfProbe()` returns before it hooks or
draws anything. The overlay (bottom right) shows frame time, draw calls, triangles, geometries, textures and
JS update time. `window.__perf.snapshot()` returns the same numbers and more as JSON: loaded bytes per
folder, unique materials and skinned meshes, estimated texture memory, shadow map and framebuffer size, load
marks.

Counts from `renderer.info` are per frame and include the shadow pass. Texture memory is an estimate: RGBA8
with a full mip chain, each shared image counted once.

## Results, 8 agents

"Before" is the committed scaffold (commit `e466cec`), "after" is the tuned tree.

| Metric | Normal before | Normal after | Low before | Low after |
|---|---|---|---|---|
| Draw calls per frame | 125 | **109** | 66 | 66 |
| Triangles per frame | 271,148 | **255,148** | 126,805 | 126,805 |
| Geometries | 22 | 22 | 18 | 18 |
| Unique materials | 61 | 61 | 53 | 53 |
| Textures on the GPU | 46 | 46 | 31 | 31 |
| Estimated texture memory | 66 MB | 66 MB | 44 MB | 44 MB |
| Shadow map | 4 MB | 4 MB | none | none |
| Framebuffer (color + depth) | 7.8 MB | 7.8 MB | 4.4 MB | 4.4 MB |
| Bytes loaded (all files) | 28.0 MB | 28.0 MB | 28.0 MB | 28.0 MB |
| Assets loaded (ms since navigation) | 4,074 | 3,789 | 4,705 | 4,961 |
| First frame rendered (ms) | 5,742 | 7,408 | 5,764 | 6,211 |
| JS update per frame (ms, see note) | 7.6 | 6.7 | 6.3 | 6.1 |
| Render call, CPU side (ms, SwiftShader) | 32.6 | 27.6 | 10.6 | 10.3 |
| JS heap | 80 MB | 61 MB | 93 MB | 91 MB |

Low mode against normal mode (after): 40% fewer draw calls, 50% fewer triangles, 33% less texture memory, no
shadow map, 44% fewer framebuffer pixels, and about 2.7 times less render time on the software rasterizer.

Notes:

- The load times vary by about 2 seconds between runs: repeated runs of the same page gave "assets loaded" from
  3.2 s to 7.5 s, and the machine's load average was about 7 on 2 cores. The differences between the
  before and after load times are noise. They say nothing about the change.
- "JS update" is the time from the first frame callback to the render call. A CPU profile of 10 s in normal
  mode showed the main thread 98.8% waiting (the software rasterizer runs in another process) and all page
  scripts together about 1%. The 6 to 7 ms average is inflated by CPU contention on this machine. JS is not the
  bottleneck here.
- Frame time and fps are not reported in the table: with SwiftShader a frame takes 0.5 s (low) to 2.2 s
  (normal), which says nothing about a GPU.

## What changed

- `characters.js`: eyes, eyebrows and the beard shell no longer cast shadows. They are too small to show a
  shadow and each cost a draw call in the shadow pass (16 fewer draw calls and 16,000 fewer triangles with 8
  agents in normal mode).
- `ui-overlay.js`: the label loop no longer builds strings and arrays every frame. It rebuilds the text only when the
  state, bubble, activity or name changed, writes the CSS transform only when a label moved by 0.05 px or more,
  and does not copy the tag list each frame.
- `office.js`: the frame loop walks an array instead of creating a Set iterator each frame (the array is
  rebuilt only when a callback is added or removed).
- `perf.js`: texture memory counts each shared image once (the first version counted 164 MB for what is
  66 MB), and the shadow map estimate uses depth only.

Already in the committed scaffold and kept: low mode drops the normal and roughness maps before the first
upload, shared geometry and instanced furniture, hidden-tab pause, the "Switch to low quality?" bar.

Tried and removed, because the numbers showed no gain: `<link rel="preload">` and `modulepreload` hints for
the manifest, the two 7.5 MB animation files and the three.js modules. Six interleaved runs with and without them
overlapped within the noise (4.3 to 7.5 s for assets loaded).

Not changed, because nothing justified it on this machine: the cloth pattern shader (a fragment cost that
SwiftShader cannot rate), the hijab shell (one small mesh per wearer), and the shadow map size in low mode
(shadows are off there).

## Limits of this machine

- No GPU. Chromium renders with SwiftShader on 2 CPU cores that were already busy. Frame time, fps, fill
  rate, texture upload time and shader compile time are not representative of a laptop.
- Load time on localhost is dominated by parsing 2.3 MB of three.js and decoding 28 MB of files on a busy CPU.
  The first-frame time includes shader compilation in software. Assets finished loading in 3 to 7.5 s here
  (median about 5 s) and the first frame came 1.5 to 2 s later. The 5 s load target is therefore **not
  verified**, even on this machine, and may need a real-laptop run.
- The estimated texture memory ignores driver padding and compression.

## Checklist: measure real fps on a laptop

1. Close other heavy programs. Plug in the laptop and use the normal power mode. Use a Chromium browser with
   hardware acceleration on (`chrome://gpu` should say "WebGL: Hardware accelerated").
2. Start the office and open `http://127.0.0.1:<port>/?mock=1&agents=8&perf=1` in a new tab. Keep the tab in
   front and the window at its normal size (a maximized window on a high-DPI screen is the worst case).
3. Wait for the first frame. Note how long the loading bar took (the target is 5 s). With cache off (DevTools,
   Network, "Disable cache") that is the cold case.
4. Wait 10 s. Read the overlay: `frame ... ms avg`. 33 ms or less means 30 fps or more. Also write down the
   draw calls and the `js ... ms update` line.
5. In the console run `JSON.stringify(__perf.snapshot())` and keep the output.
6. Click **Quality: Normal** to switch to low. Wait 10 s and read the overlay again.
7. Repeat with `&agents=4` and, on a screen with a device pixel ratio above 1, once more at the browser zoom
   you normally use.
8. Check that "Switch to low quality?" appears only if the frame time was above 40 ms for 3 s, and that
   it does not switch anything by itself.
9. If normal mode is below 30 fps on a typical laptop, the cheapest levers are the pixel ratio cap in
   `pixelRatioFor()` (`office.js`), the shadow map size (`sun.shadow.mapSize`), and the number of skinned
   meshes per character. Record the numbers in this file.

Result table to fill in on real hardware:

| Laptop (CPU, GPU, screen) | Mode | Agents | Load (s) | Frame ms avg | fps |
|---|---|---|---|---|---|
| not measured yet | normal | 8 | | | |
| not measured yet | low | 8 | | | |
