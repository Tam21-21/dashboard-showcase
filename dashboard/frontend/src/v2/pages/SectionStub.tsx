import NotRebuilt from "./NotRebuilt";
import type { PageProps } from "../shell/sections";

// The page of every section that has no page of its own yet.
export default function SectionStub({ app, section }: PageProps) {
  return <NotRebuilt crumb={app.name} title={section.label} />;
}
