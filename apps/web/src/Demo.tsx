import Ask from "./Ask";

// The public demo (P5B): ask-only, with the key Caddy serves at /config.json from the server's .env.
// The key is never in this bundle; it can only read and ask (the API answers 403 to anything else).
export default function Demo({ demoKey }: { demoKey: string }) {
  return (
    <main className="mx-auto max-w-4xl space-y-6 p-8">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">PageCite</h1>
        <p className="text-sm text-slate-600">
          Ask questions about 60 recent Reserve Bank of India circulars (September to November 2026). Every answer
          cites the circular and page it comes from, and says &ldquo;Not found in the documents&rdquo; when they
          don&rsquo;t cover the question.
        </p>
        <p className="text-xs text-slate-500">
          Public demo: up to 10 questions a minute and 20 a day per visitor, a shared daily budget, and questions of
          at most 500 characters. Uploads are disabled here.
        </p>
      </header>
      <Ask tenantKey={demoKey} />
    </main>
  );
}
