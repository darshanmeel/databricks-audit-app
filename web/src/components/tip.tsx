import React from "react";
import { createPortal } from "react-dom";

export interface TipPoint { tone?: "bad" | "good"; label?: string; text: string }

// Hover/focus popover with short bullet points; the native title cannot render bullets.
export function Tip({ points, children, label }: { points: TipPoint[]; children: React.ReactNode; label?: string }) {
  const [pos, setPos] = React.useState<{ left: number; top: number } | null>(null);
  const ref = React.useRef<HTMLSpanElement>(null);
  const show = () => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const w = Math.min(320, window.innerWidth - 24);
    setPos({ left: Math.max(12, Math.min(r.left, window.innerWidth - w - 12)), top: r.bottom + 6 });
  };
  const hide = () => setPos(null);
  return (
    <span ref={ref} className="tip" tabIndex={0} aria-label={label || points.map((p) => (p.label ? `${p.label} ` : "") + p.text).join(". ")}
      onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
      {children}
      {/* Portal: a transformed/blurred top bar would otherwise become the fixed box's frame. */}
      {pos && createPortal(
        <span className="tip-pop" role="tooltip" style={{ left: pos.left, top: pos.top }}>
          <ul className="pop-points">
            {points.map((p, i) => (
              <li key={i} className={p.tone === "bad" ? "pt-bad" : p.tone === "good" ? "pt-good" : undefined}>
                {p.label && <b>{p.label} </b>}{p.text}
              </li>
            ))}
          </ul>
        </span>,
        document.body,
      )}
    </span>
  );
}
