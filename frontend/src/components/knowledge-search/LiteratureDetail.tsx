import { useEffect, useRef, useState, type ReactNode } from "react";
import { BookOpenText, Download, ExternalLink, FileText, LoaderCircle, RefreshCw } from "lucide-react";
import { fetchLiteratureDetail, fetchLiteratureFullText, isApiRequestError, literatureAssetUrl } from "../../services/api";
import type { KnowledgeObservationRequest, LiteratureDetailResponse, LiteratureDocumentResult, LiteratureEvidence, LiteratureFullTextBlock, LiteratureSample } from "../../types";
import { KnowledgeDetailDrawer, useKnowledgeDrawerSizing } from "./KnowledgeDetailDrawer";

type Props = {
  record: LiteratureDocumentResult;
  open: boolean;
  sizing: ReturnType<typeof useKnowledgeDrawerSizing>;
  onClose: () => void;
  onOpen: () => void;
  onObserve: (source: KnowledgeObservationRequest["source"]) => void;
};

const FIELD_LABELS: Record<string, string> = {
  temperature: "温度", time: "时间", duration: "时长", atmosphere: "气氛", pressure: "压力",
  solvent: "溶剂", catalyst: "催化剂", heating_rate: "升温速率", heatingRate: "升温速率",
  method: "方法", unit: "单位", value: "值", name: "名称", instrument: "仪器", testMethod: "测试方法",
  single_value: "数值", min: "最小值", max: "最大值", uncertainty: "不确定度", amount: "用量",
  mass: "质量", volume: "体积", molarAmount: "物质的量", molar_amount: "物质的量",
};

function missing(value: unknown) {
  return value === null || value === undefined || value === "" || (Array.isArray(value) && value.length === 0);
}

/** Unknown extraction fields remain data: never interpret strings as HTML. */
export function LiteratureValue({ value, depth = 0 }: { value: unknown; depth?: number }): ReactNode {
  if (missing(value)) return <span className="ks-missing-value">本次提取未提供</span>;
  if (typeof value !== "object") return <span>{String(value)}</span>;
  if (depth >= 4) return <pre className="ks-raw-value">{JSON.stringify(value, null, 2)}</pre>;
  if (Array.isArray(value)) return <ul className="ks-value-list">{value.map((item, index) => <li key={index}><LiteratureValue value={item} depth={depth + 1} /></li>)}</ul>;
  const fields = Object.entries(value as Record<string, unknown>);
  if (!fields.length) return <span className="ks-missing-value">本次提取未提供</span>;
  return <dl className="ks-literature-values">{fields.map(([key, item]) => <div key={key}><dt>{FIELD_LABELS[key] || key}</dt><dd><LiteratureValue value={item} depth={depth + 1} /></dd></div>)}</dl>;
}

function Evidence({ evidence, onNavigate }: { evidence: LiteratureEvidence[]; onNavigate?: (blockId: string) => void }) {
  if (!evidence?.length) return null;
  return <details className="ks-evidence"><summary>查看原始依据（{evidence.length}）</summary>{evidence.map((item, index) => <div key={index}>
    {item.quote ? <blockquote>{item.quote}</blockquote> : null}
    {item.block_id !== null && item.block_id !== undefined ? <small>正文块 {String(item.block_id)} {onNavigate ? <button className="ks-evidence-link" type="button" onClick={() => onNavigate(String(item.block_id))}>查看正文</button> : null}</small> : null}
    {!missing(item.md_range) ? <div><small>原文位置</small><LiteratureValue value={item.md_range} /></div> : null}
  </div>)}</details>;
}

