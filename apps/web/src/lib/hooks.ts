import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, unwrap } from "../api/client";
import type { Job } from "../api/types";

export type Load<T> = { data: T | undefined; error: ApiError | undefined; loading: boolean; reload: () => void };

/** Load data from the API; reruns when deps change and on reload(). Stale responses are dropped. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[]): Load<T> {
  const [state, setState] = useState<{ data?: T; error?: ApiError; loading: boolean }>({ loading: true });
  const [tick, setTick] = useState(0);
  const seq = useRef(0);
  const key = JSON.stringify(deps);
  const lastKey = useRef(key);
  // Data for other inputs must never be shown (or acted on) as if it belonged to the new ones.
  const stale = lastKey.current !== key;
  useEffect(() => {
    const mine = ++seq.current;
    const changed = lastKey.current !== key;
    lastKey.current = key;
    setState((s) => (changed ? { loading: true } : { ...s, loading: true }));
    fn().then(
      (data) => mine === seq.current && setState({ data, loading: false }),
      (error: ApiError) => mine === seq.current && setState({ error, loading: false }),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return stale ? { data: undefined, error: undefined, loading: true, reload } : { data: state.data, error: state.error, loading: state.loading, reload };
}

export const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

/** Follow a durable job by polling its real state; calls onDone once when it reaches a terminal state. */
export function useJob(jobId: number | undefined, onDone?: (job: Job) => void): Job | undefined {
  const [job, setJob] = useState<Job>();
  const done = useRef(onDone);
  done.current = onDone;
  useEffect(() => {
    if (jobId === undefined) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const j = await unwrap(api.GET("/api/jobs/{job_id}", { params: { path: { job_id: jobId } } }));
        if (stop) return;
        setJob(j);
        if (TERMINAL.has(j.state)) {
          done.current?.(j);
          return;
        }
      } catch {
        // Keep polling through transient failures; the job row is durable.
      }
      if (!stop) timer = setTimeout(poll, 800);
    };
    void poll();
    return () => {
      stop = true;
      clearTimeout(timer);
    };
  }, [jobId]);
  return job;
}

const REVIEWER_KEY = "ghost-routes.reviewer";

/** The reviewer label attributed to UI decisions (stored per browser). */
export function useReviewer(): [string, (v: string) => void] {
  const [name, setName] = useState(() => {
    try {
      return localStorage.getItem(REVIEWER_KEY) ?? "";
    } catch {
      return "";
    }
  });
  const set = useCallback((v: string) => {
    setName(v);
    try {
      localStorage.setItem(REVIEWER_KEY, v);
    } catch {
      // Private mode: the label still works for this page.
    }
  }, []);
  return [name, set];
}
