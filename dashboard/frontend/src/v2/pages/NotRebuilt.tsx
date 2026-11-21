import { PageHeader } from "../kit/parts";

interface Props {
  crumb: string;
  title: string;
}

export default function NotRebuilt({ crumb, title }: Props) {
  return (
    <div className="v2-page">
      <PageHeader crumb={crumb} title={title} />
      <div className="cd">
        <p className="nr-line">This page is not rebuilt yet.</p>
      </div>
    </div>
  );
}
