// Runs in the renderer's headless Chromium: draws Milkdrop from the Odroid's
// audio samples and sends each frame to renderer.py as a JPEG.
"use strict";

const canvas = document.getElementById("c");
const st = {
  ws: null, viz: null, presets: {}, names: [], idx: -1,
  run: false, fps: 30, quality: 0.8, w: 640, h: 400,
  cycle: 30, minFps: 0, delayMs: 200, loaded: 0, judgeFrom: Infinity, judgeFrames: 0,
  slow: new Set(), busy: false, frames: 0, fpsT: 0, shownFps: 0, last: 0,
};

// ── Audio from the Odroid: u32 rate + int16 mono, kept in a ring ───────────
const ring = new Int16Array(1 << 17);
let written = 0, rate = 48000, lastPush = 0;
function push(buf) {
  const dv = new DataView(buf);
  rate = dv.getUint32(0, true) || rate;
  const s = new Int16Array(buf, 4);
  const mask = ring.length - 1;
  for (let i = 0; i < s.length; i++) ring[(written + i) & mask] = s[i];
  written += s.length;
  lastPush = performance.now();
}
const timeBytes = new Uint8Array(1024);
function samples() {
  const now = performance.now();
  if (now - lastPush > 1000) return timeBytes.fill(128);
  // The Odroid sends samples ahead of what's heard (Bluetooth delay); hold back
  const behind = Math.max(0, ((lastPush + st.delayMs - now) * rate) / 1000);
  const end = Math.floor(written - behind), mask = ring.length - 1;
  for (let i = 0; i < timeBytes.length; i++) {
    const pos = end - timeBytes.length + i;
    timeBytes[i] = pos < 0 ? 128 : 128 + (ring[pos & mask] >> 8);
  }
  return timeBytes;
}

// ── Milkdrop ────────────────────────────────────────────────────────────────
function setup() {
  canvas.width = st.w; canvas.height = st.h;
  // What draws: the GPU or SwiftShader (shown in the app's log)
  const probe = document.createElement("canvas").getContext("webgl2");
  if (!probe) { send({ type: "nogl", error: "getContext('webgl2') gave nothing" }); return; }
  const dbg = probe.getExtension("WEBGL_debug_renderer_info");
  st.gl = dbg ? probe.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : "WebGL 2";
  const bc = window.butterchurn.default || window.butterchurn;
  const packs = window.butterchurnPresets.default || window.butterchurnPresets;
  st.presets = packs.getPresets();
  st.names = Object.keys(st.presets).sort(() => Math.random() - 0.5);
  st.viz = bc.createVisualizer(new AudioContext(), canvas, {
    width: st.w, height: st.h, pixelRatio: 1, textureRatio: 1,
  });
  next(0);
}
function next(blend = 2.5, step = 1) {
  const list = st.names.filter((n) => !st.slow.has(n));
  const pool = list.length ? list : st.names;
  const cur = pool.indexOf(st.names[st.idx]);
  choose(pool[((cur < 0 ? -1 : cur) + step + pool.length) % pool.length], blend, true);
}
function choose(name, blend = 0, judge = false) {
  if (!st.presets[name]) return;
  st.idx = st.names.indexOf(name);
  st.viz.loadPreset(st.presets[name], blend);
  st.loaded = performance.now();
  st.judgeFrom = judge && st.minFps > 0 ? st.loaded + blend * 1000 + 1500 : Infinity;
  st.judgeFrames = 0;
  status();
}
function status() {
  send({ type: "status", gl: st.gl || "", preset: st.names[st.idx] || "", fps: Math.round(st.shownFps * 10) / 10,
    presets: st.names.slice().sort((a, b) => a.localeCompare(b)), slow: [...st.slow] });
}

function frame(t) {
  requestAnimationFrame(frame);
  if (!st.run || !st.viz) return;
  if (t - st.last < 1000 / st.fps - 2) return;
  st.last = t;
  const a = samples();
  st.viz.render({ audioLevels: { timeByteArray: a, timeByteArrayL: a, timeByteArrayR: a } });
  // Snapshot now (the WebGL buffer is only valid in this task); skip a frame
  // if the last one is still being encoded or sent.
  if (!st.busy && st.ws?.readyState === 1 && st.ws.bufferedAmount < 2e6) {
    st.busy = true;
    canvas.toBlob((b) => {
      st.busy = false;
      if (b && st.ws?.readyState === 1) st.ws.send(b);
    }, "image/jpeg", st.quality);
  }
  st.frames++;
  if (t - st.fpsT >= 2000) {
    st.shownFps = (st.frames * 1000) / (t - st.fpsT);
    st.frames = 0; st.fpsT = t;
    status();
  }
  if (t > st.judgeFrom) {
    st.judgeFrames++;
    const secs = (t - st.judgeFrom) / 1000;
    if (secs >= 3) {
      st.judgeFrom = Infinity;
      if (st.judgeFrames / secs < st.minFps) { st.slow.add(st.names[st.idx]); next(0); return; }
    }
  }
  if (st.cycle > 0 && t - st.loaded > st.cycle * 1000) next();
}

// ── Link to renderer.py ─────────────────────────────────────────────────────
function send(m) { if (st.ws?.readyState === 1) st.ws.send(JSON.stringify(m)); }
function connect() {
  const ws = new WebSocket(`ws://${location.host}/page`);
  ws.binaryType = "arraybuffer";
  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) return push(e.data);
    const m = JSON.parse(e.data);
    switch (m.type) {
      case "config":
        st.w = m.width || st.w; st.h = m.height || st.h; st.fps = m.fps || st.fps;
        st.quality = (m.jpeg_quality || 80) / 100;
        if (!st.viz) setup(); else { canvas.width = st.w; canvas.height = st.h; st.viz.setRendererSize(st.w, st.h); }
        break;
      case "run": st.run = !!m.on; st.fpsT = performance.now(); st.frames = 0; break;
      case "settings":
        if (m.cycle != null) st.cycle = Number(m.cycle);
        if (m.minFps != null) st.minFps = Number(m.minFps);
        if (m.delayMs != null) st.delayMs = Number(m.delayMs);
        break;
      case "next": next(0, 1); break;
      case "prev": next(0, -1); break;
      case "preset": st.slow.delete(m.name); choose(m.name); break;
      case "forget_slow": st.slow.clear(); status(); break;
      case "title": try { if (m.title) st.viz?.launchSongTitleAnim(m.title); } catch { /* optional */ } break;
      case "status": status(); break;
    }
  };
  ws.onclose = () => setTimeout(connect, 2000);
  st.ws = ws;
}
connect();
requestAnimationFrame(frame);
