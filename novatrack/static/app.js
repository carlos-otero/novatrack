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
