import type { ReactNode } from "react";
import type { ApiError } from "../api/client";
import type { Job, Outcome } from "../api/types";
import { OUTCOME_GLYPH, OUTCOME_TEXT } from "../lib/format";

export function OutcomeBadge({ outcome }: { outcome: Outcome }) {
  return (
    <span className={`badge o-${outcome}`} data-outcome={outcome}>
      <span aria-hidden="true">{OUTCOME_GLYPH[outcome]}</span>
      {OUTCOME_TEXT[outcome]}
    </span>
  );
}

export function ErrorBox({ error, title = "Request failed" }: { error: ApiError | undefined; title?: string }) {
  if (!error) return null;
  return (
    <div className="notice error" role="alert">
      <strong>{title}:</strong> {error.message} <span className="tag">{error.code}</span>
      {error.status === 409 && <p className="small">Someone or something changed this first. Reload to see the current state.</p>}
    </div>
  );
}

export function Loading({ what }: { what: string }) {
  return <p className="muted" role="status">Loading {what}...</p>;
}

/** Real job state from the durable job row: state, attempts, stage and progress counters. */
export function JobStatus({ job, label }: { job: Job | undefined; label: string }) {
  if (!job) return null;
  const p = job.progress as { stage?: string; crop?: number; of?: number };
  const pct = p.crop && p.of ? Math.round((100 * (p.crop - 1)) / p.of) : undefined;
  const tone = job.state === "failed" ? "error" : job.state === "succeeded" ? "ok" : "warn";
  return (
    <div className={`notice ${tone}`} role="status" aria-live="polite">
      <div className="row">
        <strong>{label}</strong>
        <span className="tag">job {job.id}</span>
        <span>{job.state}</span>
        {job.attempts > 1 && <span className="small muted">attempt {job.attempts} of {job.max_attempts}</span>}
        {p.stage && job.state === "running" && <span className="small muted">{p.stage}{p.of ? ` ${p.crop}/${p.of}` : ""}</span>}
        {pct !== undefined && job.state === "running" && (
          <div className="progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={`${label} progress`}>
            <div style={{ width: `${pct}%` }} />
          </div>
        )}
      </div>
      {job.error && <pre className="small mono" style={{ whiteSpace: "pre-wrap", margin: "0.4rem 0 0" }}>{job.error.split("\n")[0]}</pre>}
    </div>
  );
}

export function Panel({ title, children, actions, id }: { title: ReactNode; children: ReactNode; actions?: ReactNode; id?: string }) {
  return (
    <section className="panel" aria-labelledby={id}>
      <div className="spread">
        <h2 id={id}>{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  );
}
