import type { ReactNode } from "react";

type LayoutProps = {
  children: ReactNode;
  fullBleed?: boolean;
};

export function Layout({ children, fullBleed = false }: LayoutProps) {
  return (
    <main className="relative min-h-screen overflow-hidden px-4 py-5 md:px-8 md:py-8">
      {!fullBleed ? (
        <>
          <div className="pointer-events-none absolute inset-x-0 top-0 h-[360px] bg-[linear-gradient(180deg,rgba(255,255,255,0.74)_0%,rgba(255,255,255,0)_100%)]" />
          <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-slate-300/70 to-transparent" />
        </>
      ) : null}
      <div className={["relative mx-auto flex flex-col gap-8", fullBleed ? "max-w-none" : "max-w-[1480px]"].join(" ")}>
        {children}
      </div>
    </main>
  );
}
