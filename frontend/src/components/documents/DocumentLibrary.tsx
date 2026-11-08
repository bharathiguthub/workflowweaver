import { useCallback, useEffect, useState } from "react";

import { errorMessage, listDocumentLibrary, previewDocument } from "../../api/client";
import type { DocumentLibraryResponse, DocumentPreview, ExecutionMode } from "../../api/types";
import { Alert, Badge, Spinner } from "../StatusBadge";
import { Card, buttonSecondary } from "../layout/Page";
import { MarkdownPreview } from "./MarkdownPreview";

const size = (bytes: number | null) => (bytes === null ? "-" : bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`);

/**
 * Read-only list of documents in the approved Google Drive sandbox folder that this app can access
 * (drive.file). It never lists the rest of the user's Drive. Previewing reads one file; nothing is written.
 */
export function DocumentLibrary({ mode, refreshKey = 0 }: { mode: ExecutionMode; refreshKey?: number }) {
  const [library, setLibrary] = useState<DocumentLibraryResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [preview, setPreview] = useState<DocumentPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setLibrary(await listDocumentLibrary(mode));
    } catch (e) {
      setLibrary(null);
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [mode]);

  useEffect(() => {
    setSelected(null);
    setPreview(null);
    setPreviewError(null);
    void load();
  }, [load, refreshKey]);

  const open = async (fileId: string) => {
    setSelected(fileId);
    setPreviewing(true);
    setPreviewError(null);
    setPreview(null);
    try {
      setPreview(await previewDocument(fileId, mode));
    } catch (e) {
      setPreviewError(errorMessage(e));
    } finally {
      setPreviewing(false);
    }
  };

  return (
    <Card
      id="document-library"
      title="Sandbox documents"
      subtitle={
        <>
          Documents in the approved sandbox folder
          {library?.folder_alias ? (
            <>
              {" "}
              <span className="font-mono text-xs">{library.folder_alias}</span>
            </>
          ) : null}{" "}
          that WorkflowWeaver can access. It cannot browse the rest of your Google Drive.
        </>
      }
      actions={
        <button type="button" onClick={() => void load()} disabled={loading} className={buttonSecondary}>
          Refresh
        </button>
      }
    >
      {library?.simulated && (
        <p className="mb-3 text-xs text-slate-500">
          <Badge tone="warning" label="Simulated" /> {mode === "dry_run" ? "Dry run" : "Mock mode"} lists synthetic documents and never
          contacts Google Drive.
        </p>
      )}
      {loading && <Spinner label="Loading sandbox documents…" />}
      {error && <Alert title="Could not list sandbox documents">{error}</Alert>}
      {library && !loading && library.documents.length === 0 && (
        <p className="text-sm text-slate-500">No accessible documents in the sandbox folder yet.</p>
      )}
      {library && library.documents.length > 0 && (
        <ul aria-label="Sandbox documents" className="divide-y divide-slate-100 rounded-lg border border-slate-200">
          {library.documents.map((d) => (
            <li key={d.file_id} className={`flex flex-wrap items-center justify-between gap-2 p-3 ${selected === d.file_id ? "bg-indigo-50/60" : ""}`}>
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-slate-800">{d.name}</p>
                <p className="text-xs text-slate-500">
                  {size(d.size_bytes)}
                  {d.modified_time ? ` · ${new Date(d.modified_time).toLocaleString()}` : ""}
                  {d.duplicate_name ? " · another file has the same name" : ""}
                </p>
              </div>
              <button
                type="button"
                onClick={() => void open(d.file_id)}
                aria-label={`Preview ${d.name}`}
                aria-pressed={selected === d.file_id}
                className={buttonSecondary}
              >
                Preview
              </button>
            </li>
          ))}
        </ul>
      )}
      {previewing && (
        <div className="mt-4">
          <Spinner label="Reading the document (read-only)…" />
        </div>
      )}
      {previewError && (
        <div className="mt-4">
          <Alert title="Could not preview the document">{previewError}</Alert>
        </div>
      )}
      {preview && (
        <div className="mt-4 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-semibold text-slate-900">{preview.name}</p>
            <Badge tone={preview.simulated ? "warning" : "success"} label={preview.simulated ? "Simulated" : "Read from Google Drive"} />
            <span className="text-xs text-slate-500">{size(preview.size_bytes)} · read-only</span>
          </div>
          <MarkdownPreview content={preview.content} label={`Preview of ${preview.name}`} />
          <details className="text-xs text-slate-600">
            <summary className="cursor-pointer">Checks performed ({preview.checks.length})</summary>
            <ul className="mt-1 list-disc pl-5">
              {preview.checks.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </details>
        </div>
      )}
    </Card>
  );
}
