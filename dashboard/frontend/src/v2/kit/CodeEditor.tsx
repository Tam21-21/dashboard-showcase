import { useEffect, useRef, useState } from "react";
import Editor, { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";

// Self-hosted Monaco: the wrapper's default loader fetches the editor from a CDN, and the dashboard
// must work with the CDN blocked. Python needs only the base editor worker (its colours come from
// the Monarch tokenizer, no language server). This file loads lazily (kit/index.ts), so all of
// Monaco stays in its own build chunk and never rides into the page bundle.
(self as unknown as { MonacoEnvironment: monaco.Environment }).MonacoEnvironment = {
  getWorker: () => new EditorWorker(),
};
loader.config({ monaco });

const THEME = "v2-code";
const MIN_HEIGHT = 360;
const MAX_HEIGHT = 640;

const OPTIONS: monaco.editor.IStandaloneEditorConstructionOptions = {
  minimap: { enabled: false },
  fontSize: 13,
  fontFamily: "'Azeret Mono Variable', ui-monospace, monospace",
  scrollBeyondLastLine: false,
  tabSize: 4,
  insertSpaces: true,
  automaticLayout: true,
  renderLineHighlight: "line",
  scrollbar: { alwaysConsumeMouseWheel: false },
};

export type CodeEditorProps = {
  label: string;
  value: string;
  onChange?: (value: string) => void;
  readOnly?: boolean;
  openAt?: { from: number; to: number } | null;
  errorLine?: number | null;
  errorText?: string;
  /** Called last in the editor's own mount, with the Monaco instance (for inserting text). */
  onMount?: (editor: monaco.editor.IStandaloneCodeEditor) => void;
  /** Lets text dragged from outside drop into the code, with no drop-choice menu. */
  dropIntoEditor?: boolean;
};

// The code card of the strategy editor: Python colours, opened at the marker lines when given, one
// red mark on the line the server named.
export default function CodeEditor({
  label, value, onChange, readOnly = false, openAt = null, errorLine = null, errorText = "", onMount, dropIntoEditor = false,
}: CodeEditorProps) {
  const [height, setHeight] = useState(MIN_HEIGHT);
  const ed = useRef<monaco.editor.IStandaloneCodeEditor | null>(null);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const editor = ed.current;
    const model = editor?.getModel();
    if (!mounted || !editor || !model) return;
    if (errorLine == null || errorLine < 1 || errorLine > model.getLineCount()) {
      monaco.editor.setModelMarkers(model, "validate", []);
      return;
    }
    monaco.editor.setModelMarkers(model, "validate", [{
      severity: monaco.MarkerSeverity.Error,
      message: errorText,
      startLineNumber: errorLine,
      startColumn: 1,
      endLineNumber: errorLine,
      endColumn: model.getLineMaxColumn(errorLine),
    }]);
    editor.revealLineInCenter(errorLine);
  }, [mounted, errorLine, errorText]);

  return (
    <div className="se-code" style={{ height }} data-label={label}>
      <Editor
        height="100%"
        language="python"
        theme={THEME}
        value={value}
        onChange={(v) => onChange?.(v ?? "")}
        beforeMount={(m) => {
          m.editor.defineTheme(THEME, {
            base: "vs-dark",
            inherit: true,
            rules: [{ token: "comment", foreground: "8d98a8" }],
            colors: { "editor.background": "#06080a", "editor.foreground": "#eef2f7" },
          });
        }}
        onMount={(editor, m) => {
          ed.current = editor;
          const fit = () => {
            const h = Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, Math.ceil(editor.getContentHeight())));
            setHeight((prev) => (prev === h ? prev : h));
          };
          editor.onDidContentSizeChange(fit);
          fit();
          if (openAt) {
            editor.revealLinesInCenter(openAt.from, openAt.to);
            const model = editor.getModel();
            const line = model ? Math.min(openAt.from + 1, model.getLineCount()) : openAt.from + 1;
            const col = model ? model.getLineMaxColumn(line) : 1;
            editor.setPosition({ lineNumber: line, column: col });
          }
          void document.fonts.ready.then(() => m.editor.remeasureFonts());
          setMounted(true);
          onMount?.(editor);
        }}
        options={dropIntoEditor
          ? { ...OPTIONS, readOnly, dropIntoEditor: { enabled: true, showDropSelector: "never" } }
          : { ...OPTIONS, readOnly }}
        loading={<div className="se-loading">Loading the editor.</div>}
      />
    </div>
  );
}
