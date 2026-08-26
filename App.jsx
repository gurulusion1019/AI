import React, { useState, useRef, useEffect } from "react";

const API = "http://localhost:5000/api";

/* ─────────────────────────────────────────────────────────────
   Design tokens
   Palette is built from Accenture purple (#A100FF) rather than the
   default indigo. Purple is reserved for ACTIVE / accent only, so it
   stays meaningful; the deep aubergine ink carries the chrome.
   Mono type is used for stage names, counts and ids — this is a data
   tool, and the machinery should read like machinery.
   ───────────────────────────────────────────────────────────── */
const T = {
  ink:       "#160F22",
  inkSoft:   "#2A2036",
  purple:    "#A100FF",
  purpleDim: "#7B2FD4",
  purpleWash:"rgba(161,0,255,0.08)",
  canvas:    "#F6F4FA",
  surface:   "#FFFFFF",
  line:      "#E5E0EE",
  lineSoft:  "#EFEBF5",
  text:      "#1B1526",
  textSoft:  "#5C5470",
  textFaint: "#8C85A0",
  teal:      "#00937C",
  amber:     "#B26B00",
  mono:      "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace",
  sans:      "'Segoe UI', system-ui, -apple-system, sans-serif",
};

const STATE_LABELS = {
  NOT_STARTED:    "Not started",
  PROJECT_INIT:   "Project setup",
  RAW_DATA:       "Raw data upload",
  MAPPING_FILES:  "Mapping files",
  PROTOCOL:       "Protocol upload",
  CLARIFY:        "Clarifying",
  MAPPING_REVIEW: "Mapping review",
  ERROR_REVIEW:   "Error review",
  MODEL_BUILD:    "Building model",
  EXPORT:         "Export ready",
  COMPLETE:       "Complete",
};

/* What the agent is actually doing, per stage — shown instead of a
   generic "Thinking…". A model build can run for minutes; naming the
   work is the difference between "frozen" and "working". */
const STATE_ACTIVITY = {
  PROJECT_INIT:   "Setting up the project",
  RAW_DATA:       "Reading your data files",
  MAPPING_FILES:  "Reading mapping files",
  PROTOCOL:       "Extracting measures from the protocol",
  CLARIFY:        "Working through open questions",
  MAPPING_REVIEW: "Matching columns across tables",
  ERROR_REVIEW:   "Checking data quality",
  MODEL_BUILD:    "Generating DAX and building the model",
  EXPORT:         "Packaging your dashboard",
  COMPLETE:       "Finishing up",
};

const STEP_ORDER = [
  "PROJECT_INIT","RAW_DATA","MAPPING_FILES","PROTOCOL",
  "CLARIFY","MAPPING_REVIEW","ERROR_REVIEW","MODEL_BUILD","EXPORT","COMPLETE"
];

/* Practical constraints shown on the landing page, so users know what to
   prepare and how their data is handled before they upload anything. */
const FACTS = [
  { k:"Formats",  v:"Excel (.xlsx, .xls), CSV, Word, PDF, TXT, JSON" },
  { k:"Limits",   v:"512 MB per upload. Source files ship with the project, so row counts aren\u2019t capped \u2014 only embedded fallback tables are limited to 10,000 rows" },
  { k:"Output",   v:"Power BI project (.pbip), Excel workbook, TMDL JSON, audit log, PDF summary" },
  { k:"Security", v:"Files stay within Accenture\u2019s environment. Only column names and statistics are sent to the model \u2014 never your data rows" },
];

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function fileIcon(name) {
  const ext = name.split(".").pop().toLowerCase();
  if (["xlsx","xls"].includes(ext)) return "▦";
  if (ext === "csv")                return "▤";
  if (ext === "pdf")                return "▣";
  if (["doc","docx"].includes(ext)) return "▧";
  if (ext === "json")               return "◈";
  return "▪";
}

