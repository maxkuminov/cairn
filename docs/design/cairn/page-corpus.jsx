/* Cairn — Corpus detail + Add/Edit corpus. Exports: CorpusDetailPage, AddCorpusPage */

function CorpusDetailPage({ corpusId, go, acked, onAccept, accepted }) {
  const { CORPORA, FILES } = window.CAIRN;
  const c = CORPORA.find(x => x.id === corpusId) || CORPORA[0];
  const files = FILES[c.id] || [];
  const [filter, setFilter] = React.useState("all");
  const [query, setQuery] = React.useState("");
  const m = CORPUS_STATUS[c.status];

  const q = query.trim().toLowerCase();
  const filtered = files.filter(f => {
    if (q && !f.relpath.toLowerCase().includes(q)) return false;
    if (filter === "all") return true;
    if (filter === "issues") return f.status === "modified" || f.status === "missing";
    return f.status === filter;
  });
  const issues = c.counts.modified + c.counts.missing;

  return (
    <div>
      <PageHeader
        title={c.name}
        subtitle={null}
        back={{ label: "All corpora", onClick: () => go("dashboard") }}>
        <Button variant="default" icon="settings" onClick={() => go("addCorpus", c.id)}>Edit</Button>
        <Button variant="subtle" icon="refresh">Scan now</Button>
        {issues > 0 && <Button variant="primary" icon="check" onClick={() => onAccept(c.id)}>Accept changes</Button>}
      </PageHeader>

      {/* Meta strip */}
      <Card pad={0} style={{ marginBottom: 20, overflow: "hidden" }}>
        <div style={{ display: "flex", flexWrap: "wrap" }}>
          <MetaCell label="Status" wide>
            <Pill icon={c.status === "ok" ? "checkCircle" : "alert"} color={m.color} soft={c.status === "ok" ? "var(--ok-soft)" : (c.status === "alert" ? "var(--danger-soft)" : "var(--warn-soft)")}>{m.label}</Pill>
          </MetaCell>
          <MetaCell label="Root path"><span className="mono" style={{ fontSize: 12 }}>{c.root}</span></MetaCell>
          <MetaCell label="Policy"><span style={{ textTransform: "uppercase", fontSize: 12.5, fontWeight: 600, letterSpacing: "0.03em" }}>{c.mode}</span></MetaCell>
          <MetaCell label="Notarization">
            {c.ots === "none" ? "Tripwire only" : c.ots === "perfile" ? `Per-file${c.otsNote ? " · " + c.otsNote : ""}` : "Manifest"}
          </MetaCell>
          <MetaCell label="Scan cadence">{c.cadence}</MetaCell>
          <MetaCell label="Owner">{c.owner}</MetaCell>
        </div>
      </Card>

      {/* Stat row */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 14, marginBottom: 22 }}>
        <MiniStat label="Total files" value={c.files.toLocaleString()} sub={c.size} icon="file" />
        <MiniStat label="Verified OK" value={c.counts.ok.toLocaleString()} color="var(--ok)" icon="checkCircle" />
        <MiniStat label="Changed / missing" value={issues} color={issues ? "var(--danger)" : "var(--text-3)"} icon="alert" sub={`${c.counts.missing} missing · ${c.counts.modified} modified`} />
        {c.ots !== "none"
          ? <MiniStat label="Anchored to chain" value={c.ots_counts.complete.toLocaleString()} color="var(--accent)" icon="bitcoin" sub={c.ots_counts.pending ? `${c.ots_counts.pending} pending` : "all confirmed"} />
          : <MiniStat label="Last scan" value={c.lastScan} icon="clock" sub={c.lastScanFull} />}
      </div>

      {/* File table */}
      <Card pad={0}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "14px 18px", borderBottom: "1px solid var(--border)", flexWrap: "wrap" }}>
          <h3 style={{ fontSize: 14.5 }}>Files</h3>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flex: 1, minWidth: 180, maxWidth: 320, background: "var(--surface-2)", border: "1px solid var(--border)", borderRadius: "var(--radius)", padding: "7px 11px" }}>
            <Icon name="search" size={15} style={{ color: "var(--text-3)" }} />
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={`Search ${c.files.toLocaleString()} files by path…`}
              style={{ border: "none", background: "transparent", outline: "none", color: "var(--text)", fontSize: 12.5, width: "100%" }} />
            {query && <button onClick={() => setQuery("")} style={{ background: "none", border: "none", color: "var(--text-3)", display: "flex" }}><Icon name="x" size={14} /></button>}
          </div>
          <div style={{ display: "flex", gap: 4, background: "var(--surface-2)", padding: 4, borderRadius: "var(--radius)", marginLeft: "auto" }}>
            {[["all", "All"], ["issues", "Issues"], ["new", "New"], ["ok", "OK"]].map(([k, lbl]) => (
              <button key={k} onClick={() => setFilter(k)}
                style={{ padding: "5px 12px", borderRadius: "var(--radius-sm)", border: "none", fontSize: 12.5, fontWeight: 600,
                  background: filter === k ? "var(--surface)" : "transparent",
                  color: filter === k ? "var(--text)" : "var(--text-3)",
                  boxShadow: filter === k ? "var(--shadow)" : "none" }}>{lbl}</button>
            ))}
          </div>
        </div>

        {/* header row */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 90px 130px 140px 110px", gap: 12, padding: "10px 18px", fontSize: 11, fontWeight: 700, letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--text-3)", borderBottom: "1px solid var(--border)" }}>
          <span>Path</span><span>Size</span><span>Status</span>{c.ots !== "none" ? <span>Notarization</span> : <span></span>}<span style={{ textAlign: "right" }}>Last checked</span>
        </div>

        <div>
          {filtered.map((f, i) => {
            const isAccepted = accepted.includes(c.id) && (f.status === "modified" || f.status === "new");
            const status = isAccepted ? "ok" : f.status;
            return (
              <div key={i} style={{ display: "grid", gridTemplateColumns: "1fr 90px 130px 140px 110px", gap: 12, padding: "12px 18px", alignItems: "center", borderBottom: i < filtered.length - 1 ? "1px solid var(--border)" : "none", transition: "background 0.1s" }}
                onMouseEnter={(e) => e.currentTarget.style.background = "var(--surface-2)"}
                onMouseLeave={(e) => e.currentTarget.style.background = "transparent"}>
                <div style={{ display: "flex", alignItems: "center", gap: 9, minWidth: 0 }}>
                  <Icon name="file" size={15} style={{ color: "var(--text-3)", flexShrink: 0 }} />
                  <span className="mono" style={{ fontSize: 12, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.relpath}</span>
                </div>
                <span style={{ fontSize: 12.5, color: "var(--text-2)" }}>{f.size}</span>
                <StatusBadge status={status} size="sm" />
                {c.ots !== "none"
                  ? (f.ots === "complete"
                      ? <button onClick={() => go("verify", { filename: f.relpath.split("/").pop(), relpath: f.relpath, corpus: c.name })}
                          title="Verify this proof"
                          style={{ background: "none", border: "none", padding: 0, display: "inline-flex", cursor: "pointer", borderRadius: 999 }}
                          onMouseEnter={(e) => e.currentTarget.style.opacity = 0.7}
                          onMouseLeave={(e) => e.currentTarget.style.opacity = 1}>
                          <OtsBadge state={f.ots} size="sm" />
                        </button>
                      : <OtsBadge state={f.ots} size="sm" />)
                  : <span />}
                <span style={{ fontSize: 12, color: "var(--text-3)", textAlign: "right" }}>{f.checked}</span>
              </div>
            );
          })}
          {filtered.length === 0 && <div style={{ padding: 40, textAlign: "center", color: "var(--text-3)", fontSize: 13 }}>No files match this filter.</div>}
        </div>
        <div style={{ padding: "11px 18px", borderTop: "1px solid var(--border)", fontSize: 12, color: "var(--text-3)", display: "flex", justifyContent: "space-between" }}>
          <span>{q || filter !== "all" ? `${filtered.length} matching` : `Showing ${filtered.length} of ${c.files.toLocaleString()} files`}</span>
          <span>Sampled · full list paginated in production</span>
        </div>
      </Card>
    </div>
  );
}

