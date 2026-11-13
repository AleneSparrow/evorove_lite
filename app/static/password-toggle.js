"use strict";

function bindPasswordToggles() {
  var buttons = document.querySelectorAll("button.password-toggle");
  for (var i = 0; i < buttons.length; i++) {
    var button = buttons[i];
    var input = button.parentElement && button.parentElement.querySelector("input");
    if (!input) {
      continue;
    }
    button.hidden = false;
    button.addEventListener("click", function (event) {
      var control = event.currentTarget;
      var field = control.parentElement.querySelector("input");
      var reveal = field.getAttribute("type") === "password";
      field.setAttribute("type", reveal ? "text" : "password");
      control.setAttribute("aria-pressed", reveal ? "true" : "false");
      control.setAttribute("aria-label", reveal ? "Hide password" : "Show password");
    });
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bindPasswordToggles);
} else {
  bindPasswordToggles();
}
