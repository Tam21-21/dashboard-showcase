// The installed app: the dashboard opened from its own icon, in its own window with no address bar.
// Chrome and Edge offer it by firing beforeinstallprompt once per page load, so main.tsx imports this
// file at start-up to catch the event and keep it until the Settings button uses it. Safari has no such
// event: an iPhone adds the app by hand from the Share menu.

export interface InstallPromptEvent extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

// installed: this page runs inside the installed app, or the app was just installed from it.
// ready: the browser offered the install, so the button can open its install window.
// iphone: an iPhone or iPad, where the app is added from the Share menu.
// not-offered: the browser has not offered it on this page (it is installed already, or the browser cannot).
export type InstallState = "installed" | "ready" | "iphone" | "not-offered";

export interface InstallStore {
  subscribe(listener: () => void): () => void;
  state(): InstallState;
  install(): Promise<void>;
}

export function makeInstallStore(win: Window): InstallStore {
  let offer: InstallPromptEvent | null = null;
  let installedHere = false;
  const listeners = new Set<() => void>();
  const changed = () => listeners.forEach((l) => l());
  win.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    offer = e as InstallPromptEvent;
    changed();
  });
  win.addEventListener("appinstalled", () => {
    offer = null;
    installedHere = true;
    changed();
  });
  const nav = win.navigator as Navigator & { standalone?: boolean };
  // navigator.standalone is Safari's flag for a page opened from the home screen.
  const inApp = () =>
    nav.standalone === true ||
    (typeof win.matchMedia === "function" && win.matchMedia("(display-mode: standalone)").matches);
  // An iPad asks for the desktop site, so it reads as a Mac that has a touch screen.
  const apple = /iPhone|iPad|iPod/.test(nav.userAgent) || (/Macintosh/.test(nav.userAgent) && nav.maxTouchPoints > 1);
  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    state() {
      if (installedHere || inApp()) return "installed";
      if (offer) return "ready";
      return apple ? "iphone" : "not-offered";
    },
    async install() {
      if (offer === null) throw new Error("The browser has not offered to install the app on this page.");
      // The browser's install window opens once per offer, so the offer is spent whatever the answer.
      const used = offer;
      offer = null;
      try {
        await used.prompt();
        if ((await used.userChoice).outcome === "accepted") installedHere = true;
      } finally {
        changed();
      }
    },
  };
}

export const installStore = makeInstallStore(window);
