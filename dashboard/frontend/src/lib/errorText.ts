// Turns a failed request into words a person can read. A raw ApiError.message carries the status
// and the request URL, so a page never renders it: it shows the server's own `detail` instead, or a
// plain sentence when the server sent none. A detail that begins with a machine code ("no_data_yet: ...")
// or is only a code shows in words (plainDetail). A body that is not JSON, or a network failure, has no
// words a person can use, so it reads as one plain sentence (COULD_NOT_LOAD).
import { ApiError } from "./api";

const CODE_PREFIX = /^[a-z0-9]+(?:[_-][a-z0-9]+)+: /;
const WHOLLY_CODE = /^[a-z0-9]+(?:[_-][a-z0-9]+)+$/;

const upperFirst = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

// A leading code prefix is dropped; a text that is only a code (at least two parts, so a plain word
// like "none" stays) becomes words; anything else stays as the server wrote it.
export function plainDetail(text: string): string {
  if (CODE_PREFIX.test(text)) return upperFirst(text.replace(CODE_PREFIX, ""));
  if (WHOLLY_CODE.test(text)) return upperFirst(text.replace(/[_-]+/g, " "));
  return text;
}

export const COULD_NOT_LOAD = "Could not load this from the server.";

// The messages browsers give a fetch that never got an answer (as sindresorhus/is-network-error matches them):
// Chromium "Failed to fetch", Safari "Load failed", each with an optional "(host)", and Firefox's sentence.
const NETWORK_FAILURE = /^(?:Failed to fetch|Load failed)(?: \(.*\))?$|^NetworkError when attempting to fetch resource\.$/;
const isNetworkFailure = (message: string) => NETWORK_FAILURE.test(message);

export function serverDetail(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body;
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail.length > 0) return plainDetail(detail);
    }
    return "The server refused this request.";
  }
  if (err instanceof SyntaxError || (err instanceof TypeError && isNetworkFailure(err.message))) return COULD_NOT_LOAD;
  if (err instanceof Error) return plainDetail(err.message);
  return "Something went wrong.";
}
