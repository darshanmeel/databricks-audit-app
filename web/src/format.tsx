// Scalar formatters. Anything that is not really a number prints "-", never a made-up "$0" or "NaN";
// fmtCell's money branch has its own words for a missing or zero cell.

import { fmtChangePct } from "./components/hooks";
import { Tip } from "./components/primitives";
import type React from "react";
import type { Row } from "./types";

// Numbers are formatted in a FIXED locale, never the viewer's. toLocaleString(undefined, ...)
// takes the browser's locale, so on a European machine 33096 ms rendered as "33.096" -- which
// reads as 33 milliseconds for 61 queries (observed 2026-09-22). In an audit tool the same
// figure must not change meaning between two people's screens, and the "." / "," ambiguity is a
// real misreading risk on money. Consistency beats localisation here.
export const LOCALE = "en-US";

function isMissing(v: unknown): boolean {
  return v === null || v === undefined || (typeof v === "number" && Number.isNaN(v));
}

// True for a finite number or a string that parses to one. Column kinds are guessed from the name, so a text or
// timestamp column can reach a magnitude formatter; this gate keeps it from printing "NaN".
export function isNumericValue(v: unknown): boolean {
  if (typeof v === "number") return Number.isFinite(v);
  if (typeof v === "string") {
    const s = v.trim();
    if (s === "") return false;
    return Number.isFinite(Number(s));
  }
  return false;
}