function MetaCell({ label, children, wide }) {
  return (
    <div style={{ padding: "14px 20px", borderRight: "1px solid var(--border)", flex: wide ? "0 0 auto" : 1, minWidth: 0 }}>
      <div style={{ fontSize: 11, color: "var(--text-3)", marginBottom: 5, letterSpacing: "0.03em" }}>{label}</div>
      <div style={{ fontSize: 13.5, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{children}</div>
    </div>
  );
}
function MiniStat({ label, value, sub, color, icon }) {
  return (
    <Card pad={16}>
      <div style={{ display: "flex", alignItems: "center", gap: 7, color: "var(--text-3)", marginBottom: 9 }}>
        <Icon name={icon} size={15} stroke={1.9} /><span style={{ fontSize: 11.5, fontWeight: 600 }}>{label}</span>
      </div>
      <div style={{ fontFamily: "var(--font-head)", fontSize: 23, fontWeight: 700, color: color || "var(--text)", lineHeight: 1 }}>{value}</div>
      {sub && <div style={{ fontSize: 11.5, color: "var(--text-3)", marginTop: 6 }}>{sub}</div>}
    </Card>
  );
}

/* ---------------- Add / Edit corpus ---------------- */
function AddCorpusPage({ go, editId }) {
  const { CORPORA } = window.CAIRN;
  const existing = editId ? CORPORA.find(c => c.id === editId) : null;
  const [name, setName] = React.useState(existing?.name || "");
  const [root, setRoot] = React.useState(existing?.root || "");
  const [mode, setMode] = React.useState(existing?.mode || "worm");
  const [ots, setOts] = React.useState(existing?.ots || "perfile");
  const [cadence, setCadence] = React.useState(existing ? String(existing.cadenceSeconds) : "86400");
  const [excludes, setExcludes] = React.useState((existing?.excludes || ["**/.thumbnails/**", "**/*.tmp"]).join("\n"));
  const [alerts, setAlerts] = React.useState(existing?.alerts || ["email"]);
  const base = "/srv";
  const rootValid = !root || root.startsWith(base) || root.startsWith("/mnt/");

  const toggleAlert = (a) => setAlerts(p => p.includes(a) ? p.filter(x => x !== a) : [...p, a]);

  return (
    <div style={{ maxWidth: 760 }}>
      <PageHeader
        title={existing ? `Edit ${existing.name}` : "Add a corpus"}
        subtitle="A corpus is a folder Cairn watches under its own policy. Roots are limited to your admin-provisioned, read-only mount."
        back={{ label: existing ? "Back to corpus" : "Dashboard", onClick: () => go(existing ? "corpusDetail" : "dashboard", existing?.id) }} />

      <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
        <Card pad={22}>
          <SectionLabel>Identity &amp; location</SectionLabel>
          <div style={{ display: "grid", gap: 18, marginTop: 16 }}>
            <Field label="Corpus name" required>
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Family photos" />
            </Field>
            <Field label="Root path" required hint={<span>Must resolve under your mounted base <span className="mono" style={{ color: "var(--text-2)" }}>{base}</span>. Mounted read-only — Cairn can never modify what it watches.</span>}>
              <div style={{ position: "relative" }}>
                <Input mono value={root} onChange={(e) => setRoot(e.target.value)} placeholder={`${base}/My Folder`} style={{ borderColor: rootValid ? undefined : "var(--danger)", paddingRight: 38 }} />
                {root && (
                  <span style={{ position: "absolute", right: 12, top: "50%", transform: "translateY(-50%)", color: rootValid ? "var(--ok)" : "var(--danger)" }}>
                    <Icon name={rootValid ? "checkCircle" : "x"} size={17} />
                  </span>
                )}
              </div>
              {!rootValid && <div style={{ fontSize: 12, color: "var(--danger)", marginTop: 6 }}>Path is outside your allowed base — rejected.</div>}
            </Field>
          </div>
        </Card>

        <Card pad={22}>
          <SectionLabel>Integrity policy</SectionLabel>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, marginTop: 16 }}>
            <Field label="Change policy" hint="WORM treats any change as suspicious. Churn expects edits and only flags missing files.">
              <SegChoice value={mode} onChange={setMode} options={[["worm", "WORM"], ["churn", "Churn"]]} />
            </Field>
            <Field label="Scan cadence" hint="Stagger large corpora — you can't full-rescan 186k files every 5 minutes.">
              <Select value={cadence} onChange={(e) => setCadence(e.target.value)}>
                <option value="300">Every 5 minutes</option>
                <option value="900">Every 15 minutes</option>
                <option value="3600">Hourly</option>
                <option value="86400">Nightly</option>
                <option value="604800">Weekly</option>
              </Select>
            </Field>
          </div>
        </Card>

        <Card pad={22}>
          <SectionLabel>Notarization (OpenTimestamps)</SectionLabel>
          <div style={{ marginTop: 16 }}>
            <Field label="Anchoring mode" hint="Per-file gives each file a standalone, portable proof anchored to Bitcoin. Tripwire skips notarization — best for sets that never change.">
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {[
                  ["perfile", "Per-file", "Each file independently anchored to Bitcoin — a portable proof you can hand off.", "bitcoin"],
                  ["none", "Tripwire only", "Detect changes, but don't notarize. Best for sets that never change, like ROMs.", "verify"],
                ].map(([k, t, d, ic]) => (
                  <button key={k} type="button" onClick={() => setOts(k)}
                    style={{ display: "flex", gap: 12, alignItems: "flex-start", textAlign: "left", padding: "13px 15px", borderRadius: "var(--radius)",
                      border: `1.5px solid ${ots === k ? "var(--accent)" : "var(--border)"}`,
                      background: ots === k ? "var(--accent-soft)" : "var(--surface)", transition: "all 0.13s" }}>
                    <Icon name={ic} size={18} style={{ color: ots === k ? "var(--accent)" : "var(--text-3)", marginTop: 1 }} />
                    <div>
                      <div style={{ fontSize: 13.5, fontWeight: 600, color: ots === k ? "var(--accent)" : "var(--text)" }}>{t}</div>
                      <div style={{ fontSize: 12.5, color: "var(--text-2)", marginTop: 2 }}>{d}</div>
                    </div>
                    <span style={{ marginLeft: "auto", color: ots === k ? "var(--accent)" : "var(--border-strong)" }}><Icon name={ots === k ? "checkCircle" : "minusCircle"} size={18} /></span>
                  </button>
                ))}
              </div>
            </Field>
          </div>
        </Card>

        <Card pad={22}>
          <SectionLabel>Exclusions &amp; alerts</SectionLabel>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, marginTop: 16 }}>
            <Field label="Exclude globs" hint="One pattern per line. Skip caches, temp files, and the Obsidian vault.">
              <textarea value={excludes} onChange={(e) => setExcludes(e.target.value)} rows={5}
                className="mono" style={{ ...window.inputStyle, fontSize: 12, resize: "vertical", lineHeight: 1.6 }} />
            </Field>
            <Field label="Alert routing" hint="Where to nag when this corpus changes.">
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 11, padding: "10px 13px", border: "1px solid var(--border)", borderRadius: "var(--radius)", background: "var(--surface)" }}>
                  <Icon name="mail" size={16} style={{ color: "var(--text-3)" }} />
                  <span style={{ flex: 1, fontSize: 13 }}>Email</span>
                  <Toggle on={alerts.includes("email")} onChange={() => toggleAlert("email")} />
                </div>
                {[["webhook", "Webhook", "webhook"], ["telegram", "Telegram", "signal"], ["signal", "Signal", "signal"]].map(([k, lbl, ic]) => (
                  <div key={k} style={{ display: "flex", alignItems: "center", gap: 11, padding: "10px 13px", border: "1px dashed var(--border)", borderRadius: "var(--radius)", background: "transparent", opacity: 0.7 }}>
                    <Icon name={ic} size={16} style={{ color: "var(--text-3)" }} />
                    <span style={{ flex: 1, fontSize: 13, color: "var(--text-2)" }}>{lbl}</span>
                    <Pill color="var(--text-3)" soft="var(--surface-2)" size="sm">Planned</Pill>
                  </div>
                ))}
              </div>
            </Field>
          </div>
        </Card>

        <div style={{ display: "flex", gap: 12, justifyContent: "flex-end", paddingBottom: 10 }}>
          <Button variant="ghost" onClick={() => go(existing ? "corpusDetail" : "dashboard", existing?.id)}>Cancel</Button>
          <Button variant="primary" icon="check" disabled={!name || !root || !rootValid} onClick={() => go(existing ? "corpusDetail" : "dashboard", existing?.id)}>
            {existing ? "Save changes" : "Create corpus"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function SegChoice({ value, onChange, options }) {
  return (
    <div style={{ display: "flex", gap: 4, background: "var(--surface-2)", padding: 4, borderRadius: "var(--radius)" }}>
      {options.map(([k, lbl]) => (
        <button key={k} type="button" onClick={() => onChange(k)}
          style={{ flex: 1, padding: "8px 12px", borderRadius: "var(--radius-sm)", border: "none", fontSize: 13, fontWeight: 600,
            background: value === k ? "var(--surface)" : "transparent",
            color: value === k ? "var(--accent)" : "var(--text-3)",
            boxShadow: value === k ? "var(--shadow)" : "none" }}>{lbl}</button>
      ))}
    </div>
  );
}

Object.assign(window, { CorpusDetailPage, AddCorpusPage, SegChoice, MiniStat });
