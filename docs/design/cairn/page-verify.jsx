/* Cairn — Verify proof page (in-system; no external upload). Exports: VerifyPage */

function pseudoSha(str) {
  // deterministic, well-distributed 64-hex from a string — realistic-looking mock hashes
  let seed = 0x9e3779b9 ^ str.length;
  for (let i = 0; i < str.length; i++) {
    seed = Math.imul(seed ^ str.charCodeAt(i), 0x85ebca6b) >>> 0;
    seed = (seed ^ (seed >>> 13)) >>> 0;
  }
  let out = "";
  for (let i = 0; i < 64; i++) {
    seed = Math.imul(seed ^ (seed >>> 15), 0xc2b2ae35) >>> 0;
    seed = (seed + 0x27d4eb2f) >>> 0;
    out += ((seed >>> (i % 5) * 4) & 0xf).toString(16);
  }
  return out;
}
function makeResult(file) {
  const sha = pseudoSha(file.relpath);
  const blocks = ["2026-02-14 18:22 UTC", "2025-11-03 06:11 UTC", "2026-04-21 22:47 UTC", "2026-05-28 09:05 UTC"];
  const n = file.relpath.length;
  return {
    filename: file.filename,
    relpath: file.relpath,
    corpus: file.corpus,
    sha256: sha,
    existedBy: blocks[n % blocks.length],
    block: 826000 + (n * 137 % 9000),
    blockHash: "00000000000000000002" + sha.slice(0, 4) + "…" + sha.slice(-4),
    calendars: ["alice.btc.calendar.opentimestamps.org", "bob.btc.calendar.opentimestamps.org"],
    source: "blockstream.info (explorer lookup)",
  };
}

// flatten every anchored (complete) file across corpora
function anchoredFiles() {
  const { CORPORA, FILES } = window.CAIRN;
  const out = [];
  CORPORA.forEach(c => {
    if (c.ots === "none") return;
    (FILES[c.id] || []).forEach(f => {
      if (f.ots === "complete") out.push({ filename: f.relpath.split("/").pop(), relpath: f.relpath, corpus: c.name, corpusId: c.id, size: f.size });
    });
  });
  return out;
}

