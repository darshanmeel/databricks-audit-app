// Rows of bands (auto-stop settings, start times...): one bar per band, scaled to the largest,
// with an optional part inside it (e.g. idle cost inside total cost), and the band's own text.
import React from "react";

export interface BandRow {
  label: string;
  sub: string;
  total: number;
  part?: number | null;
  text: React.ReactNode;
  title?: string;
}

export function BandRows({ bands }: { bands: BandRow[] }) {
  const max = Math.max(...bands.map((b) => b.total), 1);
  return (
    <React.Fragment>
      {bands.map((b) => (
        <div className="cj-band-row" key={b.label}>
          <div className="cj-band-label"><b>{b.label}</b><span>{b.sub}</span></div>
          <div className="cj-band-track" title={b.title}>
            {b.total > 0 && (
              <div className={`cj-band-total${b.part == null ? " solid" : ""}`} style={{ width: `${(b.total / max) * 100}%` }}>
                {b.part != null && <div className="cj-band-part" style={{ width: `${(b.part / b.total) * 100}%` }}></div>}
              </div>
            )}
          </div>
          <div className="cj-band-val">{b.text}</div>
        </div>
      ))}
    </React.Fragment>
  );
}