function renderMarkdown(text, onOptionClick) {
  const lines = text.split("\n");
  const elements = [];
  let listItems = [];
  let key = 0;

  function flushList() {
    if (listItems.length) {
      elements.push(
        <ul key={key++} style={{ margin:"6px 0", paddingLeft:18 }}>
          {listItems.map((li,i) => (
            <li key={i} style={{ marginBottom:3, lineHeight:1.65 }}
                dangerouslySetInnerHTML={{ __html: li }}/>
          ))}
        </ul>
      );
      listItems = [];
    }
  }

  function inlineFormat(str) {
    return str
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\*(.+?)\*/g, "<em>$1</em>")
      .replace(/`(.+?)`/g,
        `<code style='background:${T.purpleWash};color:${T.purpleDim};padding:1px 5px;border-radius:4px;font-size:0.9em;font-family:${T.mono}'>$1</code>`);
  }

  for (const line of lines) {
    const trimmed = line.trim();

    if (!trimmed) { flushList(); continue; }

    if (trimmed.startsWith("# ")) {
      flushList();
      elements.push(
        <div key={key++} style={{
          fontSize:15, fontWeight:600, margin:"10px 0 6px", color:T.text
        }} dangerouslySetInnerHTML={{ __html: inlineFormat(trimmed.slice(2)) }}/>
      );
      continue;
    }
    if (trimmed.startsWith("## ")) {
      flushList();
      elements.push(
        <div key={key++} style={{
          fontSize:13.5, fontWeight:600, margin:"8px 0 4px", color:T.text
        }} dangerouslySetInnerHTML={{ __html: inlineFormat(trimmed.slice(3)) }}/>
      );
      continue;
    }

    /* Lettered choices — the agent asking the user to decide.
       These are the moments the whole flow hinges on, so they get
       real affordance rather than being buried in prose. */
    const optionMatch = trimmed.match(/^([A-E])\)\s+(.+)/);
    if (optionMatch) {
      flushList();
      const letter = optionMatch[1];
      const label  = optionMatch[2];
      const isRec  = label.toLowerCase().includes("recommended");
      elements.push(
        <button key={key++}
          onClick={() => { if (onOptionClick) onOptionClick(letter); }}
          style={{
            display:"flex", alignItems:"center", gap:10, width:"100%",
            textAlign:"left", margin:"5px 0", padding:"9px 12px",
            background: isRec ? T.purpleWash : T.surface,
            border: `1px solid ${isRec ? "rgba(161,0,255,0.35)" : T.line}`,
            borderRadius:8, cursor:"pointer",
            font:"inherit", fontSize:13,
            transition:"background .12s, border-color .12s, transform .12s"
          }}
          onMouseEnter={ev => {
            ev.currentTarget.style.background = "rgba(161,0,255,0.13)";
            ev.currentTarget.style.borderColor = T.purple;
          }}
          onMouseLeave={ev => {
            ev.currentTarget.style.background = isRec ? T.purpleWash : T.surface;
            ev.currentTarget.style.borderColor = isRec ? "rgba(161,0,255,0.35)" : T.line;
          }}
        >
          <span style={{
            width:22, height:22, borderRadius:6, flexShrink:0,
            background: isRec ? T.purple : T.lineSoft,
            color: isRec ? "#fff" : T.textSoft,
            display:"flex", alignItems:"center", justifyContent:"center",
            fontSize:11, fontWeight:700, fontFamily:T.mono
          }}>{letter}</span>
          <span dangerouslySetInnerHTML={{ __html: inlineFormat(label) }}
                style={{ color:T.text, flex:1, lineHeight:1.5 }}/>
          {isRec && (
            <span style={{
              fontSize:9.5, fontWeight:600, color:T.purpleDim,
              background:T.purpleWash, borderRadius:4,
              padding:"2px 6px", letterSpacing:"0.04em",
              textTransform:"uppercase", fontFamily:T.mono, flexShrink:0
            }}>Suggested</span>
          )}
        </button>
      );
      continue;
    }

    const bulletMatch = trimmed.match(/^[•\-\*]\s+(.+)/);
    if (bulletMatch) { listItems.push(inlineFormat(bulletMatch[1])); continue; }

    const numberedMatch = trimmed.match(/^\d+\.\s+(.+)/);
    if (numberedMatch) { listItems.push(inlineFormat(numberedMatch[1])); continue; }

    flushList();
    elements.push(
      <p key={key++} style={{ margin:"5px 0", lineHeight:1.65 }}
         dangerouslySetInnerHTML={{ __html: inlineFormat(trimmed) }}/>
    );
  }
  flushList();
  return elements;
}

function FilePreviewCard({ file, uploaded }) {
  return (
    <div style={{
      display:"inline-flex", alignItems:"center", gap:10,
      background: T.surface,
      border:`1px solid ${uploaded ? "rgba(0,147,124,0.3)" : T.line}`,
      borderRadius:8, padding:"8px 12px",
      maxWidth:250, marginBottom:4
    }}>
      <span style={{
        fontSize:15, flexShrink:0, color:T.purpleDim, fontFamily:T.mono
      }}>{fileIcon(file.name)}</span>
      <div style={{ minWidth:0 }}>
        <div style={{
          fontSize:12, fontWeight:600, color:T.text,
          whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis"
        }}>{file.name}</div>
        <div style={{
          fontSize:10.5, color:T.textFaint, marginTop:2, fontFamily:T.mono
        }}>
          {formatBytes(file.size)}
          {uploaded && <span style={{ color:T.teal, marginLeft:6 }}>uploaded</span>}
        </div>
      </div>
    </div>
  );
}

/* ── SIGNATURE ELEMENT ────────────────────────────────────────
   The pipeline rail. This product is a staged build, not a chat,
   so the stages are permanently visible rather than a strip that
   scrolls out of view. During a long model build the user can see
   exactly where the agent is.
   ───────────────────────────────────────────────────────────── */
