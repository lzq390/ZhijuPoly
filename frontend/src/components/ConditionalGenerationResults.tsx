import { useEffect, useRef, useState } from "react";
import {
  Atom,
  Check,
  Copy,
  LoaderCircle,
  SearchX,
  TriangleAlert
} from "lucide-react";
import type {
  ConditionalGenerationCandidate,
  ConditionalGenerationJobStatusResponse,
  ConditionalGenerationTgResponse
} from "../types";
import { StructureSvg } from "./StructureSvg";

type ConditionalGenerationResultsProps = {
  data: ConditionalGenerationTgResponse | null;
  error: string | null;
  isLoading: boolean;
  job: ConditionalGenerationJobStatusResponse | null;
};

function formatMetric(value: number | null | undefined, digits: number) {
  return value == null ? "—" : value.toFixed(digits);
}

function ResultState({
  icon,
  title,
  description,
  tone = "default"
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  tone?: "default" | "danger";
}) {
  return (
    <div className={`tg-result-state${tone === "danger" ? " is-danger" : ""}`}>
      <span className="tg-result-state-icon">{icon}</span>
      <strong>{title}</strong>
      <p>{description}</p>
    </div>
  );
}

async function writeClipboard(value: string) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }

  const textArea = document.createElement("textarea");
  textArea.value = value;
  textArea.style.position = "fixed";
  textArea.style.opacity = "0";
  document.body.appendChild(textArea);
  textArea.select();
  const copied = typeof document.execCommand === "function" && document.execCommand("copy");
  textArea.remove();
  if (!copied) {
    throw new Error("Clipboard unavailable");
  }
}

function CandidateCard({
  candidate,
  selected,
  copied,
  onSelect,
  onCopy
}: {
  candidate: ConditionalGenerationCandidate;
  selected: boolean;
  copied: boolean;
  onSelect: () => void;
  onCopy: () => void;
}) {
  return (
    <article
      className={`tg-candidate-card cg-candidate-card${selected ? " is-selected" : ""}`}
      data-testid={`conditional-candidate-${candidate.rank}`}
    >
      <button
        type="button"
        className="cg-candidate-select"
        aria-pressed={selected}
        aria-label={`选择候选 #${candidate.rank}：${candidate.generated_smiles}`}
        onClick={onSelect}
      >
        <header className="tg-candidate-card-header">
          <span className="tg-candidate-rank">{`#${candidate.rank}`}</span>
          <code className="cg-candidate-smiles">{candidate.generated_smiles}</code>
          <span className="cg-candidate-source">RDKit 2D</span>
        </header>

        <div className="tg-candidate-structure cg-candidate-structure">
          {candidate.structure_svg ? (
            <StructureSvg
              svg={candidate.structure_svg}
              alt={`条件生成候选 ${candidate.rank} 的二维结构`}
              className="cg-candidate-artwork"
              imageClassName="cg-candidate-artwork-image"
            />
          ) : (
            <p>{candidate.generated_smiles}</p>
          )}
        </div>

        <div className="tg-candidate-metrics cg-candidate-metrics">
          <div>
            <span>预测 Tg</span>
            <strong>{`${formatMetric(candidate.predicted_tg, 1)}${candidate.predicted_tg == null ? "" : ` ${candidate.tg_unit}`}`}</strong>
          </div>
          <div>
            <span>种子相似度</span>
            <strong>{formatMetric(candidate.similarity_score, 3)}</strong>
          </div>
          <div>
            <span>SA Score</span>
            <strong>{formatMetric(candidate.sa_score, 3)}</strong>
          </div>
        </div>
      </button>

      <footer className="cg-candidate-footer">
        <button
          type="button"
          onClick={onCopy}
        >
          {copied ? <Check /> : <Copy />}
          {copied ? "已复制" : "复制 SMILES"}
        </button>
      </footer>
    </article>
  );
}

export function ConditionalGenerationResults({
  data,
  error,
  isLoading,
  job
}: ConditionalGenerationResultsProps) {
  const [selectedRank, setSelectedRank] = useState<number | null>(null);
  const [copiedRank, setCopiedRank] = useState<number | null>(null);
  const [copyFeedback, setCopyFeedback] = useState("");
  const copyTimerRef = useRef<number | null>(null);

  useEffect(() => {
    setSelectedRank(data?.results[0]?.rank ?? null);
    setCopiedRank(null);
    setCopyFeedback("");
  }, [data]);

  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) {
        window.clearTimeout(copyTimerRef.current);
      }
    };
  }, []);

  async function copyCandidate(candidate: ConditionalGenerationCandidate) {
    try {
      await writeClipboard(candidate.generated_smiles);
      setCopiedRank(candidate.rank);
      setCopyFeedback(`已复制候选 #${candidate.rank} 的 SMILES。`);
      if (copyTimerRef.current !== null) {
        window.clearTimeout(copyTimerRef.current);
      }
      copyTimerRef.current = window.setTimeout(() => {
        setCopiedRank(null);
        copyTimerRef.current = null;
      }, 1800);
    } catch {
      setCopyFeedback("复制失败，请手动选择 SMILES。");
    }
  }

  if (error) {
    return (
      <ResultState
        tone="danger"
        icon={<TriangleAlert />}
        title="条件生成失败"
        description={error}
      />
    );
  }

  if (isLoading) {
    const statusLabel = job?.status === "pending" ? "任务正在排队" : "正在生成候选结构";
    return (
      <div className="cg-result-loading">
        <div className="tg-result-loading-title">
          <LoaderCircle className="animate-spin" />
          <div>
            <strong>{statusLabel}</strong>
            <span>{job?.message || "模型正在采样并筛选有效聚合物结构…"}</span>
          </div>
        </div>
        <div className="cg-indeterminate-progress" aria-label="生成进行中" />
        <div className="tg-result-skeleton-list" aria-hidden="true">
          {Array.from({ length: 2 }).map((_, index) => <span key={index} />)}
        </div>
      </div>
    );
  }

  if (job?.status === "cancelled") {
    return (
      <ResultState
        icon={<SearchX />}
        title="生成任务已取消"
        description="任务未返回候选结构，可以从参数面板重新运行。"
      />
    );
  }

  if (!data) {
    return (
      <ResultState
        icon={<Atom />}
        title="候选生成已就绪"
        description="设置相对 Tg 变化并运行，即可在这里查看生成结构。"
      />
    );
  }

  if (data.results.length === 0) {
    return (
      <ResultState
        icon={<SearchX />}
        title="本次没有有效候选"
        description="可以调整 ΔTg 或高级采样参数后重新生成。"
      />
    );
  }

  return (
    <div className="cg-result-success">
      <p className="cg-ranking-note">
        排序：与种子结构的 Morgan–Tanimoto 相似度降序 → SA Score 升序 → 规范化 SMILES 字典序；预测 Tg 不参与排序。
      </p>
      <div className="tg-candidate-list">
        {data.results.map((candidate) => (
          <CandidateCard
            key={`${candidate.rank}-${candidate.generated_smiles}`}
            candidate={candidate}
            selected={selectedRank === candidate.rank}
            copied={copiedRank === candidate.rank}
            onSelect={() => setSelectedRank(candidate.rank)}
            onCopy={() => void copyCandidate(candidate)}
          />
        ))}
      </div>
      <span className="tg-visually-hidden" role="status" aria-live="polite">
        {copyFeedback}
      </span>
    </div>
  );
}
