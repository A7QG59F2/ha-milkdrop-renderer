// Runs in the renderer's headless Chromium: draws Milkdrop for every channel
// (one canvas and one butterchurn each) from that channel's audio, and sends
// each frame to renderer.py as a JPEG. On the link, text messages carry "ch"
// (the channel's index); binary messages start with one byte, the index.
"use strict";

const cfg = { w: 640, h: 400, fps: 30, quality: 0.8 };
let ws = null, gl = "", bc = null, presets = {}, presetNames = [];
const chans = [];

class Chan {
  constructor(i) {
    this.i = i;
    this.canvas = document.createElement("canvas");
    this.canvas.width = cfg.w; this.canvas.height = cfg.h;
    document.body.appendChild(this.canvas);
    Object.assign(this, {
      viz: null, names: presetNames.slice().sort(() => Math.random() - 0.5), idx: -1,
      run: false, cycle: 30, minFps: 0, delayMs: 200, loaded: 0, judgeFrom: Infinity, judgeFrames: 0,
      slow: new Set(), busy: false, frames: 0, fpsT: 0, shownFps: 0, last: 0,
      ring: new Int16Array(1 << 17), written: 0, rate: 48000, lastPush: 0, timeBytes: new Uint8Array(1024),
    });
    this.viz = bc.createVisualizer(new AudioContext(), this.canvas, {
      width: cfg.w, height: cfg.h, pixelRatio: 1, textureRatio: 1,
    });
    this.next(0);
  }

  // ── audio: u32 rate + int16 mono, kept in a ring ─────────────────────────
  push(buf) {
    const dv = new DataView(buf);
    this.rate = dv.getUint32(0, true) || this.rate;
    const s = new Int16Array(buf, 4), r = this.ring, mask = r.length - 1;
    for (let i = 0; i < s.length; i++) r[(this.written + i) & mask] = s[i];
    this.written += s.length;
    this.lastPush = performance.now();
  }
  samples() {
    const now = performance.now(), tb = this.timeBytes;
    if (now - this.lastPush > 1000) return tb.fill(128);
    // The device sends samples ahead of what's heard (Bluetooth delay); hold back
    const behind = Math.max(0, ((this.lastPush + this.delayMs - now) * this.rate) / 1000);
    const end = Math.floor(this.written - behind), mask = this.ring.length - 1;
    for (let i = 0; i < tb.length; i++) {
      const pos = end - tb.length + i;
      tb[i] = pos < 0 ? 128 : 128 + (this.ring[pos & mask] >> 8);
    }
    return tb;
  }

  // ── presets ──────────────────────────────────────────────────────────────
  next(blend = 2.5, step = 1) {
    const list = this.names.filter((n) => !this.slow.has(n));
    const pool = list.length ? list : this.names;
    const cur = pool.indexOf(this.names[this.idx]);
    this.choose(pool[((cur < 0 ? -1 : cur) + step + pool.length) % pool.length], blend, true);
  }
  choose(name, blend = 0, judge = false) {
    if (!presets[name]) return;
    this.idx = this.names.indexOf(name);
    this.viz.loadPreset(presets[name], blend);
    this.loaded = performance.now();
    this.judgeFrom = judge && this.minFps > 0 ? this.loaded + blend * 1000 + 1500 : Infinity;
    this.judgeFrames = 0;
    this.status();
  }
  status() {
    send({ type: "status", ch: this.i, gl, preset: this.names[this.idx] || "",
      fps: Math.round(this.shownFps * 10) / 10, presets: presetNames, slow: [...this.slow] });
  }
  resize() {
    this.canvas.width = cfg.w; this.canvas.height = cfg.h;
    this.viz.setRendererSize(cfg.w, cfg.h);
  }

