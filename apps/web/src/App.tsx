import { useEffect, useState } from "react";
import {
  ApiError,
  completeDocument,
  createBatch,
  getBatch,
  mimeFor,
  putToStorage,
  TERMINAL_BATCH_STATUSES,
  TERMINAL_DOCUMENT_STATUSES,
  type BatchResponse,
  type FileSpec,
  type StageName,
  type StageView,
} from "./api";
import { sha256Hex } from "./sha256";

const KEY_STORAGE = "pagecite.tenantKey";
const PARALLEL_UPLOADS = 3;
const POLL_MS = 2000;

type Phase = "hashing" | "queued" | "uploading" | "completing" | "done" | "error";
type Row = { name: string; size: number; phase: Phase; documentId?: number; error?: string };

export default function App() {
  const [tenantKey, setTenantKey] = useState(() => localStorage.getItem(KEY_STORAGE) ?? "");
  const [files, setFiles] = useState<File[]>([]);
  const [rows, setRows] = useState<Row[]>([]);
  const [batch, setBatch] = useState<BatchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const saveKey = (value: string) => {
    setTenantKey(value);
    localStorage.setItem(KEY_STORAGE, value);
  };

  const patchRow = (i: number, patch: Partial<Row>) =>
    setRows((prev) => prev.map((row, j) => (j === i ? { ...row, ...patch } : row)));

  async function upload() {
    setBusy(true);
    setError(null);
    setBatch(null);
    setRows(files.map((f) => ({ name: f.name, size: f.size, phase: "hashing" })));
    try {
      const specs: FileSpec[] = await Promise.all(
        files.map(async (f) => ({ name: f.name, size: f.size, sha256: await sha256Hex(f), mime: mimeFor(f) })),
      );

      // Quotas are checked here: if the batch is rejected, no file is uploaded.
      const created = await createBatch(tenantKey, specs);
      setRows((prev) =>
        prev.map((row, i) => ({ ...row, phase: "queued", documentId: created.uploads[i].documentId })),
      );

      await runPool(created.uploads.length, PARALLEL_UPLOADS, async (i) => {
        const up = created.uploads[i];
        try {
          patchRow(i, { phase: "uploading" });
          await putToStorage(up, files[i]);
          patchRow(i, { phase: "completing" });
          await completeDocument(tenantKey, up.documentId);
          patchRow(i, { phase: "done" });
        } catch (e) {
          patchRow(i, { phase: "error", error: describe(e) });
        }
      });

      setBatch(await getBatch(tenantKey, created.batchId));
    } catch (e) {
      setError(describe(e));
      setRows([]);
    } finally {
      setBusy(false);
    }
  }

  async function refresh() {
    if (!batch) return;
    try {
      setBatch(await getBatch(tenantKey, batch.batch.id));
    } catch (e) {
      setError(describe(e));
    }
  }

  // Live progress: poll every 2 s until the batch settles. By the time `batch` is set the uploads have
  // finished, so a document still CREATED is one whose upload failed: it will not move either.
  const batchId = batch?.batch.id;
  const settled =
    !batch ||
    TERMINAL_BATCH_STATUSES.has(batch.batch.status) ||
    batch.documents.every((d) => TERMINAL_DOCUMENT_STATUSES.has(d.status) || d.status === "CREATED");
  useEffect(() => {
    if (batchId === undefined || settled) return;
    const timer = setInterval(async () => {
      try {
        setBatch(await getBatch(tenantKey, batchId));
      } catch (e) {
        setError(describe(e));
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [batchId, settled, tenantKey]);

  const docById = new Map(batch?.documents.map((d) => [d.id, d]));

  return (
    <main className="mx-auto max-w-4xl space-y-6 p-8">
      <header>
        <h1 className="text-2xl font-semibold">PageCite</h1>
        <p className="text-sm text-slate-600">
          Upload PDF or DOCX files. They go straight to storage, are verified, then parsed, chunked, embedded and indexed.
        </p>
      </header>

      <section className="space-y-4 rounded-lg border border-slate-200 bg-white p-5 shadow-sm">
        <label className="block">
          <span className="text-sm font-medium">Tenant key (X-Tenant-Key)</span>
          <input
            type="password"
            value={tenantKey}
            onChange={(e) => saveKey(e.target.value)}
            placeholder="pc_..."
            className="mt-1 w-full rounded border border-slate-300 px-3 py-2 font-mono text-sm"
          />
        </label>

        <label className="block">
          <span className="text-sm font-medium">Files</span>
          <input
            type="file"
            multiple
            accept=".pdf,.docx,application/pdf"
            onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
            className="mt-1 block w-full text-sm file:mr-3 file:rounded file:border-0 file:bg-slate-100 file:px-3 file:py-2"
          />
        </label>

        <button
          onClick={upload}
          disabled={busy || !tenantKey || files.length === 0}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
        >
          {busy ? "Uploading…" : `Upload ${files.length || ""} file${files.length === 1 ? "" : "s"}`}
        </button>

        {error && (
          <div role="alert" className="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
            {error}
          </div>
        )}
      </section>

      {rows.length > 0 && (
        <section className="rounded-lg border border-slate-200 bg-white shadow-sm">
          <div className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
            <h2 className="font-medium">
              {batch ? `Batch #${batch.batch.id} · ${batch.batch.status}` : "Batch"}
            </h2>
            {batch &&
              (settled ? (
                <button onClick={refresh} className="text-sm text-slate-600 underline">
                  Refresh
                </button>
              ) : (
                <span className="flex items-center gap-2 text-sm text-slate-600">
                  <span className="h-2 w-2 animate-pulse rounded-full bg-blue-500" /> live
                </span>
              ))}
          </div>
          <table className="w-full text-sm">
            <thead className="text-left text-slate-500">
              <tr>
                <th className="px-5 py-2 font-medium">File</th>
                <th className="px-5 py-2 font-medium">Size</th>
                <th className="px-5 py-2 font-medium">Upload</th>
                <th className="px-5 py-2 font-medium">Pipeline</th>
                <th className="px-5 py-2 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => {
                const doc = row.documentId !== undefined ? docById.get(row.documentId) : undefined;
                return (
                  <tr key={i} className="border-t border-slate-100">
                    <td className="px-5 py-2">{row.name}</td>
                    <td className="px-5 py-2 tabular-nums">{formatBytes(row.size)}</td>
                    <td className="px-5 py-2">
                      <PhaseBadge phase={row.phase} />
                      {row.error && <div className="text-xs text-red-700">{row.error}</div>}
                    </td>
                    <td className="px-5 py-2">{doc ? <StageChips stages={doc.stages ?? []} /> : "—"}</td>
                    <td className="px-5 py-2 font-mono text-xs">{doc?.status ?? "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      )}
    </main>
  );
}

const STAGES: StageName[] = ["parse", "chunk", "embed", "index"];
const STAGE_TONE: Record<string, string> = {
  PENDING: "bg-slate-100 text-slate-500",
  RUNNING: "bg-blue-100 text-blue-800 animate-pulse",
  RETRY_WAIT: "bg-amber-100 text-amber-800",
  SUCCEEDED: "bg-green-100 text-green-800",
  FAILED: "bg-red-100 text-red-800",
  CANCELLED: "bg-slate-200 text-slate-600",
};

/** parse → chunk → embed → index, each coloured by its status (grey = not started). */
function StageChips({ stages }: { stages: StageView[] }) {
  const byName = new Map(stages.map((s) => [s.stage, s]));
  return (
    <div className="flex gap-1">
      {STAGES.map((name) => {
        const s = byName.get(name);
        const status = s?.status ?? "PENDING";
        const title = s ? `${name}: ${status}${s.attempts > 1 ? ` (attempt ${s.attempts})` : ""}` : `${name}: not started`;
        return (
          <span key={name} title={title} className={`rounded px-1.5 py-0.5 text-xs ${STAGE_TONE[status]}`}>
            {name}
          </span>
        );
      })}
    </div>
  );
}

function PhaseBadge({ phase }: { phase: Phase }) {
  const tone: Record<Phase, string> = {
    hashing: "bg-slate-100 text-slate-700",
    queued: "bg-slate-100 text-slate-700",
    uploading: "bg-blue-100 text-blue-800",
    completing: "bg-blue-100 text-blue-800",
    done: "bg-green-100 text-green-800",
    error: "bg-red-100 text-red-800",
  };
  return <span className={`rounded px-2 py-0.5 text-xs ${tone[phase]}`}>{phase}</span>;
}

/** Run task(0..n-1) with at most `limit` running at once. */
async function runPool(n: number, limit: number, task: (i: number) => Promise<void>) {
  let next = 0;
  const worker = async () => {
    while (next < n) await task(next++);
  };
  await Promise.all(Array.from({ length: Math.min(limit, n) }, worker));
}

function describe(e: unknown): string {
  if (e instanceof ApiError) return `${e.message} (${e.code})`;
  return e instanceof Error ? e.message : String(e);
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
