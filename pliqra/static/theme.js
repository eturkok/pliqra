"use strict";

// Run before styles and the console lifecycle so the first paint uses the saved theme.
window.PliqraTheme = (() => {
 const storageKey = "pliqra.appearance";
 const system = window.matchMedia("(prefers-color-scheme: dark)");
 const valid = value => ["light", "dark", "system"].includes(value) ? value : "system";
 let preference = "system";
 try { preference = valid(window.localStorage.getItem(storageKey)); } catch { /* Storage may be disabled. */ }

 function apply() {
  const theme = preference === "system" ? (system.matches ? "dark" : "light") : preference;
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#16161a" : "#f7f7f8");
  for (const control of document.querySelectorAll("[data-theme-select]")) control.value = preference;
 }

 function set(value) {
  preference = valid(value);
  try { window.localStorage.setItem(storageKey, preference); } catch { /* Keep the choice for this page. */ }
  apply();
 }

 function control() {
  return `<label class="theme-control"><span>Appearance</span><select data-theme-select aria-label="Color theme">${[["system", "System"], ["light", "Light"], ["dark", "Dark"]].map(([value, label]) => `<option value="${value}"${preference === value ? " selected" : ""}>${label}</option>`).join("")}</select></label>`;
 }

 system.addEventListener("change", () => { if (preference === "system") apply(); });
 window.addEventListener("storage", event => {
  if (event.key !== storageKey && event.key !== null) return;
  // Ignore unrelated storage areas, including sessionStorage.
  try { if (event.storageArea && event.storageArea !== window.localStorage) return; } catch { return; }
  preference = valid(event.newValue);
  apply();
 });
 document.addEventListener("change", event => {
  if (event.target.matches("[data-theme-select]")) set(event.target.value);
 });
 apply();
 return {control, set};
})();
