/* Adds a Show / Hide button to every password field on the page. */
(function () {
  'use strict';

  function enhance(input) {
    if (input.dataset.passwordToggle === 'ready') { return; }
    input.dataset.passwordToggle = 'ready';

    var wrap = document.createElement('span');
    wrap.className = 'password-field';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'password-toggle';
    button.textContent = 'Show';
    button.setAttribute('aria-label', 'Show password');
    button.setAttribute('aria-pressed', 'false');
    wrap.appendChild(button);

    button.addEventListener('click', function () {
      var reveal = input.type === 'password';
      input.type = reveal ? 'text' : 'password';
      button.textContent = reveal ? 'Hide' : 'Show';
      button.setAttribute('aria-label', reveal ? 'Hide password' : 'Show password');
      button.setAttribute('aria-pressed', reveal ? 'true' : 'false');
      input.focus({ preventScroll: true });
    });

    // Never leave a password visible after the form is sent or the page is restored.
    var form = input.form;
    if (form) {
      form.addEventListener('submit', function () {
        if (input.type !== 'password') { input.type = 'password'; }
      });
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('input[type="password"]').forEach(enhance);
  });
  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) { return; }
    document.querySelectorAll('input[data-password-toggle="ready"]').forEach(function (input) {
      input.type = 'password';
      var button = input.parentNode.querySelector('.password-toggle');
      if (button) { button.textContent = 'Show'; button.setAttribute('aria-pressed', 'false'); }
    });
  });
})();
