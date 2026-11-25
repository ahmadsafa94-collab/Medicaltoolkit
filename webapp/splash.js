// Welcome splash: the logo's case opens and a stack of books, a capsule and
// a stethoscope land in front of it, then the whole screen dissolves into the
// app underneath (which loads meanwhile). The same animation as the bot's
// /start video (branding/welcome.mp4), drawn live so it is sharp at any size.
// A tap skips it. style.css also hides #splash on its own after a few seconds,
// so a script error here can never leave the app covered.
(function () {
  var splash = document.getElementById("splash");
  if (!splash) return;
  var tg = window.Telegram && window.Telegram.WebApp;
  try {
    if (tg) { tg.setHeaderColor("#5DB6F7"); tg.setBackgroundColor("#1C74D2"); }
  } catch (e) {}

  var done = false;
  function dismiss() {
    if (done) return;
    done = true;
    splash.classList.add("is-leaving");
    // back to the theme's own Telegram header and background colours
    if (window.applyTheme) { try { window.applyTheme(); } catch (e) {} }
    setTimeout(function () { splash.remove(); }, 800);
  }
  splash.addEventListener("click", dismiss);

  try {
    splash.insertAdjacentHTML("afterbegin", "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 512 512\" aria-hidden=\"true\"><defs><linearGradient id=\"sp-sky\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\"><stop offset=\"0\" stop-color=\"#5DB6F7\"/><stop offset=\"1\" stop-color=\"#1C74D2\"/></linearGradient><linearGradient id=\"sp-skyc\" gradientUnits=\"userSpaceOnUse\" x1=\"200\" y1=\"212\" x2=\"312\" y2=\"324\"><stop offset=\"0\" stop-color=\"#5DB6F7\"/><stop offset=\"1\" stop-color=\"#1C74D2\"/></linearGradient><filter id=\"sp-soft\" x=\"-20%\" y=\"-20%\" width=\"140%\" height=\"140%\"><feDropShadow dx=\"0\" dy=\"6\" stdDeviation=\"8\" flood-color=\"#0D3B66\" flood-opacity=\"0.22\"/></filter><filter id=\"sp-isoft\" x=\"-30%\" y=\"-30%\" width=\"160%\" height=\"160%\"><feDropShadow dx=\"0\" dy=\"3\" stdDeviation=\"3\" flood-color=\"#0D3B66\" flood-opacity=\"0.25\"/></filter><clipPath id=\"sp-lidClip\"><rect x=\"0\" y=\"0\" width=\"512\" height=\"230\"/></clipPath><clipPath id=\"sp-bodyClip\"><rect x=\"0\" y=\"230\" width=\"512\" height=\"282\"/></clipPath><path id=\"sp-caseShape\" d=\"M160 178 H352 a40 40 0 0 1 40 40 V318 a40 40 0 0 1 -40 40 H328 L340 412 L268 358 H160 a40 40 0 0 1 -40 -40 V218 a40 40 0 0 1 40 -40 Z\"/><path id=\"sp-crossShape\" d=\"M249 212 H263 a9 9 0 0 1 9 9 V252 H303 a9 9 0 0 1 9 9 V275 a9 9 0 0 1 -9 9 H272 V315 a9 9 0 0 1 -9 9 H249 a9 9 0 0 1 -9 -9 V284 H209 a9 9 0 0 1 -9 -9 V261 a9 9 0 0 1 9 -9 H240 V221 a9 9 0 0 1 9 -9 Z\"/><g id=\"sp-books\"><rect x=\"-54\" y=\"-28\" width=\"108\" height=\"28\" rx=\"7\" fill=\"#0D3B66\"/><rect x=\"-38\" y=\"-23\" width=\"88\" height=\"18\" rx=\"2\" fill=\"#ffffff\"/><path d=\"M-36 -17.5 H48 M-36 -11.5 H48\" stroke=\"#BFD9F2\" stroke-width=\"2\"/><rect x=\"-49\" y=\"-23\" width=\"4\" height=\"18\" rx=\"2\" fill=\"#5DB6F7\"/><rect x=\"-48\" y=\"-52\" width=\"98\" height=\"24\" rx=\"7\" fill=\"#2B8CE8\" stroke=\"#0D3B66\" stroke-width=\"3\"/><rect x=\"-44\" y=\"-47.5\" width=\"80\" height=\"15\" rx=\"2\" fill=\"#ffffff\"/><path d=\"M-42 -42.5 H34 M-42 -37.5 H34\" stroke=\"#BFD9F2\" stroke-width=\"2\"/><rect x=\"41\" y=\"-47.5\" width=\"4\" height=\"15\" rx=\"2\" fill=\"#ffffff\" opacity=\"0.85\"/><g transform=\"rotate(-4 0 -64)\"><rect x=\"-44\" y=\"-76\" width=\"90\" height=\"23\" rx=\"7\" fill=\"#9CCBF4\" stroke=\"#0D3B66\" stroke-width=\"3\"/><rect x=\"-30\" y=\"-71.5\" width=\"72\" height=\"14\" rx=\"2\" fill=\"#ffffff\"/><path d=\"M-28 -66.5 H40 M-28 -62 H40\" stroke=\"#BFD9F2\" stroke-width=\"2\"/><path d=\"M20 -58 V-44 L24 -48 L28 -44 V-58 Z\" fill=\"#0D3B66\"/></g></g><g id=\"sp-pill\"><g transform=\"translate(0 -30) rotate(-28)\"><rect x=\"-44\" y=\"-18\" width=\"88\" height=\"36\" rx=\"18\" fill=\"#ffffff\" stroke=\"#0D3B66\" stroke-width=\"5\"/><path d=\"M0 -18 H-26 a18 18 0 0 0 0 36 H0 Z\" fill=\"#0D3B66\"/><rect x=\"12\" y=\"-9\" width=\"18\" height=\"5\" rx=\"2.5\" fill=\"#CFE6FB\"/></g></g><g id=\"sp-steth\"><g transform=\"translate(-14 0)\"><path d=\"M-24 -96 V-68 a24 24 0 0 0 48 0 V-96\" stroke=\"#0D3B66\" stroke-width=\"8\" fill=\"none\" stroke-linecap=\"round\"/><circle cx=\"-24\" cy=\"-98\" r=\"7\" fill=\"#0D3B66\"/><circle cx=\"24\" cy=\"-98\" r=\"7\" fill=\"#0D3B66\"/><path d=\"M0 -44 C0 -10 46 -40 46 -20\" stroke=\"#0D3B66\" stroke-width=\"8\" fill=\"none\" stroke-linecap=\"round\"/><circle cx=\"46\" cy=\"-18\" r=\"17\" fill=\"#ffffff\" stroke=\"#0D3B66\" stroke-width=\"7\"/><circle cx=\"46\" cy=\"-18\" r=\"5\" fill=\"#0D3B66\"/></g></g></defs><g id=\"sp-shadows\" fill=\"#0D3B66\"></g><g id=\"sp-box\"><g transform=\"translate(0,-8)\"><g id=\"sp-lid\"><g filter=\"url(#sp-soft)\"><path d=\"M206 182 V160 a22 22 0 0 1 22 -22 h56 a22 22 0 0 1 22 22 V182\" stroke=\"#ffffff\" stroke-width=\"22\" fill=\"none\" stroke-linejoin=\"round\"/><use href=\"#sp-caseShape\" fill=\"#ffffff\" clip-path=\"url(#sp-lidClip)\"/></g><use href=\"#sp-crossShape\" fill=\"url(#sp-skyc)\" clip-path=\"url(#sp-lidClip)\"/></g><g id=\"sp-behind\"></g><g filter=\"url(#sp-soft)\"><use href=\"#sp-caseShape\" fill=\"#ffffff\" clip-path=\"url(#sp-bodyClip)\"/></g><rect id=\"sp-rim\" x=\"120\" y=\"230\" width=\"272\" height=\"9\" fill=\"#CFE6FB\"/><use href=\"#sp-crossShape\" fill=\"url(#sp-skyc)\" clip-path=\"url(#sp-bodyClip)\"/><circle cx=\"384\" cy=\"186\" r=\"50\" fill=\"#0D3B66\" stroke=\"#ffffff\" stroke-width=\"9\"/><text x=\"384\" y=\"203\" text-anchor=\"middle\" font-family=\"Poppins, sans-serif\" font-weight=\"700\" font-size=\"46\" fill=\"#ffffff\" letter-spacing=\"1\">AI</text></g></g><g id=\"sp-front\"></g><g id=\"sp-title\" text-anchor=\"middle\" fill=\"#ffffff\"><text x=\"256\" y=\"430\" font-family=\"'Source Sans 3', sans-serif\" font-weight=\"600\" font-size=\"24\" opacity=\"0.92\">Welcome to</text><text x=\"256\" y=\"474\" font-family=\"Poppins, sans-serif\" font-weight=\"700\" font-size=\"40\" letter-spacing=\"1\">MEDIC TOOLKIT</text></g></svg>");
  const NS = 'http://www.w3.org/2000/svg';
  const $ = (id) => document.getElementById(id);
  const clamp = (x) => Math.max(0, Math.min(1, x));
  const seg = (t, a, b) => clamp((t - a) / (b - a));
  const lerp = (a, b, p) => a + (b - a) * p;
  const easeInOut = (p) => 0.5 - 0.5 * Math.cos(Math.PI * p);
  const easeOut = (p) => 1 - Math.pow(1 - p, 3);
  const easeOutBack = (p) => { const c = 1.7; return 1 + (c + 1) * Math.pow(p - 1, 3) + c * Math.pow(p - 1, 2); };

  // Box: E3 at full size, then it rises and shrinks to make room in front.
  const BOX_FINAL = { cx: 256, cy: 200, s: 0.62 };
  const GROUND = 362;
  const ITEMS = [
    { id: 'books', t0: 1.45, from: [214, 345], land: [140, GROUND], spin: -360 },
    { id: 'pill',  t0: 1.85, from: [256, 345], land: [256, GROUND], spin: 360 },
    { id: 'steth', t0: 2.25, from: [298, 345], land: [372, GROUND], spin: 0 },
  ];
  const FLIGHT = 0.8, SQUASH = 0.3;

  const nodes = ITEMS.map((it) => {
    const g = document.createElementNS(NS, 'g');
    const u = document.createElementNS(NS, 'use'); u.setAttribute('href', '#sp-' + it.id);
    const f = document.createElementNS(NS, 'g'); f.setAttribute('filter', 'url(#sp-isoft)'); f.appendChild(u); g.appendChild(f);
    const sh = document.createElementNS(NS, 'ellipse');
    sh.setAttribute('cx', it.land[0]); sh.setAttribute('cy', GROUND + 4); sh.setAttribute('rx', 46); sh.setAttribute('ry', 7);
    $('sp-shadows').appendChild(sh);
    return { g, sh };
  });

  function boxAt(t) {
    const pop = easeOutBack(seg(t, 0, 0.45));
    const move = easeInOut(seg(t, 0.55, 1.1));
    const s0 = lerp(0.82, 1, pop);
    return { cx: 256, cy: lerp(252, BOX_FINAL.cy, move), s: lerp(s0, BOX_FINAL.s, move) };
  }
  // A point in the box's own (E3) coordinates -> the screen.
  const toScreen = (b, x, y) => [b.cx + (x - 256) * b.s, b.cy + (y - 8 - 252) * b.s];

  function render(t) {
    const b = boxAt(t);
    $('sp-box').setAttribute('transform', `translate(${b.cx} ${b.cy}) scale(${b.s}) translate(-256 -252)`);
    const open = easeOutBack(seg(t, 1.05, 1.45)) * (1 - easeInOut(seg(t, 3.25, 3.6)));
    $('sp-lid').setAttribute('transform', `rotate(${open * 28} 392 230)`);

    ITEMS.forEach((it, i) => {
      const { g, sh } = nodes[i];
      const p = seg(t, it.t0, it.t0 + FLIGHT);
      if (t < it.t0) { g.remove(); sh.setAttribute('opacity', 0); return; }
      const pe = easeInOut(p);
      const P0 = toScreen(b, it.from[0], it.from[1]);
      const P2 = it.land;
      const P1 = [(P0[0] + P2[0]) / 2, 6];
      const x = (1 - pe) ** 2 * P0[0] + 2 * (1 - pe) * pe * P1[0] + pe * pe * P2[0];
      const y = (1 - pe) ** 2 * P0[1] + 2 * (1 - pe) * pe * P1[1] + pe * pe * P2[1];
      const s = lerp(0.5, 1, easeOut(p));
      const q = seg(t, it.t0 + FLIGHT, it.t0 + FLIGHT + SQUASH);
      const squash = q > 0 && q < 1 ? Math.sin(Math.PI * q) * Math.exp(-2 * q) : 0;
      const sx = s * (1 + 0.14 * squash), sy = s * (1 - 0.16 * squash);
      const rot = it.spin * pe;
      // Up out of the case behind its front, then in front of it once past the top of the arc.
      const layer = pe < 0.5 ? $('sp-behind') : $('sp-front');
      if (g.parentNode !== layer) layer.appendChild(g);
      if (layer === $('sp-behind')) {
        // the behind layer lives inside the box's transform: convert back
        const bx = (x - b.cx) / b.s + 256, by = (y - b.cy) / b.s + 252 + 8;
        g.setAttribute('transform', `translate(${bx} ${by}) rotate(${rot} 0 -40) scale(${sx / b.s} ${sy / b.s})`);
      } else {
        g.setAttribute('transform', `translate(${x} ${y}) rotate(${rot} 0 -40) scale(${sx} ${sy})`);
      }
      sh.setAttribute('opacity', 0.22 * clamp((pe - 0.55) / 0.45));
      sh.setAttribute('rx', 46 * lerp(0.4, 1, clamp((pe - 0.55) / 0.45)));
    });

    const ti = easeOut(seg(t, 3.45, 4.0));
    $('sp-title').setAttribute('opacity', ti);
    $('sp-title').setAttribute('transform', `translate(0 ${lerp(14, 0, ti)})`);
  }

    var END = 4.0, HOLD = 0.6;
    var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) {
      render(END);
      setTimeout(dismiss, 1200);
      return;
    }
    render(0);
    var t0 = null;
    var step = function (now) {
      if (done) return;
      if (t0 === null) t0 = now;
      var t = (now - t0) / 1000;
      render(Math.min(t, END));
      if (t >= END + HOLD) dismiss();
      else requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  } catch (e) {
    dismiss();
  }
})();
