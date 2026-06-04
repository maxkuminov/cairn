/* Cairn — icon set (simple stroke glyphs) + logo mark.
   Exports to window: Icon, CairnMark */

function Icon({ name, size = 16, stroke = 1.75, style, className }) {
  const p = {
    width: size, height: size, viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", strokeWidth: stroke, strokeLinecap: "round",
    strokeLinejoin: "round", style, className,
  };
  switch (name) {
    case "dashboard": return (<svg {...p}><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>);
    case "stack": return (<svg {...p}><ellipse cx="12" cy="6" rx="7" ry="2.6"/><path d="M5 6v5c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6V6"/><path d="M5 12v5c0 1.4 3.1 2.6 7 2.6s7-1.2 7-2.6v-5"/></svg>);
    case "verify": return (<svg {...p}><path d="M12 3l7 3v5c0 4.4-3 7.6-7 9-4-1.4-7-4.6-7-9V6l7-3z"/><path d="M9 11.5l2 2 4-4"/></svg>);
    case "settings": return (<svg {...p}><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M22 12h-3M5 12H2M19.1 4.9l-2.1 2.1M7 17l-2.1 2.1M19.1 19.1L17 17M7 7L4.9 4.9"/></svg>);
    case "bell": return (<svg {...p}><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>);
    case "users": return (<svg {...p}><circle cx="9" cy="8" r="3.2"/><path d="M3.5 20a5.5 5.5 0 0 1 11 0"/><path d="M16 5.2a3.2 3.2 0 0 1 0 5.6M17 14.5a5.5 5.5 0 0 1 3.5 5.5"/></svg>);
    case "file": return (<svg {...p}><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>);
    case "folder": return (<svg {...p}><path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>);
    case "check": return (<svg {...p}><path d="M20 6 9 17l-5-5"/></svg>);
    case "checkCircle": return (<svg {...p}><circle cx="12" cy="12" r="9"/><path d="M8.5 12l2.5 2.5 4.5-5"/></svg>);
    case "x": return (<svg {...p}><path d="M18 6 6 18M6 6l12 12"/></svg>);
    case "alert": return (<svg {...p}><path d="M10.3 3.8 2.4 18a1.9 1.9 0 0 0 1.7 2.9h15.8a1.9 1.9 0 0 0 1.7-2.9L13.7 3.8a1.9 1.9 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>);
    case "minusCircle": return (<svg {...p}><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg>);
    case "clock": return (<svg {...p}><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>);
    case "link": return (<svg {...p}><path d="M9 15l6-6"/><path d="M11 6l1-1a4 4 0 0 1 6 6l-1 1M13 18l-1 1a4 4 0 0 1-6-6l1-1"/></svg>);
    case "bitcoin": return (<svg {...p}><circle cx="12" cy="12" r="9"/><path d="M9.5 7.5h4a2.2 2.2 0 0 1 0 4.5h-4M9.5 12h4.3a2.2 2.2 0 0 1 0 4.5H9.5M9.5 7.5v9M11 6v1.5M11 16.5V18M13 6v1.5M13 16.5V18"/></svg>);
    case "upload": return (<svg {...p}><path d="M12 16V4M7 9l5-5 5 5"/><path d="M5 20h14"/></svg>);
    case "search": return (<svg {...p}><circle cx="11" cy="11" r="7"/><path d="m20 20-3.2-3.2"/></svg>);
    case "plus": return (<svg {...p}><path d="M12 5v14M5 12h14"/></svg>);
    case "chevronR": return (<svg {...p}><path d="m9 6 6 6-6 6"/></svg>);
    case "chevronD": return (<svg {...p}><path d="m6 9 6 6 6-6"/></svg>);
    case "arrowLeft": return (<svg {...p}><path d="M19 12H5M12 19l-7-7 7-7"/></svg>);
    case "sun": return (<svg {...p}><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>);
    case "moon": return (<svg {...p}><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>);
    case "mail": return (<svg {...p}><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>);
    case "signal": return (<svg {...p}><path d="M4 11a8 8 0 0 1 16 0M7 12a5 5 0 0 1 10 0M12 18.5a2 2 0 0 0 2-2 2 2 0 0 0-4 0 2 2 0 0 0 2 2z"/></svg>);
    case "webhook": return (<svg {...p}><path d="M9 7a4 4 0 1 1 5 4l-2.5 4.5"/><path d="M7.5 11.5 5 16a4 4 0 1 0 4 4h5"/><path d="M16 11a4 4 0 1 1 2 7h-2"/></svg>);
    case "pulse": return (<svg {...p}><path d="M2 12h4l2.5-7 4 14L15 12h7"/></svg>);
    case "heart": return (<svg {...p}><path d="M12 20s-7-4.3-9.3-9C1.3 8.4 2.6 5 6 5c2 0 3.2 1.2 4 2.4C10.8 6.2 12 5 14 5c3.4 0 4.7 3.4 3.3 6-2.3 4.7-9.3 9-9.3 9z"/></svg>);
    case "logout": return (<svg {...p}><path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="M10 17l5-5-5-5M15 12H3"/></svg>);
    case "lock": return (<svg {...p}><rect x="4" y="11" width="16" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>);
    case "filter": return (<svg {...p}><path d="M3 5h18l-7 8v6l-4-2v-4z"/></svg>);
    case "refresh": return (<svg {...p}><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 4v4h-4"/></svg>);
    case "external": return (<svg {...p}><path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/></svg>);
    case "info": return (<svg {...p}><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>);
    case "copy": return (<svg {...p}><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></svg>);
    case "download": return (<svg {...p}><path d="M12 4v12M7 11l5 5 5-5"/><path d="M5 20h14"/></svg>);
    case "calendar": return (<svg {...p}><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 9h18M8 3v4M16 3v4"/></svg>);
    default: return (<svg {...p}><circle cx="12" cy="12" r="9"/></svg>);
  }
}

/* Cairn logo — three stacked stones (simple ellipses), nudged like a real cairn */
function CairnMark({ size = 26, color }) {
  const c = color || "var(--accent)";
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <ellipse cx="16.6" cy="9"  rx="6.2" ry="3.1" fill={c} opacity="0.95"/>
      <ellipse cx="15.2" cy="16" rx="8.4" ry="3.7" fill={c} opacity="0.78"/>
      <ellipse cx="16.4" cy="23.4" rx="10.2" ry="4.2" fill={c} opacity="0.6"/>
    </svg>
  );
}

Object.assign(window, { Icon, CairnMark });
