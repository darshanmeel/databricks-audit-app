// Volumes: what each holds (Storage > Volumes) and who reads and writes its files (Governance >
// Volume access). Files and size come from listing each volume in the export; reads and writes
// from the audit log's file-system events.

import React from "react";

import { Api } from "../api";
import { fmtBytes, fmtInt } from "../format";
import { AreaContent, Card, PqgsNote } from "../components/primitives";
import { Donut, HBarList } from "../components/charts";
import { StackRows, wsLabel } from "./tab_workspaces";

function useVolumes(filters: any) {
  const [state, setState] = React.useState<{ phase: string; data: any }>({ phase: "loading", data: null });
  const key = JSON.stringify([filters.workspaceIds, filters.envs, Api.tagParams()]);
  React.useEffect(() => {
    let live = true;
    setState({ phase: "loading", data: null });
    Api.volumes(filters.workspaceIds, filters.envs)
      .then((d: any) => { if (live) setState({ phase: "ready", data: d }); })
      .catch(() => { if (live) setState({ phase: "error", data: null }); });
    return () => { live = false; };
  }, [key]);
  return state;
}

const day = (v: any) => (v ? String(v).slice(0, 10) : "-");
const used = (v: any) => (v.reads || 0) + (v.writes || 0) > 0;
const SHOWN = 50;

// Volumes grouped by their workspace: count, used, size, files and file reads and writes.
function byWorkspace(vols: any[]) {
  const out: Record<string, any> = {};
  for (const v of vols) {
    const key = v.workspace_id || "none";
    const w = out[key] || (out[key] = { workspace_id: key, workspace_name: v.workspace_name || "No workspace", env: v.env,
      volumes: 0, used: 0, size: 0, files: 0, reads: 0, writes: 0 });
    w.volumes += 1;
    w.used += used(v) ? 1 : 0;
    w.size += v.size_in_bytes || 0;
    w.files += v.files || 0;
    w.reads += v.reads || 0;
    w.writes += v.writes || 0;
  }
  return Object.values(out);
}

function notIn(d: any) {
  return !d || d.outcome === "not_assessed" ? "Volumes not in this export: export again (settings table_sizes)." : "No volume in the workspaces shown.";
}