function PipelineRail({ state, collapsed, onToggle }) {
  const idx = STEP_ORDER.indexOf(state);
  const W = collapsed ? 56 : 196;

  return (
    <nav aria-label="Build progress" style={{
      width:W, flexShrink:0, background:T.ink,
      padding:"14px 0", overflowY:"auto", overflowX:"hidden",
      transition:"width .18s ease",
      display:"flex", flexDirection:"column"
    }}>
      {/* Header row: label + collapse toggle */}
      <div style={{
        display:"flex", alignItems:"center",
        justifyContent: collapsed ? "center" : "space-between",
        padding: collapsed ? "0 0 12px" : "0 12px 12px 18px"
      }}>
        {!collapsed && (
          <span style={{
            fontSize:9.5, letterSpacing:"0.14em", textTransform:"uppercase",
            color:"rgba(255,255,255,0.4)", fontFamily:T.mono
          }}>Pipeline</span>
        )}
        <button
          onClick={onToggle}
          aria-label={collapsed ? "Expand pipeline" : "Collapse pipeline"}
          title={collapsed ? "Expand" : "Collapse"}
          style={{
            width:26, height:26, borderRadius:6, flexShrink:0,
            background:"transparent", border:"1px solid rgba(255,255,255,0.15)",
            color:"rgba(255,255,255,0.55)", cursor:"pointer",
            fontSize:12, lineHeight:1, fontFamily:T.mono,
            display:"flex", alignItems:"center", justifyContent:"center",
            transition:"border-color .12s, color .12s"
          }}
          onMouseEnter={ev => {
            ev.currentTarget.style.borderColor = T.purple;
            ev.currentTarget.style.color = "#fff";
          }}
          onMouseLeave={ev => {
            ev.currentTarget.style.borderColor = "rgba(255,255,255,0.15)";
            ev.currentTarget.style.color = "rgba(255,255,255,0.55)";
          }}
        >{collapsed ? "»" : "«"}</button>
      </div>

      {STEP_ORDER.map((s, i) => {
        const done   = i < idx;
        const active = i === idx;
        return (
          <div key={s}
            title={collapsed ? STATE_LABELS[s] : undefined}
            style={{
              display:"flex", alignItems:"center", gap:11,
              padding: collapsed ? "9px 0" : "9px 18px",
              justifyContent: collapsed ? "center" : "flex-start",
              position:"relative",
              background: active ? "rgba(161,0,255,0.16)" : "transparent",
              borderLeft: `2px solid ${active ? T.purple : "transparent"}`
            }}>
            {/* connector line between markers (full mode only) */}
            {!collapsed && i < STEP_ORDER.length - 1 && (
              <span style={{
                position:"absolute", left:26, top:"50%", width:1, height:"100%",
                background: done ? "rgba(0,147,124,0.5)" : "rgba(255,255,255,0.1)"
              }}/>
            )}
            <span style={{
              width:15, height:15, borderRadius:"50%", flexShrink:0, zIndex:1,
              display:"flex", alignItems:"center", justifyContent:"center",
              fontSize:8.5, fontWeight:700, fontFamily:T.mono,
              background: done ? T.teal : active ? T.purple : "transparent",
              border: done || active
                ? "none" : "1px solid rgba(255,255,255,0.22)",
              color: done || active ? "#fff" : "rgba(255,255,255,0.35)",
              boxShadow: active ? `0 0 0 4px rgba(161,0,255,0.22)` : "none"
            }}>{done ? "\u2713" : i + 1}</span>
            {!collapsed && (
              <span style={{
                fontSize:11.5, lineHeight:1.3,
                color: active ? "#fff"
                     : done ? "rgba(255,255,255,0.62)"
                     : "rgba(255,255,255,0.34)",
                fontWeight: active ? 600 : 400
              }}>{STATE_LABELS[s]}</span>
            )}
          </div>
        );
      })}
    </nav>
  );
}

function Message({ msg, onOptionClick }) {
  const isUser = msg.role === "user";
  const isFile = msg.type === "file";

  if (isFile) {
    return (
      <div style={{
        display:"flex", justifyContent:"flex-end",
        alignItems:"flex-start", gap:9, marginBottom:10
      }}>
        <FilePreviewCard file={msg.file} uploaded={true}/>
      </div>
    );
  }

  return (
    <div style={{
      display:"flex",
      justifyContent: isUser ? "flex-end" : "flex-start",
      marginBottom:16, alignItems:"flex-start", gap:10
    }}>
      {!isUser && (
        <div style={{
          width:26, height:26, borderRadius:7, flexShrink:0,
          background:T.ink, display:"flex",
          alignItems:"center", justifyContent:"center",
          fontSize:9.5, fontWeight:700, color:"#fff",
          marginTop:3, letterSpacing:"0.04em", fontFamily:T.mono
        }}>AI</div>
      )}
      <div style={{
        maxWidth:"76%",
        background: isUser ? T.ink : T.surface,
        border: isUser ? "none" : `1px solid ${T.line}`,
        borderRadius: isUser ? "12px 12px 3px 12px" : "3px 12px 12px 12px",
        padding:"11px 15px",
        fontSize:13.5,
        color: isUser ? "rgba(255,255,255,0.95)" : T.text,
        boxShadow: isUser ? "none" : "0 1px 2px rgba(22,15,34,0.05)"
      }}>
        {isUser
          ? <p style={{ margin:0, lineHeight:1.6 }}>{msg.content}</p>
          : renderMarkdown(msg.content, onOptionClick)
        }
      </div>
    </div>
  );
}

