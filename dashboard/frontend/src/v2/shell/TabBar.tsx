import { APPS } from "./sections";
import { Icon } from "./icons";

interface Props {
  openApp: string | undefined;
  go: (to: string) => void;
}

// The phone's bottom bar: one button per app, the open one in the bright ink (the mockup's phone rail).
export default function TabBar({ openApp, go }: Props) {
  return (
    <nav className="tabbar" aria-label="Apps">
      {APPS.map((a) => (
        <button
          type="button"
          key={a.id}
          className={a.id === openApp ? "on" : undefined}
          aria-label={a.name}
          onClick={() => {
            // A second press on the open app opens its list of pages when they do not fit the strip (PhoneStrip takes it).
            if (a.id === openApp && !window.dispatchEvent(new Event("v2-pages", { cancelable: true }))) return;
            go(`/app/${a.id}/${a.sections[0].id}`);
          }}
        >
          <Icon name={a.icon} />
          <span>{a.name}</span>
        </button>
      ))}
    </nav>
  );
}
