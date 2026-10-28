// Shared header of every page: brand, page links (Dashboard / Training & calibration / Replay tests), status line. Each page has <div id="shell"></div> at the top of .wrap.
(function () {
  var pages = [['/', 'Dashboard'], ['/calibration', 'Training & calibration'], ['/replay', 'Replay (tests)']];
  var path = location.pathname.replace(/\/+$/, '') || '/';
  var links = pages.map(function (p) { return '<a href="' + p[0] + '"' + (p[0] === path ? ' class="on"' : '') + '>' + p[1] + '</a>'; }).join('');
  var el = document.getElementById('shell');
  if (!el) return;
  el.innerHTML = '<div class="topbar"><div class="brand"><span class="dot"></span><h1>Survival Agent</h1></div><div class="status" id="statusLine">checking status…</div></div>' +
    '<nav class="pages">' + links + '</nav>';
  // one status line for every page: window.loadStatus() is also called by the dashboard after a settings change
  window.loadStatus = function () {
    return fetch('/api/status').then(function (r) { return r.json(); }).then(function (s) {
      var dry = s.dry_run ? '<span class="on">dry-run</span>' : '<span class="off">LIVE</span>';
      var eng = s.engine_enabled ? '<span class="on">enabled</span>' : '<span class="off">disabled</span>';
      var warn = s.freqtrade_live ? '<span style="color:var(--red);font-weight:600">● LIVE — REAL MONEY</span> · ' : '';
      document.getElementById('statusLine').innerHTML = warn + 'engine: ' + eng + ' · ' + dry + ' · cycles run: ' + s.cycles_run;
    }).catch(function () { document.getElementById('statusLine').textContent = 'status unavailable'; });
  };
  window.loadStatus();
  setInterval(window.loadStatus, 30000);
})();