function DownloadPanel({ sessionId }) {
  const exports = [
    { label:"Power BI package", key:"powerbi", hint:"model.bim · .pbip · guide" },
    { label:"Excel workbook",   key:"excel",   hint:"tables · measures · log" },
    { label:"TMDL JSON",        key:"tmdl",    hint:"Analysis Services / XMLA" },
    { label:"Audit log",        key:"audit",   hint:"mapping + error decisions" },
    { label:"PDF report",       key:"report",  hint:"completion summary" },
  ];
  return (
    <section style={{
      border:`1px solid ${T.line}`, borderRadius:10,
      overflow:"hidden", background:T.surface
    }}>
      <header style={{
        padding:"10px 13px", background:"rgba(0,147,124,0.07)",
        borderBottom:`1px solid ${T.line}`,
        display:"flex", alignItems:"center", gap:7
      }}>
        <span style={{ color:T.teal, fontSize:12 }}>✓</span>
        <span style={{ fontSize:11.5, fontWeight:600, color:T.text }}>
          Ready to download
        </span>
      </header>
      <div style={{ padding:7, display:"flex", flexDirection:"column", gap:3 }}>
        {exports.map(e => (
          <a key={e.key}
            href={`${API}/export/${sessionId}/${e.key}`}
            style={{
              display:"flex", alignItems:"center", gap:9,
              padding:"9px 11px", border:`1px solid transparent`,
              borderRadius:7, textDecoration:"none", color:T.text,
              transition:"background .12s, border-color .12s"
            }}
            onMouseEnter={ev => {
              ev.currentTarget.style.background = T.purpleWash;
              ev.currentTarget.style.borderColor = "rgba(161,0,255,0.28)";
            }}
            onMouseLeave={ev => {
              ev.currentTarget.style.background = "transparent";
              ev.currentTarget.style.borderColor = "transparent";
            }}
          >
            <div style={{ flex:1, minWidth:0 }}>
              <div style={{ fontSize:12, fontWeight:600 }}>{e.label}</div>
              <div style={{
                fontSize:10, color:T.textFaint, marginTop:2, fontFamily:T.mono
              }}>{e.hint}</div>
            </div>
            <span style={{ fontSize:13, color:T.purple }}>↓</span>
          </a>
        ))}
      </div>
    </section>
  );
}

function ContextPanel({ ctx }) {
  if (!ctx) return null;
  const items = [
    { label:"Tables",   value:((ctx.raw_files && ctx.raw_files.length) || 0) +
                          ((ctx.mapping_files && ctx.mapping_files.length) || 0) },
    { label:"Measures", value:ctx.measures_count||0 },
    { label:"Mappings", value:ctx.mappings_count||0 },
    { label:"Open issues", value:ctx.errors_pending||0, warn:(ctx.errors_pending||0)>0 },
  ];
  return (
    <section style={{
      border:`1px solid ${T.line}`, borderRadius:10,
      overflow:"hidden", background:T.surface
    }}>
      <div style={{
        display:"grid", gridTemplateColumns:"1fr 1fr",
        gap:1, background:T.lineSoft
      }}>
        {items.map(it => (
          <div key={it.label} style={{ padding:"12px 13px", background:T.surface }}>
            <div style={{
              fontSize:21, fontWeight:600, lineHeight:1,
              fontFamily:T.mono,
              color: it.warn ? T.amber : T.text
            }}>{it.value}</div>
            <div style={{
              fontSize:9.5, color:T.textFaint, marginTop:5,
              letterSpacing:"0.06em", textTransform:"uppercase", fontFamily:T.mono
            }}>{it.label}</div>
          </div>
        ))}
      </div>
      {ctx.model_built && (
        <div style={{
          padding:"8px 13px", fontSize:11, color:T.teal,
          borderTop:`1px solid ${T.lineSoft}`,
          display:"flex", alignItems:"center", gap:6
        }}>
          <span>✓</span> Semantic model built
        </div>
      )}
    </section>
  );
}

function UploadedFilesList({ files }) {
  if (!files.size) return null;
  return (
    <section>
      <div style={{
        fontSize:9.5, fontWeight:600, color:T.textFaint,
        textTransform:"uppercase", letterSpacing:"0.1em",
        marginBottom:8, fontFamily:T.mono
      }}>Files · {files.size}</div>
      <div style={{ display:"flex", flexDirection:"column", gap:3 }}>
        {[...files].map(name => (
          <div key={name} style={{
            display:"flex", alignItems:"center", gap:8,
            padding:"7px 9px", background:T.surface,
            border:`1px solid ${T.line}`, borderRadius:7,
            fontSize:11, color:T.textSoft
          }}>
            <span style={{
              fontSize:12, color:T.purpleDim, fontFamily:T.mono, flexShrink:0
            }}>{fileIcon(name)}</span>
            <span style={{
              flex:1, whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis"
            }}>{name}</span>
            <span style={{ color:T.teal, flexShrink:0, fontSize:10 }}>✓</span>
          </div>
        ))}
      </div>
    </section>
  );
}

