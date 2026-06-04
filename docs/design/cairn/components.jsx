/* Cairn — shared UI components. Exports to window. */

const C = window; // shorthand for window-scoped components/icons

/* ---------- Status metadata ---------- */
const STATUS_META = {
  ok:       { label: "OK",       color: "var(--ok)",     soft: "var(--ok-soft)",     icon: "checkCircle" },
  modified: { label: "Modified", color: "var(--warn)",   soft: "var(--warn-soft)",   icon: "alert" },
  missing:  { label: "Missing",  color: "var(--danger)", soft: "var(--danger-soft)", icon: "minusCircle" },
  new:      { label: "New",      color: "var(--accent)", soft: "var(--accent-soft)", icon: "plus" },
  added:    { label: "Added",    color: "var(--accent)", soft: "var(--accent-soft)", icon: "plus" },
  restored: { label: "Restored", color: "var(--ok)",     soft: "var(--ok-soft)",     icon: "refresh" },
};
const OTS_META = {
  complete:   { label: "Anchored",   color: "var(--ok)",     soft: "var(--ok-soft)",   icon: "bitcoin" },
  pending:    { label: "Pending",    color: "var(--warn)",   soft: "var(--warn-soft)", icon: "clock" },
  incomplete: { label: "Incomplete", color: "var(--warn)",   soft: "var(--warn-soft)", icon: "clock" },
  none:       { label: "Not stamped",color: "var(--text-3)", soft: "var(--surface-2)", icon: "minusCircle" },
};
const CORPUS_STATUS = {
  ok:        { label: "All clear",  color: "var(--ok)" },
  attention: { label: "Attention",  color: "var(--warn)" },
  alert:     { label: "Alert",      color: "var(--danger)" },
};
window.STATUS_META = STATUS_META;
window.OTS_META = OTS_META;
window.CORPUS_STATUS = CORPUS_STATUS;

/* ---------- Pill / badge ---------- */
function Pill({ icon, color = "var(--text-2)", soft, children, size = "md", solid = false }) {
  const pad = size === "sm" ? "2px 8px" : "3px 10px";
  const fs = size === "sm" ? 11.5 : 12.5;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5, padding: pad,
      borderRadius: 999, fontSize: fs, fontWeight: 600, lineHeight: 1.2,
      color: solid ? "var(--accent-fg)" : color,
      background: solid ? color : (soft || "transparent"),
      border: soft || solid ? "none" : "1px solid var(--border)",
      whiteSpace: "nowrap",
    }}>
      {icon && <C.Icon name={icon} size={fs} stroke={2} />}
      {children}
    </span>
  );
}

function StatusBadge({ status, size = "md" }) {
  const m = STATUS_META[status] || STATUS_META.ok;
  return <Pill icon={m.icon} color={m.color} soft={m.soft} size={size}>{m.label}</Pill>;
}
function OtsBadge({ state, size = "md" }) {
  const m = OTS_META[state] || OTS_META.none;
  return <Pill icon={m.icon} color={m.color} soft={m.soft} size={size}>{m.label}</Pill>;
}

/* ---------- Dot ---------- */
function Dot({ color, size = 8, pulse = false }) {
  return (
    <span style={{ position: "relative", display: "inline-flex" }}>
      {pulse && <span style={{ position: "absolute", inset: 0, borderRadius: 999, background: color, opacity: 0.5, animation: "cairnPulse 2s ease-out infinite" }} />}
      <span style={{ width: size, height: size, borderRadius: 999, background: color, display: "block" }} />
    </span>
  );
}

/* ---------- Button ---------- */
function Button({ children, variant = "default", size = "md", icon, iconRight, onClick, type, full, disabled, style }) {
  const sizes = {
    sm: { padding: "6px 11px", fontSize: 13, gap: 6 },
    md: { padding: "9px 15px", fontSize: 13.5, gap: 7 },
    lg: { padding: "12px 20px", fontSize: 15, gap: 8 },
  }[size];
  const variants = {
    primary: { background: "var(--accent)", color: "var(--accent-fg)", border: "1px solid transparent" },
    default: { background: "var(--surface)", color: "var(--text)", border: "1px solid var(--border-strong)" },
    ghost:   { background: "transparent", color: "var(--text-2)", border: "1px solid transparent" },
    subtle:  { background: "var(--surface-2)", color: "var(--text)", border: "1px solid var(--border)" },
    danger:  { background: "var(--danger)", color: "#fff", border: "1px solid transparent" },
  };
  const [hover, setHover] = React.useState(false);
  return (
    <button type={type || "button"} onClick={onClick} disabled={disabled}
      onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}
      style={{
        display: "inline-flex", alignItems: "center", justifyContent: "center", ...sizes,
        ...variants[variant], borderRadius: "var(--radius)", fontWeight: 600,
        width: full ? "100%" : "auto", transition: "all 0.14s ease", whiteSpace: "nowrap",
        opacity: disabled ? 0.5 : 1, cursor: disabled ? "not-allowed" : "pointer",
        filter: hover && !disabled ? "brightness(0.96)" : "none",
        boxShadow: variant === "primary" && hover ? "var(--shadow)" : "none",
        ...style,
      }}>
      {icon && <C.Icon name={icon} size={sizes.fontSize + 2} stroke={2} />}
      {children}
      {iconRight && <C.Icon name={iconRight} size={sizes.fontSize + 2} stroke={2} />}
    </button>
  );
}

