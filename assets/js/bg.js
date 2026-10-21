(function () {
  /* ---- single live instance; re-injection (Streamlit remount / hot reload) tears the old one down first ---- */
  var prev = window.__scmBG;
  if (prev && prev.alive && prev.host && document.body.contains(prev.host)) { return; }
  if (prev && typeof prev.dispose === 'function') { try { prev.dispose(); } catch (e) {} }
  var inst = { alive: true, host: null, dispose: function () {} };
  window.__scmBG = inst;
  window.__scmBGInfo = { fps: 0, particles: 0, level: 0, disposed: false };
  var THREE_URL = 'https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js';

  function start() {
    if (!inst.alive) { return; }
    var THREE = window.THREE;
    if (!THREE) { return; }
    var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var host = document.createElement('div');
    host.id = 'scm-bg';
    host.style.cssText = 'position:fixed;left:0;top:0;width:100vw;height:100vh;z-index:0;pointer-events:none;';
    document.body.insertBefore(host, document.body.firstChild);
    inst.host = host;

    var renderer;
    try {
      renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, powerPreference: 'high-performance' });
    } catch (e) { host.remove(); inst.alive = false; return; }
    var pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
    renderer.setPixelRatio(pixelRatio);
    renderer.setClearColor(0x000000, 0);
    host.appendChild(renderer.domElement);

    var scene = new THREE.Scene();
    var camera = new THREE.PerspectiveCamera(55, 1, 0.1, 100);
    camera.position.z = 14;
    var DIST = 14, halfH = 1, halfW = 1, W = 1, H = 1;

    function sprite(inner) {
      var c = document.createElement('canvas');
      c.width = c.height = 64;
      var g = c.getContext('2d');
      var r = g.createRadialGradient(32, 32, 0, 32, 32, 32);
      r.addColorStop(0, 'rgba(255,255,255,1)');
      r.addColorStop(inner, 'rgba(255,255,255,0.65)');
      r.addColorStop(1, 'rgba(255,255,255,0)');
      g.fillStyle = r;
      g.fillRect(0, 0, 64, 64);
      return new THREE.CanvasTexture(c);
    }
    var dotTex = sprite(0.22);
    var haloTex = sprite(0.05);

    /* ---------- particle field (full-size buffers; activeN is what is simulated / drawn) ---------- */
    var NMAX = 300, N = 240, activeN = 240, MAXSEG = 900;
    var base = new Float32Array(NMAX * 3), vel = new Float32Array(NMAX * 3), off = new Float32Array(NMAX * 3);
    var pos = new Float32Array(NMAX * 3), col = new Float32Array(NMAX * 3), tint = new Float32Array(NMAX * 3);
    var PAL = [[0.78, 0.82, 0.9], [0.86, 0.89, 0.94], [0.25, 0.41, 0.88], [0.13, 0.27, 0.8], [0.34, 0.5, 0.95], [0.0, 0.95, 1.0]];
    var level = 0;   /* FPS governor: 0 = full, 1 = 50% particles, 2 = 25% */
    function seed() {
      N = Math.max(200, Math.min(300, Math.round(W * H / 6500)));
      for (var i = 0; i < N; i++) {
        base[i * 3] = (Math.random() * 2 - 1) * halfW * 1.15;
        base[i * 3 + 1] = (Math.random() * 2 - 1) * halfH * 1.15;
        base[i * 3 + 2] = -6 + Math.random() * 9;
        vel[i * 3] = (Math.random() - 0.5) * 0.006;
        vel[i * 3 + 1] = (Math.random() - 0.5) * 0.006;
        vel[i * 3 + 2] = 0;
        var pick = Math.random();
        var p = pick < 0.34 ? PAL[0] : pick < 0.5 ? PAL[1] : pick < 0.72 ? PAL[2] : pick < 0.88 ? PAL[3] : pick < 0.93 ? PAL[5] : PAL[4];
        tint[i * 3] = p[0]; tint[i * 3 + 1] = p[1]; tint[i * 3 + 2] = p[2];
        off[i * 3] = off[i * 3 + 1] = off[i * 3 + 2] = 0;
      }
      activeN = Math.max(60, Math.round(N * Math.pow(0.5, level)));
      window.__scmBGInfo.particles = activeN;
    }

    var group = new THREE.Group();
    scene.add(group);
    var pGeo = new THREE.BufferGeometry();
    pGeo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    pGeo.setAttribute('color', new THREE.BufferAttribute(col, 3));
    var pMat = new THREE.PointsMaterial({ size: 0.5, map: dotTex, vertexColors: true, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: 0.95, sizeAttenuation: true });
    var points = new THREE.Points(pGeo, pMat);
    points.frustumCulled = false;
    group.add(points);

    var lPos = new Float32Array(MAXSEG * 6), lCol = new Float32Array(MAXSEG * 6);
    var lGeo = new THREE.BufferGeometry();
    lGeo.setAttribute('position', new THREE.BufferAttribute(lPos, 3));
    lGeo.setAttribute('color', new THREE.BufferAttribute(lCol, 3));
    lGeo.setDrawRange(0, 0);
    var lMat = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.6, depthWrite: false, blending: THREE.AdditiveBlending });
    var lines = new THREE.LineSegments(lGeo, lMat);
    lines.frustumCulled = false;
    group.add(lines);

    var haloMat = new THREE.SpriteMaterial({ map: haloTex, color: 0x00f2fe, transparent: true, opacity: 0.0, depthWrite: false, blending: THREE.AdditiveBlending });
    var halo = new THREE.Sprite(haloMat);
    halo.scale.set(5.5, 5.5, 1);
    scene.add(halo);

    function resize() {
      W = window.innerWidth; H = window.innerHeight;
      renderer.setSize(W, H);
      camera.aspect = W / H;
      camera.updateProjectionMatrix();
      halfH = Math.tan(55 * Math.PI / 360) * DIST;
      halfW = halfH * W / H;
      seed();
      pGeo.setDrawRange(0, activeN);
    }
    resize();

    /* ---------- mouse tracking (named handlers so dispose() can remove them) ---------- */
    var tx = -9999, ty = -9999, mx = 0, my = 0, active = 0, nx = 0, ny = 0, ripples = [];
    function onMove(e) {
      tx = (e.clientX / W - 0.5) * 2 * halfW;
      ty = -(e.clientY / H - 0.5) * 2 * halfH;
      nx = e.clientX / W - 0.5;
      ny = e.clientY / H - 0.5;
      active = 1;
    }
    function onLeave() { active = 0; }
    function onDown(e) {
      ripples.push({ x: (e.clientX / W - 0.5) * 2 * halfW, y: -(e.clientY / H - 0.5) * 2 * halfH, t: 0 });
      if (ripples.length > 6) { ripples.shift(); }
    }
    var resizeTimer = null;
    function onResize() { clearTimeout(resizeTimer); resizeTimer = setTimeout(resize, 120); }
    function onHide() { inst.dispose(); }
    window.addEventListener('pointermove', onMove, { passive: true });
    window.addEventListener('pointerdown', onDown, { passive: true });
    window.addEventListener('resize', onResize);
    window.addEventListener('pagehide', onHide);
    document.addEventListener('mouseleave', onLeave);

    /* ---------- WebGL context loss: pause instead of freezing the tab, resume when restored ---------- */
    var lost = false;
    function onLost(e) { e.preventDefault(); lost = true; }
    function onRestored() { lost = false; dotTex.needsUpdate = true; haloTex.needsUpdate = true; }
    renderer.domElement.addEventListener('webglcontextlost', onLost, false);
    renderer.domElement.addEventListener('webglcontextrestored', onRestored, false);

    var isDark = null, last = performance.now(), time = 0, hx = 0, hy = 0, raf = 0;
    function applyTheme(dark) {
      isDark = dark;
      var b = dark ? THREE.AdditiveBlending : THREE.NormalBlending;
      pMat.blending = b; lMat.blending = b; haloMat.blending = b;
      pMat.opacity = dark ? 0.95 : 0.8;
      lMat.opacity = dark ? 0.6 : 0.5;
      pMat.size = dark ? 0.5 : 0.42;
      haloMat.color.setHex(dark ? 0x00f2fe : 0x2563eb);
      pMat.needsUpdate = true; lMat.needsUpdate = true; haloMat.needsUpdate = true;
    }

    /* ---------- FPS governor: < 30 fps for ~2s => halve the particle count (twice at most) ---------- */
    var winStart = 0, frames = 0, slow = 0, lastDegrade = 0;
    function govern(dt, now) {
      if (dt > 2) { winStart = now; frames = 0; return; }   /* hidden tab / debugger pause: restart the window, do not penalise */
      if (!winStart) { winStart = now; }
      frames++;
      var span = now - winStart;
      if (span < 1000) { return; }
      var fps = frames * 1000 / span;     /* wall-clock based: even 1-2 fps is measured correctly */
      window.__scmBGInfo.fps = Math.round(fps * 10) / 10;
      winStart = now; frames = 0;
      if (fps < 30) { slow++; } else { slow = 0; }
      window.__scmBGInfo.slow = slow; window.__scmBGInfo.since = Math.round(now - lastDegrade);
      if (slow >= 2 && level < 2 && now - lastDegrade > 3000) {
        level++; slow = 0; lastDegrade = now;
        activeN = Math.max(60, Math.round(N * Math.pow(0.5, level)));
        pGeo.setDrawRange(0, activeN);
        if (pixelRatio > 1) { pixelRatio = 1; renderer.setPixelRatio(1); renderer.setSize(W, H); }
        window.__scmBGInfo.level = level; window.__scmBGInfo.particles = activeN;
        if (window.console && console.info) { console.info('[scm-bg] low FPS (' + Math.round(fps) + '): particles reduced to ' + activeN); }
      }
    }

    function frame(now) {
      raf = requestAnimationFrame(frame);
      if (document.hidden || lost) { last = now; return; }
      var dt = Math.min(0.05, (now - last) / 1000);
      govern((now - last) / 1000, now);
      last = now; time += dt;
      var dark = document.documentElement.getAttribute('data-theme') !== 'light';
      if (dark !== isDark) { applyTheme(dark); }

      mx += (tx - mx) * 0.22; my += (ty - my) * 0.22;
      var R = 4.6, R2 = R * R;
      var i, d2, dx, dy, d, f, ux, uy, w, tgx, tgy, tgz, k;
      var scrollEl = document.querySelector('[data-testid="stMain"]');
      var sy = scrollEl ? scrollEl.scrollTop : 0;
      group.position.y = Math.sin(sy * 0.0007) * 1.4 + sy * 0.0003;

      for (i = 0; i < activeN; i++) {
        var ix = i * 3;
        if (!reduced) {
          base[ix] += vel[ix] * dt * 60; base[ix + 1] += vel[ix + 1] * dt * 60;
          if (base[ix] > halfW * 1.2) { base[ix] = -halfW * 1.2; }
          if (base[ix] < -halfW * 1.2) { base[ix] = halfW * 1.2; }
          if (base[ix + 1] > halfH * 1.2) { base[ix + 1] = -halfH * 1.2; }
          if (base[ix + 1] < -halfH * 1.2) { base[ix + 1] = halfH * 1.2; }
        }
        tgx = 0; tgy = 0; tgz = 0;
        if (active) {
          dx = base[ix] - mx; dy = base[ix + 1] - my + group.position.y;
          d2 = dx * dx + dy * dy;
          if (d2 < R2) {
            d = Math.sqrt(d2) + 0.0001; f = 1 - d / R; f = f * f;
            ux = dx / d; uy = dy / d;
            w = Math.sin(time * 3.2 - d * 2.4);
            tgx = -ux * f * 1.7 + (-uy) * f * 0.7 * w;
            tgy = -uy * f * 1.7 + (ux) * f * 0.7 * w;
            tgz = f * 2.6;
          }
        }
        for (k = 0; k < ripples.length; k++) {
          var rp = ripples[k];
          dx = base[ix] - rp.x; dy = base[ix + 1] - rp.y + group.position.y;
          d = Math.sqrt(dx * dx + dy * dy) + 0.0001;
          var ring = rp.t * 9.0, band = Math.abs(d - ring);
          if (band < 1.6) {
            var amp = (1 - band / 1.6) * Math.max(0, 1 - rp.t / 1.4) * 2.2;
            tgx += dx / d * amp; tgy += dy / d * amp; tgz += amp * 0.8;
          }
        }
        off[ix] += (tgx - off[ix]) * 0.09;
        off[ix + 1] += (tgy - off[ix + 1]) * 0.09;
        off[ix + 2] += (tgz - off[ix + 2]) * 0.09;
        pos[ix] = base[ix] + off[ix];
        pos[ix + 1] = base[ix + 1] + off[ix + 1];
        pos[ix + 2] = base[ix + 2] + off[ix + 2] + Math.sin(time * 0.6 + i) * 0.15;
        var glow = 1 + Math.min(1.2, Math.abs(off[ix + 2]) * 0.55);
        if (dark) {
          col[ix] = Math.min(1, tint[ix] * glow); col[ix + 1] = Math.min(1, tint[ix + 1] * glow); col[ix + 2] = Math.min(1, tint[ix + 2] * glow);
        } else {
          col[ix] = tint[ix] * 0.55; col[ix + 1] = tint[ix + 1] * 0.62; col[ix + 2] = Math.min(1, tint[ix + 2] * 0.85);
        }
      }
      for (k = ripples.length - 1; k >= 0; k--) {
        ripples[k].t += dt;
        if (ripples[k].t > 1.6) { ripples.splice(k, 1); }
      }
      pGeo.attributes.position.needsUpdate = true;
      pGeo.attributes.color.needsUpdate = true;

      var seg = 0, LR = 3.1, LR2 = LR * LR, j, maxSeg = level ? Math.round(MAXSEG * 0.5) : MAXSEG;
      for (i = 0; i < activeN && seg < maxSeg; i++) {
        for (j = i + 1; j < activeN && seg < maxSeg; j++) {
          dx = pos[i * 3] - pos[j * 3]; dy = pos[i * 3 + 1] - pos[j * 3 + 1];
          if (dx > LR || dx < -LR || dy > LR || dy < -LR) { continue; }
          var dz = pos[i * 3 + 2] - pos[j * 3 + 2];
          d2 = dx * dx + dy * dy + dz * dz;
          if (d2 < LR2) {
            var a = 1 - Math.sqrt(d2) / LR;
            var o = seg * 6;
            lPos[o] = pos[i * 3]; lPos[o + 1] = pos[i * 3 + 1]; lPos[o + 2] = pos[i * 3 + 2];
            lPos[o + 3] = pos[j * 3]; lPos[o + 4] = pos[j * 3 + 1]; lPos[o + 5] = pos[j * 3 + 2];
            var s1, s2, s3;
            if (dark) { s1 = 0.22 * a; s2 = 0.42 * a; s3 = 0.95 * a; }
            else { s1 = 1 - 0.8 * a; s2 = 1 - 0.62 * a; s3 = 1 - 0.1 * a; }
            lCol[o] = s1; lCol[o + 1] = s2; lCol[o + 2] = s3;
            lCol[o + 3] = s1; lCol[o + 4] = s2; lCol[o + 5] = s3;
            seg++;
          }
        }
      }
      lGeo.setDrawRange(0, seg * 2);
      lGeo.attributes.position.needsUpdate = true;
      lGeo.attributes.color.needsUpdate = true;

      hx += (mx - hx) * 0.3; hy += (my - hy) * 0.3;
      halo.position.set(hx, hy, 1);
      haloMat.opacity += ((active ? (dark ? 0.35 : 0.18) : 0) - haloMat.opacity) * 0.1;

      camera.position.x += (nx * 1.6 - camera.position.x) * 0.04;
      camera.position.y += (-ny * 1.0 - camera.position.y) * 0.04;
      camera.lookAt(0, 0, 0);
      renderer.render(scene, camera);
    }
    raf = requestAnimationFrame(frame);

    /* ---------- watchdog: if Streamlit removed our host node, release everything ---------- */
    var watch = setInterval(function () { if (!document.body.contains(host)) { inst.dispose(); } }, 3000);

    /* ---------- full teardown: listeners, rAF, GPU buffers, textures, materials, context ---------- */
    inst.dispose = function () {
      if (!inst.alive) { return; }
      inst.alive = false;
      cancelAnimationFrame(raf);
      clearInterval(watch);
      clearTimeout(resizeTimer);
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerdown', onDown);
      window.removeEventListener('resize', onResize);
      window.removeEventListener('pagehide', onHide);
      document.removeEventListener('mouseleave', onLeave);
      renderer.domElement.removeEventListener('webglcontextlost', onLost);
      renderer.domElement.removeEventListener('webglcontextrestored', onRestored);
      pGeo.dispose(); lGeo.dispose();
      pMat.dispose(); lMat.dispose(); haloMat.dispose();
      dotTex.dispose(); haloTex.dispose();
      scene.remove(group); scene.remove(halo);
      renderer.dispose();
      try { renderer.forceContextLoss(); } catch (e) {}
      if (renderer.domElement.parentNode) { renderer.domElement.parentNode.removeChild(renderer.domElement); }
      if (host.parentNode) { host.parentNode.removeChild(host); }
      window.__scmBGInfo.disposed = true;
    };
  }

  if (window.THREE) {
    start();
  } else {
    var existing = document.querySelector('script[data-scm-three]');
    if (existing) {
      existing.addEventListener('load', start);
    } else {
      var s = document.createElement('script');
      s.src = THREE_URL;
      s.async = true;
      s.setAttribute('data-scm-three', '1');
      s.onload = start;
      s.onerror = function () { /* offline: the CSS backdrop remains */ };
      document.head.appendChild(s);
    }
  }
})();
