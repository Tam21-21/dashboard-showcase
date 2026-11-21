// The ring: each row is an arc as wide as its share of the whole. Drawn after the Events tab's
// analyst-actions donut (docs/design/rail_mockup_2026-10-10.html, dlEvents). A press on an arc picks its row;
// when one is picked the others dim.

export interface RingRow {
  key: string;
  value: number;
  colour: string;
  title: string;
}

export function Ring({ label, rows, total, active, onPick, centre }: {
  label: string; rows: RingRow[]; total: number; active: string | null; onPick: (key: string) => void;
  centre?: string; // a text at the middle
}) {
  const outer = 70;
  const width = outer * 0.28; // the inner radius is 0.72 of the outer
  const r = outer - width / 2;
  const circ = 2 * Math.PI * r;
  let used = 0;
  return (
    <svg viewBox="0 0 140 140" className="kit-ring" role="img" aria-label={label}>
      <g transform="rotate(-90 70 70)">
        {rows.map((row) => {
          const len = (row.value / total) * circ;
          const arc = (
            <circle
              key={row.key}
              data-arc={row.key}
              cx={70} cy={70} r={r}
              fill="none"
              stroke={row.colour}
              strokeWidth={width}
              strokeDasharray={`${len} ${circ - len}`}
              strokeDashoffset={-used}
              opacity={active === null || active === row.key ? 1 : 0.35}
              onClick={() => onPick(row.key)}
            >
              <title>{row.title}</title>
            </circle>
          );
          used += len;
          return arc;
        })}
      </g>
      {centre !== undefined && (
        <text x={70} y={76} textAnchor="middle" fontSize={18} fill="#eef2f7">{centre}</text>
      )}
    </svg>
  );
}
