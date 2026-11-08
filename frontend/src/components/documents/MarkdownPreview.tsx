import type { ReactNode } from "react";

/**
 * Minimal, safe Markdown preview: headings, "- " bullets, horizontal rules, an _italic_ line and
 * paragraphs. Everything is rendered as React text (never as HTML), so document content cannot
 * inject markup or scripts.
 */
export function MarkdownPreview({ content, label = "Document preview" }: { content: string; label?: string }) {
  const blocks: ReactNode[] = [];
  let bullets: string[] = [];
  let paragraph: string[] = [];
  const flush = () => {
    if (paragraph.length) {
      blocks.push(<p key={`p${blocks.length}`} className="text-sm leading-6 text-slate-700">{paragraph.join(" ")}</p>);
      paragraph = [];
    }
    if (bullets.length) {
      blocks.push(
        <ul key={`u${blocks.length}`} className="list-disc space-y-1 pl-5 text-sm leading-6 text-slate-700">
          {bullets.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>,
      );
      bullets = [];
    }
  };
  for (const raw of content.split("\n")) {
    const line = raw.trimEnd();
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      const level = heading[1]?.length ?? 1;
      const text = heading[2] ?? "";
      const key = `h${blocks.length}`;
      blocks.push(
        level === 1 ? (
          <h4 key={key} className="text-lg font-semibold text-slate-900">{text}</h4>
        ) : level === 2 ? (
          <h5 key={key} className="pt-2 text-base font-semibold text-slate-900">{text}</h5>
        ) : (
          <h6 key={key} className="text-sm font-semibold text-slate-900">{text}</h6>
        ),
      );
    } else if (/^\s*[-*]\s+/.test(line)) {
      if (paragraph.length) flush();
      bullets.push(line.replace(/^\s*[-*]\s+/, ""));
    } else if (/^-{3,}$/.test(line.trim())) {
      flush();
      blocks.push(<hr key={`r${blocks.length}`} className="border-slate-200" />);
    } else if (!line.trim()) {
      flush();
    } else if (/^_.*_$/.test(line.trim())) {
      flush();
      blocks.push(<p key={`m${blocks.length}`} className="text-xs italic text-slate-500">{line.trim().slice(1, -1)}</p>);
    } else {
      if (bullets.length) flush();
      paragraph.push(line.trim());
    }
  }
  flush();
  return (
    <article aria-label={label} className="max-h-[32rem] space-y-3 overflow-y-auto rounded-lg border border-slate-200 bg-white p-4">
      {blocks}
    </article>
  );
}
