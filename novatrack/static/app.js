/* NovaTrack — minimal vanilla JS; no external dependencies */

'use strict';

/* Modal open/close via data attributes (replaces inline onclick) */
document.addEventListener('click', function (e) {
  var opener = e.target.closest('[data-modal-open]');
  if (opener) {
    var id = opener.getAttribute('data-modal-open');
    var dlg = document.getElementById(id);
    if (dlg) dlg.showModal();
  }
  var closer = e.target.closest('[data-modal-close]');
  if (closer) {
    var id2 = closer.getAttribute('data-modal-close');
    var dlg2 = document.getElementById(id2);
    if (dlg2) dlg2.close();
  }
});

/* Close modal on backdrop click */
document.querySelectorAll('dialog.modal').forEach(function (dlg) {
  dlg.addEventListener('click', function (e) {
    if (e.target === dlg) dlg.close();
  });
});

/* Confirm before dangerous form submits */
document.querySelectorAll('form[data-confirm]').forEach(function (form) {
  form.addEventListener('submit', function (e) {
    if (!window.confirm(form.dataset.confirm)) e.preventDefault();
  });
});

/* ---------------------------------------------------------------------------
 * Carrier search — drives inputs with data-carrier-search="<target-id>"
 *
 * When the user types in a carrier search input, we filter the offline
 * carriers.json catalogue and show a small results list.  Clicking a result
 * fills the target numeric input and clears the search box.
 * --------------------------------------------------------------------------- */
(function () {
  var CARRIERS_URL = '/static/carriers.json';
  var _carriers = null;
  var _loadPromise = null;

  function loadCarriers() {
    if (_carriers) return Promise.resolve(_carriers);
    if (_loadPromise) return _loadPromise;
    _loadPromise = fetch(CARRIERS_URL)
      .then(function (r) { return r.json(); })
      .then(function (data) { _carriers = data; return data; })
      .catch(function () { _carriers = []; return []; });
    return _loadPromise;
  }

  function filterCarriers(query, limit) {
    limit = limit || 12;
    if (!_carriers) return [];
    var q = query.toLowerCase().trim();
    if (!q) return [];
    var results = [];
    for (var i = 0; i < _carriers.length && results.length < limit; i++) {
      var c = _carriers[i];
      if (String(c.key) === q || c.name.toLowerCase().indexOf(q) !== -1) {
        results.push(c);
      }
    }
    return results;
  }

  function getResultsEl(input) {
    /* Find the nearest .carrier-results sibling (or cousin in same form). */
    var next = input.nextElementSibling;
    if (next && next.classList.contains('carrier-results')) return next;
    var form = input.closest('form') || input.parentElement;
    return form ? form.querySelector('.carrier-results') : null;
  }

  function attachCarrierSearch(input) {
    var targetId = input.getAttribute('data-carrier-search');
    var targetInput = document.getElementById(targetId);
    if (!targetInput) return;

    /* Preload carrier list on first focus */
    input.addEventListener('focus', function () { loadCarriers(); }, { once: true });

    input.addEventListener('input', function () {
      var q = input.value;
      var resultsEl = getResultsEl(input);
      if (!resultsEl) return;
      if (q.length < 2) { resultsEl.innerHTML = ''; return; }
      loadCarriers().then(function () {
        var hits = filterCarriers(q, 12);
        if (!hits.length) {
          resultsEl.innerHTML = '<span class="carrier-noresults">No carriers found</span>';
          return;
        }
        var html = '<ul class="carrier-list" role="listbox">';
        hits.forEach(function (c) {
          html += '<li class="carrier-item" role="option" tabindex="0"'
            + ' data-key="' + c.key + '" data-name="' + c.name.replace(/"/g, '&quot;') + '">'
            + c.key + ' \u2014 ' + c.name.replace(/</g, '&lt;').replace(/>/g, '&gt;')
            + '</li>';
        });
        html += '</ul>';
        resultsEl.innerHTML = html;
      });
    });

    /* Delegate click on result items */
    document.addEventListener('click', function (e) {
      var resultsEl = getResultsEl(input);
      if (!resultsEl) return;
      var item = e.target.closest('.carrier-item');
      if (item && resultsEl.contains(item)) {
        targetInput.value = item.getAttribute('data-key');
        input.value = item.getAttribute('data-key') + ' \u2014 ' + item.getAttribute('data-name');
        resultsEl.innerHTML = '';
      }
    });

    /* Keyboard navigation on result list */
    document.addEventListener('keydown', function (e) {
      var resultsEl = getResultsEl(input);
      if (!resultsEl) return;
      var focused = resultsEl.querySelector('.carrier-item:focus');
      if (!focused) return;
      if (e.key === 'Enter') {
        e.preventDefault();
        targetInput.value = focused.getAttribute('data-key');
        input.value = focused.getAttribute('data-key') + ' \u2014 ' + focused.getAttribute('data-name');
        resultsEl.innerHTML = '';
        input.focus();
      }
    });
  }

  /* Attach to all carrier-search inputs present at page load */
  document.querySelectorAll('[data-carrier-search]').forEach(attachCarrierSearch);
})();
