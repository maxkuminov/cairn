/* Cairn — app root: routing, theme/mode state, Tweaks panel, mount. */

const { useState, useEffect } = React;

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "theme": "slate",
  "mode": "light",
  "showLogin": false
}/*EDITMODE-END*/;

function applyTheme(theme, mode) {
  const el = document.documentElement;
  el.setAttribute("data-theme", theme);
  el.setAttribute("data-mode", mode);
}

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  const [page, setPage] = useState("dashboard");
  const [activeCorpus, setActiveCorpus] = useState("photos");
  const [editId, setEditId] = useState(null);
  const [verifyTarget, setVerifyTarget] = useState(null);
  const [authed, setAuthed] = useState(true);
  const [acked, setAcked] = useState([]);
  const [accepted, setAccepted] = useState([]);

  // theme + mode driven by tweaks
  useEffect(() => { applyTheme(t.theme, t.mode); }, [t.theme, t.mode]);
  // login screen driven by tweak toggle
  useEffect(() => { if (t.showLogin) setAuthed(false); else setAuthed(true); }, [t.showLogin]);

  const user = window.CAIRN.USERS[0]; // alice (admin)
  const isAdmin = user.admin;

  const go = (p, id) => {
    if (p === "corpusDetail" && id) setActiveCorpus(id);
    if (p === "addCorpus") setEditId(id || null);
    if (p === "verify") setVerifyTarget(id || null);
    if (p === "corpus") { setPage("dashboard"); return; }
    setPage(p);
    window.scrollTo(0, 0);
  };
  const toggleMode = () => setTweak("mode", t.mode === "dark" ? "light" : "dark");
  const alertCount = window.CAIRN.EVENTS.filter(e => !e.ack && !acked.includes(e.id) && e.kind === "missing").length;

  const onAck = (id) => setAcked(p => [...p, id]);
  const onAccept = (cid) => setAccepted(p => [...p, cid]);

  if (!authed) {
    return (
      <>
        <LoginPage mode={t.mode} onToggleMode={toggleMode} onLogin={() => { setTweak("showLogin", false); setAuthed(true); }} />
        <Tweaks t={t} setTweak={setTweak} />
      </>
    );
  }

  let content;
  if (page === "dashboard") content = <DashboardPage go={go} onAck={onAck} acked={acked} />;
  else if (page === "corpusDetail") content = <CorpusDetailPage corpusId={activeCorpus} go={go} acked={acked} onAccept={onAccept} accepted={accepted} />;
  else if (page === "addCorpus") content = <AddCorpusPage go={go} editId={editId} />;
  else if (page === "verify") content = <VerifyPage go={go} target={verifyTarget} />;
  else if (page === "settings") content = <SettingsPage isAdmin={isAdmin} />;
  else content = <DashboardPage go={go} onAck={onAck} acked={acked} />;

  return (
    <div className="cairn-app">
      <Sidebar page={page} go={go} user={user} alertCount={alertCount} isAdmin={isAdmin} />
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column" }}>
        <Topbar heartbeat={window.CAIRN.HEARTBEAT} mode={t.mode} onToggleMode={toggleMode} onLogout={() => { setTweak("showLogin", true); setAuthed(false); }} />
        <main style={{ padding: "28px 32px 60px", flex: 1, maxWidth: 1240, width: "100%", margin: "0 auto" }}>
          {content}
        </main>
      </div>
      <Tweaks t={t} setTweak={setTweak} />
    </div>
  );
}

function Tweaks({ t, setTweak }) {
  return (
    <TweaksPanel>
      <TweakSection label="Appearance" />
      <TweakRadio label="Mode" value={t.mode}
        options={["light", "dark"]}
        onChange={(v) => setTweak("mode", v)} />
      <TweakSection label="Screens" />
      <TweakToggle label="Show login screen" value={t.showLogin}
        onChange={(v) => setTweak("showLogin", v)} />
    </TweaksPanel>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