function SampleDetails({ sample, onNavigate }: { sample: LiteratureSample; onNavigate?: (blockId: string) => void }) {
  return <div className="ks-drawer-stack">
    <section className="ks-drawer-section"><h3>样品信息</h3><dl className="ks-detail-grid">
      <div><dt>样品名称</dt><dd>{sample.name || "未命名样品"}</dd></div>
      <div><dt>样品编号</dt><dd>{sample.identity_code || "本次提取未提供"}</dd></div>
      <div><dt>类别</dt><dd>{sample.category || "本次提取未提供"}</dd></div>
      <div><dt>形态</dt><dd>{sample.form || "本次提取未提供"}</dd></div>
    </dl><Evidence evidence={sample.evidence} onNavigate={onNavigate} /></section>
    <section className="ks-drawer-section"><h3>性质与测试条件</h3>
      {sample.properties.length ? sample.properties.map((property) => <article className="ks-extracted-entry" key={property.entry_uid}>
        <h4>{property.name || property.abbreviation || "未命名性质"}</h4>
        <LiteratureValue value={property.value} />
        <h5>测试条件</h5><LiteratureValue value={property.conditions} />
        <Evidence evidence={property.evidence} onNavigate={onNavigate} />
      </article>) : <p className="ks-missing-value">本次提取未提供性质信息。</p>}
    </section>
    <section className="ks-drawer-section"><h3>合成与工艺</h3>
      {sample.processes.length ? sample.processes.map((process, index) => <article className="ks-extracted-entry" key={process.entry_uid}>
        <h4>{process.product_name || `工艺 ${index + 1}`}</h4>
        {!missing(process.mass) ? <><h5>产物质量</h5><LiteratureValue value={process.mass} /></> : null}
        {!missing(process.yield_percent) ? <><h5>收率（%）</h5><LiteratureValue value={process.yield_percent} /></> : null}
        <h5>材料与用量</h5>{process.materials.length ? process.materials.map((material) => <div className="ks-extracted-material" key={material.entry_uid}>
          <strong>{material.name || "未命名材料"}</strong>
          <span className="ks-long-copy">{[material.role, material.type].filter(Boolean).join(" · ")}</span>
          {material.amounts.map((amount, amountIndex) => <div key={amountIndex}><span>{FIELD_LABELS[amount.kind || ""] || amount.kind || "用量"}</span><LiteratureValue value={amount.value} /></div>)}
          {material.smiles ? <p className="ks-long-copy">SMILES：{material.smiles}</p> : null}
          {material.cas_number ? <p>CAS：{material.cas_number}</p> : null}
          {material.formula ? <p>分子式：{material.formula}</p> : null}
          <Evidence evidence={material.evidence} onNavigate={onNavigate} />
        </div>) : <p className="ks-missing-value">未提供材料信息。</p>}
        <h5>反应条件</h5><LiteratureValue value={process.conditions} />
        <h5>后处理</h5><LiteratureValue value={process.post_treatment} />
        <Evidence evidence={process.evidence} onNavigate={onNavigate} />
      </article>) : <p className="ks-missing-value">本次提取未提供工艺信息。</p>}
    </section>
    <section className="ks-drawer-section"><h3>表征信息</h3><LiteratureValue value={sample.characterization} /></section>
  </div>;
}

