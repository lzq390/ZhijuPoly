/** Keep mixed scientific text in the body font and style only its numbers. */
export function PolymerMdNumericText({ children }: { children: string }) {
  return <span className="np-pmd-mixed-value">{children.split(/(\d+(?:[,.]\d+)*)/g).map((part, index) =>
    index % 2 ? <span className="np-pmd-numeric-value" key={index}>{part}</span> : part
  )}</span>;
}
