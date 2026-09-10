import { useId, type CSSProperties } from "react";
import { Check, Circle, Square, Star, X } from "lucide-react";
import { highThroughputDemoScenario } from "../../constants/highThroughputDemoScenario";
import { projectRatioMixPoint, ratioGridPoints, RATIO_SPACE_ANCHORS, type RatioSearchSummary } from "./ratio-search-model";

// Label positions are fixed too; leaders separate the closely spaced compositions
// without moving their actual coordinates. HTML labels retain the theme's font size.
const labelPositions: Record<string, { x: number; y: number }> = {
  "mix-0": { x: 35, y: 27 }, "mix-1": { x: 25, y: 53 }, "mix-4": { x: 42, y: 76 },
  "mix-6": { x: 67, y: 54 }, "mix-5": { x: 64, y: 28 },
};

export function RatioSearchMap({ summary }: { summary: RatioSearchSummary }) {
  const id = useId().replace(/:/g, "");
  const bestPoint = projectRatioMixPoint(summary.best);
  const proposedPoint = projectRatioMixPoint(summary.proposed);
  const previousPoint = projectRatioMixPoint(summary.previous);
  function stateFor(mixId: string) {
    return mixId === summary.proposed.id && !summary.decisionVisible ? "pending"
      : summary.rejectedIds.has(mixId) ? "rejected" : mixId === summary.best.id ? "best" : "accepted";
  }
  const states = { pending: "待确认", rejected: "预设拒绝", best: "路径最优", accepted: "已接受" };

  return <div className="ht-s5-map-block">
    <div className="ht-s5-ratio-map" role="img" aria-label={`T${summary.index} 配比搜索投影：${summary.visibleMixes.map((mix) => `${mix.id} ${states[stateFor(mix.id)]}`).join("；")}`}>
      <svg viewBox="0 0 100 48" preserveAspectRatio="none" aria-hidden="true">
        <defs><radialGradient id={`${id}-terrain`}><stop stopColor="#6091d5" stopOpacity=".28" /><stop offset=".58" stopColor="#81b8d1" stopOpacity=".13" /><stop offset="1" stopColor="#fff" stopOpacity="0" /></radialGradient></defs>
        <polygon points="20,12 78,13 80,37 22,36" className="ht-s5-map-domain" />
        <ellipse cx={bestPoint.x} cy={bestPoint.y} rx="23" ry="14" fill={`url(#${id}-terrain)`} />
        {[1, 1.6, 2.2].map((scale) => <ellipse key={scale} cx={bestPoint.x} cy={bestPoint.y} rx={6 * scale} ry={3.4 * scale} className="ht-s5-map-contour" />)}
        {ratioGridPoints.map((point) => <circle key={point.id} cx={point.x} cy={point.y} r=".23" className="ht-s5-map-grid-point" />)}
        <polyline points={summary.acceptedPath.map((mix) => { const point = projectRatioMixPoint(mix); return `${point.x},${point.y}`; }).join(" ")} className="ht-s5-map-path" />
        {!summary.isSeed && <line x1={previousPoint.x} y1={previousPoint.y} x2={proposedPoint.x} y2={proposedPoint.y}
          className={`ht-s5-map-proposal ${stateFor(summary.proposed.id)}`} />}
        {summary.visibleMixes.map((mix) => {
          const point = projectRatioMixPoint(mix);
          const label = labelPositions[mix.id];
          const state = stateFor(mix.id);
          return <g key={mix.id} className={`ht-s5-map-node ${state}`}>
            <line x1={point.x} y1={point.y} x2={label.x} y2={label.y * .48} className="ht-s5-map-leader" />
            {state === "best" ? <path d={`M ${point.x} ${point.y - 1.1} l .32 .7 .78 .08 -.58 .5 .17 .75 -.69 -.39 -.69 .39 .17 -.75 -.58 -.5 .78 -.08 Z`} />
              : state === "rejected" ? <path d={`M ${point.x - .65} ${point.y - .65} l 1.3 1.3 m 0 -1.3 l -1.3 1.3`} />
                : <circle cx={point.x} cy={point.y} r=".62" />}
          </g>;
        })}
        {Object.values(RATIO_SPACE_ANCHORS).map((point, index) => <rect key={index} x={point.x - .55} y={point.y - .55} width="1.1" height="1.1" fill={highThroughputDemoScenario.formulation.components[index].color} />)}
      </svg>
      {highThroughputDemoScenario.formulation.components.map((component, index) => <span key={component.id} className={`ht-s5-map-anchor anchor-${index + 1}`} style={{ "--component-color": component.color } as CSSProperties}><i /><b>{component.id}</b><span>{highThroughputDemoScenario.targets.find((target) => target.key === component.sourceTargetKey)?.shortLabel}</span></span>)}
      {summary.visibleMixes.map((mix) => {
        const state = stateFor(mix.id);
        const label = labelPositions[mix.id];
        return <span key={mix.id} className={`ht-s5-map-label ${state}`} style={{ left: `${label.x}%`, top: `${label.y}%` }}>
          {state === "best" ? <Star aria-hidden="true" /> : state === "rejected" ? <X aria-hidden="true" /> : state === "pending" ? <Circle aria-hidden="true" /> : <Check aria-hidden="true" />}<b>{mix.id}</b>
        </span>;
      })}
    </div>
    <div className="ht-s5-map-legend"><span><i />比例候选</span><span><Circle aria-hidden="true" />待验证</span><span><Check aria-hidden="true" />接受</span><span><X aria-hidden="true" />拒绝</span><span><Star aria-hidden="true" />路径最优</span><span><Square aria-hidden="true" />固定组分</span></div>
    <p className="ht-s5-map-note">固定配比二维投影 · 不同配比可能重叠。地形仅为预设区域示意，非预测值或置信概率。</p>
    <dl className="ht-s5-map-metrics"><div><dt>已展示决策</dt><dd>{summary.evaluatedCount}<small> / <b>{highThroughputDemoScenario.formulation.searchSteps.length}</b></small></dd></div><div><dt>当前路径解</dt><dd>{summary.current.id}</dd></div><div><dt>预设路径最优</dt><dd>{summary.best.id}<small><b>{summary.best.score}</b> 分</small></dd></div></dl>
  </div>;
}
