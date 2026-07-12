// SPDX-License-Identifier: MIT
// Shared behaviour for the generated docs/_html/ pages:
// live Mermaid render (theme-aware, re-renders on toggle) + drag-to-pan.
// Loaded after mermaid.min.js. Edit here; reload the page (no rebuild needed).
(function () {
  var root = document.documentElement;
  function curTheme() {
    var t = root.getAttribute('data-theme');
    return t || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  }
  function mmVars(dark) {
    return dark ? {
      darkMode: true, background: '#0e1512',
      primaryColor: '#16241d', primaryBorderColor: '#4f9d68', primaryTextColor: '#d7e6d9',
      lineColor: '#b79a63', secondaryColor: '#1a2a22', tertiaryColor: '#14201a',
      mainBkg: '#16241d', nodeBorder: '#4f9d68', textColor: '#d7e6d9',
      actorBkg: '#16241d', actorBorder: '#4f9d68', actorTextColor: '#d7e6d9',
      signalColor: '#c9b27a', signalTextColor: '#d7e6d9', labelBoxBkgColor: '#1a2a22',
      noteBkgColor: '#243a2e', noteTextColor: '#e6f0e6', noteBorderColor: '#4f9d68'
    } : {
      primaryColor: '#e7efe6', primaryBorderColor: '#3f7d54', primaryTextColor: '#123',
      lineColor: '#8a7a55', secondaryColor: '#f3ede0', tertiaryColor: '#eef3ee',
      noteBkgColor: '#f3ede0', noteTextColor: '#123', noteBorderColor: '#3f7d54'
    };
  }

  var blocks = [].slice.call(document.querySelectorAll('pre.mermaid'));
  blocks.forEach(function (b) { b.dataset.src = b.textContent; });
  var seq = 0;

  function renderAll() {
    if (typeof mermaid === 'undefined') {
      blocks.forEach(function (b) {
        b.innerHTML = '<div class="err">mermaid.min.js not found (expected in docs/shared/).</div>';
      });
      return;
    }
    var dark = curTheme() === 'dark';
    mermaid.initialize({
      startOnLoad: false, securityLevel: 'strict', theme: 'base',
      themeVariables: Object.assign({ fontFamily: 'ui-monospace, monospace' }, mmVars(dark)),
      flowchart: { useMaxWidth: false }, sequence: { useMaxWidth: false }, class: { useMaxWidth: false }
    });
    blocks.forEach(function (b, i) {
      var id = 'm' + i + '_' + (seq++);
      mermaid.render(id, b.dataset.src)
        .then(function (r) { b.innerHTML = r.svg; })
        .catch(function (e) { b.innerHTML = '<div class="err">render error: ' + (e && e.message || e) + '</div>'; });
    });
  }
  renderAll();

  var btn = document.getElementById('themeBtn');
  if (btn) btn.addEventListener('click', function () {
    root.setAttribute('data-theme', curTheme() === 'dark' ? 'light' : 'dark');
    renderAll();
  });

  // scrollspy: mark the nav link of the section in view
  var links = [].slice.call(document.querySelectorAll('.nav a[data-target]')), map = {};
  links.forEach(function (a) { map[a.dataset.target] = a; });
  if (links.length) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (e.isIntersecting) {
          links.forEach(function (l) { l.classList.remove('active'); });
          var a = map[e.target.id]; if (a) a.classList.add('active');
        }
      });
    }, { rootMargin: '-45% 0px -50% 0px' });
    document.querySelectorAll('[id^="d"]').forEach(function (c) { io.observe(c); });
  }

  // drag-to-pan on wide diagram stages
  document.querySelectorAll('.stage').forEach(function (st) {
    var d = false, x = 0, y = 0, l = 0, t = 0;
    st.addEventListener('pointerdown', function (e) {
      if (e.target.closest('a')) return;
      d = true; st.classList.add('panning'); x = e.clientX; y = e.clientY;
      l = st.scrollLeft; t = st.scrollTop;
      try { st.setPointerCapture(e.pointerId); } catch (_) {}
    });
    st.addEventListener('pointermove', function (e) {
      if (!d) return; st.scrollLeft = l - (e.clientX - x); st.scrollTop = t - (e.clientY - y);
    });
    function up() { d = false; st.classList.remove('panning'); }
    st.addEventListener('pointerup', up);
    st.addEventListener('pointercancel', up);
  });
})();
