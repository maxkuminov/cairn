/* Cairn — Dashboard page. Exports to window: DashboardPage */

function StatTile({ icon, label, value, sub, color }) {
  return (
    <Card pad={18} style={{ flex: 1, minWidth: 0 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 9, color: "var(--text-3)", marginBottom: 12 }}>
        <Icon name={icon} size={16} stroke={1.9} style={{ flexShrink: 0 }} />
        <span style={{ fontSize: 12, fontWeight: 600, letterSpacing: "0.02em", whiteSpace: "nowrap" }}>{label}</span>
      </div>
      <div style={{ fontFamily: "var(--font-head)", fontSize: 30, fontWeight: 700, letterSpacing: "-0.02em", color: color || "var(--text)", lineHeight: 1 }}>{value}</div>
      {sub && <div style={{ fontSize: 12, color: "var(--text-3)", marginTop: 7 }}>{sub}</div>}
    </Card>
  );
}

function CorpusCard({ c, go }) {
  const m = CORPUS_STATUS[c.status];
  const segs = [
    { label: "OK", value: c.counts.ok, color: "var(--ok)" },
    { label: "New", value: c.counts.new, color: "var(--accent)" },
    { label: "Modified", value: c.counts.modified, color: "var(--warn)" },
    { label: "Missing", value: c.counts.missing, color: "var(--danger)" },
  ];
  const issues = c.counts.modified + c.counts.missing;
  return (
    <Card hover onClick={() => go("corpusDetail", c.id)} pad={0} style={{ overflow: "hidden" }}>
      <div style={{ padding: "18px 20px 16px" }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12 }}>
          <div style={{ minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 9, marginBottom: 4 }}>
              <Icon name="folder" size={17} style={{ color: "var(--text-3)", flexShrink: 0 }} />
              <h3 style={{ fontSize: 16.5, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{c.name}</h3>
            </div>
            <div className="mono" style={{ fontSize: 11.5, color: "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 320 }}>{c.root}</div>
          </div>
          <Pill icon={c.status === "ok" ? "checkCircle" : "alert"} color={m.color} soft={c.status === "ok" ? "var(--ok-soft)" : (c.status === "alert" ? "var(--danger-soft)" : "var(--warn-soft)")}>{m.label}</Pill>
        </div>

        <div style={{ display: "flex", gap: 18, margin: "16px 0 12px", flexWrap: "wrap" }}>
          <Meta label="Files" value={c.files.toLocaleString()} />
          <Meta label="Size" value={c.size} />
          <Meta label="Owner" value={c.owner} />
          <Meta label="Last scan" value={c.lastScan} />
        </div>

        <SegBar segments={segs} />
        <div style={{ display: "flex", gap: 14, marginTop: 9, flexWrap: "wrap", fontSize: 12 }}>
          {issues > 0
            ? <>
                {c.counts.missing > 0 && <LegendStat color="var(--danger)" label={`${c.counts.missing} missing`} />}
                {c.counts.modified > 0 && <LegendStat color="var(--warn)" label={`${c.counts.modified} modified`} />}
                {c.counts.new > 0 && <LegendStat color="var(--accent)" label={`${c.counts.new} new`} />}
              </>
            : <LegendStat color="var(--ok)" label="All files verified" />}
        </div>
      </div>

      {/* OTS footer */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "11px 20px", background: "var(--surface-2)", borderTop: "1px solid var(--border)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          {c.ots === "none"
            ? <span style={{ fontSize: 12, color: "var(--text-3)", display: "inline-flex", alignItems: "center", gap: 6 }}><Icon name="minusCircle" size={14} /> Tripwire only — no notarization</span>
            : <>
                <Icon name="bitcoin" size={15} style={{ color: "var(--accent)" }} />
                <span style={{ fontSize: 12, color: "var(--text-2)" }}>
                  <strong style={{ color: "var(--text)" }}>{c.ots_counts.complete.toLocaleString()}</strong> anchored
                  {c.ots_counts.pending > 0 && <span style={{ color: "var(--warn)" }}> · {c.ots_counts.pending} pending</span>}
                </span>
              </>}
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          {c.alerts.map(a => <Icon key={a} name={a === "signal" ? "signal" : a === "email" ? "mail" : "webhook"} size={14} style={{ color: "var(--text-3)" }} />)}
        </div>
      </div>
    </Card>
  );
}
function Meta({ label, value }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-3)", marginBottom: 2, whiteSpace: "nowrap" }}>{label}</div>
      <div style={{ fontSize: 13.5, fontWeight: 600, whiteSpace: "nowrap" }}>{value}</div>
    </div>
  );
}
function LegendStat({ color, label }) {
  return <span style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--text-2)", fontWeight: 500 }}><Dot color={color} size={7} />{label}</span>;
}

