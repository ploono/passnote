// passnote landing page: plays the demo storyboard and powers the copy buttons.
// Without JavaScript, or with reduced motion, the page shows the whole story at once.
(function () {
  "use strict";

  // How long each beat plays before the next one (ms). After beat 3 the stage rests on the still,
  // loops, and stops for good after MAX_LOOPS rounds; the one button pauses it or replays it.
  var BEAT_MS = [0, 1700, 2000, 2100];
  var REST_MS = 2600;
  var MAX_LOOPS = 3;

  function each(list, fn) { Array.prototype.forEach.call(list, fn); }

  function setupDemo(demo) {
    var stage = demo.querySelector(".stage");
    var button = demo.querySelector("[data-action=replay]");
    if (!stage || !button || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var captions = demo.querySelectorAll(".captions li");
    var timer = null;
    var step = 0;
    var loops = 0;
    var playing = false;

    function show(n) {
      step = n;
      stage.setAttribute("data-step", String(n));
      each(captions, function (li, i) {
        var on = i === n - 1;
        li.classList.toggle("current", on);
        if (on) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
      });
    }

    function setPlaying(on) {
      playing = on;
      button.classList.toggle("is-playing", on);
      stage.classList.toggle("is-playing", on);
      button.setAttribute("aria-label", on ? "Pause the demo" : "Replay the demo");
    }

    function next() {
      if (step === 3) {
        loops += 1;
        if (loops >= MAX_LOOPS) { show(0); stop(); return; }
      }
      var n = step >= 3 ? 0 : step + 1;
      show(n);
      timer = setTimeout(next, n === 0 ? REST_MS : BEAT_MS[n]);
    }

    function stop() { clearTimeout(timer); timer = null; setPlaying(false); }

    function play() {
      clearTimeout(timer);
      stage.classList.remove("is-paused");
      loops = 0;
      show(0);
      setPlaying(true);
      timer = setTimeout(next, 350);
    }

    function pause() { stop(); stage.classList.add("is-paused"); }

    button.addEventListener("click", function () { if (playing) pause(); else play(); });
    document.addEventListener("visibilitychange", function () { if (document.hidden && playing) pause(); });

    demo.classList.add("is-js");
    button.hidden = false;
    show(0);
    requestAnimationFrame(function () { requestAnimationFrame(function () { demo.classList.add("is-ready"); }); });

    var started = false;
    function begin() { if (!started && !document.hidden) { started = true; play(); } }
    if (!("IntersectionObserver" in window)) { begin(); return; }
    var seen = new IntersectionObserver(function (entries) {
      if (entries[entries.length - 1].isIntersecting) { begin(); if (started) seen.disconnect(); }
    }, { threshold: 0.4 });
    seen.observe(stage);
  }

  function selectText(el) {
    var range = document.createRange();
    range.selectNodeContents(el);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  function say(button, text) {
    var label = button.getAttribute("data-label") || button.textContent;
    var status = document.getElementById("copy-status");
    button.setAttribute("data-label", label);
    button.textContent = text;
    if (status) status.textContent = text;
    setTimeout(function () { button.textContent = label; }, 1800);
  }

  // Copies the text of the element named by data-copy. Where the Clipboard API is missing or
  // refused, it selects the text instead and says how to copy it.
  function copyText(button) {
    var target = document.getElementById(button.getAttribute("data-copy"));
    if (!target) return;
    var text = target.textContent;
    var fallback = function () { selectText(target); say(button, "Selected: press Ctrl+C or ⌘C"); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { say(button, "Copied"); }, fallback);
    } else {
      fallback();
    }
  }

  each(document.querySelectorAll(".demo"), setupDemo);
  each(document.querySelectorAll("button.copy[data-copy]"), function (button) {
    if (!document.getElementById(button.getAttribute("data-copy"))) return;
    button.hidden = false;
    button.addEventListener("click", function () { copyText(button); });
  });
})();
