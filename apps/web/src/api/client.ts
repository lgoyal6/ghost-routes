import createClient from "openapi-fetch";
import type { paths } from "./schema";

export const api = createClient<paths>({ baseUrl: "" });

/** A structured API error ({error: {code, message, detail}}) or a network failure. */
export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public detail: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

type Result<T> = { data?: T; error?: unknown; response: Response };

/** Return data or throw an ApiError carrying the server's structured error. */
export async function unwrap<T>(p: Promise<Result<T>>): Promise<T> {
  let res: Result<T>;
  try {
    res = await p;
  } catch (e) {
    throw new ApiError(0, "network", `The API is unreachable (${(e as Error).message}). Is \`make dev\` running?`);
  }
  if (res.error !== undefined || !res.response.ok) {
    const body = (res.error ?? {}) as { error?: { code?: string; message?: string; detail?: Record<string, unknown> } };
    const err = body.error;
    throw new ApiError(res.response.status, err?.code ?? "http_error", err?.message ?? res.response.statusText, err?.detail ?? {});
  }
  return res.data as T;
}