// ─────────── Storage > Volumes ───────────
function VolumesStorageContent({ filters, maxCat, onVerdict }: LooseProps) {
  const st = useVolumes(filters);
  const [all, setAll] = React.useState(false);
  const d = st.phase === "ready" ? st.data : null;
  const vols: any[] = d && d.outcome !== "not_assessed" ? d.volumes || [] : [];
  const unused = vols.filter((v) => !used(v));
  const verdict = vols.length ? `${fmtInt(vols.length)} volumes; ${fmtInt(unused.length)} with no file read or written in the window.` : "Loading volumes...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdict); }, [verdict, onVerdict]);
  if (st.phase === "loading") return <span className="muted">Loading volumes...</span>;
  if (!vols.length) return <span className="muted">{notIn(d)}</span>;
  const top = Math.max(maxCat || 8, 8);
  const sum = (k: string) => vols.reduce((a, v) => a + (v[k] || 0), 0);
  const managed = vols.filter((v) => (v.volume_type || "").toUpperCase() === "MANAGED").length;
  const notListed = vols.filter((v) => v.detail && v.detail !== "listed");
  const truncated = vols.filter((v) => v.truncated);
  const shown = all ? vols : vols.slice(0, SHOWN);
  return (
    <div>
      <div className="pqgs-cards-grid">
        <Card title="Volumes">
          <div className="pqgs-card-value">{fmtInt(vols.length)}</div>
          <PqgsNote facts={[{ label: "Managed", value: fmtInt(managed) }, { label: "External", value: fmtInt(vols.length - managed) }]} />
        </Card>
        <Card title="Files">
          <div className="pqgs-card-value">{fmtInt(sum("files"))}</div>
          <PqgsNote facts={[{ label: "Too big to list", value: fmtInt(truncated.length), detail: "counted up to 50,000 entries" }]} />
        </Card>
        <Card title="Size">
          <div className="pqgs-card-value">{fmtBytes(sum("size_in_bytes"))}</div>
          <PqgsNote facts={[{ label: "From", value: "listing each volume" }]} />
        </Card>
        <Card title="Not used">
          <div className={`pqgs-card-value ${unused.length ? "warn" : "ok"}`}>{fmtInt(unused.length)}</div>
          <PqgsNote facts={[{ label: "Size", value: fmtBytes(unused.reduce((a, v) => a + (v.size_in_bytes || 0), 0)), detail: "no file read or written" }]} />
        </Card>
      </div>
      <div className="grid-2">
        <div className="chart-card">
          <div className="chart-card-title">Biggest volumes</div>
          <HBarList items={[...vols].filter((v) => v.size_in_bytes).slice(0, top).map((v) => ({ name: v.volume_full_name, value: v.size_in_bytes,
            color: used(v) ? undefined : "#c9a227" }))} valueFmt={(v) => fmtBytes(v)} />
          <div className="pqgs-card-note" style={{ marginTop: 6 }}>Gold: no file read or written in the window.</div>
        </div>
        <div className="chart-card">
          <div className="chart-card-title">Volumes by type and use</div>
          <Donut segments={[
            { label: "Managed, used", value: vols.filter((v) => used(v) && (v.volume_type || "").toUpperCase() === "MANAGED").length, color: "var(--c1)" },
            { label: "External, used", value: vols.filter((v) => used(v) && (v.volume_type || "").toUpperCase() !== "MANAGED").length, color: "var(--c2)" },
            { label: "Managed, not used", value: unused.filter((v) => (v.volume_type || "").toUpperCase() === "MANAGED").length, color: "#c9a227" },
            { label: "External, not used", value: unused.filter((v) => (v.volume_type || "").toUpperCase() !== "MANAGED").length, color: "var(--c3)" },
          ].map((s) => ({ ...s, display: fmtInt(s.value) }))} centerLabel={fmtInt(vols.length)} centerSub="volumes" />
        </div>
      </div>
      <div className="grid-2" style={{ marginTop: 14 }}>
        <div className="chart-card">
          <div className="chart-card-title">Volume size by workspace</div>
          <HBarList items={byWorkspace(vols).filter((w: any) => w.size).sort((a: any, b: any) => b.size - a.size).slice(0, top)
            .map((w: any) => ({ name: wsLabel(w), value: w.size }))} valueFmt={(v) => fmtBytes(v)} />
        </div>
        <div className="chart-card">
          <div className="chart-card-title">Volumes used and not used, by workspace</div>
          <StackRows rows={byWorkspace(vols).sort((a: any, b: any) => (b.volumes - b.used) - (a.volumes - a.used)).slice(0, top)
            .map((w: any) => ({ name: wsLabel(w), parts: [
              { label: "used", value: w.used, color: "var(--c1)" }, { label: "not used", value: w.volumes - w.used, color: "#c9a227" }] }))}
            legend={[{ label: "used in the window", color: "var(--c1)" }, { label: "not used", color: "#c9a227" }]} />
        </div>
      </div>
      <div className="chart-card" style={{ marginTop: 14 }}>
        <div className="chart-card-title">Most files</div>
        <HBarList items={[...vols].filter((v) => v.files).sort((a, b) => b.files - a.files).slice(0, top)
          .map((v) => ({ name: v.volume_full_name, value: v.files }))} valueFmt={(v) => `${fmtInt(v)} files`} />
      </div>
      <div className="chart-card" style={{ marginTop: 14 }}>
        <div className="chart-card-title">{`Every volume, biggest first · ${fmtInt(vols.length)}`}</div>
        <div className="metric-note">Its workspace is the one that reads and writes its catalog most. Who used it: <a href={`#tab=governance&w=${filters.window}&subtab=volume_access`}>Governance › Volume access</a>.</div>
        <div className="pqgs-rt">
          <div className="pqgs-rt-head pqgs-rt-cols-vol">
            <span>Volume</span><span>Type</span><span>Workspace · env</span><span style={{ textAlign: "right" }}>Files</span>
            <span style={{ textAlign: "right" }}>Size</span><span style={{ textAlign: "right" }}>Last read</span>
            <span style={{ textAlign: "right" }}>Last write</span><span style={{ textAlign: "right" }}>Users</span>
          </div>
          {shown.map((v) => (
            <div className="pqgs-rt-row pqgs-rt-cols-vol" key={v.volume_full_name}>
              <span className="pqgs-rt-name" title={v.detail}>{v.volume_full_name}</span>
              <span>{v.volume_type ? String(v.volume_type).toLowerCase() : "-"}</span>
              <span className="pqgs-rt-note">{v.workspace_name ? wsLabel(v) : "-"}</span>
              <span className="pqgs-rt-num">{v.files == null ? "-" : `${fmtInt(v.files)}${v.truncated ? "+" : ""}`}</span>
              <span className="pqgs-rt-num">{v.size_in_bytes == null ? "-" : fmtBytes(v.size_in_bytes)}</span>
              <span className="pqgs-rt-num">{day(v.last_read)}</span>
              <span className="pqgs-rt-num">{day(v.last_write)}</span>
              <span className="pqgs-rt-num">{fmtInt(v.users)}</span>
            </div>
          ))}
        </div>
        {vols.length > shown.length && <button type="button" className="link-btn" style={{ marginTop: 8 }} onClick={() => setAll(true)}>{`Show all ${fmtInt(vols.length)}`}</button>}
      </div>
      {notListed.length > 0 && (
        <div className="chart-card" style={{ marginTop: 14 }}>
          <div className="chart-card-title">{`Could not list · ${fmtInt(notListed.length)} volumes`}</div>
          <div className="metric-note">These have no file count or size: listing failed (usually no READ VOLUME for the export identity) or they were used but not listed.</div>
          <div className="pqgs-rt">
            {notListed.slice(0, 100).map((v) => (
              <div className="pqgs-rt-row pqgs-rt-cols-nd" key={v.volume_full_name}>
                <span className="pqgs-rt-name">{v.volume_full_name}</span>
                <span className="pqgs-rt-num">{v.workspace_name || "-"}</span>
                <span className="pqgs-rt-note">{String(v.detail).replace(/^failed: /, "")}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ─────────── Governance > Volume access ───────────
function VolumeUsers({ volume }: { volume: string }) {
  const [st, setSt] = React.useState<{ phase: string; rows: any[] }>({ phase: "loading", rows: [] });
  React.useEffect(() => {
    let live = true;
    Api.volumeUsers(volume).then((d: any) => { if (live) setSt({ phase: "ready", rows: d.rows || [] }); })
      .catch(() => { if (live) setSt({ phase: "error", rows: [] }); });
    return () => { live = false; };
  }, [volume]);
  if (st.phase === "loading") return <div className="muted" style={{ padding: "6px 0 10px 12px" }}>Loading...</div>;
  if (!st.rows.length) return <div className="muted" style={{ padding: "6px 0 10px 12px" }}>Nobody read or wrote a file here in the window.</div>;
  return (
    <div className="pqgs-rt" style={{ margin: "4px 0 12px 12px" }}>
      <div className="pqgs-rt-head pqgs-rt-cols-vu">
        <span>Who</span><span>Kind</span><span style={{ textAlign: "right" }}>Reads</span><span style={{ textAlign: "right" }}>Writes</span>
        <span style={{ textAlign: "right" }}>Last read</span><span style={{ textAlign: "right" }}>Last write</span>
      </div>
      {st.rows.map((r) => (
        <div className="pqgs-rt-row pqgs-rt-cols-vu" key={`${r.identity}-${r.workspace_id}`}>
          <span className="pqgs-rt-name">{r.identity}</span><span>{r.kind}</span>
          <span className="pqgs-rt-num">{fmtInt(r.reads)}</span><span className="pqgs-rt-num">{fmtInt(r.writes)}</span>
          <span className="pqgs-rt-num">{day(r.last_read)}</span><span className="pqgs-rt-num">{day(r.last_write)}</span>
        </div>
      ))}
    </div>
  );
}

function VolumeAccessContent({ filters, maxCat, onVerdict }: LooseProps) {
  const st = useVolumes(filters);
  const [open, setOpen] = React.useState<string | null>(null);
  const d = st.phase === "ready" ? st.data : null;
  const vols: any[] = d && d.outcome !== "not_assessed" ? d.volumes || [] : [];
  const usedVols = vols.filter(used).sort((a, b) => (b.reads + b.writes) - (a.reads + a.writes));
  const people: any[] = (d && d.people) || [];
  const verdict = vols.length ? `${fmtInt(usedVols.length)} of ${fmtInt(vols.length)} volumes used by ${fmtInt(people.length)} people and service principals.` : "Loading volume access...";
  React.useEffect(() => { if (onVerdict) onVerdict(verdict); }, [verdict, onVerdict]);
  if (st.phase === "loading") return <span className="muted">Loading volume access...</span>;
  if (!vols.length) return <span className="muted">{notIn(d)}</span>;
  if (!d.accessed) return <span className="muted">Volume reads and writes not in this export: export again.</span>;
  const top = Math.max(maxCat || 8, 8);
  const persons = people.filter((p) => p.kind === "Person").length;
  return (
    <div>
      <div className="pqgs-cards-grid">
        <Card title="Volumes used">
          <div className="pqgs-card-value">{fmtInt(usedVols.length)}</div>
          <PqgsNote facts={[{ label: "Of", value: fmtInt(vols.length) }, { label: "Not used", value: fmtInt(vols.length - usedVols.length) }]} />
        </Card>
        <Card title="People">
          <div className="pqgs-card-value">{fmtInt(persons)}</div>
          <PqgsNote facts={[{ label: "Service principals", value: fmtInt(people.length - persons) }]} />
        </Card>
        <Card title="File reads">
          <div className="pqgs-card-value">{fmtInt(vols.reduce((a, v) => a + (v.reads || 0), 0))}</div>
          <PqgsNote facts={[{ label: "From", value: "file-system events" }]} />
        </Card>
        <Card title="File writes">
          <div className="pqgs-card-value">{fmtInt(vols.reduce((a, v) => a + (v.writes || 0), 0))}</div>
          <PqgsNote facts={[{ label: "Puts, uploads, deletes", value: "" }]} />
        </Card>
      </div>
      <div className="grid-2">
        <div className="chart-card">
          <div className="chart-card-title">Most-used volumes</div>
          <HBarList items={usedVols.slice(0, top).map((v) => ({ name: v.volume_full_name, value: v.reads + v.writes }))} valueFmt={(v) => `${fmtInt(v)} files`} />
        </div>
        <div className="chart-card">
          <div className="chart-card-title">Who uses volumes most</div>
          <HBarList items={people.slice(0, top).map((p) => ({ name: p.identity, value: p.reads + p.writes,
            title: `${p.identity} · ${p.volumes} volume${p.volumes === 1 ? "" : "s"}`, color: p.kind === "Service principal" ? "var(--c2)" : undefined }))}
            valueFmt={(v) => `${fmtInt(v)} files`} />
          <div className="pqgs-card-note" style={{ marginTop: 6 }}>Orange: a service principal.</div>
        </div>
      </div>
      <div className="chart-card" style={{ marginTop: 14 }}>
        <div className="chart-card-title">File reads and writes, by workspace</div>
        <StackRows rows={byWorkspace(vols).filter((w: any) => w.reads + w.writes).sort((a: any, b: any) => (b.reads + b.writes) - (a.reads + a.writes)).slice(0, top)
          .map((w: any) => ({ name: wsLabel(w), parts: [
            { label: "reads", value: w.reads, color: "var(--c1)" }, { label: "writes", value: w.writes, color: "var(--c2)" }] }))}
          legend={[{ label: "file reads", color: "var(--c1)" }, { label: "file writes", color: "var(--c2)" }]} />
      </div>
      <div className="chart-card" style={{ marginTop: 14 }}>
        <div className="chart-card-title">{`Volumes used, most first · click one for who used it`}</div>
        <div className="pqgs-rt">
          <div className="pqgs-rt-head pqgs-rt-cols-vol">
            <span>Volume</span><span>Type</span><span>Workspace · env</span><span style={{ textAlign: "right" }}>Reads</span>
            <span style={{ textAlign: "right" }}>Writes</span><span style={{ textAlign: "right" }}>Last read</span>
            <span style={{ textAlign: "right" }}>Last write</span><span style={{ textAlign: "right" }}>Users</span>
          </div>
          {usedVols.slice(0, 200).map((v) => (
            <React.Fragment key={v.volume_full_name}>
              <div className="pqgs-rt-row pqgs-rt-cols-vol" style={{ cursor: "pointer" }} onClick={() => setOpen(open === v.volume_full_name ? null : v.volume_full_name)}>
                <span className="pqgs-rt-name">{`${open === v.volume_full_name ? "▾" : "▸"} ${v.volume_full_name}`}</span>
                <span>{v.volume_type ? String(v.volume_type).toLowerCase() : "-"}</span>
                <span className="pqgs-rt-note">{v.workspace_name ? wsLabel(v) : "-"}</span>
                <span className="pqgs-rt-num">{fmtInt(v.reads)}</span><span className="pqgs-rt-num">{fmtInt(v.writes)}</span>
                <span className="pqgs-rt-num">{day(v.last_read)}</span><span className="pqgs-rt-num">{day(v.last_write)}</span>
                <span className="pqgs-rt-num">{fmtInt(v.users)}</span>
              </div>
              {open === v.volume_full_name && <VolumeUsers volume={v.volume_full_name} />}
            </React.Fragment>
          ))}
        </div>
      </div>
      <div className="chart-card" style={{ marginTop: 14 }}>
        <div className="chart-card-title">{`People and service principals using volumes · ${fmtInt(d.people_total || 0)}`}</div>
        <div className="pqgs-rt">
          <div className="pqgs-rt-head pqgs-rt-cols-vp">
            <span>Who</span><span>Kind</span><span style={{ textAlign: "right" }}>Volumes</span>
            <span style={{ textAlign: "right" }}>Reads</span><span style={{ textAlign: "right" }}>Writes</span><span style={{ textAlign: "right" }}>Last access</span>
          </div>
          {people.slice(0, 100).map((p) => (
            <div className="pqgs-rt-row pqgs-rt-cols-vp" key={p.identity}>
              <span className="pqgs-rt-name">{p.identity}</span><span>{p.kind}</span>
              <span className="pqgs-rt-num">{fmtInt(p.volumes)}</span><span className="pqgs-rt-num">{fmtInt(p.reads)}</span>
              <span className="pqgs-rt-num">{fmtInt(p.writes)}</span><span className="pqgs-rt-num">{day(p.last_access)}</span>
            </div>
          ))}
        </div>
        <div className="pqgs-card-note" style={{ marginTop: 8 }}>
          Volume grants: <a href={`#tab=governance&w=${filters.window}&subtab=lineage`}>Governance › Lineage</a> (volumes inventory). Sizes: <a href={`#tab=storage&w=${filters.window}&subtab=volumes`}>Storage › Volumes</a>.
        </div>
      </div>
    </div>
  );
}

AreaContent.register("storage", "volumes", VolumesStorageContent);
AreaContent.register("governance", "volume_access", VolumeAccessContent);
