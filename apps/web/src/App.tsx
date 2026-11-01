import { useEffect, useRef } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { Readiness } from "./components/Readiness";
import { useReviewer } from "./lib/hooks";
import { Compare } from "./pages/Compare";
import { Library } from "./pages/Library";
import { Requirements } from "./pages/Requirements";
import { Scenarios } from "./pages/Scenarios";
import { SheetPage } from "./pages/Sheet";
import { Workspace } from "./pages/workspace/Workspace";

const TITLES: [RegExp, string][] = [
  [/^\/$/, "Sources"], [/^\/scenarios/, "Scenarios"], [/^\/scenario-revisions/, "Scenario"], [/^\/sheets/, "Sheet review"],
  [/^\/requirements/, "Profiles and requirements"], [/^\/compare/, "Compare and replay"],
];

function Reviewer() {
  const [name, setName] = useReviewer();
  return (
    <label className="small" style={{ marginTop: "1rem" }}>
      Reviewing as
      <input value={name} onChange={(e) => setName(e.target.value)} placeholder="your name or initials" aria-describedby="reviewer-help" />
      <span id="reviewer-help" className="muted" style={{ fontSize: "0.75rem" }}>Recorded on every decision you make here, as human UI review.</span>
    </label>
  );
}

function NotFound() {
  return <div><h1>Not found</h1><p>This page does not exist. Use the navigation to continue.</p></div>;
}

export function App() {
  const loc = useLocation();
  const main = useRef<HTMLElement>(null);
  useEffect(() => {
    const t = TITLES.find(([re]) => re.test(loc.pathname))?.[1] ?? "Ghost Routes";
    document.title = `${t} | Ghost Routes Studio`;
    // Move focus to the new page's heading so keyboard and screen reader users land on the content.
    const h = main.current?.querySelector("h1");
    if (h) { h.setAttribute("tabindex", "-1"); h.focus({ preventScroll: true }); }
  }, [loc.pathname]);
  return (
    <div className="shell">
      <a className="skip" href="#main">Skip to content</a>
      <aside className="side">
        <div className="brand">Ghost <span>Routes</span></div>
        <div className="small muted">Pedestrian access under construction phasing</div>
        <nav aria-label="Primary">
          <ul>
            <li><NavLink to="/" end>Sources</NavLink></li>
            <li><NavLink to="/scenarios">Scenarios</NavLink></li>
            <li><NavLink to="/requirements">Profiles and requirements</NavLink></li>
            <li><NavLink to="/compare">Compare and replay</NavLink></li>
          </ul>
        </nav>
        <Readiness />
        <Reviewer />
        <p className="small muted" style={{ marginTop: "1.5rem" }}>Results describe the reviewed model and its coverage, not real-world safety or legal compliance.</p>
      </aside>
      <main id="main" ref={main}>
        <Routes>
          <Route path="/" element={<Library />} />
          <Route path="/scenarios" element={<Scenarios />} />
          <Route path="/scenario-revisions/:revisionId" element={<Workspace />} />
          <Route path="/sheets/:sheetId" element={<SheetPage />} />
          <Route path="/requirements" element={<Requirements />} />
          <Route path="/compare" element={<Compare />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </div>
  );
}
