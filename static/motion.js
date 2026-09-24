(function () {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  function reveal(list) {
    var nodes = typeof list === "string" ? document.querySelectorAll(list) : list;
    Array.prototype.forEach.call(nodes, function (el) {
      ["transform", "translate", "rotate", "scale", "opacity", "visibility"].forEach(function (prop) {
        el.style.removeProperty(prop);
      });
    });
  }

  function enter(extra) {
    var vars = {
      autoAlpha: 0,
      y: 16,
      duration: 0.48,
      ease: "power2.out",
      clearProps: "transform,opacity,visibility",
      onInterrupt: function () { reveal(this.targets()); }
    };
    if (extra) Object.assign(vars, extra);
    return vars;
  }

  var sequenced = ".site-header, .intro .eyebrow, .intro h1, .intro .lede, .studio-note, .process li, .request-form, .receipt .eyebrow, .receipt .badge, .receipt h1, .session-summary > div";

  try {
    if (document.querySelector(".intro")) {
      gsap.timeline()
        .from(".site-header", enter())
        .from(".intro .eyebrow", enter())
        .from(".intro h1", enter())
        .from(".intro .lede", enter())
        .from(".studio-note", enter())
        .from(".process li", enter({ stagger: 0.07 }))
        .from(".request-form", enter());
    } else if (document.querySelector(".receipt")) {
      var timeline = gsap.timeline();
      timeline.from(".receipt .eyebrow", enter())
        .from(".receipt .badge", enter())
        .from(".receipt h1", enter());
      document.querySelectorAll(".session-summary > div").forEach(function (row) {
        timeline.from(row, enter());
      });
    }
  } catch (err) {
    reveal(sequenced);
  }

  if (!document.querySelector(".desk-heading") || !document.querySelector(".request-row")) return;
  try {
    anime({
      targets: ".request-row",
      translateY: [14, 0],
      opacity: [0, 1],
      delay: anime.stagger(70),
      duration: 420,
      easing: "easeOutCubic",
      complete: function (anim) {
        anim.animatables.forEach(function (item) { reveal([item.target]); });
      }
    });
  } catch (err) {
    reveal(".request-row");
  }
})();
