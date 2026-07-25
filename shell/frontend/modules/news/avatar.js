/*
 * Runi avatar — a dot-matrix "pin-screen" face rendered on a canvas.
 * States: idle (breathe/blink) · listening (reacts to Shell.micAnalyser) ·
 *         thinking (scatter + thought-graph) · speaking (mouth ← Shell.voiceAnalyser).
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

    let cv, cx, raf = null, state = 'idle';
    let W = 0, H = 0, dots = [], thoughts = [], amp = 0;

    function buildDots() {
        dots = [];
        const RX = W * 0.30, RY = H * 0.40, gap = Math.max(5, W / 40);
        for (let y = -RY; y <= RY; y += gap) {
            for (let x = -RX; x <= RX; x += gap) {
                const u = x / RX, v = y / RY;
                if (u * u + v * v > 1.04) continue;
                dots.push({ x, y, u, v, base: Math.sqrt(Math.max(0, 1 - u * u - v * v)) });
            }
        }
    }

    // Face heightfield: brow ridge, nose, eye sockets (fill on blink), mouth (opens on speak).
    function feature(u, v, blink, mouthOpen) {
        let e = 0;
        e += Math.max(0, 0.35 - Math.abs(v + 0.28)) * 0.5 * Math.max(0, 1 - Math.abs(u) * 1.1);
        e += Math.max(0, 0.40 - Math.abs(u)) * Math.max(0, 0.45 - Math.abs(v - 0.02)) * 0.9;
        const eye = Math.min(
            Math.hypot((u + 0.34) / 0.9, (v + 0.12) / 0.7),
            Math.hypot((u - 0.34) / 0.9, (v + 0.12) / 0.7));
        if (eye < 0.22) e -= (0.22 - eye) * 1.6 * (1 - blink);
        const md = Math.hypot(u / 0.95, (v - 0.5) / 0.42);
        if (md < 0.42) e -= (0.42 - md) * (0.6 + mouthOpen * 2.4);
        return e;
    }

    function readAmp() {
        let an = null;
        if (state === 'speaking') an = window.Shell && Shell.voiceAnalyser;
        else if (state === 'listening') an = window.Shell && Shell.micAnalyser;
        if (an) {
            const d = new Uint8Array(an.frequencyBinCount);
            an.getByteFrequencyData(d);
            let s = 0; for (let i = 0; i < d.length; i++) s += d[i];
            return Math.min(1, (s / d.length) / 70);
        }
        // gentle simulated life when there's no live signal
        if (state === 'listening') return 0.18 + 0.14 * Math.abs(Math.sin(performance.now() * 0.006));
        return 0;
    }

    function stepThoughts(now, cxp, cyp) {
        if ((state === 'thinking' || state === 'speaking') && thoughts.length < 7 && Math.random() < 0.05) {
            const a = Math.random() * 6.28, r = W * 0.4 + Math.random() * W * 0.15;
            thoughts.push({ x: cxp + Math.cos(a) * r, y: cyp + Math.sin(a) * r * 0.7, born: now,
                col: [[24,224,255],[198,255,46],[255,46,151],[0,255,156]][Math.random() * 4 | 0],
                r: 2 + Math.random() * 3 });
        }
        thoughts = thoughts.filter(n => (now - n.born < 2600) || state === 'thinking' || state === 'speaking');
    }
    function drawThoughts(now, cxp, cyp) {
        for (const n of thoughts) {
            const age = now - n.born, life = Math.min(1, age / 260);
            const fade = state === 'idle' ? Math.max(0, 1 - (age - 600) / 2000) : 1;
            if (fade <= 0) continue;
            const [r, g, b] = n.col;
            cx.globalAlpha = 0.20 * fade; cx.strokeStyle = `rgb(${r},${g},${b})`; cx.lineWidth = 1;
            cx.beginPath(); cx.moveTo(cxp, cyp); cx.lineTo(n.x, n.y); cx.stroke();
            cx.globalAlpha = 0.9 * fade; cx.fillStyle = `rgb(${r},${g},${b})`;
            cx.beginPath(); cx.arc(n.x, n.y, n.r * life, 0, 7); cx.fill();
        }
        cx.globalAlpha = 1;
    }

    function frame(now) {
        raf = requestAnimationFrame(frame);
        if (!cx) return;
        cx.clearRect(0, 0, W, H);
        const [r, g, b] = COL[state] || COL.idle;
        const blink = (state !== 'speaking' && (now % 3800) > 3680) ? 1 : 0;
        amp += (readAmp() - amp) * 0.35;
        const scatter = state === 'thinking' ? 1 : 0;
        const cxp = W / 2, cyp = H * 0.46;

        for (const d of dots) {
            const ripple = state === 'listening'
                ? Math.sin(Math.hypot(d.u, d.v) * 4 - now * 0.006) * amp * 0.5 : 0;
            const breathe = Math.sin(now * 0.0016 + d.v * 2) * 0.03;
            const noise = scatter
                ? Math.sin(now * 0.004 + d.x * 0.05) * Math.cos(now * 0.003 + d.y * 0.05) * 0.5 : 0;
            const mouthOpen = state === 'speaking' ? amp : 0;
            const e = (d.base + feature(d.u, d.v, blink, mouthOpen) + breathe + ripple) * (1 - scatter * 0.5);
            const sx = cxp + d.x + noise * (W * 0.05) * Math.sin(d.x);
            const sy = cyp + d.y + noise * (W * 0.05) * Math.cos(d.y) - e * (H * 0.06);
            const el = Math.max(0.04, e);
            const rad = Math.max(0.6, 0.7 + el * (W * 0.008));
            const bright = Math.max(0.06, Math.min(1, el * 0.95));
            cx.fillStyle = `rgba(${r},${g},${b},${bright})`;
            cx.beginPath(); cx.arc(sx, sy, rad, 0, 7); cx.fill();
        }
        stepThoughts(now, cxp, cyp);
        drawThoughts(now, cxp, cyp);
    }

    function resize() {
        if (!cv) return;
        const dpr = Math.min(2, window.devicePixelRatio || 1);
        W = Math.max(1, cv.clientWidth) * dpr;
        H = Math.max(1, cv.clientHeight) * dpr;
        cv.width = W; cv.height = H;
        buildDots();
    }

    return {
        mount(canvas) {
            cv = canvas; cx = canvas.getContext('2d');
            resize();
            window.addEventListener('resize', resize);
            if (!raf) raf = requestAnimationFrame(frame);
        },
        setState(s) {
            state = s;
            const el = document.getElementById('runi-status');
            if (el) el.textContent = s.toUpperCase();
        },
        unmount() {
            if (raf) cancelAnimationFrame(raf);
            raf = null;
            window.removeEventListener('resize', resize);
            thoughts = [];
        },
    };
})();