// Plain money formatting -- missing -> "-", a real zero -> "$0", same as every other magnitude
// formatter below. Used throughout the tiles/charts for a SUM/aggregate figure, where a zero
// already has an established meaning from the outcome branch that gated the call (e.g. "no rows
// matched this window") rather than "this one row's price was $0" -- fmtCell's own "money" branch
// is where that per-row free-vs-unpriced distinction actually belongs (see its comment).
export function fmtMoney(value: unknown, decimals = 0): string {
  if (!isNumericValue(value)) return "-";
  const n = Number(value);
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toLocaleString(LOCALE, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

// "list price (effective)", or "what-if: list price (effective), -X%" with a discount set; X to one decimal, the
// same rounding as the server's est_label.
export function estLabel(discountPct: unknown): string {
  const d = Number(discountPct);
  if (!d) return "list price (effective)";
  const pctOff = Math.round(d * 1000) / 10;
  return `what-if: list price (effective), -${pctOff}%`;
}

export function fmtDbu(value: unknown, decimals = 1): string {
  if (!isNumericValue(value)) return "-";
  return `${Number(value).toLocaleString(LOCALE, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })} DBU`;
}

export function fmtPct(value: unknown, decimals = 1): string {
  if (!isNumericValue(value)) return "-";
  return `${Number(value).toLocaleString(LOCALE, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}%`;
}

// A change the way people read it: "6.2×" once it has doubled, "new" or "from $5" when there was
// almost nothing before, otherwise a signed percent -- never "+85,205%".
export function fmtChange(current: unknown, previous: unknown, money = true): string {
  if (!isNumericValue(current) || !isNumericValue(previous)) return "-";
  const cur = Number(current), prev = Number(previous);
  const tiny = money ? 50 : 1;
  if (prev < tiny) {
    if (cur <= 0) return "-";
    return prev > 0 && money ? `from ${fmtMoney(prev, 0)}` : "new";
  }
  return fmtChangePct(((cur - prev) / prev) * 100);
}

// The same change as a phrase against `what` ("the previous period"): "6.2× vs ...", "up from $30
// in ...", "none in ..."; null when there is nothing to compare.
export function fmtChangeVs(current: unknown, previous: unknown, what: string): string | null {
  const t = fmtChange(current, previous);
  if (t === "-") return null;
  if (t === "new") return `none in ${what}`;
  if (t.indexOf("from ") === 0) return `${Number(current) >= Number(previous) ? "up" : "down"} ${t} in ${what}`;
  return `${t} vs ${what}`;
}


function fmtHours(value: unknown, decimals = 1): string {
  if (!isNumericValue(value)) return "-";
  return `${Number(value).toLocaleString(LOCALE, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })} h`;
}

export function fmtGb(value: unknown, decimals = 2, fromBytes = false): string {
  if (!isNumericValue(value)) return "-";
  const v = fromBytes ? Number(value) / 1e9 : Number(value);
  return `${v.toLocaleString(LOCALE, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })} GB`;
}

export function fmtInt(value: unknown): string {
  if (!isNumericValue(value)) return "-";
  return Math.round(Number(value)).toLocaleString(LOCALE);
}

// "$19k" for a badge/pill where a full "$18,955" would not fit -- one decimal only when it changes
// which thousand a reader would round to (e.g. "$1.2k", never "$1.0k").
export function fmtMoneyShort(value: unknown): string {
  if (!isNumericValue(value)) return "-";
  const n = Number(value);
  const sign = n < 0 ? "-" : "";
  const abs = Math.abs(n);
  if (abs < 1000) return `${sign}$${Math.round(abs)}`;
  const thousands = abs / 1000;
  const decimals = thousands < 10 && Math.round(thousands * 10) % 10 !== 0 ? 1 : 0;
  return `${sign}$${thousands.toFixed(decimals)}k`;
}

// "45 s" / "1 m 08 s" / "2 h 14 m" from a duration in seconds -- ms callers pass seconds = ms/1000.
// Never a bare "134 min" once past an hour, and the seconds component is zero-padded so "1 m 8 s"
// never misreads as eighteen seconds.
export function fmtDuration(seconds: unknown): string {
  if (!isNumericValue(seconds)) return "-";
  // Under a second reads in ms, never "0 s".
  if (Number(seconds) > 0 && Number(seconds) < 1) return `${Math.max(1, Math.round(Number(seconds) * 1000))} ms`;
  const total = Math.round(Number(seconds));
  if (total < 60) return `${total} s`;
  if (total < 3600) {
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m} m ${String(s).padStart(2, "0")} s`;
  }
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  return `${h} h ${m} m`;
}

export function fmtDurationMs(ms: unknown): string {
  return isNumericValue(ms) ? fmtDuration(Number(ms) / 1000) : "-";
}

// "Sep 15" for chart axis ticks; prose uses fmtDayMonth ("15 Sep"). UTC, so the day never shifts.
export function fmtDayShort(dateStr: unknown): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(dateStr || ""));
  if (!m) return String(dateStr || "");
  const d = new Date(`${m[1]}-${m[2]}-${m[3]}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return String(dateStr || "");
  return d.toLocaleDateString(LOCALE, { month: "short", day: "numeric", timeZone: "UTC" });
}

// "21 Sep" -- day before month, no year: the app's own plain-date convention (UI presentation
// rules), for a card with no room for fmtDate's year or fmtDayShort's month-first "Sep 21" chart-
// axis order. UTC-parsed like every other calendar-day formatter here; the input may carry a
// trailing time (a timestamp string) -- only the leading YYYY-MM-DD is read.
export function fmtDayMonth(dateStr: unknown): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(dateStr || ""));
  if (!m) return String(dateStr || "");
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${Number(m[3])} ${names[Number(m[2]) - 1] || m[2]}`;
}

// Adaptive byte size (B/KB/MB/GB/TB) -- fmtGb's fixed "0.0 GB" reads as a verified zero for a
// table or a growth delta that is genuinely just a few hundred bytes; this instead picks the
// smallest unit that keeps the number readable, so a 1,000-byte figure reads "1,000 B", not "0.0
// GB". Always takes a raw byte count (every caller here already has one), unlike fmtGb's own
// optional fromBytes flag.
export function fmtBytes(value: unknown, decimals = 1): string {
  if (!isNumericValue(value)) return "-";
  const v = Number(value);
  const abs = Math.abs(v);
  const units: [string, number][] = [["TB", 1e12], ["GB", 1e9], ["MB", 1e6], ["KB", 1e3]];
  for (const [unit, size] of units) {
    if (abs >= size) {
      return `${(v / size).toLocaleString(LOCALE, { minimumFractionDigits: decimals, maximumFractionDigits: decimals })} ${unit}`;
    }
  }
  return `${Math.round(v).toLocaleString(LOCALE)} B`;
}

// Kinds are guessed from column names, so a text or date column can carry a magnitude kind: show an ISO date or
// timestamp short, other text as is; null for anything else.
const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_DATETIME_RE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/;
export function fmtDate(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const s = value.trim();
  if (ISO_DATE_RE.test(s)) {
    const d = new Date(`${s}T00:00:00Z`);
    if (Number.isNaN(d.getTime())) return null;
    return d.toLocaleDateString(LOCALE, { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
  }
  if (ISO_DATETIME_RE.test(s)) {
    // review round 2: a timestamp with NO offset must read as the exact wall time the server
    // wrote (app/api/service.py's _json_scalar isoformats a naive pandas Timestamp -- every
    // timestamp today), never the viewer's own time zone -- `new Date(s)` on a bare (no "Z"/
    // offset) string is parsed as LOCAL time by spec, so the same cell read an hour off across
    // a DST boundary, or a different clock time entirely, depending on the browser's zone. Force
    // UTC parsing (append "Z" when there is no offset already) and always RENDER in UTC too, so
    // the reading matches what a neighbouring UTC-rendered column already shows. A timestamp that
    // DOES carry an explicit offset is shown in UTC with a "UTC" label, so it is still one fixed
    // reading, not two different ones depending on who wrote the offset.
    const iso = s.replace(" ", "T");
    const hasOffset = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
    // F2 (U-UI-04, N-N10): a bare (no offset) timestamp sitting at exactly midnight is, in this
    // app, always a calendar-DAY column (usage_date and friends) that a pandas Timestamp's own
    // isoformat() re-serialized with a spurious "T00:00:00" -- not a real point-in-time event this
    // snapshot captured. Reading "Sep 18, 2026, 12:00 AM" next to every day in a table invents
    // precision nothing here ever measured; read as a bare date instead, the same as the
    // ISO_DATE_RE branch above. An explicit offset (a genuinely captured instant, e.g. the as-of
    // banner) or a non-midnight time is untouched and still gets its full date+time reading below.
    if (!hasOffset && /T00:00:00(\.0+)?$/.test(iso)) {
      const dayOnly = new Date(`${iso.slice(0, 10)}T00:00:00Z`);
      if (Number.isNaN(dayOnly.getTime())) return null;
      return dayOnly.toLocaleDateString(LOCALE, { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });
    }
    const d = new Date(hasOffset ? iso : `${iso}Z`);
    if (Number.isNaN(d.getTime())) return null;
    const text = d.toLocaleString(LOCALE, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
    return hasOffset ? `${text} UTC` : text;
  }
  return null;
}

// A cell whose kind expected a number but whose actual value is not one: shown as-is, as a
// formatted date when it parses as one (fmtDate), otherwise as plain text -- never handed to a
// magnitude formatter (which would either mangle it or, before this fix, print the literal text
// "NaN" in front of the unit -- the review finding this item fixes).
function textOrDateCell(value: unknown): string {
  const asDate = fmtDate(value);
  if (asDate) return asDate;
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

// A missing (null/undefined/NaN) cell -- an em dash with a hover tip, never the bare "-" the
// magnitude formatters use elsewhere (those stay plain strings on purpose: dozens of callers
// interpolate them straight into template literals, see the file-top note, so they can never
// return an element) and never a raw "undefined"/"null" a bare String(value) could otherwise
// print. Reuses the one Tip hover component every other explanation in this app already renders
// through (primitives.tsx) instead of a second, copy-pasted tooltip.
function missingCell(): React.ReactElement {
  return <Tip text="no value"><span className="muted">{"—"}</span></Tip>;
}

// Kinds with a unit suffix; each is gated on isNumericValue first.
const NUMERIC_CELL_KINDS = new Set(["dbu", "pct", "hours", "gb", "gb_bytes"]);

// Money cells: "(free)" or "unpriced" only from a row's price_basis or a FREE_USAGE sku_name; many queries write
// an unpriced cost as 0 or a not-assessed one as NULL, so those keep plain fmtMoney text.
// A non-numeric value falls to textOrDateCell; a missing one gets a tip, not a bare "-".
export function fmtCell(value: unknown, kind: string | null, row?: Row | null): React.ReactNode {
  // review round 2: an empty/whitespace-only string is "no value" exactly like null/undefined/NaN
  // -- the item promises a missing value is never a blank, unexplained cell, and finding_detail.tsx's
  // DataTable already treats "" as "no value" (its own hasValue check) before ever calling fmtCell,
  // so leaving "" out of this gate left THAT caller's own promise broken.
  const blank = isMissing(value) || (typeof value === "string" && value.trim() === "");
  if (kind === "money") {
    const priceBasis = row && row.price_basis;
    const isFree = !!row && (priceBasis === "free" || (priceBasis == null && /FREE_USAGE/i.test(String(row.sku_name || ""))));
    if (isFree) return "$0 (free)";
    if (blank) {
      if (priceBasis === "no_size") return "not priced (no size on record)";
      if (row && (priceBasis === "unpriced" || Object.prototype.hasOwnProperty.call(row, "sku_name"))) return "unpriced";
      return missingCell();
    }
    if (!isNumericValue(value)) return textOrDateCell(value);
    return fmtMoney(value, 2);
  }
  if (blank) return missingCell();
  // Databricks' own redaction marker, in words.
  if (value === "__REDACTED__") return "redacted by Databricks";
  if (kind && NUMERIC_CELL_KINDS.has(kind) && !isNumericValue(value)) return textOrDateCell(value);
  switch (kind) {
    case "dbu": return fmtDbu(value);
    case "pct": return fmtPct(value);
    case "hours": return fmtHours(value);
    case "gb": return fmtGb(value, 2, false);
    case "gb_bytes": return fmtGb(value, 2, true);
    case "id": return String(value);
    default:
      if (typeof value === "number") {
        return Number.isInteger(value) ? fmtInt(value) : value.toLocaleString(LOCALE, { maximumFractionDigits: 3 });
      }
      if (typeof value === "boolean") return value ? "true" : "false";
      // F2 (U-UI-04): a column classify_columns could not name a kind for (usage_date and
      // friends -- no magnitude pattern matches "date") used to print its raw ISO text verbatim.
      // Try a date reading first, so it shows "Sep 18, 2026" like every other date in this app.
      return fmtDate(value) || String(value);
  }
}

// "25 Sep 19:06" (direct export's real as-of instant) or "25 Sep" (a snapshot's as-of date, no
// time captured) -- day + short month first, 24h clock, always UTC (the value this app is given).
export function fmtSnapshotWhen(value: unknown): string | null {
  if (!value) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(String(value).trim());
  if (!m) return null;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4] || 0), Number(m[5] || 0)));
  if (Number.isNaN(d.getTime())) return null;
  const datePart = d.toLocaleDateString(LOCALE, { day: "numeric", month: "short", timeZone: "UTC" });
  if (m[4] == null) return datePart;
  return `${datePart} ${d.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" })}`;
}
