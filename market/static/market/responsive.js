/* Wrap wide tables so they scroll sideways inside their own box on phones. */
(function () {
  'use strict';
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('table').forEach(function (table) {
      var parent = table.parentNode;
      if (parent.classList.contains('table-scroll')) { return; }
      var box = document.createElement('div');
      box.className = 'table-scroll';
      box.setAttribute('role', 'region');
      box.setAttribute('tabindex', '0');
      box.setAttribute('aria-label', 'Scrollable table');
      parent.insertBefore(box, table);
      box.appendChild(table);
    });
  });
})();
