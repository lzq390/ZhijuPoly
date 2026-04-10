import type { ReactNode } from "react";

type LayoutProps = {
  children: ReactNode;
};

export function Layout({ children }: LayoutProps) {
  return (
    <main className="relative min-h-screen overflow-hidden px-4 py-5 md:px-8 md:py-8">
      <div className="pointer-events-none absolute inset-x-0 top-0 h-[420px] bg-[radial-gradient(circle_at_top,rgba(20,184,166,0.18),transparent_48%)]" />
      <div className="pointer-events-none absolute -left-24 top-32 h-72 w-72 rounded-full bg-teal-300/20 blur-3xl" />
      <div className="pointer-events-none absolute right-0 top-20 h-80 w-80 rounded-full bg-sky-300/20 blur-3xl" />
      <div className="relative mx-auto flex max-w-[1480px] flex-col gap-8">{children}</div>
    </main>
  );
}
