/* Shell behaviours shared by every page (delivered base64-encoded by ui/shell.py, so any characters are safe here).
   - light / dark theme toggle (stored in localStorage, default dark)
   - smooth in-page scrolling for [data-go] links
   - client-side page routing for [data-page] links (clicks the real Streamlit nav link: session state is preserved)
   - scroll reveal fallback (IntersectionObserver) for browsers without CSS scroll-driven animations                       */
(function () {
  if (window.__scmNav) { return; }
  window.__scmNav = 1;

  function target(id) { return document.querySelector('[data-id="' + id + '"]'); }

  /* ---------- theme ---------- */
  var saved = null;
  try { saved = localStorage.getItem('scmTheme'); } catch (e) {}
  if (!saved) { saved = 'dark'; }
  document.documentElement.setAttribute('data-theme', saved);

  /* ---------- scrolling + routing ---------- */
  function navLinks() { return Array.prototype.slice.call(document.querySelectorAll('.st-key-topnav a[data-testid="stPageLink-NavLink"]')); }
  function pageOf(a) { var h = a.getAttribute('href') || ''; return h === '' || h === '/' ? 'home' : h.replace(/^\//, ''); }
  function navLink(page) { return navLinks().filter(function (a) { return pageOf(a) === page; })[0]; }
  function currentPage() {
    var path = location.pathname.replace(/\/+$/, '');
    var seg = path.split('/').pop();
    return seg === '' ? 'home' : seg;
  }
  function isActive(page) { return currentPage() === page; }
  /* Streamlit marks the active page only with a generated class: derive it from the URL instead */
  setInterval(function () {
    var cur = currentPage();
    navLinks().forEach(function (a) { a.setAttribute('data-active', pageOf(a) === cur ? '1' : '0'); });
  }, 250);
  var pendingGo = null;
  function goScroll(id) {
    var el = target(id);
    if (el) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); return true; }
    return false;
  }
  document.addEventListener('click', function (e) {
    var n = e.target;
    var t = n.closest ? n.closest('[data-theme-toggle]') : null;
    if (t) {
      e.preventDefault();
      var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try { localStorage.setItem('scmTheme', next); } catch (err) {}
      return;
    }
    var a = n.closest ? n.closest('[data-go],[data-page]') : null;
    if (!a) { return; }
    e.preventDefault();
    var page = a.getAttribute('data-page');
    var go = a.getAttribute('data-go');
    if (page) {
      var link = navLink(page);
      if (link && !isActive(page)) { pendingGo = go; link.click(); return; }
    }
    if (go) { goScroll(go); }
  });
  /* after a client-side route change, finish the scroll the user asked for */
  setInterval(function () {
    if (pendingGo && goScroll(pendingGo)) { pendingGo = null; }
  }, 350);

  /* ---------- reveal fallback ---------- */
  var native = window.CSS ? CSS.supports('animation-timeline: view()') : false;
  if (!native && window.IntersectionObserver) {
    document.documentElement.classList.add('rv-js');
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (en) {
        if (en.isIntersecting) { en.target.setAttribute('data-rv', 'in'); io.unobserve(en.target); }
      });
    }, { threshold: 0.12 });
    setInterval(function () {
      document.querySelectorAll('.reveal:not([data-rv])').forEach(function (el) {
        var r = el.getBoundingClientRect();
        if (r.top > window.innerHeight) { el.setAttribute('data-rv', 'wait'); io.observe(el); }
        else { el.setAttribute('data-rv', 'in'); }
      });
    }, 400);
  }
})();
