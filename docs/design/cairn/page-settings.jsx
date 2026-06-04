/* Cairn — Settings + Login. Exports: SettingsPage, LoginPage */

function SettingsPage({ isAdmin }) {
  const { USERS, HEARTBEAT } = window.CAIRN;
  const [tab, setTab] = React.useState("notifications");
  const tabs = [
    ["notifications", "Notifications", "bell"],
    ["verify", "Verification", "verify"],
    ...(isAdmin ? [["admin", "Users & mounts", "users"]] : []),
  ];
  const [chan, setChan] = React.useState({ email: true, signal: true, webhook: false, ntfy: false, kuma: true });
  const set = (k, v) => setChan(p => ({ ...p, [k]: v }));

  return (
    <div style={{ maxWidth: 820 }}>
      <PageHeader title="Settings" subtitle="Your notification channels and verification backend." />

      {/* Tabs */}
      <div style={{ display: "flex", gap: 4, borderBottom: "1px solid var(--border)", marginBottom: 24 }}>
        {tabs.map(([k, lbl, ic]) => (
          <button key={k} onClick={() => setTab(k)}
            style={{ display: "flex", alignItems: "center", gap: 7, padding: "10px 14px", border: "none", background: "transparent",
              borderBottom: `2px solid ${tab === k ? "var(--accent)" : "transparent"}`, marginBottom: -1,
              color: tab === k ? "var(--text)" : "var(--text-3)", fontSize: 13.5, fontWeight: 600 }}>
            <Icon name={ic} size={16} />{lbl}
          </button>
        ))}
      </div>

      {tab === "notifications" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 22 }}>
          {/* Email — active */}
          <div>
            <SectionLabel>Alert channel</SectionLabel>
            <div style={{ marginTop: 12 }}>
              <EmailCard on={chan.email} onChange={(v) => set("email", v)} />
            </div>
          </div>

          {/* Health monitoring */}
          <div>
            <SectionLabel>Health monitoring</SectionLabel>
            <Card pad={18} style={{ marginTop: 12 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
                <div style={{ width: 40, height: 40, borderRadius: "var(--radius)", background: "var(--ok-soft)", color: "var(--ok)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                  <Icon name="pulse" size={19} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 14, fontWeight: 600, whiteSpace: "nowrap" }}>Health endpoint</span>
                    <Pill icon="checkCircle" color="var(--ok)" soft="var(--ok-soft)" size="sm">Healthy</Pill>
                  </div>
                  <div style={{ fontSize: 12.5, color: "var(--text-3)", marginTop: 3 }}>Point Uptime Kuma or any monitor at this URL — it returns scan freshness as a dead-man's switch.</div>
                </div>
              </div>
              <div className="mono" style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 13, padding: "9px 13px", background: "var(--surface-2)", borderRadius: "var(--radius)", fontSize: 12.5 }}>
                <span style={{ color: "var(--ok)", fontWeight: 600 }}>GET</span>
                <span style={{ color: "var(--text-2)" }}>https://cairn.home.example.com/healthz</span>
                <button style={{ background: "none", border: "none", color: "var(--text-3)", display: "flex", marginLeft: "auto" }}><Icon name="copy" size={15} /></button>
              </div>
            </Card>
          </div>

          {/* Future channels */}
          <div>
            <SectionLabel>Planned channels</SectionLabel>
            <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 12 }}>
              {[["webhook", "Webhook", "POST a JSON payload to a URL of your choosing."],
                ["signal", "Telegram", "Push to a Telegram chat via a bot token."],
                ["signal", "Signal", "Push to your Signal number via a relay."]].map(([ic, lbl, desc], i) => (
                <div key={i} style={{ display: "flex", alignItems: "center", gap: 14, padding: "16px 18px", border: "1px dashed var(--border)", borderRadius: "var(--radius-lg)", background: "transparent", opacity: 0.75 }}>
                  <div style={{ width: 40, height: 40, borderRadius: "var(--radius)", background: "var(--surface-2)", color: "var(--text-3)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                    <Icon name={ic} size={19} />
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 14, fontWeight: 600 }}>{lbl}</div>
                    <div style={{ fontSize: 12.5, color: "var(--text-3)", marginTop: 3 }}>{desc}</div>
                  </div>
                  <Pill color="var(--text-3)" soft="var(--surface-2)" size="sm">Planned</Pill>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {tab === "verify" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <Card pad={22}>
            <SectionLabel>Block source</SectionLabel>
            <div style={{ fontSize: 13, color: "var(--text-2)", margin: "10px 0 16px", lineHeight: 1.5 }}>
              How Cairn checks proofs against the Bitcoin blockchain when verifying.
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <RadioCard selected title="Block explorer" desc="blockstream.info — works out of the box. You trust the explorer's lookup." badge="Default" />
              <RadioCard title="Your own Bitcoin node" desc="Point at a node's RPC for fully trustless verification — nothing to trust but your own chain." />
            </div>
          </Card>
          <Card pad={22}>
            <SectionLabel>Calendar servers</SectionLabel>
            <div style={{ fontSize: 13, color: "var(--text-2)", margin: "10px 0 16px" }}>OpenTimestamps aggregators used when stamping new files.</div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {["alice.btc.calendar.opentimestamps.org", "bob.btc.calendar.opentimestamps.org", "finney.calendar.eternitywall.com"].map(c => (
                <div key={c} className="mono" style={{ display: "flex", alignItems: "center", gap: 10, padding: "10px 13px", border: "1px solid var(--border)", borderRadius: "var(--radius)", fontSize: 12.5 }}>
                  <Dot color="var(--ok)" size={7} />{c}<span style={{ marginLeft: "auto", color: "var(--text-3)", fontSize: 11.5 }}>reachable</span>
                </div>
              ))}
            </div>
          </Card>
        </div>
      )}

      {tab === "admin" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          <Card pad={0}>
            <div style={{ display: "flex", alignItems: "center", padding: "16px 20px", borderBottom: "1px solid var(--border)" }}>
              <div>
                <h3 style={{ fontSize: 15 }}>Users</h3>
                <div style={{ fontSize: 12.5, color: "var(--text-3)", marginTop: 2 }}>Each user is scoped to their own read-only mounted base.</div>
              </div>
              <Button variant="primary" size="sm" icon="plus" style={{ marginLeft: "auto" }}>Invite user</Button>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1.1fr 1.4fr 90px 110px", gap: 12, padding: "10px 20px", fontSize: 11, fontWeight: 700, letterSpacing: "0.04em", textTransform: "uppercase", color: "var(--text-3)", borderBottom: "1px solid var(--border)" }}>
              <span>User</span><span>Mounted base (read-only)</span><span>Corpora</span><span>Role</span>
            </div>
            {USERS.map((u, i) => (
              <div key={u.username} style={{ display: "grid", gridTemplateColumns: "1.1fr 1.4fr 90px 110px", gap: 12, padding: "13px 20px", alignItems: "center", borderBottom: i < USERS.length - 1 ? "1px solid var(--border)" : "none" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <div style={{ width: 30, height: 30, borderRadius: 999, background: "var(--accent-soft)", color: "var(--accent)", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 700, fontSize: 12.5 }}>{u.username[0].toUpperCase()}</div>
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 13.5, fontWeight: 600 }}>{u.username}</div>
                    <div style={{ fontSize: 11.5, color: "var(--text-3)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{u.email}</div>
                  </div>
                </div>
                <span className="mono" style={{ fontSize: 11.5, color: "var(--text-2)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{u.base}</span>
                <span style={{ fontSize: 13, color: "var(--text-2)" }}>{u.corpora}</span>
                {u.admin
                  ? <Pill icon="lock" color="var(--accent)" soft="var(--accent-soft)" size="sm">Admin</Pill>
                  : <Pill color="var(--text-2)" soft="var(--surface-2)" size="sm">Member</Pill>}
              </div>
            ))}
          </Card>
          <div style={{ display: "flex", alignItems: "flex-start", gap: 9, padding: "13px 15px", background: "var(--surface-2)", borderRadius: "var(--radius)", fontSize: 12.5, color: "var(--text-2)" }}>
            <Icon name="lock" size={15} style={{ color: "var(--text-3)", marginTop: 1, flexShrink: 0 }} />
            <span>Watched folders are mounted <strong>read-only</strong>; the database and proof store live on a separate writable volume. Cairn physically cannot modify or delete what it watches.</span>
          </div>
        </div>
      )}
    </div>
  );
}

function EmailCard({ on, onChange }) {
  const [provider, setProvider] = React.useState("smtp");
  const providers = [["smtp", "Local SMTP"], ["resend", "Resend"], ["ses", "AWS SES"]];
  const config = {
    smtp:   [["Host", "smtp.localhost:25"], ["From", "cairn@example.com"], ["Encryption", "STARTTLS"]],
    resend: [["API key", "re_••••••••••3f9a"], ["From", "alerts@example.com"], ["Region", "global"]],
    ses:    [["Region", "us-east-1"], ["From", "alerts@example.com"], ["Access key", "AKIA••••••7B2Q"]],
  }[provider];
  return (
    <Card pad={20}>
      <div style={{ display: "flex", alignItems: "center", gap: 14, marginBottom: 16 }}>
        <div style={{ width: 40, height: 40, borderRadius: "var(--radius)", background: on ? "var(--accent-soft)" : "var(--surface-2)", color: on ? "var(--accent)" : "var(--text-3)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
          <Icon name="mail" size={19} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 14, fontWeight: 600, whiteSpace: "nowrap" }}>Email</span>
            <Pill icon="checkCircle" color="var(--ok)" soft="var(--ok-soft)" size="sm">Verified</Pill>
          </div>
          <div style={{ fontSize: 12.5, color: "var(--text-3)", marginTop: 3 }}>Each user routes alerts to their own provider and address.</div>
        </div>
        <Toggle on={on} onChange={onChange} />
      </div>

      <div style={{ opacity: on ? 1 : 0.45, pointerEvents: on ? "auto" : "none", transition: "opacity 0.15s" }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-2)", marginBottom: 8 }}>Provider</div>
        <div style={{ display: "flex", gap: 4, background: "var(--surface-2)", padding: 4, borderRadius: "var(--radius)", marginBottom: 14 }}>
          {providers.map(([k, lbl]) => (
            <button key={k} type="button" onClick={() => setProvider(k)}
              style={{ flex: 1, padding: "8px 10px", borderRadius: "var(--radius-sm)", border: "none", fontSize: 12.5, fontWeight: 600,
                background: provider === k ? "var(--surface)" : "transparent",
                color: provider === k ? "var(--accent)" : "var(--text-3)",
                boxShadow: provider === k ? "var(--shadow)" : "none" }}>{lbl}</button>
          ))}
        </div>
        <div style={{ display: "grid", gap: 1, background: "var(--border)", borderRadius: "var(--radius)", overflow: "hidden", border: "1px solid var(--border)" }}>
          {config.map(([k, v]) => (
            <div key={k} style={{ display: "flex", alignItems: "center", gap: 12, padding: "9px 13px", background: "var(--surface)" }}>
              <span style={{ fontSize: 12.5, color: "var(--text-3)", width: 92, flexShrink: 0 }}>{k}</span>
              <span className="mono" style={{ fontSize: 12, color: "var(--text-2)" }}>{v}</span>
            </div>
          ))}
        </div>
        {provider !== "smtp" && (
          <div style={{ fontSize: 12, color: "var(--text-3)", marginTop: 10, display: "flex", alignItems: "flex-start", gap: 7 }}>
            <Icon name="info" size={14} style={{ marginTop: 1, flexShrink: 0 }} />
            <span>Good for users without a local mail server — Cairn sends through {provider === "resend" ? "Resend" : "AWS SES"}.</span>
          </div>
        )}
      </div>
    </Card>
  );
}

function ChannelCard({ icon, title, desc, on, onChange, status, deadman }) {
  return (
    <Card pad={18}>
      <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
        <div style={{ width: 40, height: 40, borderRadius: "var(--radius)", background: on ? "var(--accent-soft)" : "var(--surface-2)", color: on ? "var(--accent)" : "var(--text-3)", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
          <Icon name={icon} size={19} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontSize: 14, fontWeight: 600, whiteSpace: "nowrap" }}>{title}</span>
            {status && <Pill icon={deadman ? "heart" : "checkCircle"} color="var(--ok)" soft="var(--ok-soft)" size="sm">{status}</Pill>}
          </div>
          <div style={{ fontSize: 12.5, color: "var(--text-3)", marginTop: 3, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{desc}</div>
        </div>
        <Toggle on={on} onChange={onChange} />
      </div>
    </Card>
  );
}

function RadioCard({ selected, title, desc, badge }) {
  const [sel, setSel] = React.useState(selected);
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "flex-start", padding: "14px 16px", borderRadius: "var(--radius)",
      border: `1.5px solid ${sel ? "var(--accent)" : "var(--border)"}`, background: sel ? "var(--accent-soft)" : "var(--surface)", cursor: "default" }}>
      <span style={{ color: sel ? "var(--accent)" : "var(--border-strong)", marginTop: 1 }}><Icon name={sel ? "checkCircle" : "minusCircle"} size={18} /></span>
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 13.5, fontWeight: 600, color: sel ? "var(--accent)" : "var(--text)" }}>{title}</span>
          {badge && <Pill color="var(--accent)" soft="var(--accent-soft)" size="sm">{badge}</Pill>}
        </div>
        <div style={{ fontSize: 12.5, color: "var(--text-2)", marginTop: 3 }}>{desc}</div>
      </div>
    </div>
  );
}

/* ---------------- Login ---------------- */
function LoginPage({ onLogin, mode, onToggleMode }) {
  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", padding: 24, position: "relative" }}>
      <button onClick={onToggleMode} style={{ position: "absolute", top: 22, right: 22, width: 38, height: 38, borderRadius: "var(--radius)", border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text-2)", display: "flex", alignItems: "center", justifyContent: "center" }}>
        <Icon name={mode === "dark" ? "sun" : "moon"} size={18} />
      </button>

      <div style={{ width: "100%", maxWidth: 380 }}>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", marginBottom: 28 }}>
          <CairnMark size={48} />
          <div style={{ fontFamily: "var(--font-head)", fontSize: 27, fontWeight: 700, letterSpacing: "var(--head-spacing)", marginTop: 12 }}>Cairn</div>
          <div style={{ fontSize: 13, color: "var(--text-3)", marginTop: 4 }}>File-integrity monitor &amp; notary</div>
        </div>

        <Card pad={26}>
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <Field label="Username"><Input defaultValue="alice" /></Field>
            <Field label="Password"><Input type="password" defaultValue="••••••••••" /></Field>
            <Button variant="primary" full size="lg" icon="lock" onClick={onLogin}>Sign in</Button>
          </div>
        </Card>

        <div style={{ display: "flex", alignItems: "center", gap: 9, marginTop: 16, padding: "11px 14px", background: "var(--surface-2)", borderRadius: "var(--radius)", fontSize: 12, color: "var(--text-3)" }}>
          <Icon name="info" size={15} style={{ flexShrink: 0 }} />
          <span>This instance runs in <strong style={{ color: "var(--text-2)" }}>multi-user</strong> mode. Single-user installs skip login entirely.</span>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { SettingsPage, LoginPage });
