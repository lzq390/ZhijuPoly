import { Atom, PanelRightOpen } from "lucide-react";
import type {
  ConditionalGenerationJobStatusResponse,
  ConditionalGenerationTgResponse
} from "../../types";
import { ConditionalGenerationResults } from "../ConditionalGenerationResults";
import { WorkbenchDrawerShell } from "../structure-workbench/WorkbenchDrawerShell";

type ConditionalGenerationDrawerProps = {
  open: boolean;
  hasRun: boolean;
  width: number;
  loading: boolean;
  error: string | null;
  data: ConditionalGenerationTgResponse | null;
  job: ConditionalGenerationJobStatusResponse | null;
  onWidthChange: (width: number) => void;
  onClose: () => void;
  onOpen: () => void;
};

function resultStatus(
  loading: boolean,
  error: string | null,
  data: ConditionalGenerationTgResponse | null,
  job: ConditionalGenerationJobStatusResponse | null
) {
  if (loading) return job?.status === "pending" ? "任务排队中" : "候选生成中";
  if (error) return "生成需要检查";
  if (data) return `${data.returned_count} 个候选`;
  if (job?.status === "cancelled") return "任务已取消";
  return "等待生成";
}

export function ConditionalGenerationDrawer({
  open,
  hasRun,
  width,
  loading,
  error,
  data,
  job,
  onWidthChange,
  onClose,
  onOpen
}: ConditionalGenerationDrawerProps) {
  return (
    <WorkbenchDrawerShell
      open={open}
      hasRun={hasRun}
      width={width}
      title="条件生成候选"
      status={resultStatus(loading, error, data, job)}
      headerIcon={<Atom aria-hidden="true" />}
      reopenIcon={<PanelRightOpen aria-hidden="true" />}
      reopenLabel="展开条件生成候选"
      reopenVariant="side-handle"
      closeLabel="关闭候选结果"
      resizeLabel="调整候选结果抽屉宽度"
      drawerClassName="np-cg-results-drawer cg-results-drawer"
      onWidthChange={onWidthChange}
      onClose={onClose}
      onOpen={onOpen}
    >
      <ConditionalGenerationResults
        data={data}
        error={error}
        isLoading={loading}
        job={job}
      />
    </WorkbenchDrawerShell>
  );
}
