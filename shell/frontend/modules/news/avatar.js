/*
 * Runi avatar — a generative, audio-reactive AI core (canvas).
 * States: idle · listening (reacts to Shell.micAnalyser) · thinking (swirl +
 * mindmap) · speaking (spectrum + shockwaves ← Shell.voiceAnalyser).
 *
 * window.RuniAvatar.mount(canvasEl) / .setState(name) / .unmount()
 */
window.RuniAvatar = (function () {
    'use strict';
    const RM = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const COL = {
        idle:      [24, 224, 255],
        listening: [24, 224, 255],
        thinking:  [198, 255, 46],
        speaking:  [255, 46, 151],
    };
    const N = 72;                         // spectrum bars (symmetric)
    let cv, cx, raf = null, state = 'idle';
    let W = 0, H = 0, CX = 0, CY = 0, R = 0;
    let spec = new Float32Array(N), amp = 0, rot = 0, rot2 = 0, spin = 0;
    let nodes = [], pulses = [], ripples = [], pulseAcc = 0, ripAcc = 0;

    function build() {
        nodes = Array.from({ length: 11 }, () => ({
            a: Math.random() * 6.283, va: (Math.random() - 0.5) * 0.003,
            r: 0.46 + Math.random() * 0.26, rph: Math.random() * 6.283, flare: 0,
        }));
        pulses = []; ripples = [];
    }

    function readBins() {
        const an = state === 'speaking' ? (window.Shell && Shell.voiceAnalyser)
                 : state === 'listening' ? (window.Shell && Shell.micAnalyser) : null;
        if (an) { const d = new Uint8Array(an.frequencyBinCount); an.getByteFrequencyData(d); return d; }
        return null;
    }

    function frame(now) {
        raf = requestAnimationFrame(frame);
        if (!cx) return;
        // self-heal: keep the drawing buffer matched to the on-screen box so the
        // core stays a circle (not a squished ellipse) regardless of layout timing.
        const dpr = Math.min(2, window.devicePixelRatio || 1);
        const bw = Math.round(Math.max(1, cv.clientWidth) * dpr);
        const bh = Math.round(Math.max(1, cv.clientHeight) * dpr);
        if (bw > 2 && (cv.width !== bw || cv.height !== bh)) {
            cv.width = bw; cv.height = bh; W = bw; H = bh; CX = bw / 2; CY = bh / 2; R = Math.min(bw, bh) / 2;
        }
        const [r, g, b] = COL[state] || COL.idle;
        const t = now / 1000;
        const bins = readBins();

        // ---- drive spectrum ----
        let overall = 0;
        for (let i = 0; i < N; i++) {
            const half = i < N / 2 ? i : (N - 1 - i);
            let v;
            if (bins) v = bins[2 + half * 2] / 255;
            else if (state === 'speaking')  v = 0.15 + 0.15 * Math.abs(Math.sin(half * 0.6 + now * 0.02));
            else if (state === 'listening') v = 0.12 + 0.10 * Math.abs(Math.sin(half * 0.5 + now * 0.006));
            else if (state === 'thinking')  v = 0.18 + 0.16 * Math.abs(Math.sin(half * 0.9 - now * 0.01)) + 0.1 * Math.random();
            else v = 0.06 + 0.05 * Math.abs(Math.sin(half * 0.4 + now * 0.0018));
            spec[i] += (v - spec[i]) * 0.4; overall += spec[i];
        }
        overall /= N; amp += (overall - amp) * 0.3;

        cx.clearRect(0, 0, W, H);
        cx.save(); cx.translate(CX, CY);
        rot += 0.0016 + (state === 'thinking' ? 0.006 : 0);
        rot2 -= 0.0026 + (state === 'thinking' ? 0.004 : 0);
        spin += 0.02;

        const r0 = R * 0.30, maxLen = R * 0.34 * (state === 'speaking' ? 1.25 : 1);

        // ---- HUD rings + ticks ----
        cx.lineWidth = Math.max(1, R * 0.012);
        arc(R * 0.9, rot, 0.15, 1.0, `rgba(${r},${g},${b},0.5)`);
        arc(R * 0.9, rot + Math.PI, 0.15, 1.0, `rgba(${r},${g},${b},0.5)`);
        arc(R * 0.82, rot2, 0.55, 0.9, `rgba(${r},${g},${b},0.22)`);
        cx.strokeStyle = `rgba(${r},${g},${b},0.28)`; cx.lineWidth = 1;
        for (let i = 0; i < 48; i++) { const a = i / 48 * 6.283 + rot * 0.4, ri = R * 0.72, ro = ri + (i % 4 === 0 ? R * 0.05 : R * 0.025);
            cx.beginPath(); cx.moveTo(Math.cos(a) * ri, Math.sin(a) * ri); cx.lineTo(Math.cos(a) * ro, Math.sin(a) * ro); cx.stroke(); }

        // ---- mindmap ----
        const swirl = state === 'thinking' ? 0.011 : state === 'speaking' ? 0.005 : 0.0018;
        const np = [];
        for (const n of nodes) { n.a += n.va + swirl; const rr = R * n.r * (1 + 0.04 * Math.sin(t * 0.9 + n.rph));
            np.push([Math.cos(n.a) * rr, Math.sin(n.a) * rr]); n.flare *= 0.92; }
        for (let i = 0; i < nodes.length; i++) { const p = np[i];
            cx.strokeStyle = `rgba(${r},${g},${b},0.10)`; cx.lineWidth = 1;
            cx.beginPath(); cx.moveTo(0, 0); cx.lineTo(p[0], p[1]); cx.stroke();
            let best = -1, bd = 1e9; for (let j = i + 1; j < nodes.length; j++) { const dx = np[j][0] - p[0], dy = np[j][1] - p[1], d = dx * dx + dy * dy; if (d < bd) { bd = d; best = j; } }
            if (best >= 0 && bd < (R * 0.46) * (R * 0.46)) { cx.strokeStyle = `rgba(${r},${g},${b},0.07)`;
                cx.beginPath(); cx.moveTo(p[0], p[1]); cx.lineTo(np[best][0], np[best][1]); cx.stroke(); } }
        pulseAcc += state === 'thinking' ? 0.10 : state === 'speaking' ? (0.05 + amp * 0.8) : state === 'listening' ? (0.02 + amp * 0.4) : 0.012;
        while (pulseAcc >= 1) { pulseAcc -= 1; pulses.push({ ni: Math.random() * nodes.length | 0, t: 0, sp: 0.028 + Math.random() * 0.03 }); }
        cx.shadowColor = `rgba(${r},${g},${b},1)`;
        for (let k = pulses.length - 1; k >= 0; k--) { const p = pulses[k]; p.t += p.sp;
            if (p.t >= 1) { nodes[p.ni].flare = 1; pulses.splice(k, 1); continue; }
            const q = np[p.ni]; cx.shadowBlur = R * 0.06; cx.fillStyle = `rgba(${r},${g},${b},${(1 - p.t) * 0.95})`;
            cx.beginPath(); cx.arc(q[0] * p.t, q[1] * p.t, Math.max(1.4, R * 0.018), 0, 6.283); cx.fill(); }
        for (let i = 0; i < nodes.length; i++) { const p = np[i], fl = nodes[i].flare;
            cx.shadowBlur = R * 0.05 + fl * R * 0.12; cx.fillStyle = `rgba(${r},${g},${b},${0.4 + fl * 0.6})`;
            cx.beginPath(); cx.arc(p[0], p[1], Math.max(1.4, R * 0.017) + fl * R * 0.02, 0, 6.283); cx.fill(); }
        cx.shadowBlur = 0;

        // ---- speaking shockwaves ----
        if (state === 'speaking') { ripAcc += 0.05 + amp * 0.5; while (ripAcc >= 1) { ripAcc -= 1; if (amp > 0.14) ripples.push({ born: now }); } }
        for (let k = ripples.length - 1; k >= 0; k--) { const age = (now - ripples[k].born) / 850; if (age >= 1) { ripples.splice(k, 1); continue; }
            cx.strokeStyle = `rgba(${r},${g},${b},${(1 - age) * 0.4})`; cx.lineWidth = Math.max(1, R * 0.015);
            cx.beginPath(); cx.arc(0, 0, R * 0.30 + age * R * 0.62, 0, 6.283); cx.stroke(); }

        // ---- radial spectrum ----
        cx.lineCap = 'round';
        for (let i = 0; i < N; i++) { const a = i / N * 6.283 + spin * 0.15, len = r0 + spec[i] * maxLen;
            cx.strokeStyle = `rgba(${r},${g},${b},${0.35 + spec[i] * 0.65})`;
            cx.lineWidth = Math.max(1.6, R * 0.026); cx.shadowColor = `rgba(${r},${g},${b},0.9)`; cx.shadowBlur = R * 0.05 + spec[i] * R * 0.1;
            cx.beginPath(); cx.moveTo(Math.cos(a) * r0, Math.sin(a) * r0); cx.lineTo(Math.cos(a) * len, Math.sin(a) * len); cx.stroke(); }
        cx.shadowBlur = 0;
        cx.strokeStyle = `rgba(${r},${g},${b},0.5)`; cx.lineWidth = 1.2;
        cx.beginPath(); cx.arc(0, 0, r0 - R * 0.04, 0, 6.283); cx.stroke();

        // ---- core orb ----
        const breathe = 1 + 0.06 * Math.sin(t * 1.6) + amp * (state === 'speaking' ? 1.15 : 0.6);
        const cr = R * 0.15 * breathe;
        const grd = cx.createRadialGradient(0, 0, 0, 0, 0, cr * 2.4);
        grd.addColorStop(0, 'rgba(255,255,255,0.9)');
        grd.addColorStop(0.25, `rgba(${r},${g},${b},0.9)`);
        grd.addColorStop(1, `rgba(${r},${g},${b},0)`);
        cx.fillStyle = grd; cx.beginPath(); cx.arc(0, 0, cr * 2.4, 0, 6.283); cx.fill();
        cx.fillStyle = 'rgba(255,255,255,0.85)'; cx.beginPath(); cx.arc(0, 0, cr * 0.42, 0, 6.283); cx.fill();
        cx.restore();
    }

    function arc(rad, start, gap, span, style) {
        cx.strokeStyle = style; const seg = span * Math.PI;
        cx.beginPath(); cx.arc(0, 0, rad, start, start + seg); cx.stroke();
        cx.beginPath(); cx.arc(0, 0, rad, start + seg + gap, start + 2 * seg + gap); cx.stroke();
    }

    function resize() {
        if (!cv) return;
        const dpr = Math.min(2, window.devicePixelRatio || 1);
        W = Math.max(1, cv.clientWidth) * dpr; H = Math.max(1, cv.clientHeight) * dpr;
        cv.width = W; cv.height = H; CX = W / 2; CY = H / 2; R = Math.min(W, H) / 2;
    }

    return {
        mount(canvas) {
            cv = canvas; cx = canvas.getContext('2d'); build(); resize();
            window.addEventListener('resize', resize);
            if (!raf) raf = requestAnimationFrame(frame);
        },
        setState(s) {
            state = s;
            const el = document.getElementById('runi-status');
            if (el) el.textContent = s.toUpperCase();
        },
        unmount() { if (raf) cancelAnimationFrame(raf); raf = null; window.removeEventListener('resize', resize); },
    };
})();
