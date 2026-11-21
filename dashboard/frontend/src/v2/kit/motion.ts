import { useEffect, type RefObject } from "react";

// Motion in the new look (Tamer 2026-11-14: "smooth animations that won't slow down the dashboard"): a part comes in by
// moving (transform) or fading (opacity) only, once, when it first shows; nothing repaints frame by frame. "x" opens a
// chart left to right (time runs that way): the box slides in from the left while everything inside slides back by as
// much, so the drawing stays still and only the window onto it grows. "y" grows a bar chart from its base. "fade" rises in.
// Motion off (the root has .still) or reduced motion in the system: the part is simply there.
export type Unveil = "x" | "y" | "fade";

function still(el: Element): boolean {
  const reduced = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  return reduced || el.closest(".v2.still") !== null;
}

export function unveil(el: HTMLElement, how: Unveil = "x", { dur = 1100, delay = 0 }: { dur?: number; delay?: number } = {}): void {
  if (still(el) || typeof el.animate !== "function") return;
  if (how === "y") {
    el.style.transformOrigin = "50% 100%";
    el.animate([{ scale: "1 0" }, { scale: "1 1" }], { duration: dur, delay, easing: "cubic-bezier(.16,1,.3,1)", fill: "backwards" });
    return;
  }
  if (how === "fade") {
    el.animate([{ opacity: 0, translate: "0 14px" }, { opacity: 1, translate: "0 0" }], { duration: 700, delay, easing: "cubic-bezier(.16,1,.3,1)", fill: "backwards" });
    return;
  }
  const w = el.getBoundingClientRect().width;
  const ease = "cubic-bezier(.65,0,.35,1)";
  el.classList.add("nl-rv");
  const a = el.animate([{ translate: `${-w}px 0` }, { translate: "0 0" }], { duration: dur, delay, easing: ease, fill: "backwards" });
  for (const c of Array.from(el.children)) c.animate([{ translate: `${w}px 0` }, { translate: "0 0" }], { duration: dur, delay, easing: ease, fill: "backwards" });
  const done = () => el.classList.remove("nl-rv");
  a.onfinish = done;
  a.oncancel = done;
}

/** Unveils the element once, the first time it scrolls into view. */
export function useUnveil(ref: RefObject<HTMLElement | null>, how: Unveil = "x", opts?: { dur?: number; delay?: number }): void {
  const dur = opts?.dur;
  const delay = opts?.delay;
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver((es) => {
      if (es.some((e) => e.isIntersecting)) { io.disconnect(); unveil(el, how, { dur, delay }); }
    }, { rootMargin: "0px 0px -8% 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, [ref, how, dur, delay]);
}
