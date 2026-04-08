import type { ReactNode } from "react";

type LayoutProps = {
  children: ReactNode;
};

export function Layout({ children }: LayoutProps) {
  return (
    <main className="min-h-screen bg-[radial-gradient(circle_at_top,rgba(37,99,235,0.08),transparent_34%),linear-gradient(180deg,#f6f8fc_0%,#f3f6fb_40%,#eef3f9_100%)] px-4 py-6 md:px-8 md:py-8">
      <div className="mx-auto flex max-w-[1440px] flex-col gap-6">{children}</div>
    </main>
  );
}