  frame(t) {
    if (!this.run || t - this.last < 1000 / cfg.fps - 2) return;
    this.last = t;
    const a = this.samples();
    this.viz.render({ audioLevels: { timeByteArray: a, timeByteArrayL: a, timeByteArrayR: a } });
    // Snapshot now (the WebGL buffer is only valid in this task); skip a frame
    // if the last one is still being encoded or sent.
    if (!this.busy && ws?.readyState === 1 && ws.bufferedAmount < 2e6 * chans.length) {
      this.busy = true;
      this.canvas.toBlob(async (b) => {
        if (b && ws?.readyState === 1) {
          const jpeg = new Uint8Array(await b.arrayBuffer());
          const out = new Uint8Array(jpeg.length + 1);
          out[0] = this.i; out.set(jpeg, 1);
          ws.send(out);
        }
        this.busy = false;
      }, "image/jpeg", cfg.quality);
    }
    this.frames++;
    if (t - this.fpsT >= 2000) {
      this.shownFps = (this.frames * 1000) / (t - this.fpsT);
      this.frames = 0; this.fpsT = t;
      this.status();
    }
    if (t > this.judgeFrom) {
      this.judgeFrames++;
      const secs = (t - this.judgeFrom) / 1000;
      if (secs >= 3) {
        this.judgeFrom = Infinity;
        if (this.judgeFrames / secs < this.minFps) { this.slow.add(this.names[this.idx]); this.next(0); return; }
      }
    }
    if (this.cycle > 0 && t - this.loaded > this.cycle * 1000) this.next();
  }

  handle(m) {
    switch (m.type) {
      case "run": this.run = !!m.on; this.fpsT = performance.now(); this.frames = 0; break;
      case "settings":
        if (m.cycle != null) this.cycle = Number(m.cycle);
        if (m.minFps != null) this.minFps = Number(m.minFps);
        if (m.delayMs != null) this.delayMs = Number(m.delayMs);
        break;
      case "next": this.next(0, 1); break;
      case "prev": this.next(0, -1); break;
      case "preset": this.slow.delete(m.name); this.choose(m.name); break;
      case "forget_slow": this.slow.clear(); this.status(); break;
      case "title": try { if (m.title) this.viz.launchSongTitleAnim(m.title); } catch { /* optional */ } break;
      case "status": this.status(); break;
    }
  }
}

// ── Milkdrop: the engine once, then a channel per canvas ───────────────────
function setup(n) {
  if (!bc) {
    // What draws: the GPU or SwiftShader (shown in the app's log)
    const probe = document.createElement("canvas").getContext("webgl2");
    if (!probe) { send({ type: "nogl", error: "getContext('webgl2') gave nothing" }); return; }
    const dbg = probe.getExtension("WEBGL_debug_renderer_info");
    gl = dbg ? probe.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : "WebGL 2";
    bc = window.butterchurn.default || window.butterchurn;
    const packs = window.butterchurnPresets.default || window.butterchurnPresets;
    presets = packs.getPresets();
    presetNames = Object.keys(presets).sort((a, b) => a.localeCompare(b));
  }
  while (chans.length < n) chans.push(new Chan(chans.length));
  for (const c of chans) c.resize();
}

function frame(t) {
  requestAnimationFrame(frame);
  for (const c of chans) c.frame(t);
}

// ── Link to renderer.py ─────────────────────────────────────────────────────
function send(m) { if (ws?.readyState === 1) ws.send(JSON.stringify(m)); }
function connect() {
  const sock = new WebSocket(`ws://${location.host}/page`);
  sock.binaryType = "arraybuffer";
  sock.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) {
      const ch = chans[new Uint8Array(e.data, 0, 1)[0]];
      // copy past the channel byte, so the samples start on an even offset
      if (ch && e.data.byteLength > 5) ch.push(e.data.slice(1));
      return;
    }
    const m = JSON.parse(e.data);
    if (m.type === "config") {
      cfg.w = m.width || cfg.w; cfg.h = m.height || cfg.h; cfg.fps = m.fps || cfg.fps;
      cfg.quality = (m.jpeg_quality || 80) / 100;
      setup(Math.max(1, m.channels || 1));
      return;
    }
    chans[m.ch || 0]?.handle(m);
  };
  sock.onclose = () => setTimeout(connect, 2000);
  ws = sock;
}
connect();
requestAnimationFrame(frame);
