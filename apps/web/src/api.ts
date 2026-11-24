/** Typed client for the PageCite API (served under /api by Caddy and by the Vite dev proxy). */

export const PDF_MIME = "application/pdf";
export const DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

export type FileSpec = { name: string; size: number; sha256: string; mime: string };

export type Upload = {
  documentId: number;
  presignedUrl: string;
  /** Signed into the URL: the PUT must send exactly these. */
  headers: Record<string, string>;
};

export type CreateBatchResponse = { batchId: number; uploads: Upload[] };

export type DocumentView = {
  id: number;
  batchId: number;
  filename: string;
  sizeBytes: number;
  mime: string;
  status: string;
  processingVersion: number;
};

export type BatchView = { id: number; status: string; total: number; succeeded: number; failed: number };

export type BatchResponse = { batch: BatchView; documents: DocumentView[] };

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, key: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { "X-Tenant-Key": key };
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`/api${path}`, { ...init, headers });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const err = body?.error;
    throw new ApiError(res.status, err?.code ?? `HTTP_${res.status}`, err?.message ?? res.statusText, err?.request_id);
  }
  return body as T;
}

export const createBatch = (key: string, files: FileSpec[]) =>
  request<CreateBatchResponse>("/batches", key, { method: "POST", body: JSON.stringify({ files }) });

export const completeDocument = (key: string, documentId: number) =>
  request<{ document: DocumentView }>(`/documents/${documentId}/complete`, key, { method: "POST" });

export const getBatch = (key: string, batchId: number) => request<BatchResponse>(`/batches/${batchId}`, key);

/** PUT the file straight to S3/MinIO with the signed headers. */
export async function putToStorage(upload: Upload, file: File): Promise<void> {
  const res = await fetch(upload.presignedUrl, { method: "PUT", body: file, headers: upload.headers });
  if (!res.ok) throw new Error(`storage rejected the upload (HTTP ${res.status})`);
}

/** Browsers sometimes report "" for .docx; fall back to the extension. */
export function mimeFor(file: File): string {
  const ext = file.name.split(".").pop()?.toLowerCase();
  if (ext === "pdf") return PDF_MIME;
  if (ext === "docx") return DOCX_MIME;
  return file.type || "application/octet-stream";
}
