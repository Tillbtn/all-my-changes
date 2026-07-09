/* Simple-3D-Buildings renderer: a MapLibre custom layer that models roof
 * shapes (gabled, hipped, skillion, pyramidal, dome, ...) with three.js.
 *
 * Geometry is built per building in local meters (anchored at the first
 * vertex, converted through MercatorCoordinate), merged into one mesh with
 * per-vertex colors, and drawn with the map's own WebGL context.
 *
 * Exposed as window.Roofs3D.createLayer(features, opts):
 *   opts.color  string or function(props) -> css color for walls
 *   Tags win over the style: building:colour / roof:colour (bc / rc).
 */
"use strict";

window.Roofs3D = (function () {
  const EPS = 1e-9;

  /* roof:shape values -> the builder that approximates them */
  const SHAPE_ALIAS = {
    flat: "flat",
    skillion: "skillion", lean_to: "skillion",
    gabled: "gabled", gambrel: "gabled", saltbox: "gabled",
    double_saltbox: "gabled", round: "gabled",
    hipped: "hipped", "half-hipped": "hipped", side_hipped: "hipped",
    "side_half-hipped": "hipped", mansard: "hipped",
    pyramidal: "pyramidal", cone: "pyramidal",
    dome: "dome", onion: "onion",
  };

  const DOME_PROFILE = [];   // [ringScale, heightFraction]
  for (let i = 0; i <= 6; i++) {
    const a = (i / 6) * Math.PI / 2;
    DOME_PROFILE.push([Math.cos(a), Math.sin(a)]);
  }
  const ONION_PROFILE = [
    [1, 0], [1.15, 0.25], [1.0, 0.45], [0.65, 0.65], [0.3, 0.82], [0.05, 0.95], [0, 1],
  ];

  /* ---------------------------------------------------------- 2D helpers */

  function ringArea(ring) {
    let a = 0;
    for (let i = 0, n = ring.length; i < n; i++) {
      const [x1, y1] = ring[i], [x2, y2] = ring[(i + 1) % n];
      a += x1 * y2 - x2 * y1;
    }
    return a / 2;
  }

  function dedupe(ring) {
    const out = [];
    for (const p of ring) {
      const q = out[out.length - 1];
      if (!q || Math.abs(q[0] - p[0]) > EPS || Math.abs(q[1] - p[1]) > EPS) out.push(p);
    }
    while (out.length > 1) {
      const a = out[0], b = out[out.length - 1];
      if (Math.abs(a[0] - b[0]) < EPS && Math.abs(a[1] - b[1]) < EPS) out.pop();
      else break;
    }
    return out;
  }

  function convexHull(pts) {
    const p = [...pts].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    const cross = (o, a, b) =>
      (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    const build = (list) => {
      const h = [];
      for (const pt of list) {
        while (h.length >= 2 && cross(h[h.length - 2], h[h.length - 1], pt) <= 0) h.pop();
        h.push(pt);
      }
      return h;
    };
    const lower = build(p), upper = build(p.reverse());
    return lower.slice(0, -1).concat(upper.slice(0, -1));
  }

  /* minimum-area oriented bounding box: center, long/short axis, half sizes */
  function orientedBBox(pts) {
    const hull = pts.length > 3 ? convexHull(pts) : pts;
    let best = null;
    for (let i = 0; i < hull.length; i++) {
      const a = hull[i], b = hull[(i + 1) % hull.length];
      let dx = b[0] - a[0], dy = b[1] - a[1];
      const len = Math.hypot(dx, dy);
      if (len < EPS) continue;
      dx /= len; dy /= len;
      let umin = 1e30, umax = -1e30, wmin = 1e30, wmax = -1e30;
      for (const p of hull) {
        const u = p[0] * dx + p[1] * dy;
        const w = -p[0] * dy + p[1] * dx;
        if (u < umin) umin = u; if (u > umax) umax = u;
        if (w < wmin) wmin = w; if (w > wmax) wmax = w;
      }
      const area = (umax - umin) * (wmax - wmin);
      if (!best || area < best.area) {
        best = { area, dx, dy, umin, umax, wmin, wmax };
      }
    }
    if (!best) return null;
    const { dx, dy, umin, umax, wmin, wmax } = best;
    const cu = (umin + umax) / 2, cw = (wmin + wmax) / 2;
    const c = [dx * cu - dy * cw, dy * cu + dx * cw];
    let u = [dx, dy], w = [-dy, dx];
    let halfL = (umax - umin) / 2, halfW = (wmax - wmin) / 2;
    if (halfW > halfL) {           // ensure u is the long axis
      [u, w] = [w, u];
      [halfL, halfW] = [halfW, halfL];
      w = [-u[1], u[0]];
    }
    return { c, u, w, halfL, halfW };
  }

  /* insert intersections of ring edges with the line sd(p)=0 */
  function insertLineCrossings(ring, sd) {
    const out = [];
    for (let i = 0; i < ring.length; i++) {
      const a = ring[i], b = ring[(i + 1) % ring.length];
      out.push(a);
      const da = sd(a), db = sd(b);
      if ((da > EPS && db < -EPS) || (da < -EPS && db > EPS)) {
        const t = da / (da - db);
        out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
      }
    }
    return out;
  }

  /* Sutherland-Hodgman: keep the part of the ring where sd(p) >= 0 */
  function clipHalfPlane(ring, sd) {
    let out = [];
    for (let i = 0; i < ring.length; i++) {
      const a = ring[i], b = ring[(i + 1) % ring.length];
      const da = sd(a), db = sd(b);
      if (da >= -EPS) out.push(a);
      if ((da > EPS && db < -EPS) || (da < -EPS && db > EPS)) {
        const t = da / (da - db);
        out.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
      }
    }
    return dedupe(out);
  }

  /* insert points lying on a boundary chord (both edge ends with sd~0) */
  function insertOnChord(ring, sd, along, points) {
    let out = ring;
    for (const p of points) {
      const ap = along(p);
      for (let i = 0; i < out.length; i++) {
        const a = out[i], b = out[(i + 1) % out.length];
        if (Math.abs(sd(a)) < 1e-6 && Math.abs(sd(b)) < 1e-6) {
          const aa = along(a), ab = along(b);
          if ((ap - aa) * (ap - ab) < -EPS) {
            out = out.slice(0, i + 1).concat([p], out.slice(i + 1));
            break;
          }
        }
      }
    }
    return out;
  }

  /* ---------------------------------------------------------- 3D helpers */

  /* vertices arrive in local meters relative to the feature anchor; the
   * push converts to mercator-unit offsets from the layer origin, so far
   * apart buildings keep their own latitude-correct meter scale */
  function makePush(positions, colors, aox, aoy, k) {
    return (ax, ay, az, bx, by, bz, cx, cy, cz, col) => {
      positions.push(
        aox + ax * k, aoy + ay * k, az * k,
        aox + bx * k, aoy + by * k, bz * k,
        aox + cx * k, aoy + cy * k, cz * k
      );
      colors.push(col.r, col.g, col.b, col.r, col.g, col.b, col.r, col.g, col.b);
    };
  }

  function triangulateLifted(outer, holes, zf, push, col) {
    const contour = outer.map((p) => new THREE.Vector2(p[0], p[1]));
    const holeVs = holes.map((h) => h.map((p) => new THREE.Vector2(p[0], p[1])));
    let tris;
    try {
      tris = THREE.ShapeUtils.triangulateShape(contour, holeVs);
    } catch {
      return;
    }
    const all = outer.concat(...holes);
    for (const [i, j, k] of tris) {
      const a = all[i], b = all[j], c = all[k];
      push(a[0], a[1], zf(a), b[0], b[1], zf(b), c[0], c[1], zf(c), col);
    }
  }

  function buildWalls(rings, base, topf, push, col) {
    for (const ring of rings) {
      for (let i = 0; i < ring.length; i++) {
        const a = ring[i], b = ring[(i + 1) % ring.length];
        const ta = topf(a), tb = topf(b);
        if (ta - base < 0.01 && tb - base < 0.01) continue;
        push(a[0], a[1], base, b[0], b[1], base, b[0], b[1], tb, col);
        push(a[0], a[1], base, b[0], b[1], tb, a[0], a[1], ta, col);
      }
    }
  }


  /* ------------------------------------------------------- roof builders */

  function centroidOf(ring) {
    let x = 0, y = 0;
    for (const p of ring) { x += p[0]; y += p[1]; }
    return [x / ring.length, y / ring.length];
  }

  /* Emits the roof surface and returns { topf, wallRings } so the caller
   * can raise walls from the base up to the roof edge (gables included). */
  function buildRoof(shape, rings, eave, roofH, props, push, roofCol) {
    const [outer, ...holes] = rings;

    if (shape === "skillion") {
      /* plane sloping down toward roof:direction (default: short OBB axis) */
      let dir;
      if (props.rd !== undefined) {
        const rad = (props.rd * Math.PI) / 180;
        dir = [Math.sin(rad), -Math.cos(rad)];   // local y points south
      } else {
        const box = orientedBBox(outer);
        dir = box ? box.w : [1, 0];
      }
      let lo = 1e30, hi = -1e30;
      for (const p of outer) {
        const d = p[0] * dir[0] + p[1] * dir[1];
        if (d < lo) lo = d; if (d > hi) hi = d;
      }
      const span = Math.max(hi - lo, EPS);
      const zf = (p) =>
        eave + roofH * ((hi - (p[0] * dir[0] + p[1] * dir[1])) / span);
      triangulateLifted(outer, holes, zf, push, roofCol);
      return { topf: zf, wallRings: rings };
    }

    if (shape === "gabled" || shape === "hipped") {
      const box = orientedBBox(outer);
      if (!box) return null;
      let { c, u, w, halfL, halfW } = box;
      if (props.ro === "across") {
        [u, w] = [w, u];
        [halfL, halfW] = [halfW, halfL];
      }
      const dc = (p) => (p[0] - c[0]) * w[0] + (p[1] - c[1]) * w[1];
      const dl = (p) => (p[0] - c[0]) * u[0] + (p[1] - c[1]) * u[1];
      const tent =
        shape === "gabled"
          ? (p) => 1 - Math.abs(dc(p)) / Math.max(halfW, EPS)
          : (p) =>
              Math.min(halfW - Math.abs(dc(p)), halfL - Math.abs(dl(p))) /
              Math.max(halfW, EPS);
      const zf = (p) => eave + roofH * Math.min(Math.max(tent(p), 0), 1);

      if (holes.length === 0) {
        /* split along the ridge so the crease is real geometry */
        let halves = [
          clipHalfPlane(outer, dc),
          clipHalfPlane(outer, (p) => -dc(p)),
        ];
        if (shape === "hipped" && halfL > halfW) {
          const ridge = halfL - halfW;
          const apexes = [
            [c[0] + u[0] * ridge, c[1] + u[1] * ridge],
            [c[0] - u[0] * ridge, c[1] - u[1] * ridge],
          ];
          halves = halves.map((h) => insertOnChord(h, dc, dl, apexes));
        }
        for (const half of halves) {
          if (half.length >= 3) triangulateLifted(half, [], zf, push, roofCol);
        }
      } else {
        triangulateLifted(outer, holes, zf, push, roofCol);
      }
      return { topf: zf, wallRings: rings.map((r) => insertLineCrossings(r, dc)) };
    }

    if (shape === "pyramidal" || shape === "dome" || shape === "onion") {
      const apex = centroidOf(outer);
      if (shape === "pyramidal") {
        for (let i = 0; i < outer.length; i++) {
          const a = outer[i], b = outer[(i + 1) % outer.length];
          push(a[0], a[1], eave, b[0], b[1], eave,
               apex[0], apex[1], eave + roofH, roofCol);
        }
      } else {
        const profile = shape === "dome" ? DOME_PROFILE : ONION_PROFILE;
        let prev = outer.map((p) => [p[0], p[1], eave]);
        for (let s = 1; s < profile.length; s++) {
          const [sc, zfrac] = profile[s];
          const cur = outer.map((p) => [
            apex[0] + (p[0] - apex[0]) * sc,
            apex[1] + (p[1] - apex[1]) * sc,
            eave + roofH * zfrac,
          ]);
          for (let i = 0; i < outer.length; i++) {
            const j = (i + 1) % outer.length;
            push(prev[i][0], prev[i][1], prev[i][2],
                 prev[j][0], prev[j][1], prev[j][2],
                 cur[j][0], cur[j][1], cur[j][2], roofCol);
            push(prev[i][0], prev[i][1], prev[i][2],
                 cur[j][0], cur[j][1], cur[j][2],
                 cur[i][0], cur[i][1], cur[i][2], roofCol);
          }
          prev = cur;
        }
      }
      const flat = () => eave;
      return { topf: flat, wallRings: rings };
    }

    /* flat (default) */
    const flat = () => eave;
    triangulateLifted(outer, holes, flat, push, roofCol);
    return { topf: flat, wallRings: rings };
  }

  /* ------------------------------------------------------ mesh assembly */

  function buildGeometry(features, opts) {
    const positions = [], colors = [];
    const colorFor =
      typeof opts.color === "function" ? opts.color : () => opts.color || "#ff3131";
    let origin = null;
    const cache = {};

    for (const feat of features) {
      const p = feat.properties || {};
      if (!p.b || p.op) continue;
      const g = feat.geometry;
      if (!g || (g.type !== "Polygon" && g.type !== "MultiPolygon")) continue;
      const polys = g.type === "Polygon" ? [g.coordinates] : g.coordinates;

      /* anchor + meter scale at this building's location */
      const first = polys[0][0][0];
      const anchor = maplibregl.MercatorCoordinate.fromLngLat({
        lng: first[0], lat: first[1],
      });
      const k = anchor.meterInMercatorCoordinateUnits();
      if (!origin) origin = anchor;
      const push = makePush(
        positions, colors, anchor.x - origin.x, anchor.y - origin.y, k
      );

      const shape = SHAPE_ALIAS[p.rs] || "flat";
      const base = p.mh || 0;

      /* rings in local meters relative to the anchor, y growing south */
      const rings = [];
      for (const poly of polys) {
        const converted = poly.map((ring) =>
          dedupe(ring.map((c) => {
            const m = maplibregl.MercatorCoordinate.fromLngLat({ lng: c[0], lat: c[1] });
            return [(m.x - anchor.x) / k, (m.y - anchor.y) / k];
          }))
        ).filter((r) => r.length >= 3);
        if (converted.length) rings.push(converted);
      }

      const wallCss = p.bc || colorFor(p);
      const wall = colorCached(cache, wallCss);
      const roof = colorCached(cache, p.rc || darken(wallCss, cache));

      for (const ringSet of rings) {
        const outer = ringSet[0];
        /* orient: outer counterclockwise in y-down space -> outward walls */
        if (ringArea(outer) < 0) outer.reverse();
        for (let i = 1; i < ringSet.length; i++) {
          if (ringArea(ringSet[i]) > 0) ringSet[i].reverse();
        }

        let roofH = p.rh;
        if (roofH === undefined) {
          if (p.rl !== undefined) roofH = p.rl * 2.5;
          else if (shape === "flat") roofH = 0;
          else {
            const box = orientedBBox(outer);
            roofH = Math.min(Math.max((box ? box.halfW : 3) * 0.75, 1.5), 6);
          }
        }
        let eave;
        if (p.h !== undefined) {
          eave = p.hl ? base + p.h : Math.max(base + 1, p.h - roofH);
        } else {
          eave = Math.max(base + 1, 8 - roofH);
        }

        try {
          const built = buildRoof(shape, ringSet, eave, roofH, p, push, roof);
          if (built) buildWalls(built.wallRings, base, built.topf, push, wall);
        } catch {
          /* degenerate footprint: skip the building rather than the layer */
        }
      }
    }
    return { positions, colors, origin };
  }

  function colorCached(cache, css) {
    if (!cache[css]) {
      try { cache[css] = new THREE.Color(css); }
      catch { cache[css] = new THREE.Color("#888888"); }
    }
    return cache[css];
  }

  function darken(css, cache) {
    const c = colorCached(cache, css).clone().multiplyScalar(0.72);
    return "#" + c.getHexString();
  }

  /* ------------------------------------------------------- custom layer */

  function createLayer(features, opts = {}) {
    return {
      id: opts.id || "amc-3d",
      type: "custom",
      renderingMode: "3d",

      onAdd(map, gl) {
        this.map = map;
        const { positions, colors, origin } = buildGeometry(features, opts);
        const geom = new THREE.BufferGeometry();
        geom.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
        geom.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
        geom.computeVertexNormals();
        const mat = new THREE.MeshLambertMaterial({
          vertexColors: true,
          side: THREE.DoubleSide,
        });
        this.scene = new THREE.Scene();
        this.scene.add(new THREE.Mesh(geom, mat));
        this.scene.add(new THREE.AmbientLight(0xffffff, 0.7));
        const sun = new THREE.DirectionalLight(0xffffff, 0.9);
        sun.position.set(0.4, -0.6, 1).normalize();
        this.scene.add(sun);
        const sun2 = new THREE.DirectionalLight(0xffffff, 0.35);
        sun2.position.set(-0.6, 0.5, 0.6).normalize();
        this.scene.add(sun2);
        this.camera = new THREE.Camera();
        this.origin = origin;
        this.renderer = new THREE.WebGLRenderer({
          canvas: map.getCanvas(),
          context: gl,
        });
        this.renderer.autoClear = false;
        this.geom = geom;
      },

      onRemove() {
        if (this.geom) this.geom.dispose();
      },

      render(gl, args) {
        if (!this.origin || this.map.getZoom() < 12) return;
        const matrix =
          args && args.defaultProjectionData
            ? args.defaultProjectionData.mainMatrix
            : args;
        const m = new THREE.Matrix4().fromArray(matrix);
        const model = new THREE.Matrix4().makeTranslation(
          this.origin.x, this.origin.y, 0
        );
        this.camera.projectionMatrix = m.multiply(model);
        this.renderer.resetState();
        this.renderer.render(this.scene, this.camera);
      },
    };
  }

  return { createLayer };
})();
