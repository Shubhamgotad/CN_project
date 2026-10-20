import { lazy, Suspense, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import type { ImportedEvidenceReport, ImportedRunnerReport } from "./types";

const Dashboard = lazy(() => import("./pages/Dashboard").then((module) => ({ default: module.Dashboard })));
const TrafficLab = lazy(() => import("./pages/TrafficLab").then((module) => ({ default: module.TrafficLab })));
const AiQos = lazy(() => import("./pages/AiQos").then((module) => ({ default: module.AiQos })));
const Experiments = lazy(() => import("./pages/Experiments").then((module) => ({ default: module.Experiments })));

export default function App() {
  const [runnerReport, setRunnerReport] = useState<ImportedRunnerReport | null>(null);
  const [runnerReportSource, setRunnerReportSource] = useState<"api" | "imported">("imported");
  const [evidenceReport, setEvidenceReport] = useState<ImportedEvidenceReport | null>(null);
  const [evidenceReportSource, setEvidenceReportSource] = useState<"api" | "imported">("imported");
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Suspense fallback={<div className="page-loading">Loading dashboard...</div>}><Dashboard /></Suspense>} />
        <Route path="traffic-lab" element={<Suspense fallback={<div className="page-loading">Loading Traffic Lab...</div>}><TrafficLab /></Suspense>} />
        <Route path="ai-qos" element={<Suspense fallback={<div className="page-loading">Loading AI & QoS...</div>}><AiQos report={runnerReport} reportSource={runnerReportSource} onReport={(report, source) => { setRunnerReport(report); setRunnerReportSource(source); }} /></Suspense>} />
        <Route path="experiments" element={<Suspense fallback={<div className="page-loading">Loading experiments...</div>}><Experiments report={evidenceReport} reportSource={evidenceReportSource} onReport={(report, source) => { setEvidenceReport(report); setEvidenceReportSource(source); }} /></Suspense>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
