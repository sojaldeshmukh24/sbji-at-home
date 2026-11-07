/* SBJI@Home location picker.
 *
 * Markup: <div data-location-picker [data-save-url="..."]> with a
 * [data-location-button], [data-location-manual], [data-location-status].
 *
 * - With data-save-url: posts the location to the server, then reloads.
 * - Without it: fills the latitude/longitude fields of the enclosing form.
 */
(function () {
  'use strict';

  function parseManual(text) {
    var match = String(text || '').trim().match(
      /^(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)$/
    );
    if (!match) { return null; }
    var lat = parseFloat(match[1]);
    var lng = parseFloat(match[2]);
    if (lat < -90 || lat > 90 || lng < -180 || lng > 180) { return null; }
    return { lat: lat, lng: lng };
  }

  function init(picker) {
    var button = picker.querySelector('[data-location-button]');
    var manual = picker.querySelector('[data-location-manual]');
    var manualButton = picker.querySelector('[data-location-manual-button]');
    var status = picker.querySelector('[data-location-status]');
    var saveUrl = picker.getAttribute('data-save-url');
    var form = picker.closest('form');
    var csrf = picker.querySelector('input[name="csrfmiddlewaretoken"]');

    function say(message, isError) {
      if (!status) { return; }
      status.textContent = message;
      status.setAttribute('data-state', isError ? 'error' : 'ok');
    }

    function setBusy(isBusy) {
      if (button) { button.disabled = isBusy; }
      if (manualButton) { manualButton.disabled = isBusy; }
    }

    function applyLocation(lat, lng) {
      if (saveUrl) {
        var body = new URLSearchParams();
        body.set('latitude', lat);
        body.set('longitude', lng);
        say('Saving your location...', false);
        fetch(saveUrl, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-CSRFToken': csrf ? csrf.value : ''
          },
          body: body.toString()
        }).then(function (response) {
          if (!response.ok) { throw new Error('save failed'); }
          say('Location saved. Updating...', false);
          window.location.reload();
        }).catch(function () {
          setBusy(false);
          say('Could not save your location. Please try again.', true);
        });
        return;
      }
      if (form) {
        var latField = form.querySelector('[name="latitude"]');
        var lngField = form.querySelector('[name="longitude"]');
        if (latField) { latField.value = lat; }
        if (lngField) { lngField.value = lng; }
      }
      setBusy(false);
      say('Location captured (' + lat.toFixed(5) + ', ' + lng.toFixed(5) + ').', false);
    }

    if (button) {
      button.addEventListener('click', function () {
        if (!('geolocation' in navigator)) {
          say('Your browser cannot share location. Paste coordinates below instead.', true);
          return;
        }
        setBusy(true);
        say('Finding your location...', false);
        navigator.geolocation.getCurrentPosition(
          function (position) {
            applyLocation(position.coords.latitude, position.coords.longitude);
          },
          function (error) {
            setBusy(false);
            say(
              error.code === 1
                ? 'Location permission was denied. Allow it in your browser, or paste coordinates below.'
                : 'We could not get your location. Try again, or paste coordinates below.',
              true
            );
          },
          { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 }
        );
      });
    }

    function useManual() {
      var parsed = parseManual(manual && manual.value);
      if (!parsed) {
        say('Enter coordinates like 22.7196, 75.8577 (latitude, longitude).', true);
        return false;
      }
      applyLocation(parsed.lat, parsed.lng);
      return true;
    }

    if (manualButton) {
      manualButton.addEventListener('click', useManual);
    }
    if (manual) {
      manual.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
          event.preventDefault();
          useManual();
        }
      });
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-location-picker]').forEach(init);
  });
})();
