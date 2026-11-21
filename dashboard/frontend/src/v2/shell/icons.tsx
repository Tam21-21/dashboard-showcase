// Plain line icons on a 24px grid, as the mockup draws them (trading_lab_new_ui.html, the shell's P).
// "history" is new: a clock with a back arrow, in the same stroke style.

const PATHS = {
  home: <path d="M4 10.5 12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-6h-5v6H5a1 1 0 0 1-1-1z" />,
  lab: <><path d="M9.5 3.5h5" /><path d="M10.5 3.5v5.2L5.2 17.6A1.6 1.6 0 0 0 6.6 20h10.8a1.6 1.6 0 0 0 1.4-2.4L13.5 8.7V3.5" /><path d="M7.6 14.5h8.8" /></>,
  cal: <><rect x="4" y="5.5" width="16" height="14.5" rx="2.5" /><path d="M4 10.5h16M8.5 3.5v4M15.5 3.5v4" /></>,
  trade: <><path d="M7.5 4v3.5M7.5 15.5V20M16.5 4v5.5M16.5 17.5V20" /><rect x="5.25" y="7.5" width="4.5" height="8" rx="1.2" /><rect x="14.25" y="9.5" width="4.5" height="8" rx="1.2" /></>,
  paper: <><path d="M6.5 4h8l4 4v10.5A1.5 1.5 0 0 1 17 20H6.5A1.5 1.5 0 0 1 5 18.5v-13A1.5 1.5 0 0 1 6.5 4z" /><path d="M14.5 4v4h4" /><path d="M8.5 15.5l2.4-2.6 2 1.6 2.6-3.2" /></>,
  shield: <><path d="M12 3.5l7 2.5v5.5c0 4.3-2.9 7.6-7 9-4.1-1.4-7-4.7-7-9V6l7-2.5z" /><path d="M9 12l2.2 2.2 3.8-3.9" /></>,
  pie: <><path d="M11.5 4.5a8 8 0 1 0 8 8h-8v-8z" /><path d="M14.5 3.6a6.6 6.6 0 0 1 5.9 5.9h-5.9V3.6z" /></>,
  coins: <><ellipse cx="12" cy="7" rx="6.5" ry="2.8" /><path d="M5.5 7v5c0 1.5 2.9 2.8 6.5 2.8s6.5-1.3 6.5-2.8V7" /><path d="M5.5 12v5c0 1.5 2.9 2.8 6.5 2.8s6.5-1.3 6.5-2.8v-5" /></>,
  plan: <><path d="M5 6.5h14M5 12h14M5 17.5h14" /><path d="M5 6.5h8M5 12h11M5 17.5h5" strokeWidth="3.2" /></>,
  wallet: <><path d="M4 8a2.5 2.5 0 0 1 2.5-2.5H17a1 1 0 0 1 1 1V8" /><path d="M4 8v9a2.5 2.5 0 0 0 2.5 2.5H18a2 2 0 0 0 2-2V10a2 2 0 0 0-2-2H4z" /><path d="M15.5 13.8h1" /></>,
  overview: <><rect x="4" y="4" width="7" height="9.5" rx="1.6" /><rect x="13" y="4" width="7" height="5.5" rx="1.6" /><rect x="13" y="11.5" width="7" height="8.5" rx="1.6" /><rect x="4" y="15.5" width="7" height="4.5" rx="1.6" /></>,
  run: <path d="M3.5 12.5h4l2.5-6.5 4 12 2.5-5.5h4" />,
  data: <><ellipse cx="12" cy="6.5" rx="7.5" ry="2.8" /><path d="M4.5 6.5v5.5c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8V6.5" /><path d="M4.5 12v5.5c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8V12" /></>,
  gen: <><path d="M11 4.5l1.7 4.3L17 10.5l-4.3 1.7L11 16.5l-1.7-4.3L5 10.5l4.3-1.7z" /><path d="M18 15v4M16 17h4" /></>,
  strat: <><circle cx="12" cy="12" r="8" /><circle cx="12" cy="12" r="4" /><path d="M12 12h.01" /></>,
  runs: <><circle cx="12" cy="12" r="8" /><path d="M10.2 8.8v6.4l5.3-3.2z" /></>,
  judge: <><path d="M12 4.5V20M8 20h8M5.5 7.5h13" /><path d="M5.5 7.5 3 13.5a2.7 2.7 0 0 0 5 0zM18.5 7.5 16 13.5a2.7 2.7 0 0 0 5 0z" /></>,
  chains: <><path d="M10.5 13.5a4 4 0 0 0 5.7 0l2.6-2.6a4 4 0 0 0-5.7-5.7l-1 1" /><path d="M13.5 10.5a4 4 0 0 0-5.7 0l-2.6 2.6a4 4 0 0 0 5.7 5.7l1-1" /></>,
  fwd: <path d="M4.5 7v10l7-5zM12.5 7v10l7-5z" />,
  ops: <><rect x="4" y="4.5" width="16" height="6.5" rx="1.8" /><rect x="4" y="13" width="16" height="6.5" rx="1.8" /><path d="M7.5 7.75h.01M7.5 16.25h.01M11 7.75h5.5M11 16.25h5.5" /></>,
  gear: <><path d="M4 7.5h9M17.5 7.5H20M4 16.5h2.5M11 16.5h9" /><circle cx="15.2" cy="7.5" r="2.2" /><circle cx="8.8" cy="16.5" r="2.2" /></>,
  panel: <><rect x="3.5" y="4.5" width="17" height="15" rx="3" /><path d="M9.5 4.5v15" /></>,
  down: <path d="M7 10l5 5 5-5" />,
  history: <><path d="M3.5 12a8.5 8.5 0 1 1 2.5 6" /><path d="M3 6.5V12h5.5" /><path d="M12 7.5V12l3 2" /></>,
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name }: { name: IconName }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      {PATHS[name]}
    </svg>
  );
}