function VerifyPage({ go, target }) {
  const [state, setState] = React.useState("idle"); // idle | checking | result
  const [selected, setSelected] = React.useState(null);
  const [query, setQuery] = React.useState("");
  const result = selected ? makeResult(selected) : null;

  const run = (file) => { setSelected(file); setState("checking"); setTimeout(() => setState("result"), 1300); };
  const reset = () => { setState("idle"); setSelected(null); };

  // arriving from the corpus browser with a target file → verify it straight away
  React.useEffect(() => {
    if (target && target.relpath) run(target);
  }, [target && target.relpath]);

  const files = anchoredFiles();
  const totalAnchored = window.CAIRN.CORPORA.reduce((s, c) => s + (c.ots_counts ? c.ots_counts.complete : 0), 0);
  const q = query.trim().toLowerCase();
  const matched = q ? files.filter(f => (f.relpath + f.corpus).toLowerCase().includes(q)) : [];
  const recent = files.slice(0, 4);
  const shown = q ? matched : recent;

  return (
    <div style={{ maxWidth: 820 }}>
      <PageHeader title="Verify a proof"
        subtitle="Search for any file Cairn already tracks. It re-hashes the bytes in the read-only store, loads the OpenTimestamps proof it holds for that file, and checks it against the Bitcoin blockchain. Nothing is uploaded.">
      </PageHeader>

      {state === "idle" && (
        <Card pad={0}>
          <div style={{ padding: "18px 18px 16px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 11, background: "var(--surface-2)", border: "1px solid var(--border-strong)", borderRadius: "var(--radius)", padding: "11px 14px" }}>
              <Icon name="search" size={18} style={{ color: "var(--text-3)" }} />
              <input autoFocus value={query} onChange={(e) => setQuery(e.target.value)} placeholder={`Search ${totalAnchored.toLocaleString()} anchored files by path or corpus…`}
                style={{ border: "none", background: "transparent", outline: "none", color: "var(--text)", fontSize: 14, width: "100%" }} />
              {query && <button onClick={() => setQuery("")} style={{ background: "none", border: "none", color: "var(--text-3)", display: "flex" }}><Icon name="x" size={16} /></button>}
            </div>
          </div>

          <div style={{ padding: "0 18px 6px", fontSize: 11, fontWeight: 700, letterSpacing: "0.05em", textTransform: "uppercase", color: "var(--text-3)" }}>
            {q ? `${matched.length} match${matched.length === 1 ? "" : "es"}` : "Recently anchored"}
          </div>

          <div style={{ maxHeight: 420, overflowY: "auto", paddingBottom: 6 }}>
            {shown.map((f, i) => (
              <button key={i} onClick={() => run(f)}
                style={{ display: "flex", alignItems: "center", gap: 12, width: "100%", padding: "12px 18px", border: "none",
                  borderTop: "1px solid var(--border)", background: "transparent", textAlign: "left", transition: "background 0.1s" }}
                onMouseEnter={(e) => e.currentTarget.style.background = "var(--surface-2)"}
                onMouseLeave={(e) => e.currentTarget.style.background = "transparent"}>
                <Icon name="file" size={16} style={{ color: "var(--text-3)", flexShrink: 0 }} />
                <div style={{ minWidth: 0, flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 600 }}>{f.filename}</div>
                  <div className="mono" style={{ fontSize: 11.5, color: "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.corpus} · {f.relpath}</div>
                </div>
                <Pill icon="bitcoin" color="var(--ok)" soft="var(--ok-soft)" size="sm">Anchored</Pill>
                <Icon name="chevronR" size={15} style={{ color: "var(--text-3)", flexShrink: 0 }} />
              </button>
            ))}
            {q && matched.length === 0 && <div style={{ padding: 40, textAlign: "center", color: "var(--text-3)", fontSize: 13, borderTop: "1px solid var(--border)" }}>No anchored files match “{query}”.</div>}
          </div>

          <div style={{ padding: "11px 18px", borderTop: "1px solid var(--border)", fontSize: 12, color: "var(--text-3)", display: "flex", alignItems: "center", gap: 7 }}>
            <Icon name="info" size={14} />
            <span>Or open any file from a corpus and click its <strong style={{ color: "var(--text-2)" }}>Anchored</strong> badge to verify it directly.</span>
          </div>
        </Card>
      )}

      {state === "checking" && (
        <Card pad={48} style={{ textAlign: "center" }}>
          <div className="cairn-spin" style={{ width: 44, height: 44, borderRadius: 999, border: "3px solid var(--border)", borderTopColor: "var(--accent)", margin: "0 auto 20px" }} />
          <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 6 }}>Verifying against the blockchain…</div>
          <div style={{ fontSize: 13, color: "var(--text-3)" }}>Re-hashing <span className="mono">{selected?.filename}</span> · loading proof · checking explorer</div>
        </Card>
      )}

      {state === "result" && result && (
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 16, padding: "22px 24px", borderRadius: "var(--radius-lg)", background: "var(--ok-soft)", border: "1px solid var(--ok)", marginBottom: 18 }}>
            <div style={{ width: 48, height: 48, borderRadius: 999, background: "var(--ok)", color: "#fff", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
              <Icon name="check" size={26} stroke={2.4} />
            </div>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontFamily: "var(--font-head)", fontSize: 19, fontWeight: 700, color: "var(--ok)" }}>Proof verified</div>
              <div style={{ fontSize: 13.5, color: "var(--text-2)", marginTop: 2 }}>
                <span className="mono">{result.filename}</span> existed, unaltered, by <strong>{result.existedBy}</strong>.
              </div>
            </div>
            <Button variant="ghost" icon="arrowLeft" onClick={reset}>Verify another</Button>
          </div>

          <Card pad={0}>
            <DetailRow label="File" value={<span className="mono" style={{ fontSize: 12.5 }}>{result.relpath}</span>} />
            <DetailRow label="Corpus" value={result.corpus} />
            <DetailRow label="SHA-256" value={<span className="mono" style={{ fontSize: 11.5, wordBreak: "break-all", color: "var(--text-2)" }}>{result.sha256}</span>} copy />
            <DetailRow label="Existed by" value={<span style={{ fontWeight: 600 }}>{result.existedBy}</span>} icon="calendar" />
            <DetailRow label="Bitcoin block" value={<span><span className="mono">#{result.block.toLocaleString()}</span> <span className="mono" style={{ color: "var(--text-3)", fontSize: 11.5 }}>{result.blockHash}</span></span>} icon="bitcoin" />
            <DetailRow label="Calendars" value={<div style={{ display: "flex", flexDirection: "column", gap: 3 }}>{result.calendars.map(c => <span key={c} className="mono" style={{ fontSize: 11.5, color: "var(--text-2)" }}>{c}</span>)}</div>} />
            <DetailRow label="Verified via" value={result.source} icon="link" last />
          </Card>

          <div style={{ display: "flex", gap: 12, marginTop: 16 }}>
            <Button variant="default" icon="download">Export proof bundle (.ots + file)</Button>
            <Button variant="ghost" icon="copy">Copy verification report</Button>
          </div>
          <div style={{ display: "flex", alignItems: "flex-start", gap: 9, marginTop: 18, padding: "12px 14px", background: "var(--surface-2)", borderRadius: "var(--radius)", fontSize: 12.5, color: "var(--text-2)" }}>
            <Icon name="info" size={15} style={{ color: "var(--text-3)", marginTop: 1, flexShrink: 0 }} />
            <span>Verified by explorer lookup. For fully trustless verification, point Cairn at your own Bitcoin node in Settings. Export bundles the file with its <span className="mono">.ots</span> proof so a third party can verify it independently.</span>
          </div>
        </div>
      )}
    </div>
  );
}

function DetailRow({ label, value, icon, copy, last }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "150px 1fr auto", gap: 16, padding: "14px 20px", alignItems: "center", borderBottom: last ? "none" : "1px solid var(--border)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, color: "var(--text-3)", fontSize: 12.5, fontWeight: 600 }}>
        {icon && <Icon name={icon} size={15} />}{label}
      </div>
      <div style={{ fontSize: 13.5, minWidth: 0 }}>{value}</div>
      {copy ? <button style={{ background: "none", border: "none", color: "var(--text-3)", display: "flex" }}><Icon name="copy" size={15} /></button> : <span />}
    </div>
  );
}

Object.assign(window, { VerifyPage });
