(function () {
  var originalFocus = HTMLElement.prototype.focus;
  var allowFocusScroll = false;

  function enableNormalFocus() {
    allowFocusScroll = true;
  }

  HTMLElement.prototype.focus = function patchedFocus(options) {
    if (allowFocusScroll) {
      return originalFocus.call(this, options);
    }

    var nextOptions = options && typeof options === "object" ? Object.assign({}, options) : {};
    nextOptions.preventScroll = true;
    return originalFocus.call(this, nextOptions);
  };

  window.addEventListener("pointerdown", enableNormalFocus, { capture: true, once: true });
  window.addEventListener("keydown", enableNormalFocus, { capture: true, once: true });
})();
