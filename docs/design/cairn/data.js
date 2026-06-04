/* Cairn mock data — drawn from DESIGN.md §4 (an example multi-user instance) */
(function () {
  const CORPORA = [
    {
      id: "photos",
      name: "Photos",
      owner: "alice",
      root: "/srv/media/photos",
      mode: "worm",                 // worm | churn
      ots: "perfile",               // none | manifest | perfile
      otsNote: "new files only",
      cadence: "Nightly · 03:00",
      cadenceSeconds: 86400,
      excludes: ["**/.thumbnails/**", "**/*.tmp", "**/@eaDir/**"],
      alerts: ["email"],
      files: 186432,
      size: "1.41 TiB",
      lastScan: "6h ago",
      lastScanFull: "Today 03:00",
      status: "alert",              // ok | attention | alert
      counts: { ok: 186429, modified: 0, missing: 1, new: 2 },
      ots_counts: { complete: 41208, pending: 2, incomplete: 0, none: 145222 },
    },
    {
      id: "tax",
      name: "Tax practice files",
      owner: "bob",
      root: "/srv/documents/tax",
      mode: "worm",
      ots: "perfile",
      otsNote: "",
      cadence: "Every 15 min",
      cadenceSeconds: 900,
      excludes: ["**/~$*", "**/.DS_Store", "**/Obsidian/**"],
      alerts: ["email"],
      files: 3204,
      size: "18.6 GiB",
      lastScan: "4 min ago",
      lastScanFull: "Today 11:42",
      status: "attention",
      counts: { ok: 3201, modified: 3, missing: 0, new: 0 },
      ots_counts: { complete: 3198, pending: 6, incomplete: 0, none: 0 },
    },
    {
      id: "roms",
      name: "Game ROM collection",
      owner: "carol",
      root: "/srv/games/roms",
      mode: "worm",
      ots: "none",
      otsNote: "tripwire only",
      cadence: "Weekly · Sun 04:00",
      cadenceSeconds: 604800,
      excludes: ["**/*.sav", "**/*.state"],
      alerts: ["email"],
      files: 8417,
      size: "342 GiB",
      lastScan: "2 days ago",
      lastScanFull: "Sun 04:00",
      status: "ok",
      counts: { ok: 8417, modified: 0, missing: 0, new: 0 },
      ots_counts: { complete: 0, pending: 0, incomplete: 0, none: 8417 },
    },
  ];

  const EVENTS = [
    { id: 1, corpus: "photos", kind: "missing", relpath: "2019/Italy/IMG_4821.jpg", at: "2h ago", atFull: "Today 09:34", ack: false },
    { id: 2, corpus: "tax",    kind: "modified", relpath: "TAX CLIENTS/Brennan LLC/2024-return.pdf", at: "4 min ago", atFull: "Today 11:42", ack: false },
    { id: 3, corpus: "tax",    kind: "modified", relpath: "TAX CLIENTS/Brennan LLC/W2-scan.pdf", at: "4 min ago", atFull: "Today 11:42", ack: false },
    { id: 4, corpus: "tax",    kind: "modified", relpath: "TAX CLIENTS/Okafor/invoice-0142.pdf", at: "4 min ago", atFull: "Today 11:42", ack: false },
    { id: 5, corpus: "photos", kind: "added",    relpath: "Alices phone/Upload/2026-05-29_173210.jpg", at: "1 day ago", atFull: "Sat 17:32", ack: true, stamped: true },
    { id: 6, corpus: "photos", kind: "added",    relpath: "Bobs Phone/Upload/2026-05-29_154411.heic", at: "1 day ago", atFull: "Sat 15:44", ack: true, stamped: true },
    { id: 7, corpus: "photos", kind: "restored", relpath: "2015/Wedding/DSC_0099.jpg", at: "3 days ago", atFull: "Thu 21:08", ack: true },
  ];

  // Corpus-detail file rows (a representative sample for the Photos corpus)
  const FILES = {
    photos: [
      { relpath: "2019/Italy/IMG_4821.jpg",            size: "4.2 MB",  status: "missing",  ots: "complete",   checked: "6h ago" },
      { relpath: "Alices phone/Upload/2026-05-29_173210.jpg", size: "3.8 MB", status: "new",  ots: "pending",    checked: "6h ago" },
      { relpath: "Bobs Phone/Upload/2026-05-29_154411.heic", size: "2.1 MB", status: "new", ots: "pending", checked: "6h ago" },
      { relpath: "2015/Wedding/DSC_0099.jpg",          size: "6.0 MB",  status: "ok",       ots: "complete",   checked: "6h ago" },
      { relpath: "2015/Wedding/DSC_0100.jpg",          size: "5.9 MB",  status: "ok",       ots: "complete",   checked: "6h ago" },
      { relpath: "2018/Japan/DSCF1042.RAF",            size: "28.4 MB", status: "ok",       ots: "complete",   checked: "6h ago" },
      { relpath: "2021/Kids/VID_20210712_birthday.mp4", size: "412 MB", status: "ok",       ots: "complete",   checked: "6h ago" },
      { relpath: "UNSORTED From Bobs Phone/scan_0031.jpg", size: "1.7 MB", status: "ok", ots: "none",       checked: "6h ago" },
      { relpath: "2009/Archive/0001.jpg",              size: "2.3 MB",  status: "ok",       ots: "none",       checked: "6h ago" },
      { relpath: "2009/Archive/0002.jpg",              size: "2.4 MB",  status: "ok",       ots: "none",       checked: "6h ago" },
    ],
    tax: [
      { relpath: "TAX CLIENTS/Brennan LLC/2024-return.pdf", size: "1.2 MB", status: "modified", ots: "pending", checked: "4 min ago" },
      { relpath: "TAX CLIENTS/Brennan LLC/W2-scan.pdf",     size: "880 KB", status: "modified", ots: "pending", checked: "4 min ago" },
      { relpath: "TAX CLIENTS/Okafor/invoice-0142.pdf",     size: "210 KB", status: "modified", ots: "pending", checked: "4 min ago" },
      { relpath: "TAX CLIENTS/Okafor/2023-return.pdf",      size: "1.4 MB", status: "ok",       ots: "complete", checked: "4 min ago" },
      { relpath: "TAX CLIENTS/Delgado/receipts-2024.pdf",   size: "3.1 MB", status: "ok",       ots: "complete", checked: "4 min ago" },
      { relpath: "Engagement Letters/2025-master.docx",     size: "94 KB",  status: "ok",       ots: "complete", checked: "4 min ago" },
    ],
    roms: [
      { relpath: "snes/Chrono Trigger (USA).sfc",       size: "4.0 MB",  status: "ok", ots: "none", checked: "2 days ago" },
      { relpath: "snes/Super Metroid (USA).sfc",        size: "3.0 MB",  status: "ok", ots: "none", checked: "2 days ago" },
      { relpath: "n64/The Legend of Zelda OOT (USA).z64", size: "32 MB", status: "ok", ots: "none", checked: "2 days ago" },
      { relpath: "gba/Pokemon Emerald (USA).gba",       size: "16 MB",   status: "ok", ots: "none", checked: "2 days ago" },
      { relpath: "genesis/Sonic the Hedgehog 2 (W).md", size: "1.0 MB",  status: "ok", ots: "none", checked: "2 days ago" },
    ],
  };

  const USERS = [
    { username: "alice",    email: "alice@example.com",    admin: true,  active: true,  base: "/srv", corpora: 1, lastLogin: "Today 08:12" },
    { username: "bob", email: "bob@example.com", admin: false, active: true,  base: "/srv/documents/tax", corpora: 1, lastLogin: "Today 11:40" },
    { username: "carol", email: "carol@example.com", admin: false, active: true,  base: "/srv/games", corpora: 1, lastLogin: "5 days ago" },
  ];

  const HEARTBEAT = { ok: true, last: "3 min ago", endpoint: "GET /healthz", monitor: "Health endpoint · polled by Uptime Kuma #85" };

  // Sample verify result (file already tracked → proof looked up internally)
  const VERIFY_SAMPLE = {
    filename: "2024-return.pdf",
    relpath: "TAX CLIENTS/Brennan LLC/2024-return.pdf",
    corpus: "Tax practice files",
    sha256: "9f2a4c1e8b7d6033a5e1c9b40f8e2d7a16c3b9e05d4a8f2c1b6e9d0a3f7c2e84",
    verified: true,
    existedBy: "2026-02-14 18:22 UTC",
    block: 831402,
    blockHash: "00000000000000000002a7c4…3f9e",
    calendars: ["alice.btc.calendar.opentimestamps.org", "bob.btc.calendar.opentimestamps.org"],
    source: "blockstream.info (explorer lookup)",
  };

  window.CAIRN = { CORPORA, EVENTS, FILES, USERS, HEARTBEAT, VERIFY_SAMPLE };
})();