function EventRow({ e, corpora, onAck }) {
  const m = STATUS_META[e.kind];
  const cName = corpora.find(c => c.id === e.corpus)?.name || e.corpus;
  return (
    <div style={{ display: "flex", gap: 11, padding: "12px 0", borderBottom: "1px solid var(--border)" }}>
      <div style={{ width: 30, height: 30, borderRadius: 999, background: m.soft, color: m.color, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
        <Icon name={m.icon} size={15} stroke={2.1} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 7, marginBottom: 2 }}>
          <span style={{ fontSize: 12.5, fontWeight: 600, color: m.color }}>{m.label}</span>
          {e.stamped && <Pill icon="bitcoin" color="var(--accent)" soft="var(--accent-soft)" size="sm">stamped</Pill>}
          <span style={{ fontSize: 11.5, color: "var(--text-3)", marginLeft: "auto" }}>{e.at}</span>
        </div>
        <div className="mono" style={{ fontSize: 11.5, color: "var(--text-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{e.relpath}</div>
        <div style={{ fontSize: 11, color: "var(--text-3)", marginTop: 3 }}>{cName}</div>
        {!e.ack && (
          <div style={{ marginTop: 8 }}>
            <Button size="sm" variant={e.kind === "missing" ? "danger" : "subtle"} icon="check" onClick={() => onAck(e.id)}>Acknowledge</Button>
          </div>
        )}
      </div>
    </div>
  );
}

function DashboardPage({ go, onAck, acked }) {
  const { CORPORA, EVENTS, HEARTBEAT } = window.CAIRN;
  const totalFiles = CORPORA.reduce((s, c) => s + c.files, 0);
  const totalMissing = CORPORA.reduce((s, c) => s + c.counts.missing, 0);
  const totalModified = CORPORA.reduce((s, c) => s + c.counts.modified, 0);
  const totalAnchored = CORPORA.reduce((s, c) => s + c.ots_counts.complete, 0);
  const pending = CORPORA.reduce((s, c) => s + c.ots_counts.pending, 0);
  const openEvents = EVENTS.filter(e => !e.ack && !acked.includes(e.id));

  return (
    <div>
      <PageHeader title="Dashboard" subtitle="Integrity status across every corpus you monitor.">
        <Button variant="subtle" icon="refresh">Run scan now</Button>
      </PageHeader>

      {/* Summary tiles */}
      <div style={{ display: "flex", gap: 16, marginBottom: 22, flexWrap: "wrap" }}>
        <StatTile icon="file" label="Files monitored" value={totalFiles.toLocaleString()} sub={`${CORPORA.length} corpora · 1.77 TiB`} />
        <StatTile icon="alert" label="Open issues" value={totalMissing + totalModified} sub={`${totalMissing} missing · ${totalModified} modified`} color={(totalMissing + totalModified) > 0 ? "var(--danger)" : "var(--ok)"} />
        <StatTile icon="bitcoin" label="Proofs anchored" value={totalAnchored.toLocaleString()} sub={pending > 0 ? `${pending} pending confirmation` : "all confirmed"} />
        <StatTile icon="pulse" label="Last activity" value="4 min" sub="Tax practice files scan" />
      </div>

      {/* Main grid */}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1.55fr) minmax(0, 1fr)", gap: 22, alignItems: "start" }}>
        {/* Corpus cards */}
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <SectionLabel>Corpora</SectionLabel>
          {CORPORA.map(c => <CorpusCard key={c.id} c={c} go={go} />)}
        </div>

        {/* Right rail */}
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          {/* Events */}
          <Card pad={18}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 4 }}>
              <h3 style={{ fontSize: 14.5 }}>Recent events</h3>
              {openEvents.length > 0 && <Pill color="var(--danger)" soft="var(--danger-soft)" size="sm">{openEvents.length} need action</Pill>}
            </div>
            <div>
              {EVENTS.map(e => <EventRow key={e.id} e={{ ...e, ack: e.ack || acked.includes(e.id) }} corpora={CORPORA} onAck={onAck} />)}
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}

function SectionLabel({ children }) {
  return <div style={{ fontSize: 11.5, fontWeight: 700, letterSpacing: "0.07em", textTransform: "uppercase", color: "var(--text-3)" }}>{children}</div>;
}

Object.assign(window, { DashboardPage, SectionLabel });
