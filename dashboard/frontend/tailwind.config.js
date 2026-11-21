/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg:      "rgb(var(--c-bg)      / <alpha-value>)",
        surface: "rgb(var(--c-surface) / <alpha-value>)",
        surface2:"rgb(var(--c-surface2)/ <alpha-value>)",
        border:  "rgb(var(--c-border)  / <alpha-value>)",
        hair:    "var(--c-hair)",
        muted:   "rgb(var(--c-muted)   / <alpha-value>)",
        subtle:  "rgb(var(--c-subtle)  / <alpha-value>)",
        fg:      "rgb(var(--c-fg)      / <alpha-value>)",
        hi:      "rgb(var(--c-hi)      / <alpha-value>)",
        ghost:   "rgb(var(--c-ghost)   / <alpha-value>)",
        // Brand / semantic — one system. See redesign brief §5 for the meaning map.
        accent: "#FF4D57",    // brand · action · LIVE · danger · negative / down
        accentDim: "#B3373F", // a dim of accent (hover / pressed / dim text)
        pos: "#54E08A",       // positive · up · ready · synced-ok · healthy
        neg: "#FF4D57",       // negative · down — shares the one red with accent
        warn: "#F6B545",      // warning · pending · mid-range
        info: "#4FE0D2",      // data · streaming · informational · sparklines (cyan)
        cat: "#A68BFF",       // categorical tags · event-type datasets (violet)
        // TERM A4 design-kit colors (D1, DECISIONS #134) — a separate additive namespace,
        // resolved from the --term-* channel triples in index.css so opacity modifiers work
        // (rgb(var(--term-*) / <alpha-value>)). Tailwind v3 mechanism — see §2.1 / the v3 pin
        // below. Byte-exact A4 palette; the legacy names above stay untouched.
        "term-bg":      "rgb(var(--term-bg)      / <alpha-value>)",
        "term-bg2":     "rgb(var(--term-bg2)     / <alpha-value>)",
        "term-panel":   "rgb(var(--term-panel)   / <alpha-value>)",
        "term-panel2":  "rgb(var(--term-panel2)  / <alpha-value>)",
        "term-line":    "rgb(var(--term-line)    / <alpha-value>)",
        "term-line2":   "rgb(var(--term-line2)   / <alpha-value>)",
        "term-txt":     "rgb(var(--term-txt)     / <alpha-value>)",
        "term-txt2":    "rgb(var(--term-txt2)    / <alpha-value>)",
        "term-dim":     "rgb(var(--term-dim)     / <alpha-value>)",
        "term-faint":   "rgb(var(--term-faint)   / <alpha-value>)",
        "term-red":     "rgb(var(--term-red)     / <alpha-value>)",
        "term-redtint": "rgb(var(--term-redtint) / <alpha-value>)",
        "term-redsoft": "rgb(var(--term-redsoft) / <alpha-value>)",
        "term-grn":     "rgb(var(--term-grn)     / <alpha-value>)",
        "term-cyn":     "rgb(var(--term-cyn)     / <alpha-value>)",
        "term-vio":     "rgb(var(--term-vio)     / <alpha-value>)",
        "term-eq":      "rgb(var(--term-eq)      / <alpha-value>)",
        "term-amb":     "rgb(var(--term-amb)     / <alpha-value>)",
        "term-evt":     "rgb(var(--term-evt)     / <alpha-value>)",
        "term-fnd":     "rgb(var(--term-fnd)     / <alpha-value>)",
      },
      fontFamily: {
        display: ["'Space Grotesk'", "ui-sans-serif", "system-ui", "sans-serif"],
        sans: ["'Space Grotesk'", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["'IBM Plex Mono'", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
        // TERM A4 kit type token: the mockup's all-mono stack (A4_terminal_max.html:12). Adopted
        // by pages in D2, not forced on legacy pages in D1.
        "term-mono": ["ui-monospace", "'JetBrains Mono'", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      fontSize: {
        "eyebrow": ["11px", { lineHeight: "1", letterSpacing: "0.14em" }],
        "hero-sm": ["52px", { lineHeight: "0.95", letterSpacing: "-0.02em" }],
        "hero": ["88px", { lineHeight: "0.92", letterSpacing: "-0.03em" }],
        "hero-lg": ["112px", { lineHeight: "0.9", letterSpacing: "-0.035em" }],
      },
      letterSpacing: {
        eyebrow: "0.14em",
        wordmark: "0.2em",
      },
      borderRadius: {
        xs: "2px",         // small elements (chips, inputs, badges) — kept sharp (D-M5)
        panel: "13px",     // W2/D-M5: the v2 panel radius, applied to every Panel/Card
      },
      backgroundImage: {
        // W2/D-M5 global tokens, values lifted from dashboard_redesign_mockup_v2.html.
        "btn-primary": "linear-gradient(180deg, #ff5b64, #ec3641)",   // v2 primary button (:128-134)
        "band": "linear-gradient(180deg, rgba(255,255,255,0.025), rgba(0,0,0,0.10))",  // v2 table band (:166-180)
      },
      keyframes: {
        "pulse-red": {
          "0%, 100%": { boxShadow: "0 0 0 0 rgba(255,77,87,0.55)" },
          "70%": { boxShadow: "0 0 0 10px rgba(255,77,87,0)" },
        },
        "pulse-ring": {
          "0%": { transform: "scale(0.9)", opacity: "0.8" },
          "100%": { transform: "scale(2.4)", opacity: "0" },
        },
        "card-in": {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "sheen": {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(100%)" },
        },
        // TERM A4: the terminal block cursor blink (mockup `@keyframes bl`, A4_terminal_max.html:35).
        // Namespaced `term-blink` so it does not collide with any legacy animation.
        "term-blink": {
          "50%": { opacity: "0.25" },
        },
      },
      animation: {
        "pulse-red": "pulse-red 2s cubic-bezier(0.4,0,0.6,1) infinite",
        "pulse-ring": "pulse-ring 2s cubic-bezier(0.25,0.8,0.25,1) infinite",
        "card-in": "card-in 400ms cubic-bezier(0.16,1,0.3,1) both",
        "sheen": "sheen 1400ms ease-out",
        // TERM A4: the work-surface title-band cursor (D2.1b §1.1). steps(2) = the hard on/off blink.
        "term-blink": "term-blink 1.1s steps(2, end) infinite",
      },
      transitionTimingFunction: {
        "luxe": "cubic-bezier(0.16, 1, 0.3, 1)",
      },
      transitionDuration: {
        "micro": "140ms", // hover, color — brief §6
        "base": "200ms",  // most transitions
      },
      boxShadow: {
        panel: "var(--shadow-panel)",   // elevated surface
        raised: "var(--shadow-raised)", // hover / raised
        // W2/D-M5: the v2 primary-button glow + inset top highlight (:128-134) and the deep
        // table-band drop + inset highlight (:166-180).
        "btn-primary": "0 8px 24px -8px rgba(255,77,87,0.7), inset 0 1px 0 rgba(255,255,255,0.22)",
        "band": "0 24px 60px -24px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.05)",
      },
    },
  },
  plugins: [],
};
