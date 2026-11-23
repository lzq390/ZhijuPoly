import { AlertTriangle, Info } from "lucide-react";
import { useId, useState } from "react";
import type { StructureDescriptorResponse } from "../../types";
import {
  STRUCTURE_FACTS,
  attachmentPointWarning,
  formatStructureFact,
  formulaParts,
  type StructureFactKey
} from "./structureFacts";
import type { StructureDescriptorState } from "./useStructureDescriptors";

type StructureFactsStripProps = {
  state: StructureDescriptorState;
  onOpenHelp: () => void;
};

/** One quiet row of RDKit facts under the structure editor; each fact explains itself on hover, focus or tap. */
export function StructureFactsStrip({ state, onOpenHelp }: StructureFactsStripProps) {
  if (state.status === "idle" || state.status === "no-access") return null;
  const descriptors =
    state.status === "ready" ? state.descriptors : state.status === "loading" ? state.previous : null;
  return (
    <section
      className={`np-hp-facts${state.status === "loading" ? " is-loading" : ""}`}
      aria-label="结构信息"
      aria-busy={state.status === "loading"}
    >
      <span className="np-hp-facts__title" aria-hidden="true">结构信息</span>
      {descriptors ? (
        <FactList descriptors={descriptors} />
      ) : (
        <p className="np-hp-facts__note" role="status">
          {state.status === "loading"
            ? "正在计算…"
            : state.status === "invalid"
              ? "结构无法解析，暂不显示结构信息。"
              : "结构信息暂时无法计算。"}
        </p>
      )}
      <button type="button" className="np-hp-facts__help" aria-label="结构信息说明" onClick={onOpenHelp}>
        <Info aria-hidden="true" />
        <span>说明</span>
      </button>
    </section>
  );
}

function FactList({ descriptors }: { descriptors: StructureDescriptorResponse }) {
  const baseId = useId();
  const [openKey, setOpenKey] = useState<StructureFactKey | null>(null);
  const warning = attachmentPointWarning(descriptors.attachment_point_count);
  return (
    <ul className="np-hp-facts__list">
      {STRUCTURE_FACTS.map((fact, index) => {
        const value = formatStructureFact(fact.key, descriptors);
        const flagged = fact.key === "attachment_point_count" && warning !== null;
        const tipId = `${baseId}-${fact.key}`;
        const edge = index === 0 ? " is-start" : index >= STRUCTURE_FACTS.length - 2 ? " is-end" : "";
        return (
          <li
            key={fact.key}
            className={`np-hp-fact${flagged ? " is-warning" : ""}${openKey === fact.key ? " is-open" : ""}${edge}`}
            onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpenKey(null);
            }}
            onKeyDown={(event) => {
              if (event.key === "Escape" && openKey === fact.key) {
                event.stopPropagation();
                setOpenKey(null);
              }
            }}
          >
            <button
              type="button"
              aria-describedby={tipId}
              aria-label={`${fact.label} ${value}${fact.unit ? ` ${fact.unit}` : ""}${flagged ? "，需要检查" : ""}`}
              onClick={() => setOpenKey((current) => (current === fact.key ? null : fact.key))}
            >
              <span className="np-hp-fact__label">{fact.label}</span>
              <span className="np-hp-fact__value">
                {fact.key === "formula"
                  ? formulaParts(value).map((part, partIndex) =>
                      part.sub ? <sub key={partIndex}>{part.text}</sub> : <span key={partIndex}>{part.text}</span>
                    )
                  : value}
                {fact.unit ? <small>{fact.unit}</small> : null}
                {flagged ? <AlertTriangle aria-hidden="true" /> : null}
              </span>
            </button>
            <span role="tooltip" id={tipId} className="np-hp-fact__tip">
              <b>{fact.label}{fact.unit ? `（${fact.unit}）` : ""}</b>
              {fact.explanation}
              {flagged ? <strong>{warning}</strong> : null}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
