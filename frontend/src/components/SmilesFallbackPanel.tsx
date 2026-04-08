import { Sigma } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "./ui/card";
import { Textarea } from "./ui/textarea";

type SmilesFallbackPanelProps = {
  smiles: string;
  onChange: (value: string) => void;
};

export function SmilesFallbackPanel({ smiles, onChange }: SmilesFallbackPanelProps) {
  return (
    <Card className="overflow-hidden rounded-[28px] border-slate-200/90">
      <CardHeader className="min-h-[124px] gap-4 border-b border-slate-200/80 bg-[linear-gradient(180deg,#ffffff_0%,#f8fafc_100%)]">
        <div className="space-y-2">
          <CardTitle className="flex items-center gap-2 text-xl">
            <Sigma className="h-5 w-5 text-blue-600" />
            SMILES Fallback
          </CardTitle>
          <CardDescription>可直接粘贴或修改 SMILES，作为查询输入的文本回退入口。</CardDescription>
        </div>
      </CardHeader>

      <CardContent className="pt-6">
        <Textarea
          value={smiles}
          onChange={(event) => onChange(event.target.value)}
          placeholder="例如: *CC*、CCO 或其他用于匹配查询的 SMILES"
          className="min-h-[188px] rounded-[18px] border-slate-200 bg-white px-4 py-3"
        />
      </CardContent>
    </Card>
  );
}
