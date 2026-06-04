/* Cairn — app shell: Sidebar + Topbar. Exports to window. */

function NavItem({ icon, label, active, onClick, badge }) {
  const [h, setH] = React.useState(false);
  return (
    <button onClick={onClick} onMouseEnter={() => setH(true)} onMouseLeave={() => setH(false)}
      style={{
        display: "flex", alignItems: "center", gap: 11, width: "100%",
        padding: "9px 12px", borderRadius: "var(--radius)", border: "none",
        background: active ? "var(--accent-soft)" : (h ? "var(--surface-2)" : "transparent"),
        color: active ? "var(--accent)" : "var(--text-2)",
        fontSize: 13.5, fontWeight: active ? 600 : 500, textAlign: "left",
        transition: "all 0.13s", position: "relative",
      }}>
      <Icon name={icon} size={18} stroke={active ? 2.1 : 1.8} />
      <span style={{ flex: 1 }}>{label}</span>
      {badge > 0 && (
        <span style={{ fontSize: 11, fontWeight: 700, minWidth: 18, height: 18, padding: "0 5px",
          borderRadius: 999, background: "var(--danger)", color: "#fff",
          display: "inline-flex", alignItems: "center", justifyContent: "center" }}>{badge}</span>
      )}
    </button>
  );
}

function Sidebar({ page, go, user, alertCount, isAdmin }) {
  return (
    <aside style={{
      width: 248, flexShrink: 0, background: "var(--bg-2)", borderRight: "1px solid var(--border)",
      display: "flex", flexDirection: "column", height: "100vh", position: "sticky", top: 0,
    }}>
      {/* Brand */}
      <div style={{ padding: "20px 18px 16px", display: "flex", alignItems: "center", gap: 11 }}>
        <CairnMark size={30} />
        <div>
          <div style={{ fontFamily: "var(--font-head)", fontSize: 19, fontWeight: 700, letterSpacing: "var(--head-spacing)", lineHeight: 1 }}>Cairn</div>
          <div style={{ fontSize: 10.5, color: "var(--text-3)", letterSpacing: "0.06em", textTransform: "uppercase", marginTop: 3 }}>Integrity &amp; Notary</div>
        </div>
      </div>

      {/* Nav */}
      <nav style={{ padding: "8px 12px", display: "flex", flexDirection: "column", gap: 3, flex: 1, overflowY: "auto" }}>
        <NavItem icon="dashboard" label="Dashboard" active={page === "dashboard"} onClick={() => go("dashboard")} badge={alertCount} />
        <NavItem icon="stack" label="Corpora" active={page === "corpus" || page === "addCorpus"} onClick={() => go("corpus")} />
        <NavItem icon="verify" label="Verify proof" active={page === "verify"} onClick={() => go("verify")} />

        <div style={{ height: 1, background: "var(--border)", margin: "12px 6px" }} />
        <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: "0.07em", textTransform: "uppercase", color: "var(--text-3)", padding: "2px 12px 6px" }}>Corpora</div>
        {window.CAIRN.CORPORA.map((c) => {
          const m = CORPUS_STATUS[c.status];
          return (
            <button key={c.id} onClick={() => go("corpusDetail", c.id)}
              style={{ display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "8px 12px",
                borderRadius: "var(--radius)", border: "none", background: "transparent", color: "var(--text-2)",
                fontSize: 13, fontWeight: 500, textAlign: "left" }}
              onMouseEnter={(e) => e.currentTarget.style.background = "var(--surface-2)"}
              onMouseLeave={(e) => e.currentTarget.style.background = "transparent"}>
              <Dot color={m.color} size={7} pulse={c.status === "alert"} />
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.name}</span>
              <span style={{ fontSize: 11, color: "var(--text-3)" }}>{c.files > 9999 ? (c.files/1000).toFixed(0) + "k" : c.files.toLocaleString()}</span>
            </button>
          );
        })}
        <button onClick={() => go("addCorpus")} style={{ display: "flex", alignItems: "center", gap: 10, width: "100%", padding: "8px 12px", borderRadius: "var(--radius)", border: "1px dashed var(--border-strong)", background: "transparent", color: "var(--text-3)", fontSize: 12.5, fontWeight: 600, marginTop: 4, whiteSpace: "nowrap" }}>
          <Icon name="plus" size={15} /> Add corpus
        </button>
      </nav>

      {/* Footer / settings + user */}
      <div style={{ padding: "10px 12px", borderTop: "1px solid var(--border)" }}>
        <NavItem icon="settings" label="Settings" active={page === "settings"} onClick={() => go("settings")} />
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 10px 4px" }}>
          <div style={{ width: 30, height: 30, borderRadius: 999, background: "var(--accent)", color: "var(--accent-fg)", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 700, fontSize: 13, flexShrink: 0 }}>
            {user.username[0].toUpperCase()}
          </div>
          <div style={{ flex: 1, overflow: "hidden" }}>
            <div style={{ fontSize: 13, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{user.username}{isAdmin && <span style={{ fontSize: 10, fontWeight: 700, color: "var(--accent)", marginLeft: 6, padding: "1px 5px", background: "var(--accent-soft)", borderRadius: 4, verticalAlign: "middle" }}>ADMIN</span>}</div>
            <div style={{ fontSize: 11, color: "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{user.email}</div>
          </div>
        </div>
      </div>
    </aside>
  );
}

function Topbar({ heartbeat, mode, onToggleMode, onLogout, modeLabel }) {
  return (
    <header style={{
      height: 60, borderBottom: "1px solid var(--border)", background: "var(--bg)",
      display: "flex", alignItems: "center", gap: 14, padding: "0 28px",
      position: "sticky", top: 0, zIndex: 20, backdropFilter: "blur(8px)",
    }}>
      {/* Search */}
      <div style={{ display: "flex", alignItems: "center", gap: 9, flex: 1, maxWidth: 380,
        background: "var(--surface-2)", border: "1px solid var(--border)", borderRadius: "var(--radius)", padding: "7px 12px" }}>
        <Icon name="search" size={16} style={{ color: "var(--text-3)" }} />
        <input placeholder="Search files, paths, hashes…" style={{ border: "none", background: "transparent", outline: "none", color: "var(--text)", fontSize: 13, width: "100%" }} />
      </div>
      <div style={{ flex: 1 }} />

      {/* Health endpoint status */}
      <div title="Exposed at /healthz for external monitors" style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 12px", borderRadius: 999, background: "var(--ok-soft)", border: "1px solid transparent", whiteSpace: "nowrap", flexShrink: 0 }}>
        <Dot color="var(--ok)" size={7} pulse />
        <span style={{ fontSize: 12, fontWeight: 600, color: "var(--ok)" }}>Healthy</span>
        <span className="mono" style={{ fontSize: 11, color: "var(--text-3)" }}>/healthz</span>
      </div>

      {/* Mode toggle */}
      <button onClick={onToggleMode} title={`Switch to ${mode === "dark" ? "light" : "dark"} mode`}
        style={{ width: 38, height: 38, borderRadius: "var(--radius)", border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text-2)", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <Icon name={mode === "dark" ? "sun" : "moon"} size={18} />
      </button>

      <button onClick={onLogout} title="Log out"
        style={{ width: 38, height: 38, borderRadius: "var(--radius)", border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text-2)", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <Icon name="logout" size={18} />
      </button>
    </header>
  );
}

Object.assign(window, { Sidebar, Topbar, NavItem });
