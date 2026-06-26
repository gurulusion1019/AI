import { useState, useRef, useEffect } from "react";

const API = "http://localhost:5000/api";

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

const STEP_ORDER = [
  "PROJECT_INIT","RAW_DATA","MAPPING_FILES","PROTOCOL",
  "CLARIFY","MAPPING_REVIEW","ERROR_REVIEW","MODEL_BUILD","EXPORT","COMPLETE"
];

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function fileIcon(name) {
  const ext = name.split(".").pop().toLowerCase();
  if (["xlsx","xls"].includes(ext)) return "🟢";
  if (ext === "csv") return "📊";
  if (ext === "pdf") return "🔴";
  if (["doc","docx"].includes(ext)) return "🔵";
  if (["txt","md"].includes(ext)) return "📄";
  return "📎";
}

function renderMarkdown(text, onOptionClick) {
  if (!text) return [];
  const lines = text.split("\n");
  const elements = [];
  let listItems = [];
  let key = 0;

  function flushList() {
    if (listItems.length > 0) {
      elements.push(
        <ul key={key++} style={{ margin:"6px 0", paddingLeft:20, lineHeight:1.7 }}>
          {listItems.map((item, i) => (
            <li key={i} style={{ marginBottom:2 }}
              dangerouslySetInnerHTML={{ __html: inlineFormat(item) }}/>
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
      .replace(/`(.+?)`/g, "<code style='background:rgba(0,0,0,0.06);padding:1px 5px;border-radius:4px;font-size:0.92em;font-family:monospace'>$1</code>");
  }

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      flushList();
      elements.push(<div key={key++} style={{ height:6 }}/>);
      continue;
    }
    if (trimmed.startsWith("# ")) {
      flushList();
      elements.push(
        <p key={key++} style={{ fontWeight:500, fontSize:15, margin:"8px 0 4px" }}
          dangerouslySetInnerHTML={{ __html: inlineFormat(trimmed.slice(2)) }}/>
      );
      continue;
    }
    if (trimmed.startsWith("## ")) {
      flushList();
      elements.push(
        <p key={key++} style={{ fontWeight:500, fontSize:14, margin:"6px 0 2px" }}
          dangerouslySetInnerHTML={{ __html: inlineFormat(trimmed.slice(3)) }}/>
      );
      continue;
    }

    // Option buttons: A) ... B) ... C) ...
    const optionMatch = trimmed.match(/^([A-E])\)\s+(.+)/);
    if (optionMatch) {
      flushList();
      const letter = optionMatch[1];
      const label  = optionMatch[2];
      const isRec  = label.toLowerCase().includes("recommended");
      elements.push(
        <button key={key++}
          onClick={() => onOptionClick && onOptionClick(letter)}
          style={{
            display:"flex", alignItems:"center", gap:10,
            width:"100%", textAlign:"left",
            background: isRec ? "rgba(99,102,241,0.07)" : "#f9fafb",
            border: isRec ? "1.5px solid rgba(99,102,241,0.35)" : "1.5px solid #e5e7eb",
            borderRadius:9, padding:"9px 14px",
            fontSize:13, cursor:"pointer", marginBottom:6,
            transition:"background 0.12s, border-color 0.12s"
          }}
          onMouseEnter={ev => {
            ev.currentTarget.style.background = "rgba(99,102,241,0.12)";
            ev.currentTarget.style.borderColor = "#6366f1";
          }}
          onMouseLeave={ev => {
            ev.currentTarget.style.background = isRec ? "rgba(99,102,241,0.07)" : "#f9fafb";
            ev.currentTarget.style.borderColor = isRec ? "rgba(99,102,241,0.35)" : "#e5e7eb";
          }}
        >
          <span style={{
            width:24, height:24, borderRadius:6, flexShrink:0,
            background: isRec ? "#6366f1" : "#e5e7eb",
            color: isRec ? "#fff" : "#374151",
            display:"flex", alignItems:"center", justifyContent:"center",
            fontSize:11, fontWeight:700
          }}>{letter}</span>
          <span dangerouslySetInnerHTML={{ __html: inlineFormat(label) }}
            style={{ color:"#111827", flex:1 }}/>
          {isRec && (
            <span style={{
              fontSize:10, fontWeight:500, color:"#6366f1",
              background:"rgba(99,102,241,0.1)", borderRadius:4,
              padding:"2px 7px", flexShrink:0
            }}>Recommended</span>
          )}
        </button>
      );
      continue;
    }

    const bulletMatch = trimmed.match(/^[•\-\*]\s+(.+)/);
    if (bulletMatch) { listItems.push(bulletMatch[1]); continue; }
    const numberedMatch = trimmed.match(/^\d+\.\s+(.+)/);
    if (numberedMatch) { listItems.push(numberedMatch[1]); continue; }

    flushList();
    elements.push(
      <p key={key++} style={{ margin:"2px 0", lineHeight:1.65 }}
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
      background: uploaded ? "rgba(29,158,117,0.08)" : "var(--color-background-secondary)",
      border: `0.5px solid ${uploaded ? "rgba(29,158,117,0.3)" : "var(--color-border-tertiary)"}`,
      borderRadius:8, padding:"8px 12px",
      maxWidth:240, marginBottom:4
    }}>
      <span style={{ fontSize:20, flexShrink:0 }}>{fileIcon(file.name)}</span>
      <div style={{ minWidth:0 }}>
        <div style={{
          fontSize:12, fontWeight:500,
          color:"var(--color-text-primary)",
          whiteSpace:"nowrap", overflow:"hidden", textOverflow:"ellipsis"
        }}>{file.name}</div>
        <div style={{ fontSize:11, color:"var(--color-text-tertiary)", marginTop:1 }}>
          {formatBytes(file.size)}
          {uploaded && <span style={{ color:"rgba(29,158,117,0.9)", marginLeft:6 }}>✓ Uploaded</span>}
        </div>
      </div>
    </div>
  );
}

function StepBar({ state }) {
  const idx = STEP_ORDER.indexOf(state);
  return (
    <div style={{
      display:"flex", gap:0, alignItems:"stretch",
      borderBottom:"0.5px solid var(--color-border-tertiary)",
      background:"var(--color-background-secondary)",
      overflowX:"auto"
    }}>
      {STEP_ORDER.map((s, i) => {
        const done = i < idx;
        const active = i === idx;
        return (
          <div key={s} style={{
            display:"flex", alignItems:"center", gap:6,
            padding:"10px 14px", flexShrink:0,
            borderBottom: active ? "2px solid var(--color-text-info)" : "2px solid transparent",
            background: active ? "var(--color-background-info)" : "transparent"
          }}>
            <div style={{
              width:7, height:7, borderRadius:"50%", flexShrink:0,
              background: done ? "var(--color-text-success)"
                : active ? "var(--color-text-info)"
                : "var(--color-border-secondary)"
            }}/>
            <span style={{
              fontSize:11, whiteSpace:"nowrap",
              color: active ? "var(--color-text-info)"
                : done ? "var(--color-text-success)"
                : "var(--color-text-tertiary)",
              fontWeight: active ? 500 : 400
            }}>{STATE_LABELS[s]}</span>
          </div>
        );
      })}
    </div>
  );
}

function Message({ msg, onOptionClick }) {
  const isUser = msg.role === "user";
  const isFile = msg.type === "file";

  if (isFile) {
    return (
      <div style={{ display:"flex", justifyContent:"flex-end", alignItems:"flex-start", gap:8, marginBottom:8 }}>
        <FilePreviewCard file={msg.file} uploaded={true}/>
        <div style={{
          width:30, height:30, borderRadius:"50%", flexShrink:0,
          background:"linear-gradient(135deg, #6366f1, #8b5cf6)",
          display:"flex", alignItems:"center", justifyContent:"center",
          fontSize:15, marginTop:2,
          boxShadow:"0 1px 4px rgba(99,102,241,0.3)"
        }}>👤</div>
      </div>
    );
  }

  return (
    <div style={{
      display:"flex",
      justifyContent: isUser ? "flex-end" : "flex-start",
      marginBottom:14, alignItems:"flex-start", gap:8
    }}>
      {!isUser && (
        <div style={{
          width:30, height:30, borderRadius:"50%", flexShrink:0,
          background:"linear-gradient(135deg, #0ea5e9, #6366f1)",
          display:"flex", alignItems:"center", justifyContent:"center",
          fontSize:11, fontWeight:700, color:"#fff",
          marginTop:2, boxShadow:"0 1px 4px rgba(14,165,233,0.35)",
          letterSpacing:"0.03em"
        }}>AI</div>
      )}
      <div style={{
        maxWidth:"74%",
        background: isUser ? "linear-gradient(135deg, #6366f1, #8b5cf6)" : "#ffffff",
        border: isUser ? "none" : "0.5px solid #e5e7eb",
        borderRadius: isUser ? "14px 14px 2px 14px" : "2px 14px 14px 14px",
        padding:"10px 14px",
        fontSize:13.5,
        color: isUser ? "#ffffff" : "#111827",
        boxShadow: isUser
          ? "0 2px 8px rgba(99,102,241,0.3)"
          : "0 1px 4px rgba(0,0,0,0.07)"
      }}>
        {isUser
          ? <p style={{ margin:0, lineHeight:1.6 }}>{msg.content}</p>
          : renderMarkdown(msg.content, onOptionClick)
        }
      </div>
      {isUser && (
        <div style={{
          width:30, height:30, borderRadius:"50%", flexShrink:0,
          background:"linear-gradient(135deg, #6366f1, #8b5cf6)",
          display:"flex", alignItems:"center", justifyContent:"center",
          fontSize:15, marginTop:2,
          boxShadow:"0 1px 4px rgba(99,102,241,0.3)"
        }}>👤</div>
      )}
    </div>
  );
}

function DownloadPanel({ sessionId }) {
  const exports = [
    { label:"Power BI Package", key:"powerbi", icon:"📦",
      hint:"model.bim + .pbip + import guide" },
    { label:"Excel Workbook", key:"excel", icon:"🟢",
      hint:"Tables + measures + quality log" },
    { label:"TMDL JSON", key:"tmdl", icon:"📋",
      hint:"Azure Analysis Services / XMLA" },
    { label:"Audit Log", key:"audit", icon:"📝",
      hint:"All mapping + error decisions" },
    { label:"PDF Report", key:"report", icon:"🔴",
      hint:"Completion summary" },
  ];
  return (
    <div style={{
      border:"0.5px solid var(--color-border-tertiary)",
      borderRadius:10, overflow:"hidden",
      background:"var(--color-background-primary)"
    }}>
      <div style={{
        padding:"10px 14px",
        borderBottom:"0.5px solid var(--color-border-tertiary)",
        background:"var(--color-background-secondary)",
        display:"flex", alignItems:"center", gap:6
      }}>
        <span style={{ color:"var(--color-text-success)", fontSize:13 }}>✓</span>
        <span style={{ fontSize:12, fontWeight:500 }}>Ready to download</span>
      </div>
      <div style={{ padding:8, display:"flex", flexDirection:"column", gap:4 }}>
        {exports.map(e => (
          <a key={e.key}
            href={`${API}/export/${sessionId}/${e.key}`}
            style={{
              display:"flex", alignItems:"center", gap:10,
              padding:"9px 12px",
              border:"0.5px solid var(--color-border-tertiary)",
              borderRadius:7, textDecoration:"none",
              color:"var(--color-text-primary)",
              background:"var(--color-background-secondary)",
              transition:"background 0.1s"
            }}
            onMouseEnter={ev => ev.currentTarget.style.background="var(--color-background-tertiary)"}
            onMouseLeave={ev => ev.currentTarget.style.background="var(--color-background-secondary)"}
          >
            <span style={{ fontSize:16 }}>{e.icon}</span>
            <div style={{ flex:1, minWidth:0 }}>
              <div style={{ fontSize:12, fontWeight:500 }}>{e.label}</div>
              <div style={{ fontSize:10, color:"var(--color-text-tertiary)", marginTop:1 }}>{e.hint}</div>
            </div>
            <span style={{ fontSize:12, color:"var(--color-text-tertiary)" }}>↓</span>
          </a>
        ))}
      </div>
    </div>
  );
}

function ContextPanel({ ctx }) {
  if (!ctx) return null;
  const items = [
    { label:"Tables", value:(ctx.raw_files?.length||0)+(ctx.mapping_files?.length||0) },
    { label:"Measures", value:ctx.measures_count||0 },
    { label:"Mappings", value:ctx.mappings_count||0 },
    { label:"Errors pending", value:ctx.errors_pending||0 },
  ];
  return (
    <div style={{
      border:"0.5px solid var(--color-border-tertiary)",
      borderRadius:10, overflow:"hidden",
      background:"var(--color-background-primary)"
    }}>
      <div style={{
        padding:"10px 14px",
        borderBottom:"0.5px solid var(--color-border-tertiary)",
        background:"var(--color-background-secondary)"
      }}>
        <div style={{ fontSize:12, fontWeight:500 }}>{ctx.project_name || "Project"}</div>
        <div style={{ fontSize:10, color:"var(--color-text-tertiary)", marginTop:2 }}>
          {ctx.state?.replace(/_/g," ")}
        </div>
      </div>
      <div style={{
        display:"grid", gridTemplateColumns:"1fr 1fr",
        gap:"0.5px", background:"var(--color-border-tertiary)"
      }}>
        {items.map(it => (
          <div key={it.label} style={{
            padding:"10px 12px",
            background:"var(--color-background-primary)"
          }}>
            <div style={{ fontSize:20, fontWeight:500, lineHeight:1 }}>{it.value}</div>
            <div style={{ fontSize:10, color:"var(--color-text-tertiary)", marginTop:3 }}>{it.label}</div>
          </div>
        ))}
      </div>
      {ctx.model_built && (
        <div style={{
          padding:"7px 14px", fontSize:11,
          color:"var(--color-text-success)",
          borderTop:"0.5px solid var(--color-border-tertiary)",
          display:"flex", alignItems:"center", gap:4
        }}>
          <span>✓</span> Semantic model built
        </div>
      )}
    </div>
  );
}

function UploadedFilesList({ files }) {
  if (!files.size) return null;
  return (
    <div>
      <div style={{
        fontSize:10, fontWeight:500, color:"var(--color-text-tertiary)",
        textTransform:"uppercase", letterSpacing:"0.06em", marginBottom:6
      }}>Uploaded files</div>
      <div style={{ display:"flex", flexDirection:"column", gap:3 }}>
        {[...files].map(name => (
          <div key={name} style={{
            display:"flex", alignItems:"center", gap:7,
            padding:"5px 8px",
            background:"var(--color-background-primary)",
            border:"0.5px solid var(--color-border-tertiary)",
            borderRadius:6, fontSize:11,
            color:"var(--color-text-secondary)"
          }}>
            <span style={{ fontSize:13 }}>{fileIcon(name)}</span>
            <span style={{
              flex:1, whiteSpace:"nowrap", overflow:"hidden",
              textOverflow:"ellipsis"
            }}>{name}</span>
            <span style={{ color:"var(--color-text-success)", flexShrink:0 }}>✓</span>
          </div>
        ))}
      </div>
    </div>
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
  const chatEndRef  = useRef(null);
  const fileInputRef = useRef(null);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior:"smooth" });
  }, [messages]);

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
    } catch(e) { addMsg("assistant", `Error: ${e.message}`); }
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
    } catch(e) { addMsg("assistant", `Error: ${e.message}`); }
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

  const handleOptionClick = (letter) => {
    sendMessage(letter);
  };

  if (!sessionId) {
    return (
      <div style={{
        minHeight:480, display:"flex", flexDirection:"column",
        alignItems:"center", justifyContent:"center",
        gap:28, padding:"48px 24px",
        background:"linear-gradient(135deg, #f0f4ff 0%, #faf5ff 50%, #f0fdf4 100%)"
      }}>
        <div style={{ textAlign:"center" }}>
          <div style={{
            width:56, height:56, borderRadius:14,
            background:"var(--color-background-info)",
            border:"0.5px solid var(--color-border-info)",
            display:"flex", alignItems:"center", justifyContent:"center",
            fontSize:26, margin:"0 auto 16px"
          }}>📊</div>
          <h1 style={{ fontSize:22, fontWeight:500, margin:"0 0 8px" }}>
            AI BI Copilot
          </h1>
          <p style={{ fontSize:14, color:"var(--color-text-secondary)", margin:0, maxWidth:340 }}>
            Upload your data files and a plain-English protocol document.
            We'll build your Power BI semantic model automatically.
          </p>
        </div>
        <div style={{ width:"100%", maxWidth:380 }}>
          <div style={{ fontSize:12, color:"var(--color-text-tertiary)", marginBottom:6 }}>
            Project name
          </div>
          <div style={{ display:"flex", gap:8 }}>
            <input
              value={projectName}
              onChange={ev => setProjectName(ev.target.value)}
              onKeyDown={ev => ev.key==="Enter" && startProject()}
              placeholder="e.g. Q2 Sales Performance"
              style={{ flex:1, fontSize:14 }}
              autoFocus
            />
            <button
              onClick={startProject}
              disabled={loading || !projectName.trim()}
              style={{ padding:"0 20px", fontSize:14, fontWeight:500 }}
            >
              {loading ? "..." : "Start →"}
            </button>
          </div>
        </div>
        <div style={{
          display:"flex", gap:20, fontSize:12,
          color:"var(--color-text-tertiary)"
        }}>
          {["Upload data files","AI builds semantic model","Download .pbip"].map((s,i) => (
            <div key={i} style={{ display:"flex", alignItems:"center", gap:6 }}>
              <div style={{
                width:18, height:18, borderRadius:"50%",
                background:"var(--color-background-secondary)",
                border:"0.5px solid var(--color-border-tertiary)",
                display:"flex", alignItems:"center", justifyContent:"center",
                fontSize:10, fontWeight:500, flexShrink:0
              }}>{i+1}</div>
              {s}
            </div>
          ))}
        </div>
      </div>
    );
  }

  const isComplete = state === "COMPLETE";

  return (
    <div style={{ display:"flex", flexDirection:"column", minHeight:600 }}>

      {/* Gradient header */}
      <div style={{
        background:"linear-gradient(135deg, #6366f1 0%, #8b5cf6 60%, #06b6d4 100%)",
        padding:"10px 20px",
        display:"flex", alignItems:"center", justifyContent:"space-between"
      }}>
        <div style={{ display:"flex", alignItems:"center", gap:10 }}>
          <div style={{
            width:30, height:30, borderRadius:8,
            background:"rgba(255,255,255,0.2)",
            display:"flex", alignItems:"center", justifyContent:"center",
            fontSize:16
          }}>📊</div>
          <span style={{ color:"#fff", fontWeight:600, fontSize:15 }}>AI BI Copilot</span>
        </div>
        <div style={{
          fontSize:11, color:"rgba(255,255,255,0.8)",
          background:"rgba(255,255,255,0.15)", borderRadius:6, padding:"4px 10px"
        }}>
          {ctx?.project_name || "New project"}
        </div>
      </div>

      <StepBar state={state}/>
      <div style={{ display:"flex", flex:1 }}>
        <div style={{ flex:1, display:"flex", flexDirection:"column" }}>
          <div style={{
            flex:1, overflowY:"auto", padding:"20px 24px",
            display:"flex", flexDirection:"column", minHeight:400,
            background:"#f8f9fb"
          }}>
            {messages.length === 0 && (
              <div style={{
                flex:1, display:"flex", alignItems:"center",
                justifyContent:"center", flexDirection:"column", gap:8
              }}>
                <div style={{ fontSize:28 }}>👋</div>
                <div style={{ fontSize:13, color:"var(--color-text-tertiary)" }}>
                  Session started — upload your raw data files to begin
                </div>
              </div>
            )}
            {messages.map((m,i) => (
              <Message key={i} msg={m} onOptionClick={handleOptionClick}/>
            ))}
            {loading && (
              <div style={{ display:"flex", alignItems:"flex-start", gap:8, marginBottom:14 }}>
                <div style={{
                  width:30, height:30, borderRadius:"50%",
                  background:"linear-gradient(135deg, #0ea5e9, #6366f1)",
                  display:"flex", alignItems:"center", justifyContent:"center",
                  fontSize:11, fontWeight:700, color:"#fff",
                  boxShadow:"0 1px 4px rgba(14,165,233,0.35)", flexShrink:0
                }}>AI</div>
                <div style={{
                  background:"#fff",
                  border:"0.5px solid #e5e7eb",
                  borderRadius:"2px 14px 14px 14px",
                  padding:"10px 14px", fontSize:13,
                  boxShadow:"0 1px 3px rgba(0,0,0,0.06)"
                }}>
                  <span style={{ color:"var(--color-text-tertiary)" }}>
                    Thinking
                    <span style={{ animation:"dots 1.2s steps(3,end) infinite" }}>...</span>
                  </span>
                  <style>{`@keyframes dots{0%,100%{opacity:0.3}50%{opacity:1}}`}</style>
                </div>
              </div>
            )}
            <div ref={chatEndRef}/>
          </div>

          <div style={{
            borderTop:"1.5px solid #e5e7eb",
            padding:"12px 16px",
            background:"#f3f4f6",
            display:"flex", gap:8, alignItems:"flex-end"
          }}>
            <input type="file" ref={fileInputRef} multiple
              accept=".csv,.xlsx,.xls,.txt,.md,.pdf,.docx,.json"
              onChange={handleFileSelect} style={{ display:"none"}}/>
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={loading}
              title="Attach files"
              style={{
                padding:"0 12px", height:36, fontSize:16, flexShrink:0,
                color:"var(--color-text-secondary)"
              }}
            >📎</button>
            <textarea
              value={input}
              onChange={ev => setInput(ev.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Type a message… (Enter to send, Shift+Enter for new line)"
              disabled={loading}
              rows={1}
              style={{
                flex:1, resize:"none", fontSize:13.5,
                lineHeight:1.5, padding:"9px 14px",
                borderRadius:10,
                border:"1.5px solid #d1d5db",
                background:"#f9fafb",
                color:"#111827",
                fontFamily:"var(--font-sans)",
                boxShadow:"inset 0 1px 3px rgba(0,0,0,0.06)",
                outline:"none",
                transition:"border-color 0.15s, box-shadow 0.15s"
              }}
              onFocus={ev => {
                ev.target.style.border = "1.5px solid #6366f1";
                ev.target.style.boxShadow = "0 0 0 3px rgba(99,102,241,0.12), inset 0 1px 3px rgba(0,0,0,0.04)";
                ev.target.style.background = "#ffffff";
              }}
              onBlur={ev => {
                ev.target.style.border = "1.5px solid #d1d5db";
                ev.target.style.boxShadow = "inset 0 1px 3px rgba(0,0,0,0.06)";
                ev.target.style.background = "#f9fafb";
              }}
            />
            <button
              onClick={handleSend}
              disabled={loading || !input.trim()}
              style={{
                padding:"0 18px", height:36, fontSize:13,
                fontWeight:500, flexShrink:0
              }}
            >Send</button>
          </div>
        </div>

        <div style={{
          width:228, borderLeft:"0.5px solid var(--color-border-tertiary)",
          overflowY:"auto", padding:12, display:"flex",
          flexDirection:"column", gap:14,
          background:"var(--color-background-secondary)"
        }}>
          <div>
            <div style={{
              fontSize:10, fontWeight:500, color:"var(--color-text-tertiary)",
              textTransform:"uppercase", letterSpacing:"0.06em", marginBottom:4
            }}>Session</div>
            <div style={{
              fontSize:10, color:"var(--color-text-tertiary)",
              fontFamily:"var(--font-mono)", wordBreak:"break-all"
            }}>{sessionId?.slice(0,18)}...</div>
          </div>

          {ctx && <ContextPanel ctx={ctx}/>}
          {uploadedFiles.size > 0 && <UploadedFilesList files={uploadedFiles}/>}
          {isComplete && <DownloadPanel sessionId={sessionId}/>}
        </div>
      </div>
    </div>
  );
}
