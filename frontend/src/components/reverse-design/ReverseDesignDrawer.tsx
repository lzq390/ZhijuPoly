import { MessageSquareText, Search } from "lucide-react";
import type {
  KnowledgeNavigationRequest,
  ReverseDesignTgJobStatusResponse,
  ReverseDesignTgRequest,
  ReverseDesignTgResponse
} from "../../types";
import { ReverseDesignResults } from "../ReverseDesignResults";
import { WorkbenchDrawerShell } from "../structure-workbench/WorkbenchDrawerShell";

type ReverseDesignDrawerProps = {
  open: boolean;
  hasRun: boolean;
  width: number;
  status: string;
  data: ReverseDesignTgResponse | null;
  error: string | null;
  loading: boolean;
  job: ReverseDesignTgJobStatusResponse | null;
  submittedRequest: ReverseDesignTgRequest | null;
  page: number;
  onPageChange: (page: number) => void;
  onOpenKnowledge: (request: KnowledgeNavigationRequest) => void;
  onWidthChange: (width: number) => void;
  onClose: () => void;
  onOpen: () => void;
};

export function ReverseDesignDrawer({
  open,
  hasRun,
  width,
  status,
  data,
  error,
  loading,
  job,
  submittedRequest,
  page,
  onPageChange,
  onOpenKnowledge,
  onWidthChange,
  onClose,
  onOpen
}: ReverseDesignDrawerProps) {
  return (
    <WorkbenchDrawerShell
      open={open}
      hasRun={hasRun}
      width={width}
      title="Tg 候选结果"
      status={status}
      headerIcon={<MessageSquareText aria-hidden="true" />}
      reopenIcon={<Search aria-hidden="true" />}
      reopenLabel="展开 Tg 候选结果"
      reopenVariant="side-handle"
      closeLabel="关闭候选结果"
      resizeLabel="调整候选结果抽屉宽度"
      drawerClassName="np-tg-results-drawer"
      onWidthChange={onWidthChange}
      onClose={onClose}
      onOpen={onOpen}
    >
      <ReverseDesignResults
        data={data}
        error={error}
        isLoading={loading}
        job={job}
        submittedRequest={submittedRequest}
        onOpenKnowledge={onOpenKnowledge}
        page={page}
        onPageChange={onPageChange}
      />
    </WorkbenchDrawerShell>
  );
}
