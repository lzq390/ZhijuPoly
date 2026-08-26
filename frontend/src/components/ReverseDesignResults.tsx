import { useEffect, useId, useState } from "react";
import {
  Atom,
  BookOpen,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Database,
  LoaderCircle,
  SearchX,
  TriangleAlert
} from "lucide-react";
import type {
  KnowledgeNavigationRequest,
  ReverseDesignTgCandidate,
  ReverseDesignTgJobStatusResponse,
  ReverseDesignTgRequest,
  ReverseDesignTgResponse
} from "../types";
import { StructureSvg } from "./StructureSvg";

type ReverseDesignResultsProps = {
  data: ReverseDesignTgResponse | null;
  error: string | null;
  isLoading?: boolean;
  job?: ReverseDesignTgJobStatusResponse | null;
  submittedRequest: ReverseDesignTgRequest | null;
  onOpenKnowledge: (request: KnowledgeNavigationRequest) => void;
  page?: number;
  onPageChange?: (page: number) => void;
};

const RESULTS_PAGE_SIZE = 5;

function formatInteger(value: number | null | undefined) {
  return value == null ? "0" : value.toLocaleString();
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

function ResultPagination({
  page,
  totalPages,
  start,
  end,
  total,
  onChange
}: {
  page: number;
  totalPages: number;
  start: number;
  end: number;
  total: number;
  onChange: (page: number) => void;
}) {
  if (total <= RESULTS_PAGE_SIZE) {
    return null;
  }
  return (
    <div className="tg-result-pagination" aria-label="候选结果分页">
      <button
        type="button"
        onClick={() => onChange(page - 1)}
        disabled={page <= 1}
        aria-label="上一页"
      >
        <ChevronLeft />
      </button>
      <span>{`${start + 1}–${end} / ${total}`}</span>
      <strong>{`${page} / ${totalPages}`}</strong>
      <button
        type="button"
        onClick={() => onChange(page + 1)}
        disabled={page >= totalPages}
        aria-label="下一页"
      >
        <ChevronRight />
      </button>
    </div>
  );
}

function CandidateCard({
  candidate,
  onOpenKnowledge
}: {
  candidate: ReverseDesignTgCandidate;
  onOpenKnowledge: (request: KnowledgeNavigationRequest) => void;
}) {
  const menuId = useId();
  const [showIupac, setShowIupac] = useState(false);
  const [showKnowledge, setShowKnowledge] = useState(false);
  const displaySmiles = candidate.canonical_polym || candidate.polymer_smiles;
  const monomerATerm =
    candidate.monomer_a_iupac?.trim() || candidate.monomer_a_smiles.trim();
  const monomerBTerm =
    candidate.monomer_b_iupac?.trim() || candidate.monomer_b_smiles.trim();

  function openKnowledge(terms: string[]) {
    const normalizedTerms = terms.filter(Boolean);
    if (normalizedTerms.length === 0) {
      return;
    }
    setShowKnowledge(false);
    onOpenKnowledge({
      query: normalizedTerms.join("；"),
      groups: normalizedTerms.map((term) => ({ terms: [term] }))
    });
  }

  return (
    <article className="tg-candidate-card">
      <header className="tg-candidate-card-header">
        <span className="tg-candidate-rank">{`#${candidate.rank}`}</span>
        <span className="tg-candidate-id">{`PI ${candidate.pi_id}`}</span>
        <span className="tg-candidate-score">
          {`${Math.round(candidate.similarity_score * 100)}%`}
        </span>
      </header>

      <div className="tg-candidate-structure">
        {candidate.structure_svg ? (
          <StructureSvg
            svg={candidate.structure_svg}
            alt={`PI candidate ${candidate.pi_id}`}
            imageClassName="max-h-[150px]"
          />
        ) : (
          <p>{displaySmiles}</p>
        )}
      </div>

      <div className="tg-candidate-metrics">
        <div>
          <span>Tg</span>
          <strong>{`${candidate.tg_value.toFixed(1)} ${candidate.tg_unit}`}</strong>
        </div>
        <div>
          <span>差值</span>
          <strong className="is-accent">{`${candidate.tg_difference >= 0 ? "+" : ""}${candidate.tg_difference.toFixed(1)} °C`}</strong>
        </div>
        <div>
          <span>相似度</span>
          <strong>{candidate.similarity_score.toFixed(3)}</strong>
        </div>
      </div>

      <details className="tg-candidate-details">
        <summary>
          <span>单体与结构信息</span>
          <ChevronDown />
        </summary>
        <div className="tg-candidate-detail-body">
          <div className="tg-monomer-block">
            <span>单体 A</span>
            {candidate.monomer_a_structure_svg ? (
              <StructureSvg
                svg={candidate.monomer_a_structure_svg}
                alt={`Monomer A for PI ${candidate.pi_id}`}
                imageClassName="max-h-[96px]"
              />
            ) : null}
            <code>{candidate.monomer_a_smiles || "暂无数据"}</code>
            {showIupac ? (
              <p>{candidate.monomer_a_iupac || "暂无 IUPAC 名称"}</p>
            ) : null}
          </div>
          <div className="tg-monomer-block">
            <span>单体 B</span>
            {candidate.monomer_b_structure_svg ? (
              <StructureSvg
                svg={candidate.monomer_b_structure_svg}
                alt={`Monomer B for PI ${candidate.pi_id}`}
                imageClassName="max-h-[96px]"
              />
            ) : null}
            <code>{candidate.monomer_b_smiles || "暂无数据"}</code>
            {showIupac ? (
              <p>{candidate.monomer_b_iupac || "暂无 IUPAC 名称"}</p>
            ) : null}
          </div>
          <code className="tg-polymer-smiles">{displaySmiles}</code>
        </div>
      </details>

      <footer className="tg-candidate-actions">
        <button type="button" onClick={() => setShowIupac((current) => !current)}>
          <Atom />
          {showIupac ? "隐藏 IUPAC" : "IUPAC"}
        </button>
        <div className="tg-knowledge-menu">
          <button
            type="button"
            onClick={() => setShowKnowledge((current) => !current)}
            aria-expanded={showKnowledge}
            aria-controls={menuId}
            disabled={!monomerATerm && !monomerBTerm}
          >
            <BookOpen />
            知识检索
            <ChevronDown />
          </button>
          {showKnowledge ? (
            <div id={menuId} role="menu" className="tg-knowledge-menu-panel">
              <button
                type="button"
                role="menuitem"
                disabled={!monomerATerm}
                onClick={() => openKnowledge([monomerATerm])}
              >
                单体 A
              </button>
              <button
                type="button"
                role="menuitem"
                disabled={!monomerBTerm}
                onClick={() => openKnowledge([monomerBTerm])}
              >
                单体 B
              </button>
              <button
                type="button"
                role="menuitem"
                disabled={!monomerATerm && !monomerBTerm}
                onClick={() => openKnowledge([monomerATerm, monomerBTerm])}
              >
                A + B
              </button>
            </div>
          ) : null}
        </div>
      </footer>
    </article>
  );
}

export function ReverseDesignResults({
  data,
  error,
  isLoading = false,
  job,
  submittedRequest,
  onOpenKnowledge,
  page,
  onPageChange
}: ReverseDesignResultsProps) {
  const [internalPage, setInternalPage] = useState(1);
  const activePage = page ?? internalPage;
  const changePage = onPageChange ?? setInternalPage;
  const total = data?.results.length ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / RESULTS_PAGE_SIZE));
  const currentPage = Math.min(activePage, totalPages);
  const start = (currentPage - 1) * RESULTS_PAGE_SIZE;
  const end = Math.min(start + RESULTS_PAGE_SIZE, total);
  const results = data?.results.slice(start, end) ?? [];

  useEffect(() => {
    if (page === undefined) setInternalPage(1);
  }, [data, page]);

  if (error) {
    return (
      <ResultState
        tone="danger"
        icon={<TriangleAlert />}
        title="Tg 搜索失败"
        description={error}
      />
    );
  }

  if (isLoading) {
    return (
      <div className="tg-result-loading">
        <div className="tg-result-loading-title">
          <LoaderCircle className="animate-spin" />
          <div>
            <strong>正在搜索 PI 候选</strong>
            <span>{job?.message || "按 Tg 距离和结构相似度扫描候选库…"}</span>
          </div>
        </div>
        <div className="tg-result-loading-grid">
          <div><span>已扫描</span><strong>{formatInteger(job?.scanned_rows)}</strong></div>
          <div><span>已命中</span><strong>{formatInteger(job?.matched_count)}</strong></div>
          <div>
            <span>Tg 半径</span>
            <strong>{job?.current_tg_radius == null ? "—" : `±${job.current_tg_radius.toFixed(1)}`}</strong>
          </div>
          <div>
            <span>最佳相似度</span>
            <strong>{job?.best_similarity_score == null ? "—" : job.best_similarity_score.toFixed(3)}</strong>
          </div>
        </div>
        <div className="tg-result-skeleton-list" aria-hidden="true">
          {Array.from({ length: 3 }).map((_, index) => (
            <span key={index} />
          ))}
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <ResultState
        icon={<Database />}
        title="候选搜索已就绪"
        description="设置搜索参数后运行，即可在这里查看真实 PI 候选。"
      />
    );
  }

  if (data.total === 0 || data.results.length === 0) {
    return (
      <ResultState
        icon={<SearchX />}
        title="没有找到候选"
        description="可以降低相似度阈值、扩大候选数量，或检查当前聚合物结构。"
      />
    );
  }

  return (
    <div className="tg-result-success">
      <section className="tg-result-summary" aria-label="本次搜索摘要">
        <div><span>目标 Tg</span><strong>{`${submittedRequest?.target_tg ?? data.target_tg} °C`}</strong></div>
        <div><span>阈值</span><strong>{submittedRequest?.similarity_threshold.toFixed(2) ?? "—"}</strong></div>
        <div><span>候选</span><strong>{data.total}</strong></div>
        <div><span>已扫描</span><strong>{formatInteger(job?.scanned_rows)}</strong></div>
      </section>
      <div className="tg-result-meta">
        <span>{`候选池 ${data.candidate_pool_size.toLocaleString()}`}</span>
        <span>{`${data.query_time_ms.toFixed(1)} ms`}</span>
        <span>{`每页 ${RESULTS_PAGE_SIZE} 条`}</span>
      </div>
      <ResultPagination
        page={currentPage}
        totalPages={totalPages}
        start={start}
        end={end}
        total={total}
        onChange={(nextPage) => changePage(Math.min(Math.max(1, nextPage), totalPages))}
      />
      <div className="tg-candidate-list">
        {results.map((candidate) => (
          <CandidateCard
            key={candidate.pi_id}
            candidate={candidate}
            onOpenKnowledge={onOpenKnowledge}
          />
        ))}
      </div>
      <ResultPagination
        page={currentPage}
        totalPages={totalPages}
        start={start}
        end={end}
        total={total}
        onChange={(nextPage) => changePage(Math.min(Math.max(1, nextPage), totalPages))}
      />
    </div>
  );
}