function FullTextBlock({ block, index }: { block: LiteratureFullTextBlock; index: number }) {
  const text = block.text || "";
  return <article className="ks-fulltext-block">
    <small>正文块 {String(block.block_id ?? block.idx ?? index)}</small>
    {block.type === "heading" ? <h4>{text}</h4> : block.parts?.length ? block.parts.map((part, partIndex) => part.type === "table" ? <div className="ks-safe-table" key={partIndex}><table><tbody>{part.rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table></div> : <p className="ks-long-copy" key={partIndex}>{part.text}</p>) : block.table?.rows ? <div className="ks-safe-table"><table>
      {block.table.headers?.length ? <thead><tr>{block.table.headers.map((cell, cellIndex) => <th key={cellIndex}>{cell}</th>)}</tr></thead> : null}
      <tbody>{block.table.rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody>
    </table></div> : <p className="ks-long-copy">{text || "该内容块没有可展示的文本。"}</p>}
  </article>;
}

export function LiteratureDetail({ record, open, sizing, onClose, onOpen, onObserve }: Props) {
  const [detail, setDetail] = useState<LiteratureDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  const [sampleUid, setSampleUid] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState("overview");
  const [blocks, setBlocks] = useState<LiteratureFullTextBlock[]>([]);
  const [fulltextStarted, setFulltextStarted] = useState(false);
  const [fulltextLoading, setFulltextLoading] = useState(false);
  const [fulltextError, setFulltextError] = useState<string | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [exactInputVerified, setExactInputVerified] = useState(false);
  const [pendingBlockId, setPendingBlockId] = useState<string | null>(null);
  const completedDetailKey = useRef<string | null>(null);
  const fulltextFirstPageCompleted = useRef(false);
  const fulltextAbort = useRef<AbortController | null>(null);
  const blockRefs = useRef(new Map<string, HTMLDivElement>());

  useEffect(() => {
    const key = JSON.stringify([record.document_uid, record.generation_id, record.snapshot_uid, retry]);
    if (!open || completedDetailKey.current === key) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void fetchLiteratureDetail(record.document_uid, record.generation_id, record.snapshot_uid, controller.signal)
      .then((data) => {
        if (controller.signal.aborted) return;
        completedDetailKey.current = key;
        setDetail(data);
        setSampleUid(data.samples[0]?.sample_uid ?? null);
      })
      .catch((failure: unknown) => {
        if (!controller.signal.aborted) setError(isApiRequestError(failure, 410) ? "该文献快照已更新，请重新检索。" : failure instanceof Error ? failure.message : "无法加载文献详情。");
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [open, record.document_uid, record.generation_id, record.snapshot_uid, retry]);

  useEffect(() => () => fulltextAbort.current?.abort(), []);

  useEffect(() => {
    if (open) return;
    fulltextAbort.current?.abort();
    fulltextAbort.current = null;
    setFulltextLoading(false);
    // A completed empty page is cached too. Only an unfinished first page
    // resumes automatically; later pages retain their cursor for explicit load.
    if (!fulltextFirstPageCompleted.current) setFulltextStarted(false);
  }, [open]);

  async function loadFulltext(nextCursor?: string) {
    if (!open) return;
    fulltextAbort.current?.abort();
    const controller = new AbortController();
    fulltextAbort.current = controller;
    setFulltextStarted(true);
    setFulltextLoading(true);
    setFulltextError(null);
    try {
      const data = await fetchLiteratureFullText(record.document_uid, record.generation_id, record.snapshot_uid, nextCursor, controller.signal);
      if (controller.signal.aborted) return;
      if (nextCursor === undefined) fulltextFirstPageCompleted.current = true;
      setBlocks((previous) => nextCursor ? [...previous, ...data.blocks] : data.blocks);
      setCursor(data.next_cursor);
      setExactInputVerified(data.exact_input_verified === true);
    } catch (failure) {
      if (!controller.signal.aborted) setFulltextError(failure instanceof Error ? failure.message : "全文暂不可用。");
    } finally {
      if (!controller.signal.aborted) setFulltextLoading(false);
      if (fulltextAbort.current === controller) fulltextAbort.current = null;
    }
  }

  useEffect(() => {
    if (open && activeTab === "source" && detail?.capabilities.has_fulltext && !fulltextStarted) void loadFulltext();
    // Full text starts only after the user enters this tab; subsequent pages are explicit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, activeTab, detail, fulltextStarted]);

  useEffect(() => {
    if (activeTab !== "source" || !pendingBlockId || !exactInputVerified) return;
    const target = blockRefs.current.get(pendingBlockId);
    const reducedMotion = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target?.scrollIntoView?.({ block: "center", behavior: reducedMotion ? "auto" : "smooth" });
    target?.focus({ preventScroll: true });
  }, [activeTab, pendingBlockId, exactInputVerified, blocks]);

  const canNavigateEvidence = exactInputVerified || detail?.assets.some((asset) => asset.role === "full_text" && asset.exact_input_verified === true);
  function navigateToEvidence(blockId: string) {
    setPendingBlockId(blockId);
    setActiveTab("source");
    onObserve("fulltext_tab");
  }

  const metadata = detail?.document ?? record;
  const sample = detail?.samples.find((item) => item.sample_uid === sampleUid) ?? detail?.samples[0];
  const downloadableAssets = detail?.assets.filter((asset) => asset.role !== "full_text") ?? [];
  const status = loading ? <p className="ks-detail-loading" role="status"><LoaderCircle className="is-spinning" aria-hidden="true" />正在加载文献详情…</p> : error ? <div className="ks-inline-alert is-error" role="alert"><span>{error}</span><button type="button" onClick={() => setRetry((value) => value + 1)}><RefreshCw aria-hidden="true" />重试</button></div> : null;

  return <KnowledgeDetailDrawer
    id="literature-knowledge-detail" open={open} width={sizing.drawerWidth}
    contentKey={`literature-${record.generation_id}-${record.document_uid}-${record.snapshot_uid}`}
    title="文献详情" subtitle={`${record.year ?? "年份未提供"} · 已提取文献`}
    icon={<BookOpenText aria-hidden="true" />} verticalReopen reopenLabel="查看文献详情"
    activeTabId={activeTab}
    widthProfile={sizing.widthProfile} onWidthChange={sizing.setDrawerWidth} onClose={onClose} onOpen={onOpen}
    onTabChange={(tab) => {
      setPendingBlockId(null);
      setActiveTab(tab);
      if (tab === "extracted") onObserve("extracted_tab");
      if (tab === "source") onObserve("fulltext_tab");
    }}
    tabs={[
      { id: "overview", label: "文献概览", content: <div className="ks-drawer-stack">
        <section className="ks-drawer-section"><p className="ks-drawer-title-main">{metadata.title}</p><p className="ks-drawer-title-secondary">{metadata.authors.join("、") || "作者未提供"}</p><p>{[metadata.journal, metadata.year].filter(Boolean).join(" · ")}</p>
          {metadata.doi ? <a className="ks-literature-link" href={`https://doi.org/${metadata.doi.split("/").map(encodeURIComponent).join("/")}`} target="_blank" rel="noopener noreferrer">DOI：{metadata.doi}<ExternalLink aria-hidden="true" /></a> : <p className="ks-missing-value">DOI 未提供</p>}
          <div className="ks-chip-row">{metadata.keywords.map((keyword) => <span className="ks-chip" key={keyword}>{keyword}</span>)}</div>
        </section>
        <section className="ks-drawer-section"><h3>摘要</h3><p className="ks-long-copy">{metadata.abstract || "摘要未提供"}</p></section>{status}
      </div> },
      { id: "extracted", label: "已提取信息", content: <div className="ks-drawer-stack">{status}
        {detail ? <>
          <section className="ks-drawer-section"><label className="ks-field"><span>查看样品</span><select aria-label="文献样品" value={sample?.sample_uid || ""} onChange={(event) => setSampleUid(event.target.value)}>
            {detail.samples.map((item, index) => <option key={item.sample_uid} value={item.sample_uid}>{[item.identity_code, item.name].filter(Boolean).join(" · ") || `未命名样品 ${index + 1}`}</option>)}
          </select></label><p className="ks-missing-value">各样品分别展示；未提供的单位和条件保持缺失。</p></section>
          {sample ? <SampleDetails sample={sample} onNavigate={canNavigateEvidence ? navigateToEvidence : undefined} /> : <p className="ks-missing-value">本次提取未形成可展示的样品信息。</p>}
          {detail.quality_flags.length ? <p className="ks-drawer-callout is-warning">部分字段需要结合原文核验。</p> : null}
        </> : null}
      </div> },
      { id: "source", label: "原文与来源", content: <div className="ks-drawer-stack">{status}
        {detail ? <>
          <section className="ks-drawer-section"><h3><FileText aria-hidden="true" />正文与附件</h3>
            {downloadableAssets.length ? <ul className="ks-asset-list">{downloadableAssets.map((asset) => {
              const name = asset.filename || asset.name || asset.file_name || "文献文件";
              const pdf = (asset.content_type || asset.media_type || "").includes("pdf") || /\.pdf$/i.test(name);
              const unavailable = asset.available === false || (!!asset.status && asset.status !== "available");
              const assetState = asset.status === "unverified" ? "文件尚未验证" : asset.status === "ambiguous" ? "文件存在多份候选，暂不可用" : "文件暂不可用";
              return <li key={asset.asset_uid}><strong>{name}</strong><small>{asset.role === "attachment" ? "补充材料" : asset.role === "full_text" ? "解析正文" : "论文 PDF"}{asset.size_bytes ? ` · ${(asset.size_bytes / 1024).toFixed(1)} KB` : ""}</small>
                {unavailable ? <span className="ks-missing-value">{assetState}</span> : <div className="ks-asset-actions">
                  {pdf ? <a className="ks-button" href={literatureAssetUrl(record.document_uid, record.generation_id, record.snapshot_uid, asset.asset_uid)} target="_blank" rel="noopener noreferrer"><ExternalLink aria-hidden="true" />打开 PDF</a> : null}
                  <a className="ks-button" href={literatureAssetUrl(record.document_uid, record.generation_id, record.snapshot_uid, asset.asset_uid, true)} target="_blank" rel="noopener noreferrer"><Download aria-hidden="true" />下载</a>
                </div>}
              </li>;
            })}</ul> : <p className="ks-missing-value">未提供可访问的 PDF 或附件，已提取信息仍可查看。</p>}
          </section>
          <section className="ks-drawer-section"><h3>全文</h3>
            {!detail.capabilities.has_fulltext ? <p className="ks-missing-value">未提供可读全文。</p> : <>
              {blocks.map((block, index) => {
                const blockId = String(block.block_id ?? block.idx ?? index);
                return <div key={`${blockId}-${index}`} tabIndex={-1} className={pendingBlockId === blockId && exactInputVerified ? "ks-fulltext-block is-evidence-target" : undefined} ref={(element) => { if (element) blockRefs.current.set(blockId, element); else blockRefs.current.delete(blockId); }}><FullTextBlock block={block} index={index} /></div>;
              })}
              {pendingBlockId && fulltextStarted && !fulltextLoading && !exactInputVerified ? <p className="ks-missing-value">正文版本尚未验证，未自动定位原始依据。</p> : null}
              {pendingBlockId && exactInputVerified && !blocks.some((block) => String(block.block_id ?? block.idx) === pendingBlockId) ? <p className="ks-missing-value">该依据不在已加载的正文范围内{cursor ? "，可继续加载正文。" : "，请对照摘录核验。"}</p> : null}
              {fulltextLoading ? <p role="status">正在加载全文…</p> : null}
              {fulltextError ? <div role="alert"><p>{fulltextError}</p><button className="ks-button" type="button" onClick={() => void loadFulltext(cursor ?? undefined)}>重试全文</button></div> : null}
              {cursor && !fulltextLoading && !fulltextError ? <button className="ks-button" type="button" onClick={() => void loadFulltext(cursor)}>加载更多正文</button> : null}
              {fulltextStarted && !fulltextLoading && !fulltextError && !blocks.length ? <p className="ks-missing-value">全文暂无可展示内容。</p> : null}
            </>}
          </section>
          <section className="ks-drawer-section"><h3>提取来源</h3><dl className="ks-detail-grid">
            <div><dt>原论文记录</dt><dd>{detail.extraction.source_paper_id}</dd></div>
            <div><dt>提取记录</dt><dd>{detail.extraction.source_extract_id}</dd></div>
            <div><dt>提取模型</dt><dd>{detail.extraction.model_version || "本次提取未提供"}</dd></div>
            <div><dt>提取时间</dt><dd>{detail.extraction.source_extract_created_at || "本次提取未提供"}</dd></div>
          </dl></section>
        </> : null}
      </div> }
    ]}
  />;
}
