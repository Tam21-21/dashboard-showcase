// The phone's side of the evening notification: no React here. A browser that can receive it has
// window.pushManager and Notification (WebKit, "Meet Declarative Web Push": no service worker is needed).
import { api } from "../../../lib/api";

export type PushStateWord = "off" | "on" | "refused";

type Sub = {
  endpoint: string;
  toJSON(): { endpoint?: string; keys?: { p256dh?: string; auth?: string } };
  unsubscribe(): Promise<boolean>;
};
type Manager = {
  getSubscription(): Promise<Sub | null>;
  subscribe(options: { userVisibleOnly: boolean; applicationServerKey: Uint8Array }): Promise<Sub>;
};
type Asker = { permission: string; requestPermission(): Promise<string> };

const manager = () => (window as unknown as { pushManager: Manager }).pushManager;
const asker = () => (window as unknown as { Notification: Asker }).Notification;

export function canPush(): boolean {
  return "pushManager" in window && "Notification" in window;
}

/** Base64url text to bytes (the server's public key is 65 bytes). */
export function keyBytes(text: string): Uint8Array {
  const padded = text.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(text.length / 4) * 4, "=");
  const raw = atob(padded);
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function body(sub: Sub) {
  const j = sub.toJSON();
  const p256dh = j.keys?.p256dh, auth = j.keys?.auth;
  if (j.endpoint === undefined || p256dh === undefined || auth === undefined) {
    throw new Error("The phone gave a subscription without its keys.");
  }
  return { endpoint: j.endpoint, keys: { p256dh, auth } };
}

/** Refused when the phone said no, On when it holds a subscription (told to the server again when the server lost it), else Off. */
export async function readState(): Promise<PushStateWord> {
  if (asker().permission === "denied") return "refused";
  const sub = await manager().getSubscription();
  if (sub === null) return "off";
  const known = await api.life.pushState({ endpoint: sub.endpoint });
  if (!known.known) await api.life.pushSubscribe(body(sub));
  return "on";
}

/** The press: the server's key, the phone's question, the subscription, then the server is told. */
export async function turnOn(): Promise<PushStateWord> {
  const { key } = await api.life.pushKey();
  const answer = await asker().requestPermission();
  if (answer === "denied") return "refused";
  if (answer !== "granted") return "off";
  const sub = await manager().subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
  await api.life.pushSubscribe(body(sub));
  return "on";
}

export async function turnOff(): Promise<PushStateWord> {
  const sub = await manager().getSubscription();
  if (sub !== null) {
    await sub.unsubscribe();
    await api.life.pushUnsubscribe({ endpoint: sub.endpoint });
  }
  return "off";
}
