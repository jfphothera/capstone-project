/* Runs synchronously in <head>, before paint. Shared by all three interfaces. */
(() => {
  'use strict';
  const key = 'capstone.theme.v1';
  const valid = value => ['light', 'dark', 'system'].includes(value) ? value : 'system';
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  let choice = 'system';
  try { choice = valid(window.localStorage.getItem(key)); } catch (_) { /* private storage */ }
  const apply = () => {
    const theme = choice === 'system' ? (media.matches ? 'dark' : 'light') : choice;
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.themePreference = choice;
    document.documentElement.style.colorScheme = theme;
    document.querySelectorAll('[data-theme-picker]').forEach(select => { select.value = choice; });
  };
  apply();
  media.addEventListener('change', () => { if (choice === 'system') apply(); });
  window.addEventListener('storage', event => {
    if (event.key === key || event.key === null) { choice = valid(event.newValue); apply(); }
  });
  const bind = () => {
    document.querySelectorAll('[data-theme-picker]').forEach(select => {
      select.value = choice;
      select.addEventListener('change', () => {
        choice = valid(select.value);
        try { window.localStorage.setItem(key, choice); } catch (_) { /* still works in this window */ }
        apply();
      });
    });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind, {once: true});
  else bind();
})();
