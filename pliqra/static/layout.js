"use strict";

/* Layout behavior is shared by every console view, dialog and request drawer. */
window.PliqraLayout = (() => {
 const mobile = matchMedia('(max-width: 1024px)');
 let menuOpener = null, drawerOpener = null, scheduled = false;
 const focusable = root => [...root.querySelectorAll('a[href],button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),summary,[tabindex="0"]')].filter(el => el.getClientRects().length && !el.closest('[inert]'));
 function menu(open, restoreFocus = true) {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  const wasOpen = sidebar.classList.contains('open');
  const opened = Boolean(open && mobile.matches);
  if (opened) menuOpener = document.activeElement;
  sidebar.classList.toggle('open', opened);
  sidebar.setAttribute('aria-label', opened ? 'Workspace menu' : 'Workspace sidebar');
  if (opened) { sidebar.setAttribute('role','dialog'); sidebar.setAttribute('aria-modal','true'); }
  else { sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal'); }
  const main = document.querySelector('.main'), backdrop = document.getElementById('nav-backdrop');
  if (main) main.inert = opened;
  if (backdrop) backdrop.hidden = !opened;
  document.querySelector('[data-action="menu"]')?.setAttribute('aria-expanded', String(opened));
  document.body.classList.toggle('nav-open', opened);
  if (opened) requestAnimationFrame(() => {
   if (!sidebar.isConnected || !sidebar.classList.contains('open')) return;
   const target = sidebar.querySelector('[aria-current="page"]') || sidebar.querySelector('.sidebar-close');
   target.focus({preventScroll:true}); target.scrollIntoView({block:'nearest'});
  });
  else {
   if (wasOpen && restoreFocus && menuOpener?.isConnected && menuOpener.getClientRects().length) menuOpener.focus({preventScroll:true});
   menuOpener = null;
  }
 }
 function tables(root) {
  const found = [...root.querySelectorAll('table')];
  if (root.matches?.('table')) found.unshift(root);
  const parent = root.closest?.('table');
  if (parent && !found.includes(parent)) found.push(parent);
  for (const table of found) {
   const headers = [...(table.tHead?.rows[0]?.cells || [])];
   if (!headers.length || headers.some(cell => cell.colSpan > 1 || cell.rowSpan > 1)) continue;
   table.classList.add('responsive-table'); table.setAttribute('role','table');
   table.tHead.setAttribute('role','rowgroup'); table.tHead.rows[0].setAttribute('role','row');
   for (const header of headers) { header.scope = 'col'; header.setAttribute('role','columnheader'); }
   for (const body of table.tBodies) {
    body.setAttribute('role','rowgroup');
    for (const row of body.rows) {
     row.setAttribute('role','row');
     for (const [index, cell] of [...row.cells].entries()) {
      cell.setAttribute('role','cell'); cell.dataset.label = headers[index]?.textContent.trim() || '';
      if (!cell.querySelector(':scope > .table-cell-value')) {
       const content = document.createElement('div'); content.className = 'table-cell-value';
       while (cell.firstChild) content.append(cell.firstChild);
       cell.append(content);
      }
     }
    }
   }
  }
 }
 function sync() {
  scheduled = false;
  document.body.classList.toggle('dialog-open', Boolean(document.getElementById('modal')?.open));
  document.body.classList.toggle('drawer-open', Boolean(document.querySelector('.drawer')));
  for (const wrap of document.querySelectorAll('.table-wrap')) {
   const overflow = wrap.scrollWidth > wrap.clientWidth + 1;
   if (overflow) {
    wrap.tabIndex = 0; wrap.setAttribute('role','region');
    wrap.setAttribute('aria-label', (wrap.closest('.panel')?.querySelector('h2')?.textContent || 'Data table') + ' — scroll for more columns');
   } else { wrap.removeAttribute('tabindex'); wrap.removeAttribute('role'); wrap.removeAttribute('aria-label'); }
  }
 }
 function schedule() { if (!scheduled) { scheduled = true; requestAnimationFrame(sync); } }
 function closeDrawer() {
  document.getElementById('drawer-root').replaceChildren(); document.getElementById('app').inert = false;
  document.body.classList.remove('drawer-open');
  if (drawerOpener?.isConnected) drawerOpener.focus({preventScroll:true});
  drawerOpener = null;
 }
 mobile.addEventListener('change', () => menu(false));
 window.addEventListener('resize', schedule);
 document.addEventListener('keydown', event => {
  if (event.key !== 'Tab' || document.getElementById('modal')?.open) return;
  const root = document.querySelector('.drawer') || (mobile.matches ? document.querySelector('.sidebar.open') : null);
  if (!root) return;
  const items = focusable(root), first = items[0], last = items.at(-1);
  if (!first) { event.preventDefault(); return; }
  if (event.shiftKey && (document.activeElement === first || !items.includes(document.activeElement))) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && (document.activeElement === last || !items.includes(document.activeElement))) { event.preventDefault(); first.focus(); }
 });
 document.addEventListener('DOMContentLoaded', () => {
  const observer = new MutationObserver(records => {
   for (const record of records) for (const node of record.addedNodes) {
    if (node.nodeType === Node.ELEMENT_NODE && (node.matches('table,tr,tbody') || node.querySelector('table'))) tables(node);
   }
   schedule();
  });
  for (const id of ['app','modal','drawer-root']) observer.observe(document.getElementById(id), {childList:true,subtree:true,...(id==='modal'?{attributes:true,attributeFilter:['open']}:{})});
  tables(document); schedule();
 });
 return {menu, tables, closeDrawer, rememberDrawerFocus:() => { drawerOpener = document.activeElement; }};
})();
