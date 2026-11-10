/* Editor de pista sobre <canvas>.
 *
 * Un mismo editor sirve para dos sistemas de coordenadas:
 *   - "plan":   milímetros sobre la plancha. Origen (0,0) = esquina superior izquierda,
 *               X a lo largo del largo, Y hacia abajo a lo ancho.
 *   - "camera": píxeles de la imagen nativa de la cámara (origen arriba a la izquierda).
 *
 * Capas: centerline, border_outer ({points:[[x,y]], closed}),
 *        inner_islands ([{name, points}]: islas interiores de la pista; un circuito en 8 tiene dos)
 *        y exclusion_zones ([{name, points}]). Las listas son siempre polígonos cerrados.
 * El editor modifica el objeto `layers` recibido (lo comparte con la página).
 */
(function (global) {
  "use strict";

  const LINE_LAYERS = ["centerline", "border_outer"];
  // capas que son LISTAS de polígonos: tipo de capa activa -> clave en `layers`
  const POLY = { island: "inner_islands", zone: "exclusion_zones" };
  const polyList = (L, kind) => (L[POLY[kind]] = L[POLY[kind]] || []);
  const HIT_PX = 9;

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function dist2(ax, ay, bx, by) { const dx = ax - bx, dy = ay - by; return dx * dx + dy * dy; }

  function projectOnSegment(px, py, ax, ay, bx, by) {
    const vx = bx - ax, vy = by - ay;
    const len2 = vx * vx + vy * vy;
    let t = len2 ? ((px - ax) * vx + (py - ay) * vy) / len2 : 0;
    t = Math.max(0, Math.min(1, t));
    return { x: ax + t * vx, y: ay + t * vy, t };
  }

  function polylineLength(points, closed) {
    let L = 0;
    for (let i = 1; i < points.length; i++) L += Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
    if (closed && points.length > 2) {
      const a = points[points.length - 1], b = points[0];
      L += Math.hypot(a[0] - b[0], a[1] - b[1]);
    }
    return L;
  }

  function polygonArea(points) {
    let A = 0;
    for (let i = 0, j = points.length - 1; i < points.length; j = i++) A += (points[j][0] + points[i][0]) * (points[j][1] - points[i][1]);
    return Math.abs(A / 2);
  }

  // ------------------------------------------------------------------ homografía (misma que admin/homography.py)
  const Homography = {
    _norm(pts) {
      const n = pts.length;
      const cx = pts.reduce((a, p) => a + p[0], 0) / n, cy = pts.reduce((a, p) => a + p[1], 0) / n;
      const d = pts.reduce((a, p) => a + Math.hypot(p[0] - cx, p[1] - cy), 0) / n;
      if (d < 1e-9) throw new Error("Todos los puntos coinciden");
      const s = Math.SQRT2 / d;
      return { pts: pts.map((p) => [(p[0] - cx) * s, (p[1] - cy) * s]), T: [[s, 0, -s * cx], [0, s, -s * cy], [0, 0, 1]] };
    },
    _solve(A, b) {
      const n = A.length, M = A.map((r, i) => [...r, b[i]]);
      for (let c = 0; c < n; c++) {
        let piv = c;
        for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[piv][c])) piv = r;
        if (Math.abs(M[piv][c]) < 1e-12) throw new Error("Puntos degenerados (¿alineados?)");
        [M[c], M[piv]] = [M[piv], M[c]];
        for (let r = c + 1; r < n; r++) { const f = M[r][c] / M[c][c]; for (let k = c; k <= n; k++) M[r][k] -= f * M[c][k]; }
      }
      const x = new Array(n).fill(0);
      for (let r = n - 1; r >= 0; r--) { let acc = M[r][n]; for (let k = r + 1; k < n; k++) acc -= M[r][k] * x[k]; x[r] = acc / M[r][r]; }
      return x;
    },
    mul(A, B) { return A.map((r, i) => [0, 1, 2].map((j) => r[0] * B[0][j] + r[1] * B[1][j] + r[2] * B[2][j])); },
    inv(H) {
      const [[a, b, c], [d, e, f], [g, h, i]] = H;
      const det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
      if (Math.abs(det) < 1e-15) throw new Error("No invertible");
      return [[(e * i - f * h) / det, -(b * i - c * h) / det, (b * f - c * e) / det],
              [-(d * i - f * g) / det, (a * i - c * g) / det, -(a * f - c * d) / det],
              [(d * h - e * g) / det, -(a * h - b * g) / det, (a * e - b * d) / det]];
    },
    apply(H, x, y) {
      const w = H[2][0] * x + H[2][1] * y + H[2][2];
      if (w <= 1e-9) return null; // detrás de la cámara o en el infinito
      return [(H[0][0] * x + H[0][1] * y + H[0][2]) / w, (H[1][0] * x + H[1][1] * y + H[1][2]) / w];
    },
    fit(src, dst) {
      if (src.length < 4) throw new Error(`Faltan ${4 - src.length} punto(s)`);
      const ns = this._norm(src), nd = this._norm(dst);
      const ata = Array.from({ length: 8 }, () => new Array(8).fill(0)), atb = new Array(8).fill(0);
      ns.pts.forEach(([x, y], k) => {
        const [u, v] = nd.pts[k];
        for (const [row, rhs] of [[[x, y, 1, 0, 0, 0, -u * x, -u * y], u], [[0, 0, 0, x, y, 1, -v * x, -v * y], v]]) {
          for (let i = 0; i < 8; i++) { atb[i] += row[i] * rhs; for (let j = 0; j < 8; j++) ata[i][j] += row[i] * row[j]; }
        }
      });
      const h = this._solve(ata, atb);
      const Hn = [[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1]];
      const H = this.mul(this.inv(nd.T), this.mul(Hn, ns.T));
      return H.map((r) => r.map((v) => v / H[2][2]));
    },
    unflatten(f) { return [f.slice(0, 3), f.slice(3, 6), f.slice(6, 9)]; },
  };

  class TrackEditor {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.opts = opts;
      this.view = null;          // {key, kind, width, height, gridMm, snap, image, layers}
      this.active = { layer: "centerline", zone: -1 };
      this.scale = 1; this.ox = 0; this.oy = 0;
      this.undoStacks = opts.undoStacks || new Map();
      this.mode = "draw";        // "draw" | "calib"
      this.drag = null;          // {target:[...], index}
      this.pan = null;
      this.spaceDown = false;
      this.hover = null;
      this.cursor = null;
      this.needsFit = true;

      const ro = new ResizeObserver(() => this._resize());
      ro.observe(canvas);
      canvas.addEventListener("pointerdown", (e) => this._onDown(e));
      canvas.addEventListener("pointermove", (e) => this._onMove(e));
      canvas.addEventListener("pointerup", (e) => this._onUp(e));
      canvas.addEventListener("pointercancel", (e) => this._onUp(e));
      canvas.addEventListener("pointerleave", () => { this.cursor = null; this._emitCursor(); this.render(); });
      canvas.addEventListener("contextmenu", (e) => this._onContext(e));
      canvas.addEventListener("wheel", (e) => this._onWheel(e), { passive: false });
      window.addEventListener("keydown", (e) => {
        if (e.code === "Space" && document.activeElement === canvas) { this.spaceDown = true; e.preventDefault(); }
      });
      window.addEventListener("keyup", (e) => { if (e.code === "Space") this.spaceDown = false; });
    }

    // ------------------------------------------------------------------ API
    setView(view) {
      const changedKey = !this.view || this.view.key !== view.key;
      this.view = view;
      if (!this.undoStacks.has(this._ukey())) this.undoStacks.set(this._ukey(), []);
      if (changedKey) { this.needsFit = true; this.drag = null; }
      this._resize();
    }

    updateViewProps(props) { Object.assign(this.view, props); this.render(); }

    setActive(layer, zone = -1) { this.active = { layer, zone }; this.render(); this._emitInfo(); }

    setMode(mode) { this.mode = mode; this.render(); }

    _ukey() { return this.view.undoKey || this.view.key; }

    // punto del mundo (mm del plano, o px si no hay transformación) -> pantalla
    _toS(p) {
      const xf = this.view.xf;
      const q = xf ? xf.fwd(p[0], p[1]) : p;
      return q ? this.toScreen(q[0], q[1]) : null;
    }
    // pantalla -> mundo (null si no se puede proyectar)
    _fromS(sx, sy) {
      const q = this.toWorld(sx, sy);
      return this.view.xf ? this.view.xf.inv(q[0], q[1]) : q;
    }

    fit() { this.needsFit = true; this._resize(); }

    undo() {
      const st = this.undoStacks.get(this._ukey());
      if (!st || !st.length) return false;
      const prev = JSON.parse(st.pop());
      const L = this.view.layers;
      for (const k of Object.keys(L)) delete L[k];
      Object.assign(L, prev);
      if (POLY[this.active.layer]) {
        const n = polyList(L, this.active.layer).length;
        if (this.active.zone >= n) this.active.zone = n - 1;
        if (this.active.zone < 0) this.active = { layer: "centerline", zone: -1 };
      }
      this._changed();
      return true;
    }

    pushUndo() {
      let st = this.undoStacks.get(this._ukey());
      if (!st) { st = []; this.undoStacks.set(this._ukey(), st); }
      st.push(JSON.stringify(this.view.layers));
      if (st.length > 100) st.shift();
    }

    clearActive() {
      const pts = this._activePoints();
      if (!pts || !pts.length) return;
      this.pushUndo();
      pts.length = 0;
      this._changed();
    }

    addPoly(kind, name) {
      this.pushUndo();
      const list = polyList(this.view.layers, kind);
      list.push({ name, points: [] });
      this.active = { layer: kind, zone: list.length - 1 };
      this._changed();
    }

    removePoly(kind, i) {
      this.pushUndo();
      const list = polyList(this.view.layers, kind);
      list.splice(i, 1);
      if (this.active.layer === kind) {
        this.active.zone = Math.min(this.active.zone, list.length - 1);
        if (this.active.zone < 0) this.active = { layer: "centerline", zone: -1 };
      }
      this._changed();
    }

    addZone(name) { this.addPoly("zone", name); }
    removeZone(i) { this.removePoly("zone", i); }

    static stats(layers, kind) {
      const unit = kind === "plan" ? "mm" : "px";
      const out = {};
      for (const k of LINE_LAYERS) {
        const l = layers[k];
        out[k] = { n: l.points.length, length: polylineLength(l.points, l.closed), unit };
      }
      out.zones = (layers.exclusion_zones || []).map((z) => ({ n: z.points.length, area: polygonArea(z.points), unit }));
      out.islands = (layers.inner_islands || []).map((z) => ({ n: z.points.length, area: polygonArea(z.points), unit }));
      return out;
    }

    // ------------------------------------------------------------------ geometría
    _resize() {
      const c = this.canvas;
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(1, Math.round(c.clientWidth * dpr));
      const h = Math.max(1, Math.round(c.clientHeight * dpr));
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
      if (this.needsFit && this.view && c.clientWidth > 0) { this._fitNow(); this.needsFit = false; }
      this.render();
    }

    _fitNow() {
      const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
      const pad = this.view.kind === "plan" ? 46 : 16;
      const s = Math.min((W - 2 * pad) / this.view.width, (H - 2 * pad) / this.view.height);
      this.scale = Math.max(s, 1e-4);
      this.ox = (W - this.view.width * this.scale) / 2;
      this.oy = (H - this.view.height * this.scale) / 2;
    }

    toScreen(x, y) { return [this.ox + x * this.scale, this.oy + y * this.scale]; }
    toWorld(sx, sy) { return [(sx - this.ox) / this.scale, (sy - this.oy) / this.scale]; }

    _eventPos(e) {
      const r = this.canvas.getBoundingClientRect();
      return [e.clientX - r.left, e.clientY - r.top];
    }

    _clampSnap(x, y) {
      const v = this.view;
      if ((v.kind === "plan" || v.xf) && v.snap && v.gridMm > 0) {
        x = Math.round(x / v.gridMm) * v.gridMm;
        y = Math.round(y / v.gridMm) * v.gridMm;
      }
      const b = v.worldBounds || { w: v.width, h: v.height };
      x = Math.max(0, Math.min(b.w, x));
      y = Math.max(0, Math.min(b.h, y));
      return [Math.round(x), Math.round(y)];
    }

    _activePoints() {
      const L = this.view && this.view.layers;
      if (!L) return null;
      if (POLY[this.active.layer]) {
        const z = polyList(L, this.active.layer)[this.active.zone];
        return z ? z.points : null;
      }
      return L[this.active.layer] ? L[this.active.layer].points : null;
    }

    _activeClosed() {
      if (POLY[this.active.layer]) return true;
      const l = this.view.layers[this.active.layer];
      return l ? l.closed : false;
    }

    _hitPoint(sx, sy) {
      const pts = this._activePoints();
      if (!pts) return -1;
      let best = -1, bestD = HIT_PX * HIT_PX;
      pts.forEach((p, i) => {
        const s = this._toS(p);
        if (!s) return;
        const d = dist2(s[0], s[1], sx, sy);
        if (d <= bestD) { bestD = d; best = i; }
      });
      return best;
    }

    _hitSegment(sx, sy) {
      const pts = this._activePoints();
      if (!pts || pts.length < 2) return null;
      const n = pts.length;
      const segs = this._activeClosed() && n > 2 ? n : n - 1;
      let best = null, bestD = (HIT_PX + 3) ** 2;
      for (let i = 0; i < segs; i++) {
        const a = this._toS(pts[i]), b = this._toS(pts[(i + 1) % n]);
        if (!a || !b) continue;
        const pr = projectOnSegment(sx, sy, a[0], a[1], b[0], b[1]);
        const d = dist2(pr.x, pr.y, sx, sy);
        if (d < bestD) { bestD = d; best = { index: i + 1, sx: pr.x, sy: pr.y }; }
      }
      return best;
    }

    // ------------------------------------------------------------------ eventos
    _onDown(e) {
      if (!this.view) return;
      this.canvas.focus();
      const [sx, sy] = this._eventPos(e);
      if (e.button === 1 || (e.button === 0 && this.spaceDown)) {
        this.pan = { sx, sy, ox: this.ox, oy: this.oy };
        this.canvas.classList.add("panning");
        this.canvas.setPointerCapture(e.pointerId);
        e.preventDefault();
        return;
      }
      if (e.button !== 0) return;
      if (this.view.static) return;
      if (this.mode === "calib") {
        if (this.opts.onCalibClick) this.opts.onCalibClick(this.view, this.toWorld(sx, sy));
        return;
      }
      if (this.view.readonly) { this._toast(this.view.readonlyMsg || "Capturá primero una imagen de referencia de esta cámara"); return; }
      const pts = this._activePoints();
      if (!pts) { this._toast("Elegí o creá una zona primero"); return; }
      const hit = this._hitPoint(sx, sy);
      if (hit >= 0) {
        this.pushUndo();
        this.drag = { index: hit, moved: false };
        this.canvas.setPointerCapture(e.pointerId);
        return;
      }
      if (e.shiftKey) {
        const seg = this._hitSegment(sx, sy);
        if (seg) {
          this.pushUndo();
          const wpt = this._fromS(seg.sx, seg.sy);
          if (!wpt) return;
          const [x, y] = this._clampSnap(...wpt);
          pts.splice(seg.index, 0, [x, y]);
          this.drag = { index: seg.index, moved: false };
          this.canvas.setPointerCapture(e.pointerId);
          this._changed();
          return;
        }
      }
      const wpt = this._fromS(sx, sy);
      const bnd = this.view.worldBounds || { w: this.view.width, h: this.view.height };
      if (!wpt || wpt[0] < -0.02 * bnd.w || wpt[1] < -0.02 * bnd.h || wpt[0] > bnd.w * 1.02 || wpt[1] > bnd.h * 1.02) {
        this._toast(this.view.xf ? "Ese punto cae fuera de la plancha" : "Fuera del área de dibujo");
        return;
      }
      const [wx, wy] = wpt;
      this.pushUndo();
      pts.push(this._clampSnap(wx, wy));
      this._changed();
    }

    _onMove(e) {
      if (!this.view) return;
      const [sx, sy] = this._eventPos(e);
      this.cursor = this.toWorld(sx, sy);
      if (this.pan) {
        this.ox = this.pan.ox + (sx - this.pan.sx);
        this.oy = this.pan.oy + (sy - this.pan.sy);
        this.render();
        return;
      }
      if (this.drag) {
        const pts = this._activePoints();
        const wpt = this._fromS(sx, sy);
        if (wpt) pts[this.drag.index] = this._clampSnap(...wpt);
        this.drag.moved = true;
        this._emitCursor();
        this.render();
        return;
      }
      const hov = this.mode === "calib" || this.view.static ? -1 : this._hitPoint(sx, sy);
      if (hov !== this.hover) { this.hover = hov; this.canvas.classList.toggle("over-point", hov >= 0); }
      this._emitCursor();
      this.render();
    }

    _onUp(e) {
      if (this.pan) { this.pan = null; this.canvas.classList.remove("panning"); }
      if (this.drag) {
        const moved = this.drag.moved;
        this.drag = null;
        if (moved) this._changed();
        else this.undoStacks.get(this._ukey()).pop(); // clic sin mover: no ensuciar el historial
      }
      try { this.canvas.releasePointerCapture(e.pointerId); } catch (_) { /* sin captura */ }
    }

    _onContext(e) {
      e.preventDefault();
      if (!this.view) return;
      const [sx, sy] = this._eventPos(e);
      const hit = this.view.readonly || this.view.static || this.mode === "calib" ? -1 : this._hitPoint(sx, sy);
      if (hit >= 0) {
        this.pushUndo();
        this._activePoints().splice(hit, 1);
        this.hover = -1;
        this._changed();
      }
    }

    _onWheel(e) {
      // Rueda del mouse o dos dedos del trackpad: desplaza (como un mapa). Ctrl/Cmd+rueda
      // (o el "pinch" del trackpad, que el navegador manda como wheel+ctrlKey): zoom centrado
      // en el cursor. Antes la rueda sola hacía zoom, que resultaba incómodo para moverse por
      // un plano grande sin perder de vista dónde se estaba.
      if (!this.view) return;
      e.preventDefault();
      if (e.ctrlKey || e.metaKey) {
        const [sx, sy] = this._eventPos(e);
        const [wx, wy] = this.toWorld(sx, sy);
        const f = Math.exp(-e.deltaY * 0.0015);
        this.scale = Math.min(Math.max(this.scale * f, 1e-4), 200);
        this.ox = sx - wx * this.scale;
        this.oy = sy - wy * this.scale;
      } else {
        this.ox -= e.deltaX;
        this.oy -= e.deltaY;
      }
      this.render();
    }

    _changed() { this.render(); this._emitInfo(); if (this.opts.onChange) this.opts.onChange(); }
    _emitCursor() {
      if (!this.opts.onCursor) return;
      if (!this.cursor) return this.opts.onCursor(null);
      const v = this.view;
      const uf = (v.unit && v.unit.factor) || 1, ul = (v.unit && v.unit.label) || "mm";
      const dec = uf === 1 ? 0 : 1;
      if (v.kind === "plan") {
        this.opts.onCursor(`x ${(this.cursor[0] * uf).toFixed(dec)} ${ul} · y ${(this.cursor[1] * uf).toFixed(dec)} ${ul}`);
        return;
      }
      let txt = `x ${this.cursor[0].toFixed(0)} px · y ${this.cursor[1].toFixed(0)} px`;
      if (v.xf) {
        const w = v.xf.inv(this.cursor[0], this.cursor[1]);
        if (w) txt += `  →  plano ${(w[0] * uf).toFixed(dec)}, ${(w[1] * uf).toFixed(dec)} ${ul}`;
      }
      this.opts.onCursor(txt);
    }
    _emitInfo() { if (this.opts.onInfo) this.opts.onInfo(); }
    _toast(msg) { if (this.opts.onMessage) this.opts.onMessage(msg); }

    // ------------------------------------------------------------------ dibujo
    render() {
      const c = this.canvas, ctx = this.ctx, v = this.view;
      const dpr = window.devicePixelRatio || 1;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const W = c.clientWidth, H = c.clientHeight;
      ctx.fillStyle = cssVar("--cv-outside", "#222");
      ctx.fillRect(0, 0, W, H);
      if (!v) return;

      const [x0, y0] = this.toScreen(0, 0);
      const [x1, y1] = this.toScreen(v.width, v.height);
      if (v.kind === "plan") this._drawBoard(ctx, x0, y0, x1, y1);
      else this._drawImage(ctx, x0, y0, x1, y1);

      const L = v.layers;
      const colors = {
        centerline: cssVar("--cv-center", "#ffb100"),
        border_outer: cssVar("--cv-outer", "#3bc9db"),
        island: cssVar("--cv-inner", "#b197fc"),
        zone: cssVar("--cv-zone", "#ff6b6b"),
      };
      // zonas a ignorar e islas interiores
      for (const kind of ["zone", "island"]) {
        (L[POLY[kind]] || []).forEach((z, i) => {
          const act = this.active.layer === kind && this.active.zone === i;
          this._drawPoly(ctx, z.points, true, colors[kind], act,
            kind === "zone" ? { fill: true, dash: [6, 4], width: 1.5 } : { fill: true, width: 2 });
          if (z.points.length) {
            const cx = z.points.reduce((s, p) => s + p[0], 0) / z.points.length;
            const cy = z.points.reduce((s, p) => s + p[1], 0) / z.points.length;
            const sp = this._toS([cx, cy]);
            if (sp) this._label(ctx, z.name, sp[0], sp[1], colors[kind], "center");
          }
        });
      }
      // borde exterior y línea central
      for (const k of ["border_outer", "centerline"]) {
        const l = L[k];
        const act = this.active.layer === k;
        this._drawPoly(ctx, l.points, l.closed, colors[k], act, { width: k === "centerline" ? 2.5 : 2, dash: k === "centerline" ? [10, 6] : null });
      }
      // inicio y sentido de carrera
      const cl = L.centerline.points;
      const a0 = cl.length >= 2 ? this._toS(cl[0]) : null, b0 = cl.length >= 2 ? this._toS(cl[1]) : null;
      if (a0 && b0) {
        const a = a0, b = b0;
        const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
        const ang = Math.atan2(b[1] - a[1], b[0] - a[0]);
        ctx.save();
        ctx.translate(mx, my); ctx.rotate(ang);
        ctx.fillStyle = colors.centerline;
        ctx.beginPath(); ctx.moveTo(9, 0); ctx.lineTo(-7, -7); ctx.lineTo(-7, 7); ctx.closePath(); ctx.fill();
        ctx.restore();
      }
      const s0 = cl.length ? this._toS(cl[0]) : null;
      if (s0) this._label(ctx, "INICIO", s0[0] + 10, s0[1] - 10, colors.centerline, "left");
      if (v.overlay) { ctx.save(); v.overlay(ctx, this); ctx.restore(); }
      if (v.banner) {
        ctx.save();
        ctx.font = "600 13px system-ui, sans-serif";
        const tw = ctx.measureText(v.banner).width;
        ctx.fillStyle = "rgba(0,0,0,.72)"; ctx.fillRect(10, 10, tw + 20, 28);
        ctx.fillStyle = "#ffd43b"; ctx.textBaseline = "middle"; ctx.fillText(v.banner, 20, 24);
        ctx.restore();
      }
    }

    _drawBoard(ctx, x0, y0, x1, y1) {
      const v = this.view;
      const uf = (v.unit && v.unit.factor) || 1, ul = (v.unit && v.unit.label) || "mm";
      ctx.fillStyle = cssVar("--cv-board", "#1e211f");
      ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
      const bg = v.bgImage;
      if (bg && bg.complete && bg.naturalWidth) {   // foto de una cámara enderezada sobre el plano
        ctx.save();
        ctx.globalAlpha = v.bgAlpha != null ? v.bgAlpha : 0.9;
        ctx.drawImage(bg, x0, y0, x1 - x0, y1 - y0);
        ctx.restore();
      }
      // grilla adaptativa: no dibujar líneas a menos de 7 px
      let step = v.gridMm > 0 ? v.gridMm : 50;
      while (step * this.scale < 7) step *= 2;
      const major = 500;
      ctx.lineWidth = 1;
      for (let x = 0; x <= v.width + 1e-6; x += step) {
        const sx = Math.round(this.toScreen(x, 0)[0]) + 0.5;
        ctx.strokeStyle = Math.abs(x % major) < 1e-6 ? cssVar("--cv-grid-major", "#555") : cssVar("--cv-grid", "#333");
        ctx.beginPath(); ctx.moveTo(sx, y0); ctx.lineTo(sx, y1); ctx.stroke();
      }
      for (let y = 0; y <= v.height + 1e-6; y += step) {
        const sy = Math.round(this.toScreen(0, y)[1]) + 0.5;
        ctx.strokeStyle = Math.abs(y % major) < 1e-6 ? cssVar("--cv-grid-major", "#555") : cssVar("--cv-grid", "#333");
        ctx.beginPath(); ctx.moveTo(x0, sy); ctx.lineTo(x1, sy); ctx.stroke();
      }
      ctx.strokeStyle = cssVar("--cv-board-edge", "#777");
      ctx.lineWidth = 2;
      ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
      // reglas (cada 500 mm)
      ctx.fillStyle = cssVar("--cv-label", "#aaa");
      ctx.font = "11px ui-monospace, Consolas, monospace";
      ctx.textBaseline = "bottom"; ctx.textAlign = "center";
      let labelStep = major;
      while (labelStep * this.scale < 45) labelStep += major;
      for (let x = 0; x <= v.width + 1e-6; x += labelStep) ctx.fillText(String(Math.round(x * uf)), this.toScreen(x, 0)[0], y0 - 5);
      ctx.textAlign = "right"; ctx.textBaseline = "middle";
      for (let y = 0; y <= v.height + 1e-6; y += labelStep) ctx.fillText(String(Math.round(y * uf)), x0 - 6, this.toScreen(0, y)[1]);
      ctx.textAlign = "left"; ctx.textBaseline = "top";
      ctx.fillText(`${Math.round(v.width * uf)} × ${Math.round(v.height * uf)} ${ul}`, x0, y1 + 6);
    }

    _drawImage(ctx, x0, y0, x1, y1) {
      const img = this.view.image;
      if (img && img.complete && img.naturalWidth) {
        ctx.imageSmoothingEnabled = this.scale < 2;
        ctx.drawImage(img, x0, y0, x1 - x0, y1 - y0);
      } else {
        ctx.fillStyle = cssVar("--cv-board", "#1e211f");
        ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
        ctx.fillStyle = cssVar("--cv-label", "#aaa");
        ctx.font = "14px system-ui, sans-serif";
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText("Sin imagen de referencia: usá «Capturar imagen actual»", (x0 + x1) / 2, (y0 + y1) / 2);
      }
      ctx.strokeStyle = cssVar("--cv-board-edge", "#777");
      ctx.lineWidth = 1;
      ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
    }

    _drawPoly(ctx, pts, closed, color, active, { fill = false, dash = null, width = 2 } = {}) {
      if (!pts.length) return;
      ctx.save();
      ctx.globalAlpha = active ? 1 : 0.55;
      ctx.lineJoin = "round"; ctx.lineCap = "round";
      ctx.beginPath();
      const sp = pts.map((p) => this._toS(p));
      const allOk = sp.every(Boolean);
      let pen = false;
      sp.forEach((q) => { if (!q) { pen = false; return; } if (pen) ctx.lineTo(q[0], q[1]); else { ctx.moveTo(q[0], q[1]); pen = true; } });
      if (closed && pts.length > 2 && allOk) ctx.closePath();
      if (fill && pts.length > 2 && allOk) { ctx.fillStyle = color; ctx.globalAlpha *= 0.18; ctx.fill(); ctx.globalAlpha = active ? 1 : 0.55; }
      ctx.strokeStyle = "rgba(0,0,0,.55)"; ctx.lineWidth = width + 2.5; ctx.setLineDash(dash || []); ctx.stroke();
      ctx.strokeStyle = color; ctx.lineWidth = width; ctx.stroke();
      ctx.setLineDash([]);
      // vértices
      sp.forEach((q, i) => {
        if (!q) return;
        const [sx, sy] = q;
        const r = active ? (i === this.hover ? 6 : 4) : 2.5;
        ctx.fillStyle = i === 0 && active ? "#fff" : color;
        ctx.strokeStyle = "rgba(0,0,0,.7)"; ctx.lineWidth = 1.5;
        ctx.beginPath();
        if (active) ctx.rect(sx - r, sy - r, 2 * r, 2 * r); else ctx.arc(sx, sy, r, 0, Math.PI * 2);
        ctx.fill(); ctx.stroke();
      });
      ctx.restore();
    }

    _label(ctx, text, x, y, color, align) {
      ctx.save();
      ctx.font = "600 11px system-ui, sans-serif";
      ctx.textAlign = align; ctx.textBaseline = "middle";
      const w = ctx.measureText(text).width;
      const bx = align === "center" ? x - w / 2 - 4 : x - 4;
      ctx.fillStyle = "rgba(0,0,0,.65)";
      ctx.fillRect(bx, y - 9, w + 8, 18);
      ctx.fillStyle = color;
      ctx.fillText(text, x, y);
      ctx.restore();
    }
  }

  TrackEditor.LINE_LAYERS = LINE_LAYERS;
  TrackEditor.POLY = POLY;
  TrackEditor.Homography = Homography;
  global.TrackEditor = TrackEditor;
})(window);
