(function () {
  function thread() {
    return document.getElementById("thread");
  }

  function scrollThread() {
    var node = thread();
    if (node) {
      node.scrollTop = node.scrollHeight;
    }
  }

  function formOf(event) {
    var node = event.detail && event.detail.elt ? event.detail.elt : event.target;
    if (!node || !node.classList || !node.classList.contains("chat-form")) {
      return null;
    }
    return node;
  }

  document.addEventListener("DOMContentLoaded", scrollThread);

  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var xhr = event.detail && event.detail.xhr;
    if (xhr && xhr.status === 429) {
      event.detail.shouldSwap = true;
      event.detail.isError = false;
    }
  });

  document.body.addEventListener("htmx:beforeRequest", function (event) {
    var form = formOf(event);
    if (!form) {
      return;
    }
    var button = form.querySelector("button");
    if (button) {
      button.disabled = true;
    }
    var note = document.getElementById("typing");
    if (note) {
      note.hidden = false;
    }
  });

  document.body.addEventListener("htmx:afterRequest", function (event) {
    var form = formOf(event);
    if (!form) {
      return;
    }
    var button = form.querySelector("button");
    if (button) {
      button.disabled = false;
    }
    var note = document.getElementById("typing");
    if (note) {
      note.hidden = true;
    }
    if (event.detail && event.detail.successful) {
      var box = form.querySelector("textarea");
      if (box) {
        box.value = "";
      }
    }
    scrollThread();
  });
})();
