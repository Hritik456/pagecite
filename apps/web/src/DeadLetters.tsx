import { useCallback, useEffect, useState } from "react";
import { ApiError, listDeadLetters, replayDocument, type DeadLetter } from "./api";

const KIND_LABEL: Record<DeadLetter["kind"], string> = {
  exhausted: "retries exhausted",
  permanent: "permanent error",
  attempt_limit: "attempt limit",
};

/** Open dead letters (final failures) with a Replay button: reprocess as a new version. */
export default function DeadLetters({ tenantKey, active }: { tenantKey: string; active: boolean }) {
  const [items, setItems] = useState<DeadLetter[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(async () => {
    if (!tenantKey) return;
    try {
      setItems((await listDeadLetters(tenantKey)).deadLetters);
    } catch (e) {
      setMessage(e instanceof ApiError ? `${e.message} (${e.code})` : String(e));
    }
  }, [tenantKey]);

  useEffect(() => {
    if (active) void load();
  }, [active, load]);

  async function replay(item: DeadLetter) {
    setBusy(item.documentId);
    setMessage(null);
    try {
      const res = await replayDocument(tenantKey, item.documentId);
      setMessage(`${item.filename}: replaying as version ${res.document.processingVersion}.`);
      await load();
    } catch (e) {
      setMessage(e instanceof ApiError ? `${e.message} (${e.code})` : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
        <h2 className="font-medium">Dead letters</h2>
        <button onClick={load} className="text-sm text-slate-600 underline">
          Refresh
        </button>
      </div>
      {message && <p className="border-b border-slate-100 px-5 py-2 text-sm text-slate-700">{message}</p>}
      {items.length === 0 ? (
        <p className="px-5 py-6 text-sm text-slate-500">No open failures.</p>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-slate-500">
            <tr>
              <th className="px-5 py-2 font-medium">Document</th>
              <th className="px-5 py-2 font-medium">Stage</th>
              <th className="px-5 py-2 font-medium">Why</th>
              <th className="px-5 py-2 font-medium" />
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.id} className="border-t border-slate-100 align-top">
                <td className="px-5 py-2">
                  {item.filename}
                  <div className="text-xs text-slate-500">
                    document {item.documentId}, version {item.version}
                  </div>
                </td>
                <td className="px-5 py-2 font-mono text-xs">{item.stage}</td>
                <td className="px-5 py-2">
                  <span className="rounded bg-red-100 px-1.5 py-0.5 text-xs text-red-800">{KIND_LABEL[item.kind]}</span>
                  <div className="mt-1 font-mono text-xs text-slate-600">{item.error}</div>
                </td>
                <td className="px-5 py-2 text-right">
                  <button
                    onClick={() => replay(item)}
                    disabled={busy !== null}
                    className="rounded bg-slate-900 px-3 py-1 text-xs font-medium text-white disabled:opacity-40"
                  >
                    {busy === item.documentId ? "Replaying…" : "Replay"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
