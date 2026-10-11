/* F12 — notification preferences panel.
 *
 * Loads the caller's effective preferences (server-side, so a user with no
 * saved record correctly sees the documented defaults), renders one checkbox
 * per SUPPORTED category, and saves the whole selection.
 *
 * Deliberately small: it reuses the existing notifications panel and the
 * global CSRF fetch patch in base.html, and never touches notifications the
 * user already has. CSRF is injected automatically by the fetch wrapper, so
 * this file does not handle tokens itself.
 */
(function () {
  'use strict';

  var toggle = document.getElementById('notif-prefs-toggle');
  var panel = document.getElementById('notif-prefs-panel');
  if (!toggle || !panel) return;

  var list = document.getElementById('notif-prefs-list');
  var saveBtn = document.getElementById('notif-prefs-save');
  var status = document.getElementById('notif-prefs-status');
  var loaded = false;
  var loading = false;

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function say(message, tone) {
    if (!status) return;
    status.textContent = message;
    status.style.color = tone === 'error'
      ? 'var(--danger, #f87171)'
      : 'var(--text-mid)';
  }

  function render(categories, prefs) {
    if (!categories.length) {
      list.textContent = 'No notification categories are available.';
      return;
    }
    list.textContent = '';
    var frag = document.createDocumentFragment();

    categories.forEach(function (cat) {
      var wrap = document.createElement('div');
      wrap.className = 'flex items-start gap-2';

      var box = document.createElement('input');
      box.type = 'checkbox';
      box.id = 'notif-pref-' + cat.key;
      box.name = 'category';
      box.value = cat.key;
      box.checked = !!prefs[cat.key];
      box.className = 'mt-0.5 accent-[var(--accent)]';
      box.setAttribute('data-category', cat.key);

      var label = document.createElement('label');
      label.setAttribute('for', box.id);
      label.className = 'flex-1 cursor-pointer';

      var title = document.createElement('span');
      title.className = 'block text-sm text-white';
      title.textContent = cat.label;

      var desc = document.createElement('span');
      desc.className = 'block text-xs';
      desc.style.color = 'var(--text-low)';
      desc.textContent = cat.description;

      label.appendChild(title);
      label.appendChild(desc);
      wrap.appendChild(box);
      wrap.appendChild(label);
      frag.appendChild(wrap);
    });

    list.appendChild(frag);
  }

  function load() {
    if (loaded || loading) return;
    loading = true;
    fetch('/api/notification-preferences', { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('load failed');
        return r.json();
      })
      .then(function (data) {
        render(data.categories || [], data.preferences || {});
        loaded = true;
      })
      .catch(function () {
        list.textContent = 'Could not load notification settings.';
      })
      .finally(function () { loading = false; });
  }

  function collect() {
    var prefs = {};
    Array.prototype.forEach.call(
      list.querySelectorAll('input[data-category]'), function (box) {
        prefs[box.getAttribute('data-category')] = box.checked;
      });
    return prefs;
  }

  if (saveBtn) {
    saveBtn.addEventListener('click', function () {
      saveBtn.disabled = true;
      say('Saving…');
      fetch('/api/notification-preferences', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preferences: collect() })
      })
        .then(function (r) {
          return r.json().then(function (body) {
            return { ok: r.ok, body: body };
          });
        })
        .then(function (res) {
          if (!res.ok) {
            say(res.body.error || 'Could not save settings.', 'error');
            return;
          }
          say('Saved.');
        })
        .catch(function () { say('Could not save settings.', 'error'); })
        .finally(function () { saveBtn.disabled = false; });
    });
  }

  toggle.addEventListener('click', function () {
    var open = panel.classList.toggle('hidden');
    toggle.setAttribute('aria-expanded', open ? 'false' : 'true');
    if (!open) load();
  });
})();