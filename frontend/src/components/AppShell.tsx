import {
  Activity,
  Bot,
  ChartNoAxesCombined,
  Coffee,
  FlaskConical,
  Gauge,
  LayoutDashboard,
  Menu,
  Network,
  X,
} from "lucide-react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useEffect, useState } from "react";
import { backendConnection, getBackendHealth } from "../api/client";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { to: "/traffic-lab", label: "Traffic Lab", icon: FlaskConical },
  { to: "/ai-qos", label: "AI & QoS", icon: Bot },
  { to: "/experiments", label: "Experiments", icon: ChartNoAxesCombined },
];

export function AppShell() {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [apiStatus, setApiStatus] = useState<"not-configured" | "checking" | "connected" | "unavailable">(
    backendConnection.baseUrl ? "checking" : "not-configured",
  );
  const location = useLocation();
  const currentPage = NAV_ITEMS.find((item) => item.to === location.pathname)?.label ?? "Dashboard";

  useEffect(() => {
    if (!backendConnection.baseUrl) {
      setApiStatus("not-configured");
      return;
    }
    let active = true;
    getBackendHealth().then(
      () => active && setApiStatus("connected"),
      () => active && setApiStatus("unavailable"),
    );
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className="app-frame">
      <aside className={`sidebar ${mobileMenuOpen ? "sidebar--open" : ""}`}>
        <div className="brand-lockup">
          <div className="brand-mark"><Coffee size={20} /></div>
          <div>
            <strong>Adaptive QoS</strong>
            <span>Network studio</span>
          </div>
          <button
            className="icon-button sidebar-close"
            onClick={() => setMobileMenuOpen(false)}
            aria-label="Close navigation"
          >
            <X size={18} />
          </button>
        </div>
        <div className="sidebar-label">WORKSPACE</div>
        <nav className="primary-nav" aria-label="Main navigation">
          {NAV_ITEMS.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              to={to}
              end={end}
              key={to}
              onClick={() => setMobileMenuOpen(false)}
              className={({ isActive }) => `nav-link ${isActive ? "nav-link--active" : ""}`}
            >
              <Icon size={17} />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-bottom__icon"><Network size={17} /></div>
          <div><strong>Offline first</strong><span>No live network controls</span></div>
        </div>
      </aside>

      {mobileMenuOpen && (
        <button
          className="mobile-scrim"
          aria-label="Close navigation overlay"
          onClick={() => setMobileMenuOpen(false)}
        />
      )}

      <main className="main-column">
        <header className="topbar">
          <button
            className="icon-button mobile-menu"
            onClick={() => setMobileMenuOpen(true)}
            aria-label="Open navigation"
          >
            <Menu size={20} />
          </button>
          <div className="topbar__context">
            <span className="breadcrumb">Adaptive QoS</span>
            <span className="breadcrumb-divider">/</span>
            <span className="breadcrumb-current"><Activity size={14} /> {currentPage} workspace</span>
          </div>
          <div className="topbar__status">
            <span className={`connection-dot ${apiStatus === "connected" ? "connection-dot--connected" : ""}`} />
            <span>
              {apiStatus === "connected"
                ? "Dashboard API connected"
                : apiStatus === "checking"
                  ? "Checking API"
                  : apiStatus === "unavailable"
                    ? "API unavailable"
                    : "API not configured"}
            </span>
            <span className="topbar-divider" />
            <Gauge size={15} />
            <span>Demo fixtures labelled</span>
          </div>
        </header>
        <div className="page-container"><Outlet /></div>
        <footer className="app-footer">
          <span>Adaptive QoS · Guarded execution integration</span>
          <span>Prediction plans are not policy applications.</span>
        </footer>
      </main>
    </div>
  );
}
