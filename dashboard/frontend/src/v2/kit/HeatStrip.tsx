// The heat strip: one cell per year, the darker the cell the more is held, with the first and last year
// under it. With no cells it shows a dash and the reason on hover.

export interface HeatCell {
  key: number;
  title: string;
  opacity: number;
}

export function HeatStrip({ cells, colour, first, last, why }: {
  cells: HeatCell[] | null; colour: string; first: number | string | null; last: number | string | null; why: string;
}) {
  if (cells === null) {
    return <span className="ds-cov ds-covnone"><span title={why}>-</span></span>;
  }
  return (
    <>
      <span className="ds-cov">
        {cells.map((c) => <i key={c.key} title={c.title} style={{ background: colour, opacity: c.opacity }} />)}
      </span>
      <span className="ds-covl num"><span>{first}</span><span>{last}</span></span>
    </>
  );
}
