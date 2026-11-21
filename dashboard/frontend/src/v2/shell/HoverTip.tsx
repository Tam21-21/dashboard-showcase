import { useEffect, useRef, useState } from "react";

// The mockup's hover box (K.tip): every hover text on a page shows in one dark box beside the pointer instead of the
// browser's own grey label. Pages keep writing a plain title; while the pointer is on the element its title moves to
// data-tip (so the browser shows nothing) and comes back when the pointer leaves, so the page and its tests still see it.
// A title of several lines shows its first line in bold, as the mockup's boxes open with a name.
type Tip = { lines: string[]; x: number; y: number; name: boolean };

export default function HoverTip() {
  const [tip, setTip] = useState<Tip | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const [at, setAt] = useState<{ left: number; top: number } | null>(null);

  useEffect(() => {
    if (typeof window.matchMedia !== "function" || !window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;
    let held: HTMLElement | null = null;
    const give = () => {
      if (held && !held.hasAttribute("title") && held.dataset.tip !== undefined) held.setAttribute("title", held.dataset.tip);
      if (held) delete held.dataset.tip;
      held = null;
    };
    const over = (e: PointerEvent) => {
      const t = e.target instanceof Element ? (e.target.closest(".v2 [title], .v2 [data-tip]") as HTMLElement | null) : null;
      if (t && t === held) return;
      give();
      const text = t?.getAttribute("title")?.trim();
      if (!t || !text || t.closest(".tabbar")) { setTip(null); return; }
      held = t;
      t.dataset.tip = t.getAttribute("title") ?? "";
      t.removeAttribute("title");
      // A name in the rail is written bold, as the mockup's rail does.
      setTip({ lines: text.split("\n").filter((l) => l.trim() !== ""), x: e.clientX, y: e.clientY, name: !!t.closest(".rail") });
    };
    const move = (e: PointerEvent) => { if (held) setTip((p) => (p ? { ...p, x: e.clientX, y: e.clientY } : p)); };
    const out = (e: PointerEvent) => {
      if (!held || (e.relatedTarget instanceof Node && held.contains(e.relatedTarget))) return;
      give();
      setTip(null);
    };
    const away = () => { give(); setTip(null); };
    document.addEventListener("pointerover", over);
    document.addEventListener("pointermove", move, { passive: true });
    document.addEventListener("pointerout", out);
    document.addEventListener("pointerdown", away);
    window.addEventListener("scroll", away, true);
    return () => {
      give();
      document.removeEventListener("pointerover", over);
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerout", out);
      document.removeEventListener("pointerdown", away);
      window.removeEventListener("scroll", away, true);
    };
  }, []);

  // Beside the pointer, 16 pixels off, and flipped to the other side near an edge (the mockup's rule).
  useEffect(() => {
    const el = box.current;
    if (!tip || !el) { setAt(null); return; }
    const w = el.offsetWidth, h = el.offsetHeight;
    const left = tip.x + 16 + w > window.innerWidth - 8 ? tip.x - 16 - w : tip.x + 16;
    const top = tip.y + 16 + h > window.innerHeight - 8 ? tip.y - 16 - h : tip.y + 16;
    setAt({ left: Math.max(8, left), top: Math.max(8, top) });
  }, [tip]);

  if (!tip) return null;
  const [head, ...rest] = tip.lines;
  return (
    <div className={`htip${at ? " on" : ""}`} aria-hidden="true" style={at ? { transform: `translate(${at.left}px, ${at.top}px)` } : undefined}>
      <div className="htip-in" ref={box}>
        {rest.length || tip.name ? <><b>{head}</b>{rest.map((l, i) => <div key={i}>{l}</div>)}</> : head}
      </div>
    </div>
  );
}
