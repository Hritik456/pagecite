import { useState, type FormEvent } from "react";
import { ApiError, askStream, documentUrl, type Citation } from "./api";
import { readSse } from "./sse";

const ABSTAIN = "Not found in the documents.";
const MARKER = /(\[\d+(?:\s*,\s*\d+)*\])/; // capture group: split() keeps the markers
const IS_MARKER = /^\[\d+(?:\s*,\s*\d+)*\]$/; // no /g: .test() must not keep lastIndex state

type ErrorInfo = { message: string; requestId?: string };

export default function Ask({ tenantKey }: { tenantKey: string }) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<Citation[] | null>(null);
  const [abstained, setAbstained] = useState(false);
  const [error, setError] = useState<ErrorInfo | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!question.trim() || busy) return;
    setBusy(true);
    setAnswer("");
    setCitations(null);
    setAbstained(false);
    setError(null);
    try {
      const res = await askStream(tenantKey, question);
      const requestId = res.headers.get("X-Request-ID") ?? undefined;
      for await (const { event, data } of readSse(res)) {
        if (event === "token") setAnswer((a) => a + (data as { text: string }).text);
        else if (event === "citations") {
          const d = data as { citations: Citation[]; abstained: boolean };
          setCitations(d.citations);
          setAbstained(d.abstained);
        } else if (event === "error") {
          const d = data as { message: string; request_id?: string };
          setError({ message: d.message, requestId: d.request_id ?? requestId });
        }
      }
    } catch (err) {
      setError(
        err instanceof ApiError
          ? { message: `${err.message} (${err.code})`, requestId: err.requestId }
          : { message: err instanceof Error ? err.message : String(err) },
      );
    } finally {
      setBusy(false);
    }
  }

  async function open(c: Citation) {
    // Open the tab synchronously (popup blockers), then point it at the presigned URL.
    const tab = window.open("about:blank", "_blank");
    try {
      const { url } = await documentUrl(tenantKey, c.document_id);
      if (tab) {
        tab.opener = null;
        tab.location.href = `${url}#page=${c.page}`;
      }
    } catch (err) {
      tab?.close();
      setError({ message: err instanceof Error ? err.message : String(err) });
    }
  }

  const byN = new Map((citations ?? []).map((c) => [c.n, c]));

  return (
    <section className="space-y-4 rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
      <form onSubmit={submit} className="flex gap-2">
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          maxLength={1000}
          placeholder="Ask a question about your documents, e.g. What does circular FIAS/HR/2025-26/014 say about daily allowance?"
          className="flex-1 rounded border border-slate-300 px-3 py-2 text-sm"
        />
        <button
          disabled={busy || !tenantKey || !question.trim()}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
        >
          {busy ? "Answering…" : "Ask"}
        </button>
      </form>

      {(answer || busy) && (
        <div
          className={`whitespace-pre-wrap rounded border px-4 py-3 text-sm leading-6 ${
            abstained || answer.trim() === ABSTAIN
              ? "border-amber-200 bg-amber-50 text-amber-900"
              : "border-slate-200 bg-slate-50"
          }`}
        >
          {answer.split(MARKER).map((part, i) =>
            IS_MARKER.test(part) ? (
              <Markers key={i} marker={part} byN={byN} onOpen={open} />
            ) : (
              <span key={i}>{part}</span>
            ),
          )}
          {busy && <span className="ml-1 inline-block h-4 w-2 animate-pulse bg-slate-400 align-middle" />}
        </div>
      )}

      {citations && citations.length > 0 && (
        <div>
          <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-slate-500">Sources</h3>
          <ul className="space-y-1 text-sm">
            {citations.map((c) => (
              <li key={c.n}>
                <button onClick={() => open(c)} className="text-left hover:underline">
                  <span className="mr-2 rounded bg-blue-100 px-1.5 py-0.5 text-xs font-medium text-blue-800">{c.n}</span>
                  {c.title}, {c.page === c.page_to ? `p.${c.page}` : `pp.${c.page}–${c.page_to}`}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {error && (
        <div role="alert" className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
          {error.message}
          {error.requestId && <span className="ml-2 font-mono text-xs text-red-600">request {error.requestId}</span>}
        </div>
      )}
    </section>
  );
}

/** "[2][3]" or "[1, 3]": one chip per number; cited numbers open the document at that page. */
function Markers({
  marker,
  byN,
  onOpen,
}: {
  marker: string;
  byN: Map<number, Citation>;
  onOpen: (c: Citation) => void;
}) {
  const numbers = marker.slice(1, -1).split(",").map((s) => Number(s.trim()));
  return (
    <>
      {numbers.map((n, i) => {
        const c = byN.get(n);
        return c ? (
          <button
            key={i}
            onClick={() => onOpen(c)}
            title={`${c.title}, p.${c.page}`}
            className="mx-0.5 rounded bg-blue-100 px-1.5 text-xs font-medium text-blue-800 hover:bg-blue-200"
          >
            {n}
          </button>
        ) : (
          <span key={i} className="mx-0.5 rounded bg-slate-200 px-1.5 text-xs text-slate-600">
            {n}
          </span>
        );
      })}
    </>
  );
}
