import React, { useState, useEffect, useRef } from "react";
import { Network } from "vis-network";
import { DataSet } from "vis-data";
import { 
  Search, 
  Calendar, 
  Network as NetIcon, 
  Settings, 
  Heart, 
  CheckCircle2, 
  AlertTriangle,
  Lock,
  User,
  Users,
  Briefcase,
  LogOut
} from "lucide-react";

// Dynamically resolve Gateway URL to port 8000 of the accessing hostname
const GATEWAY_URL = `http://${window.location.hostname}:8000`;

interface QueryResponse {
  answer: string;
  confidence_score: number;
  sources: any[];
  processing_steps: string[];
  degraded?: boolean;
  timeline?: {
    events: any[];
    outcome_assessment?: string;
    confidence_score?: number;
  };
}

export default function App() {
  // Authentication states
  const [token, setToken] = useState<string | null>(localStorage.getItem("dna_token"));
  const [username, setUsername] = useState<string>(localStorage.getItem("dna_username") || "");
  const [teamName, setTeamName] = useState<string>(localStorage.getItem("dna_team") || "");
  const [reportingManager, setReportingManager] = useState<string>(localStorage.getItem("dna_manager") || "");
  
  // Auth Form states
  const [authTab, setAuthTab] = useState<"signin" | "signup">("signin");
  const [formUsername, setFormUsername] = useState<string>("");
  const [formPassword, setFormPassword] = useState<string>("");
  const [formTeamName, setFormTeamName] = useState<string>("");
  const [formReportingManager, setFormReportingManager] = useState<string>("");
  const [authError, setAuthError] = useState<string>("");
  const [authSuccess, setAuthSuccess] = useState<string>("");

  // App navigation & filters
  const [activeTab, setActiveTab] = useState<string>("ask");
  const [projectFilter, setProjectFilter] = useState<string>("All Projects");
  const [question, setQuestion] = useState<string>("");
  
  // States for Ask Page
  const [loading, setLoading] = useState<boolean>(false);
  const [queryResult, setQueryResult] = useState<QueryResponse | null>(null);
  const [errorMsg, setErrorMsg] = useState<string>("");

  // States for Timeline Page
  const [timelineTopic, setTimelineTopic] = useState<string>("");
  const [timelineLoading, setTimelineLoading] = useState<boolean>(false);
  const [timelineResult, setTimelineResult] = useState<any | null>(null);

  // States for Ingestion Page
  const [ingestLoading, setIngestLoading] = useState<boolean>(false);
  const [ingestJobId, setIngestJobId] = useState<string>("");
  const [ingestStatus, setIngestStatus] = useState<string>("");

  // States for Health Page
  const [healthStatus, setHealthStatus] = useState<any | null>(null);

  // States for Graph Page
  const [graphEntity, setGraphEntity] = useState<string>("");
  const [graphLoading, setGraphLoading] = useState<boolean>(false);
  const graphContainerRef = useRef<HTMLDivElement>(null);
  const networkRef = useRef<Network | null>(null);

  // Prefill helper
  const handlePrefill = (q: string) => {
    setQuestion(q);
    setActiveTab("ask");
  };

  // Poll Health on mount
  useEffect(() => {
    fetchHealth();
    const interval = setInterval(fetchHealth, 15000);
    return () => clearInterval(interval);
  }, []);

  const fetchHealth = async () => {
    try {
      const r = await fetch(`${GATEWAY_URL}/health`);
      if (r.ok) {
        const data = await r.json();
        setHealthStatus(data);
      }
    } catch (e) {
      console.error("Health check failed", e);
    }
  };

  // Helper fetch wrapper to auto-inject Bearer tokens and handle 401s
  const authFetch = async (url: string, options: RequestInit = {}) => {
    const headers = {
      ...(options.headers || {}),
      "Authorization": `Bearer ${token}`
    };
    const r = await fetch(url, { ...options, headers });
    if (r.status === 401) {
      handleLogout();
      throw new Error("Session expired. Please log in again.");
    }
    return r;
  };

  // ── Authentication Actions ───────────────────────────────
  const handleSignup = async (e: React.FormEvent) => {
    e.preventDefault();
    setAuthError("");
    setAuthSuccess("");

    if (!formUsername.trim() || !formPassword.trim() || !formTeamName.trim() || !formReportingManager.trim()) {
      setAuthError("All fields are required.");
      return;
    }

    try {
      const r = await fetch(`${GATEWAY_URL}/api/v1/auth/signup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: formUsername,
          password: formPassword,
          team_name: formTeamName,
          reporting_manager: formReportingManager
        })
      });
      const data = await r.json();
      if (!r.ok) {
        throw new Error(data.detail || "Registration failed");
      }
      setAuthSuccess("User registered successfully! Please sign in.");
      setAuthTab("signin");
      setFormPassword("");
    } catch (err: any) {
      setAuthError(err.message || "Failed to register.");
    }
  };

  const handleSignin = async (e: React.FormEvent) => {
    e.preventDefault();
    setAuthError("");
    setAuthSuccess("");

    if (!formUsername.trim() || !formPassword.trim()) {
      setAuthError("Username and password are required.");
      return;
    }

    try {
      const r = await fetch(`${GATEWAY_URL}/api/v1/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: formUsername,
          password: formPassword
        })
      });
      const data = await r.json();
      if (!r.ok) {
        throw new Error(data.detail || "Authentication failed");
      }
      
      // Save credentials in localStorage
      localStorage.setItem("dna_token", data.token);
      localStorage.setItem("dna_username", data.username);
      localStorage.setItem("dna_team", data.team_name);
      localStorage.setItem("dna_manager", data.reporting_manager);

      setToken(data.token);
      setUsername(data.username);
      setTeamName(data.team_name);
      setReportingManager(data.reporting_manager);
      
      // Reset form fields
      setFormUsername("");
      setFormPassword("");
      setFormTeamName("");
      setFormReportingManager("");
    } catch (err: any) {
      setAuthError(err.message || "Failed to sign in.");
    }
  };

  const handleLogout = () => {
    localStorage.removeItem("dna_token");
    localStorage.removeItem("dna_username");
    localStorage.removeItem("dna_team");
    localStorage.removeItem("dna_manager");

    setToken(null);
    setUsername("");
    setTeamName("");
    setReportingManager("");
    setQueryResult(null);
    setTimelineResult(null);
  };

  // ── Ask Request ──────────────────────────────────────────
  const handleAsk = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!question.trim()) return;
    setLoading(true);
    setQueryResult(null);
    setErrorMsg("");

    try {
      const payload = {
        question: question,
        project_filter: projectFilter === "All Projects" ? null : projectFilter
      };
      const r = await authFetch(`${GATEWAY_URL}/api/v1/query`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (!r.ok) {
        const errData = await r.json();
        throw new Error(errData.detail || `Server error: ${r.status}`);
      }
      const data = await r.json();
      setQueryResult(data);
    } catch (err: any) {
      setErrorMsg(err.message || "Failed to contact gateway. Check Docker container state.");
    } finally {
      setLoading(false);
    }
  };

  // ── Timeline Request ─────────────────────────────────────
  const handleTimeline = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!timelineTopic.trim()) return;
    setTimelineLoading(true);
    setTimelineResult(null);

    try {
      const projParam = projectFilter !== "All Projects" ? `&project=${projectFilter}` : "";
      const r = await authFetch(`${GATEWAY_URL}/api/v1/timeline/${encodeURIComponent(timelineTopic)}?topic=${encodeURIComponent(timelineTopic)}${projParam}`);
      if (r.ok) {
        const data = await r.json();
        setTimelineResult(data);
      } else {
        throw new Error(`Failed to load timeline: ${r.status}`);
      }
    } catch (err: any) {
      alert(err.message);
    } finally {
      setTimelineLoading(false);
    }
  };

  // ── Ingest Request ───────────────────────────────────────
  const handleIngest = async () => {
    setIngestLoading(true);
    setIngestJobId("");
    setIngestStatus("Triggering Ingestion...");
    try {
      const r = await authFetch(`${GATEWAY_URL}/api/v1/ingest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          data_dir: "/app/data/synthetic",
          trigger_embedding: true,
          trigger_graph: true
        })
      });
      if (r.ok) {
        const data = await r.json();
        setIngestJobId(data.job_id);
        setIngestStatus("Running - Ingest job submitted. Check gateway status for completion.");
      } else {
        setIngestStatus("Failed to trigger ingestion.");
      }
    } catch (e) {
      setIngestStatus("Inbound connection error. Ingestion offline.");
    } finally {
      setIngestLoading(false);
    }
  };

  // ── Render Network Graph ─────────────────────────────────
  const drawNetworkGraph = (nodesArray: any[], edgesArray: any[]) => {
    if (!graphContainerRef.current) return;

    if (networkRef.current) {
      networkRef.current.destroy();
      networkRef.current = null;
    }

    const data = {
      nodes: new DataSet(nodesArray),
      edges: new DataSet(edgesArray)
    };

    const options = {
      nodes: {
        shape: "dot",
        scaling: { min: 10, max: 30 },
        font: {
          color: "#f4f4f5",
          size: 12,
          face: "Inter, system-ui, sans-serif"
        },
        borderWidth: 2,
        shadow: true
      },
      edges: {
        width: 2,
        color: {
          color: "#3f3f46",
          highlight: "#8b5cf6",
          hover: "#a78bfa"
        },
        arrows: {
          to: { enabled: true, scaleFactor: 0.8 }
        },
        font: {
          color: "#a1a1aa",
          size: 9,
          face: "Inter, system-ui, sans-serif",
          background: "#09090b",
          strokeWidth: 0
        },
        smooth: { type: "continuous" }
      },
      physics: {
        solver: "forceAtlas2Based",
        forceAtlas2Based: {
          gravitationalConstant: -50,
          centralGravity: 0.01,
          springConstant: 0.08,
          springLength: 120
        },
        stabilization: { iterations: 150, updateInterval: 25 }
      },
      interaction: {
        hover: true,
        tooltipDelay: 200,
        navigationButtons: true,
        keyboard: true
      }
    };

    networkRef.current = new Network(graphContainerRef.current, data, options as any);
  };

  // Fetch decisions or specific entities for graph visualization
  const fetchGraphData = async (entityName?: string) => {
    setGraphLoading(true);
    try {
      if (entityName && entityName.trim()) {
        const r = await authFetch(`${GATEWAY_URL}/api/v1/graph/entities/${encodeURIComponent(entityName)}`);
        if (r.ok) {
          const data = await r.json();
          const connections = data.connections || [];
          
          const nodes: any[] = [];
          const edges: any[] = [];
          const seen = new Set<string>();

          // Center Query Node
          const isProj = ["CloudMigration", "DataPlatform", "AuthRefactor", "MobileApp"].includes(entityName);
          const centerColor = isProj ? "#06B6D4" : "#8b5cf6";
          nodes.push({
            id: entityName,
            label: `${isProj ? "Project" : "Person"}\n${entityName}`,
            color: centerColor,
            size: 24,
            font: { size: 14, bold: true }
          });
          seen.add(entityName);

          connections.forEach((conn: any) => {
            const rel = conn.rel || "?";
            const labels = conn.labels || [];
            const m = conn.m || {};
            const type = labels[0] || "Unknown";

            let n_id = "";
            let n_label = "";

            if (["Person", "Project"].includes(type)) {
              n_id = m.name || "Unknown";
              n_label = n_id;
            } else {
              n_id = m.id || "Unknown";
              if (type === "Decision") {
                n_label = m.description || "";
                if (n_label.length > 40) n_label = n_label.substring(0, 37) + "...";
              } else if (type === "Meeting") {
                n_label = m.title || "";
              } else if (type === "Ticket") {
                n_label = m.title || "";
                if (n_label.length > 30) n_label = n_label.substring(0, 27) + "...";
              } else if (type === "Email") {
                n_label = m.subject || "";
              } else {
                n_label = n_id;
              }
            }

            let color = "#8b5cf6";
            if (type === "Person") color = "#a78bfa";
            else if (type === "Project") color = "#06B6D4";
            else if (type === "Decision") color = "#10B981";
            else if (type === "Meeting") color = "#F59E0B";
            else if (type === "Ticket") color = "#EF4444";
            else if (type === "Email") color = "#3B82F6";

            if (!seen.has(n_id)) {
              seen.add(n_id);
              const tooltipProps = { ...m };
              delete tooltipProps.content;
              nodes.push({
                id: n_id,
                label: `${type}\n${n_label}`,
                color: color,
                size: 16,
                title: `Type: ${type}\n` + Object.entries(tooltipProps).map(([k, v]) => `${k}: ${v}`).join("\n")
              });
            }

            edges.push({
              from: entityName,
              to: n_id,
              label: rel
            });
          });

          drawNetworkGraph(nodes, edges);
        }
      } else {
        const projParam = projectFilter !== "All Projects" ? `?project=${projectFilter}` : "";
        const r = await authFetch(`${GATEWAY_URL}/api/v1/graph/decisions${projParam}`);
        if (r.ok) {
          const data = await r.json();
          const decisions = data.decisions || [];

          const nodes: any[] = [];
          const edges: any[] = [];
          const seen = new Set<string>();

          decisions.slice(0, 15).forEach((d: any) => {
            const d_id = d.id;
            const d_desc = d.description || "";
            const d_proj = d.project || "";
            let d_label = d_desc;
            if (d_label.length > 35) d_label = d_label.substring(0, 32) + "...";

            if (!seen.has(d_id)) {
              seen.add(d_id);
              nodes.push({
                id: d_id,
                label: `Decision\n${d_label}`,
                color: "#10B981",
                size: 15,
                title: `Decision: ${d_desc}\nDate: ${d.date || ""}`
              });
            }

            if (d_proj) {
              if (!seen.has(d_proj)) {
                seen.add(d_proj);
                nodes.push({
                  id: d_proj,
                  label: `Project\n${d_proj}`,
                  color: "#06B6D4",
                  size: 20,
                  title: `Project: ${d_proj}`
                });
              }
              edges.push({
                from: d_id,
                to: d_proj,
                label: "PART_OF"
              });
            }

            if (d_id && d_id.includes("_")) {
              const source_id = d_id.split("_")[0];
              if (source_id.startsWith("MTG")) {
                if (!seen.has(source_id)) {
                  seen.add(source_id);
                  nodes.push({
                    id: source_id,
                    label: `Meeting\n${source_id}`,
                    color: "#F59E0B",
                    size: 18,
                    title: `Meeting: ${source_id}`
                  });
                }
                edges.push({
                  from: source_id,
                  to: d_id,
                  label: "PRODUCED"
                });
              }
            }
          });

          drawNetworkGraph(nodes, edges);
        }
      }
    } catch (e) {
      console.error("Failed to load graph network", e);
    } finally {
      setGraphLoading(false);
    }
  };

  useEffect(() => {
    if (activeTab === "graph" && token) {
      fetchGraphData(graphEntity);
    }
  }, [activeTab, projectFilter, token]);

  const handleGraphSearch = (e: React.FormEvent) => {
    e.preventDefault();
    fetchGraphData(graphEntity);
  };

  const getConfidenceColor = (score: number) => {
    const pct = score * 100;
    return pct > 70 ? "#10b981" : pct > 40 ? "#f59e0b" : "#ef4444";
  };

  // ── RENDER AUTHENTICATION VIEW ──
  if (!token) {
    return (
      <div style={{
        display: "flex", 
        justifyContent: "center", 
        alignItems: "center", 
        minHeight: "100vh", 
        backgroundColor: "#09090b",
        padding: "20px"
      }}>
        <div className="card" style={{ width: "100%", maxWidth: "440px", padding: "32px", borderColor: "var(--accent-purple)" }}>
          <div style={{ textAlign: "center", marginBottom: "28px" }}>
            <h2 style={{ color: "#c084fc", fontFamily: "Trebuchet MS", fontSize: "28px" }}>🧬 DecisionDNA</h2>
            <p style={{ fontSize: "13px", color: "var(--text-muted)", marginTop: "4px" }}>Secure Organizational Memory Engine</p>
          </div>

          <div style={{ display: "flex", gap: "10px", marginBottom: "24px" }}>
            <button 
              className={`menu-item ${authTab === "signin" ? "active" : ""}`}
              onClick={() => { setAuthTab("signin"); setAuthError(""); setAuthSuccess(""); }}
              style={{ flex: 1, textAlign: "center", justifyContent: "center" }}
            >
              Sign In
            </button>
            <button 
              className={`menu-item ${authTab === "signup" ? "active" : ""}`}
              onClick={() => { setAuthTab("signup"); setAuthError(""); setAuthSuccess(""); }}
              style={{ flex: 1, textAlign: "center", justifyContent: "center" }}
            >
              Create Account
            </button>
          </div>

          {authError && (
            <div style={{ color: "var(--color-red)", backgroundColor: "rgba(239, 68, 68, 0.1)", border: "1px solid var(--color-red)", padding: "10px", borderRadius: "6px", fontSize: "13px", marginBottom: "16px", display: "flex", gap: "8px", alignItems: "center" }}>
              <AlertTriangle size={15} /> {authError}
            </div>
          )}

          {authSuccess && (
            <div style={{ color: "var(--color-green)", backgroundColor: "rgba(16, 185, 129, 0.1)", border: "1px solid var(--color-green)", padding: "10px", borderRadius: "6px", fontSize: "13px", marginBottom: "16px" }}>
              {authSuccess}
            </div>
          )}

          <form onSubmit={authTab === "signin" ? handleSignin : handleSignup} style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
            <div>
              <label style={{ fontSize: "12px", color: "var(--text-muted)", display: "block", marginBottom: "6px" }}>Username</label>
              <div style={{ position: "relative" }}>
                <User size={16} style={{ position: "absolute", left: "14px", top: "14px", color: "var(--text-muted)" }} />
                <input 
                  type="text" 
                  className="search-input" 
                  style={{ paddingLeft: "42px", width: "100%" }}
                  placeholder="e.g. ravisharma" 
                  value={formUsername}
                  onChange={(e) => setFormUsername(e.target.value)}
                  required
                />
              </div>
            </div>

            {authTab === "signup" && (
              <>
                <div>
                  <label style={{ fontSize: "12px", color: "var(--text-muted)", display: "block", marginBottom: "6px" }}>Team Name</label>
                  <div style={{ position: "relative" }}>
                    <Users size={16} style={{ position: "absolute", left: "14px", top: "14px", color: "var(--text-muted)" }} />
                    <input 
                      type="text" 
                      className="search-input" 
                      style={{ paddingLeft: "42px", width: "100%" }}
                      placeholder="e.g. CloudMigration" 
                      value={formTeamName}
                      onChange={(e) => setFormTeamName(e.target.value)}
                      required
                    />
                  </div>
                </div>

                <div>
                  <label style={{ fontSize: "12px", color: "var(--text-muted)", display: "block", marginBottom: "6px" }}>Reporting Manager Name</label>
                  <div style={{ position: "relative" }}>
                    <Briefcase size={16} style={{ position: "absolute", left: "14px", top: "14px", color: "var(--text-muted)" }} />
                    <input 
                      type="text" 
                      className="search-input" 
                      style={{ paddingLeft: "42px", width: "100%" }}
                      placeholder="e.g. Priya Patel" 
                      value={formReportingManager}
                      onChange={(e) => setFormReportingManager(e.target.value)}
                      required
                    />
                  </div>
                </div>
              </>
            )}

            <div>
              <label style={{ fontSize: "12px", color: "var(--text-muted)", display: "block", marginBottom: "6px" }}>Password</label>
              <div style={{ position: "relative" }}>
                <Lock size={16} style={{ position: "absolute", left: "14px", top: "14px", color: "var(--text-muted)" }} />
                <input 
                  type="password" 
                  className="search-input" 
                  style={{ paddingLeft: "42px", width: "100%" }}
                  placeholder="••••••••" 
                  value={formPassword}
                  onChange={(e) => setFormPassword(e.target.value)}
                  required
                />
              </div>
            </div>

            <button type="submit" className="btn-primary" style={{ padding: "12px", marginTop: "10px", width: "100%" }}>
              {authTab === "signin" ? "Sign In" : "Register & Create Profile"}
            </button>
          </form>

          <div style={{ borderTop: "1px solid var(--border-subtle)", marginTop: "24px", paddingTop: "16px", fontSize: "11px", color: "var(--text-muted)", textAlign: "center" }}>
            👤 Session storage tokens are actively verified and logged in PostgreSQL Server <code style={{ color: "#c084fc" }}>asset</code>.
          </div>
        </div>
      </div>
    );
  }

  // ── RENDER SECURED MAIN APPLICATION ──
  return (
    <div className="app-container">
      {/* ── SIDEBAR Navigation ── */}
      <aside className="sidebar">
        <div className="sidebar-logo">
          🧬 DecisionDNA
        </div>
        <div className="sidebar-sublogo">AI Organizational Memory Engine</div>
        
        <div className="sidebar-divider" />

        <div className="sidebar-menu">
          <button 
            className={`menu-item ${activeTab === "ask" ? "active" : ""}`}
            onClick={() => setActiveTab("ask")}
          >
            <Search size={18} /> Ask DecisionDNA
          </button>
          <button 
            className={`menu-item ${activeTab === "timeline" ? "active" : ""}`}
            onClick={() => setActiveTab("timeline")}
          >
            <Calendar size={18} /> Decision Timeline
          </button>
          <button 
            className={`menu-item ${activeTab === "graph" ? "active" : ""}`}
            onClick={() => setActiveTab("graph")}
          >
            <NetIcon size={18} /> Knowledge Graph
          </button>
          <button 
            className={`menu-item ${activeTab === "ingest" ? "active" : ""}`}
            onClick={() => setActiveTab("ingest")}
          >
            <Settings size={18} /> Ingest Data
          </button>
          <button 
            className={`menu-item ${activeTab === "health" ? "active" : ""}`}
            onClick={() => setActiveTab("health")}
          >
            <Heart size={18} /> System Health
          </button>
        </div>

        <div className="sidebar-divider" />

        <div className="project-filter-box">
          <div className="project-filter-label">Filter by Project</div>
          <select 
            className="project-select"
            value={projectFilter}
            onChange={(e) => setProjectFilter(e.target.value)}
          >
            <option>All Projects</option>
            <option>CloudMigration</option>
            <option>DataPlatform</option>
            <option>AuthRefactor</option>
            <option>MobileApp</option>
          </select>
        </div>

        <div className="sidebar-divider" />

        <div className="project-filter-label">Example Questions</div>
        <div className="example-section">
          <button 
            className="example-button"
            onClick={() => handlePrefill("Why did we reject Vendor X?")}
          >
            Why did we reject Vendor X?
          </button>
          <button 
            className="example-button"
            onClick={() => handlePrefill("Why did we migrate to Azure Functions?")}
          >
            Why did we migrate to Azure Functions?
          </button>
          <button 
            className="example-button"
            onClick={() => handlePrefill("What security risks were flagged in the auth refactor?")}
          >
            What security risks were flagged in the auth refactor?
          </button>
        </div>

        {/* ── Active User Profile Section ── */}
        <div style={{ marginTop: "auto", paddingTop: "16px", borderTop: "1px solid var(--border-subtle)" }}>
          <div style={{ display: "flex", flexDirection: "column", gap: "2px" }}>
            <div style={{ fontSize: "13px", fontWeight: "bold", color: "#c084fc" }}>👤 {username}</div>
            <div style={{ fontSize: "11px", color: "var(--text-muted)" }}>Team: {teamName}</div>
            <div style={{ fontSize: "11px", color: "var(--text-muted)" }}>Manager: {reportingManager}</div>
          </div>
          <button 
            className="menu-item"
            style={{ 
              marginTop: "12px", 
              padding: "6px 12px", 
              borderColor: "var(--color-red)", 
              color: "var(--color-red)", 
              backgroundColor: "rgba(239, 68, 68, 0.05)",
              display: "flex",
              justifyContent: "center",
              gap: "8px"
            }}
            onClick={handleLogout}
          >
            <LogOut size={14} /> Sign Out
          </button>
        </div>
      </aside>

      {/* ── MAIN CONTENT PANEL ── */}
      <main className="content-panel">
        
        {/* ── TAB 1: ASK ── */}
        {activeTab === "ask" && (
          <div>
            <h1 className="page-title">🔍 Ask DecisionDNA</h1>
            <p className="page-subtitle">Ask any question about historical decisions, vendor evaluations, or project context.</p>

            <form onSubmit={handleAsk} className="search-form">
              <input 
                type="text" 
                className="search-input"
                placeholder="Why did we reject Vendor X in Q1 2024?"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                disabled={loading}
              />
              <button type="submit" className="btn-primary" disabled={loading}>
                {loading ? "Thinking..." : "Search"}
              </button>
            </form>

            {errorMsg && (
              <div className="card" style={{ borderColor: "#ef4444" }}>
                <div style={{ display: "flex", gap: "10px", color: "#ef4444", fontWeight: "bold" }}>
                  <AlertTriangle size={20} /> Error connecting to Pipeline
                </div>
                <p style={{ marginTop: "8px", color: "var(--text-muted)", fontSize: "14px" }}>{errorMsg}</p>
              </div>
            )}

            {queryResult && (
              <div>
                <h3 style={{ marginBottom: "12px" }}>💡 Answer Response</h3>
                <div className="answer-box">
                  {queryResult.answer.split("\n").map((line, idx) => (
                    <React.Fragment key={idx}>
                      {line}
                      <br />
                    </React.Fragment>
                  ))}
                </div>

                {queryResult.degraded && (
                  <div className="card" style={{ borderColor: "#f59e0b", padding: "16px" }}>
                    <p style={{ fontSize: "13px", color: "#f59e0b" }}>
                      ⚠️ <strong>Degraded Mode:</strong> OpenAI embeddings are offline. Running search operations with local fallback hash-based vector matching.
                    </p>
                  </div>
                )}

                {/* Confidence Bar */}
                <div className="card" style={{ padding: "16px" }}>
                  <div className="confidence-wrapper">
                    <div className="confidence-header">
                      <span>Confidence score validation</span>
                      <span style={{ color: getConfidenceColor(queryResult.confidence_score), fontWeight: "bold" }}>
                        {Math.round(queryResult.confidence_score * 100)}%
                      </span>
                    </div>
                    <div className="confidence-bar-bg">
                      <div 
                        className="confidence-bar-fill"
                        style={{ 
                          width: `${queryResult.confidence_score * 100}%`,
                          backgroundColor: getConfidenceColor(queryResult.confidence_score)
                        }}
                      />
                    </div>
                  </div>
                </div>

                <div style={{ display: "flex", gap: "24px", marginTop: "24px" }}>
                  {/* Timeline section */}
                  <div style={{ flex: 3 }}>
                    <h3 style={{ marginBottom: "12px" }}>📅 Decisive Trail Chronology</h3>
                    {queryResult.timeline && queryResult.timeline.events ? (
                      <div className="card">
                        {queryResult.timeline.events.map((event, idx) => {
                          const isCrit = event.is_critical;
                          const sentiment = event.sentiment || "neutral";
                          const cls = isCrit ? "critical" : sentiment === "dissent" ? "dissent" : sentiment === "concern" ? "concern" : "";
                          return (
                            <div className={`timeline-event-card ${cls}`} key={idx}>
                              <div className="timeline-event-meta">
                                {event.date} · {(event.event_type || "").toUpperCase()}
                              </div>
                              <div className="timeline-event-title">
                                {event.icon || "📅"} {event.title}
                              </div>
                              <div className="timeline-event-desc">{event.description}</div>
                              {event.participants && event.participants.length > 0 && (
                                <div className="timeline-event-participants">
                                  👥 {event.participants.join(", ")}
                                </div>
                              )}
                            </div>
                          );
                        })}

                        {queryResult.timeline.outcome_assessment && (
                          <div style={{ borderTop: "1px solid var(--border-subtle)", marginTop: "20px", paddingTop: "16px" }}>
                            <strong>🎯 Outcome Assessment:</strong>
                            <p style={{ color: "var(--text-muted)", fontSize: "14px", marginTop: "4px" }}>
                              {queryResult.timeline.outcome_assessment}
                            </p>
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="card">No timeline events compiled.</div>
                    )}
                  </div>

                  {/* Sources & agents steps */}
                  <div style={{ flex: 2 }}>
                    <h3 style={{ marginBottom: "12px" }}>📎 Context Sources</h3>
                    {queryResult.sources && queryResult.sources.length > 0 ? (
                      queryResult.sources.map((src, idx) => (
                        <div className="card" key={idx} style={{ padding: "16px", marginBottom: "12px" }}>
                          <div style={{ fontSize: "10px", color: "var(--text-muted)" }}>
                            {src.doc_type.toUpperCase()} · {src.date.substring(0, 10)} · MATCH: {Math.round(src.relevance_score * 100)}%
                          </div>
                          <div style={{ fontWeight: "bold", fontSize: "14px", margin: "4px 0" }}>{src.title}</div>
                          <p style={{ fontSize: "12px", color: "var(--text-muted)" }}>{src.excerpt}...</p>
                        </div>
                      ))
                    ) : (
                      <p style={{ color: "var(--text-muted)" }}>No sources indexed.</p>
                    )}

                    <h3 style={{ marginBottom: "12px", marginTop: "24px" }}>🤖 Agent Steps</h3>
                    {queryResult.processing_steps && queryResult.processing_steps.map((step, idx) => (
                      <div key={idx} className="step-badge" style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                        <CheckCircle2 size={13} style={{ color: "var(--accent-purple)" }} /> {step}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {/* ── TAB 2: TIMELINE ── */}
        {activeTab === "timeline" && (
          <div>
            <h1 className="page-title">📅 Decision Timeline</h1>
            <p className="page-subtitle">Compile and visualize chronological timeline logs for any query topic.</p>

            <form onSubmit={handleTimeline} className="search-form">
              <input 
                type="text" 
                className="search-input"
                placeholder="Azure migration, Vendor X, database auth..."
                value={timelineTopic}
                onChange={(e) => setTimelineTopic(e.target.value)}
                disabled={timelineLoading}
              />
              <button type="submit" className="btn-primary" disabled={timelineLoading}>
                {timelineLoading ? "Loading..." : "Build Timeline"}
              </button>
            </form>

            {timelineResult && (
              <div className="card">
                <h3 style={{ marginBottom: "20px" }}>Timeline Results for: <em>{timelineResult.topic}</em></h3>
                {timelineResult.events && timelineResult.events.map((event: any, idx: number) => {
                  const isCrit = event.is_critical;
                  const sentiment = event.sentiment || "neutral";
                  const cls = isCrit ? "critical" : sentiment === "dissent" ? "dissent" : sentiment === "concern" ? "concern" : "";
                  return (
                    <div className={`timeline-event-card ${cls}`} key={idx}>
                      <div className="timeline-event-meta">
                        {event.date} · {(event.event_type || "").toUpperCase()}
                      </div>
                      <div className="timeline-event-title">
                        {event.icon || "📅"} {event.title}
                      </div>
                      <div className="timeline-event-desc">{event.description}</div>
                      {event.participants && event.participants.length > 0 && (
                        <div className="timeline-event-participants">
                          👥 {event.participants.join(", ")}
                        </div>
                      )}
                    </div>
                  );
                })}

                {timelineResult.outcome_assessment && (
                  <div style={{ borderTop: "1px solid var(--border-subtle)", marginTop: "24px", paddingTop: "16px" }}>
                    <strong>🎯 Consolidated Outcomes:</strong>
                    <p style={{ color: "var(--text-muted)", fontSize: "14px", marginTop: "4px" }}>
                      {timelineResult.outcome_assessment}
                    </p>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* ── TAB 3: KNOWLEDGE GRAPH ── */}
        {activeTab === "graph" && (
          <div>
            <h1 className="page-title">🕸️ Knowledge Graph Explorer</h1>
            <p className="page-subtitle">Inspect the relational mapping of people, projects, decisions, and files in real-time.</p>

            <form onSubmit={handleGraphSearch} className="search-form">
              <input 
                type="text" 
                className="search-input"
                placeholder="Type a Person or Project name to search (e.g. Ravi Sharma, CloudMigration) or leave blank for full view..."
                value={graphEntity}
                onChange={(e) => setGraphEntity(e.target.value)}
                disabled={graphLoading}
              />
              <button type="submit" className="btn-primary" disabled={graphLoading}>
                {graphLoading ? "Loading Graph..." : "Explore"}
              </button>
            </form>

            <div className="card" style={{ padding: "16px" }}>
              <div 
                ref={graphContainerRef} 
                className="network-graph-wrapper"
              />
              <div style={{ display: "flex", gap: "16px", marginTop: "12px", fontSize: "12px", justifyContent: "center" }}>
                <div><span style={{ display: "inline-block", width: "10px", height: "10px", borderRadius: "50%", backgroundColor: "#8b5cf6", marginRight: "4px" }}></span>Center Query</div>
                <div><span style={{ display: "inline-block", width: "10px", height: "10px", borderRadius: "50%", backgroundColor: "#a78bfa", marginRight: "4px" }}></span>Person</div>
                <div><span style={{ display: "inline-block", width: "10px", height: "10px", borderRadius: "50%", backgroundColor: "#06b6d4", marginRight: "4px" }}></span>Project</div>
                <div><span style={{ display: "inline-block", width: "10px", height: "10px", borderRadius: "50%", backgroundColor: "#10b981", marginRight: "4px" }}></span>Decision</div>
                <div><span style={{ display: "inline-block", width: "10px", height: "10px", borderRadius: "50%", backgroundColor: "#f59e0b", marginRight: "4px" }}></span>Meeting</div>
                <div><span style={{ display: "inline-block", width: "10px", height: "10px", borderRadius: "50%", backgroundColor: "#ef4444", marginRight: "4px" }}></span>Ticket</div>
                <div><span style={{ display: "inline-block", width: "10px", height: "10px", borderRadius: "50%", backgroundColor: "#3b82f6", marginRight: "4px" }}></span>Email</div>
              </div>
            </div>
          </div>
        )}

        {/* ── TAB 4: INGESTION ── */}
        {activeTab === "ingest" && (
          <div>
            <h1 className="page-title">⚙️ Data Ingestion Panel</h1>
            <p className="page-subtitle">Load and index raw documents (emails, meeting logs, tickets) into vector space and graph nodes.</p>

            <div className="ingest-box">
              <h3>Database Storage Details</h3>
              <p style={{ color: "var(--text-muted)", fontSize: "14px", marginTop: "10px" }}>
                Place your source data JSON files into the following project directories inside the workspace:
              </p>
              <code>data/synthetic/emails/emails.json</code>
              <code>data/synthetic/meetings/meetings.json</code>
              <code>data/synthetic/jira/tickets.json</code>
            </div>

            <button 
              className="btn-primary" 
              onClick={handleIngest} 
              disabled={ingestLoading}
              style={{ height: "46px" }}
            >
              {ingestLoading ? "Triggering..." : "🚀 Start Async Ingestion"}
            </button>

            {ingestStatus && (
              <div className="card" style={{ marginTop: "24px" }}>
                <strong>Job Status Logs:</strong>
                <p style={{ color: "var(--text-muted)", fontSize: "14px", marginTop: "8px" }}>{ingestStatus}</p>
                {ingestJobId && (
                  <p style={{ fontSize: "12px", color: "var(--text-muted)", marginTop: "4px" }}>
                    Job ID: <code>{ingestJobId}</code>
                  </p>
                )}
              </div>
            )}
          </div>
        )}

        {/* ── TAB 5: HEALTH ── */}
        {activeTab === "health" && (
          <div>
            <h1 className="page-title">❤️ System Health & Connections</h1>
            <p className="page-subtitle">Monitor connection status across microservices and DB providers.</p>

            {healthStatus ? (
              <div>
                <div className="card">
                  <strong>API Gateway Connection:</strong> 
                  <span style={{ color: "var(--color-green)", marginLeft: "8px", fontWeight: "bold" }}>
                    {healthStatus.gateway ? healthStatus.gateway.toUpperCase() : "OK"}
                  </span>
                </div>

                <h3 style={{ margin: "24px 0 12px" }}>Microservices Status Indicators</h3>
                <div className="health-grid">
                  {healthStatus.services && Object.entries(healthStatus.services).map(([name, status]) => {
                    const isHealthy = status === "healthy";
                    return (
                      <div key={name} className="health-card" style={{ borderColor: isHealthy ? "var(--border-subtle)" : "var(--color-red)" }}>
                        <div style={{ textTransform: "capitalize", fontWeight: "bold" }}>{name}</div>
                        <div className="health-value" style={{ color: isHealthy ? "var(--color-green)" : "var(--color-red)" }}>
                          {isHealthy ? "● Healthy" : "○ Down"}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : (
              <div className="card" style={{ borderColor: "#ef4444" }}>
                <p style={{ color: "#ef4444" }}>
                  ❌ Connection refused. Unable to contact the API Gateway on port 8000. Ensure the Docker containers are built and running.
                </p>
              </div>
            )}
          </div>
        )}

      </main>
    </div>
  );
}
