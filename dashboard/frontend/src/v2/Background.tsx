// The Aurora background of the mockup (lab-r2.css, part 1): three soft lights on a layer of their own, painted once.
// The colours of the lights come from the open page (look.css, .v2[data-page]); "Moving" slides them with transforms only.
// The other backgrounds (Glass, Glow, Dots, Charcoal, Black) are plain CSS on .v2[data-bg], so they draw nothing here.
import { useSettings, type BgId } from "./settings";

// bg names the background; without it the viewer's choice is read from the settings. app is accepted for the pages that
// still pass it and is not needed: the lights follow the page through the data-page attribute on .v2.
export function Background({ bg }: { bg?: BgId; app?: string }) {
  const { settings } = useSettings();
  if ((bg ?? settings.bg) !== "aurora") return null;
  return (
    <div className="aur" aria-hidden="true">
      <i className="l1" />
      <i className="l2" />
      <i className="l3" />
    </div>
  );
}