/* ---------- Form fields ---------- */
function Field({ label, hint, children, required }) {
  return (
    <label style={{ display: "block" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 7 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text)" }}>
          {label}{required && <span style={{ color: "var(--danger)" }}> *</span>}
        </span>
      </div>
      {children}
      {hint && <div style={{ fontSize: 12, color: "var(--text-3)", marginTop: 6, lineHeight: 1.45 }}>{hint}</div>}
    </label>
  );
}
const inputStyle = {
  width: "100%", padding: "9px 12px", borderRadius: "var(--radius)",
  border: "1px solid var(--border-strong)", background: "var(--surface)",
  color: "var(--text)", fontSize: 13.5, outline: "none", transition: "border-color 0.14s",
};
function Input({ mono, style, ...rest }) {
  return <input {...rest} className={mono ? "mono" : ""} style={{ ...inputStyle, ...(mono ? { fontSize: 12.5 } : {}), ...style }}
    onFocus={(e) => e.target.style.borderColor = "var(--accent)"} onBlur={(e) => e.target.style.borderColor = "var(--border-strong)"} />;
}
function Select({ children, style, ...rest }) {
  return <select {...rest} style={{ ...inputStyle, appearance: "none", backgroundImage: "none", cursor: "pointer", ...style }}>{children}</select>;
}
function Toggle({ on, onChange }) {
  return (
    <button type="button" role="switch" aria-checked={on} onClick={() => onChange(!on)}
      style={{ width: 40, height: 23, borderRadius: 999, border: "none", padding: 2,
        background: on ? "var(--accent)" : "var(--border-strong)", transition: "background 0.16s", flexShrink: 0 }}>
      <span style={{ display: "block", width: 19, height: 19, borderRadius: 999, background: "#fff",
        transform: on ? "translateX(17px)" : "translateX(0)", transition: "transform 0.16s",
        boxShadow: "0 1px 3px rgba(0,0,0,0.25)" }} />
    </button>
  );
}

/* ---------- Card ---------- */
function Card({ children, style, pad = 20, onClick, hover = false }) {
  const [h, setH] = React.useState(false);
  return (
    <div onClick={onClick}
      onMouseEnter={() => setH(true)} onMouseLeave={() => setH(false)}
      style={{
        background: "var(--surface)", border: "1px solid var(--border)",
        borderRadius: "var(--radius-lg)", padding: pad,
        boxShadow: hover && h ? "var(--shadow-lg)" : "var(--shadow)",
        transition: "box-shadow 0.16s, transform 0.16s, border-color 0.16s",
        transform: hover && h ? "translateY(-2px)" : "none",
        cursor: onClick ? "pointer" : "default",
        borderColor: hover && h ? "var(--border-strong)" : "var(--border)",
        ...style,
      }}>
      {children}
    </div>
  );
}

/* ---------- Progress bar (segmented) ---------- */
function SegBar({ segments, height = 7 }) {
  const total = segments.reduce((s, x) => s + x.value, 0) || 1;
  return (
    <div style={{ display: "flex", height, borderRadius: 999, overflow: "hidden", background: "var(--surface-2)", gap: 1.5 }}>
      {segments.filter(s => s.value > 0).map((s, i) => (
        <div key={i} title={`${s.label}: ${s.value.toLocaleString()}`}
          style={{ width: `${(s.value / total) * 100}%`, background: s.color, minWidth: 3 }} />
      ))}
    </div>
  );
}

/* ---------- Page header ---------- */
function PageHeader({ title, subtitle, children, back }) {
  return (
    <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16, marginBottom: 24, flexWrap: "wrap" }}>
      <div>
        {back && (
          <button onClick={back.onClick} style={{ display: "inline-flex", alignItems: "center", gap: 6, background: "none", border: "none", color: "var(--text-3)", fontSize: 13, fontWeight: 600, padding: 0, marginBottom: 10 }}>
            <C.Icon name="arrowLeft" size={15} /> {back.label}
          </button>
        )}
        <h1 style={{ fontSize: 25, marginBottom: subtitle ? 5 : 0 }}>{title}</h1>
        {subtitle && <div style={{ color: "var(--text-2)", fontSize: 14, maxWidth: 620 }}>{subtitle}</div>}
      </div>
      {children && <div style={{ display: "flex", gap: 10, alignItems: "center" }}>{children}</div>}
    </div>
  );
}

Object.assign(window, { Pill, StatusBadge, OtsBadge, Dot, Button, Field, Input, Select, Toggle, Card, SegBar, PageHeader, inputStyle });
