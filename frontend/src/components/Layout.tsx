import type { ReactNode } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";

type LayoutProps = {
  children: ReactNode;
};

export function Layout({ children }: LayoutProps) {
  return (
    <main className="min-h-screen bg-[radial-gradient(circle_at_top_left,rgba(15,118,110,0.16),transparent_32%),linear-gradient(180deg,#f4efe8_0%,#f8f5ef_100%)] px-4 py-8 md:px-8">
      <div className="mx-auto flex max-w-7xl flex-col gap-6">
        <Card className="border-white/60 bg-card/90 backdrop-blur">
          <CardHeader>
            <CardTitle className="text-4xl md:text-5xl">PolyProp</CardTitle>
            <CardDescription>
              输入结构式后，按精确匹配或相似度匹配检索聚合物属性。
            </CardDescription>
          </CardHeader>
          <CardContent>{children}</CardContent>
        </Card>
      </div>
    </main>
  );
}