export default function App() {
  const [sessionId, setSessionId]       = useState(null);
  const [messages, setMessages]         = useState([]);
  const [state, setState]               = useState("NOT_STARTED");
  const [ctx, setCtx]                   = useState(null);
  const [projectName, setProjectName]   = useState("");
  const [input, setInput]               = useState("");
  const [loading, setLoading]           = useState(false);
  const [uploadedFiles, setUploadedFiles] = useState(new Set());
  const [railCollapsed, setRailCollapsed] = useState(false);
  const chatEndRef  = useRef(null);
  const fileInputRef = useRef(null);

  useEffect(() => {
    if (chatEndRef.current) {
      chatEndRef.current.scrollIntoView({ behavior:"smooth" });
    }
  }, [messages, loading]);

  const addMsg = (role, content, extra={}) =>
    setMessages(prev => [...prev, { role, content, ...extra }]);

  const handleApiResponse = (data) => {
    if (data.reply) addMsg("assistant", data.reply);
    if (data.state) setState(data.state);
    if (data.context) setCtx(data.context);
  };

  const startProject = async () => {
    if (!projectName.trim()) return;
    setLoading(true);
    try {
      const r1  = await fetch(`${API}/session`, { method:"POST" });
      const sess = await r1.json();
      setSessionId(sess.session_id);
      setState(sess.state);
      const r2  = await fetch(`${API}/chat/${sess.session_id}`, {
        method:"POST",
        headers:{ "Content-Type":"application/json" },
        body: JSON.stringify({ message: projectName })
      });
      handleApiResponse(await r2.json());
    } catch(e) { addMsg("assistant", `Couldn't start the project: ${e.message}`); }
    finally { setLoading(false); }
  };

  const sendMessage = async (text, files=[]) => {
    if (!sessionId) return;
    if (text) addMsg("user", text);
    setLoading(true);
    try {
      if (files.length > 0) {
        for (const file of files) {
          addMsg("user", file.name, { type:"file", file });
          const fd = new FormData();
          fd.append("file", file);
          fd.append("message", `Uploading ${file.name}`);
          const r = await fetch(`${API}/chat/${sessionId}`, { method:"POST", body:fd });
          handleApiResponse(await r.json());
          setUploadedFiles(prev => new Set([...prev, file.name]));
        }
      } else if (text) {
        const r = await fetch(`${API}/chat/${sessionId}`, {
          method:"POST",
          headers:{ "Content-Type":"application/json" },
          body: JSON.stringify({ message: text })
        });
        handleApiResponse(await r.json());
      }
    } catch(e) { addMsg("assistant", `Something went wrong: ${e.message}`); }
    finally { setLoading(false); setInput(""); }
  };

  const handleFileSelect = (ev) => {
    const files = Array.from(ev.target.files||[]);
    const newFiles = files.filter(f => !uploadedFiles.has(f.name));
    if (newFiles.length > 0) sendMessage("", newFiles);
    ev.target.value = "";
  };

  const handleSend = () => {
    if (!input.trim()) return;
    sendMessage(input);
  };

  const handleKeyDown = (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      handleSend();
    }
  };

  const handleOptionClick = (letter) => sendMessage(letter);

  /* ── Landing ─────────────────────────────────────────────── */
  /* ── Landing ─────────────────────────────────────────────
     Two columns: the pitch + project name on the left, a short
     "how it works" walkthrough on the right. First-time users
     otherwise have no idea WHAT to prepare before starting —
     the walkthrough names the three inputs up front.
     ───────────────────────────────────────────────────────── */
  if (!sessionId) {
    const HOW = [
      {
        n: "01",
        title: "Upload your raw data",
        body: "The fact-level files — transactions, events, records. CSV or Excel, one or many."
      },
      {
        n: "02",
        title: "Add mapping files",
        body: "Lookup and dimension tables that give context: products, customers, regions, dates."
      },
      {
        n: "03",
        title: "Share the protocol document",
        body: "Your metrics in plain English. Add a DASHBOARD VISUALS section to say which charts you want."
      },
      {
        n: "04",
        title: "Download the .pbip",
        body: "The agent writes the DAX, plans the visuals and hands back a Power BI project that opens as-is."
      },
    ];

    return (
      <div style={{
        /* minHeight:100vh guarantees the dark canvas reaches the bottom of
           the window even if an ancestor has no height set; height:100% lets
           it shrink to a shorter frame when the portal provides one. */
        height:"100%", minHeight:"100vh", overflowY:"auto",
        display:"flex", alignItems:"center", justifyContent:"center",
        padding:"48px 32px", background:T.ink, fontFamily:T.sans,
        boxSizing:"border-box"
      }}>
        <div style={{
          width:"100%", maxWidth:1000,
          display:"flex", gap:64, flexWrap:"wrap",
          alignItems:"flex-start", justifyContent:"center"
        }}>

          {/* ── Left: pitch + start ───────────────────────────── */}
          <div style={{ flex:"1 1 380px", maxWidth:460 }}>
            <div style={{
              fontSize:9.5, letterSpacing:"0.18em", textTransform:"uppercase",
              color:"rgba(255,255,255,0.45)", fontFamily:T.mono, marginBottom:16
            }}>Accenture · SynOps</div>

            <h1 style={{
              fontSize:46, fontWeight:700, lineHeight:1.05,
              margin:"0 0 12px", color:"#fff", letterSpacing:"-0.03em"
            }}>
              Agentic <span style={{ color:T.purple }}>Reporting</span>
            </h1>

            <p style={{
              fontSize:19, fontWeight:600, lineHeight:1.35,
              margin:"0 0 16px", color:"rgba(255,255,255,0.85)",
              letterSpacing:"-0.01em"
            }}>
              Describe the dashboard. Get the Power BI file.
            </p>

            <p style={{
              fontSize:14, lineHeight:1.65, margin:"0 0 32px",
              color:"rgba(255,255,255,0.6)", maxWidth:400
            }}>
              Upload your data and a plain-English protocol. The agent extracts
              your measures, writes the DAX, plans the visuals and hands back a
              ready-to-open <code style={{
                fontFamily:T.mono, fontSize:12.5, color:"rgba(255,255,255,0.85)"
              }}>.pbip</code> project.
            </p>

            <label style={{
              display:"block", fontSize:9.5, letterSpacing:"0.1em",
              textTransform:"uppercase", color:"rgba(255,255,255,0.45)",
              fontFamily:T.mono, marginBottom:8
            }} htmlFor="projectName">Project name</label>

            <div style={{ display:"flex", gap:9 }}>
              <input
                id="projectName"
                value={projectName}
                onChange={ev => setProjectName(ev.target.value)}
                onKeyDown={ev => ev.key==="Enter" && startProject()}
                placeholder="Q2 Sales Performance"
                autoFocus
                style={{
                  flex:1, fontSize:14, padding:"11px 14px",
                  borderRadius:9, color:"#fff",
                  background:"rgba(255,255,255,0.07)",
                  border:"1px solid rgba(255,255,255,0.18)",
                  fontFamily:T.sans, outline:"none",
                  transition:"border-color .15s, box-shadow .15s"
                }}
                onFocus={ev => {
                  ev.target.style.borderColor = T.purple;
                  ev.target.style.boxShadow = "0 0 0 3px rgba(161,0,255,0.22)";
                }}
                onBlur={ev => {
                  ev.target.style.borderColor = "rgba(255,255,255,0.18)";
                  ev.target.style.boxShadow = "none";
                }}
              />
              <button
                onClick={startProject}
                disabled={loading || !projectName.trim()}
                style={{
                  padding:"0 22px", fontSize:13.5, fontWeight:600,
                  borderRadius:9, border:"none", cursor:"pointer",
                  background: projectName.trim() ? T.purple : "rgba(255,255,255,0.12)",
                  color: projectName.trim() ? "#fff" : "rgba(255,255,255,0.4)",
                  fontFamily:T.sans,
                  transition:"background .15s"
                }}
              >{loading ? "Starting\u2026" : "Start"}</button>
            </div>

            {/* Practical constraints — what to prepare and how data is
                handled. Kept in the LEFT column so the right column stays
                a single readable sequence of steps. */}
            <dl style={{
              marginTop:30, paddingTop:20,
              borderTop:"1px solid rgba(255,255,255,0.1)",
              display:"grid", gridTemplateColumns:"auto 1fr",
              columnGap:16, rowGap:12, margin:0, marginTop:30
            }}>
              {FACTS.map(f => (
                <React.Fragment key={f.k}>
                  <dt style={{
                    fontSize:9.5, letterSpacing:"0.08em", textTransform:"uppercase",
                    color:"rgba(255,255,255,0.38)", fontFamily:T.mono,
                    paddingTop:2, whiteSpace:"nowrap"
                  }}>{f.k}</dt>
                  <dd style={{
                    margin:0, fontSize:12, lineHeight:1.55,
                    color:"rgba(255,255,255,0.55)"
                  }}>{f.v}</dd>
                </React.Fragment>
              ))}
            </dl>
          </div>

          {/* ── Right: how it works ───────────────────────────── */}
          <div style={{ flex:"1 1 340px", maxWidth:420, paddingTop:6 }}>
            <div style={{
              fontSize:9.5, letterSpacing:"0.14em", textTransform:"uppercase",
              color:"rgba(255,255,255,0.4)", fontFamily:T.mono,
              marginBottom:18, paddingBottom:12,
              borderBottom:"1px solid rgba(255,255,255,0.1)"
            }}>How it works</div>

            {HOW.map((step, i) => (
              <div key={step.n} style={{
                display:"flex", gap:14, position:"relative",
                paddingBottom: i < HOW.length - 1 ? 22 : 0
              }}>
                {/* connector line down the numbers */}
                {i < HOW.length - 1 && (
                  <span style={{
                    position:"absolute", left:13, top:26, bottom:4, width:1,
                    background:"rgba(255,255,255,0.12)"
                  }}/>
                )}
                <span style={{
                  width:27, height:27, borderRadius:8, flexShrink:0, zIndex:1,
                  background:"rgba(161,0,255,0.14)",
                  border:"1px solid rgba(161,0,255,0.4)",
                  color:T.purple, fontFamily:T.mono, fontSize:10,
                  fontWeight:700, display:"flex",
                  alignItems:"center", justifyContent:"center"
                }}>{step.n}</span>
                <div style={{ minWidth:0 }}>
                  <div style={{
                    fontSize:13.5, fontWeight:600, color:"#fff",
                    marginBottom:4, letterSpacing:"-0.01em"
                  }}>{step.title}</div>
                  <div style={{
                    fontSize:12.5, lineHeight:1.6,
                    color:"rgba(255,255,255,0.52)"
                  }}>{step.body}</div>
                </div>
              </div>
            ))}

            <div style={{
              marginTop:26, padding:"12px 14px", borderRadius:9,
              background:"rgba(255,255,255,0.04)",
              border:"1px solid rgba(255,255,255,0.09)",
              fontSize:12, lineHeight:1.6, color:"rgba(255,255,255,0.5)"
            }}>
              <span style={{ color:T.purple, fontFamily:T.mono, fontSize:11 }}>Tip</span>
              {"  "}No protocol document? Say <strong style={{
                color:"rgba(255,255,255,0.8)", fontWeight:600
              }}>generate</strong> and the agent will propose measures from your
              data for you to approve.
            </div>

          </div>

        </div>
      </div>
    );
  }

  const isComplete = state === "COMPLETE";
  const activity = STATE_ACTIVITY[state] || "Working";

  /* ── Workspace ───────────────────────────────────────────── */
  return (
    <div style={{
      display:"flex", flexDirection:"column",
      /* height:100% (not 100vh) so it works standalone AND when the
         SynOps portal embeds this in a fixed-height frame. overflow
         hidden keeps the PAGE from scrolling — only the message list
         scrolls, so the pipeline rail and context panel stay put. */
      height:"100%", minHeight:0, overflow:"hidden",
      fontFamily:T.sans, color:T.text
    }}>
      {/* Header */}
      <header style={{
        background:T.ink, padding:"0 20px",
        height:52, display:"flex", alignItems:"center",
        justifyContent:"space-between",
        borderBottom:`2px solid ${T.purple}`
      }}>
        <div style={{ display:"flex", alignItems:"center", gap:11 }}>
          <span style={{
            width:7, height:7, borderRadius:"50%", background:T.purple,
            boxShadow:`0 0 0 3px rgba(161,0,255,0.25)`
          }}/>
          <span style={{
            color:"#fff", fontWeight:700, fontSize:16.5, letterSpacing:"-0.015em"
          }}>Agentic <span style={{ color:T.purple }}>Reporting</span></span>
        </div>
        <div style={{
          fontSize:11, color:"rgba(255,255,255,0.6)", fontFamily:T.mono,
          border:"1px solid rgba(255,255,255,0.15)",
          borderRadius:6, padding:"4px 10px"
        }}>
          {(ctx && ctx.project_name) || "New project"}
        </div>
      </header>

      <div style={{ display:"flex", flex:1, minHeight:0, overflow:"hidden" }}>
        <PipelineRail
          state={state}
          collapsed={railCollapsed}
          onToggle={() => setRailCollapsed(v => !v)}
        />

        {/* Conversation */}
        <main style={{
          flex:1, display:"flex", flexDirection:"column",
          minWidth:0, minHeight:0
        }}>
          <div style={{
            flex:1, overflowY:"auto", padding:"22px 26px",
            display:"flex", flexDirection:"column", minHeight:0,
            background:T.canvas
          }}>
            {messages.length === 0 && (
              <div style={{
                flex:1, display:"flex", alignItems:"center",
                justifyContent:"center", flexDirection:"column", gap:10
              }}>
                <div style={{
                  fontSize:10, letterSpacing:"0.12em", textTransform:"uppercase",
                  color:T.purple, fontFamily:T.mono
                }}>Ready</div>
                <div style={{
                  fontSize:14, color:T.textSoft, textAlign:"center", maxWidth:320
                }}>
                  Attach your raw data files to begin the build.
                </div>
              </div>
            )}

            {messages.map((m,i) => (
              <Message key={i} msg={m} onOptionClick={handleOptionClick}/>
            ))}

            {/* Working state — names the stage instead of "Thinking…" */}
            {loading && (
              <div style={{
                display:"flex", alignItems:"flex-start", gap:10, marginBottom:16
              }}>
                <div style={{
                  width:26, height:26, borderRadius:7, background:T.ink,
                  display:"flex", alignItems:"center", justifyContent:"center",
                  fontSize:9.5, fontWeight:700, color:"#fff",
                  marginTop:3, flexShrink:0, fontFamily:T.mono
                }}>AI</div>
                <div style={{
                  background:T.surface, border:`1px solid ${T.line}`,
                  borderRadius:"3px 12px 12px 12px",
                  padding:"11px 15px",
                  display:"flex", alignItems:"center", gap:10
                }}>
                  <span style={{ display:"flex", gap:3 }}>
                    {[0,1,2].map(i => (
                      <span key={i} style={{
                        width:5, height:5, borderRadius:"50%", background:T.purple,
                        animation:`pulse 1.1s ease-in-out ${i*0.16}s infinite`
                      }}/>
                    ))}
                  </span>
                  <span style={{ fontSize:12.5, color:T.textSoft }}>{activity}…</span>
                  <style>{`
                    @keyframes pulse {
                      0%,100% { opacity:.25; transform:translateY(0) }
                      50%     { opacity:1;   transform:translateY(-2px) }
                    }
                    @media (prefers-reduced-motion: reduce) {
                      * { animation:none !important; transition:none !important }
                    }
                  `}</style>
                </div>
              </div>
            )}
            <div ref={chatEndRef}/>
          </div>

          {/* Composer */}
          <div style={{
            borderTop:`1px solid ${T.line}`,
            padding:"13px 18px", background:T.surface,
            display:"flex", gap:9, alignItems:"flex-end"
          }}>
            <input type="file" ref={fileInputRef} multiple
              accept=".csv,.xlsx,.xls,.txt,.md,.pdf,.docx,.json"
              onChange={handleFileSelect} style={{ display:"none"}}/>
            <button
              onClick={() => { if (fileInputRef.current) fileInputRef.current.click(); }}
              disabled={loading}
              title="Attach data files"
              style={{
                width:38, height:38, flexShrink:0, borderRadius:9,
                border:`1px solid ${T.line}`, background:T.surface,
                color:T.textSoft, fontSize:15, cursor:"pointer",
                transition:"border-color .12s, color .12s"
              }}
              onMouseEnter={ev => {
                ev.currentTarget.style.borderColor = T.purple;
                ev.currentTarget.style.color = T.purple;
              }}
              onMouseLeave={ev => {
                ev.currentTarget.style.borderColor = T.line;
                ev.currentTarget.style.color = T.textSoft;
              }}
            >+</button>

            <textarea
              value={input}
              onChange={ev => setInput(ev.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Reply, or ask for a change…"
              disabled={loading}
              rows={1}
              style={{
                flex:1, resize:"none", fontSize:13.5, lineHeight:1.5,
                padding:"10px 14px", borderRadius:9,
                border:`1px solid ${T.line}`, background:T.canvas,
                color:T.text, fontFamily:T.sans, outline:"none",
                transition:"border-color .15s, box-shadow .15s, background .15s"
              }}
              onFocus={ev => {
                ev.target.style.borderColor = T.purple;
                ev.target.style.boxShadow = "0 0 0 3px rgba(161,0,255,0.14)";
                ev.target.style.background = T.surface;
              }}
              onBlur={ev => {
                ev.target.style.borderColor = T.line;
                ev.target.style.boxShadow = "none";
                ev.target.style.background = T.canvas;
              }}
            />
            <button
              onClick={handleSend}
              disabled={loading || !input.trim()}
              style={{
                padding:"0 20px", height:38, fontSize:13, fontWeight:600,
                flexShrink:0, borderRadius:9, border:"none",
                cursor: input.trim() && !loading ? "pointer" : "default",
                background: input.trim() && !loading ? T.purple : T.lineSoft,
                color: input.trim() && !loading ? "#fff" : T.textFaint,
                fontFamily:T.sans, transition:"background .15s"
              }}
            >Send</button>
          </div>
        </main>

        {/* Context */}
        <aside style={{
          width:236, flexShrink:0, borderLeft:`1px solid ${T.line}`,
          overflowY:"auto", padding:14,
          display:"flex", flexDirection:"column", gap:16,
          background:T.canvas
        }}>
          {ctx && <ContextPanel ctx={ctx}/>}
          {uploadedFiles.size > 0 && <UploadedFilesList files={uploadedFiles}/>}
          {isComplete && <DownloadPanel sessionId={sessionId}/>}

          <div style={{ marginTop:"auto", paddingTop:12 }}>
            <div style={{
              fontSize:9, letterSpacing:"0.1em", textTransform:"uppercase",
              color:T.textFaint, fontFamily:T.mono, marginBottom:4
            }}>Session</div>
            <div style={{
              fontSize:10, color:T.textFaint, fontFamily:T.mono,
              wordBreak:"break-all"
            }}>{sessionId ? sessionId.slice(0,18) : ""}…</div>
          </div>
        </aside>
      </div>
    </div>
  );
}

444
